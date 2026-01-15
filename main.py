import os
import requests
import time
import math
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from pathlib import Path

# ==== Settings ====
BINANCE_API = "https://api.binance.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RSI_PERIOD = 14
reported_signals = set()

# Standard SuperTrend params (Pine v6)
ATR_PERIOD = 10
MULTIPLIER = 3.0

# Volume settings
VOL_LEN = 20

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

LOG_FILE = Path("/tmp/retest_patterns_only.log")

session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=1)
session.mount("https://", adapter)

def format_volume(v):
    return f"{v/1_000_000:.2f}"

def get_binance_server_time():
    try:
        return session.get(f"{BINANCE_API}/api/v3/time", timeout=3).json()["serverTime"] / 1000
    except:
        return time.time()

# ==== Standard ATR (SMA) ====
def calculate_atr_sma(candles, period):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i][2])
        l = float(candles[i][3])
        c_prev = float(candles[i-1][4])
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)
    
    atr_vals = [None] * len(candles)
    atr_vals[period] = sum(trs[:period]) / period
    for i in range(period + 1, len(candles)):
        atr_vals[i] = (atr_vals[i-1] * (period - 1) + trs[i-1]) / period
    return atr_vals

# ==== Standard SuperTrend (Pine v6 logic) ====
def calculate_supertrend_standard(candles, atr_period=10, multiplier=3.0):
    n = len(candles)
    if n < atr_period + 1:
        return None

    atr_vals = calculate_atr_sma(candles, atr_period)
    if atr_vals is None:
        return None

    up = [0.0] * n
    dn = [0.0] * n
    direction = [1] * n

    for i in range(atr_period, n):
        high = float(candles[i][2])
        low = float(candles[i][3])
        src = (high + low) / 2.0
        atr = atr_vals[i]
        
        basic_upper = src - multiplier * atr
        basic_lower = src + multiplier * atr

        if i == atr_period:
            up[i] = basic_upper
            dn[i] = basic_lower
        else:
            if direction[i-1] == 1:
                up[i] = max(basic_upper, up[i-1])
            else:
                up[i] = basic_upper
            if direction[i-1] == -1:
                dn[i] = min(basic_lower, dn[i-1])
            else:
                dn[i] = basic_lower

        close = float(candles[i][4])
        if close > dn[i-1]:
            direction[i] = 1
        elif close < up[i-1]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]

    return {
        'direction': direction,
        'up': up,
        'dn': dn,
        'atr': atr_vals
    }

# ==== BULLISH REVERSAL PATTERNS ====
def is_bullish_engulfing(candles, idx):
    if idx < 1: return False
    c1 = candles[idx-1]; c2 = candles[idx]
    o1, c1 = float(c1[1]), float(c1[4]); o2, c2 = float(c2[1]), float(c2[4])
    return (c1 < o1) and (c2 > o2) and (o2 < c1) and (c2 > o1)

def is_hammer(candles, idx):
    c = candles[idx]
    o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
    body = abs(cl - o)
    if body == 0: return False
    lower_wick = o - l if cl >= o else cl - l
    upper_wick = h - cl if cl >= o else h - o
    return (lower_wick >= 2 * body) and (upper_wick <= body) and (cl > (h + l) / 2)

def is_piercing_line(candles, idx):
    if idx < 1: return False
    c1 = candles[idx-1]; c2 = candles[idx]
    o1, c1 = float(c1[1]), float(c1[4]); o2, c2 = float(c2[1]), float(c2[4])
    return (c1 < o1) and (c2 > o2) and (c2 > (o1 + c1) / 2)

def is_bullish_pin_bar(candles, idx):
    c = candles[idx]
    o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
    body = abs(cl - o)
    if body == 0: return False
    lower_wick = min(o, cl) - l
    upper_wick = h - max(o, cl)
    return (lower_wick >= 2 * body) and (upper_wick <= body) and (cl > o) and (cl > (h + l) / 2)

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
    gains = [max(c, 0) for c in changes]; losses = [max(-c, 0) for c in changes]
    avg_gain = sum(gains[:period]) / period; avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)

