import os
import requests
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ==== Settings ====
BINANCE_API = "https://api.binance.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RSI_PERIOD = 14
reported_retests = set()

MIN_CANDLES_AFTER_BREAKOUT = 5  # Min uptrend duration

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
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=2)
session.mount("https://", adapter)

# ==== Telegram ====
def send_telegram(msg, max_retries=3):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("✗ ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set!")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    for attempt in range(max_retries):
        try:
            response = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg[:3900]
            }, timeout=10)
            
            if response.status_code == 200:
                print("✓ Telegram alert sent")
                return True
            else:
                print(f"✗ Telegram API error ({response.status_code}): {response.text}")
                return False
        except Exception as e:
            print(f"✗ Telegram exception (attempt {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
    return False

# ==== Utils ====
def format_volume(v):
    if v >= 1_000_000:
        return f"{v/1_000_000:.0f}M"
    else:
        return f"{v/1_000:.0f}K"

def get_binance_server_time():
    try:
        return session.get(f"{BINANCE_API}/api/v3/time", timeout=5).json()["serverTime"] / 1000
    except:
        return time.time()

# ==== Indicators ====
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)

def calculate_atr(candles, period=10):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i-1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr

def calculate_supertrend(candles, atr_period=10, multiplier=3.0):
    if len(candles) < atr_period + 1:
        return None
    up_list = []
    dn_list = []
    trend_list = []
    for idx in range(atr_period, len(candles)):
        high = float(candles[idx][2])
        low = float(candles[idx][3])
        close = float(candles[idx][4])
        src = (high + low) / 2
        atr = calculate_atr(candles[:idx+1], atr_period)
        if atr is None:
            return None
        up = src - (multiplier * atr)
        up1 = up_list[-1] if up_list else up
        prev_close = float(candles[idx-1][4])
        if prev_close > up1:
            up = max(up, up1)
        up_list.append(up)
        dn = src + (multiplier * atr)
        dn1 = dn_list[-1] if dn_list else dn
        if prev_close < dn1:
            dn = min(dn, dn1)
        dn_list.append(dn)
        if idx == atr_period:
            trend = 1
        else:
            prev_trend = trend_list[-1]
            prev_up = up_list[-2]
            prev_dn = dn_list[-2]
            if prev_trend == -1 and close > prev_dn:
                trend = 1
            elif prev_trend == 1 and close < prev_up:
                trend = -1
            else:
                trend = prev_trend
        trend_list.append(trend)
    return {'up_list': up_list, 'dn_list': dn_list, 'trend_list': trend_list}

