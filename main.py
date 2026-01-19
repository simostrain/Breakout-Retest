import os
import requests
import time
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ==== Settings ====
BINANCE_API = "https://api.binance.com"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RSI_PERIOD = 14
reported_signals = set()

ATR_PERIOD = 10
MULTIPLIER = 3.0

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
    "SOMI","W","WAL","XPL","ZBT","ZKC","BREV","ZKP"
]

LOG_FILE = Path("/tmp/supertrend_retests_only.json")

session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=2)
session.mount("https://", adapter)

def format_volume(v):
    return f"{v/1_000_000:.2f}"

# ==== Standard ATR (SMA) ====
def calculate_atr(candles, period):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i][2])
        l = float(candles[i][3])
        c_prev = float(candles[i-1][4])
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)
    atr = [None] * len(candles)
    atr[period] = sum(trs[:period]) / period
    for i in range(period + 1, len(candles)):
        atr[i] = (atr[i-1] * (period - 1) + trs[i-1]) / period
    return atr

# ==== Standard SuperTrend (Pine v6 logic) ====
def calculate_supertrend(candles, atr_period=10, multiplier=3.0):
    n = len(candles)
    if n < atr_period + 1:
        return None
    atr_vals = calculate_atr(candles, atr_period)
    if atr_vals is None:
        return None

    up = [0.0] * n
    dn = [0.0] * n
    direction = [1] * n

    for i in range(atr_period, n):
        high = float(candles[i][2])
        low = float(candles[i][3])
        src = (high + low) / 2.0
        atr = atr_vals[i] or 1e-8

        basic_upper = src - multiplier * atr
        basic_lower = src + multiplier * atr

        if i == atr_period:
            up[i] = basic_upper
            dn[i] = basic_lower
        else:
            up[i] = max(basic_upper, up[i-1]) if direction[i-1] == 1 else basic_upper
            dn[i] = min(basic_lower, dn[i-1]) if direction[i-1] == -1 else basic_lower

        close = float(candles[i][4])
        if close > dn[i-1]:
            direction[i] = 1
        elif close < up[i-1]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]

    supertrend = [up[i] if direction[i] == 1 else dn[i] for i in range(n)]
    return {
        'supertrend': supertrend,
        'direction': direction,
        'up': up,
        'dn': dn,
        'atr': atr_vals
    }

# ==== Bullish Patterns ====
def is_bullish_engulfing(candles, idx):
    if idx < 1: return False
    o1, c1 = float(candles[idx-1][1]), float(candles[idx-1][4])
    o2, c2 = float(candles[idx][1]), float(candles[idx][4])
    return c1 < o1 and c2 > o2 and o2 < c1 and c2 > o1

def is_hammer(candles, idx):
    o, h, l, c = float(candles[idx][1]), float(candles[idx][2]), float(candles[idx][3]), float(candles[idx][4])
    body = abs(c - o)
    if body == 0: return False
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return lower_wick >= 2 * body and upper_wick <= body and c > (h + l) / 2

def is_piercing_line(candles, idx):
    if idx < 1: return False
    o1, c1 = float(candles[idx-1][1]), float(candles[idx-1][4])
    o2, c2 = float(candles[idx][1]), float(candles[idx][4])
    return c1 < o1 and c2 > o2 and c2 > (o1 + c1) / 2

def is_bullish_pin_bar(candles, idx):
    o, h, l, c = float(candles[idx][1]), float(candles[idx][2]), float(candles[idx][3]), float(candles[idx][4])
    body = abs(c - o)
    if body == 0: return False
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return lower_wick >= 2 * body and upper_wick <= body and c > o and c > (h + l) / 2

def has_bullish_reversal_pattern(candles, idx, support_line):
    support_buffer = support_line * 0.005
    low = float(candles[idx][3])
    if low > support_line + support_buffer:
        return None
    if is_bullish_engulfing(candles, idx): return "Bullish Engulfing"
    if is_piercing_line(candles, idx): return "Piercing Line"
    if is_hammer(candles, idx): return "Hammer"
    if is_bullish_pin_bar(candles, idx): return "Bullish Pin Bar"
    return None

# ==== RSI ====
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)

