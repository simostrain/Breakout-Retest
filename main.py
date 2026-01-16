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

# SuperTrend+ parameters
ATR_PERIOD = 10
MULTIPLIER = 3.0
CLOSE_BARS = 1  # confirmation with 1 closed bar

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

LOG_FILE = Path("/tmp/supertrend_patterns_log.json")

session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=2)
session.mount("https://", adapter)

def format_volume(v):
    return f"{v/1_000_000:.2f}"

def get_binance_server_time():
    try:
        return session.get(f"{BINANCE_API}/api/v3/time", timeout=5).json()["serverTime"] / 1000
    except:
        return time.time()

# ==== SuperTrend+ with ATR=10, SMA, close_bars=1 ====
def calculate_supertrend_with_confirmation(candles, atr_period=10, multiplier=3.0, close_bars=1):
    n = len(candles)
    min_required = atr_period + close_bars + 1
    if n < min_required:
        return None

    trs = []
    for i in range(1, n):
        h = float(candles[i][2])
        l = float(candles[i][3])
        c_prev = float(candles[i-1][4])
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)

    atr_vals = [None] * n
    for i in range(atr_period, n):
        atr_vals[i] = sum(trs[i - atr_period:i]) / atr_period

    src = [(float(c[2]) + float(c[3])) / 2.0 for c in candles]
    up = [0.0] * n
    dn = [0.0] * n
    unconfirmed_trend = [1] * n
    confirmed_trend = [1] * n
    reversal = [False] * n
    warn = [False] * n

    for i in range(atr_period, n):
        atr = atr_vals[i] or 1e-8
        basic_upper = src[i] - multiplier * atr
        basic_lower = src[i] + multiplier * atr

        if i == atr_period:
            up[i] = basic_upper
            dn[i] = basic_lower
            close = float(candles[i][4])
            unconfirmed_trend[i] = 1 if close > dn[i] else -1
        else:
            if unconfirmed_trend[i-1] == 1:
                up[i] = max(basic_upper, up[i-1])
            else:
                up[i] = basic_upper

            if unconfirmed_trend[i-1] == -1:
                dn[i] = min(basic_lower, dn[i-1])
            else:
                dn[i] = basic_lower

            close = float(candles[i][4])
            if close > dn[i]:
                unconfirmed_trend[i] = 1
            elif close < up[i]:
                unconfirmed_trend[i] = -1
            else:
                unconfirmed_trend[i] = unconfirmed_trend[i-1]

    confirmed_trend[atr_period] = unconfirmed_trend[atr_period]
    for i in range(atr_period + 1, n):
        confirmed_trend[i] = confirmed_trend[i-1]
        close = float(candles[i][4])
        prev = confirmed_trend[i-1]

        if prev == -1 and close > up[i]:
            count = 0
            j = i
            while j >= max(atr_period, i - close_bars + 1):
                if float(candles[j][4]) > up[j]:
                    count += 1
                else:
                    break
                j -= 1
            if count >= close_bars:
                confirmed_trend[i] = 1
                reversal[i] = True
            warn[i] = True

        elif prev == 1 and close < dn[i]:
            count = 0
            j = i
            while j >= max(atr_period, i - close_bars + 1):
                if float(candles[j][4]) < dn[j]:
                    count += 1
                else:
                    break
                j -= 1
            if count >= close_bars:
                confirmed_trend[i] = -1
                reversal[i] = True
            warn[i] = True

    supertrend = [up[i] if confirmed_trend[i] == 1 else dn[i] for i in range(n)]
    return {
        'supertrend': supertrend,
        'trend': confirmed_trend,
        'up': up,
        'dn': dn,
        'reversal': reversal,
        'warn': warn,
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

# ==== Binance Pairs ====
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

# ==== Signal Detection ====
def detect_signals(symbol):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=100"
        candles = session.get(url, timeout=5).json()
        if not candles or isinstance(candles, dict) or len(candles) < 30:
            return None

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

        # ✅ NO VOLUME FILTER — ALL SYMBOLS INCLUDED
        st_result = calculate_supertrend_with_confirmation(
            candles[:last_idx+1],
            atr_period=ATR_PERIOD,
            multiplier=MULTIPLIER,
            close_bars=CLOSE_BARS
        )
        if not st_result:
            return None

        current_trend = st_result['trend'][last_idx]
        is_reversal = st_result['reversal'][last_idx]
        supertrend_line = st_result['supertrend'][last_idx]

        results = {}

        if current_trend == 1 and is_reversal:
            results['breakout'] = {
                'symbol': symbol,
                'hour': hour,
                'pct': pct,
                'close': close,
                'supertrend_line': supertrend_line,
                'vol_usdt': vol_usdt
            }

        elif current_trend == 1:
            up_band = st_result['up'][last_idx]
            touched = low <= up_band
            held = close > up_band
            if touched and held:
                pattern = has_bullish_reversal_pattern(candles, last_idx, up_band)
                if pattern:
                    support_distance = ((close - up_band) / up_band) * 100
                    results['retest'] = {
                        'symbol': symbol,
                        'hour': hour,
                        'pct': pct,
                        'close': close,
                        'supertrend_line': up_band,
                        'vol_usdt': vol_usdt,
                        'support_distance': support_distance,
                        'pattern': pattern
                    }

        return results if results else None

    except Exception:
        return None

# ==== RSI Fetch ====
def calculate_rsi_for_signal(symbol):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit=25"
        candles = session.get(url, timeout=5).json()
        if not candles or len(candles) < 20:
            return None
        last_idx = len(candles) - 2
        closes = [float(candles[j][4]) for j in range(last_idx + 1)]
        return calculate_rsi(closes, RSI_PERIOD)
    except:
        return None

# ==== Logging & Telegram ====
def log_signal_to_file(signal_data, signal_type):
    log_entry = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'type': signal_type,
        'data': signal_data
    }
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')
    except:
        pass

