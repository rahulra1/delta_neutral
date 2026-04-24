"""Backtest EMA+Trendline Breakout — baseline vs filtered versions."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.chart import get_candles, calc_ema_trendline_breakout

ASSETS = ['BTC', 'ETH', 'NIFTY', 'BANKNIFTY', 'SENSEX']
TFS = ['15m', '1h']


def forward_test(candles, signals, max_bars=50):
    time_idx = {c['t']: i for i, c in enumerate(candles)}
    wins = losses = 0
    total_pnl = 0
    for s in signals:
        idx = time_idx.get(s['time'])
        if idx is None:
            continue
        entry, sl, tp = s['price'], s['sl'], s['tp1']
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        hit = None
        for c in candles[idx + 1:idx + 1 + max_bars]:
            if s['type'] == 'buy':
                if c['l'] <= sl: hit = 'sl'; break
                if c['h'] >= tp: hit = 'tp'; break
            else:
                if c['h'] >= sl: hit = 'sl'; break
                if c['l'] <= tp: hit = 'tp'; break
        if hit == 'tp':
            wins += 1; total_pnl += abs(tp - entry)
        elif hit == 'sl':
            losses += 1; total_pnl -= risk
    total = wins + losses
    if total == 0:
        return None
    return {'total': total, 'wins': wins, 'losses': losses, 'wr': round(wins / total * 100, 1), 'pnl': round(total_pnl, 2)}


def ema_list(data, p):
    k = 2 / (p + 1)
    e = [data[0]]
    for v in data[1:]:
        e.append(v * k + e[-1] * (1 - k))
    return e


def gen_filtered(candles, lb=5, min_rr=2, vol_filter=False, vol_mult=1.0, min_touches=2, rr_min=2):
    """Regenerate signals with filters applied."""
    ema_period = 200
    if len(candles) < ema_period + lb * 3:
        return []

    closes = [c['c'] for c in candles]
    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    vols = [c.get('v', 0) for c in candles]
    ema = ema_list(closes, ema_period)

    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        start = max(0, i - 19)
        w = vols[start:i + 1]
        avg_vol[i] = sum(w) / len(w) if w else 1

    # Swing points
    swing_highs, swing_lows = [], []
    for i in range(lb, len(candles) - lb):
        if highs[i] == max(highs[i - lb:i + lb + 1]):
            swing_highs.append(i)
        if lows[i] == min(lows[i - lb:i + lb + 1]):
            swing_lows.append(i)

    signals = []

    def find_sigs(pivots, direction):
        sigs = []
        for j in range(1, len(pivots)):
            i1, i2 = pivots[j - 1], pivots[j]
            if i2 - i1 < lb or i2 >= len(candles) - 2:
                continue

            if direction == 'long':
                p1, p2 = highs[i1], highs[i2]
                if p2 >= p1:
                    continue
                slope = (p2 - p1) / (i2 - i1)
                # Count touches on trendline
                touches = 0
                for t in range(i1, i2 + 1):
                    tl_val = p1 + slope * (t - i1)
                    if abs(highs[t] - tl_val) / tl_val < 0.003:
                        touches += 1
                if touches < min_touches:
                    continue

                for k in range(i2 + 1, min(i2 + lb * 3, len(candles) - 1)):
                    tl_val = p2 + slope * (k - i2)
                    if candles[k]['c'] > tl_val and closes[k] > ema[k]:
                        # Volume filter
                        if vol_filter and avg_vol[k] > 0 and vols[k] < avg_vol[k] * vol_mult:
                            break
                        sl = candles[k]['l']
                        risk = candles[k]['c'] - sl
                        if risk <= 0:
                            break
                        tp = candles[k]['c'] + risk * rr_min
                        sigs.append({'time': candles[k]['t'], 'type': 'buy', 'price': candles[k]['c'], 'sl': sl, 'tp1': tp, 'touches': touches})
                        break
            else:
                p1, p2 = lows[i1], lows[i2]
                if p2 <= p1:
                    continue
                slope = (p2 - p1) / (i2 - i1)
                touches = 0
                for t in range(i1, i2 + 1):
                    tl_val = p1 + slope * (t - i1)
                    if abs(lows[t] - tl_val) / tl_val < 0.003:
                        touches += 1
                if touches < min_touches:
                    continue

                for k in range(i2 + 1, min(i2 + lb * 3, len(candles) - 1)):
                    tl_val = p2 + slope * (k - i2)
                    if candles[k]['c'] < tl_val and closes[k] < ema[k]:
                        if vol_filter and avg_vol[k] > 0 and vols[k] < avg_vol[k] * vol_mult:
                            break
                        sl = candles[k]['h']
                        risk = sl - candles[k]['c']
                        if risk <= 0:
                            break
                        tp = candles[k]['c'] - risk * rr_min
                        sigs.append({'time': candles[k]['t'], 'type': 'sell', 'price': candles[k]['c'], 'sl': sl, 'tp1': tp, 'touches': touches})
                        break
        return sigs

    signals += find_sigs(swing_highs, 'long')
    signals += find_sigs(swing_lows, 'short')
    signals.sort(key=lambda s: s['time'])
    return signals


CONFIGS = [
    ('Baseline', {}),
    ('Vol > 1.0x', {'vol_filter': True, 'vol_mult': 1.0}),
    ('Vol > 1.5x', {'vol_filter': True, 'vol_mult': 1.5}),
    ('3+ touches', {'min_touches': 3}),
    ('3+ touches + Vol>1x', {'min_touches': 3, 'vol_filter': True, 'vol_mult': 1.0}),
    ('RR 1:3', {'rr_min': 3}),
    ('RR 1:3 + Vol>1x', {'rr_min': 3, 'vol_filter': True, 'vol_mult': 1.0}),
    ('3touch + Vol>1x + RR3', {'min_touches': 3, 'vol_filter': True, 'vol_mult': 1.0, 'rr_min': 3}),
]

print("=" * 100)
print("EMA + TRENDLINE BREAKOUT — FILTER COMPARISON")
print("=" * 100)

all_results = []
for asset in ASSETS:
    for tf in TFS:
        candles = get_candles(asset, tf)
        if not candles or len(candles) < 210:
            continue
        for name, cfg in CONFIGS:
            sigs = gen_filtered(candles, **cfg)
            bt = forward_test(candles, sigs)
            if bt and bt['total'] >= 3:
                all_results.append({'asset': asset, 'tf': tf, 'filter': name, **bt})

print(f"\n{'Filter':<28} {'Asset':<10} {'TF':<5} {'Trades':<8} {'Wins':<6} {'Loss':<6} {'WR%':<8} {'PnL':<14}")
print("-" * 100)
for name, _ in CONFIGS:
    rows = [r for r in all_results if r['filter'] == name]
    for r in sorted(rows, key=lambda x: -x['wr']):
        print(f"{r['filter']:<28} {r['asset']:<10} {r['tf']:<5} {r['total']:<8} {r['wins']:<6} {r['losses']:<6} {r['wr']:<8} {r['pnl']:<+14.2f}")
    if rows:
        avg_wr = sum(r['wr'] for r in rows) / len(rows)
        total_pnl = sum(r['pnl'] for r in rows)
        profitable = sum(1 for r in rows if r['pnl'] > 0)
        print(f"  {'>>> AVG':<26} {'':10} {'':5} {'':8} {'':6} {'':6} {avg_wr:<8.1f} {total_pnl:<+14.2f}  ({profitable}/{len(rows)} profitable)")
    print()

print("=" * 100)
print("SUMMARY")
print("=" * 100)
for name, _ in CONFIGS:
    rows = [r for r in all_results if r['filter'] == name]
    if rows:
        avg_wr = sum(r['wr'] for r in rows) / len(rows)
        total_pnl = sum(r['pnl'] for r in rows)
        profitable = sum(1 for r in rows if r['pnl'] > 0)
        print(f"  {name:<30} AvgWR: {avg_wr:5.1f}% | TotalPnL: {total_pnl:>+12.2f} | Profitable: {profitable}/{len(rows)}")
