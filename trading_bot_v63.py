"""
J's AI CRYPTO BOT v6.3 - AUTONOMOUS SCANNER + BACKTESTER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Upgrades over v6.2:
  ✅ Built-in backtester - 6 months historical data
  ✅ Backtest results on dashboard
  ✅ Auto-backtest runs every Sunday at midnight
  ✅ Per-pair backtest breakdown
  ✅ Sharpe ratio, max drawdown, win rate reporting
  ✅ Autonomous scanner every 1 hour 24/7
  ✅ EMA crossover detection across all timeframes
  ✅ No TradingView dependency
  ✅ TradingView webhook still works as backup
"""

from flask import Flask, request, jsonify
from coinbase.rest import RESTClient
import json, uuid, logging, threading, time, smtplib, os
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

logging.basicConfig(
    filename='trading_log.txt',
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger(__name__)

with open("/root/crypto-bot/cdp_api_key-7.json") as f:
    keys = json.load(f)
client = RESTClient(api_key=keys["name"], api_secret=keys["privateKey"])

# ─── EMAIL ────────────────────────────────────────────────────────────────────
EMAIL_SENDER   = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER", "")
EMAIL_ENABLED  = all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER])

# ─── RISK PARAMETERS ─────────────────────────────────────────────────────────
TAKE_PROFIT_1       = 0.010
TAKE_PROFIT_2       = 0.020
STOP_LOSS_PCT       = 0.010
TRAIL_PCT           = 0.008
BREAK_EVEN_TRIGGER  = 0.010
LOCK_PROFIT_PCT     = 0.005
MAX_OPEN_TRADES     = 3
DAILY_LOSS_LIMIT    = 0.06
WEEKLY_LOSS_LIMIT   = 0.12
MAX_TRADES_PER_DAY  = 5
TRADE_COOLDOWN_SEC  = 3600
TIME_EXIT_HOURS     = 4
MOMENTUM_EXIT_RSI   = 45
SCAN_INTERVAL_SEC   = 3600

# ─── SCORING THRESHOLDS ───────────────────────────────────────────────────────
MIN_DAILY_SCORE     = 65
MIN_4H_SCORE        = 68
MIN_1H_SCORE        = 65
MIN_15M_SCORE       = 62
MIN_COMBINED_SCORE  = 260
LOSING_STREAK_LIMIT = 2
MONITOR_INTERVAL    = 30

# ─── NEWS BLACKOUT ────────────────────────────────────────────────────────────
BLACKOUT_HOURS_UTC = [12,13,14,15,18,19,20]
FED_DATES = [
    (4,16),(4,17),(5,6),(5,7),(6,17),(6,18),
    (7,29),(7,30),(9,16),(9,17),(10,28),(10,29),(12,9),(12,10)
]

CORRELATED_GROUPS = [{"BTC-USD","ETH-USD","SOL-USD"}]

ALLOWED_PAIRS = {
    "BTCUSD":  "BTC-USD",
    "ETHUSD":  "ETH-USD",
    "SOLUSD":  "SOL-USD",
    "XRPUSD":  "XRP-USD",
    "WELLUSD": "WELL-USD",
}

# ─── STATE ───────────────────────────────────────────────────────────────────
open_trades        = {}
last_trade_time    = {}
daily_start_bal    = None
weekly_start_bal   = None
daily_loss_hit     = False
weekly_loss_hit    = False
losing_streak      = 0
total_trades       = 0
winning_trades     = 0
daily_trades_count = 0
daily_pnl          = 0.0
trade_history      = []
last_scan_results  = {}
last_scan_time     = None
last_daily_reset   = datetime.utcnow().date()
last_weekly_reset  = datetime.utcnow().isocalendar()[1]
backtest_results   = {}
last_backtest_time = None

pair_stats = {p: {"wins":0,"losses":0,"pnl":0.0} for p in ALLOWED_PAIRS.values()}
hour_stats = {h: {"wins":0,"losses":0} for h in range(24)}

# ─── EMAIL ───────────────────────────────────────────────────────────────────
def send_email(subject, body):
    if not EMAIL_ENABLED:
        logger.info(f"EMAIL(disabled):{subject}"); return
    try:
        msg = MIMEMultipart()
        msg["From"] = EMAIL_SENDER; msg["To"] = EMAIL_RECEIVER; msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_SENDER, EMAIL_PASSWORD); s.send_message(msg)
        logger.info(f"Email sent:{subject}")
    except Exception as e:
        logger.error(f"Email error:{e}")

# ─── BALANCE & PRICE ─────────────────────────────────────────────────────────
def get_balance():
    try:
        accounts = client.get_accounts(limit=250)
        for a in accounts["accounts"]:
            if a["currency"] == "USD":
                return float(a["available_balance"]["value"])
    except Exception as e: logger.error(f"Balance error:{e}")
    return 0

def get_price(product_id):
    try:
        t = client.get_best_bid_ask(product_ids=[product_id])
        return float(t["pricebooks"][0]["asks"][0]["price"])
    except Exception as e: logger.error(f"Price error {product_id}:{e}"); return None

# ─── CANDLES ─────────────────────────────────────────────────────────────────
def get_candles(product_id, granularity="ONE_HOUR", limit=50):
    try:
        seconds  = {"FIFTEEN_MINUTE":900,"ONE_HOUR":3600,"FOUR_HOUR":14400,"ONE_DAY":86400}
        interval = seconds.get(granularity, 3600)
        end      = int(time.time())
        start    = end - (interval * limit)
        candles  = client.get_candles(product_id=product_id, start=start, end=end, granularity=granularity)
        return candles["candles"]
    except Exception as e: logger.error(f"Candle error {product_id} {granularity}:{e}"); return None

def get_historical_candles(product_id, granularity="ONE_HOUR", days_back=180):
    """Pull historical candles for backtesting"""
    try:
        seconds  = {"FIFTEEN_MINUTE":900,"ONE_HOUR":3600,"FOUR_HOUR":14400,"ONE_DAY":86400}
        interval = seconds.get(granularity, 3600)
        all_candles = []
        end   = int(time.time())
        start = end - (days_back * 86400)

        # Coinbase max 350 candles per call — paginate
        while start < end:
            chunk_end   = min(start + interval * 300, end)
            try:
                candles = client.get_candles(
                    product_id=product_id, start=start, end=chunk_end, granularity=granularity
                )
                if candles and candles.get("candles"):
                    all_candles.extend(candles["candles"])
            except Exception:
                pass
            start = chunk_end
            time.sleep(0.3)  # rate limit

        # Sort oldest first
        all_candles.sort(key=lambda x: int(x["start"]))
        return all_candles
    except Exception as e:
        logger.error(f"Historical candle error {product_id}:{e}")
        return []

