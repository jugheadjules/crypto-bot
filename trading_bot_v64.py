"""
J's AI CRYPTO BOT v6.4 - REGIME DETECTION + ADAPTIVE THRESHOLDS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Upgrades over v6.3:
  ✅ Market regime detection (Bull / Bear / Sideways / Volatile)
  ✅ Adaptive thresholds - adjusts score requirements by regime
  ✅ Regime-specific strategy - more aggressive in bull, defensive in bear
  ✅ Regime shown on dashboard and status
  ✅ Telegram notifications (add token + chat_id to .env)
  ✅ All v6.3 features retained
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
EMAIL_SENDER    = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD  = os.getenv("EMAIL_PASSWORD", "")
EMAIL_RECEIVER  = os.getenv("EMAIL_RECEIVER", "")
EMAIL_ENABLED   = all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER])
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
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

# ─── BASE SCORING THRESHOLDS (adjusted by regime) ─────────────────────────────
BASE_MIN_DAILY    = 65
BASE_MIN_4H       = 68
BASE_MIN_1H       = 65
BASE_MIN_15M      = 62
BASE_MIN_COMBINED = 260

# ─── REGIME THRESHOLDS ────────────────────────────────────────────────────────
REGIME_SETTINGS = {
    "STRONG_BULL": {
        "min_daily": 60, "min_4h": 62, "min_1h": 58, "min_15m": 50,
        "min_combined": 230, "max_trades": 5, "size_mult": 1.2,
        "description": "🐂 Strong Bull — aggressive entries"
    },
    "BULL": {
        "min_daily": 63, "min_4h": 65, "min_1h": 62, "min_15m": 55,
        "min_combined": 245, "max_trades": 5, "size_mult": 1.1,
        "description": "📈 Bull Market — normal entries"
    },
    "SIDEWAYS": {
        "min_daily": 65, "min_4h": 68, "min_1h": 65, "min_15m": 62,
        "min_combined": 260, "max_trades": 4, "size_mult": 1.0,
        "description": "➡️ Sideways — standard entries"
    },
    "BEAR": {
        "min_daily": 70, "min_4h": 72, "min_1h": 68, "min_15m": 65,
        "min_combined": 275, "max_trades": 3, "size_mult": 0.8,
        "description": "📉 Bear Market — defensive, fewer trades"
    },
    "STRONG_BEAR": {
        "min_daily": 75, "min_4h": 75, "min_1h": 72, "min_15m": 68,
        "min_combined": 290, "max_trades": 2, "size_mult": 0.6,
        "description": "🐻 Strong Bear — very defensive"
    },
    "VOLATILE": {
        "min_daily": 72, "min_4h": 72, "min_1h": 68, "min_15m": 65,
        "min_combined": 277, "max_trades": 2, "size_mult": 0.7,
        "description": "⚡ Volatile — reduced size, tight entries"
    }
}

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
backtest_results   = {}
last_backtest_time = None
current_regime     = "SIDEWAYS"
regime_history     = []
last_daily_reset   = datetime.utcnow().date()
last_weekly_reset  = datetime.utcnow().isocalendar()[1]

pair_stats = {p: {"wins":0,"losses":0,"pnl":0.0} for p in ALLOWED_PAIRS.values()}
hour_stats = {h: {"wins":0,"losses":0} for h in range(24)}

# ─── NOTIFICATIONS ────────────────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_ENABLED:
        logger.info(f"TELEGRAM(disabled):{message[:50]}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": message, "parse_mode": "HTML"}, timeout=10)
        logger.info("Telegram sent")
    except Exception as e:
        logger.error(f"Telegram error:{e}")

def send_email(subject, body):
    if not EMAIL_ENABLED:
        logger.info(f"EMAIL(disabled):{subject}")
        return
    try:
        msg = MIMEMultipart()
        msg["From"]=EMAIL_SENDER; msg["To"]=EMAIL_RECEIVER; msg["Subject"]=subject
        msg.attach(MIMEText(body,"plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
            s.login(EMAIL_SENDER,EMAIL_PASSWORD); s.send_message(msg)
        logger.info(f"Email sent:{subject}")
    except Exception as e:
        logger.error(f"Email error:{e}")

def notify(subject, body):
    """Send both email and Telegram"""
    send_email(subject, body)
    send_telegram(f"<b>{subject}</b>\n\n{body}")

# ─── BALANCE & PRICE ─────────────────────────────────────────────────────────
def get_balance():
    try:
        accounts = client.get_accounts(limit=250)
        for a in accounts["accounts"]:
            if a["currency"]=="USD":
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
        interval = seconds.get(granularity,3600)
        end=int(time.time()); start=end-(interval*limit)
        candles=client.get_candles(product_id=product_id,start=start,end=end,granularity=granularity)
        return candles["candles"]
    except Exception as e: logger.error(f"Candle error {product_id} {granularity}:{e}"); return None

def get_historical_candles(product_id, granularity="ONE_HOUR", days_back=180):
    try:
        seconds  = {"FIFTEEN_MINUTE":900,"ONE_HOUR":3600,"FOUR_HOUR":14400,"ONE_DAY":86400}
        interval = seconds.get(granularity,3600)
        all_candles=[]; end=int(time.time()); start=end-(days_back*86400)
        while start < end:
            chunk_end=min(start+interval*300,end)
            try:
                candles=client.get_candles(product_id=product_id,start=start,end=chunk_end,granularity=granularity)
                if candles and candles.get("candles"):
                    all_candles.extend(candles["candles"])
            except: pass
            start=chunk_end; time.sleep(0.3)
        all_candles.sort(key=lambda x:int(x["start"]))
        return all_candles
    except Exception as e: logger.error(f"Historical candle error:{e}"); return []

# ─── INDICATORS ──────────────────────────────────────────────────────────────
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
    """Average True Range - measures volatility"""
    if len(candles) < period+1: return 0
    trs=[]
    for i in range(1,len(candles)):
        h=float(candles[i]["high"]); l=float(candles[i]["low"]); pc=float(candles[i-1]["close"])
        tr=max(h-l, abs(h-pc), abs(l-pc))
        trs.append(tr)
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

# ─── REGIME DETECTION ─────────────────────────────────────────────────────────
def detect_market_regime():
    """
    Detect overall market regime using BTC as the market indicator.
    Returns: STRONG_BULL, BULL, SIDEWAYS, BEAR, STRONG_BEAR, or VOLATILE
    """
    global current_regime, regime_history
    try:
        # Use BTC-USD as market proxy
        candles_daily = get_candles("BTC-USD", "ONE_DAY", 30)
        candles_4h    = get_candles("BTC-USD", "FOUR_HOUR", 50)
        if not candles_daily or not candles_4h:
            return current_regime

        daily_cc = list(reversed(candles_daily))
        h4_cc    = list(reversed(candles_4h))

        daily_closes = [float(c["close"]) for c in daily_cc]
        h4_closes    = [float(c["close"]) for c in h4_cc]
        price        = daily_closes[-1]

        # Indicators
        rsi_daily = calculate_rsi(daily_closes)
        rsi_4h    = calculate_rsi(h4_closes)
        ema21     = calculate_ema(daily_closes, 21)
        ema50     = calculate_ema(daily_closes, 50)
        ema200    = calculate_ema(daily_closes, 200) if len(daily_closes)>=200 else ema50
        atr       = calculate_atr(daily_cc)
        atr_pct   = (atr/price)*100 if price>0 else 0

        # 30-day momentum
        mom_30 = (daily_closes[-1]-daily_closes[0])/daily_closes[0]*100 if len(daily_closes)>=2 else 0
        # 7-day momentum
        mom_7  = (daily_closes[-1]-daily_closes[-7])/daily_closes[-7]*100 if len(daily_closes)>=7 else 0

        # Higher highs/lows
        hhl = detect_higher_highs_lows(daily_closes, 14)

        # Regime scoring
        bull_score = 0
        bear_score = 0

        # Price vs EMAs
        if price > ema21:   bull_score += 2
        else:               bear_score += 2
        if price > ema50:   bull_score += 2
        else:               bear_score += 2
        if price > ema200:  bull_score += 1
        else:               bear_score += 1
        if ema21 > ema50:   bull_score += 1
        else:               bear_score += 1

        # RSI
        if rsi_daily > 55:  bull_score += 2
        elif rsi_daily < 45: bear_score += 2
        if rsi_4h > 55:     bull_score += 1
        elif rsi_4h < 45:   bear_score += 1

        # Momentum
        if mom_30 > 10:     bull_score += 2
        elif mom_30 < -10:  bear_score += 2
        elif mom_30 > 3:    bull_score += 1
        elif mom_30 < -3:   bear_score += 1

        if mom_7 > 3:       bull_score += 1
        elif mom_7 < -3:    bear_score += 1

        # Higher highs/lows
        if hhl:             bull_score += 1
        else:               bear_score += 1

        # Volatility check
        if atr_pct > 5:
            regime = "VOLATILE"
        elif bull_score >= 10:
            regime = "STRONG_BULL"
        elif bull_score >= 7:
            regime = "BULL"
        elif bear_score >= 10:
            regime = "STRONG_BEAR"
        elif bear_score >= 7:
            regime = "BEAR"
        else:
            regime = "SIDEWAYS"

        if regime != current_regime:
            old_regime = current_regime
            current_regime = regime
            regime_history.append({
                "time": datetime.utcnow().isoformat(),
                "old": old_regime,
                "new": regime,
                "bull_score": bull_score,
                "bear_score": bear_score
            })
            msg = (f"🔄 REGIME CHANGE: {old_regime} → {regime}\n\n"
                   f"BTC Price: ${price:,.2f}\n"
                   f"RSI Daily: {round(rsi_daily,1)}\n"
                   f"30d Momentum: {round(mom_30,1)}%\n"
                   f"7d Momentum: {round(mom_7,1)}%\n"
                   f"Bull Score: {bull_score} | Bear Score: {bear_score}\n\n"
                   f"Strategy: {REGIME_SETTINGS[regime]['description']}")
            logger.info(f"REGIME CHANGE: {old_regime} → {regime} | Bull:{bull_score} Bear:{bear_score}")
            notify(f"🔄 Market Regime Changed: {regime}", msg)
        else:
            logger.info(f"REGIME: {regime} | Bull:{bull_score} Bear:{bear_score} | BTC=${price:,.2f} RSI={round(rsi_daily,1)} Mom30={round(mom_30,1)}%")

        return regime

    except Exception as e:
        logger.error(f"Regime detection error:{e}")
        return current_regime

def get_adaptive_thresholds():
    """Get scoring thresholds based on current regime"""
    settings = REGIME_SETTINGS.get(current_regime, REGIME_SETTINGS["SIDEWAYS"])
    return (
        settings["min_daily"],
        settings["min_4h"],
        settings["min_1h"],
        settings["min_15m"],
        settings["min_combined"],
        settings["max_trades"],
        settings["size_mult"]
    )

# ─── BLACKOUT & FILTERS ───────────────────────────────────────────────────────
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
            daily_loss_hit=True
            notify("🚨 Daily Loss Limit Hit",f"Balance:${bal:.2f} Start:${daily_start_bal:.2f}")
            return True,"daily loss limit"
        if weekly_start_bal and bal<weekly_start_bal*(1-WEEKLY_LOSS_LIMIT):
            weekly_loss_hit=True
            notify("🚨 Weekly Loss Limit Hit",f"Balance:${bal:.2f} Start:${weekly_start_bal:.2f}")
            return True,"weekly loss limit"
    except Exception as e: logger.error(f"CB error:{e}")
    return False,"ok"

# ─── POSITION SIZING ─────────────────────────────────────────────────────────
def get_size_pct(combined, product_id):
    _,_,_,_,_,_,regime_mult = get_adaptive_thresholds()
    stats=pair_stats.get(product_id,{"wins":0,"losses":0})
    total=stats["wins"]+stats["losses"]
    pair_mult=1.0
    if total>=5:
        wr=stats["wins"]/total
        if wr>=0.70:   pair_mult=1.3
        elif wr>=0.60: pair_mult=1.1
        elif wr<=0.35: pair_mult=0.7
    if losing_streak>=LOSING_STREAK_LIMIT: base=0.010
    elif combined>=360: base=0.050
    elif combined>=330: base=0.035
    elif combined>=300: base=0.025
    else:               base=0.015
    return min(base*pair_mult*regime_mult, 0.05)

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
    bal=get_balance()
    wr=round(winning_trades/total_trades*100,1) if total_trades>0 else 0
    regime_info=REGIME_SETTINGS.get(current_regime,{}).get("description","Unknown")
    bt=backtest_results.get("overall",{})
    body=(f"Good morning J! ☀️\n\nBalance:${bal:.2f}\n"
          f"Total Trades:{total_trades}\nWin Rate:{wr}%\nDaily PNL:${daily_pnl:.2f}\n\n"
          f"Market Regime: {regime_info}\n\n"
          f"Backtest WR:{bt.get('win_rate','pending')}\n"
          f"Bot scanning 24/7\n- Your Bot 🤖 v6.4")
    notify("☀️ Daily Summary v6.4", body)

# ─── SCORING ─────────────────────────────────────────────────────────────────
def score_timeframe(product_id, granularity, price, label):
    score=0; reason=[]
    candles=get_candles(product_id,granularity=granularity,limit=60)
    if not candles: return 0,[],False
    cc=list(reversed(candles))
    closes=[float(c["close"]) for c in cc]; volumes=[float(c["volume"]) for c in cc]
    crossover=detect_ema_crossover(closes); aligned=detect_ema_aligned(closes,price)
    rsi=calculate_rsi(closes)
    if 38<=rsi<=65:   score+=20; reason.append(f"RSI={round(rsi,1)} ideal")
    elif 30<=rsi<=72: score+=10; reason.append(f"RSI={round(rsi,1)} ok")
    else:             reason.append(f"RSI={round(rsi,1)} poor")
    avg_vol=sum(volumes[:-1])/max(len(volumes)-1,1); vr=volumes[-1]/avg_vol if avg_vol>0 else 0
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
    vwap=calculate_vwap(cc[-20:])
    if vwap>0 and price>vwap: score+=10; reason.append("Above VWAP")
    else:                     reason.append("Below VWAP")
    if detect_support(closes): score+=10; reason.append("Near support")
    if detect_bullish_candle(cc[-3:]): score+=5; reason.append("Bullish candle")
    logger.info(f"{label} {product_id}:{score}/100 | {' | '.join(reason)}")
    return score,reason,crossover

def score_daily(product_id, price):
    score=0; reason=[]
    candles=get_candles(product_id,granularity="ONE_DAY",limit=50)
    if not candles: return 0,[]
    cc=list(reversed(candles))
    closes=[float(c["close"]) for c in cc]; vols=[float(c["volume"]) for c in cc]
    rsi=calculate_rsi(closes)
    if 50<=rsi<=72:   score+=25; reason.append(f"RSI={round(rsi,1)} bullish")
    elif 45<=rsi<50:  score+=10; reason.append(f"RSI={round(rsi,1)} neutral")
    else:             reason.append(f"RSI={round(rsi,1)} bearish")
    e21=calculate_ema(closes,21); e50=calculate_ema(closes,50)
    if price>e21 and e21>e50: score+=25; reason.append("Above EMA21&50")
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
    return score,reason

def analyze_pair(product_id):
    price=get_price(product_id)
    if not price: return None
    min_d,min_4h,min_1h,min_15m,min_combo,_,_ = get_adaptive_thresholds()
    s_d,_=score_daily(product_id,price)
    s4h,_,x4h=score_timeframe(product_id,"FOUR_HOUR",price,"4H")
    s1h,_,x1h=score_timeframe(product_id,"ONE_HOUR",price,"1H")
    s15m,_,x15m=score_timeframe(product_id,"FIFTEEN_MINUTE",price,"15M")
    combined=s_d+s4h+s1h+s15m
    crossovers=[tf for tf,cx in [("4H",x4h),("1H",x1h),("15M",x15m)] if cx]
    ready=(s_d>=min_d and s4h>=min_4h and s1h>=min_1h and s15m>=min_15m and combined>=min_combo)
    result={"price":price,"daily":s_d,"4h":s4h,"1h":s1h,"15m":s15m,
            "combined":combined,"crossovers":crossovers,"ready":ready,
            "regime":current_regime,"thresholds":f"D≥{min_d} 4H≥{min_4h} 1H≥{min_1h} 15M≥{min_15m} C≥{min_combo}",
            "time":datetime.utcnow().isoformat()}
    logger.info(f"SCAN {product_id}: D={s_d} 4H={s4h} 1H={s1h} 15M={s15m} "
                f"COMBO={combined}/400 | CX:{crossovers or 'none'} | "
                f"Regime:{current_regime} | {'✅ READY' if ready else '❌'}")
    return result

# ─── BACKTESTER ───────────────────────────────────────────────────────────────
def run_backtest(product_id, days=180, starting_balance=66.7):
    logger.info(f"BACKTEST {product_id}: pulling {days} days...")
    candles=get_historical_candles(product_id,granularity="ONE_HOUR",days_back=days)
    if len(candles)<50: return {"error":"not enough data","pair":product_id}
    balance=starting_balance; trades=[]; wins=0; losses=0
    max_balance=balance; min_balance=balance
    in_trade=False; entry=0; entry_time=0; tp1=0; tp2=0; sl=0
    tp1_hit=False; size=0; size_rem=0; cooldown=0
    for i in range(60,len(candles)):
        window=candles[max(0,i-59):i+1]
        closes=[float(c["close"]) for c in window]
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
                    pnl=(current-entry)*size_rem; balance+=pnl; losses+=1
                    trades.append({"entry":entry,"exit":current,"pnl":round((current-entry)/entry*100,2),"result":"loss","reason":"SL"})
                    in_trade=False; tp1_hit=False; size_rem=0; cooldown=0
                elif (ts-entry_time)>TIME_EXIT_HOURS*3600 and current<entry:
                    pnl=(current-entry)*size_rem; balance+=pnl; losses+=1
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
        if aligned:     score+=35
        if mom>=0:      score+=20
        if crossover:   score+=10
        if score<70: continue
        risk=balance*0.02; size=risk/current; size_rem=size
        entry=current; entry_time=ts
        tp1=entry*(1+TAKE_PROFIT_1); tp2=entry*(1+TAKE_PROFIT_2); sl=entry*(1-STOP_LOSS_PCT)
        in_trade=True; tp1_hit=False; cooldown=4
        max_balance=max(max_balance,balance); min_balance=min(min_balance,balance)
    if in_trade and len(candles)>0:
        final=float(candles[-1]["close"]); pnl=(final-entry)*size_rem; balance+=pnl
        if pnl>0: wins+=1
        else: losses+=1
        trades.append({"entry":entry,"exit":final,"pnl":round((final-entry)/entry*100,2),"result":"win" if pnl>0 else "loss","reason":"end"})
    total=wins+losses
    win_rate=round(wins/total*100,1) if total>0 else 0
    total_ret=round((balance-starting_balance)/starting_balance*100,1)
    max_dd=round((max_balance-min_balance)/max_balance*100,1) if max_balance>0 else 0
    avg_pnl=round(sum(t["pnl"] for t in trades)/len(trades),2) if trades else 0
    result={"pair":product_id,"days":days,"total_trades":total,"wins":wins,"losses":losses,
            "win_rate":f"{win_rate}%","total_return":f"{total_ret}%","final_balance":round(balance,2),
            "max_drawdown":f"{max_dd}%","avg_trade_pnl":f"{avg_pnl}%","start_balance":starting_balance,
            "go_live_ready":win_rate>=55 and max_dd<=15,"trades_sample":trades[-5:] if trades else []}
    logger.info(f"BACKTEST {product_id}: {total} trades | WR:{win_rate}% | Return:{total_ret}% | MaxDD:{max_dd}% | {'✅ GO LIVE' if result['go_live_ready'] else '❌'}")
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
            f"6-Month Backtest Done!\n\nAvg Win Rate:{avg_wr}%\nAvg Return:{avg_ret}%\nPairs Ready:{ready}/{len(valid)}\n\n{pairs_str}\n\n{'✅ READY FOR LIVE!' if ready>=3 else '⚠️ Needs optimization'}")
    backtest_results=results; last_backtest_time=datetime.utcnow().isoformat()
    logger.info(f"🔬 BACKTEST COMPLETE")
    return results

# ─── SMART FILTER ────────────────────────────────────────────────────────────
def smart_filter(product_id, price):
    _,_,_,_,_,max_t,_ = get_adaptive_thresholds()
    now=time.time()
    if price<=0:                      return False,"invalid price"
    if product_id in open_trades:     return False,"already in trade"
    if len(open_trades)>=MAX_OPEN_TRADES: return False,"max trades open"
    if daily_trades_count>=max_t:     return False,f"max daily trades ({max_t} in {current_regime})"
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
    if daily_loss_hit:  return {"status":"skip","reason":"daily circuit breaker"}
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
    min_d,min_4h,min_1h,min_15m,min_combo,_,_ = get_adaptive_thresholds()
    s_d=analysis["daily"]; s4h=analysis["4h"]; s1h=analysis["1h"]
    s15m=analysis["15m"]; combined=analysis["combined"]
    if s_d<min_d:        return {"status":"skip","reason":f"Daily={s_d} need {min_d} ({current_regime})"}
    if s4h<min_4h:       return {"status":"skip","reason":f"4H={s4h} need {min_4h}"}
    if s1h<min_1h:       return {"status":"skip","reason":f"1H={s1h} need {min_1h}"}
    if s15m<min_15m:     return {"status":"skip","reason":f"15M={s15m} need {min_15m}"}
    if combined<min_combo: return {"status":"skip","reason":f"Combined={combined} need {min_combo}"}
    c1h=get_candles(product_id,"ONE_HOUR",15)
    if c1h:
        cl=[float(c["close"]) for c in reversed(c1h)]
        if not check_volatility(cl): return {"status":"skip","reason":"market too flat"}
    balance=get_balance(); pct=get_size_pct(combined,product_id); size=round((balance*pct)/price,6)
    try:
        client.market_order_buy(client_order_id=str(uuid.uuid4()),product_id=product_id,quote_size=str(round(size*price,2)))
        tp1=price*(1+TAKE_PROFIT_1); tp2=price*(1+TAKE_PROFIT_2); sl=price*(1-STOP_LOSS_PCT)
        now_utc=datetime.now(timezone.utc)
        open_trades[product_id]={"entry":price,"size":size,"size_remaining":size,"trail_high":price,
            "stop":sl,"take_profit_1":tp1,"take_profit_2":tp2,"tp1_hit":False,
            "time":now_utc.isoformat(),"entry_epoch":time.time(),
            "score_d":s_d,"score_4h":s4h,"score_1h":s1h,"score_15m":s15m,
            "combined":combined,"size_pct":pct,"entry_hour":now_utc.hour,
            "crossovers":analysis.get("crossovers",[]),"regime":current_regime}
        last_trade_time[product_id]=time.time(); daily_trades_count+=1
        cx=analysis.get("crossovers",[])
        logger.info(f"BUY {product_id} @ ${price} | {round(pct*100,1)}% | "
                   f"TP1={round(tp1,4)} TP2={round(tp2,4)} SL={round(sl,4)} | "
                   f"D={s_d} 4H={s4h} 1H={s1h} 15M={s15m} COMBO={combined} | "
                   f"Regime:{current_regime} | CX:{cx}")
        notify(f"🟢 BUY: {product_id}",
            f"Trade opened!\n\nPair:{product_id}\nEntry:${price}\nSize:{round(pct*100,1)}%\n"
            f"TP1:${round(tp1,4)} TP2:${round(tp2,4)} SL:${round(sl,4)}\n"
            f"D:{s_d} 4H:{s4h} 1H:{s1h} 15M:{s15m} Combined:{combined}/400\n"
            f"Market Regime:{current_regime}\nCrossover:{cx or 'none'}\nBalance:${round(balance,2)}")
        return {"status":"success","combined_score":combined,"regime":current_regime}
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
                "crossovers":trade.get("crossovers",[]),"regime":trade.get("regime","")})
            em="✅" if pnl>0 else "🔴"
            wr=round(winning_trades/total_trades*100,1) if total_trades>0 else 0
            notify(f"{em} CLOSE: {product_id}",
                f"Trade closed!\nPair:{product_id}\nEntry:${trade['entry']}\nExit:${price}\n"
                f"PNL:${round(pnl,2)} ({round(pct,2)}%)\nReason:{reason}\n"
                f"Daily PNL:${round(daily_pnl,2)}\nWin Rate:{wr}%\nRegime:{trade.get('regime','')}")
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
    global last_scan_results,last_scan_time
    time.sleep(120)
    while True:
        try:
            # Detect regime before scanning
            regime=detect_market_regime()
            min_d,min_4h,min_1h,min_15m,min_combo,max_t,size_mult=get_adaptive_thresholds()
            regime_desc=REGIME_SETTINGS.get(regime,{}).get("description","")
            logger.info(f"🔍 AUTO SCAN | Regime:{regime} | Thresholds: D≥{min_d} 4H≥{min_4h} 1H≥{min_1h} 15M≥{min_15m} C≥{min_combo} | Balance:${get_balance():.2f}")
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
            logger.info(f"🔍 SCAN COMPLETE | Regime:{regime} | Fired:{trades_fired} | Next in 1hr")
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
    blk,bmsg=is_blackout()
    min_d,min_4h,min_1h,min_15m,min_combo,max_t,size_mult=get_adaptive_thresholds()
    bt=backtest_results.get("overall",{})
    return jsonify({"version":"6.4","running":True,"open_trades":open_trades,
        "circuit_breaker":daily_loss_hit or weekly_loss_hit,
        "daily_start_bal":daily_start_bal,"current_bal":get_balance(),
        "daily_pnl":round(daily_pnl,2),"daily_trades":f"{daily_trades_count}/{max_t}",
        "losing_streak":losing_streak,"win_rate":f"{wr}%","total_trades":total_trades,
        "blackout":blk,"blackout_reason":bmsg,"last_scan":last_scan_time,
        "regime":current_regime,"regime_description":REGIME_SETTINGS.get(current_regime,{}).get("description",""),
        "backtest_win_rate":bt.get("win_rate","pending..."),"pair_stats":pair_stats,
        "adaptive_thresholds":{"daily":min_d,"4h":min_4h,"1h":min_1h,"15m":min_15m,"combined":min_combo,"size_mult":size_mult}})

@app.route("/regime")
def regime_route():
    return jsonify({"current_regime":current_regime,
        "description":REGIME_SETTINGS.get(current_regime,{}).get("description",""),
        "thresholds":REGIME_SETTINGS.get(current_regime,{}),
        "history":regime_history[-10:]})

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
    regime_desc=REGIME_SETTINGS.get(current_regime,{}).get("description","Unknown")
    regime_colors={"STRONG_BULL":"#3fb950","BULL":"#58a6ff","SIDEWAYS":"#d29922",
                   "BEAR":"#f85149","STRONG_BEAR":"#ff0000","VOLATILE":"#ff8c00"}
    rc=regime_colors.get(current_regime,"#8b949e")

    scan_rows=""
    if last_scan_results:
        for pid,r in last_scan_results.items():
            color="green" if r["ready"] else "#8b949e"
            cx=",".join(r["crossovers"]) if r["crossovers"] else "—"
            scan_rows+=(f"<tr><td>{pid}</td><td>${r['price']}</td>"
                       f"<td>{r['daily']}<small>/{min_d}</small></td>"
                       f"<td>{r['4h']}<small>/{min_4h}</small></td>"
                       f"<td>{r['1h']}<small>/{min_1h}</small></td>"
                       f"<td>{r['15m']}<small>/{min_15m}</small></td>"
                       f"<td>{r['combined']}<small>/{min_combo}</small></td>"
                       f"<td style='color:yellow'>{cx}</td>"
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
                 f"<td style='color:{'green' if r.get('go_live_ready') else 'orange'}'>{'✅ READY' if r.get('go_live_ready') else '⚠️'}</td></tr>")

    rows=""
    for t in reversed(trade_history[-15:]):
        c="green" if t["pnl"]>0 else "red"
        cx=",".join(t.get("crossovers",[])) or "—"
        rows+=(f"<tr><td>{t['time'][:16]}</td><td>{t['pair']}</td>"
               f"<td>${t['entry']}</td><td>${t['exit']}</td>"
               f"<td style='color:{c}'>${t['pnl']} ({t['pnl_pct']}%)</td>"
               f"<td>{t['reason']}</td><td>{t.get('regime','')}</td><td>{cx}</td></tr>")

    pair_rows=""
    for pair,stats in pair_stats.items():
        total=stats["wins"]+stats["losses"]
        if total>0:
            pwr=round(stats["wins"]/total*100,1); pc="green" if stats["pnl"]>0 else "red"
            pair_rows+=(f"<tr><td>{pair}</td><td>{stats['wins']}W/{stats['losses']}L</td>"
                       f"<td>{pwr}%</td><td style='color:{pc}'>${round(stats['pnl'],2)}</td></tr>")

    go_live_color="green" if bt_ready else "orange"
    go_live_text="✅ READY FOR LIVE TRADING" if bt_ready else "⏳ Backtest pending or needs optimization"

    return f"""<!DOCTYPE html><html><head><title>J's Bot v6.4</title>
    <meta http-equiv="refresh" content="60">
    <style>
    body{{font-family:Arial;background:#0d1117;color:#e6edf3;padding:20px;font-size:14px}}
    h1,h2{{color:#58a6ff}}h2{{margin-top:25px;font-size:16px}}
    .card{{background:#161b22;border-radius:8px;padding:12px;margin:6px;display:inline-block;min-width:130px}}
    .card h3{{margin:0;color:#8b949e;font-size:11px}}.card p{{margin:4px 0 0;font-size:20px;font-weight:bold}}
    table{{width:100%;border-collapse:collapse;margin-top:10px;background:#161b22;border-radius:8px}}
    th,td{{padding:8px;text-align:left;border-bottom:1px solid #30363d;font-size:12px}}
    th{{color:#8b949e}}.green{{color:#3fb950}}.red{{color:#f85149}}.orange{{color:#d29922}}
    .banner{{border-radius:8px;padding:12px;margin:12px 0}}
    small{{color:#8b949e}}
    </style></head><body>
    <h1>🤖 J's Crypto Bot v6.4</h1>
    <p style="color:#8b949e">Refreshes every 60s | Autonomous 24/7 | Regime-Adaptive</p>

    <div class="banner" style="border-left:4px solid {go_live_color};background:#161b22">
    <strong style="color:{go_live_color}">GO LIVE: {go_live_text}</strong>
    {f"<br><small>WR:{bt.get('win_rate','—')} | Return:{bt.get('total_return','—')} | Pairs Ready:{bt.get('pairs_ready','—')}</small>" if bt else ""}
    </div>

    <div class="banner" style="border-left:4px solid {rc};background:#161b22">
    <strong style="color:{rc}">MARKET REGIME: {regime_desc}</strong>
    <br><small>Thresholds: Daily≥{min_d} | 4H≥{min_4h} | 1H≥{min_1h} | 15M≥{min_15m} | Combined≥{min_combo} | Size×{size_mult} | Max {max_t} trades/day</small>
    </div>

    <div>
    <div class="card"><h3>BALANCE</h3><p>${bal:.2f}</p></div>
    <div class="card"><h3>TODAY P&L</h3><p class="{'green' if profit>=0 else 'red'}">${profit:.2f}</p></div>
    <div class="card"><h3>WIN RATE</h3><p>{wr}%</p></div>
    <div class="card"><h3>TOTAL TRADES</h3><p>{total_trades}</p></div>
    <div class="card"><h3>OPEN TRADES</h3><p>{len(open_trades)}</p></div>
    <div class="card"><h3>DAILY TRADES</h3><p>{daily_trades_count}/{max_t}</p></div>
    <div class="card"><h3>BLACKOUT</h3><p class="{'red' if blk else 'green'}" style="font-size:12px">{'YES' if blk else 'NO'}</p></div>
    <div class="card"><h3>LOSING STREAK</h3><p class="{'red' if losing_streak>0 else 'green'}">{losing_streak}</p></div>
    </div>

    <h2>🔍 Last Scan {f"({last_scan_time[:16]})" if last_scan_time else "(pending...)"} — Regime: <span style="color:{rc}">{current_regime}</span></h2>
    <table><tr><th>Pair</th><th>Price</th><th>Daily</th><th>4H</th><th>1H</th><th>15M</th><th>Combined</th><th>Crossovers</th><th>Status</th></tr>
    {scan_rows if scan_rows else "<tr><td colspan='9' style='color:#8b949e;text-align:center'>Scanning...</td></tr>"}
    </table>

    <h2>🔬 Backtest (6 Months) {f"({last_backtest_time[:16]})" if last_backtest_time else "(running...)"}</h2>
    <table><tr><th>Pair</th><th>Trades</th><th>Win Rate</th><th>Return</th><th>Max DD</th><th>Go Live?</th></tr>
    {bt_rows if bt_rows else "<tr><td colspan='6' style='color:#8b949e;text-align:center'>Running... check back in 5 min</td></tr>"}
    </table>

    <h2>📊 Live Pair Performance</h2>
    <table><tr><th>Pair</th><th>Record</th><th>Win Rate</th><th>PNL</th></tr>
    {pair_rows if pair_rows else "<tr><td colspan='4' style='color:#8b949e;text-align:center'>No live trades yet</td></tr>"}
    </table>

    <h2>📋 Recent Trades</h2>
    <table><tr><th>Time</th><th>Pair</th><th>Entry</th><th>Exit</th><th>PNL</th><th>Reason</th><th>Regime</th><th>Crossovers</th></tr>
    {rows if rows else "<tr><td colspan='8' style='color:#8b949e;text-align:center'>No trades yet — scanning every hour</td></tr>"}
    </table>

    <p style="color:#8b949e;font-size:11px;margin-top:15px">
    <a href="/backtest" style="color:#58a6ff">/backtest</a> |
    <a href="/scan" style="color:#58a6ff">/scan</a> |
    <a href="/regime" style="color:#58a6ff">/regime</a> |
    <a href="/performance" style="color:#58a6ff">/performance</a>
    </p>
    </body></html>"""

@app.route("/performance")
def performance():
    return jsonify({"pair_stats":pair_stats,"total_trades":total_trades,
        "win_rate":f"{round(winning_trades/total_trades*100,1) if total_trades>0 else 0}%",
        "daily_pnl":round(daily_pnl,2),"losing_streak":losing_streak,
        "regime":current_regime,"last_scan":last_scan_time,
        "backtest":backtest_results.get("overall",{})})

if __name__=="__main__":
    daily_start_bal=get_balance(); weekly_start_bal=daily_start_bal
    logger.info(f"BOT v6.4 STARTED | Balance:${daily_start_bal} | "
               f"Email:{'ON' if EMAIL_ENABLED else 'OFF'} | "
               f"Telegram:{'ON' if TELEGRAM_ENABLED else 'OFF'} | "
               f"Mode:AUTONOMOUS + REGIME DETECTION")
    try:
        notify("🚀 Bot v6.4 Started",
            f"J's Crypto Bot v6.4 is live!\n\nBalance:${daily_start_bal:.2f}\n"
            f"Mode: Autonomous 24/7 + Regime Detection\n"
            f"Telegram: {'✅ Active' if TELEGRAM_ENABLED else '❌ Not configured'}\n"
            f"Dashboard: http://165.227.113.102/dashboard")
    except: pass
    threading.Thread(target=monitor_trades,daemon=True).start()
    threading.Thread(target=autonomous_scanner,daemon=True).start()
    threading.Thread(target=backtest_scheduler,daemon=True).start()
    threading.Thread(target=daily_summary_scheduler,daemon=True).start()
    app.run(host="0.0.0.0",port=80,debug=False)