# ==== Binance ====
def get_usdt_pairs():
    candidates = list(dict.fromkeys([t.upper() + "USDT" for t in CUSTOM_TICKERS]))
    try:
        data = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=10).json()
        valid = {s["symbol"] for s in data["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        pairs = [c for c in candidates if c in valid]
        print(f"✓ Loaded {len(pairs)} valid USDT pairs")
        return pairs
    except Exception as e:
        print(f"✗ Exchange info error: {e}")
        return []

# ==== Detection: TRUE SUPPORT REJECTION ====
def detect_retest(symbol):
    try:
        # Changed to 15m interval and increased limit for more data
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=100"
        candles = session.get(url, timeout=5).json()
        if not candles or isinstance(candles, dict) or len(candles) < 30:
            return None

        last_idx = len(candles) - 2
        last_candle = candles[last_idx]
        candle_time = datetime.fromtimestamp(last_candle[0]/1000, tz=timezone.utc)
        time_str = candle_time.strftime("%Y-%m-%d %H:%M")

        st_result = calculate_supertrend(candles[:last_idx+1])
        if not st_result:
            return None

        trend_list = st_result['trend_list']
        up_list = st_result['up_list']

        # Must be in uptrend
        if trend_list[-1] != 1:
            return None

        # Min uptrend duration
        uptrend_candles = 0
        for i in range(len(trend_list) - 1, -1, -1):
            if trend_list[i] == 1:
                uptrend_candles += 1
            else:
                break
        if uptrend_candles < MIN_CANDLES_AFTER_BREAKOUT:
            return None

        current_support = up_list[-1]
        low = float(last_candle[3])
        close = float(last_candle[4])

        # 🔑 CRITICAL: True retest logic
        touched_or_broken = (low <= current_support)      # Low touched or broke support
        closed_above = (close > current_support)         # Closed above = bullish rejection

        if not (touched_or_broken and closed_above):
            return None

        # Optional: ensure price was moving toward support (not away)
        recent_distances = []
        for i in range(max(0, last_idx - 2), last_idx + 1):
            if i < 11:
                continue
            c = float(candles[i][4])
            s = up_list[i - 11]
            d = ((c - s) / s) * 100
            recent_distances.append(d)

        if len(recent_distances) >= 2 and recent_distances[-1] > recent_distances[0]:
            return None  # Price moving away from support

        # Final data
        prev_close = float(candles[last_idx - 1][4])
        pct = ((close - prev_close) / prev_close) * 100

        return {
            'symbol': symbol,
            'time_str': time_str,
            'pct': pct,
            'close': close,
            'support_line': current_support,
            'distance_from_support': ((close - current_support) / current_support) * 100,
            'uptrend_candles': uptrend_candles
        }

    except Exception as e:
        return None

# ==== RSI & VM Calculation ====
def calculate_rsi_and_vm(symbol):
    try:
        # Changed to 15m interval and increased limit for proper RSI calculation
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=100"
        candles = session.get(url, timeout=5).json()
        if not candles or isinstance(candles, dict) or len(candles) < 20:
            return None, None, None

        last_idx = len(candles) - 2
        last_candle = candles[last_idx]
        open_p = float(last_candle[1])
        volume = float(last_candle[5])
        vol_usdt = open_p * volume

        all_closes = [float(candles[j][4]) for j in range(0, last_idx + 1)]
        rsi = calculate_rsi(all_closes, RSI_PERIOD)

        # Calculate 20-period volume MA (adjust to 15m timeframe - approx 5 hours of data)
        ma_start = max(0, last_idx - 19)
        ma_vol = [float(candles[j][1]) * float(candles[j][5]) for j in range(ma_start, last_idx + 1)]
        ma = sum(ma_vol) / len(ma_vol)
        vm = vol_usdt / ma if ma > 0 else 1.0

        return rsi, vm, vol_usdt
    except:
        return None, None, None

# ==== Scanning ====
def scan_all_symbols(symbols):
    retest_candidates = []
    with ThreadPoolExecutor(max_workers=150) as ex:
        futures = {ex.submit(detect_retest, s): s for s in symbols}
        for f in as_completed(futures):
            data = f.result()
            if data:
                retest_candidates.append(data)

    retests_final = []
    if retest_candidates:
        with ThreadPoolExecutor(max_workers=50) as ex:
            futures = {ex.submit(calculate_rsi_and_vm, d['symbol']): d for d in retest_candidates}
            for f in as_completed(futures):
                rsi, vm, vol_usdt = f.result()
                data = futures[f]
                if rsi is not None and vm is not None:
                    retests_final.append((
                        data['symbol'],
                        data['pct'],
                        data['close'],
                        vol_usdt,
                        vm,
                        rsi,
                        data['support_line'],
                        data['distance_from_support'],
                        data['uptrend_candles'],
                        data['time_str']
                    ))
    return retests_final

# ==== Compact Output (Your Exact Format) ====
def format_compact_retest_report(retests, duration):
    if not retests:
        return None

    grouped = defaultdict(list)
    for r in retests:
        grouped[r[9]].append(r)

    lines = []
    lines.append(f"🎯 RETESTS (15M) | Found: {len(retests)} | Scan: {duration:.1f}s")

    for h in sorted(grouped, reverse=True):
        # Sort by distance (closest = best)
        sorted_items = sorted(grouped[h], key=lambda x: x[7])
        for item in sorted_items:
            symbol, pct, close, vol_usdt, vm, rsi, support_line, distance, uptrend_candles, time_str = item
            sym = symbol.replace("USDT", "")[:6]

            line = (
                f"{sym:>6s} "
                f"{pct:5.2f} "
                f"{rsi:4.1f} "
                f"{vm:4.1f}x "
                f"{format_volume(vol_usdt):>4s} "
                f"{distance:4.2f} "
                f"ST:{support_line:.6f}"
            )
            lines.append(line)

    lines.append("💡 True support rejection: Low ≤ Support & Close > Support")
    return "\n".join(lines)

# ==== Main Loop ====
def main():
    print("="*80)
    print("🎯 BULLISH REJECTION SCANNER (15M) - TRUE SUPPORT TEST")
    print("="*80)

    symbols = get_usdt_pairs()
    if not symbols:
        print("❌ No symbols loaded. Exiting.")
        return

    print(f"✓ Monitoring {len(symbols)} pairs\n")

    while True:
        now = datetime.now(timezone.utc)
        print(f"\n{'='*80}")
        print(f"🕐 Scan started: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"{'='*80}\n")

        total_start = time.time()
        retests = scan_all_symbols(symbols)
        total_duration = time.time() - total_start

        fresh_retests = [r for r in retests if (r[0], r[9]) not in reported_retests]
        for r in fresh_retests:
            reported_retests.add((r[0], r[9]))

        print(f"✓ Scan done in {total_duration:.2f}s | New alerts: {len(fresh_retests)}")

        if fresh_retests:
            msg = format_compact_retest_report(fresh_retests, total_duration)
            if msg:
                print("\n📤 Sending alert...")
                print("\n" + "="*60)
                print(msg)
                print("="*60)
                send_telegram(msg)

        # Changed sleep timing for 15-minute intervals
        server_time = get_binance_server_time()
        next_15min = (server_time // 900 + 1) * 900  # 900 seconds = 15 minutes
        sleep_time = max(30, next_15min - server_time + 5)
        print(f"\n😴 Sleeping {sleep_time:.0f}s until next 15-min candle...\n")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