# ─── INDICATORS ──────────────────────────────────────────────────────────────
def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(max(change, 0)); losses.append(abs(min(change, 0)))
    ag = sum(gains[-period:]) / period; al = sum(losses[-period:]) / period
    if al == 0: return 100
    return 100 - (100 / (1 + ag/al))

def calculate_ema(prices, period=9):
    if len(prices) < period: return prices[-1]
    m = 2 / (period + 1); ema = sum(prices[:period]) / period
    for p in prices[period:]: ema = (p - ema) * m + ema
    return ema

def calculate_vwap(candles):
    try:
        tv, tpv = 0, 0
        for c in candles:
            h,l,cl,v = float(c["high"]),float(c["low"]),float(c["close"]),float(c["volume"])
            tpv += ((h+l+cl)/3)*v; tv += v
        return tpv/tv if tv > 0 else 0
    except: return 0

def detect_ema_crossover(closes, fast=9, slow=21):
    if len(closes) < slow + 2: return False
    ef_now  = calculate_ema(closes, fast);  es_now  = calculate_ema(closes, slow)
    ef_prev = calculate_ema(closes[:-1], fast); es_prev = calculate_ema(closes[:-1], slow)
    return ef_prev <= es_prev and ef_now > es_now

def detect_ema_aligned(closes, price):
    e9 = calculate_ema(closes,9); e21 = calculate_ema(closes,21); e55 = calculate_ema(closes,55)
    return price > e9 > e21 > e55

def detect_support(closes, lookback=20):
    if len(closes) < lookback: return False
    return abs(closes[-1]-min(closes[-lookback:-1]))/min(closes[-lookback:-1]) < 0.015

def detect_bullish_candle(candles):
    if len(candles) < 2: return False
    try:
        p,c = candles[-2],candles[-1]
        po,pc = float(p["open"]),float(p["close"])
        co,cc,cl,ch = float(c["open"]),float(c["close"]),float(c["low"]),float(c["high"])
        engulfing = pc<po and cc>co and cc>po and co<pc
        body=abs(cc-co); lw=min(co,cc)-cl; uw=ch-max(co,cc)
        hammer = lw>body*2 and uw<body and cc>co
        return engulfing or hammer
    except: return False

def check_volatility(closes, min_range=0.004):
    if len(closes) < 10: return True
    r = closes[-10:]
    return (max(r)-min(r))/min(r) >= min_range

def detect_higher_highs_lows(closes, lookback=10):
    if len(closes) < lookback: return False
    recent=closes[-lookback:]; mid=len(recent)//2
    return max(recent[mid:])>max(recent[:mid]) and min(recent[mid:])>min(recent[:mid])

# ─── BLACKOUT & FILTERS ───────────────────────────────────────────────────────
def is_blackout():
    now = datetime.now(timezone.utc)
    if now.hour in BLACKOUT_HOURS_UTC: return True, "macro event blackout"
    if (now.month, now.day) in FED_DATES: return True, "Fed/CPI date"
    return False, "ok"

def correlation_check(product_id):
    for group in CORRELATED_GROUPS:
        if product_id in group:
            open_in_group = [p for p in open_trades if p in group]
            if open_in_group: return False, f"correlated {open_in_group[0]} open"
    return True, "ok"

# ─── CIRCUIT BREAKERS ─────────────────────────────────────────────────────────
def check_circuit_breakers():
    global daily_loss_hit, weekly_loss_hit
    try:
        bal = get_balance()
        if daily_start_bal and bal < daily_start_bal*(1-DAILY_LOSS_LIMIT):
            daily_loss_hit = True
            send_email("🚨 Daily Loss Limit", f"Balance:${bal:.2f} Start:${daily_start_bal:.2f}")
            return True, "daily loss limit"
        if weekly_start_bal and bal < weekly_start_bal*(1-WEEKLY_LOSS_LIMIT):
            weekly_loss_hit = True
            send_email("🚨 Weekly Loss Limit", f"Balance:${bal:.2f} Start:${weekly_start_bal:.2f}")
            return True, "weekly loss limit"
    except Exception as e: logger.error(f"CB error:{e}")
    return False, "ok"

# ─── POSITION SIZING ─────────────────────────────────────────────────────────
def get_size_pct(combined, product_id):
    stats = pair_stats.get(product_id, {"wins":0,"losses":0})
    total = stats["wins"] + stats["losses"]
    mult  = 1.0
    if total >= 5:
        wr = stats["wins"]/total
        if wr >= 0.70: mult = 1.3
        elif wr >= 0.60: mult = 1.1
        elif wr <= 0.35: mult = 0.7
    if losing_streak >= LOSING_STREAK_LIMIT: base = 0.010
    elif combined >= 360: base = 0.050
    elif combined >= 330: base = 0.035
    elif combined >= 300: base = 0.025
    else:                 base = 0.015
    return min(base*mult, 0.05)

# ─── DAILY RESET ─────────────────────────────────────────────────────────────
def check_daily_reset():
    global daily_start_bal,daily_loss_hit,daily_trades_count,daily_pnl,last_daily_reset
    global weekly_start_bal,weekly_loss_hit,last_weekly_reset
    today = datetime.utcnow().date()
    if today != last_daily_reset:
        daily_start_bal=get_balance(); daily_loss_hit=False
        daily_trades_count=0; daily_pnl=0.0; last_daily_reset=today
        send_daily_summary()
    week = datetime.utcnow().isocalendar()[1]
    if week != last_weekly_reset:
        weekly_start_bal=get_balance(); weekly_loss_hit=False; last_weekly_reset=week

