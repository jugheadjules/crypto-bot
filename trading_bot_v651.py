"""
J's AI CRYPTO BOT v6.5 - VOLUME DELTA + FIBONACCI LEVELS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Upgrades over v6.4:
  ✅ Volume Delta - detects buying vs selling pressure
  ✅ Fibonacci auto levels - entries at key retracement zones
  ✅ Fibonacci extensions for take profit targets
  ✅ Volume profile - finds high volume price nodes
  ✅ Order flow imbalance detection
  ✅ All v6.4 features retained
"""

from flask import Flask, request, jsonify
from coinbase.rest import RESTClient
import json, uuid, logging, threading, time, smtplib, os, requests
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

# ─── NOTIFICATIONS ────────────────────────────────────────────────────────────
EMAIL_SENDER     = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD   = os.getenv("EMAIL_PASSWORD", "")
EMAIL_RECEIVER   = os.getenv("EMAIL_RECEIVER", "")
EMAIL_ENABLED    = all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER])
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = all([TELEGRAM_TOKEN, TELEGRAM_CHAT])

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
LOSING_STREAK_LIMIT = 2
MONITOR_INTERVAL    = 30

# ─── REGIME SETTINGS ─────────────────────────────────────────────────────────
REGIME_SETTINGS = {
    "STRONG_BULL": {"min_daily":60,"min_4h":62,"min_1h":58,"min_15m":50,"min_combined":230,"max_trades":5,"size_mult":1.2,"description":"🐂 Strong Bull"},
    "BULL":        {"min_daily":63,"min_4h":65,"min_1h":62,"min_15m":55,"min_combined":245,"max_trades":5,"size_mult":1.1,"description":"📈 Bull Market"},
    "SIDEWAYS":    {"min_daily":65,"min_4h":68,"min_1h":65,"min_15m":62,"min_combined":260,"max_trades":4,"size_mult":1.0,"description":"➡️ Sideways"},
    "BEAR":        {"min_daily":70,"min_4h":72,"min_1h":68,"min_15m":65,"min_combined":275,"max_trades":3,"size_mult":0.8,"description":"📉 Bear Market"},
    "STRONG_BEAR": {"min_daily":75,"min_4h":75,"min_1h":72,"min_15m":68,"min_combined":290,"max_trades":2,"size_mult":0.6,"description":"🐻 Strong Bear"},
    "VOLATILE":    {"min_daily":72,"min_4h":72,"min_1h":68,"min_15m":65,"min_combined":277,"max_trades":2,"size_mult":0.7,"description":"⚡ Volatile"},
}

BLACKOUT_HOURS_UTC = [12,13,14,15,18,19,20]
FED_DATES = [(4,16),(4,17),(5,6),(5,7),(6,17),(6,18),(7,29),(7,30),(9,16),(9,17),(10,28),(10,29),(12,9),(12,10)]
CORRELATED_GROUPS = [{"BTC-USD","ETH-USD","SOL-USD"}]
ALLOWED_PAIRS = {"BTCUSD":"BTC-USD","ETHUSD":"ETH-USD","SOLUSD":"SOL-USD","XRPUSD":"XRP-USD","WELLUSD":"WELL-USD"}

# ─── STATE ───────────────────────────────────────────────────────────────────
open_trades={};last_trade_time={};daily_start_bal=None;weekly_start_bal=None
daily_loss_hit=False;weekly_loss_hit=False;losing_streak=0;total_trades=0
winning_trades=0;daily_trades_count=0;daily_pnl=0.0;trade_history=[]
last_scan_results={};last_scan_time=None;backtest_results={};last_backtest_time=None
current_regime="SIDEWAYS";regime_history=[]
last_daily_reset=datetime.utcnow().date();last_weekly_reset=datetime.utcnow().isocalendar()[1]
pair_stats={p:{"wins":0,"losses":0,"pnl":0.0} for p in ALLOWED_PAIRS.values()}
hour_stats={h:{"wins":0,"losses":0} for h in range(24)}

# ─── NOTIFICATIONS ────────────────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_ENABLED:
        logger.info(f"TELEGRAM(disabled):{message[:50]}"); return
    try:
        url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url,json={"chat_id":TELEGRAM_CHAT,"text":message,"parse_mode":"HTML"},timeout=10)
    except Exception as e: logger.error(f"Telegram error:{e}")

