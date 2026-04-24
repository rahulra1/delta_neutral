"""Backtest Box Theory v2 — refined: first touch per zone per day + candle confirmation."""
import sys, os, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.chart import get_candles

ASSETS = ['NIFTY', 'BANKNIFTY', 'SENSEX', 'BTC', 'ETH']
TIMEFRAMES = ['15m', '1h', '1d']


def calc_box_theory_v2(candles):
    """Refined Box Theory: one signal per zone per day, candle rejection required."""
    if not candles or len(candles) < 2:
        return []

    days = {}
    for c in candles:
        d = datetime.datetime.utcfromtimestamp(c['t']).date()
        days.setdefault(d, []).append(c)

    sorted_days = sorted(days.keys())
    if len(sorted_days) < 2:
        return []

    signals = []
    for di in range(1, len(sorted_days)):
        prev = days[sorted_days[di - 1]]
        prev_high = max(c['h'] for c in prev)
        prev_low = min(c['l'] for c in prev)
        box_range = prev_high - prev_low
        if box_range <= 0:
            continue
        mid = (prev_high + prev_low) / 2
        sell_zone = prev_high - box_range * 0.20
        buy_zone = prev_low + box_range * 0.20

        today = days[sorted_days[di]]
        buy_triggered = False
        sell_triggered = False

        for i, c in enumerate(today):
            # Buy: price enters bottom zone + candle closes with rejection (close > open = bullish)
            if not buy_triggered and c['l'] <= buy_zone and c['c'] > c['o'] and c['c'] > buy_zone:
                signals.append({
                    'time': c['t'], 'type': 'buy', 'price': c['c'],
                    'sl': prev_low, 'tp1': mid, 'tp2': sell_zone,
                    'box_high': prev_high, 'box_low': prev_low, 'box_mid': mid,
                })
                buy_triggered = True

            # Sell: price enters top zone + candle closes with rejection (close < open = bearish)
            if not sell_triggered and c['h'] >= sell_zone and c['c'] < c['o'] and c['c'] < sell_zone:
                signals.append({
                    'time': c['t'], 'type': 'sell', 'price': c['c'],
                    'sl': prev_high, 'tp1': mid, 'tp2': buy_zone,
                    'box_high': prev_high, 'box_low': prev_low, 'box_mid': mid,
                })
                sell_triggered = True

    return signals


def forward_test(candles, signals, max_bars=50):
    """Walk forward to check TP1, TP2, or SL hit."""
    if not signals or not candles:
        return None

    time_idx = {}
    for i, c in enumerate(candles):
        time_idx[c['t']] = i

    wins_tp1 = wins_tp2 = losses = 0
    total_pnl = 0
    trades = []

    for s in signals:
        idx = time_idx.get(s['time'])
        if idx is None:
            continue
        entry = s['price']
        sl = s['sl']
        tp1 = s['tp1']
        tp2 = s.get('tp2', tp1)
        risk = abs(entry - sl)
        if risk <= 0:
            continue

        hit = None
        exit_price = entry
        for c in candles[idx + 1: idx + 1 + max_bars]:
            if s['type'] == 'buy':
                if c['l'] <= sl:
                    hit = 'sl'; exit_price = sl; break
                if c['h'] >= tp1:
                    hit = 'tp1'; exit_price = tp1; break
            else:
                if c['h'] >= sl:
                    hit = 'sl'; exit_price = sl; break
                if c['l'] <= tp1:
                    hit = 'tp1'; exit_price = tp1; break

        if hit == 'tp1':
            wins_tp1 += 1
            reward = abs(tp1 - entry)
            total_pnl += reward
            trades.append({'type': s['type'], 'result': 'win', 'rr': round(reward / risk, 2), 'pnl': round(reward, 2)})
        elif hit == 'sl':
            losses += 1
            total_pnl -= risk
            trades.append({'type': s['type'], 'result': 'loss', 'rr': -1, 'pnl': round(-risk, 2)})
        # else: no hit, skip

    total = wins_tp1 + losses
    if total == 0:
        return None

    buy_trades = [t for t in trades if t['type'] == 'buy']
    sell_trades = [t for t in trades if t['type'] == 'sell']
    buy_wins = sum(1 for t in buy_trades if t['result'] == 'win')
    sell_wins = sum(1 for t in sell_trades if t['result'] == 'win')

    return {
        'total': total,
        'wins': wins_tp1,
        'losses': losses,
        'win_rate': round(wins_tp1 / total * 100, 1),
        'pnl_points': round(total_pnl, 2),
        'avg_win': round(sum(t['pnl'] for t in trades if t['result'] == 'win') / wins_tp1, 2) if wins_tp1 else 0,
        'avg_loss': round(sum(t['pnl'] for t in trades if t['result'] == 'loss') / losses, 2) if losses else 0,
        'buy_total': len(buy_trades),
        'buy_wr': round(buy_wins / len(buy_trades) * 100, 1) if buy_trades else 0,
        'sell_total': len(sell_trades),
        'sell_wr': round(sell_wins / len(sell_trades) * 100, 1) if sell_trades else 0,
    }