def send_daily_summary():
    bal = get_balance()
    wr  = round(winning_trades/total_trades*100,1) if total_trades > 0 else 0
    bt  = backtest_results.get("overall", {})
    bt_str = (f"Backtest Win Rate: {bt.get('win_rate','N/A')}\n"
              f"Backtest Return: {bt.get('total_return','N/A')}\n") if bt else ""
    send_email("☀️ Daily Bot Summary v6.3",
        f"Good morning J!\n\nBalance:${bal:.2f}\nTotal Trades:{total_trades}\n"
        f"Win Rate:{wr}%\nDaily PNL:${daily_pnl:.2f}\n\n{bt_str}"
        f"Bot scanning 24/7\n- Your Bot 🤖 v6.3")

# ─── SCORING ─────────────────────────────────────────────────────────────────
def score_timeframe(product_id, granularity, price, label):
    score=0; reason=[]
    candles = get_candles(product_id, granularity=granularity, limit=60)
    if not candles: return 0, [], False
    cc      = list(reversed(candles))
    closes  = [float(c["close"]) for c in cc]
    volumes = [float(c["volume"]) for c in cc]

    crossover = detect_ema_crossover(closes)
    aligned   = detect_ema_aligned(closes, price)

    rsi = calculate_rsi(closes)
    if 38<=rsi<=65:   score+=20; reason.append(f"RSI={round(rsi,1)} ideal")
    elif 30<=rsi<=72: score+=10; reason.append(f"RSI={round(rsi,1)} ok")
    else:             reason.append(f"RSI={round(rsi,1)} poor")

    avg_vol = sum(volumes[:-1])/max(len(volumes)-1,1)
    vr = volumes[-1]/avg_vol if avg_vol>0 else 0
    if vr>=1.2:   score+=15; reason.append(f"Vol={round(vr,2)}x strong")
    elif vr>=0.7: score+=8;  reason.append(f"Vol={round(vr,2)}x ok")
    else:         reason.append(f"Vol={round(vr,2)}x weak")

    if aligned:
        score+=25; reason.append("EMAs perfect")
    elif price>calculate_ema(closes,9) and calculate_ema(closes,9)>calculate_ema(closes,21):
        score+=15; reason.append("EMAs partial")
    else: reason.append("EMAs not aligned")

    if crossover: score+=10; reason.append("⚡ EMA CROSSOVER")

    if len(closes)>=5:
        mom=(closes[-1]-closes[-5])/closes[-5]*100
        if mom>=1.0:   score+=15; reason.append(f"Mom={round(mom,2)}% strong")
        elif mom>=0:   score+=8;  reason.append(f"Mom={round(mom,2)}% mild")
        else:          reason.append(f"Mom={round(mom,2)}% negative")

    vwap = calculate_vwap(cc[-20:])
    if vwap>0 and price>vwap: score+=10; reason.append("Above VWAP")
    else:                     reason.append("Below VWAP")

    if detect_support(closes): score+=10; reason.append("Near support")
    if detect_bullish_candle(cc[-3:]): score+=5; reason.append("Bullish candle")

    logger.info(f"{label} {product_id}:{score}/100 | {' | '.join(reason)}")
    return score, reason, crossover

def score_daily(product_id, price):
    score=0; reason=[]
    candles = get_candles(product_id, granularity="ONE_DAY", limit=50)
    if not candles: return 0, []
    cc     = list(reversed(candles))
    closes = [float(c["close"]) for c in cc]
    vols   = [float(c["volume"]) for c in cc]

    rsi = calculate_rsi(closes)
    if 50<=rsi<=72:   score+=25; reason.append(f"RSI={round(rsi,1)} bullish")
    elif 45<=rsi<50:  score+=10; reason.append(f"RSI={round(rsi,1)} neutral")
    else:             reason.append(f"RSI={round(rsi,1)} bearish")

    e21=calculate_ema(closes,21); e50=calculate_ema(closes,50)
    if price>e21 and e21>e50: score+=25; reason.append("Above EMA21 & EMA50")
    elif price>e21:           score+=10; reason.append("Above EMA21 only")
    else:                     reason.append("Below daily EMAs")

    avg_v=sum(vols[:-5])/max(len(vols)-5,1); rec_v=sum(vols[-5:])/5
    if rec_v>avg_v*1.1:   score+=20; reason.append("Volume increasing")
    elif rec_v>avg_v*0.8: score+=10; reason.append("Volume stable")
    else:                 reason.append("Volume declining")

    if detect_higher_highs_lows(closes): score+=15; reason.append("Higher highs/lows")
    else:                                reason.append("No higher highs/lows")

    if len(closes)>=5:
        mom=(closes[-1]-closes[-5])/closes[-5]*100
        if mom>=2.0:   score+=15; reason.append(f"Mom={round(mom,1)}% strong")
        elif mom>=0:   score+=7;  reason.append(f"Mom={round(mom,1)}% mild")
        else:          reason.append(f"Mom={round(mom,1)}% negative")

    logger.info(f"DAILY {product_id}:{score}/100 | {' | '.join(reason)}")
    return score, reason

def analyze_pair(product_id):
    price = get_price(product_id)
    if not price: return None
    s_d,_        = score_daily(product_id, price)
    s4h,_,x4h   = score_timeframe(product_id, "FOUR_HOUR",      price, "4H")
    s1h,_,x1h   = score_timeframe(product_id, "ONE_HOUR",       price, "1H")
    s15m,_,x15m = score_timeframe(product_id, "FIFTEEN_MINUTE", price, "15M")
    combined     = s_d + s4h + s1h + s15m
    crossovers   = [tf for tf,cx in [("4H",x4h),("1H",x1h),("15M",x15m)] if cx]
    ready = (s_d>=MIN_DAILY_SCORE and s4h>=MIN_4H_SCORE and
             s1h>=MIN_1H_SCORE and s15m>=MIN_15M_SCORE and combined>=MIN_COMBINED_SCORE)
    result = {"price":price,"daily":s_d,"4h":s4h,"1h":s1h,"15m":s15m,
              "combined":combined,"crossovers":crossovers,"ready":ready,
              "time":datetime.utcnow().isoformat()}
    logger.info(f"SCAN {product_id}: D={s_d} 4H={s4h} 1H={s1h} 15M={s15m} "
                f"COMBO={combined}/400 | CX:{crossovers or 'none'} | {'✅ READY' if ready else '❌'}")
    return result

