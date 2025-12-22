import requests
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ==== Settings ====
BINANCE_API = "https://api.binance.com"
TELEGRAM_BOT_TOKEN = "7913078821:AAH_jUTHXlFx66daqBkYY7mKw7UZnwpp_A0"
TELEGRAM_CHAT_ID = "1692583809"
PUMP_THRESHOLD = 2.9  # percent
reported = set()  # use a set of (symbol, hour) to avoid duplicates

CUSTOM_TICKERS = ["At", "A2Z", "ACE", "ACH", "ACT", "ADA", "ADX", "AGLD", "AIXBT", "Algo", "ALICE", "ALPINE", "ALT", "AMP", "ANKR", "APE", "API3", "APT", "AR", "ARB", "ARDR", "Ark", "ARKM", "ARPA", "ASTR", "Ata", "ATOM", "AVA", "AVAX", "AWE", "AXL", "BANANA", "BAND", "BAT", "BCH", "BEAMX", "BICO", "BIO", "Blur", "BMT", "Btc", "CELO", "Celr", "CFX", "CGPT", "CHR", "CHZ", "CKB", "COOKIE", "Cos", "CTSI", "CVC", "Cyber", "Dash", "DATA", "DCR", "Dent", "DeXe", "DGB", "DIA", "DOGE", "DOT", "DUSK", "EDU", "EGLD", "ENJ", "ENS", "EPIC", "ERA", "ETC", "ETH", "FET", "FIDA", "FIL", "fio", "Flow", "Flux", "Gala", "Gas", "GLM", "GLMR", "GMT", "GPS", "GRT", "GTC", "HBAR", "HEI", "HIGH", "Hive", "HOOK", "HOT", "HYPER", "ICP", "ICX", "ID", "IMX", "INIT", "IO", "IOST", "IOTA", "IOTX", "IQ", "JASMY", "Kaia", "KAITO", "KSM", "la", "layer", "LINK", "LPT", "LRC", "LSK", "LTC", "LUNA", "MAGIC", "MANA", "Manta", "Mask", "MDT", "ME", "Metis", "Mina", "MOVR", "MTL", "NEAR", "NEWT", "NFP", "NIL", "NKN", "NTRN", "OM", "ONE", "ONG", "OP", "ORDI", "OXT", "PARTI", "PAXG", "PHA", "PHB", "PIVX", "Plume", "POL", "POLYX", "POND", "Portal", "POWR", "Prom", "PROVE", "PUNDIX", "Pyth", "QKC", "QNT", "Qtum", "RAD", "RARE", "REI", "Render", "REQ", "RIF", "RLC", "Ronin", "ROSE", "Rsr", "RVN", "Saga", "SAHARA", "SAND", "SC", "SCR", "SCRT", "SEI", "SFP", "SHELL", "Sign", "SKL", "Sol", "SOPH", "Ssv", "Steem", "Storj", "STRAX", "STX", "Sui", "SXP", "SXT", "SYS", "TAO", "TFUEL", "Theta", "TIA", "TNSR", "TON", "TOWNS", "TRB", "TRX", "TWT", "Uma", "UTK", "Vana", "VANRY", "VET", "VIC", "VIRTUAL", "VTHO", "WAXP", "WCT", "win", "WLD", "Xai", "XEC", "XLM", "XNO", "XRP", "XTZ", "XVG", "Zec", "ZEN", "ZIL", "ZK", "ZRO", "0G", "2Z", "C", "D", "ENSO", "G", "HOLO", "KITE", "LINEA", "MIRA", "OPEN", "S", "SAPIEN", "SOMI", "W", "WAL", "XPL", "ZBT", "ZKC"]

# ==== Session for connection pooling ====
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
session.mount('https://', adapter)

# ==== Telegram ====
def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, data=payload, timeout=60)
    except Exception as e:
        print("Telegram error:", e)

# ==== Volume Formatter ====
def format_volume(vol):
    if vol >= 1_000_000:
        return f"{vol / 1_000_000:.2f}M"
    elif vol >= 1_000:
        return f"{vol / 1_000:.2f}K"
    else:
        return str(int(vol))

# ==== Binance Helpers ====
def get_binance_server_time():
    try:
        url = f"{BINANCE_API}/api/v3/time"
        data = session.get(url, timeout=60).json()
        return int(data["serverTime"]) / 1000
    except Exception as e:
        print("Error fetching Binance time:", e)
        return time.time()

def get_usdt_pairs():
    candidate_symbols = [ticker.strip().upper() + "USDT" for ticker in CUSTOM_TICKERS]
    candidate_symbols = list(dict.fromkeys(candidate_symbols))
    try:
        url = f"{BINANCE_API}/api/v3/exchangeInfo"
        data = session.get(url, timeout=60).json()
        valid_symbols = {
            s["symbol"] for s in data["symbols"]
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
        }
        filtered = [sym for sym in candidate_symbols if sym in valid_symbols]
        print(f"Loaded {len(filtered)} valid USDT pairs.")
        return filtered
    except Exception as e:
        print("Error fetching Binance exchange info:", e)
        return []