print("=" * 90)
print("BOX THEORY v2 BACKTEST — Refined (1st touch + candle rejection)")
print("=" * 90)

results = []

for asset in ASSETS:
    for tf in TIMEFRAMES:
        print(f"\n▶ {asset} / {tf} ...", end=" ", flush=True)
        candles = get_candles(asset, tf)
        if not candles:
            print("❌ No data"); continue
        print(f"({len(candles)} candles)", end=" ", flush=True)

        signals = calc_box_theory_v2(candles)
        if not signals:
            print("— 0 signals"); continue

        bt = forward_test(candles, signals)
        if not bt:
            print(f"— {len(signals)} signals, no completions"); continue

        print(f"✅ {bt['total']} trades | WR: {bt['win_rate']}% | PnL: {bt['pnl_points']:+.2f}")
        results.append({'asset': asset, 'tf': tf, 'candles': len(candles), 'signals': len(signals), **bt})

print("\n\n" + "=" * 90)
print(f"{'Asset':<12} {'TF':<6} {'Signals':<9} {'Trades':<8} {'Wins':<6} {'Loss':<6} {'Win%':<8} {'BuyWR%':<9} {'SellWR%':<9} {'AvgWin':<10} {'AvgLoss':<10} {'PnL':<12}")
print("-" * 90)

for r in sorted(results, key=lambda x: (-x['win_rate'], -x['pnl_points'])):
    print(f"{r['asset']:<12} {r['tf']:<6} {r['signals']:<9} {r['total']:<8} {r['wins']:<6} {r['losses']:<6} {r['win_rate']:<8} {r['buy_wr']:<9} {r['sell_wr']:<9} {r.get('avg_win',0):<10.2f} {r.get('avg_loss',0):<10.2f} {r['pnl_points']:<+12.2f}")

print("\n" + "=" * 90)
print("TOP PERFORMERS")
print("=" * 90)
if results:
    # Filter for meaningful sample size (>= 10 trades)
    viable = [r for r in results if r['total'] >= 10]
    if viable:
        best_wr = max(viable, key=lambda x: x['win_rate'])
        best_pnl = max(viable, key=lambda x: x['pnl_points'])
        print(f"🏆 Highest Win Rate (≥10 trades):  {best_wr['asset']} / {best_wr['tf']} — {best_wr['win_rate']}% WR | {best_wr['total']} trades | PnL: {best_wr['pnl_points']:+.2f}")
        print(f"💰 Best PnL (≥10 trades):           {best_pnl['asset']} / {best_pnl['tf']} — PnL: {best_pnl['pnl_points']:+.2f} | {best_pnl['win_rate']}% WR | {best_pnl['total']} trades")

        # Best per asset
        print(f"\n{'Asset':<12} {'Best TF':<8} {'Win%':<8} {'Trades':<8} {'PnL':<12}")
        print("-" * 50)
        for asset in ASSETS:
            asset_results = [r for r in viable if r['asset'] == asset]
            if asset_results:
                best = max(asset_results, key=lambda x: x['win_rate'])
                print(f"{best['asset']:<12} {best['tf']:<8} {best['win_rate']:<8} {best['total']:<8} {best['pnl_points']:<+12.2f}")
    else:
        print("Not enough trades (need ≥10) for reliable results.")