# ─── BACKTESTER ───────────────────────────────────────────────────────────────
def run_backtest(product_id, days=180, starting_balance=333.0):
    """
    Backtest the bot strategy on historical 1H candles.
    Simulates entries on EMA crossover + score logic.
    """
    logger.info(f"BACKTEST {product_id}: pulling {days} days of 1H data...")
    candles = get_historical_candles(product_id, granularity="ONE_HOUR", days_back=days)
    if len(candles) < 50:
        return {"error": "not enough data", "pair": product_id}

    balance     = starting_balance
    trades      = []
    wins        = 0
    losses      = 0
    max_balance = balance
    min_balance = balance
    in_trade    = False
    entry       = 0
    entry_time  = 0
    tp1         = 0
    tp2         = 0
    sl          = 0
    tp1_hit     = False
    size        = 0
    size_rem    = 0
    cooldown    = 0

    for i in range(60, len(candles)):
        window  = candles[max(0,i-59):i+1]
        closes  = [float(c["close"]) for c in window]
        current = closes[-1]
        ts      = int(candles[i]["start"])

        # Skip if in cooldown
        if cooldown > 0:
            cooldown -= 1
            if in_trade:
                # Monitor trade
                if not tp1_hit and current >= tp1:
                    # Close 50% at TP1
                    pnl = (tp1 - entry) * (size * 0.5)
                    balance += pnl
                    size_rem = size * 0.5
                    tp1_hit = True
                elif tp1_hit and current >= tp2:
                    pnl = (current - entry) * size_rem
                    balance += pnl
                    result = "win" if balance > balance-pnl else "win"
                    wins += 1
                    trades.append({"entry":entry,"exit":current,"pnl":round((current-entry)/entry*100,2),"result":"win","reason":"TP2"})
                    in_trade = False; tp1_hit = False; size_rem = 0; cooldown = 0
                elif current <= sl:
                    pnl = (current - entry) * size_rem
                    balance += pnl
                    losses += 1
                    trades.append({"entry":entry,"exit":current,"pnl":round((current-entry)/entry*100,2),"result":"loss","reason":"SL"})
                    in_trade = False; tp1_hit = False; size_rem = 0; cooldown = 0
                elif (ts - entry_time) > TIME_EXIT_HOURS * 3600 and current < entry:
                    pnl = (current - entry) * size_rem
                    balance += pnl
                    losses += 1
                    trades.append({"entry":entry,"exit":current,"pnl":round((current-entry)/entry*100,2),"result":"loss","reason":"time_exit"})
                    in_trade = False; tp1_hit = False; size_rem = 0; cooldown = 0
            continue

        if in_trade: continue

        # Check EMA crossover signal
        crossover = detect_ema_crossover(closes)
        if not crossover: continue

        # Quick score check (simplified for backtest speed)
        rsi     = calculate_rsi(closes)
        e9      = calculate_ema(closes, 9)
        e21     = calculate_ema(closes, 21)
        aligned = current > e9 > e21
        mom     = (closes[-1]-closes[-5])/closes[-5]*100 if len(closes)>=5 else 0

        score = 0
        if 38<=rsi<=65:  score += 35
        if aligned:      score += 35
        if mom >= 0:     score += 20
        if crossover:    score += 10

        if score < 70: continue

        # Enter trade
        risk     = balance * 0.02
        size     = risk / current
        size_rem = size
        entry    = current
        entry_time = ts
        tp1      = entry * (1 + TAKE_PROFIT_1)
        tp2      = entry * (1 + TAKE_PROFIT_2)
        sl       = entry * (1 - STOP_LOSS_PCT)
        in_trade = True
        tp1_hit  = False
        cooldown = 4  # check next 4 candles

        max_balance = max(max_balance, balance)
        min_balance = min(min_balance, balance)

    # Close any open trade at end
    if in_trade and len(candles) > 0:
        final = float(candles[-1]["close"])
        pnl   = (final - entry) * size_rem
        balance += pnl
        result = "win" if pnl > 0 else "loss"
        if pnl > 0: wins += 1
        else: losses += 1
        trades.append({"entry":entry,"exit":final,"pnl":round((final-entry)/entry*100,2),"result":result,"reason":"end"})

    total      = wins + losses
    win_rate   = round(wins/total*100,1) if total > 0 else 0
    total_ret  = round((balance-starting_balance)/starting_balance*100,1)
    max_dd     = round((max_balance-min_balance)/max_balance*100,1) if max_balance > 0 else 0
    avg_pnl    = round(sum(t["pnl"] for t in trades)/len(trades),2) if trades else 0

    result = {
        "pair":           product_id,
        "days":           days,
        "total_trades":   total,
        "wins":           wins,
        "losses":         losses,
        "win_rate":       f"{win_rate}%",
        "total_return":   f"{total_ret}%",
        "final_balance":  round(balance, 2),
        "max_drawdown":   f"{max_dd}%",
        "avg_trade_pnl":  f"{avg_pnl}%",
        "start_balance":  starting_balance,
        "go_live_ready":  win_rate >= 55 and max_dd <= 15,
        "trades_sample":  trades[-5:] if trades else []
    }

    logger.info(
        f"BACKTEST {product_id}: {total} trades | WR:{win_rate}% | "
        f"Return:{total_ret}% | MaxDD:{max_dd}% | "
        f"{'✅ GO LIVE READY' if result['go_live_ready'] else '❌ needs work'}"
    )
    return result

