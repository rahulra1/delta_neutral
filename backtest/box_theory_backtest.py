"""Backtest Box Theory across all assets and timeframes."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.chart import get_candles, calc_box_theory

ASSETS = ['NIFTY', 'BANKNIFTY', 'SENSEX', 'BTC', 'ETH']
TIMEFRAMES = ['15m', '1h', '1d']

def backtest_signals(signals):
    if not signals:
        return None
    wins = losses = 0
    total_rr = 0
    pnl = 0
    for s in signals:
        entry = s['price']
        sl = s['sl']
        tp = s['tp1']
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk <= 0:
            continue
        rr = reward / risk
        # Simulate: assume TP hit ~70% of time (box theory claim), SL hit 30%
        # But let's use actual price data — check if tp or sl was closer to entry direction
        # Since we don't have forward candles in signal data, use the R:R ratio
        # and assume the signal direction is correct (mean reversion to mid)
        if s['type'] == 'buy':
            # TP is above entry (mid), SL is below (box low)
            hit_tp = tp > entry and sl < entry
        else:
            # TP is below entry (mid), SL is above (box high)
            hit_tp = tp < entry and sl > entry

        if hit_tp and rr > 0:
            wins += 1
            pnl += reward
            total_rr += rr
        else:
            losses += 1
            pnl -= risk

    total = wins + losses
    if total == 0:
        return None
    return {
        'total': total,
        'wins': wins,
        'losses': losses,
        'win_rate': round(wins / total * 100, 1),
        'avg_rr': round(total_rr / wins, 2) if wins else 0,
        'pnl_points': round(pnl, 2),
    }

def run_forward_backtest(candles, signals):
    """Forward-test signals against actual candle data."""
    if not signals or not candles:
        return None

    # Build time->index map for candles
    time_idx = {c['t']: i for i, c in enumerate(candles)}

    wins = losses = 0
    total_pnl = 0

    for s in signals:
        entry = s['price']
        sl = s['sl']
        tp = s['tp1']
        risk = abs(entry - sl)
        if risk <= 0:
            continue

        sig_time = s['time']
        start_idx = time_idx.get(sig_time)
        if start_idx is None:
            # Find nearest candle after signal
            start_idx = next((i for i, c in enumerate(candles) if c['t'] >= sig_time), None)
        if start_idx is None:
            continue

        # Walk forward through candles to see if TP or SL hit first
        hit = None
        for c in candles[start_idx + 1:start_idx + 50]:  # max 50 bars lookahead
            if s['type'] == 'buy':
                if c['l'] <= sl:
                    hit = 'sl'; break
                if c['h'] >= tp:
                    hit = 'tp'; break
            else:  # sell
                if c['h'] >= sl:
                    hit = 'sl'; break
                if c['l'] <= tp:
                    hit = 'tp'; break

        if hit == 'tp':
            wins += 1
            total_pnl += abs(tp - entry)
        elif hit == 'sl':
            losses += 1
            total_pnl -= risk
        # else: no hit within 50 bars, skip

    total = wins + losses
    if total == 0:
        return None
    return {
        'total': total,
        'wins': wins,
        'losses': losses,
        'win_rate': round(wins / total * 100, 1),
        'avg_reward': round(total_pnl / wins, 2) if wins else 0,
        'pnl_points': round(total_pnl, 2),
    }

print("=" * 80)
print("BOX THEORY BACKTEST — All Assets × All Timeframes")
print("=" * 80)

results = []

for asset in ASSETS:
    for tf in TIMEFRAMES:
        print(f"\n▶ {asset} / {tf} ...", end=" ", flush=True)
        candles = get_candles(asset, tf)
        if not candles:
            print("❌ No data")
            continue
        print(f"({len(candles)} candles)", end=" ", flush=True)

        data = calc_box_theory(candles)
        signals = data.get('signals', [])
        if not signals:
            print("— 0 signals")
            continue

        buys = [s for s in signals if s['type'] == 'buy']
        sells = [s for s in signals if s['type'] == 'sell']

        bt = run_forward_backtest(candles, signals)
        if not bt:
            print(f"— {len(signals)} signals, no completions")
            continue

        print(f"✅ {bt['total']} trades | WR: {bt['win_rate']}% | PnL: {bt['pnl_points']:+.2f}")
        results.append({
            'asset': asset, 'tf': tf,
            'candles': len(candles),
            'signals': len(signals),
            'buys': len(buys), 'sells': len(sells),
            **bt,
        })

print("\n" + "=" * 80)
print("RESULTS SUMMARY (sorted by win rate)")
print("=" * 80)
print(f"{'Asset':<12} {'TF':<6} {'Candles':<9} {'Signals':<9} {'Trades':<8} {'Wins':<6} {'Losses':<8} {'Win%':<8} {'PnL Pts':<12}")
print("-" * 80)

for r in sorted(results, key=lambda x: (-x['win_rate'], -x['pnl_points'])):
    print(f"{r['asset']:<12} {r['tf']:<6} {r['candles']:<9} {r['signals']:<9} {r['total']:<8} {r['wins']:<6} {r['losses']:<8} {r['win_rate']:<8} {r['pnl_points']:<+12.2f}")

print("\n" + "=" * 80)
print("BEST COMBINATIONS")
print("=" * 80)
if results:
    best_wr = max(results, key=lambda x: x['win_rate'])
    best_pnl = max(results, key=lambda x: x['pnl_points'])
    most_signals = max(results, key=lambda x: x['signals'])
    print(f"🏆 Highest Win Rate:  {best_wr['asset']} / {best_wr['tf']} — {best_wr['win_rate']}% ({best_wr['total']} trades)")
    print(f"💰 Best PnL:          {best_pnl['asset']} / {best_pnl['tf']} — {best_pnl['pnl_points']:+.2f} pts ({best_pnl['win_rate']}% WR)")
    print(f"📊 Most Signals:      {most_signals['asset']} / {most_signals['tf']} — {most_signals['signals']} signals")
else:
    print("No results to summarize.")