# ==== Binance ====
def get_usdt_pairs():
    candidates = list(dict.fromkeys([t.upper() + "USDT" for t in CUSTOM_TICKERS]))
    try:
        data = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=5).json()
        valid = {s["symbol"] for s in data["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        return [c for c in candidates if c in valid]
    except:
        return []

# ==== Strength Scoring ====
def calculate_strength_score_indicator(volume, vol_sma, close, supertrend_line, atr):
    if vol_sma <= 0 or atr <= 0: return 0.0
    vol_ratio = volume / vol_sma
    momentum = abs(close - supertrend_line) / atr
    return min(math.log(vol_ratio + 1) * momentum, 10.0)

# ==== RETEST SIGNAL DETECTION ONLY ====
def detect_retest_signal(symbol):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=50"
        candles = session.get(url, timeout=4).json()
        if not candles or len(candles) < 25: return None

        last_idx = len(candles) - 2
        last_candle = candles[last_idx]
        prev_candle = candles[last_idx - 1]

        candle_time = datetime.fromtimestamp(last_candle[0]/1000, tz=timezone.utc)
        hour = candle_time.strftime("%Y-%m-%d %H:%M")

        prev_close = float(prev_candle[4])
        open_p = float(last_candle[1])
        high = float(last_candle[2])
        low = float(last_candle[3])
        close = float(last_candle[4])
        volume = float(last_candle[5])
        vol_usdt = open_p * volume
        pct = ((close - prev_close) / prev_close) * 100

        st_result = calculate_supertrend_standard(candles[:last_idx+1], ATR_PERIOD, MULTIPLIER)
        if not st_result: return None

        atr = st_result['atr'][last_idx] or 1e-8
        up_band = st_result['up'][last_idx]
        direction = st_result['direction'][last_idx]
        prev_direction = st_result['direction'][last_idx - 1]

        # ONLY consider retests (no breakouts)
        if direction != 1:
            return None

        vol_ma_start = max(0, last_idx - VOL_LEN + 1)
        vol_ma_data = [float(candles[j][5]) for j in range(vol_ma_start, last_idx + 1)]
        vol_sma = sum(vol_ma_data) / len(vol_ma_data) if vol_ma_data else volume
        vm = volume / vol_sma if vol_sma > 0 else 1.0

        indicator_strength = calculate_strength_score_indicator(volume, vol_sma, close, up_band, atr)

        # Retest conditions
        touched_support = low <= up_band
        held_support = close > up_band

        if touched_support and held_support:
            pattern_name = has_bullish_reversal_pattern(candles, last_idx, up_band)
            if pattern_name:
                # Calculate bars since last breakout (for info only)
                bars_since_breakout = 0
                for i in range(last_idx, ATR_PERIOD - 1, -1):
                    past_st = calculate_supertrend_standard(candles[:i+1], ATR_PERIOD, MULTIPLIER)
                    if past_st and past_st['direction'][i] == 1 and (i == ATR_PERIOD or past_st['direction'][i-1] == -1):
                        bars_since_breakout = last_idx - i
                        break

                support_distance = ((close - up_band) / up_band) * 100
                
                # Get RSI
                rsi = None
                try:
                    url_rsi = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=20"
                    candles_rsi = session.get(url_rsi, timeout=3).json()
                    if candles_rsi and len(candles_rsi) >= 15:
                        closes = [float(candles_rsi[j][4]) for j in range(len(candles_rsi)-15, len(candles_rsi)-1)]
                        rsi = calculate_rsi(closes, RSI_PERIOD)
                except:
                    pass

                if rsi is not None:
                    return {
                        'symbol': symbol,
                        'hour': hour,
                        'pct': pct,
                        'close': close,
                        'supertrend_line': up_band,
                        'bars_since_breakout': bars_since_breakout,
                        'vol_usdt': vol_usdt,
                        'vm': vm,
                        'indicator_strength': indicator_strength,
                        'support_distance': support_distance,
                        'pattern': pattern_name,
                        'rsi': rsi
                    }

        return None

    except Exception:
        return None

# ==== Telegram ====
def send_telegram(msg, max_retries=2):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(max_retries):
        try:
            response = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML"
            }, timeout=8)
            if response.status_code == 200:
                return True
        except:
            if attempt < max_retries - 1:
                time.sleep(1)
    return False

# ==== Report Formatting ====
def format_retest_report(retests, duration):
    if not retests:
        return None

    report = f"🔵 <b>RETEST PATTERNS ONLY</b> 🔵\n"
    report += f"⏱ Scan: {duration:.1f}s | Signals: {len(retests)}\n\n"

    grouped = defaultdict(list)
    for r in retests:
        grouped[r['hour']].append(r)

    for hour in sorted(grouped.keys(), reverse=True):
        items = sorted(grouped[hour], key=lambda x: x['indicator_strength'], reverse=True)
        report += f"⏰ {hour} UTC\n"
        for r in items:
            sym = r['symbol'].replace("USDT", "")
            line1 = f"{sym:6s} {r['pct']:5.2f}% {r['rsi']:4.1f} {r['vm']:4.1f}x {format_volume(r['vol_usdt']):4s}M {r['indicator_strength']:5.2f}"
            line2 = f"       🟢ST: ${r['supertrend_line']:.5f} ({r['support_distance']:+.2f}%)"
            line3 = f"       🕯️ Pattern: {r['pattern']}"
            report += f"<code>{line1}</code>\n<code>{line2}</code>\n<code>{line3}</code>\n"
        report += "\n"

    report += "💡 High-probability pullback entries only"
    return report

# ==== Main Loop ====
def main():
    print("🔵 RETEST PATTERNS ONLY (NO BREAKOUTS)")
    print("="*50)

    symbols = get_usdt_pairs()
    if not symbols:
        print("❌ No symbols")
        return

    print(f"✓ Monitoring {len(symbols)} pairs\n")

    while True:
        total_start = time.time()
        retests = []

        # Scan for retests
        with ThreadPoolExecutor(max_workers=30) as ex:
            futures = {ex.submit(detect_retest_signal, s): s for s in symbols}
            for f in as_completed(futures):
                result = f.result()
                if result:
                    retests.append(result)

        # Filter new signals
        fresh_retests = []
        for r in retests:
            key = ('R', r['symbol'], r['hour'])
            if key not in reported_signals:
                reported_signals.add(key)
                fresh_retests.append(r)

        # Send alerts
        if fresh_retests:
            msg = format_retest_report(fresh_retests, time.time() - total_start)
            if msg:
                send_telegram(msg[:4096])

        # Sleep until next 15m
        server_time = get_binance_server_time()
        next_interval = (server_time // 900 + 1) * 900
        sleep_time = max(30, next_interval - server_time + 2)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
