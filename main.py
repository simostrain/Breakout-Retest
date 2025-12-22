import os
import requests
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ==== ENV VARIABLES ====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

# ==== SETTINGS ====
BINANCE_API = "https://api.binance.com"
PUMP_THRESHOLD = 2.9
reported = set()

CUSTOM_TICKERS = [
    "BTC","ETH","SOL","BNB","ADA","XRP","DOGE","AVAX","DOT","LINK",
    "OP","ARB","NEAR","MATIC","TRX","LTC","ATOM","ETC","FIL","ICP",
    "IMX","INJ","AAVE","SAND","APE","GALA","FTM","RUNE","EGLD"
]

# ==== SESSION ====
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=30, pool_maxsize=30, max_retries=2)
session.mount("https://", adapter)

# ==== TELEGRAM ====
def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg
        }, timeout=30)
    except Exception as e:
        print("Telegram error:", e)

# ==== HELPERS ====
def format_volume(v):
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v/1_000:.2f}K"
    return str(int(v))

def get_usdt_pairs():
    url = f"{BINANCE_API}/api/v3/exchangeInfo"
    data = session.get(url, timeout=30).json()
    allowed = {s["symbol"] for s in data["symbols"]
               if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
    return [t + "USDT" for t in CUSTOM_TICKERS if t + "USDT" in allowed]

def fetch(symbol):
    url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval=1h&limit=30"
    candles = session.get(url, timeout=30).json()
    results = []

    for i, c in enumerate(candles[:-1]):
        o, h, l, cl = map(float, c[1:5])
        vol = o * float(c[5])
        pct = (cl - o) / o * 100

        if pct < PUMP_THRESHOLD:
            continue

        rng = h - l
        cr = ((cl - l) / rng) * 100 if rng > 0 else 50
        ma = sum(float(x[1]) * float(x[5]) for x in candles[max(0,i-19):i+1]) / max(1, min(i+1, 20))
        vr = vol / ma if ma > 0 else 1

        hour = datetime.fromtimestamp(c[0]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:00")
        results.append((symbol, pct, cl, hour, vol, cr, vr))

    return results

def scan(symbols):
    pumps = []
    with ThreadPoolExecutor(max_workers=30) as ex:
        futures = [ex.submit(fetch, s) for s in symbols]
        for f in as_completed(futures):
            pumps.extend(f.result())
    return pumps

def format_report(pumps, dur):
    grouped = defaultdict(list)
    for p in pumps:
        grouped[p[3]].append(p)

    msg = f"⏱ {dur:.1f}s\n\n"
    for hour in sorted(grouped):
        msg += f"=== {hour} UTC ===\n"
        for s,p,cl,_,v,cr,vr in sorted(grouped[hour], key=lambda x: x[6], reverse=True):
            tag = "🔥" if vr >= 1.5 and cr >= 80 else "  "
            msg += f"{tag}{s.replace('USDT',''):6} {p:4.1f}%  Close:{cl:g}\n"
            msg += f"      V:{format_volume(v)} VM:{vr:.1f}x CR:{cr:.0f}%\n"
        msg += "\n"
    return msg

def main():
    symbols = get_usdt_pairs()
    send_telegram(f"🚀 Scanner started ({len(symbols)} pairs)")

    while True:
        start = time.time()
        pumps = scan(symbols)
        new = []

        for p in pumps:
            key = (p[0], p[3])
            if key not in reported:
                reported.add(key)
                new.append(p)

        if new:
            send_telegram(format_report(new, time.time()-start)[:4096])

        time.sleep(3600)

if __name__ == "__main__":
    main()