def send_telegram(msg, max_retries=3):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for _ in range(max_retries):
        try:
            resp = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML"
            }, timeout=10)
            if resp.status_code == 200:
                return True
            time.sleep(2)
        except:
            time.sleep(2)
    return False

# ==== Main Scan ====
def scan_all_symbols(symbols):
    signal_candidates = []
    with ThreadPoolExecutor(max_workers=100) as ex:
        futures = {ex.submit(detect_signals, s): s for s in symbols}
        for f in as_completed(futures):
            r = f.result()
            if r:
                signal_candidates.append(r)

    final_signals = {'breakouts': [], 'retests': []}
    if not signal_candidates:
        return final_signals

    with ThreadPoolExecutor(max_workers=50) as ex:
        futures = {}
        for res in signal_candidates:
            if 'breakout' in res:
                futures[ex.submit(calculate_rsi_for_signal, res['breakout']['symbol'])] = ('breakout', res['breakout'])
            if 'retest' in res:
                futures[ex.submit(calculate_rsi_for_signal, res['retest']['symbol'])] = ('retest', res['retest'])

        for f in as_completed(futures):
            rsi = f.result()
            sig_type, data = futures[f]
            if rsi is not None:
                data['rsi'] = rsi
                if sig_type == 'breakout':
                    final_signals['breakouts'].append(data)
                else:
                    final_signals['retests'].append(data)

    return final_signals

# ==== Report Formatting ====
def format_signal_report(signals, duration):
    breakouts = signals['breakouts']
    retests = signals['retests']
    if not breakouts and not retests:
        return None

    report = f"🚀 <b>SUPERTREND+ BUY SIGNALS</b>\n"
    report += f"⏱ {duration:.2f}s | B: {len(breakouts)} | R: {len(retests)}\n\n"

    all_items = []
    for b in breakouts:
        all_items.append(('B', b))
    for r in retests:
        all_items.append(('R', r))

    def sort_key(item):
        typ, d = item
        rsi_factor = 1.0 if d['rsi'] <= 70 else 0.5
        return abs(d['pct']) * rsi_factor

    all_items.sort(key=sort_key, reverse=True)

    for typ, d in all_items:
        sym = d['symbol'].replace("USDT", "")
        line1 = f"{sym:6s} {d['pct']:5.2f}% RSI:{d['rsi']:4.1f} Vol:{format_volume(d['vol_usdt']):4s}M"
        if typ == 'B':
            st_dist = ((d['close'] - d['supertrend_line']) / d['supertrend_line']) * 100
            line2 = f"       🟢 Breakout ST: ${d['supertrend_line']:.5f} ({st_dist:+.2f}%)"
            report += f"<code>{line1}</code>\n<code>{line2}</code>\n\n"
        else:
            line2 = f"       🟢 Retest ST: ${d['supertrend_line']:.5f} ({d['support_distance']:+.2f}%)"
            line3 = f"       🕯️ {d['pattern']}"
            report += f"<code>{line1}</code>\n<code>{line2}</code>\n<code>{line3}</code>\n\n"

    return report

# ==== Main Loop ====
def main():
    print("="*80)
    print("🚀 SUPERTREND+ (ATR=10, close_bars=1) — BUY SIGNALS ONLY")
    print("="*80)

    symbols = get_usdt_pairs()
    if not symbols:
        return

    print(f"✓ Monitoring {len(symbols)} pairs | No volume filter applied")
    print("🔁 Scanning every 15m...\n")

    while True:
        total_start = time.time()
        signals = scan_all_symbols(symbols)
        total_duration = time.time() - total_start

        fresh_breakouts = []
        fresh_retests = []

        for b in signals['breakouts']:
            key = ('B', b['symbol'], b['hour'])
            if key not in reported_signals:
                reported_signals.add(key)
                fresh_breakouts.append(b)
                log_signal_to_file(b, 'breakout')

        for r in signals['retests']:
            key = ('R', r['symbol'], r['hour'])
            if key not in reported_signals:
                reported_signals.add(key)
                fresh_retests.append(r)
                log_signal_to_file(r, 'retest')

        if fresh_breakouts or fresh_retests:
            msg = format_signal_report({'breakouts': fresh_breakouts, 'retests': fresh_retests}, total_duration)
            if msg:
                success = send_telegram(msg[:4096])
                if not success:
                    for b in fresh_breakouts:
                        reported_signals.discard(('B', b['symbol'], b['hour']))
                    for r in fresh_retests:
                        reported_signals.discard(('R', r['symbol'], r['hour']))

        server_time = get_binance_server_time()
        next_interval = (server_time // 900 + 1) * 900
        sleep_time = max(30, next_interval - server_time + 2)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
