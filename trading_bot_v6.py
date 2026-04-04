"""
J's AI CRYPTO BOT v6.0 (CONSERVATIVE SCALPER)
Triple Timeframe | Session Filter | Correlation Filter | Email Alerts | Dashboard
"""
from flask import Flask, request, jsonify
from coinbase.rest import RESTClient
import json, uuid, logging, threading, time, smtplib, os
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

logging.basicConfig(filename='trading_log.txt', level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

with open("cdp_api_key-7.json") as f:
    keys = json.load(f)

client = RESTClient(api_key=keys["name"], api_secret=keys["privateKey"])

EMAIL_SENDER   = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER", "")
EMAIL_ENABLED  = all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER])

TAKE_PROFIT_1       = 0.010
TAKE_PROFIT_2       = 0.020
STOP_LOSS_PCT       = 0.010
TRAIL_PCT           = 0.008
BREAK_EVEN_TRIGGER  = 0.010
LOCK_PROFIT_PCT     = 0.005
MAX_OPEN_TRADES     = 3
DAILY_LOSS_LIMIT    = 0.06
WEEKLY_LOSS_LIMIT   = 0.12
MAX_TRADES_PER_DAY  = 4
TRADE_COOLDOWN_SEC  = 7200
MIN_4H_SCORE        = 72
MIN_1H_SCORE        = 68
MIN_15M_SCORE       = 65
MIN_COMBINED_SCORE  = 205
LOSING_STREAK_LIMIT = 2
MONITOR_INTERVAL    = 30

SESSIONS           = [(8,0,12,0),(20,0,24,0)]
BLACKOUT_HOURS_UTC = [14,18,19]
CORRELATED_GROUPS  = [{"BTC-USD","ETH-USD","SOL-USD"}]

ALLOWED_PAIRS = {
    "BTCUSD":"BTC-USD","ETHUSD":"ETH-USD","SOLUSD":"SOL-USD",
    "XRPUSD":"XRP-USD","WELLUSD":"WELL-USD",
}

open_trades={};last_trade_time={};daily_start_bal=None;weekly_start_bal=None
daily_loss_hit=False;weekly_loss_hit=False;losing_streak=0;total_trades=0
winning_trades=0;daily_trades_count=0;daily_pnl=0.0;trade_history=[]
last_daily_reset=datetime.utcnow().date();last_weekly_reset=datetime.utcnow().isocalendar()[1]

