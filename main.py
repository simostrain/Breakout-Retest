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

# Persistent tracking state: {symbol: { 'confirmation_price': float, 'pattern_time': str, 'pattern_type': str }}
tracked_coins = {}
reported_signals = set()  # for deduplication of final alerts

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

LOG_FILE = Path("/tmp/supertrend_retest_confirmed.json")
TRACKING_FILE = Path("/tmp/supertrend_tracked_coins.json")

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

# ==== Standard SuperTrend ====
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

# ==== Pattern Detection ====
def is_red_candle(candle):
    return float(candle[4]) < float(candle[1])

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

def find_last_red_before(candles, pattern_idx):
    """Find the closest red candle BEFORE pattern_idx"""
    for i in range(pattern_idx - 1, -1, -1):
        if is_red_candle(candles[i]):
            return i
    return None

def detect_pattern_and_red(candles, idx, support_line):
    support_buffer = support_line * 0.005
    low = float(candles[idx][3])
    if low > support_line + support_buffer:
        return None, None

    pattern_type = None
    if is_bullish_engulfing(candles, idx):
        pattern_type = "Bullish Engulfing"
    elif is_piercing_line(candles, idx):
        pattern_type = "Piercing Line"
    elif is_hammer(candles, idx):
        pattern_type = "Hammer"
    elif is_bullish_pin_bar(candles, idx):
        pattern_type = "Bullish Pin Bar"
    else:
        return None, None

    red_idx = find_last_red_before(candles, idx)
    if red_idx is None:
        return None, None

    red_open = float(candles[red_idx][1])
    return pattern_type, red_open

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

