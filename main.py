import os
import requests
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ==== Settings ====
BINANCE_API = "https://api.binance.com"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PUMP_THRESHOLD = 2.9  # percent
reported = set()  # avoid duplicate (symbol, hour)

CUSTOM_TICKERS = [
    "At","A2Z","ACE","ACH","ACT","ADA","ADX","AGLD","AIXBT","Algo","ALICE","ALPINE","ALT","AMP","ANKR","APE",
    "API3","APT","AR","ARB","ARDR","Ark","ARKM","ARPA","ASTR","Ata","ATOM","AVA","AVAX","AWE","AXL","BANANA",
    "BAND","BAT","BCH","BEAMX","BICO","BIO","Blur","BMT","Btc","CELO","Celr","CFX","CGPT","CHR","CHZ","CKB",
    "COOKIE","Cos","CTSI","CVC","Cyber","Dash","DATA","DCR","Dent","DeXe","DGB","DIA","DOGE","DOT","DUSK",
    "EDU","EGLD","ENJ","ENS","EPIC","ERA","ETC","ETH","FET","FIDA","FIL","fio","Flow","Flux","Gala","Gas",
    "GLM","GLMR","GMT","GPS","GRT","GTC","HBAR","HEI","HIGH","Hive","HOOK","HOT","HYPER","ICP","ICX","ID",
    "IMX","INIT","IO","IOST","IOTA","IOTX","IQ","JASMY","Kaia","KAITO","KSM","la","layer","LINK","LPT","LRC",
    "LSK","LTC","LUNA","MAGIC","MANA","Manta","Mask","MDT","ME","Metis","Mina","MOVR","MTL","NEAR","NEWT",
    "NFP","NIL","NKN","NTRN","OM","ONE","ONG","OP","ORDI","OXT","PARTI","PAXG","PHA","PHB","PIVX","Plume",
    "POL","POLYX","POND","Portal","POWR","Prom","PROVE","PUNDIX","Pyth","QKC","QNT","Qtum","RAD","RARE",
    "REI","Render","REQ","RIF","RLC","Ronin","ROSE","Rsr","RVN","Saga","SAHARA","SAND","SC","SCR","SCRT",
    "SEI","SFP","SHELL","Sign","SKL","Sol","SOPH","Ssv","Steem","Storj","STRAX","STX","Sui","SXP","SXT",
    "SYS","TAO","TFUEL","Theta","TIA","TNSR","TON","TOWNS","TRB","TRX","TWT","Uma","UTK","Vana","VANRY",
    "VET","VIC","VIRTUAL","VTHO","WAXP","WCT","win","WLD","Xai","XEC","XLM","XNO","XRP","XTZ","XVG","Zec",
    "ZEN","ZIL","ZK","ZRO","0G","2Z","C","D","ENSO","G","HOLO","KITE","LINEA","MIRA","OPEN","S","SAPIEN",
    "SOMI","W","WAL","XPL","ZBT","ZKC"
]

# ==== Session ====
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
session.mount("https://", adapter)

# ==== Telegram ====
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=60)
    except Exception as e:
        print("Telegram error:", e)

# ==== Utils ====
def format_volume(v):
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v/1_000:.2f}K"
    return str(int(v))

def get_binance_server_time():
    try:
        return session.get(f"{BINANCE_API}/api/v3/time", timeout=60).json()["serverTime"] / 1000
    except:
        return time.time()

# ==== Binance ====
def get_usdt_pairs():
    candidates = list(dict.fromkeys([t.upper() + "USDT" for t in CUSTOM_TICKERS]))
    try:
        data = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=60).json()
        valid = {s["symbol"] for s in data["symbols"]
                 if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        pairs = [c for c in candidates if c in valid]
        print(f"Loaded {len(pairs)} valid USDT pairs.")
        return pairs
    except Exception as e:
        print("Exchange info error:", e)
        return []

def fetch_pump_candles(symbol, now_utc, start_time):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=46"
        candles = session.get(url, timeout=60).json()
        if not candles or isinstance(candles, dict):
            return []

        results = []
        for i, c in enumerate(candles):
            candle_time = datetime.fromtimestamp(c[0]/1000, tz=timezone.utc)
            if candle_time < start_time or candle_time >= now_utc - timedelta(hours=1):
                continue

            open_p = float(c[1])
            high = float(c[2])
            low = float(c[3])
            close = float(c[4])
            volume = float(c[5])
            vol_usdt = open_p * volume

            pct = ((close - open_p) / open_p) * 100
            if pct < PUMP_THRESHOLD:
                continue

            candle_range = high - low
            cr = ((close - low) / candle_range) * 100 if candle_range > 0 else 50

            ma_start = max(0, i - 19)
            ma_vol = [
                float(candles[j][1]) * float(candles[j][5])
                for j in range(ma_start, i + 1)
            ]
            ma = sum(ma_vol) / len(ma_vol)
            vm = vol_usdt / ma if ma > 0 else 1.0

            # === Prices ===
            buy = (open_p + close) / 2
            sell = buy * 1.022
            sl = buy * 0.99

            hour = candle_time.strftime("%Y-%m-%d %H:00")
            results.append((symbol, pct, close, buy, sell, sl, hour, vol_usdt, cr, vm))

        return results
    except Exception as e:
        print(f"{symbol} error:", e)
        return []

def check_pumps(symbols):
    now_utc = datetime.now(timezone.utc)
    start_time = (now_utc - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    pumps = []

    with ThreadPoolExecutor(max_workers=60) as ex:
        for f in as_completed([ex.submit(fetch_pump_candles, s, now_utc, start_time) for s in symbols]):
            pumps.extend(f.result() or [])

    return pumps

# ==== Report ====
def format_report(pumps, duration):
    grouped = defaultdict(list)
    for s, pct, c, b, se, sl, h, v, cr, vm in pumps:
        grouped[h].append((s, pct, c, b, se, sl, v, cr, vm))

    report = f"⏱ {duration:.2f}s\n\n"
    for h in sorted(grouped):
        items = sorted(grouped[h], key=lambda x: x[8], reverse=True)
        report += f"=== {h} UTC ===\n"
        maxlen = max(len(i[0].replace("USDT","")) for i in items)

        for s, pct, c, b, se, sl, v, cr, vm in items:
            sym = s.replace("USDT","")
            mark = "🔥" if vm >= 1.5 and cr >= 80 else "  "
            report += f"{mark}{sym:<{maxlen}} {pct:5.1f}% │ C:{c:g}\n"
            report += f"{'':>{maxlen+2}} B:{b:.5g} │ S:{se:.5g} │ SL:{sl:.5g}\n"
            report += f"{'':>{maxlen+2}} V:{format_volume(v)} │ VM:{vm:.1f}x │ CR:{cr:.0f}%\n"
        report += "\n"

    return report

# ==== Main ====
def main():
    symbols = get_usdt_pairs()
    if not symbols:
        return

    while True:
        start = time.time()
        pumps = check_pumps(symbols)
        duration = time.time() - start

        fresh = []
        for p in pumps:
            key = (p[0], p[6])
            if key not in reported:
                reported.add(key)
                fresh.append(p)

        if fresh:
            msg = format_report(fresh, duration)
            print(msg)
            send_telegram(msg[:4096])
        else:
            print("No pumps this hour.")

        server = get_binance_server_time()
        next_hour = (server // 3600 + 1) * 3600
        time.sleep(max(0, next_hour - server + 1))

if __name__ == "__main__":
    main()
