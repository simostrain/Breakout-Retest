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

# Global: {symbol: red_line_value} — tracks breakout levels silently
pending_breakouts = {}

ATR_PERIOD = 10
MULTIPLIER = 3.0
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

LOG_FILE = Path("/tmp/supertrend_persistent.log")

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

# ==== BULLISH REVERSAL PATTERNS (for retests only) ====
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

def get_usdt_pairs():
    candidates = list(dict.fromkeys([t.upper() + "USDT" for t in CUSTOM_TICKERS]))
    try:
        data = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=5).json()
        valid = {s["symbol"] for s in data["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        return [c for c in candidates if c in valid]
    except:
        return []

def calculate_strength_score_indicator(volume, vol_sma, close, supertrend_line, atr):
    if vol_sma <= 0 or atr <= 0: return 0.0
    vol_ratio = volume / vol_sma
    momentum = abs(close - supertrend_line) / atr
    return min(math.log(vol_ratio + 1) * momentum, 10.0)

# ==== CORE LOGIC: SILENT BREAKOUT DETECTION + PERSISTENT TRACKING ====
def detect_signals(symbol):
    global pending_breakouts
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=60"
        candles = session.get(url, timeout=4).json()
        if not candles or len(candles) < 25:
            return None

        last_idx = len(candles) - 2
        current_candle = candles[last_idx]

        candle_time = datetime.fromtimestamp(current_candle[0]/1000, tz=timezone.utc)
        hour = candle_time.strftime("%Y-%m-%d %H:%M")

        close = float(current_candle[4])
        open_p = float(current_candle[1])
        high = float(current_candle[2])
        low = float(current_candle[3])
        volume = float(current_candle[5])
        vol_usdt = open_p * volume

        st_result = calculate_supertrend_standard(candles[:last_idx+1], ATR_PERIOD, MULTIPLIER)
        if not st_result:
            return None

        direction = st_result['direction'][last_idx]
        prev_direction = st_result['direction'][last_idx - 1] if last_idx >= 1 else -1

        # ==== STEP 1: Detect FLIP (red → green) → STORE RED LINE SILENTLY ====
        if prev_direction == -1 and direction == 1:
            # The red line = dn_band of the last downtrend candle (index last_idx - 1)
            red_line = st_result['dn'][last_idx - 1]
            pending_breakouts[symbol] = red_line
            # DO NOT ALERT HERE — just remember the level

        # ==== STEP 2: Check if ANY stored breakout is now confirmed ====
        results = {}
        if symbol in pending_breakouts:
            red_line = pending_breakouts[symbol]
            # ✅ ONLY alert if CLOSE IS STRICTLY ABOVE RED LINE
            if close > red_line:
                # Calculate RSI separately later
                results['breakout'] = {
                    'symbol': symbol,
                    'hour': hour,
                    'close': close,
                    'red_line': red_line,
                    'vol_usdt': vol_usdt
                }
                # Remove from tracking after alert
                del pending_breakouts[symbol]

        # ==== RETEST: Only if in uptrend AND not a breakout ====
        if direction == 1 and 'breakout' not in results:
            up_band = st_result['up'][last_idx]
            touched_support = low <= up_band
            held_support = close > up_band
            if touched_support and held_support:
                pattern_name = has_bullish_reversal_pattern(candles, last_idx, up_band)
                if pattern_name:
                    vol_ma_start = max(0, last_idx - VOL_LEN + 1)
                    vol_ma_data = [float(candles[j][5]) for j in range(vol_ma_start, last_idx + 1)]
                    vol_sma = sum(vol_ma_data) / len(vol_ma_data) if vol_ma_data else volume
                    vm = volume / vol_sma if vol_sma > 0 else 1.0
                    atr = st_result['atr'][last_idx] or 1e-8
                    indicator_strength = calculate_strength_score_indicator(volume, vol_sma, close, up_band, atr)
                    prev_close = float(candles[last_idx-1][4])
                    pct = ((close - prev_close) / prev_close) * 100
                    results['retest'] = {
                        'symbol': symbol,
                        'hour': hour,
                        'pct': pct,
                        'close': close,
                        'supertrend_line': up_band,
                        'vol_usdt': vol_usdt,
                        'vm': vm,
                        'indicator_strength': indicator_strength,
                        'pattern': pattern_name
                    }

        return results if results else None

    except Exception:
        return None

# ==== TELEGRAM & MAIN LOOP ====
def send_telegram(msg, max_retries=2):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(max_retries):
        try:
            response = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=8)
            if response.status_code == 200:
                return True
        except:
            if attempt < max_retries - 1:
                time.sleep(1)
    return False

def calculate_rsi_for_signal(symbol):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=20"
        candles = session.get(url, timeout=3).json()
        if not candles or len(candles) < 15: return None
        closes = [float(candles[j][4]) for j in range(len(candles)-15, len(candles)-1)]
        return calculate_rsi(closes, RSI_PERIOD)
    except:
        return None

def format_alert(signal, rsi, signal_type):
    sym = signal['symbol'].replace("USDT", "")
    close = signal['close']
    vol_str = format_volume(signal['vol_usdt'])
    if signal_type == 'breakout':
        red_line = signal['red_line']
        pct = ((close - red_line) / red_line) * 100
        msg = f"🟢 <b>BREAKOUT CONFIRMED</b>\n"
        msg += f"Symbol: <b>{sym}</b>\n"
        msg += f"Price: ${close:.5f}\n"
        msg += f"Red Line: ${red_line:.5f}\n"
        msg += f"Move: +{pct:.2f}%\n"
        msg += f"Volume: {vol_str}M\n"
        msg += f"RSI: {rsi:.1f}"
    else:
        st_line = signal['supertrend_line']
        pct = signal['pct']
        pattern = signal['pattern']
        msg = f"🔵 <b>RETEST</b>\n"
        msg += f"Symbol: <b>{sym}</b>\n"
        msg += f"Price: ${close:.5 f}\n"
        msg += f"Move: +{pct:.2f}%\n"
        msg += f"Pattern: {pattern}\n"
        msg += f"Volume: {vol_str}M\n"
        msg += f"RSI: {rsi:.1f}"
    return msg

def main():
    print("🚀 SUPERTREND SCANNER: BREAKOUTS TRACKED UNTIL CLOSE > RED LINE")
    symbols = get_usdt_pairs()
    if not symbols:
        print("❌ No symbols")
        return

    while True:
        fresh_alerts = []
        with ThreadPoolExecutor(max_workers=30) as ex:
            futures = {ex.submit(detect_signals, s): s for s in symbols}
            for f in as_completed(futures):
                result = f.result()
                if result:
                    if 'breakout' in result:
                        key = ('B', result['breakout']['symbol'], result['breakout']['hour'])
                        if key not in reported_signals:
                            reported_signals.add(key)
                            fresh_alerts.append(('breakout', result['breakout']))
                    if 'retest' in result:
                        key = ('R', result['retest']['symbol'], result['retest']['hour'])
                        if key not in reported_signals:
                            reported_signals.add(key)
                            fresh_alerts.append(('retest', result['retest']))

        # Add RSI and send
        if fresh_alerts:
            with ThreadPoolExecutor(max_workers=20) as ex:
                rsi_futures = {}
                for typ, data in fresh_alerts:
                    rsi_futures[ex.submit(calculate_rsi_for_signal, data['symbol'])] = (typ, data)
                for f in as_completed(rsi_futures):
                    rsi = f.result()
                    typ, data = rsi_futures[f]
                    if rsi is not None:
                        msg = format_alert(data, rsi, typ)
                        send_telegram(msg)

        server_time = get_binance_server_time()
        next_scan = (server_time // 900 + 1) * 900
        time.sleep(max(30, next_scan - server_time + 2))

if __name__ == "__main__":
    main()
