"""Backtest EMA Credit Spread strategy on historical 1D candles.

Logic (mirrors strategy/ema_credit_spread.py):
- Each day: if close < EMA14 → bear call spread, else → bull put spread
- Spread sold at 20Δ, hedge bought at 10Δ
- TP: 90% of net premium | SL: 100% of net premium
- Hold up to 8 bars (simulating ~8 days to expiry)

Option premium is approximated using ATR-based model:
- 20Δ option premium ≈ 0.04 * ATR(14) (further OTM = lower premium)
- 10Δ option premium ≈ 0.015 * ATR(14)
- Net credit = sell_premium - buy_premium

P&L simulation:
- If price moves against the spread past the sold strike, loss accrues
- TP/SL checked daily based on spread value change approximation
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.chart import get_candles
from datetime import datetime

ASSETS = ['BTC', 'ETH', 'NIFTY', 'BANKNIFTY', 'SENSEX']
EMA_PERIOD = 14
ATR_PERIOD = 14
TP_PCT = 0.90
SL_PCT = 1.00
MAX_HOLD = 8  # bars
SELL_DELTA_DIST = 1.0   # sold strike distance = 1.0 * ATR from spot
BUY_DELTA_DIST = 1.8    # bought strike distance = 1.8 * ATR from spot
SELL_PREM_MULT = 0.040  # premium as fraction of spot for 20Δ
BUY_PREM_MULT = 0.015   # premium as fraction of spot for 10Δ


def ema(closes, period):
    k = 2 / (period + 1)
    e = [closes[0]]
    for v in closes[1:]:
        e.append(v * k + e[-1] * (1 - k))
    return e


def atr(candles, period=14):
    atrs = [0.0] * len(candles)
    trs = [candles[0]['h'] - candles[0]['l']]
    for i in range(1, len(candles)):
        tr = max(candles[i]['h'] - candles[i]['l'],
                 abs(candles[i]['h'] - candles[i - 1]['c']),
                 abs(candles[i]['l'] - candles[i - 1]['c']))
        trs.append(tr)
    if len(trs) >= period:
        atrs[period - 1] = sum(trs[:period]) / period
        for i in range(period, len(candles)):
            atrs[i] = (atrs[i - 1] * (period - 1) + trs[i]) / period
    return atrs


def backtest_asset(asset, candles):
    if not candles or len(candles) < EMA_PERIOD + ATR_PERIOD + MAX_HOLD + 10:
        return None

    closes = [c['c'] for c in candles]
    emas = ema(closes, EMA_PERIOD)
    atrs = atr(candles, ATR_PERIOD)

    trades = []
    i = max(EMA_PERIOD, ATR_PERIOD)

    while i < len(candles) - MAX_HOLD:
        price = closes[i]
        ema_val = emas[i]
        cur_atr = atrs[i]
        if cur_atr <= 0:
            i += 1
            continue

        bearish = price < ema_val

        # Estimate premiums as fraction of spot
        sell_prem = price * SELL_PREM_MULT
        buy_prem = price * BUY_PREM_MULT
        net_credit = sell_prem - buy_prem

        # Strike distances from spot
        if bearish:
            # Bear call spread: sell call at spot + 1*ATR, buy call at spot + 1.8*ATR
            sell_strike = price + SELL_DELTA_DIST * cur_atr
            buy_strike = price + BUY_DELTA_DIST * cur_atr
        else:
            # Bull put spread: sell put at spot - 1*ATR, buy put at spot - 1.8*ATR
            sell_strike = price - SELL_DELTA_DIST * cur_atr
            buy_strike = price - BUY_DELTA_DIST * cur_atr

        spread_width = abs(sell_strike - buy_strike)
        max_loss = spread_width - net_credit if spread_width > net_credit else net_credit

        tp_target = net_credit * TP_PCT
        sl_target = net_credit * SL_PCT

        # Simulate over next MAX_HOLD bars
        exit_reason = 'expiry'
        exit_pnl = 0
        exit_bar = i + MAX_HOLD

        for j in range(i + 1, min(i + MAX_HOLD + 1, len(candles))):
            future_price = closes[j]

            # Approximate spread P&L based on intrinsic value at current price
            if bearish:
                # Bear call spread: profit if price stays below sell_strike
                intrinsic_sold = max(0, future_price - sell_strike)
                intrinsic_bought = max(0, future_price - buy_strike)
                spread_value = intrinsic_sold - intrinsic_bought
            else:
                # Bull put spread: profit if price stays above sell_strike
                intrinsic_sold = max(0, sell_strike - future_price)
                intrinsic_bought = max(0, buy_strike - future_price)
                spread_value = intrinsic_sold - intrinsic_bought

            # P&L = credit received - current spread value
            current_pnl = net_credit - spread_value

            # Time decay benefit: as days pass, extrinsic value decays
            days_held = j - i
            decay_factor = 1 - (days_held / MAX_HOLD) * 0.6  # 60% theta decay over hold
            # If not in the money, PnL improves with time
            if spread_value == 0:
                current_pnl = net_credit * (1 - decay_factor * 0.1)  # Nearly full credit kept

            if current_pnl >= tp_target:
                exit_reason = 'tp'
                exit_pnl = tp_target
                exit_bar = j
                break
            elif current_pnl <= -sl_target:
                exit_reason = 'sl'
                exit_pnl = -sl_target
                exit_bar = j
                break
        else:
            # At expiry, calculate final P&L
            final_price = closes[min(i + MAX_HOLD, len(candles) - 1)]
            if bearish:
                intrinsic_sold = max(0, final_price - sell_strike)
                intrinsic_bought = max(0, final_price - buy_strike)
                spread_value = intrinsic_sold - intrinsic_bought
            else:
                intrinsic_sold = max(0, sell_strike - final_price)
                intrinsic_bought = max(0, buy_strike - final_price)
                spread_value = intrinsic_sold - intrinsic_bought
            exit_pnl = net_credit - spread_value

        trades.append({
            'entry_idx': i,
            'date': datetime.utcfromtimestamp(candles[i]['t']).strftime('%Y-%m-%d') if candles[i]['t'] > 1e9 else str(candles[i]['t']),
            'direction': 'bear_call' if bearish else 'bull_put',
            'price': price,
            'ema': round(ema_val, 2),
            'atr': round(cur_atr, 2),
            'net_credit': round(net_credit, 4),
            'pnl': round(exit_pnl, 4),
            'exit_reason': exit_reason,
            'bars_held': exit_bar - i,
        })

        # Skip to after this trade exits
        i = exit_bar + 1

    return trades


def print_results(asset, trades):
    if not trades:
        print(f"  {asset}: No trades")
        return

    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    total_pnl = sum(t['pnl'] for t in trades)
    avg_pnl = total_pnl / len(trades)
    wr = len(wins) / len(trades) * 100

    bear_trades = [t for t in trades if t['direction'] == 'bear_call']
    bull_trades = [t for t in trades if t['direction'] == 'bull_put']
    bear_wr = len([t for t in bear_trades if t['pnl'] > 0]) / len(bear_trades) * 100 if bear_trades else 0
    bull_wr = len([t for t in bull_trades if t['pnl'] > 0]) / len(bull_trades) * 100 if bull_trades else 0

    tp_exits = len([t for t in trades if t['exit_reason'] == 'tp'])
    sl_exits = len([t for t in trades if t['exit_reason'] == 'sl'])
    exp_exits = len([t for t in trades if t['exit_reason'] == 'expiry'])

    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0

    print(f"\n  {'─' * 60}")
    print(f"  {asset} | {len(trades)} trades | WR: {wr:.1f}% | PnL: ${total_pnl:+,.2f}")
    print(f"  {'─' * 60}")
    print(f"  Bear Call: {len(bear_trades)} trades ({bear_wr:.1f}% WR) | Bull Put: {len(bull_trades)} trades ({bull_wr:.1f}% WR)")
    print(f"  Exits → TP: {tp_exits} | SL: {sl_exits} | Expiry: {exp_exits}")
    print(f"  Avg Win: ${avg_win:+,.2f} | Avg Loss: ${avg_loss:+,.2f} | Avg Trade: ${avg_pnl:+,.2f}")

    # Cumulative P&L curve stats
    cum = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cum += t['pnl']
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
    print(f"  Max Drawdown: ${max_dd:,.2f} | Final Cumulative: ${cum:+,.2f}")

    return {'asset': asset, 'trades': len(trades), 'wr': wr, 'pnl': total_pnl,
            'bear_wr': bear_wr, 'bull_wr': bull_wr, 'max_dd': max_dd}


# ─── Main ───

print("=" * 70)
print("  EMA CREDIT SPREAD BACKTEST")
print(f"  EMA{EMA_PERIOD} | Sell 20Δ / Buy 10Δ | TP: {TP_PCT*100:.0f}% | SL: {SL_PCT*100:.0f}% | Max hold: {MAX_HOLD}d")
print("=" * 70)

all_results = []
for asset in ASSETS:
    print(f"\n  Fetching {asset} 1D candles...")
    candles = get_candles(asset, '1d')
    if not candles:
        print(f"  ✗ No data for {asset}")
        continue
    print(f"  ✓ {len(candles)} candles ({datetime.utcfromtimestamp(candles[0]['t']).strftime('%Y-%m-%d')} → {datetime.utcfromtimestamp(candles[-1]['t']).strftime('%Y-%m-%d')})")

    trades = backtest_asset(asset, candles)
    result = print_results(asset, trades)
    if result:
        all_results.append(result)

# Summary
if all_results:
    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print(f"{'=' * 70}")
    print(f"  {'Asset':<12} {'Trades':<8} {'WR%':<8} {'Bear WR':<10} {'Bull WR':<10} {'PnL':<14} {'MaxDD':<12}")
    print(f"  {'─' * 68}")
    for r in all_results:
        print(f"  {r['asset']:<12} {r['trades']:<8} {r['wr']:<8.1f} {r['bear_wr']:<10.1f} {r['bull_wr']:<10.1f} ${r['pnl']:<+13,.2f} ${r['max_dd']:<11,.2f}")

    total_trades = sum(r['trades'] for r in all_results)
    avg_wr = sum(r['wr'] for r in all_results) / len(all_results)
    total_pnl = sum(r['pnl'] for r in all_results)
    print(f"  {'─' * 68}")
    print(f"  {'TOTAL':<12} {total_trades:<8} {avg_wr:<8.1f} {'':10} {'':10} ${total_pnl:<+13,.2f}")
