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

# Telegram Bot for alerts
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RSI_PERIOD = 14
reported_signals = set()  # Track confirmed buy signals

# Strength filters (set to 0 to disable)
MIN_STRENGTH_SCORE = 0
MIN_CSINCE = 0
MIN_VOLUME_MULT = 0.0

# Supertrend+ settings (from Pine Script)
ATR_PERIOD = 10          # ← Changed to 10 as requested
MULTIPLIER = 3.0
CLOSE_BARS = 2           # Confirmation: 2 closed candles above band

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

LOG_FILE = Path("/tmp/signal_log.json")

# ==== Session ====
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=2)
session.mount("https://", adapter)

# ==== Utils ====
def format_volume(v):
    return f"{v/1_000_000:.2f}"

def get_binance_server_time():
    try:
        return session.get(f"{BINANCE_API}/api/v3/time", timeout=5).json()["serverTime"] / 1000
    except:
        return time.time()

# ==== VAWMA & ATR ====
def vawma(values, volumes, period):
    if len(values) < period or len(volumes) < period:
        return None
    weighted_sum = sum(v * vol for v, vol in zip(values[-period:], volumes[-period:]))
    volume_sum = sum(volumes[-period:])
    return weighted_sum / volume_sum if volume_sum > 0 else values[-1]

def calculate_atr_vawma(candles, atr_period):
    if len(candles) < atr_period + 1:
        return None
    tr_list = []
    for i in range(1, len(candles)):
        h = float(candles[i][2])
        l = float(candles[i][3])
        c_prev = float(candles[i-1][4])
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_list.append(tr)
    
    atr_vals = [None] * len(candles)
    initial_trs = tr_list[:atr_period]
    if not initial_trs:
        return None
    atr_vals[atr_period] = sum(initial_trs) / len(initial_trs)

    volumes = [float(c[5]) for c in candles[1:]]
    for i in range(atr_period + 1, len(candles)):
        atr_val = vawma(tr_list[:i], volumes[:i], atr_period)
        atr_vals[i] = atr_val
    return atr_vals

# ==== Supertrend+ (VAWMA + Confirmation) ====
def calculate_supertrend_vawma(candles, atr_period=10, multiplier=3.0, close_bars=2):
    n = len(candles)
    if n < atr_period + 2:
        return None

    atr_vals = calculate_atr_vawma(candles, atr_period)
    if atr_vals is None:
        return None

    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]

    up = [0.0] * n
    dn = [0.0] * n
    trend = [1] * n
    reversal = [False] * n

    start_idx = atr_period
    for i in range(start_idx, n):
        src = (highs[i] + lows[i]) / 2.0
        atr = atr_vals[i] or 0.0
        basic_upper = src - multiplier * atr
        basic_lower = src + multiplier * atr

        if i == start_idx:
            up[i] = basic_upper
            dn[i] = basic_lower
            trend[i] = 1
        else:
            # Upper band
            if closes[i - 1] > up[i - 1]:
                up[i] = max(basic_upper, up[i - 1])
            else:
                up[i] = basic_upper
            # Lower band
            if closes[i - 1] < dn[i - 1]:
                dn[i] = min(basic_lower, dn[i - 1])
            else:
                dn[i] = basic_lower
            # Trend
            prev_trend = trend[i - 1]
            if prev_trend == -1 and closes[i] > dn[i - 1]:
                trend[i] = 1
            elif prev_trend == 1 and closes[i] < up[i - 1]:
                trend[i] = -1
            else:
                trend[i] = prev_trend

    # Apply close_bars confirmation
    confirmed_trend = trend[:]
    for i in range(start_idx + close_bars, n):
        # Check for bullish reversal confirmed over `close_bars`
        was_down = all(confirmed_trend[i - j] == -1 for j in range(1, close_bars + 1))
        now_up = confirmed_trend[i] == 1
        if was_down and now_up:
            # Verify all last `close_bars` closes > prior lower band
            valid = True
            for j in range(close_bars):
                idx = i - j
                if closes[idx] <= dn[idx - 1]:
                    valid = False
                    break
            if valid:
                reversal[i] = True
            else:
                # Revert trend during window
                for k in range(i - close_bars + 1, i + 1):
                    confirmed_trend[k] = -1
                    reversal[k] = False

    return {
        'trend': confirmed_trend,
        'up': up,
        'dn': dn,
        'reversal': reversal,
        'atr': atr_vals
    }

# ==== RSI ====
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