# ==== Fetch Data ====
def get_usdt_pairs():
    candidates = [t.upper() + "USDT" for t in CUSTOM_TICKERS]
    try:
        data = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=10).json()
        valid = {s["symbol"] for s in data["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        return [c for c in candidates if c in valid]
    except:
        return []

def fetch_candles(symbol, limit=100):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=15m&limit={limit}"
        return session.get(url, timeout=5).json()
    except:
        return None

# ==== Save/Load Tracking State ====
def load_tracked_coins():
    global tracked_coins
    if TRACKING_FILE.exists():
        try:
            with open(TRACKING_FILE, 'r') as f:
                data = json.load(f)
                # Convert keys back to proper format
                tracked_coins = {k: v for k, v in data.items()}
        except:
            tracked_coins = {}

def save_tracked_coins():
    try:
        with open(TRACKING_FILE, 'w') as f:
            json.dump(tracked_coins, f)
    except:
        pass

# ==== Main Scan Logic ====
def scan_for_new_patterns(symbols):
    """Detect new retest patterns and start tracking them"""
    for symbol in symbols:
        candles = fetch_candles(symbol)
        if not candles or len(candles) < 30:
            continue

        last_idx = len(candles) - 2
        st = calculate_supertrend(candles[:last_idx+1], ATR_PERIOD, MULTIPLIER)
        if not st or st['direction'][last_idx] != 1:
            continue

        up_band = st['up'][last_idx]
        pattern_type, confirmation_price = detect_pattern_and_red(candles, last_idx, up_band)
        if pattern_type and confirmation_price:
            # Start tracking
            tracked_coins[symbol] = {
                'confirmation_price': confirmation_price,
                'pattern_type': pattern_type,
                'detected_at': candles[last_idx][0]  # timestamp
            }
            print(f"🔍 Started tracking {symbol}: wait for close > ${confirmation_price:.5f}")

def check_tracked_coins(symbols):
    """Check if any tracked coin has closed above confirmation price"""
    alerts_to_send = []
    symbols_to_remove = []

    for symbol in list(tracked_coins.keys()):
        if symbol not in symbols:
            symbols_to_remove.append(symbol)
            continue

        candles = fetch_candles(symbol, limit=20)
        if not candles or len(candles) < 5:
            continue

        # Get latest completed candle
        last_idx = len(candles) - 2
        current_close = float(candles[last_idx][4])
        current_time = datetime.fromtimestamp(candles[last_idx][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

        # Check SuperTrend direction
        st = calculate_supertrend(candles[:last_idx+1], ATR_PERIOD, MULTIPLIER)
        if not st:
            continue

        # If SuperTrend turned bearish → stop tracking
        if st['direction'][last_idx] == -1:
            print(f"🔻 {symbol}: SuperTrend bearish → stopped tracking")
            symbols_to_remove.append(symbol)
            continue

        # Check confirmation
        conf_price = tracked_coins[symbol]['confirmation_price']
        if current_close > conf_price:
            # Fetch RSI for alert
            rsi = None
            try:
                closes = [float(c[4]) for c in candles[-RSI_PERIOD-2:]]
                rsi = calculate_rsi(closes, RSI_PERIOD)
            except:
                rsi = None

            vol_usdt = float(candles[last_idx][5]) * float(candles[last_idx][1])
            pct = ((current_close - float(candles[last_idx-1][4])) / float(candles[last_idx-1][4])) * 100

            alert_data = {
                'symbol': symbol,
                'hour': current_time,
                'pct': pct,
                'close': current_close,
                'confirmation_price': conf_price,
                'vol_usdt': vol_usdt,
                'rsi': rsi,
                'pattern_type': tracked_coins[symbol]['pattern_type']
            }

            key = ('CONFIRMED', symbol, current_time)
            if key not in reported_signals:
                reported_signals.add(key)
                alerts_to_send.append(alert_data)
                symbols_to_remove.append(symbol)
                # Log
                try:
                    with open(LOG_FILE, 'a') as f:
                        entry = {'timestamp': datetime.now(timezone.utc).isoformat(), 'data': alert_data}
                        f.write(json.dumps(entry) + '\n')
                except:
                    pass

    # Clean up
    for sym in symbols_to_remove:
        tracked_coins.pop(sym, None)

    return alerts_to_send

# ==== Telegram ====
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

def format_alert(alerts):
    if not alerts:
        return None
    report = "✅ <b>BUY CONFIRMED (Retest + Break)</b>\n\n"
    for a in alerts:
        sym = a['symbol'].replace("USDT", "")
        line1 = f"{sym:6s} {a['pct']:5.2f}% RSI:{a['rsi'] or 'N/A':4} Vol:{format_volume(a['vol_usdt']):4s}M"
        line2 = f"       🟢 Break: ${a['confirmation_price']:.5f}"
        line3 = f"       🕯️ Pattern: {a['pattern_type']}"
        report += f"<code>{line1}</code>\n<code>{line2}</code>\n<code>{line3}</code>\n\n"
    return report

# ==== Main Loop ====
def main():
    print("🚀 Retest Confirmation Tracker (ATR=10, Multiplier=3)")
    symbols = get_usdt_pairs()
    if not symbols:
        print("❌ No symbols")
        return

    print(f"✓ Monitoring {len(symbols)} pairs")
    load_tracked_coins()

    while True:
        try:
            # Step 1: Look for NEW patterns to track
            scan_for_new_patterns(symbols)

            # Step 2: Check already tracked coins for confirmation
            alerts = check_tracked_coins(symbols)

            # Step 3: Send alerts
            if alerts:
                msg = format_alert(alerts)
                if msg:
                    success = send_telegram(msg[:4096])
                    if not success:
                        # Optional: retry or log failure
                        pass

            # Save state
            save_tracked_coins()

            # Sleep until next 15m
            try:
                server_time = session.get(f"{BINANCE_API}/api/v3/time", timeout=5).json()["serverTime"] / 1000
            except:
                server_time = time.time()
            next_interval = (server_time // 900 + 1) * 900
            sleep_time = max(30, next_interval - server_time + 2)
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n🛑 Exiting...")
            save_tracked_coins()
            break
        except Exception as e:
            print(f"⚠️ Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