def fetch_pump_candles(symbol, now_utc, start_time):
    """Fetch 46 candles and detect pumps with volume/CR stats"""
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=46"
        candles = session.get(url, timeout=60).json()
        if not candles or isinstance(candles, dict):
            return []

        results = []
        for i, c in enumerate(candles):
            candle_time = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc)
            if candle_time < start_time or candle_time >= now_utc - timedelta(hours=1):
                continue

            open_price = float(c[1])
            high_price = float(c[2])
            low_price = float(c[3])
            close_price = float(c[4])
            volume_base = float(c[5])
            volume_usdt = open_price * volume_base

            change_pct = ((close_price - open_price) / open_price) * 100
            if change_pct < PUMP_THRESHOLD:
                continue

            # Close Ratio
            candle_range = high_price - low_price
            close_ratio = ((close_price - low_price) / candle_range) * 100 if candle_range > 0 else 50.0

            # 20h MA Volume (use up to current candle)
            ma_start = max(0, i - 19)
            ma_volumes = [float(candles[j][1]) * float(candles[j][5]) for j in range(ma_start, i + 1)]
            ma_volume = sum(ma_volumes) / len(ma_volumes) if ma_volumes else volume_usdt
            vol_ratio = volume_usdt / ma_volume if ma_volume > 0 else 1.0

            hour_label = candle_time.strftime("%Y-%m-%d %H:00")
            results.append((symbol, change_pct, close_price, hour_label, volume_usdt, close_ratio, vol_ratio))

        return results
    except Exception as e:
        print(f"Error scanning {symbol}: {e}")
        return []

def check_pumps(symbols):
    now_utc = datetime.now(timezone.utc)
    start_time = (now_utc - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)

    all_pumps = []
    with ThreadPoolExecutor(max_workers=60) as executor:
        futures = {executor.submit(fetch_pump_candles, s, now_utc, start_time): s for s in symbols}
        for f in as_completed(futures):
            res = f.result()
            if res:
                all_pumps.extend(res)
    return all_pumps

# ==== Report Formatting ====
def format_report(pumps, duration_sec):
    grouped = defaultdict(list)
    for s, pct, close, hour, vol, ratio, vol_ratio in pumps:
        grouped[hour].append((s, pct, close, vol, ratio, vol_ratio))

    report = f"⏱ {duration_sec:.2f}s\n\n"
    for hour in sorted(grouped.keys()):
        sorted_list = sorted(grouped[hour], key=lambda x: x[5], reverse=True)  # by volume
        report += f"=== {hour} UTC ===\n"
        max_sym_len = max(len(item[0].replace("USDT", "")) for item in sorted_list) if sorted_list else 6
        for s, pct, close, vol, ratio, vol_ratio in sorted_list:
            clean = s.replace("USDT", "")
            vol_str = format_volume(vol)
            marker = "🔥" if vol_ratio >= 1.5 and ratio >= 80 else "  "
            report += f"{marker}{clean:<{max_sym_len}} {pct:5.1f}% │ Close: {close:g}\n"
            report += f"{'':>{max_sym_len+2}} V:{vol_str} │ VM:{vol_ratio:.1f}x │ CR:{ratio:.0f}%\n"
        report += "\n"
    return report

# ==== Main Loop ====
def main():
    symbols = get_usdt_pairs()
    if not symbols:
        print("❌ No valid symbols. Exiting.")
        return
    print(f"Monitoring {len(symbols)} symbols...")

    interval_seconds = 3600

    while True:
        now = datetime.now(timezone.utc)
        print(f"\n=== Scanning at {now} UTC ===")

        start_time = time.time()
        raw_pumps = check_pumps(symbols)
        duration = time.time() - start_time

        # Filter out already reported (symbol, hour)
        new_pumps = []
        for p in raw_pumps:
            key = (p[0], p[3])  # (symbol, hour_label)
            if key not in reported:
                reported.add(key)
                new_pumps.append(p)

        if new_pumps:
            report = format_report(new_pumps, duration)
            print(report)
            send_telegram(report[:4096])
        else:
            msg = f"No pumps ≥{PUMP_THRESHOLD}% this hour. ⏱ {duration:.2f}s"
            print(msg)
            # Optionally send to Telegram: send_telegram(msg)

        # Wait until next full hour + 1 sec
        server_time = get_binance_server_time()
        next_close = (server_time // interval_seconds + 1) * interval_seconds
        wait_time = next_close - server_time + 1
        time.sleep(max(0, wait_time))

if __name__ == "__main__":
    main()