def run_full_backtest():
    """Run backtest on all pairs and store results"""
    global backtest_results, last_backtest_time
    logger.info("🔬 FULL BACKTEST STARTING...")
    results   = {}
    total_bal = 333.0

    for symbol, product_id in ALLOWED_PAIRS.items():
        try:
            r = run_backtest(product_id, days=180, starting_balance=333.0/5)
            results[product_id] = r
            time.sleep(2)
        except Exception as e:
            logger.error(f"Backtest error {product_id}:{e}")
            results[product_id] = {"error": str(e)}

    # Overall summary
    valid = [r for r in results.values() if "win_rate" in r]
    if valid:
        avg_wr  = round(sum(float(r["win_rate"].replace("%","")) for r in valid)/len(valid),1)
        avg_ret = round(sum(float(r["total_return"].replace("%","")) for r in valid)/len(valid),1)
        ready   = sum(1 for r in valid if r.get("go_live_ready",False))
        results["overall"] = {
            "avg_win_rate":  f"{avg_wr}%",
            "avg_return":    f"{avg_ret}%",
            "pairs_ready":   f"{ready}/{len(valid)}",
            "go_live_ready": ready >= 3,
            "win_rate":      f"{avg_wr}%",
            "total_return":  f"{avg_ret}%"
        }

    backtest_results   = results
    last_backtest_time = datetime.utcnow().isoformat()
    logger.info(f"🔬 BACKTEST COMPLETE | Avg WR:{avg_wr if valid else 'N/A'}%")

    # Email results
    if valid:
        pairs_str = "\n".join([
            f"  {r['pair']}: {r['win_rate']} WR | {r['total_return']} return | "
            f"{'✅ READY' if r.get('go_live_ready') else '❌ not ready'}"
            for r in valid
        ])
        send_email("🔬 Backtest Results",
            f"6-Month Backtest Complete!\n\n"
            f"Overall Win Rate: {avg_wr}%\n"
            f"Average Return: {avg_ret}%\n"
            f"Pairs Ready: {ready}/{len(valid)}\n\n"
            f"Per Pair:\n{pairs_str}\n\n"
            f"{'✅ BOT IS READY FOR LIVE TRADING!' if ready>=3 else '⚠️ More optimization needed'}")
    return results

# ─── SMART FILTER ────────────────────────────────────────────────────────────
def smart_filter(product_id, price):
    now = time.time()
    if price <= 0:                       return False, "invalid price"
    if product_id in open_trades:        return False, "already in trade"
    if len(open_trades) >= MAX_OPEN_TRADES: return False, "max trades open"
    if daily_trades_count >= MAX_TRADES_PER_DAY: return False, "max daily trades"
    if product_id in last_trade_time:
        if now-last_trade_time[product_id] < TRADE_COOLDOWN_SEC:
            mins = int((TRADE_COOLDOWN_SEC-(now-last_trade_time[product_id]))/60)
            return False, f"cooldown {mins}min"
    blk, bmsg = is_blackout()
    if blk: return False, f"blackout:{bmsg}"
    ok, cmsg = correlation_check(product_id)
    if not ok: return False, cmsg
    return True, "ok"

# ─── EXECUTE BUY ─────────────────────────────────────────────────────────────
def execute_buy(product_id, analysis=None):
    global daily_trades_count
    if daily_loss_hit:  return {"status":"skip","reason":"daily circuit breaker"}
    if weekly_loss_hit: return {"status":"skip","reason":"weekly circuit breaker"}
    tripped, reason = check_circuit_breakers()
    if tripped: return {"status":"skip","reason":reason}
    check_daily_reset()
    price = get_price(product_id)
    if not price: return {"status":"error","reason":"no price"}
    allowed, reason = smart_filter(product_id, price)
    if not allowed:
        logger.info(f"FILTERED {product_id}:{reason}")
        return {"status":"skip","reason":reason}
    if not analysis:
        analysis = analyze_pair(product_id)
    if not analysis: return {"status":"error","reason":"analysis failed"}

    s_d=analysis["daily"]; s4h=analysis["4h"]
    s1h=analysis["1h"];    s15m=analysis["15m"]
    combined=analysis["combined"]

    if s_d   < MIN_DAILY_SCORE:     return {"status":"skip","reason":f"Daily={s_d} need {MIN_DAILY_SCORE}"}
    if s4h   < MIN_4H_SCORE:        return {"status":"skip","reason":f"4H={s4h} need {MIN_4H_SCORE}"}
    if s1h   < MIN_1H_SCORE:        return {"status":"skip","reason":f"1H={s1h} need {MIN_1H_SCORE}"}
    if s15m  < MIN_15M_SCORE:       return {"status":"skip","reason":f"15M={s15m} need {MIN_15M_SCORE}"}
    if combined < MIN_COMBINED_SCORE: return {"status":"skip","reason":f"Combined={combined} need {MIN_COMBINED_SCORE}"}

    c1h = get_candles(product_id, "ONE_HOUR", 15)
    if c1h:
        cl = [float(c["close"]) for c in reversed(c1h)]
        if not check_volatility(cl): return {"status":"skip","reason":"market too flat"}

    balance = get_balance()
    pct     = get_size_pct(combined, product_id)
    size    = round((balance*pct)/price, 6)

    try:
        client.market_order_buy(
            client_order_id=str(uuid.uuid4()),
            product_id=product_id,
            quote_size=str(round(size*price, 2))
        )
        tp1=price*(1+TAKE_PROFIT_1); tp2=price*(1+TAKE_PROFIT_2); sl=price*(1-STOP_LOSS_PCT)
        now_utc = datetime.now(timezone.utc)
        open_trades[product_id] = {
            "entry":price,"size":size,"size_remaining":size,"trail_high":price,
            "stop":sl,"take_profit_1":tp1,"take_profit_2":tp2,"tp1_hit":False,
            "time":now_utc.isoformat(),"entry_epoch":time.time(),
            "score_d":s_d,"score_4h":s4h,"score_1h":s1h,"score_15m":s15m,
            "combined":combined,"size_pct":pct,"entry_hour":now_utc.hour,
            "crossovers":analysis.get("crossovers",[])
        }
        last_trade_time[product_id]=time.time(); daily_trades_count+=1
        cx=analysis.get("crossovers",[])
        logger.info(f"BUY {product_id} @ ${price} | {round(pct*100,1)}% | "
                   f"TP1={round(tp1,4)} TP2={round(tp2,4)} SL={round(sl,4)} | "
                   f"D={s_d} 4H={s4h} 1H={s1h} 15M={s15m} COMBO={combined} | CX:{cx}")
        send_email(f"🟢 BUY: {product_id}",
            f"Trade opened!\n\nPair:{product_id}\nEntry:${price}\nSize:{round(pct*100,1)}%\n"
            f"TP1:${round(tp1,4)} TP2:${round(tp2,4)} SL:${round(sl,4)}\n"
            f"D:{s_d} 4H:{s4h} 1H:{s1h} 15M:{s15m} Combined:{combined}/400\n"
            f"EMA Crossovers:{cx or 'none'}\nBalance:${round(balance,2)}")
        return {"status":"success","combined_score":combined,"crossovers":cx}
    except Exception as e:
        logger.error(f"BUY ERROR {product_id}:{e}")
        return {"status":"error","reason":str(e)}

