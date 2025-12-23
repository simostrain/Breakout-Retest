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
RSI_PERIOD = 14  # standard RSI period
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

# ==== RSI Calculation ====
def calculate_rsi_with_full_history(closes, period=14):
    """
    Calculate RSI using ALL available historical data for proper RMA convergence.
    This matches TradingView/Binance exactly because RMA needs full history to converge properly.
    
    TradingView uses: RSI = 100 - (100 / (1 + RS))
    Where RS = RMA(gains, period) / RMA(losses, period)
    """
    if len(closes) < period + 1:
        return None
    
    # Calculate all price changes
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    
    # Separate gains and losses
    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]
    
    # Calculate initial RMA using SMA of first 'period' values
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    # Apply Wilder's smoothing (RMA) to ALL remaining values
    # This is the key - we need to smooth through ALL data, not just stop at period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    
    # Calculate final RSI
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    
    return round(rsi, 2)

def calculate_rsi(candles, current_index, period=14):
    """
    Calculate RSI using RMA (Rolling Moving Average) - matches TradingView/Binance exactly.
    TradingView uses: RSI = 100 - (100 / (1 + RS))
    Where RS = RMA(gains, period) / RMA(losses, period)
    RMA is Wilder's smoothing method.
    """
    # Need at least period + 1 candles
    if current_index < period:
        return None
    
    # Get closing prices - we need period+1 closes to get period changes
    closes = [float(candles[i][4]) for i in range(current_index - period, current_index + 1)]
    
    if len(closes) < period + 1:
        return None
    
    # Calculate all price changes first
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    
    # Separate into gains and losses
    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]
    
    # Calculate RMA (Wilder's smoothing)
    # First value is SMA of first 'period' values
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    # Then apply smoothing for remaining values
    # RMA formula: (previous_RMA * (period-1) + current_value) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    
    # Calculate RS and RSI
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    
    return round(rsi, 2)

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
        # Fetch more candles to have enough data for RSI calculation
        # Need at least RSI_PERIOD + 1 candles before the pump
        # Using 200 to ensure we have plenty of historical data
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=200"
        candles = session.get(url, timeout=60).json()
        if not candles or isinstance(candles, dict):
            return []

        results = []
        for i, c in enumerate(candles):
            candle_time = datetime.fromtimestamp(c[0]/1000, tz=timezone.utc)
            if candle_time < start_time or candle_time >= now_utc - timedelta(hours=1):
                continue

            # Skip first candle as we need previous close for percentage calculation
            if i == 0:
                continue

            prev_close = float(candles[i-1][4])  # Previous candle's close
            open_p = float(c[1])
            high = float(c[2])
            low = float(c[3])
            close = float(c[4])
            volume = float(c[5])
            vol_usdt = open_p * volume

            # Calculate percentage change from previous close to current close (Binance method)
            pct = ((close - prev_close) / prev_close) * 100
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

            # === Calculate RSI ===
            # Use ALL candles up to current index for proper RMA convergence
            # This matches how TradingView/Binance calculates RSI
            if i >= RSI_PERIOD:
                # Get all closes from start up to current candle
                all_closes = [float(candles[j][4]) for j in range(0, i + 1)]
                rsi = calculate_rsi_with_full_history(all_closes, RSI_PERIOD)
            else:
                rsi = None

            # === Prices ===
            buy = (open_p + close) / 2
            sell = buy * 1.022
            sl = buy * 0.99

            hour = candle_time.strftime("%Y-%m-%d %H:00")
            results.append((symbol, pct, close, buy, sell, sl, hour, vol_usdt, cr, vm, rsi))

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
    for s, pct, c, b, se, sl, h, v, cr, vm, rsi in pumps:
        grouped[h].append((s, pct, c, b, se, sl, v, cr, vm, rsi))

    report = f"⏱ Scan: {duration:.2f}s\n\n"
    
    for h in sorted(grouped):
        items = sorted(grouped[h], key=lambda x: x[8], reverse=True)
        report += f"{'='*70}\n"
        report += f"  {h} UTC\n"
        report += f"{'='*70}\n"
        
        for s, pct, c, b, se, sl, v, cr, vm, rsi in items:
            sym = s.replace("USDT","")
            
            # Determine emoji based on RSI and other factors
            if vm >= 1.5 and cr >= 80:
                if rsi and rsi >= 70:
                    mark = "🔥⚠️"
                else:
                    mark = "🔥 "
            elif rsi and rsi >= 70:
                mark = "⚠️ "
            else:
                mark = "  "
            
            rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
            
            # Two-line format: prices on line 1, metrics on line 2
            report += f"{mark} {sym:8s} │ {pct:5.2f}% │ RSI:{rsi_str:5s} │ C:{c:.6g}\n"
            report += f"{'':13s} │ VM:{vm:.1f}x │ Vol:{format_volume(v):7s} │ CR:{cr:.0f}%\n"
        
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