# ==== Fetch Pairs ====
def get_usdt_pairs():
    candidates = [t.upper() + "USDT" for t in CUSTOM_TICKERS]
    try:
        data = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=10).json()
        valid = {s["symbol"] for s in data["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        return [c for c in candidates if c in valid]
    except:
        return []

# ==== Detect Retests Only ====
def detect_retest(symbol):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=100"
        candles = session.get(url, timeout=5).json()
        if not candles or len(candles) < 30:
            return None

        last_idx = len(candles) - 2
        prev_candle = candles[last_idx - 1]
        curr_candle = candles[last_idx]

        candle_time = datetime.fromtimestamp(curr_candle[0]/1000, tz=timezone.utc)
        hour = candle_time.strftime("%Y-%m-%d %H:%M")

        prev_close = float(prev_candle[4])
        open_p = float(curr_candle[1])
        low = float(curr_candle[3])
        close = float(curr_candle[4])
        volume = float(curr_candle[5])
        vol_usdt = open_p * volume
        pct = ((close - prev_close) / prev_close) * 100

        st = calculate_supertrend(candles[:last_idx+1], ATR_PERIOD, MULTIPLIER)
        if not st:
            return None

        direction = st['direction'][last_idx]
        up_band = st['up'][last_idx]

        # Only consider established uptrends (not just flipped)
        if direction != 1:
            return None

        # Check pullback to support
        touched = low <= up_band
        held = close > up_band
        if not (touched and held):
            return None

        # Require bullish pattern
        pattern = has_bullish_reversal_pattern(candles, last_idx, up_band)
        if not pattern:
            return None

        support_distance = ((close - up_band) / up_band) * 100

        return {
            'symbol': symbol,
            'hour': hour,
            'pct': pct,
            'close': close,
            'supertrend_line': up_band,
            'vol_usdt': vol_usdt,
            'support_distance': support_distance,
            'pattern': pattern
        }

    except:
        return None

# ==== RSI for Signal ====
def get_rsi(symbol):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=25"
        candles = session.get(url, timeout=5).json()
        if len(candles) < 20:
            return None
        last_idx = len(candles) - 2
        closes = [float(candles[i][4]) for i in range(last_idx + 1)]
        return calculate_rsi(closes, RSI_PERIOD)
    except:
        return None

# ==== Telegram & Logging ====
def log_signal(data):
    try:
        with open(LOG_FILE, 'a') as f:
            entry = {'timestamp': datetime.now(timezone.utc).isoformat(), 'data': data}
            f.write(json.dumps(entry) + '\n')
    except:
        pass

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
        return resp.status_code == 200
    except:
        return False

# ==== Main Scan ====
def scan_retests(symbols):
    candidates = []
    with ThreadPoolExecutor(max_workers=80) as ex:
        futures = {ex.submit(detect_retest, s): s for s in symbols}
        for f in as_completed(futures):
            res = f.result()
            if res:
                candidates.append(res)

    # Add RSI
    retests = []
    with ThreadPoolExecutor(max_workers=50) as ex:
        futures = {ex.submit(get_rsi, r['symbol']): r for r in candidates}
        for f in as_completed(futures):
            rsi = f.result()
            r = futures[f]
            if rsi is not None:
                r['rsi'] = rsi
                retests.append(r)

    return retests

# ==== Format Alert ====
def format_alert(retests, duration):
    if not retests:
        return None
    report = f"🔵 <b>SUPERTREND RETESTS</b>\n⏱ {duration:.2f}s | Count: {len(retests)}\n\n"
    for r in sorted(retests, key=lambda x: abs(x['pct']), reverse=True):
        sym = r['symbol'].replace("USDT", "")
        line1 = f"{sym:6s} {r['pct']:5.2f}% RSI:{r['rsi']:4.1f} Vol:{format_volume(r['vol_usdt']):4s}M"
        line2 = f"       🟢 ST: ${r['supertrend_line']:.5f} ({r['support_distance']:+.2f}%)"
        line3 = f"       🕯️ {r['pattern']}"
        report += f"<code>{line1}</code>\n<code>{line2}</code>\n<code>{line3}</code>\n\n"
    return report

# ==== Main Loop ====
def main():
    print("🔵 SUPERTREND RETESTS ONLY (ATR=10, Multiplier=3)")
    symbols = get_usdt_pairs()
    if not symbols:
        print("❌ No symbols found")
        return
    print(f"✓ Monitoring {len(symbols)} pairs")

    while True:
        start = time.time()
        raw_retests = scan_retests(symbols)
        duration = time.time() - start

        fresh = []
        for r in raw_retests:
            key = ('R', r['symbol'], r['hour'])
            if key not in reported_signals:
                reported_signals.add(key)
                fresh.append(r)
                log_signal(r)

        if fresh:
            msg = format_alert(fresh, duration)
            if msg:
                success = send_telegram(msg[:4096])
                if not success:
                    for r in fresh:
                        reported_signals.discard(('R', r['symbol'], r['hour']))

        server_time = session.get(f"{BINANCE_API}/api/v3/time", timeout=5).json()["serverTime"] / 1000
        next_interval = (server_time // 900 + 1) * 900
        sleep_time = max(30, next_interval - server_time + 2)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