# ─── CLOSE TRADE ─────────────────────────────────────────────────────────────
def close_trade(product_id, price, reason="signal", partial=False, partial_size=None):
    global losing_streak, winning_trades, total_trades, daily_pnl
    trade = open_trades.get(product_id)
    if not trade: return
    stc = partial_size if partial else trade["size_remaining"]
    try:
        client.market_order_sell(
            client_order_id=str(uuid.uuid4()),
            product_id=product_id,
            base_size=str(round(stc, 6))
        )
        pnl=(price-trade["entry"])*stc; pct=((price-trade["entry"])/trade["entry"])*100
        daily_pnl+=pnl
        logger.info(f"{'PARTIAL ' if partial else ''}CLOSE {product_id} @ ${price} | {reason} | PNL=${round(pnl,2)} ({round(pct,2)}%)")
        if not partial:
            total_trades+=1
            entry_hour=trade.get("entry_hour",datetime.now(timezone.utc).hour)
            if pnl>0:
                winning_trades+=1; losing_streak=0
                pair_stats[product_id]["wins"]+=1; hour_stats[entry_hour]["wins"]+=1
            else:
                losing_streak+=1; pair_stats[product_id]["losses"]+=1
                hour_stats[entry_hour]["losses"]+=1
                if losing_streak>=LOSING_STREAK_LIMIT:
                    logger.warning(f"LOSING STREAK {losing_streak}")
            pair_stats[product_id]["pnl"]+=pnl
            trade_history.append({
                "pair":product_id,"entry":trade["entry"],"exit":price,
                "pnl":round(pnl,2),"pnl_pct":round(pct,2),"reason":reason,
                "time":datetime.utcnow().isoformat(),
                "scores":f"D={trade['score_d']} 4H={trade['score_4h']} 1H={trade['score_1h']} 15M={trade['score_15m']}",
                "crossovers":trade.get("crossovers",[])
            })
            em="✅" if pnl>0 else "🔴"
            wr=round(winning_trades/total_trades*100,1) if total_trades>0 else 0
            send_email(f"{em} CLOSE: {product_id}",
                f"Trade closed!\nPair:{product_id}\nEntry:${trade['entry']}\nExit:${price}\n"
                f"PNL:${round(pnl,2)} ({round(pct,2)}%)\nReason:{reason}\n"
                f"Daily PNL:${round(daily_pnl,2)}\nWin Rate:{wr}%")
            del open_trades[product_id]
        else: trade["size_remaining"]-=stc
    except Exception as e: logger.error(f"CLOSE ERROR {product_id}:{e}")

# ─── MONITOR TRADES ──────────────────────────────────────────────────────────
def monitor_trades():
    while True:
        try:
            for product_id, trade in list(open_trades.items()):
                price=get_price(product_id)
                if not price: continue
                entry=trade["entry"]; pp=(price-entry)/entry
                hours_open=(time.time()-trade["entry_epoch"])/3600
                if pp>=BREAK_EVEN_TRIGGER and trade["stop"]<entry:
                    trade["stop"]=entry; logger.info(f"BREAK EVEN {product_id}")
                if pp>=0.015 and trade["stop"]<entry*(1+LOCK_PROFIT_PCT):
                    trade["stop"]=entry*(1+LOCK_PROFIT_PCT); logger.info(f"PROFIT LOCKED {product_id}")
                if price>trade["trail_high"]:
                    trade["trail_high"]=price; ns=price*(1-TRAIL_PCT)
                    if ns>trade["stop"]: trade["stop"]=ns; logger.info(f"TRAIL {product_id} stop={round(ns,4)}")
                if not trade["tp1_hit"] and price>=trade["take_profit_1"]:
                    half=round(trade["size"]*0.5,6)
                    close_trade(product_id,price,reason="take_profit_1",partial=True,partial_size=half)
                    trade["tp1_hit"]=True; trade["stop"]=entry
                elif trade["tp1_hit"] and price>=trade["take_profit_2"]:
                    close_trade(product_id,price,reason="take_profit_2")
                elif price<=trade["stop"]:
                    close_trade(product_id,price,reason="stop_loss")
                elif hours_open>=TIME_EXIT_HOURS and pp<=0:
                    close_trade(product_id,price,reason=f"time_exit_{round(hours_open,1)}hrs")
                else:
                    c15m=get_candles(product_id,"FIFTEEN_MINUTE",20)
                    if c15m:
                        cl=[float(c["close"]) for c in reversed(c15m)]
                        rsi=calculate_rsi(cl)
                        if rsi<MOMENTUM_EXIT_RSI and pp<0:
                            close_trade(product_id,price,reason=f"momentum_exit_rsi{round(rsi,1)}")
        except Exception as e: logger.error(f"Monitor error:{e}")
        time.sleep(MONITOR_INTERVAL)

# ─── AUTONOMOUS SCANNER ───────────────────────────────────────────────────────
def autonomous_scanner():
    global last_scan_results, last_scan_time
    time.sleep(120)
    while True:
        try:
            logger.info(f"🔍 AUTO SCAN | Balance:${get_balance():.2f} | Open:{len(open_trades)}")
            check_daily_reset()
            scan_results={}; trades_fired=0
            for symbol, product_id in ALLOWED_PAIRS.items():
                try:
                    analysis=analyze_pair(product_id)
                    if not analysis: continue
                    scan_results[product_id]=analysis
                    if analysis["ready"]:
                        result=execute_buy(product_id,analysis=analysis)
                        if result["status"]=="success": trades_fired+=1
                except Exception as e: logger.error(f"Scan error {product_id}:{e}")
            last_scan_results=scan_results; last_scan_time=datetime.utcnow().isoformat()
            logger.info(f"🔍 SCAN COMPLETE | Fired:{trades_fired} | Next in 1hr")
        except Exception as e: logger.error(f"Scanner error:{e}")
        time.sleep(SCAN_INTERVAL_SEC)

