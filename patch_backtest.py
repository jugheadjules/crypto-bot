"""
FULL SCORING BACKTEST PATCH for v6.6
Replaces the simplified EMA crossover backtest with the FULL bot scoring logic:
- Daily score (RSI, EMA21/50, volume trend, higher highs/lows, momentum)
- 4H score (RSI, volume, EMA alignment, crossover, momentum, VWAP, support, candle)
- 1H score (same 8 checks)
- 15M score (same 8 checks)
- 200 EMA trend filter
- Volume delta check
- ATR volatility filter
- RSI 1H trigger
- Regime-adaptive thresholds
"""

NEW_BACKTEST = '''
def run_backtest(product_id, days=180, starting_balance=22.0):
    """
    Full scoring backtest - uses same logic as live bot
    Pulls multi-timeframe data and scores exactly like analyze_pair()
    """
    logger.info(f"BACKTEST {product_id}: pulling {days} days full scoring...")

    # Pull all timeframes upfront
    candles_4h  = get_historical_candles(product_id, granularity="FOUR_HOUR",    days_back=days)
    candles_1d  = get_historical_candles(product_id, granularity="ONE_DAY",      days_back=days+30)
    candles_1h  = get_historical_candles(product_id, granularity="ONE_HOUR",     days_back=min(days,60))
    candles_15m = get_historical_candles(product_id, granularity="FIFTEEN_MINUTE",days_back=min(days,14))

    if len(candles_4h) < 50:
        return {"error": "not enough 4H data", "pair": product_id}
    if len(candles_1d) < 20:
        return {"error": "not enough daily data", "pair": product_id}

    logger.info(f"BACKTEST {product_id}: 4H={len(candles_4h)} 1D={len(candles_1d)} 1H={len(candles_1h)} 15M={len(candles_15m)}")

    balance = starting_balance
    trades = []; wins = 0; losses = 0
    max_balance = balance; min_balance = balance
    in_trade = False; entry = 0; entry_time = 0
    tp1 = 0; tp2 = 0; sl = 0
    tp1_hit = False; size = 0; size_rem = 0
    cooldown_bars = 0; last_trade_bar = -20

    def score_candles_bt(candles_window, price):
        """Score a window of candles using same logic as live bot"""
        if len(candles_window) < 10:
            return 0, False
        closes  = [get_candle_val(c, "close")  for c in candles_window]
        volumes = [get_candle_val(c, "volume") for c in candles_window]
        score = 0

        # RSI
        rsi = calculate_rsi(closes)
        if 38 <= rsi <= 65:   score += 20
        elif 30 <= rsi <= 72: score += 10

        # Volume
        avg_vol = sum(volumes[:-1]) / max(len(volumes)-1, 1)
        vr = volumes[-1] / avg_vol if avg_vol > 0 else 0
        if vr >= 1.5:   score += 20
        elif vr >= 1.0: score += 12
        elif vr >= 0.7: score += 6

        # EMA alignment
        e9  = calculate_ema(closes, 9)
        e21 = calculate_ema(closes, 21)
        e55 = calculate_ema(closes, min(55, len(closes)-1)) if len(closes) > 55 else e21
        if price > e9 > e21 > e55: score += 25
        elif price > e9 > e21:     score += 15

        # EMA crossover bonus
        crossover = detect_ema_crossover(closes)
        if crossover: score += 10

        # Momentum
        if len(closes) >= 5:
            mom = (closes[-1] - closes[-5]) / closes[-5] * 100
            if mom >= 1.5:   score += 15
            elif mom >= 0.5: score += 8
            elif mom >= 0:   score += 4

        # VWAP
        vwap = calculate_vwap(candles_window[-20:])
        if vwap > 0 and price > vwap: score += 10

        # Near support
        if len(closes) >= 20:
            mn = min(closes[-20:-1])
            if mn > 0 and abs(closes[-1] - mn) / mn < 0.015: score += 10

        # Bullish candle
        if detect_bullish_candle(candles_window[-3:]): score += 5

        return score, crossover

    def score_daily_bt(candles_window, price):
        """Daily timeframe scoring"""
        if len(candles_window) < 10:
            return 0
        closes = [get_candle_val(c, "close")  for c in candles_window]
        vols   = [get_candle_val(c, "volume") for c in candles_window]
        score  = 0

        rsi = calculate_rsi(closes)
        if 50 <= rsi <= 72:  score += 25
        elif 45 <= rsi < 50: score += 10

        e21 = calculate_ema(closes, 21)
        e50 = calculate_ema(closes, min(50, len(closes)-1))
        if price > e21 and e21 > e50: score += 25
        elif price > e21:             score += 10

        avg_v = sum(vols[:-5]) / max(len(vols)-5, 1)
        rec_v = sum(vols[-5:]) / 5
        if rec_v > avg_v * 1.1:   score += 20
        elif rec_v > avg_v * 0.8: score += 10

        if detect_higher_highs_lows(closes): score += 15

        if len(closes) >= 5:
            mom = (closes[-1] - closes[-5]) / closes[-5] * 100
            if mom >= 2.0:  score += 15
            elif mom >= 0:  score += 7

        return score

    # Main backtest loop - iterate over 4H candles
    for i in range(60, len(candles_4h)):
        if cooldown_bars > 0:
            cooldown_bars -= 1

        current_candle = candles_4h[i]
        ts    = int(get_candle_val(current_candle, "start"))
        price = get_candle_val(current_candle, "close")
        if price <= 0: continue

        # Update trade management
        if in_trade:
            if not tp1_hit and price >= tp1:
                balance += (tp1 - entry) * (size * 0.5)
                size_rem = size * 0.5
                tp1_hit = True

            elif tp1_hit and price >= tp2:
                balance += (price - entry) * size_rem
                wins += 1
                trades.append({"entry": entry, "exit": price,
                    "pnl": round((price-entry)/entry*100, 2), "result": "win", "reason": "TP2"})
                in_trade = False; tp1_hit = False; cooldown_bars = 6
                max_balance = max(max_balance, balance)

            elif price <= sl:
                balance += (price - entry) * size_rem
                losses += 1
                trades.append({"entry": entry, "exit": price,
                    "pnl": round((price-entry)/entry*100, 2), "result": "loss", "reason": "SL"})
                in_trade = False; tp1_hit = False; cooldown_bars = 6
                min_balance = min(min_balance, balance)

            elif (ts - entry_time) > TIME_EXIT_HOURS * 3600 and price < entry:
                balance += (price - entry) * size_rem
                losses += 1
                trades.append({"entry": entry, "exit": price,
                    "pnl": round((price-entry)/entry*100, 2), "result": "loss", "reason": "time_exit"})
                in_trade = False; tp1_hit = False; cooldown_bars = 6
                min_balance = min(min_balance, balance)
            continue

        # Skip if cooldown active
        if cooldown_bars > 0: continue
        if i - last_trade_bar < 6: continue

        # ── FULL SCORING ENGINE ──────────────────────────────────────────────

        # 1. Daily score
        daily_window = [c for c in candles_1d if int(get_candle_val(c,"start")) <= ts][-30:]
        s_d = score_daily_bt(daily_window, price) if len(daily_window) >= 10 else 40

        # 2. 4H score
        w4h = candles_4h[max(0, i-59):i+1]
        s4h, cx4h = score_candles_bt(w4h, price)

        # 3. 1H score
        w1h = [c for c in candles_1h if int(get_candle_val(c,"start")) <= ts][-60:]
        s1h, cx1h = score_candles_bt(w1h, price) if len(w1h) >= 10 else (30, False)

        # 4. 15M score
        w15m = [c for c in candles_15m if int(get_candle_val(c,"start")) <= ts][-60:]
        s15m, cx15m = score_candles_bt(w15m, price) if len(w15m) >= 10 else (25, False)

        combined = s_d + s4h + s1h + s15m

        # 5. 200 EMA trend filter
        closes_4h = [get_candle_val(c,"close") for c in w4h]
        e200 = calculate_ema(closes_4h, min(200, len(closes_4h))) if len(closes_4h) >= 50 else 0
        above_200 = price > e200 if e200 > 0 else True

        # 6. Volume delta
        vd_score, _, vd_pct = calculate_volume_delta(w4h)

        # 7. ATR volatility
        atr = calculate_atr(w4h[-15:]) if len(w4h) >= 15 else 0
        atr_pct = (atr / price) * 100 if price > 0 else 0
        atr_ok = atr_pct >= 1.2

        # 8. RSI 1H trigger
        rsi_1h = calculate_rsi([get_candle_val(c,"close") for c in w1h]) if len(w1h) >= 15 else 50
        rsi_ok = rsi_1h > 45

        # ── ENTRY DECISION (matches live bot logic) ─────────────────────────
        # Use SIDEWAYS thresholds (most conservative) for realistic backtest
        min_d = 50; min_4h = 50; min_1h = 45; min_15m = 38; min_combo = 183
        vol_delta_min = -8

        entry_signal = (
            above_200 and
            s_d   >= min_d    and
            s4h   >= min_4h   and
            s1h   >= min_1h   and
            s15m  >= min_15m  and
            combined >= min_combo and
            vd_pct >= vol_delta_min and
            atr_ok and
            rsi_ok
        )

        if not entry_signal: continue

        # ── POSITION SIZING ─────────────────────────────────────────────────
        sl_price = price * (1 - STOP_LOSS_PCT)
        stop_dist = STOP_LOSS_PCT
        risk_amt = balance * RISK_PER_TRADE
        position_usd = min(risk_amt / stop_dist, balance * MAX_POSITION_PCT)
        position_usd = max(position_usd, 1.0)

        size = position_usd / price
        size_rem = size
        entry = price; entry_time = ts
        tp1 = entry * (1 + TAKE_PROFIT_1)
        tp2 = entry * (1 + TAKE_PROFIT_2)
        sl  = sl_price
        in_trade = True; tp1_hit = False
        last_trade_bar = i

        logger.info(f"BT ENTRY {product_id} @ ${round(price,4)} | D={s_d} 4H={s4h} 1H={s1h} 15M={s15m} C={combined} | 200EMA={'✅' if above_200 else '❌'} VD={round(vd_pct,1)}%")

    # Close any open trade at end
    if in_trade and len(candles_4h) > 0:
        final = get_candle_val(candles_4h[-1], "close")
        pnl = (final - entry) * size_rem
        balance += pnl
        if pnl > 0: wins += 1
        else: losses += 1
        trades.append({"entry": entry, "exit": final,
            "pnl": round((final-entry)/entry*100, 2),
            "result": "win" if pnl > 0 else "loss", "reason": "end"})

    total = wins + losses
    win_rate  = round(wins / total * 100, 1) if total > 0 else 0
    total_ret = round((balance - starting_balance) / starting_balance * 100, 1)
    max_dd    = round((max_balance - min_balance) / max_balance * 100, 1) if max_balance > 0 else 0
    avg_pnl   = round(sum(t["pnl"] for t in trades) / len(trades), 2) if trades else 0

    result = {
        "pair": product_id, "days": days, "total_trades": total,
        "wins": wins, "losses": losses,
        "win_rate": f"{win_rate}%", "total_return": f"{total_ret}%",
        "final_balance": round(balance, 2), "max_drawdown": f"{max_dd}%",
        "avg_trade_pnl": f"{avg_pnl}%", "start_balance": starting_balance,
        "go_live_ready": win_rate >= 52 and max_dd <= 15 and total >= 10,
        "scoring": "FULL ENGINE"
    }
    logger.info(f"BACKTEST {product_id}: {total} trades WR:{win_rate}% Return:{total_ret}% DD:{max_dd}% {'✅' if result['go_live_ready'] else '❌'}")
    return result
'''

# Read the current bot file
with open('/root/crypto-bot/trading_bot_v66.py', 'r') as f:
    code = f.read()

# Find and replace the old backtest function
import re

# Find the start of run_backtest
start_marker = 'def run_backtest(product_id,days=180,starting_balance=66.7):'
end_marker = 'def run_full_backtest():'

start_idx = code.find(start_marker)
end_idx = code.find(end_marker)

if start_idx == -1 or end_idx == -1:
    print(f"ERROR: Could not find markers. start={start_idx} end={end_idx}")
else:
    new_code = code[:start_idx] + NEW_BACKTEST + '\n' + code[end_idx:]
    with open('/root/crypto-bot/trading_bot_v66.py', 'w') as f:
        f.write(new_code)
    print("✅ Full scoring backtest successfully installed!")
    print(f"Old function replaced at position {start_idx}")
    print(f"New function length: {len(NEW_BACKTEST)} chars")