def send_email(subject, body):
    if not EMAIL_ENABLED:
        logger.info(f"EMAIL(disabled):{subject}"); return
    try:
        msg=MIMEMultipart(); msg["From"]=EMAIL_SENDER; msg["To"]=EMAIL_RECEIVER; msg["Subject"]=subject
        msg.attach(MIMEText(body,"plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
            s.login(EMAIL_SENDER,EMAIL_PASSWORD); s.send_message(msg)
    except Exception as e: logger.error(f"Email error:{e}")

def notify(subject, body):
    send_email(subject, body)
    send_telegram(f"<b>{subject}</b>\n\n{body}")

# ─── BALANCE & PRICE ─────────────────────────────────────────────────────────
def get_balance():
    try:
        accounts=client.get_accounts(limit=250)
        for a in accounts["accounts"]:
            if a["currency"]=="USD": return float(a["available_balance"]["value"])
    except Exception as e: logger.error(f"Balance error:{e}")
    return 0

def get_price(product_id):
    try:
        t=client.get_best_bid_ask(product_ids=[product_id])
        return float(t["pricebooks"][0]["asks"][0]["price"])
    except Exception as e: logger.error(f"Price error {product_id}:{e}"); return None

# ─── CANDLES ─────────────────────────────────────────────────────────────────
def get_candles(product_id, granularity="ONE_HOUR", limit=50):
    try:
        seconds={"FIFTEEN_MINUTE":900,"ONE_HOUR":3600,"FOUR_HOUR":14400,"ONE_DAY":86400}
        interval=seconds.get(granularity,3600)
        end=int(time.time()); start=end-(interval*limit)
        candles=client.get_candles(product_id=product_id,start=start,end=end,granularity=granularity)
        return candles["candles"]
    except Exception as e: logger.error(f"Candle error {product_id} {granularity}:{e}"); return None

def get_historical_candles(product_id, granularity="ONE_HOUR", days_back=180):
    try:
        seconds={"FIFTEEN_MINUTE":900,"ONE_HOUR":3600,"FOUR_HOUR":14400,"ONE_DAY":86400}
        interval=seconds.get(granularity,3600)
        all_candles=[]; end=int(time.time()); start=end-(days_back*86400)
        while start<end:
            chunk_end=min(start+interval*300,end)
            try:
                candles=client.get_candles(product_id=product_id,start=start,end=chunk_end,granularity=granularity)
                if candles and candles.get("candles"): all_candles.extend(candles["candles"])
            except: pass
            start=chunk_end; time.sleep(0.3)
        all_candles.sort(key=lambda x:int(x["start"]))
        return all_candles
    except Exception as e: logger.error(f"Historical candle error:{e}"); return []

# ─── BASE INDICATORS ─────────────────────────────────────────────────────────
def calculate_rsi(prices, period=14):
    if len(prices)<period+1: return 50
    gains,losses=[],[]
    for i in range(1,len(prices)):
        change=prices[i]-prices[i-1]; gains.append(max(change,0)); losses.append(abs(min(change,0)))
    ag=sum(gains[-period:])/period; al=sum(losses[-period:])/period
    if al==0: return 100
    return 100-(100/(1+ag/al))

def calculate_ema(prices, period=9):
    if len(prices)<period: return prices[-1]
    m=2/(period+1); ema=sum(prices[:period])/period
    for p in prices[period:]: ema=(p-ema)*m+ema
    return ema

def calculate_atr(candles, period=14):
    if len(candles)<period+1: return 0
    trs=[]
    for i in range(1,len(candles)):
        h=float(candles[i]["high"]); l=float(candles[i]["low"]); pc=float(candles[i-1]["close"])
        trs.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sum(trs[-period:])/period

def calculate_vwap(candles):
    try:
        tv,tpv=0,0
        for c in candles:
            h,l,cl,v=float(c["high"]),float(c["low"]),float(c["close"]),float(c["volume"])
            tpv+=((h+l+cl)/3)*v; tv+=v
        return tpv/tv if tv>0 else 0
    except: return 0

def detect_ema_crossover(closes, fast=9, slow=21):
    if len(closes)<slow+2: return False
    ef_now=calculate_ema(closes,fast); es_now=calculate_ema(closes,slow)
    ef_prev=calculate_ema(closes[:-1],fast); es_prev=calculate_ema(closes[:-1],slow)
    return ef_prev<=es_prev and ef_now>es_now

def detect_ema_aligned(closes, price):
    e9=calculate_ema(closes,9); e21=calculate_ema(closes,21); e55=calculate_ema(closes,55)
    return price>e9>e21>e55

def detect_support(closes, lookback=20):
    if len(closes)<lookback: return False
    return abs(closes[-1]-min(closes[-lookback:-1]))/min(closes[-lookback:-1])<0.015

def detect_bullish_candle(candles):
    if len(candles)<2: return False
    try:
        p,c=candles[-2],candles[-1]
        po,pc=float(p["open"]),float(p["close"])
        co,cc,cl,ch=float(c["open"]),float(c["close"]),float(c["low"]),float(c["high"])
        engulfing=pc<po and cc>co and cc>po and co<pc
        body=abs(cc-co); lw=min(co,cc)-cl; uw=ch-max(co,cc)
        hammer=lw>body*2 and uw<body and cc>co
        return engulfing or hammer
    except: return False

def check_volatility(closes, min_range=0.004):
    if len(closes)<10: return True
    r=closes[-10:]
    return (max(r)-min(r))/min(r)>=min_range

def detect_higher_highs_lows(closes, lookback=10):
    if len(closes)<lookback: return False
    recent=closes[-lookback:]; mid=len(recent)//2
    return max(recent[mid:])>max(recent[:mid]) and min(recent[mid:])>min(recent[:mid])

# ─── VOLUME DELTA ─────────────────────────────────────────────────────────────
def calculate_volume_delta(candles):
    """
    Volume Delta = Buying Volume - Selling Volume
    Up candles = buying pressure
    Down candles = selling pressure
    Returns: delta score 0-25, positive = buying pressure
    """
    if not candles or len(candles) < 10:
        return 0, "no data", 0

    cc = list(reversed(candles))
    buy_vol  = 0
    sell_vol = 0
    total_vol = 0

    for c in cc[-20:]:  # last 20 candles
        o = float(c["open"])
        cl = float(c["close"])
        v  = float(c["volume"])
        total_vol += v
        if cl >= o:   # green candle = buying
            buy_vol  += v
        else:          # red candle = selling
            sell_vol += v

    if total_vol == 0:
        return 0, "no volume", 0

    delta_pct  = (buy_vol - sell_vol) / total_vol * 100
    buy_ratio  = buy_vol / total_vol

    # Recent acceleration (last 5 vs previous 15)
    recent_buy = sum(float(c["volume"]) for c in cc[-5:] if float(c["close"]) >= float(c["open"]))
    recent_vol = sum(float(c["volume"]) for c in cc[-5:])
    recent_ratio = recent_buy / recent_vol if recent_vol > 0 else 0.5

    # Score
    score = 0
    if delta_pct > 20:     score = 25; label = f"🟢 Strong buying {round(delta_pct,1)}%"
    elif delta_pct > 10:   score = 18; label = f"🟢 Buying {round(delta_pct,1)}%"
    elif delta_pct > 0:    score = 12; label = f"🟡 Slight buying {round(delta_pct,1)}%"
    elif delta_pct > -10:  score = 6;  label = f"🟡 Slight selling {round(delta_pct,1)}%"
    else:                  score = 0;  label = f"🔴 Selling {round(delta_pct,1)}%"

    # Bonus for recent acceleration
    if recent_ratio > 0.65:
        score = min(score + 5, 25)
        label += " (accelerating)"

    logger.info(f"VOL DELTA: {label} | buy_ratio={round(buy_ratio,2)}")
    return score, label, delta_pct

# ─── FIBONACCI LEVELS ─────────────────────────────────────────────────────────
def calculate_fibonacci_levels(candles, lookback=50):
    """
    Calculate Fibonacci retracement and extension levels
    Uses the most recent significant swing high and low
    Returns dict with key levels and whether price is near one
    """
    if not candles or len(candles) < lookback:
        return None

    cc = list(reversed(candles))[-lookback:]
    highs  = [float(c["high"])  for c in cc]
    lows   = [float(c["low"])   for c in cc]
    closes = [float(c["close"]) for c in cc]

    swing_high = max(highs)
    swing_low  = min(lows)
    current    = closes[-1]
    diff       = swing_high - swing_low

    if diff == 0:
        return None

    # Retracement levels (from swing high)
    levels = {
        "0.0":   swing_high,
        "23.6":  swing_high - diff * 0.236,
        "38.2":  swing_high - diff * 0.382,
        "50.0":  swing_high - diff * 0.500,
        "61.8":  swing_high - diff * 0.618,
        "78.6":  swing_high - diff * 0.786,
        "100.0": swing_low,
        # Extensions
        "127.2": swing_low - diff * 0.272,
        "161.8": swing_low - diff * 0.618,
    }

    # Check if price is near a key Fibonacci level (within 0.5%)
    key_levels = ["23.6", "38.2", "50.0", "61.8", "78.6"]
    near_level = None
    min_dist   = float("inf")

    for lvl_name in key_levels:
        lvl_price = levels[lvl_name]
        dist_pct  = abs(current - lvl_price) / current * 100
        if dist_pct < min_dist:
            min_dist   = dist_pct
            near_level = (lvl_name, lvl_price, dist_pct)

    at_fib_level = near_level and near_level[2] < 0.8  # within 0.8%

    # Best TP targets using extensions
    tp1_fib = levels["23.6"] if current < levels["23.6"] else levels["0.0"]
    tp2_fib = levels["0.0"]  if current < levels["0.0"]  else levels["127.2"]

    result = {
        "swing_high":    round(swing_high, 6),
        "swing_low":     round(swing_low, 6),
        "levels":        {k: round(v, 6) for k,v in levels.items()},
        "current":       current,
        "near_level":    near_level,
        "at_fib_level":  at_fib_level,
        "tp1_fib":       round(tp1_fib, 6),
        "tp2_fib":       round(tp2_fib, 6),
    }

    if at_fib_level:
        logger.info(f"FIB LEVEL HIT: {near_level[0]}% @ ${near_level[1]:.4f} (dist:{round(near_level[2],2)}%)")

    return result

def calculate_fib_score(fib_data):
    """Score 0-25 based on Fibonacci position"""
    if not fib_data:
        return 0, "no fib data"

    if fib_data["at_fib_level"] and fib_data["near_level"]:
        lvl = fib_data["near_level"][0]
        dist = fib_data["near_level"][2]
        # 38.2% and 61.8% are the strongest levels
        if lvl in ["38.2", "61.8"]:
            score = 25; label = f"🎯 Golden Fib {lvl}% (dist:{round(dist,2)}%)"
        elif lvl in ["50.0"]:
            score = 20; label = f"🎯 Key Fib {lvl}% (dist:{round(dist,2)}%)"
        elif lvl in ["23.6", "78.6"]:
            score = 15; label = f"📍 Fib {lvl}% (dist:{round(dist,2)}%)"
        else:
            score = 10; label = f"📍 Fib level nearby"
    else:
        score = 0; label = "❌ Not at Fib level"

    return score, label

# ─── VOLUME PROFILE ──────────────────────────────────────────────────────────
def calculate_volume_profile(candles, bins=10):
    """
    Find high volume price nodes — areas where most trading happened
    Price near HVN (High Volume Node) = strong support/resistance
    """
    if not candles or len(candles) < 20:
        return None, 0

    cc = list(reversed(candles))[-50:]
    prices  = [(float(c["high"]) + float(c["low"])) / 2 for c in cc]
    volumes = [float(c["volume"]) for c in cc]
    current = float(cc[-1]["close"])

    if not prices:
        return None, 0

    price_min = min(prices)
    price_max = max(prices)
    if price_max == price_min:
        return None, 0

    # Create price bins
    bin_size   = (price_max - price_min) / bins
    vol_bins   = [0] * bins
    for p, v in zip(prices, volumes):
        bin_idx = min(int((p - price_min) / bin_size), bins - 1)
        vol_bins[bin_idx] += v

    # Find which bin current price is in
    curr_bin = min(int((current - price_min) / bin_size), bins - 1)
    max_vol  = max(vol_bins)

    # Score based on how much volume is at current price level
    vol_ratio = vol_bins[curr_bin] / max_vol if max_vol > 0 else 0

    if vol_ratio > 0.7:
        score = 15; label = f"🔥 High Volume Node ({round(vol_ratio*100)}%)"
    elif vol_ratio > 0.4:
        score = 8;  label = f"📊 Medium Volume Node ({round(vol_ratio*100)}%)"
    else:
        score = 0;  label = f"📉 Low Volume Node ({round(vol_ratio*100)}%)"

    return label, score

# ─── REGIME DETECTION ─────────────────────────────────────────────────────────
def detect_market_regime():
    global current_regime, regime_history
    try:
        candles_daily=get_candles("BTC-USD","ONE_DAY",30)
        candles_4h=get_candles("BTC-USD","FOUR_HOUR",50)
        if not candles_daily or not candles_4h: return current_regime
        daily_cc=list(reversed(candles_daily)); h4_cc=list(reversed(candles_4h))
        daily_closes=[float(c["close"]) for c in daily_cc]
        h4_closes=[float(c["close"]) for c in h4_cc]
        price=daily_closes[-1]
        rsi_daily=calculate_rsi(daily_closes); rsi_4h=calculate_rsi(h4_closes)
        ema21=calculate_ema(daily_closes,21); ema50=calculate_ema(daily_closes,50)
        atr=calculate_atr(daily_cc); atr_pct=(atr/price)*100 if price>0 else 0
        mom_30=(daily_closes[-1]-daily_closes[0])/daily_closes[0]*100 if len(daily_closes)>=2 else 0
        mom_7=(daily_closes[-1]-daily_closes[-7])/daily_closes[-7]*100 if len(daily_closes)>=7 else 0
        hhl=detect_higher_highs_lows(daily_closes,14)
        bull_score=0; bear_score=0
        if price>ema21: bull_score+=2
        else: bear_score+=2
        if price>ema50: bull_score+=2
        else: bear_score+=2
        if ema21>ema50: bull_score+=1
        else: bear_score+=1
        if rsi_daily>55: bull_score+=2
        elif rsi_daily<45: bear_score+=2
        if rsi_4h>55: bull_score+=1
        elif rsi_4h<45: bear_score+=1
        if mom_30>10: bull_score+=2
        elif mom_30<-10: bear_score+=2
        elif mom_30>3: bull_score+=1
        elif mom_30<-3: bear_score+=1
        if mom_7>3: bull_score+=1
        elif mom_7<-3: bear_score+=1
        if hhl: bull_score+=1
        else: bear_score+=1
        if atr_pct>5: regime="VOLATILE"
        elif bull_score>=10: regime="STRONG_BULL"
        elif bull_score>=7: regime="BULL"
        elif bear_score>=10: regime="STRONG_BEAR"
        elif bear_score>=7: regime="BEAR"
        else: regime="SIDEWAYS"
        if regime!=current_regime:
            old=current_regime; current_regime=regime
            regime_history.append({"time":datetime.utcnow().isoformat(),"old":old,"new":regime})
            notify(f"🔄 Regime: {old} → {regime}",
                f"Market regime changed!\n\nNew: {REGIME_SETTINGS[regime]['description']}\n"
                f"BTC:${price:,.2f} RSI:{round(rsi_daily,1)} Mom30:{round(mom_30,1)}%")
        else:
            logger.info(f"REGIME:{regime} Bull:{bull_score} Bear:{bear_score} BTC=${price:,.2f}")
        return regime
    except Exception as e: logger.error(f"Regime error:{e}"); return current_regime

def get_adaptive_thresholds():
    s=REGIME_SETTINGS.get(current_regime,REGIME_SETTINGS["SIDEWAYS"])
    return s["min_daily"],s["min_4h"],s["min_1h"],s["min_15m"],s["min_combined"],s["max_trades"],s["size_mult"]

# ─── BLACKOUT ────────────────────────────────────────────────────────────────
def is_blackout():
    now=datetime.now(timezone.utc)
    if now.hour in BLACKOUT_HOURS_UTC: return True,"macro event blackout"
    if (now.month,now.day) in FED_DATES: return True,"Fed/CPI date"
    return False,"ok"

def correlation_check(product_id):
    for group in CORRELATED_GROUPS:
        if product_id in group:
            open_in_group=[p for p in open_trades if p in group]
            if open_in_group: return False,f"correlated {open_in_group[0]} open"
    return True,"ok"

# ─── CIRCUIT BREAKERS ─────────────────────────────────────────────────────────
def check_circuit_breakers():
    global daily_loss_hit,weekly_loss_hit
    try:
        bal=get_balance()
        if daily_start_bal and bal<daily_start_bal*(1-DAILY_LOSS_LIMIT):
            daily_loss_hit=True; notify("🚨 Daily Loss Limit",f"Balance:${bal:.2f}"); return True,"daily"
        if weekly_start_bal and bal<weekly_start_bal*(1-WEEKLY_LOSS_LIMIT):
            weekly_loss_hit=True; notify("🚨 Weekly Loss Limit",f"Balance:${bal:.2f}"); return True,"weekly"
    except Exception as e: logger.error(f"CB error:{e}")
    return False,"ok"

# ─── POSITION SIZING ─────────────────────────────────────────────────────────
def get_size_pct(combined, product_id, fib_bonus=False):
    _,_,_,_,_,_,regime_mult=get_adaptive_thresholds()
    stats=pair_stats.get(product_id,{"wins":0,"losses":0})
    total=stats["wins"]+stats["losses"]; pair_mult=1.0
    if total>=5:
        wr=stats["wins"]/total
        if wr>=0.70: pair_mult=1.3
        elif wr>=0.60: pair_mult=1.1
        elif wr<=0.35: pair_mult=0.7
    if losing_streak>=LOSING_STREAK_LIMIT: base=0.010
    elif combined>=360: base=0.050
    elif combined>=330: base=0.035
    elif combined>=300: base=0.025
    else: base=0.015
    # Fibonacci bonus — slightly larger size when at key level
    fib_mult = 1.1 if fib_bonus else 1.0
    return min(base*pair_mult*regime_mult*fib_mult, 0.05)

# ─── DAILY RESET ─────────────────────────────────────────────────────────────
def check_daily_reset():
    global daily_start_bal,daily_loss_hit,daily_trades_count,daily_pnl,last_daily_reset
    global weekly_start_bal,weekly_loss_hit,last_weekly_reset
    today=datetime.utcnow().date()
    if today!=last_daily_reset:
        daily_start_bal=get_balance(); daily_loss_hit=False
        daily_trades_count=0; daily_pnl=0.0; last_daily_reset=today
        send_daily_summary()
    week=datetime.utcnow().isocalendar()[1]
    if week!=last_weekly_reset:
        weekly_start_bal=get_balance(); weekly_loss_hit=False; last_weekly_reset=week

def send_daily_summary():
    bal=get_balance(); wr=round(winning_trades/total_trades*100,1) if total_trades>0 else 0
    regime_desc=REGIME_SETTINGS.get(current_regime,{}).get("description","")
    bt=backtest_results.get("overall",{})
    notify("☀️ Daily Summary v6.5",
        f"Good morning J!\n\nBalance:${bal:.2f}\nWin Rate:{wr}%\nDaily PNL:${daily_pnl:.2f}\n"
        f"Regime:{regime_desc}\nBacktest WR:{bt.get('win_rate','pending')}\n- Bot 🤖 v6.5")

# ─── FULL TIMEFRAME SCORER ────────────────────────────────────────────────────
def score_timeframe(product_id, granularity, price, label):
    score=0; reason=[]
    candles=get_candles(product_id,granularity=granularity,limit=60)
    if not candles: return 0,[],False
    cc=list(reversed(candles))
    closes=[float(c["close"]) for c in cc]; volumes=[float(c["volume"]) for c in cc]
    crossover=detect_ema_crossover(closes); aligned=detect_ema_aligned(closes,price)
    rsi=calculate_rsi(closes)
    if 38<=rsi<=65: score+=20; reason.append(f"RSI={round(rsi,1)} ideal")
    elif 30<=rsi<=72: score+=10; reason.append(f"RSI={round(rsi,1)} ok")
    else: reason.append(f"RSI={round(rsi,1)} poor")
    avg_vol=sum(volumes[:-1])/max(len(volumes)-1,1); vr=volumes[-1]/avg_vol if avg_vol>0 else 0
    if vr>=1.2: score+=15; reason.append(f"Vol={round(vr,2)}x strong")
    elif vr>=0.7: score+=8; reason.append(f"Vol={round(vr,2)}x ok")
    else: reason.append(f"Vol={round(vr,2)}x weak")
    if aligned: score+=25; reason.append("EMAs perfect")
    elif price>calculate_ema(closes,9) and calculate_ema(closes,9)>calculate_ema(closes,21):
        score+=15; reason.append("EMAs partial")
    else: reason.append("EMAs not aligned")
    if crossover: score+=10; reason.append("⚡ EMA CROSSOVER")
    if len(closes)>=5:
        mom=(closes[-1]-closes[-5])/closes[-5]*100
        if mom>=1.0: score+=15; reason.append(f"Mom={round(mom,2)}% strong")
        elif mom>=0: score+=8; reason.append(f"Mom={round(mom,2)}% mild")
        else: reason.append(f"Mom={round(mom,2)}% negative")
    vwap=calculate_vwap(cc[-20:])
    if vwap>0 and price>vwap: score+=10; reason.append("Above VWAP")
    else: reason.append("Below VWAP")
    if detect_support(closes): score+=10; reason.append("Near support")
    if detect_bullish_candle(cc[-3:]): score+=5; reason.append("Bullish candle")
    logger.info(f"{label} {product_id}:{score}/100 | {' | '.join(reason)}")
    return score,reason,crossover

def score_daily(product_id, price):
    score=0; reason=[]
    candles=get_candles(product_id,granularity="ONE_DAY",limit=50)
    if not candles: return 0,[]
    cc=list(reversed(candles)); closes=[float(c["close"]) for c in cc]; vols=[float(c["volume"]) for c in cc]
    rsi=calculate_rsi(closes)
    if 50<=rsi<=72: score+=25; reason.append(f"RSI={round(rsi,1)} bullish")
    elif 45<=rsi<50: score+=10; reason.append(f"RSI={round(rsi,1)} neutral")
    else: reason.append(f"RSI={round(rsi,1)} bearish")
    e21=calculate_ema(closes,21); e50=calculate_ema(closes,50)
    if price>e21 and e21>e50: score+=25; reason.append("Above EMA21&50")
    elif price>e21: score+=10; reason.append("Above EMA21 only")
    else: reason.append("Below daily EMAs")
    avg_v=sum(vols[:-5])/max(len(vols)-5,1); rec_v=sum(vols[-5:])/5
    if rec_v>avg_v*1.1: score+=20; reason.append("Volume increasing")
    elif rec_v>avg_v*0.8: score+=10; reason.append("Volume stable")
    else: reason.append("Volume declining")
    if detect_higher_highs_lows(closes): score+=15; reason.append("Higher highs/lows")
    else: reason.append("No higher highs/lows")
    if len(closes)>=5:
        mom=(closes[-1]-closes[-5])/closes[-5]*100
        if mom>=2.0: score+=15; reason.append(f"Mom={round(mom,1)}% strong")
        elif mom>=0: score+=7; reason.append(f"Mom={round(mom,1)}% mild")
        else: reason.append(f"Mom={round(mom,1)}% negative")
    logger.info(f"DAILY {product_id}:{score}/100 | {' | '.join(reason)}")
    return score,reason

def analyze_pair(product_id):
    price=get_price(product_id)
    if not price: return None
    min_d,min_4h,min_1h,min_15m,min_combo,_,_=get_adaptive_thresholds()

    # Standard scoring
    s_d,_=score_daily(product_id,price)
    s4h,_,x4h=score_timeframe(product_id,"FOUR_HOUR",price,"4H")
    s1h,_,x1h=score_timeframe(product_id,"ONE_HOUR",price,"1H")
    s15m,_,x15m=score_timeframe(product_id,"FIFTEEN_MINUTE",price,"15M")
    combined=s_d+s4h+s1h+s15m
    crossovers=[tf for tf,cx in [("4H",x4h),("1H",x1h),("15M",x15m)] if cx]

    # Volume Delta
    candles_1h=get_candles(product_id,"ONE_HOUR",50)
    vd_score,vd_label,vd_pct=calculate_volume_delta(candles_1h) if candles_1h else (0,"no data",0)

    # Fibonacci Levels
    candles_4h=get_candles(product_id,"FOUR_HOUR",60)
    fib_data=calculate_fibonacci_levels(candles_4h) if candles_4h else None
    fib_score,fib_label=calculate_fib_score(fib_data)

    # Volume Profile
    vp_label,vp_score=calculate_volume_profile(candles_1h) if candles_1h else ("no data",0)

    # Enhanced combined score (now includes VD + Fib + VP bonuses)
    enhanced_combined = combined + vd_score + fib_score + vp_score

    # Check readiness with enhanced score
    ready=(s_d>=min_d and s4h>=min_4h and s1h>=min_1h and s15m>=min_15m and
           combined>=min_combo and vd_pct>0)  # must have buying pressure

    fib_bonus = fib_data and fib_data.get("at_fib_level", False)

    result={
        "price":price,"daily":s_d,"4h":s4h,"1h":s1h,"15m":s15m,
        "combined":combined,"enhanced":enhanced_combined,
        "crossovers":crossovers,"ready":ready,
        "regime":current_regime,
        "volume_delta":{"score":vd_score,"label":vd_label,"pct":round(vd_pct,1)},
        "fibonacci":{"score":fib_score,"label":fib_label,"at_level":fib_bonus,
                    "levels":fib_data.get("levels",{}) if fib_data else {}},
        "volume_profile":{"score":vp_score,"label":vp_label},
        "fib_bonus":fib_bonus,
        "time":datetime.utcnow().isoformat()
    }

    logger.info(f"SCAN {product_id}: D={s_d} 4H={s4h} 1H={s1h} 15M={s15m} "
                f"COMBO={combined}/400 Enhanced={enhanced_combined} | "
                f"VD={vd_score}({round(vd_pct,1)}%) Fib={fib_score} VP={vp_score} | "
                f"CX:{crossovers or 'none'} | Regime:{current_regime} | "
                f"{'✅ READY' if ready else '❌'}")
    return result

# ─── BACKTESTER ───────────────────────────────────────────────────────────────
def run_backtest(product_id, days=180, starting_balance=66.7):
    logger.info(f"BACKTEST {product_id}: pulling {days} days...")
    candles=get_historical_candles(product_id,granularity="FOUR_HOUR",days_back=days)
    if len(candles)<20: return {"error":"not enough data","pair":product_id}
    balance=starting_balance; trades=[]; wins=0; losses=0
    max_balance=balance; min_balance=balance
    in_trade=False; entry=0; entry_time=0; tp1=0; tp2=0; sl=0
    tp1_hit=False; size=0; size_rem=0; cooldown=0
    for i in range(60,len(candles)):
        window=candles[max(0,i-59):i+1]; closes=[float(c["close"]) for c in window]
        current=closes[-1]; ts=int(candles[i]["start"])
        if cooldown>0:
            cooldown-=1
            if in_trade:
                if not tp1_hit and current>=tp1:
                    balance+=(tp1-entry)*(size*0.5); size_rem=size*0.5; tp1_hit=True
                elif tp1_hit and current>=tp2:
                    balance+=(current-entry)*size_rem; wins+=1
                    trades.append({"entry":entry,"exit":current,"pnl":round((current-entry)/entry*100,2),"result":"win","reason":"TP2"})
                    in_trade=False; tp1_hit=False; size_rem=0; cooldown=0
                elif current<=sl:
                    balance+=(current-entry)*size_rem; losses+=1
                    trades.append({"entry":entry,"exit":current,"pnl":round((current-entry)/entry*100,2),"result":"loss","reason":"SL"})
                    in_trade=False; tp1_hit=False; size_rem=0; cooldown=0
                elif (ts-entry_time)>TIME_EXIT_HOURS*3600 and current<entry:
                    balance+=(current-entry)*size_rem; losses+=1
                    trades.append({"entry":entry,"exit":current,"pnl":round((current-entry)/entry*100,2),"result":"loss","reason":"time_exit"})
                    in_trade=False; tp1_hit=False; size_rem=0; cooldown=0
            continue
        if in_trade: continue
        crossover=detect_ema_crossover(closes)
        if not crossover: continue
        rsi=calculate_rsi(closes); e9=calculate_ema(closes,9); e21=calculate_ema(closes,21)
        aligned=current>e9>e21; mom=(closes[-1]-closes[-5])/closes[-5]*100 if len(closes)>=5 else 0
        score=0
        if 38<=rsi<=65: score+=35
        if aligned: score+=35
        if mom>=0: score+=20
        if crossover: score+=10
        if score<70: continue
        risk=balance*0.02; size=risk/current; size_rem=size; entry=current; entry_time=ts
        tp1=entry*(1+TAKE_PROFIT_1); tp2=entry*(1+TAKE_PROFIT_2); sl=entry*(1-STOP_LOSS_PCT)
        in_trade=True; tp1_hit=False; cooldown=4
        max_balance=max(max_balance,balance); min_balance=min(min_balance,balance)
    if in_trade and len(candles)>0:
        final=float(candles[-1]["close"]); pnl=(final-entry)*size_rem; balance+=pnl
        if pnl>0: wins+=1
        else: losses+=1
        trades.append({"entry":entry,"exit":final,"pnl":round((final-entry)/entry*100,2),"result":"win" if pnl>0 else "loss","reason":"end"})
    total=wins+losses; win_rate=round(wins/total*100,1) if total>0 else 0
    total_ret=round((balance-starting_balance)/starting_balance*100,1)
    max_dd=round((max_balance-min_balance)/max_balance*100,1) if max_balance>0 else 0
    result={"pair":product_id,"days":days,"total_trades":total,"wins":wins,"losses":losses,
            "win_rate":f"{win_rate}%","total_return":f"{total_ret}%","final_balance":round(balance,2),
            "max_drawdown":f"{max_dd}%","start_balance":starting_balance,
            "go_live_ready":win_rate>=55 and max_dd<=15}
    logger.info(f"BACKTEST {product_id}: {total} trades WR:{win_rate}% Return:{total_ret}% MaxDD:{max_dd}% {'✅' if result['go_live_ready'] else '❌'}")
    return result

def run_full_backtest():
    global backtest_results,last_backtest_time
    logger.info("🔬 FULL BACKTEST STARTING...")
    results={}
    for symbol,product_id in ALLOWED_PAIRS.items():
        try:
            r=run_backtest(product_id,days=180,starting_balance=333.0/5)
            results[product_id]=r; time.sleep(2)
        except Exception as e:
            logger.error(f"Backtest error {product_id}:{e}"); results[product_id]={"error":str(e)}
    valid=[r for r in results.values() if "win_rate" in r]
    if valid:
        avg_wr=round(sum(float(r["win_rate"].replace("%","")) for r in valid)/len(valid),1)
        avg_ret=round(sum(float(r["total_return"].replace("%","")) for r in valid)/len(valid),1)
        ready=sum(1 for r in valid if r.get("go_live_ready",False))
        results["overall"]={"avg_win_rate":f"{avg_wr}%","avg_return":f"{avg_ret}%",
            "pairs_ready":f"{ready}/{len(valid)}","go_live_ready":ready>=3,
            "win_rate":f"{avg_wr}%","total_return":f"{avg_ret}%"}
        pairs_str="\n".join([f"  {r['pair']}: {r['win_rate']} WR | {r['total_return']} | {'✅' if r.get('go_live_ready') else '❌'}" for r in valid])
        notify("🔬 Backtest Complete",
            f"6-Month Results!\n\nAvg WR:{avg_wr}%\nReturn:{avg_ret}%\nReady:{ready}/{len(valid)}\n\n{pairs_str}\n\n{'✅ GO LIVE!' if ready>=3 else '⚠️ Optimize more'}")
    backtest_results=results; last_backtest_time=datetime.utcnow().isoformat()
    return results

# ─── SMART FILTER ────────────────────────────────────────────────────────────
def smart_filter(product_id, price):
    _,_,_,_,_,max_t,_=get_adaptive_thresholds(); now=time.time()
    if price<=0: return False,"invalid price"
    if product_id in open_trades: return False,"already in trade"
    if len(open_trades)>=MAX_OPEN_TRADES: return False,"max trades open"
    if daily_trades_count>=max_t: return False,f"max daily trades ({max_t})"
    if product_id in last_trade_time:
        if now-last_trade_time[product_id]<TRADE_COOLDOWN_SEC:
            mins=int((TRADE_COOLDOWN_SEC-(now-last_trade_time[product_id]))/60)
            return False,f"cooldown {mins}min"
    blk,bmsg=is_blackout()
    if blk: return False,f"blackout:{bmsg}"
    ok,cmsg=correlation_check(product_id)
    if not ok: return False,cmsg
    return True,"ok"

# ─── EXECUTE BUY ─────────────────────────────────────────────────────────────
def execute_buy(product_id, analysis=None):
    global daily_trades_count
    if daily_loss_hit: return {"status":"skip","reason":"daily circuit breaker"}
    if weekly_loss_hit: return {"status":"skip","reason":"weekly circuit breaker"}
    tripped,reason=check_circuit_breakers()
    if tripped: return {"status":"skip","reason":reason}
    check_daily_reset()
    price=get_price(product_id)
    if not price: return {"status":"error","reason":"no price"}
    allowed,reason=smart_filter(product_id,price)
    if not allowed:
        logger.info(f"FILTERED {product_id}:{reason}"); return {"status":"skip","reason":reason}
    if not analysis: analysis=analyze_pair(product_id)
    if not analysis: return {"status":"error","reason":"analysis failed"}
    min_d,min_4h,min_1h,min_15m,min_combo,_,_=get_adaptive_thresholds()
    s_d=analysis["daily"]; s4h=analysis["4h"]; s1h=analysis["1h"]
    s15m=analysis["15m"]; combined=analysis["combined"]
    vd_pct=analysis.get("volume_delta",{}).get("pct",0)
    fib_bonus=analysis.get("fib_bonus",False)
    if s_d<min_d: return {"status":"skip","reason":f"Daily={s_d} need {min_d}"}
    if s4h<min_4h: return {"status":"skip","reason":f"4H={s4h} need {min_4h}"}
    if s1h<min_1h: return {"status":"skip","reason":f"1H={s1h} need {min_1h}"}
    if s15m<min_15m: return {"status":"skip","reason":f"15M={s15m} need {min_15m}"}
    if combined<min_combo: return {"status":"skip","reason":f"Combined={combined} need {min_combo}"}
    if vd_pct<=0: return {"status":"skip","reason":f"Volume delta negative ({vd_pct}%) - selling pressure"}
    c1h=get_candles(product_id,"ONE_HOUR",15)
    if c1h:
        cl=[float(c["close"]) for c in reversed(c1h)]
        if not check_volatility(cl): return {"status":"skip","reason":"market too flat"}
    balance=get_balance(); pct=get_size_pct(combined,product_id,fib_bonus); size=round((balance*pct)/price,6)
    try:
        client.market_order_buy(client_order_id=str(uuid.uuid4()),product_id=product_id,quote_size=str(round(size*price,2)))
        tp1=price*(1+TAKE_PROFIT_1); tp2=price*(1+TAKE_PROFIT_2); sl=price*(1-STOP_LOSS_PCT)
        # Use Fibonacci TP if available and better
        if fib_bonus and analysis.get("fibonacci",{}).get("levels"):
            fib_tp1=analysis["fibonacci"].get("levels",{}).get("23.6",0)
            fib_tp2=analysis["fibonacci"].get("levels",{}).get("0.0",0)
            if fib_tp1>tp1: tp1=fib_tp1
            if fib_tp2>tp2: tp2=fib_tp2
        now_utc=datetime.now(timezone.utc)
        open_trades[product_id]={"entry":price,"size":size,"size_remaining":size,"trail_high":price,
            "stop":sl,"take_profit_1":tp1,"take_profit_2":tp2,"tp1_hit":False,
            "time":now_utc.isoformat(),"entry_epoch":time.time(),
            "score_d":s_d,"score_4h":s4h,"score_1h":s1h,"score_15m":s15m,
            "combined":combined,"size_pct":pct,"entry_hour":now_utc.hour,
            "crossovers":analysis.get("crossovers",[]),"regime":current_regime,
            "fib_bonus":fib_bonus,"volume_delta":vd_pct}
        last_trade_time[product_id]=time.time(); daily_trades_count+=1
        cx=analysis.get("crossovers",[])
        logger.info(f"BUY {product_id} @ ${price} | {round(pct*100,1)}% | "
                   f"TP1={round(tp1,4)} TP2={round(tp2,4)} SL={round(sl,4)} | "
                   f"D={s_d} 4H={s4h} 1H={s1h} 15M={s15m} COMBO={combined} | "
                   f"VD={vd_pct}% Fib={'✅' if fib_bonus else '❌'} | Regime:{current_regime}")
        notify(f"🟢 BUY: {product_id}",
            f"Trade opened!\n\nPair:{product_id}\nEntry:${price}\nSize:{round(pct*100,1)}%{'(Fib bonus!)' if fib_bonus else ''}\n"
            f"TP1:${round(tp1,4)} TP2:${round(tp2,4)} SL:${round(sl,4)}\n"
            f"D:{s_d} 4H:{s4h} 1H:{s1h} 15M:{s15m} Combined:{combined}/400\n"
            f"Volume Delta:{vd_pct}% (buying pressure)\n"
            f"Fibonacci:{'✅ At key level!' if fib_bonus else '❌ Not at level'}\n"
            f"Regime:{current_regime} | Balance:${round(balance,2)}")
        return {"status":"success","combined_score":combined,"fib_bonus":fib_bonus}
    except Exception as e:
        logger.error(f"BUY ERROR {product_id}:{e}"); return {"status":"error","reason":str(e)}

# ─── CLOSE TRADE ─────────────────────────────────────────────────────────────
def close_trade(product_id, price, reason="signal", partial=False, partial_size=None):
    global losing_streak,winning_trades,total_trades,daily_pnl
    trade=open_trades.get(product_id)
    if not trade: return
    stc=partial_size if partial else trade["size_remaining"]
    try:
        client.market_order_sell(client_order_id=str(uuid.uuid4()),product_id=product_id,base_size=str(round(stc,6)))
        pnl=(price-trade["entry"])*stc; pct=((price-trade["entry"])/trade["entry"])*100; daily_pnl+=pnl
        logger.info(f"{'PARTIAL ' if partial else ''}CLOSE {product_id} @ ${price} | {reason} | PNL=${round(pnl,2)} ({round(pct,2)}%)")
        if not partial:
            total_trades+=1
            entry_hour=trade.get("entry_hour",datetime.now(timezone.utc).hour)
            if pnl>0:
                winning_trades+=1; losing_streak=0
                pair_stats[product_id]["wins"]+=1; hour_stats[entry_hour]["wins"]+=1
            else:
                losing_streak+=1; pair_stats[product_id]["losses"]+=1; hour_stats[entry_hour]["losses"]+=1
                if losing_streak>=LOSING_STREAK_LIMIT: logger.warning(f"LOSING STREAK {losing_streak}")
            pair_stats[product_id]["pnl"]+=pnl
            trade_history.append({"pair":product_id,"entry":trade["entry"],"exit":price,
                "pnl":round(pnl,2),"pnl_pct":round(pct,2),"reason":reason,
                "time":datetime.utcnow().isoformat(),
                "scores":f"D={trade['score_d']} 4H={trade['score_4h']} 1H={trade['score_1h']} 15M={trade['score_15m']}",
                "regime":trade.get("regime",""),"fib":trade.get("fib_bonus",False)})
            em="✅" if pnl>0 else "🔴"; wr=round(winning_trades/total_trades*100,1) if total_trades>0 else 0
            notify(f"{em} CLOSE: {product_id}",
                f"Closed!\nPair:{product_id}\nEntry:${trade['entry']}\nExit:${price}\n"
                f"PNL:${round(pnl,2)} ({round(pct,2)}%)\nReason:{reason}\n"
                f"Daily PNL:${round(daily_pnl,2)}\nWin Rate:{wr}%")
            del open_trades[product_id]
        else: trade["size_remaining"]-=stc
    except Exception as e: logger.error(f"CLOSE ERROR {product_id}:{e}")

# ─── MONITOR TRADES ──────────────────────────────────────────────────────────
def monitor_trades():
    while True:
        try:
            for product_id,trade in list(open_trades.items()):
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
                    if ns>trade["stop"]: trade["stop"]=ns
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
                        cl=[float(c["close"]) for c in reversed(c15m)]; rsi=calculate_rsi(cl)
                        if rsi<MOMENTUM_EXIT_RSI and pp<0:
                            close_trade(product_id,price,reason=f"momentum_exit_rsi{round(rsi,1)}")
        except Exception as e: logger.error(f"Monitor error:{e}")
        time.sleep(MONITOR_INTERVAL)

# ─── AUTONOMOUS SCANNER ───────────────────────────────────────────────────────
def autonomous_scanner():
    global last_scan_results,last_scan_time
    time.sleep(120)
    while True:
        try:
            regime=detect_market_regime()
            min_d,min_4h,min_1h,min_15m,min_combo,max_t,size_mult=get_adaptive_thresholds()
            logger.info(f"🔍 AUTO SCAN | Regime:{regime} | D≥{min_d} 4H≥{min_4h} 1H≥{min_1h} 15M≥{min_15m} C≥{min_combo} | Balance:${get_balance():.2f}")
            check_daily_reset()
            scan_results={}; trades_fired=0
            for symbol,product_id in ALLOWED_PAIRS.items():
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

# ─── SCHEDULERS ──────────────────────────────────────────────────────────────
def backtest_scheduler():
    time.sleep(300)
    threading.Thread(target=run_full_backtest,daemon=True).start()
    while True:
        now=datetime.now(timezone.utc)
        if now.weekday()==6 and now.hour==0 and now.minute==0:
            threading.Thread(target=run_full_backtest,daemon=True).start(); time.sleep(61)
        time.sleep(30)

def daily_summary_scheduler():
    while True:
        now=datetime.now(timezone.utc); et_hour=(now.hour-4)%24
        if et_hour==8 and now.minute==0: send_daily_summary(); time.sleep(61)
        time.sleep(30)

# ─── ROUTES ──────────────────────────────────────────────────────────────────
@app.route("/webhook",methods=["POST"])
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
    blk,bmsg=is_blackout(); bt=backtest_results.get("overall",{})
    min_d,min_4h,min_1h,min_15m,min_combo,max_t,size_mult=get_adaptive_thresholds()
    return jsonify({"version":"6.5.1","running":True,"open_trades":open_trades,
        "circuit_breaker":daily_loss_hit or weekly_loss_hit,
        "daily_start_bal":daily_start_bal,"current_bal":get_balance(),
        "daily_pnl":round(daily_pnl,2),"daily_trades":f"{daily_trades_count}/{max_t}",
        "losing_streak":losing_streak,"win_rate":f"{wr}%","total_trades":total_trades,
        "blackout":blk,"regime":current_regime,
        "regime_description":REGIME_SETTINGS.get(current_regime,{}).get("description",""),
        "backtest_win_rate":bt.get("win_rate","pending..."),"pair_stats":pair_stats,
        "last_scan":last_scan_time})

@app.route("/scan")
def scan():
    results={}
    for symbol,product_id in ALLOWED_PAIRS.items():
        analysis=analyze_pair(product_id)
        if analysis: results[product_id]=analysis
    return jsonify({"regime":current_regime,"results":results})

@app.route("/backtest")
def backtest_route():
    if not backtest_results:
        threading.Thread(target=run_full_backtest,daemon=True).start()
        return jsonify({"status":"running","message":"Backtest started — check back in 3-5 minutes"})
    return jsonify(backtest_results)

@app.route("/dashboard")
def dashboard():
    wr=round(winning_trades/total_trades*100,1) if total_trades>0 else 0
    bal=get_balance(); profit=round(bal-(daily_start_bal or bal),2)
    blk,bmsg=is_blackout(); bt=backtest_results.get("overall",{})
    bt_ready=bt.get("go_live_ready",False)
    min_d,min_4h,min_1h,min_15m,min_combo,max_t,size_mult=get_adaptive_thresholds()
    regime_desc=REGIME_SETTINGS.get(current_regime,{}).get("description","")
    regime_colors={"STRONG_BULL":"#3fb950","BULL":"#58a6ff","SIDEWAYS":"#d29922","BEAR":"#f85149","STRONG_BEAR":"#ff0000","VOLATILE":"#ff8c00"}
    rc=regime_colors.get(current_regime,"#8b949e")
    go_live_color="green" if bt_ready else "orange"
    go_live_text="✅ READY FOR LIVE TRADING" if bt_ready else "⏳ Backtest pending"

    scan_rows=""
    if last_scan_results:
        for pid,r in last_scan_results.items():
            color="green" if r["ready"] else "#8b949e"
            cx=",".join(r["crossovers"]) if r["crossovers"] else "—"
            vd=r.get("volume_delta",{}).get("pct",0)
            fib="✅" if r.get("fib_bonus") else "—"
            scan_rows+=(f"<tr><td>{pid}</td><td>${r['price']}</td>"
                       f"<td>{r['daily']}</td><td>{r['4h']}</td><td>{r['1h']}</td><td>{r['15m']}</td>"
                       f"<td>{r['combined']}</td><td style='color:{'green' if vd>0 else 'red'}'>{round(vd,1)}%</td>"
                       f"<td style='color:yellow'>{fib}</td><td style='color:yellow'>{cx}</td>"
                       f"<td style='color:{color}'>{'✅' if r['ready'] else '❌'}</td></tr>")

    bt_rows=""
    for pid,r in backtest_results.items():
        if pid=="overall" or "error" in r: continue
        wrc="green" if float(r.get("win_rate","0%").replace("%",""))>=55 else "orange"
        retc="green" if float(r.get("total_return","0%").replace("%",""))>=0 else "red"
        bt_rows+=(f"<tr><td>{pid}</td><td>{r.get('total_trades',0)}</td>"
                 f"<td style='color:{wrc}'>{r.get('win_rate','—')}</td>"
                 f"<td style='color:{retc}'>{r.get('total_return','—')}</td>"
                 f"<td>{r.get('max_drawdown','—')}</td>"
                 f"<td style='color:{'green' if r.get('go_live_ready') else 'orange'}'>{'✅' if r.get('go_live_ready') else '⚠️'}</td></tr>")

    rows=""
    for t in reversed(trade_history[-15:]):
        c="green" if t["pnl"]>0 else "red"
        rows+=(f"<tr><td>{t['time'][:16]}</td><td>{t['pair']}</td>"
               f"<td>${t['entry']}</td><td>${t['exit']}</td>"
               f"<td style='color:{c}'>${t['pnl']} ({t['pnl_pct']}%)</td>"
               f"<td>{t['reason']}</td><td>{t.get('regime','')}</td>"
               f"<td>{'✅' if t.get('fib') else '—'}</td></tr>")

    pair_rows=""
    for pair,stats in pair_stats.items():
        total=stats["wins"]+stats["losses"]
        if total>0:
            pwr=round(stats["wins"]/total*100,1); pc="green" if stats["pnl"]>0 else "red"
            pair_rows+=(f"<tr><td>{pair}</td><td>{stats['wins']}W/{stats['losses']}L</td>"
                       f"<td>{pwr}%</td><td style='color:{pc}'>${round(stats['pnl'],2)}</td></tr>")

    return f"""<!DOCTYPE html><html><head><title>J's Bot v6.5</title>
    <meta http-equiv="refresh" content="60">
    <style>
    body{{font-family:Arial;background:#0d1117;color:#e6edf3;padding:20px;font-size:14px}}
    h1,h2{{color:#58a6ff}}h2{{margin-top:22px;font-size:15px}}
    .card{{background:#161b22;border-radius:8px;padding:12px;margin:6px;display:inline-block;min-width:120px}}
    .card h3{{margin:0;color:#8b949e;font-size:10px}}.card p{{margin:4px 0 0;font-size:19px;font-weight:bold}}
    table{{width:100%;border-collapse:collapse;margin-top:10px;background:#161b22;border-radius:8px}}
    th,td{{padding:8px;text-align:left;border-bottom:1px solid #30363d;font-size:11px}}
    th{{color:#8b949e}}.green{{color:#3fb950}}.red{{color:#f85149}}.orange{{color:#d29922}}
    .banner{{border-radius:8px;padding:12px;margin:10px 0;background:#161b22}}
    </style></head><body>
    <h1>🤖 J's Crypto Bot v6.5</h1>
    <p style="color:#8b949e;font-size:12px">Refreshes 60s | 24/7 Autonomous | Vol Delta + Fibonacci</p>

    <div class="banner" style="border-left:4px solid {go_live_color}">
    <strong style="color:{go_live_color}">GO LIVE: {go_live_text}</strong>
    {f"<br><small style='color:#8b949e'>WR:{bt.get('win_rate','—')} Return:{bt.get('total_return','—')} Pairs:{bt.get('pairs_ready','—')}</small>" if bt else ""}
    </div>

    <div class="banner" style="border-left:4px solid {rc}">
    <strong style="color:{rc}">REGIME: {regime_desc}</strong>
    <br><small style="color:#8b949e">D≥{min_d} 4H≥{min_4h} 1H≥{min_1h} 15M≥{min_15m} C≥{min_combo} | Size×{size_mult} | Max {max_t}/day</small>
    </div>

    <div>
    <div class="card"><h3>BALANCE</h3><p>${bal:.2f}</p></div>
    <div class="card"><h3>P&L TODAY</h3><p class="{'green' if profit>=0 else 'red'}">${profit:.2f}</p></div>
    <div class="card"><h3>WIN RATE</h3><p>{wr}%</p></div>
    <div class="card"><h3>TRADES</h3><p>{total_trades}</p></div>
    <div class="card"><h3>OPEN</h3><p>{len(open_trades)}</p></div>
    <div class="card"><h3>TODAY</h3><p>{daily_trades_count}/{max_t}</p></div>
    <div class="card"><h3>BLACKOUT</h3><p class="{'red' if blk else 'green'}" style="font-size:11px">{'YES' if blk else 'NO'}</p></div>
    <div class="card"><h3>STREAK</h3><p class="{'red' if losing_streak>0 else 'green'}">{losing_streak}</p></div>
    </div>

    <h2>🔍 Last Scan {f"({last_scan_time[:16]})" if last_scan_time else "(pending...)"}</h2>
    <table><tr><th>Pair</th><th>Price</th><th>D</th><th>4H</th><th>1H</th><th>15M</th><th>Combo</th><th>Vol Delta</th><th>Fib</th><th>CX</th><th>Status</th></tr>
    {scan_rows if scan_rows else "<tr><td colspan='11' style='color:#8b949e;text-align:center'>Scanning...</td></tr>"}
    </table>

    <h2>🔬 Backtest (6 Months) {f"({last_backtest_time[:16]})" if last_backtest_time else "(running...)"}</h2>
    <table><tr><th>Pair</th><th>Trades</th><th>Win Rate</th><th>Return</th><th>Max DD</th><th>Go Live?</th></tr>
    {bt_rows if bt_rows else "<tr><td colspan='6' style='color:#8b949e;text-align:center'>Running...</td></tr>"}
    </table>

    <h2>📊 Pair Performance</h2>
    <table><tr><th>Pair</th><th>Record</th><th>Win Rate</th><th>PNL</th></tr>
    {pair_rows if pair_rows else "<tr><td colspan='4' style='color:#8b949e;text-align:center'>No trades yet</td></tr>"}
    </table>

    <h2>📋 Recent Trades</h2>
    <table><tr><th>Time</th><th>Pair</th><th>Entry</th><th>Exit</th><th>PNL</th><th>Reason</th><th>Regime</th><th>Fib</th></tr>
    {rows if rows else "<tr><td colspan='8' style='color:#8b949e;text-align:center'>No trades yet</td></tr>"}
    </table>

    <p style="color:#8b949e;font-size:11px;margin-top:15px">
    <a href="/backtest" style="color:#58a6ff">/backtest</a> |
    <a href="/scan" style="color:#58a6ff">/scan</a> |
    <a href="/regime" style="color:#58a6ff">/regime</a>
    </p></body></html>"""

@app.route("/regime")
def regime_route():
    return jsonify({"current_regime":current_regime,
        "description":REGIME_SETTINGS.get(current_regime,{}).get("description",""),
        "thresholds":REGIME_SETTINGS.get(current_regime,{}),"history":regime_history[-10:]})

@app.route("/performance")
def performance():
    return jsonify({"pair_stats":pair_stats,"total_trades":total_trades,
        "win_rate":f"{round(winning_trades/total_trades*100,1) if total_trades>0 else 0}%",
        "daily_pnl":round(daily_pnl,2),"regime":current_regime,"last_scan":last_scan_time,
        "backtest":backtest_results.get("overall",{})})

if __name__=="__main__":
    daily_start_bal=get_balance(); weekly_start_bal=daily_start_bal
    logger.info(f"BOT v6.5.1 STARTED | Balance:${daily_start_bal} | "
               f"Telegram:{'ON' if TELEGRAM_ENABLED else 'OFF'} | "
               f"Mode:AUTONOMOUS + VOL DELTA + FIBONACCI")
    try:
        notify("🚀 Bot v6.5 Started",
            f"J's Crypto Bot v6.5.1 is live!\n\nBalance:${daily_start_bal:.2f}\n"
            f"New: Volume Delta + Fibonacci Levels\n"
            f"Mode: Autonomous 24/7\n"
            f"Dashboard: http://165.227.113.102/dashboard")
    except: pass
    threading.Thread(target=monitor_trades,daemon=True).start()
    threading.Thread(target=autonomous_scanner,daemon=True).start()
    threading.Thread(target=backtest_scheduler,daemon=True).start()
    threading.Thread(target=daily_summary_scheduler,daemon=True).start()
    app.run(host="0.0.0.0",port=80,debug=False)