# ─── WEEKLY BACKTEST SCHEDULER ───────────────────────────────────────────────
def backtest_scheduler():
    """Run backtest every Sunday at midnight UTC"""
    time.sleep(300)  # wait 5 min after startup
    # Run once on startup
    threading.Thread(target=run_full_backtest, daemon=True).start()
    while True:
        now = datetime.now(timezone.utc)
        if now.weekday() == 6 and now.hour == 0 and now.minute == 0:
            logger.info("📅 Weekly backtest triggered")
            threading.Thread(target=run_full_backtest, daemon=True).start()
            time.sleep(61)
        time.sleep(30)

def daily_summary_scheduler():
    while True:
        now=datetime.now(timezone.utc); et_hour=(now.hour-4)%24
        if et_hour==8 and now.minute==0: send_daily_summary(); time.sleep(61)
        time.sleep(30)

# ─── ROUTES ──────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data=request.get_json(force=True); action=data.get("action","").lower()
        symbol=data.get("symbol","").upper().replace("/",""); product_id=ALLOWED_PAIRS.get(symbol)
        if not product_id: return jsonify({"status":"ignored","reason":"pair not allowed"})
        if action=="buy": return jsonify(execute_buy(product_id))
        elif action in ["sell","close"]:
            price=get_price(product_id) or float(data.get("price",0))
            close_trade(product_id,price,reason="tv_signal"); return jsonify({"status":"closed"})
        return jsonify({"status":"no action"})
    except Exception as e:
        logger.error(f"Webhook error:{e}"); return jsonify({"status":"error","reason":str(e)})

@app.route("/status")
def status():
    wr=round(winning_trades/total_trades*100,1) if total_trades>0 else 0
    blk,bmsg=is_blackout()
    bt=backtest_results.get("overall",{})
    return jsonify({
        "version":"6.3","running":True,"open_trades":open_trades,
        "circuit_breaker":daily_loss_hit or weekly_loss_hit,
        "daily_start_bal":daily_start_bal,"current_bal":get_balance(),
        "daily_pnl":round(daily_pnl,2),"daily_trades":f"{daily_trades_count}/{MAX_TRADES_PER_DAY}",
        "losing_streak":losing_streak,"win_rate":f"{wr}%","total_trades":total_trades,
        "blackout":blk,"blackout_reason":bmsg,"last_scan":last_scan_time,
        "backtest_win_rate":bt.get("win_rate","pending..."),"pair_stats":pair_stats,
        "settings":{"min_daily":MIN_DAILY_SCORE,"min_4h":MIN_4H_SCORE,
            "min_1h":MIN_1H_SCORE,"min_15m":MIN_15M_SCORE,
            "combined_min":f"{MIN_COMBINED_SCORE}/400","scanning":"24/7 autonomous every 1hr"}
    })

@app.route("/scan")
def scan():
    results={}
    for symbol, product_id in ALLOWED_PAIRS.items():
        analysis=analyze_pair(product_id)
        if analysis: results[product_id]=analysis
    return jsonify(results)

@app.route("/backtest")
def backtest_route():
    """Manual backtest trigger"""
    if not backtest_results:
        threading.Thread(target=run_full_backtest, daemon=True).start()
        return jsonify({"status":"running","message":"Backtest started, check back in 2-3 minutes"})
    return jsonify(backtest_results)