# ==== Binance ====
def get_usdt_pairs():
    candidates = list(dict.fromkeys([t.upper() + "USDT" for t in CUSTOM_TICKERS]))
    try:
        data = session.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=10).json()
        valid = {s["symbol"] for s in data["symbols"]
                 if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        pairs = [c for c in candidates if c in valid]
        print(f"✓ Loaded {len(pairs)} valid USDT pairs")
        return pairs
    except Exception as e:
        print(f"✗ Exchange info error: {e}")
        return []

# ==== Strength Scoring ====
def calculate_strength_score_indicator(volume, vol_sma, close, supertrend_line, atr):
    if vol_sma <= 0 or atr <= 0:
        return 0.0
    vol_ratio = volume / vol_sma
    momentum = abs(close - supertrend_line) / atr
    strength_score = math.log(vol_ratio + 1) * momentum
    return min(strength_score, 10.0)

def get_strength_emoji(score):
    if score >= 8.5: return "🔥"
    elif score >= 7.5: return "⭐"
    elif score >= 6.5: return "✅"
    elif score >= 5.5: return "🟢"
    elif score >= 4.5: return "🟡"
    else: return "⚪"

# ==== Signal Detection ====
def detect_signals(symbol):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=100"
        candles = session.get(url, timeout=5).json()
        if not candles or isinstance(candles, dict) or len(candles) < 30:
            return None

        last_idx = len(candles) - 2
        last_candle = candles[last_idx]
        prev_candle = candles[last_idx - 1]

        candle_time = datetime.fromtimestamp(last_candle[0]/1000, tz=timezone.utc)
        hour = candle_time.strftime("%Y-%m-%d %H:00")

        prev_close = float(prev_candle[4])
        open_p = float(last_candle[1])
        high = float(last_candle[2])
        low = float(last_candle[3])
        close = float(last_candle[4])
        volume = float(last_candle[5])
        vol_usdt = open_p * volume
        pct = ((close - prev_close) / prev_close) * 100

        st_result = calculate_supertrend_vawma(
            candles[:last_idx+1],
            atr_period=ATR_PERIOD,
            multiplier=MULTIPLIER,
            close_bars=CLOSE_BARS
        )
        if not st_result or not st_result['reversal'][last_idx]:
            return None

        atr = st_result['atr'][last_idx] or 1e-8
        up_band = st_result['up'][last_idx]
        dn_band = st_result['dn'][last_idx]
        current_trend = st_result['trend'][last_idx]

        if current_trend != 1:
            return None

        # Volume SMA (base volume)
        vol_ma_start = max(0, last_idx - 20 + 1)
        vol_ma_data = [float(candles[j][5]) for j in range(vol_ma_start, last_idx + 1)]
        vol_sma = sum(vol_ma_data) / len(vol_ma_data) if vol_ma_data else volume
        vm = volume / vol_sma if vol_sma > 0 else 1.0

        # Strength score
        supertrend_line = up_band  # since trend=1
        indicator_strength = calculate_strength_score_indicator(volume, vol_sma, close, supertrend_line, atr)

        # csince: candles since last reversal
        csince = 500
        for look_back in range(1, min(499, last_idx)):
            idx = last_idx - look_back
            if idx < ATR_PERIOD + CLOSE_BARS:
                break
            past_st = calculate_supertrend_vawma(candles[:idx+1], ATR_PERIOD, MULTIPLIER, CLOSE_BARS)
            if past_st and past_st['reversal'][idx]:
                csince = look_back
                break

        return {
            'breakout': {
                'symbol': symbol,
                'hour': hour,
                'pct': pct,
                'close': close,
                'supertrend_line': supertrend_line,
                'csince': csince,
                'vol_usdt': vol_usdt,
                'vm': vm,
                'indicator_strength': indicator_strength
            }
        }

    except Exception as e:
        return None

# ==== RSI Fetch ====
def calculate_rsi_for_signal(symbol):
    try:
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=25"
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
        print(f"  📝 Logged {signal_type} to file")
    except Exception as e:
        print(f"  ⚠️ Failed to log to file: {e}")

def send_telegram(msg, max_retries=3):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials not set!")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(max_retries):
        try:
            response = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML"
            }, timeout=10)
            if response.status_code == 200:
                print(f"  ✅ Alert sent to Telegram (attempt {attempt + 1})")
                return True
            else:
                print(f"  ⚠️ Telegram API returned status {response.status_code}")
        except Exception as e:
            print(f"  ⚠️ Telegram error: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
    print(f"  ❌ Failed to send to Telegram after {max_retries} attempts")
    return False

# ==== Main Scan ====
def scan_all_symbols(symbols):
    signal_candidates = []
    print(f"🔍 Scanning for confirmed SuperTrend+ buy signals...")
    scan_start = time.time()

    with ThreadPoolExecutor(max_workers=100) as ex:
        futures = {ex.submit(detect_signals, s): s for s in symbols}
        for f in as_completed(futures):
            result = f.result()
            if result and 'breakout' in result:
                signal_candidates.append(result)

    scan_duration = time.time() - scan_start
    print(f"✓ Scan completed in {scan_duration:.2f}s | Found: {len(signal_candidates)} signals")

    final_breakouts = []
    if signal_candidates:
        print("🔬 Calculating RSI...")
        rsi_start = time.time()
        with ThreadPoolExecutor(max_workers=50) as ex:
            futures = {ex.submit(calculate_rsi_for_signal, list(r.values())[0]['symbol']): r for r in signal_candidates}
            for f in as_completed(futures):
                rsi = f.result()
                result = futures[f]
                if rsi is not None:
                    b = result['breakout']
                    b['rsi'] = rsi
                    if (b['indicator_strength'] >= MIN_STRENGTH_SCORE and
                        b['csince'] >= MIN_CSINCE and
                        b['vm'] >= MIN_VOLUME_MULT):
                        final_breakouts.append(b)
        print(f"✓ RSI done in {time.time() - rsi_start:.2f}s")

    return {'breakouts': final_breakouts, 'retests': []}

# ==== Report ====
def format_signal_report(signals, duration):
    breakouts = signals['breakouts']
    if not breakouts:
        return None
    report = f"🚀 <b>SUPERTREND+ BUY SIGNALS</b> 🚀\n"
    report += f"⏱ Scan: {duration:.2f}s | Signals: {len(breakouts)}\n\n"

    grouped = defaultdict(list)
    for b in breakouts:
        grouped[b['hour']].append(b)

    for hour in sorted(grouped.keys(), reverse=True):
        report += f"⏰ {hour} UTC\n"
        items = sorted(grouped[hour], key=lambda x: x['indicator_strength'], reverse=True)
        for b in items:
            sym = b['symbol'].replace("USDT", "")
            rsi_str = f"{b['rsi']:.1f}"
            csince_str = f"{b['csince']:03d}"
            ind_str = f"{b['indicator_strength']:.2f}"
            line1 = f"{sym:6s} {b['pct']:5.2f}% {rsi_str:>4s} {b['vm']:4.1f}x {format_volume(b['vol_usdt']):4s}M {csince_str} {ind_str}"
            line2 = f"       🟢ST: ${b['supertrend_line']:.5f}"
            report += f"<code>{line1}</code>\n<code>{line2}</code>\n"
        report += "\n"

    report += "💡 <b>Legend:</b>\n"
    report += "SYMBOL %CHG RSI VMx VolM CSINCE STRENGTH\n"
    report += "ST = SuperTrend Line | Confirmed Buy Signal (2-bar close)\n"
    return report

# ==== Main Loop ====
def main():
    print("="*80)
    print("🚀 SUPERSTREND+ SCANNER (VAWMA ATR=10, CONFIRMED BUYS)")
    print("="*80)
    print(f"📊 ATR Period: {ATR_PERIOD} | Multiplier: {MULTIPLIER} | Confirmation: {CLOSE_BARS} bars")
    print(f"📝 Log File: {LOG_FILE}")
    print("="*80)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured!")

    symbols = get_usdt_pairs()
    if not symbols:
        print("❌ No symbols. Exiting.")
        return

    print(f"✓ Monitoring {len(symbols)} pairs\n")

    while True:
        now = datetime.now(timezone.utc)
        print(f"\n{'='*80}\n🕐 Scan started: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC\n{'='*80}")

        total_start = time.time()
        signals = scan_all_symbols(symbols)
        total_duration = time.time() - total_start

        fresh_breakouts = []
        for b in signals['breakouts']:
            key = ('BUY', b['symbol'], b['hour'])
            if key not in reported_signals:
                reported_signals.add(key)
                fresh_breakouts.append(b)
                log_signal_to_file(b, 'buy_signal')

        if fresh_breakouts:
            print(f"\n🆕 {len(fresh_breakouts)} new buy signal(s) detected")
            msg = format_signal_report({'breakouts': fresh_breakouts, 'retests': []}, total_duration)
            if msg:
                success = send_telegram(msg[:4096])
                if not success:
                    for b in fresh_breakouts:
                        reported_signals.discard(('BUY', b['symbol'], b['hour']))
        else:
            print("\n  ℹ️ No new signals")

        server_time = get_binance_server_time()
        next_hour = (server_time // 3600 + 1) * 3600
        sleep_time = max(60, next_hour - server_time + 5)
        print(f"\n😴 Sleeping {sleep_time:.0f}s until next hour...")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
