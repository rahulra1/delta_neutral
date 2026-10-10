"""Backtest EMA Credit Spread V2 strategy on historical 1D candles.

Logic (mirrors strategy/ema_credit_spread_v2.py):
- Each day: if close < EMA14 → bear call spread, else → bull put spread
- Spread sold at 20Δ, hedge bought at 10Δ
- Two-tier partial exits (percentages are of the FULL net credit, P):
    • TP1 (50%)  → position at +0.50·P → close HALF, book +0.25·P
    • TP2 (90%)  → remaining half at +0.90·P → close rest, book +0.45·P  (best ≈ +0.70·P)
    • SL1 (90%)  → position at -0.90·P → close HALF, book -0.45·P
    • SL2 (150%) → remaining half at -1.50·P → close rest, book -0.75·P  (worst ≈ -1.20·P)
- Hold up to 8 bars (simulating ~8 days to expiry)

Premium / P&L approximation is identical to the single-exit backtest; only the
exit ladder differs. Each tranche is half the position, so a tranche closing at
X% of full credit books 0.5 * X% * net_credit.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.chart import get_candles
from datetime import datetime

ASSETS = ['BTC', 'ETH', 'NIFTY', 'BANKNIFTY', 'SENSEX']
EMA_PERIOD = 14
ATR_PERIOD = 14
TP1_PCT = 0.50
TP2_PCT = 0.90
SL1_PCT = 0.90
SL2_PCT = 1.50
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
            sell_strike = price + SELL_DELTA_DIST * cur_atr
            buy_strike = price + BUY_DELTA_DIST * cur_atr
        else:
            sell_strike = price - SELL_DELTA_DIST * cur_atr
            buy_strike = price - BUY_DELTA_DIST * cur_atr

        # Two-tier thresholds (full-credit levels)
        tp1 = net_credit * TP1_PCT
        tp2 = net_credit * TP2_PCT
        sl1 = net_credit * SL1_PCT
        sl2 = net_credit * SL2_PCT

        # Two-tranche state. Each tranche carries half the position; the PnL it
        # books is half of whatever the full-position PnL is at the moment it
        # closes.
        tier1_done = False
        realized = 0.0          # dollars booked from tranche 1
        exit_reason = 'expiry'
        exit_bar = i + MAX_HOLD

        def full_pnl_at(future_price):
            if bearish:
                intrinsic_sold = max(0, future_price - sell_strike)
                intrinsic_bought = max(0, future_price - buy_strike)
            else:
                intrinsic_sold = max(0, sell_strike - future_price)
                intrinsic_bought = max(0, buy_strike - future_price)
            spread_value = intrinsic_sold - intrinsic_bought
            return net_credit - spread_value, spread_value

        for j in range(i + 1, min(i + MAX_HOLD + 1, len(candles))):
            future_price = closes[j]
            current_pnl, spread_value = full_pnl_at(future_price)

            # Time decay benefit (same model as single-exit backtest)
            days_held = j - i
            decay_factor = 1 - (days_held / MAX_HOLD) * 0.6
            if spread_value == 0:
                current_pnl = net_credit * (1 - decay_factor * 0.1)

            if not tier1_done:
                if current_pnl >= tp1:
                    realized = current_pnl * 0.5     # book half the position
                    tier1_done = True
                    continue
                if current_pnl <= -sl1:
                    realized = current_pnl * 0.5
                    tier1_done = True
                    continue
                continue

            # Tier 2 — after EITHER tier-1 event the remaining half exits on TP2
            # (upside) or SL2 (downside). `current_pnl` is full-position PnL; the
            # half's dollar contribution is current_pnl * 0.5. TP2/SL2 compared on
            # the full-credit scale map 1:1 to "% of the half's basis".
            if current_pnl >= tp2:
                exit_reason = 'tp2'
                exit_bar = j
                realized += current_pnl * 0.5
                break
            if current_pnl <= -sl2:
                exit_reason = 'sl2'
                exit_bar = j
                realized += current_pnl * 0.5
                break
        else:
            # At expiry, settle whatever is still open.
            final_price = closes[min(i + MAX_HOLD, len(candles) - 1)]
            final_pnl, _ = full_pnl_at(final_price)
            if tier1_done:
                realized += final_pnl * 0.5          # only the remaining half left
                exit_reason = 'expiry_t2'
            else:
                realized = final_pnl                 # full position still on
                exit_reason = 'expiry'

        exit_pnl = realized

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
            'tier1_hit': tier1_done,
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

    tp2_exits = len([t for t in trades if t['exit_reason'] == 'tp2'])
    sl2_exits = len([t for t in trades if t['exit_reason'] == 'sl2'])
    exp_exits = len([t for t in trades if t['exit_reason'].startswith('expiry')])
    tier1_hits = len([t for t in trades if t.get('tier1_hit')])

    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0

    print(f"\n  {'─' * 60}")
    print(f"  {asset} | {len(trades)} trades | WR: {wr:.1f}% | PnL: ${total_pnl:+,.2f}")
    print(f"  {'─' * 60}")
    print(f"  Bear Call: {len(bear_trades)} trades ({bear_wr:.1f}% WR) | Bull Put: {len(bull_trades)} trades ({bull_wr:.1f}% WR)")
    print(f"  Tier-1 partial exits hit: {tier1_hits}/{len(trades)}")
    print(f"  Exits → TP2: {tp2_exits} | SL2: {sl2_exits} | Expiry: {exp_exits}")
    print(f"  Avg Win: ${avg_win:+,.2f} | Avg Loss: ${avg_loss:+,.2f} | Avg Trade: ${avg_pnl:+,.2f}")

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

if __name__ == '__main__':
    print("=" * 70)
    print("  EMA CREDIT SPREAD V2 BACKTEST (two-tier TP1/TP2/SL1/SL2)")
    print(f"  EMA{EMA_PERIOD} | Sell 20Δ / Buy 10Δ | "
          f"TP1 {TP1_PCT*100:.0f}% / TP2 {TP2_PCT*100:.0f}% | "
          f"SL1 {SL1_PCT*100:.0f}% / SL2 {SL2_PCT*100:.0f}% | Max hold: {MAX_HOLD}d")
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