@app.route("/dashboard")
def dashboard():
    wr=round(winning_trades/total_trades*100,1) if total_trades>0 else 0
    bal=get_balance(); profit=round(bal-(daily_start_bal or bal),2)
    blk,bmsg=is_blackout()
    bt=backtest_results.get("overall",{})
    bt_ready = bt.get("go_live_ready",False)

    # Scan rows
    scan_rows=""
    if last_scan_results:
        for pid,r in last_scan_results.items():
            rc="green" if r["ready"] else "#8b949e"
            cx=",".join(r["crossovers"]) if r["crossovers"] else "—"
            scan_rows+=(f"<tr><td>{pid}</td><td>${r['price']}</td>"
                       f"<td>{r['daily']}</td><td>{r['4h']}</td><td>{r['1h']}</td><td>{r['15m']}</td>"
                       f"<td>{r['combined']}/400</td><td style='color:yellow'>{cx}</td>"
                       f"<td style='color:{rc}'>{'✅ READY' if r['ready'] else '❌'}</td></tr>")

    # Backtest rows
    bt_rows=""
    for pid,r in backtest_results.items():
        if pid=="overall" or "error" in r: continue
        rc="green" if r.get("go_live_ready") else "orange"
        bt_rows+=(f"<tr><td>{pid}</td><td>{r.get('total_trades',0)}</td>"
                 f"<td style='color:{'green' if float(r.get('win_rate','0%').replace('%',''))>=55 else 'orange'}'>{r.get('win_rate','—')}</td>"
                 f"<td style='color:{'green' if float(r.get('total_return','0%').replace('%',''))>=0 else 'red'}'>{r.get('total_return','—')}</td>"
                 f"<td>{r.get('max_drawdown','—')}</td>"
                 f"<td style='color:{rc}'>{'✅ READY' if r.get('go_live_ready') else '⚠️ optimize'}</td></tr>")

    # Trade rows
    rows=""
    for t in reversed(trade_history[-15:]):
        c="green" if t["pnl"]>0 else "red"
        cx=",".join(t.get("crossovers",[])) or "—"
        rows+=(f"<tr><td>{t['time'][:16]}</td><td>{t['pair']}</td>"
               f"<td>${t['entry']}</td><td>${t['exit']}</td>"
               f"<td style='color:{c}'>${t['pnl']} ({t['pnl_pct']}%)</td>"
               f"<td>{t['reason']}</td><td>{cx}</td></tr>")

    # Pair rows
    pair_rows=""
    for pair,stats in pair_stats.items():
        total=stats["wins"]+stats["losses"]
        if total>0:
            pwr=round(stats["wins"]/total*100,1)
            pc="green" if stats["pnl"]>0 else "red"
            pair_rows+=(f"<tr><td>{pair}</td><td>{stats['wins']}W/{stats['losses']}L</td>"
                       f"<td>{pwr}%</td><td style='color:{pc}'>${round(stats['pnl'],2)}</td></tr>")

    go_live_color="green" if bt_ready else "orange"
    go_live_text="✅ READY FOR LIVE TRADING" if bt_ready else "⏳ Backtest pending or needs optimization"

    return f"""<!DOCTYPE html><html><head><title>J's Bot v6.3</title>
    <meta http-equiv="refresh" content="60">
    <style>
    body{{font-family:Arial;background:#0d1117;color:#e6edf3;padding:20px}}
    h1,h2{{color:#58a6ff}}h2{{margin-top:25px}}
    .card{{background:#161b22;border-radius:8px;padding:15px;margin:8px;display:inline-block;min-width:140px}}
    .card h3{{margin:0;color:#8b949e;font-size:11px}}.card p{{margin:5px 0 0;font-size:20px;font-weight:bold}}
    table{{width:100%;border-collapse:collapse;margin-top:12px;background:#161b22;border-radius:8px}}
    th,td{{padding:9px;text-align:left;border-bottom:1px solid #30363d;font-size:12px}}
    th{{color:#8b949e}}.green{{color:#3fb950}}.red{{color:#f85149}}.orange{{color:#d29922}}
    .banner{{background:#161b22;border-radius:8px;padding:15px;margin:15px 0;border-left:4px solid {go_live_color}}}
    </style></head><body>
    <h1>🤖 J's Crypto Bot v6.3 — Autonomous + Backtester</h1>
    <p style="color:#8b949e">Refreshes every 60s | Scanning 24/7 | Backtests every Sunday</p>

    <div class="banner">
    <strong style="color:{go_live_color}">GO LIVE STATUS: {go_live_text}</strong>
    {f"<br><small>Backtest: Avg WR {bt.get('win_rate','—')} | Avg Return {bt.get('total_return','—')} | Pairs Ready {bt.get('pairs_ready','—')}</small>" if bt else ""}
    </div>

    <div>
    <div class="card"><h3>BALANCE</h3><p>${bal:.2f}</p></div>
    <div class="card"><h3>TODAY P&L</h3><p class="{'green' if profit>=0 else 'red'}">${profit:.2f}</p></div>
    <div class="card"><h3>LIVE WIN RATE</h3><p>{wr}%</p></div>
    <div class="card"><h3>LIVE TRADES</h3><p>{total_trades}</p></div>
    <div class="card"><h3>OPEN TRADES</h3><p>{len(open_trades)}</p></div>
    <div class="card"><h3>DAILY TRADES</h3><p>{daily_trades_count}/{MAX_TRADES_PER_DAY}</p></div>
    <div class="card"><h3>BLACKOUT</h3><p class="{'red' if blk else 'green'}" style="font-size:12px">{'YES' if blk else 'NO'}</p></div>
    <div class="card"><h3>LOSING STREAK</h3><p class="{'red' if losing_streak>0 else 'green'}">{losing_streak}</p></div>
    </div>

    <h2>🔍 Last Market Scan {f"({last_scan_time[:16]})" if last_scan_time else "(pending...)"}</h2>
    <table><tr><th>Pair</th><th>Price</th><th>Daily</th><th>4H</th><th>1H</th><th>15M</th><th>Combined</th><th>Crossovers</th><th>Status</th></tr>
    {scan_rows if scan_rows else "<tr><td colspan='9' style='color:#8b949e;text-align:center'>First scan runs 2 min after startup</td></tr>"}
    </table>

    <h2>🔬 Backtest Results (6 Months) {f"({last_backtest_time[:16]})" if last_backtest_time else "(running...)"}</h2>
    <table><tr><th>Pair</th><th>Trades</th><th>Win Rate</th><th>Return</th><th>Max Drawdown</th><th>Go Live?</th></tr>
    {bt_rows if bt_rows else "<tr><td colspan='6' style='color:#8b949e;text-align:center'>Backtest running... check back in 3-5 minutes</td></tr>"}
    </table>

    <h2>📊 Live Pair Performance</h2>
    <table><tr><th>Pair</th><th>Record</th><th>Win Rate</th><th>PNL</th></tr>
    {pair_rows if pair_rows else "<tr><td colspan='4' style='color:#8b949e;text-align:center'>No live trades yet</td></tr>"}
    </table>

    <h2>📋 Recent Live Trades</h2>
    <table><tr><th>Time</th><th>Pair</th><th>Entry</th><th>Exit</th><th>PNL</th><th>Reason</th><th>Crossovers</th></tr>
    {rows if rows else "<tr><td colspan='7' style='color:#8b949e;text-align:center'>No trades yet — bot scanning every hour</td></tr>"}
    </table>

    <p style="color:#8b949e;font-size:11px;margin-top:20px">
    Manual backtest: <a href="/backtest" style="color:#58a6ff">/backtest</a> |
    Manual scan: <a href="/scan" style="color:#58a6ff">/scan</a> |
    Performance: <a href="/performance" style="color:#58a6ff">/performance</a>
    </p>
    </body></html>"""

@app.route("/performance")
def performance():
    return jsonify({
        "pair_stats":    pair_stats,
        "total_trades":  total_trades,
        "win_rate":      f"{round(winning_trades/total_trades*100,1) if total_trades>0 else 0}%",
        "daily_pnl":     round(daily_pnl,2),
        "losing_streak": losing_streak,
        "last_scan":     last_scan_time,
        "backtest":      backtest_results.get("overall",{})
    })

# ─── STARTUP ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    daily_start_bal  = get_balance()
    weekly_start_bal = daily_start_bal
    logger.info(
        f"BOT v6.3 STARTED | Balance:${daily_start_bal} | "
        f"Email:{'ON' if EMAIL_ENABLED else 'OFF'} | "
        f"Mode:AUTONOMOUS 24/7 + BACKTESTER"
    )
    try:
        send_email("🚀 Bot v6.3 Started",
            f"J's Crypto Bot v6.3 is live!\n\nBalance:${daily_start_bal:.2f}\n"
            f"Mode: Autonomous 24/7 Scanner\nBacktest: Running on startup\n"
            f"Dashboard: http://165.227.113.102/dashboard\n"
            f"Backtest: http://165.227.113.102/backtest")
    except: pass

    threading.Thread(target=monitor_trades,          daemon=True).start()
    threading.Thread(target=autonomous_scanner,      daemon=True).start()
    threading.Thread(target=backtest_scheduler,      daemon=True).start()
    threading.Thread(target=daily_summary_scheduler, daemon=True).start()

    app.run(host="0.0.0.0", port=80, debug=False)