def send_email(subject,body):
    if not EMAIL_ENABLED:
        logger.info(f"EMAIL(disabled):{subject}");return
    try:
        msg=MIMEMultipart();msg["From"]=EMAIL_SENDER;msg["To"]=EMAIL_RECEIVER;msg["Subject"]=subject
        msg.attach(MIMEText(body,"plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
            s.login(EMAIL_SENDER,EMAIL_PASSWORD);s.send_message(msg)
        logger.info(f"Email sent:{subject}")
    except Exception as e:logger.error(f"Email error:{e}")

def get_balance():
    try:
        accounts=client.get_accounts(limit=250)
        for a in accounts["accounts"]:
            if a["currency"]=="USD":return float(a["available_balance"]["value"])
    except Exception as e:logger.error(f"Balance error:{e}")
    return 0

def get_price(product_id):
    try:
        t=client.get_best_bid_ask(product_ids=[product_id])
        return float(t["pricebooks"][0]["asks"][0]["price"])
    except Exception as e:logger.error(f"Price error {product_id}:{e}");return None

def get_candles(product_id,granularity="ONE_HOUR",limit=50):
    try:
        seconds={"FIFTEEN_MINUTE":900,"ONE_HOUR":3600,"FOUR_HOUR":14400}
        interval=seconds.get(granularity,3600)
        end=int(time.time());start=end-(interval*limit)
        candles=client.get_candles(product_id=product_id,start=start,end=end,granularity=granularity)
        return candles["candles"]
    except Exception as e:logger.error(f"Candle error {product_id} {granularity}:{e}");return None

def calculate_rsi(prices,period=14):
    if len(prices)<period+1:return 50
    gains,losses=[],[]
    for i in range(1,len(prices)):
        change=prices[i]-prices[i-1];gains.append(max(change,0));losses.append(abs(min(change,0)))
    ag=sum(gains[-period:])/period;al=sum(losses[-period:])/period
    if al==0:return 100
    return 100-(100/(1+ag/al))

def calculate_ema(prices,period=9):
    if len(prices)<period:return prices[-1]
    m=2/(period+1);ema=sum(prices[:period])/period
    for p in prices[period:]:ema=(p-ema)*m+ema
    return ema

def calculate_vwap(candles):
    try:
        tv,tpv=0,0
        for c in candles:
            h,l,cl,v=float(c["high"]),float(c["low"]),float(c["close"]),float(c["volume"])
            tpv+=((h+l+cl)/3)*v;tv+=v
        return tpv/tv if tv>0 else 0
    except:return 0

def detect_support(closes,lookback=20):
    if len(closes)<lookback:return False
    return abs(closes[-1]-min(closes[-lookback:-1]))/min(closes[-lookback:-1])<0.015

def detect_bullish_candle(candles):
    if len(candles)<2:return False
    try:
        p,c=candles[-2],candles[-1]
        po,pc=float(p["open"]),float(p["close"])
        co,cc,cl,ch=float(c["open"]),float(c["close"]),float(c["low"]),float(c["high"])
        engulfing=pc<po and cc>co and cc>po and co<pc
        body=abs(cc-co);lw=min(co,cc)-cl;uw=ch-max(co,cc)
        hammer=lw>body*2 and uw<body and cc>co
        return engulfing or hammer
    except:return False

def check_volatility(closes,min_range=0.005):
    if len(closes)<10:return True
    r=closes[-10:]
    return (max(r)-min(r))/min(r)>=min_range

def is_trading_session():
    now=datetime.now(timezone.utc);et_hour=(now.hour-4)%24;et_min=now.minute
    for sh,sm,eh,em in SESSIONS:
        if sh*60+sm<=et_hour*60+et_min<eh*60+em:
            return True,f"{et_hour:02d}:{et_min:02d} ET in session"
    return False,f"{et_hour:02d}:{et_min:02d} ET outside session"

def is_blackout():
    return datetime.now(timezone.utc).hour in BLACKOUT_HOURS_UTC

def correlation_check(product_id):
    for group in CORRELATED_GROUPS:
        if product_id in group:
            open_in_group=[p for p in open_trades if p in group]
            if open_in_group:return False,f"correlated {open_in_group[0]} open"
    return True,"ok"

def check_circuit_breakers():
    global daily_loss_hit,weekly_loss_hit
    try:
        bal=get_balance()
        if daily_start_bal and bal<daily_start_bal*(1-DAILY_LOSS_LIMIT):
            daily_loss_hit=True
            send_email("🚨 Daily Loss Limit Hit",f"Balance:${bal:.2f} Start:${daily_start_bal:.2f}")
            return True,"daily loss limit"
        if weekly_start_bal and bal<weekly_start_bal*(1-WEEKLY_LOSS_LIMIT):
            weekly_loss_hit=True
            send_email("🚨 Weekly Loss Limit Hit",f"Balance:${bal:.2f} Start:${weekly_start_bal:.2f}")
            return True,"weekly loss limit"
    except Exception as e:logger.error(f"CB error:{e}")
    return False,"ok"

def get_size_pct(combined):
    if losing_streak>=LOSING_STREAK_LIMIT:return 0.015
    if combined>=270:return 0.05
    elif combined>=250:return 0.035
    elif combined>=225:return 0.025
    else:return 0.015             
def score_timeframe(product_id,granularity,price,label):
    score=0;reason=[]
    candles=get_candles(product_id,granularity=granularity,limit=50)
    if not candles:return 0,[f"{label}:no data"]
    cc=list(reversed(candles))
    closes=[float(c["close"]) for c in cc];volumes=[float(c["volume"]) for c in cc]
    rsi=calculate_rsi(closes)
    if 38<=rsi<=62:score+=20;reason.append(f"RSI={round(rsi,1)} ideal")
    elif 30<=rsi<=70:score+=10;reason.append(f"RSI={round(rsi,1)} ok")
    else:reason.append(f"RSI={round(rsi,1)} poor")
    avg_vol=sum(volumes[:-1])/max(len(volumes)-1,1);vr=volumes[-1]/avg_vol if avg_vol>0 else 0
    if vr>=1.2:score+=15;reason.append(f"Vol={round(vr,2)}x strong")
    elif vr>=0.8:score+=8;reason.append(f"Vol={round(vr,2)}x ok")
    else:reason.append(f"Vol={round(vr,2)}x weak")
    e9=calculate_ema(closes,9);e21=calculate_ema(closes,21);e55=calculate_ema(closes,55)
    if price>e9>e21>e55:score+=20;reason.append("EMAs perfect")
    elif price>e9 and e9>e21:score+=12;reason.append("EMAs partial")
    else:reason.append("EMAs not aligned")
    if len(closes)>=5:
        mom=(closes[-1]-closes[-5])/closes[-5]*100
        if mom>=1.0:score+=15;reason.append(f"Mom={round(mom,2)}% strong")
        elif mom>=0:score+=8;reason.append(f"Mom={round(mom,2)}% mild")
        else:reason.append(f"Mom={round(mom,2)}% negative")
    vwap=calculate_vwap(cc[-20:])
    if vwap>0 and price>vwap:score+=10;reason.append("Above VWAP")
    else:reason.append("Below VWAP")
    if detect_support(closes):score+=10;reason.append("Near support")
    else:reason.append("No support")
    if detect_bullish_candle(cc[-3:]):score+=10;reason.append("Bullish candle")
    else:reason.append("No pattern")
    logger.info(f"{label} {product_id}:{score}/100|{' | '.join(reason)}")
    return score,reason

def triple_tf_score(product_id,price):
    s4h,_=score_timeframe(product_id,"FOUR_HOUR",price,"4H")
    s1h,_=score_timeframe(product_id,"ONE_HOUR",price,"1H")
    s15m,_=score_timeframe(product_id,"FIFTEEN_MINUTE",price,"15M")
    combined=s4h+s1h+s15m
    logger.info(f"TRIPLE TF {product_id}: 4H={s4h} 1H={s1h} 15M={s15m} COMBINED={combined}/300")
    return s4h,s1h,s15m,combined

def check_daily_reset():
    global daily_start_bal,daily_loss_hit,daily_trades_count,daily_pnl,last_daily_reset
    global weekly_start_bal,weekly_loss_hit,last_weekly_reset
    today=datetime.utcnow().date()
    if today!=last_daily_reset:
        daily_start_bal=get_balance();daily_loss_hit=False
        daily_trades_count=0;daily_pnl=0.0;last_daily_reset=today
        send_daily_summary()
    week=datetime.utcnow().isocalendar()[1]
    if week!=last_weekly_reset:
        weekly_start_bal=get_balance();weekly_loss_hit=False;last_weekly_reset=week

def send_daily_summary():
    bal=get_balance();wr=round(winning_trades/total_trades*100,1) if total_trades>0 else 0
    send_email("☀️ Daily Bot Summary",
        f"Good morning J!\n\nBalance:${bal:.2f}\nTotal Trades:{total_trades}\nWin Rate:{wr}%\nDaily PNL:${daily_pnl:.2f}\nOpen Trades:{len(open_trades)}\n\nSessions today:\n• 8am-12pm ET\n• 8pm-12am ET\n\n- Your Bot 🤖")

def smart_filter(product_id,price):
    now=time.time()
    if price<=0:return False,"invalid price"
    if product_id in open_trades:return False,"already in trade"
    if len(open_trades)>=MAX_OPEN_TRADES:return False,"max trades open"
    if daily_trades_count>=MAX_TRADES_PER_DAY:return False,"max daily trades"
    if product_id in last_trade_time:
        if now-last_trade_time[product_id]<TRADE_COOLDOWN_SEC:
            mins=int((TRADE_COOLDOWN_SEC-(now-last_trade_time[product_id]))/60)
            return False,f"cooldown {mins}min"
    in_s,smsg=is_trading_session()
    if not in_s:return False,f"outside session ({smsg})"
    if is_blackout():return False,"blackout window"
    ok,cmsg=correlation_check(product_id)
    if not ok:return False,cmsg
    return True,"ok"

def execute_buy(product_id):
    global daily_trades_count
    if daily_loss_hit:return {"status":"skip","reason":"daily circuit breaker"}
    if weekly_loss_hit:return {"status":"skip","reason":"weekly circuit breaker"}
    tripped,reason=check_circuit_breakers()
    if tripped:return {"status":"skip","reason":reason}
    check_daily_reset()
    price=get_price(product_id)
    if not price:return {"status":"error","reason":"no price"}
    allowed,reason=smart_filter(product_id,price)
    if not allowed:
        logger.info(f"FILTERED {product_id}:{reason}");return {"status":"skip","reason":reason}
    c1h=get_candles(product_id,"ONE_HOUR",15)
    if c1h:
        cl=[float(c["close"]) for c in reversed(c1h)]
        if not check_volatility(cl):return {"status":"skip","reason":"market too flat"}
    s4h,s1h,s15m,combined=triple_tf_score(product_id,price)
    if s4h<MIN_4H_SCORE:return {"status":"skip","reason":f"4H={s4h} need {MIN_4H_SCORE}"}
    if s1h<MIN_1H_SCORE:return {"status":"skip","reason":f"1H={s1h} need {MIN_1H_SCORE}"}
    if s15m<MIN_15M_SCORE:return {"status":"skip","reason":f"15M={s15m} need {MIN_15M_SCORE}"}
    if combined<MIN_COMBINED_SCORE:return {"status":"skip","reason":f"combined={combined} need {MIN_COMBINED_SCORE}"}
    balance=get_balance();pct=get_size_pct(combined)
    size=round((balance*pct)/price,6)
    try:
        client.market_order_buy(client_order_id=str(uuid.uuid4()),product_id=product_id,quote_size=str(round(size*price,2)))
        tp1=price*(1+TAKE_PROFIT_1);tp2=price*(1+TAKE_PROFIT_2);sl=price*(1-STOP_LOSS_PCT)
        open_trades[product_id]={"entry":price,"size":size,"size_remaining":size,"trail_high":price,
            "stop":sl,"take_profit_1":tp1,"take_profit_2":tp2,"tp1_hit":False,
            "time":datetime.utcnow().isoformat(),"score_4h":s4h,"score_1h":s1h,"score_15m":s15m,
            "combined":combined,"size_pct":pct}
        last_trade_time[product_id]=time.time();daily_trades_count+=1
        logger.info(f"BUY {product_id} @ ${price} | {round(pct*100,1)}% | TP1={round(tp1,4)} TP2={round(tp2,4)} SL={round(sl,4)} | 4H={s4h} 1H={s1h} 15M={s15m} COMBO={combined}")
        send_email(f"🟢 BUY: {product_id}",f"Trade opened!\n\nPair:{product_id}\nEntry:${price}\nSize:{round(pct*100,1)}%\nTP1:${round(tp1,4)}\nTP2:${round(tp2,4)}\nSL:${round(sl,4)}\n4H:{s4h} 1H:{s1h} 15M:{s15m}\nCombined:{combined}/300\nBalance:${round(balance,2)}")
        return {"status":"success","combined_score":combined}
    except Exception as e:
        logger.error(f"BUY ERROR {product_id}:{e}");return {"status":"error","reason":str(e)}

def close_trade(product_id,price,reason="signal",partial=False,partial_size=None):
    global losing_streak,winning_trades,total_trades,daily_pnl
    trade=open_trades.get(product_id)
    if not trade:return
    stc=partial_size if partial else trade["size_remaining"]
    try:
        client.market_order_sell(client_order_id=str(uuid.uuid4()),product_id=product_id,base_size=str(round(stc,6)))
        pnl=(price-trade["entry"])*stc;pct=((price-trade["entry"])/trade["entry"])*100;daily_pnl+=pnl
        logger.info(f"{'PARTIAL ' if partial else ''}CLOSE {product_id} @ ${price} | {reason} | PNL=${round(pnl,2)} ({round(pct,2)}%)")
        if not partial:
            total_trades+=1
            if pnl>0:winning_trades+=1;losing_streak=0
            else:
                losing_streak+=1
                if losing_streak>=LOSING_STREAK_LIMIT:logger.warning(f"LOSING STREAK {losing_streak}")
            trade_history.append({"pair":product_id,"entry":trade["entry"],"exit":price,"pnl":round(pnl,2),
                "pnl_pct":round(pct,2),"reason":reason,"time":datetime.utcnow().isoformat(),
                "scores":f"4H={trade['score_4h']} 1H={trade['score_1h']} 15M={trade['score_15m']}"})
            em="✅" if pnl>0 else "🔴"
            wr=round(winning_trades/total_trades*100,1) if total_trades>0 else 0
            send_email(f"{em} CLOSE: {product_id}",f"Trade closed!\n\nPair:{product_id}\nEntry:${trade['entry']}\nExit:${price}\nPNL:${round(pnl,2)} ({round(pct,2)}%)\nReason:{reason}\nDaily PNL:${round(daily_pnl,2)}\nWin Rate:{wr}%")
            del open_trades[product_id]
        else:trade["size_remaining"]-=stc
    except Exception as e:logger.error(f"CLOSE ERROR {product_id}:{e}")

def monitor_trades():
    while True:
        try:
            for product_id,trade in list(open_trades.items()):
                price=get_price(product_id)
                if not price:continue
                entry=trade["entry"];pp=(price-entry)/entry
                if pp>=BREAK_EVEN_TRIGGER and trade["stop"]<entry:
                    trade["stop"]=entry;logger.info(f"BREAK EVEN {product_id}")
                if pp>=0.015 and trade["stop"]<entry*(1+LOCK_PROFIT_PCT):
                    trade["stop"]=entry*(1+LOCK_PROFIT_PCT);logger.info(f"PROFIT LOCKED {product_id}")
                if price>trade["trail_high"]:
                    trade["trail_high"]=price;ns=price*(1-TRAIL_PCT)
                    if ns>trade["stop"]:trade["stop"]=ns;logger.info(f"TRAIL {product_id} stop={round(ns,4)}")
                if not trade["tp1_hit"] and price>=trade["take_profit_1"]:
                    half=round(trade["size"]*0.5,6)
                    close_trade(product_id,price,reason="take_profit_1",partial=True,partial_size=half)
                    trade["tp1_hit"]=True;trade["stop"]=entry
                elif trade["tp1_hit"] and price>=trade["take_profit_2"]:
                    close_trade(product_id,price,reason="take_profit_2")
                elif price<=trade["stop"]:
                    close_trade(product_id,price,reason="stop_loss")
        except Exception as e:logger.error(f"Monitor error:{e}")
        time.sleep(MONITOR_INTERVAL)

def daily_summary_scheduler():
    while True:
        now=datetime.now(timezone.utc);et_hour=(now.hour-4)%24
        if et_hour==8 and now.minute==0:send_daily_summary();time.sleep(61)
        time.sleep(30)    
@app.route("/webhook",methods=["POST"])
def webhook():
    try:
        data=request.get_json(force=True);action=data.get("action","").lower()
        symbol=data.get("symbol","").upper().replace("/","");product_id=ALLOWED_PAIRS.get(symbol)
        if not product_id:return jsonify({"status":"ignored","reason":"pair not allowed"})
        if action=="buy":return jsonify(execute_buy(product_id))
        elif action in ["sell","close"]:
            price=get_price(product_id) or float(data.get("price",0))
            close_trade(product_id,price,reason="signal");return jsonify({"status":"closed"})
        return jsonify({"status":"no action"})
    except Exception as e:
        logger.error(f"Webhook error:{e}");return jsonify({"status":"error","reason":str(e)})

@app.route("/status")
def status():
    wr=round(winning_trades/total_trades*100,1) if total_trades>0 else 0
    in_s,smsg=is_trading_session()
    return jsonify({"version":"6.0","running":True,"open_trades":open_trades,
        "circuit_breaker":daily_loss_hit or weekly_loss_hit,"daily_start_bal":daily_start_bal,
        "current_bal":get_balance(),"daily_pnl":round(daily_pnl,2),
        "daily_trades":f"{daily_trades_count}/{MAX_TRADES_PER_DAY}","losing_streak":losing_streak,
        "win_rate":f"{wr}%","total_trades":total_trades,"in_session":in_s,"session_status":smsg,
        "settings":{"min_4h_score":MIN_4H_SCORE,"min_1h_score":MIN_1H_SCORE,"min_15m_score":MIN_15M_SCORE,
            "combined_min":MIN_COMBINED_SCORE,"take_profit_1":"1.0%","take_profit_2":"2.0%",
            "stop_loss":"1.0%","trailing_stop":"0.8%","session_filter":"8am-12pm & 8pm-12am ET"}})

@app.route("/dashboard")
def dashboard():
    wr=round(winning_trades/total_trades*100,1) if total_trades>0 else 0
    bal=get_balance();profit=round(bal-(daily_start_bal or bal),2)
    recent=trade_history[-20:] if trade_history else []
    rows=""
    for t in reversed(recent):
        c="green" if t["pnl"]>0 else "red"
        rows+=f"<tr><td>{t['time'][:16]}</td><td>{t['pair']}</td><td>${t['entry']}</td><td>${t['exit']}</td><td style='color:{c}'>${t['pnl']} ({t['pnl_pct']}%)</td><td>{t['reason']}</td><td>{t['scores']}</td></tr>"
    in_s,smsg=is_trading_session();sc="green" if in_s else "orange"
    return f"""<!DOCTYPE html><html><head><title>J's Bot v6</title><meta http-equiv="refresh" content="60">
    <style>body{{font-family:Arial;background:#0d1117;color:#e6edf3;padding:20px}}h1{{color:#58a6ff}}
    .card{{background:#161b22;border-radius:8px;padding:15px;margin:10px;display:inline-block;min-width:150px}}
    .card h3{{margin:0;color:#8b949e;font-size:12px}}.card p{{margin:5px 0 0;font-size:24px;font-weight:bold}}
    table{{width:100%;border-collapse:collapse;margin-top:20px;background:#161b22;border-radius:8px}}
    th,td{{padding:10px;text-align:left;border-bottom:1px solid #30363d;font-size:13px}}th{{color:#8b949e}}
    .green{{color:#3fb950}}.red{{color:#f85149}}.orange{{color:#d29922}}</style></head><body>
    <h1>🤖 J's Crypto Bot v6.0</h1><p style="color:#8b949e">Refreshes every 60s</p>
    <div><div class="card"><h3>BALANCE</h3><p>${bal:.2f}</p></div>
    <div class="card"><h3>TODAY P&L</h3><p class="{'green' if profit>=0 else 'red'}">${profit:.2f}</p></div>
    <div class="card"><h3>WIN RATE</h3><p>{wr}%</p></div>
    <div class="card"><h3>TOTAL TRADES</h3><p>{total_trades}</p></div>
    <div class="card"><h3>OPEN TRADES</h3><p>{len(open_trades)}</p></div>
    <div class="card"><h3>SESSION</h3><p class="{sc}" style="font-size:14px">{smsg}</p></div>
    <div class="card"><h3>LOSING STREAK</h3><p class="{'red' if losing_streak>0 else 'green'}">{losing_streak}</p></div></div>
    <h2 style="margin-top:30px">Recent Trades</h2>
    <table><tr><th>Time</th><th>Pair</th><th>Entry</th><th>Exit</th><th>PNL</th><th>Reason</th><th>Scores</th></tr>
    {rows if rows else "<tr><td colspan='7' style='color:#8b949e;text-align:center'>No trades yet</td></tr>"}
    </table></body></html>"""

@app.route("/scan")
def scan():
    results={}
    for symbol,product_id in ALLOWED_PAIRS.items():
        price=get_price(product_id)
        if price:
            s4h,s1h,s15m,combo=triple_tf_score(product_id,price)
            results[product_id]={"price":price,"4H":s4h,"1H":s1h,"15M":s15m,"combined":combo,"ready":combo>=MIN_COMBINED_SCORE}
    return jsonify(results)

if __name__=="__main__":
    daily_start_bal=get_balance();weekly_start_bal=daily_start_bal
    logger.info(f"BOT v6.0 STARTED | Balance:${daily_start_bal} | Email:{'ON' if EMAIL_ENABLED else 'OFF'}")
    send_email("🚀 Bot v6.0 Started",f"J's Crypto Bot v6.0 is running!\n\nBalance:${daily_start_bal:.2f}\nMode:Paper Trading\nSessions:8am-12pm & 8pm-12am ET\nMin Score:{MIN_COMBINED_SCORE}/300\n\nDashboard:http://165.227.113.102/dashboard")
    threading.Thread(target=monitor_trades,daemon=True).start()
    threading.Thread(target=daily_summary_scheduler,daemon=True).start()
    app.run(host="0.0.0.0",port=80,debug=False)
