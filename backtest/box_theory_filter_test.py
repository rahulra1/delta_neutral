"""Backtest Box Theory — filter comparison to improve results."""
import sys, os, datetime as _dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.chart import get_candles

ASSETS = ['BTC', 'ETH', 'NIFTY', 'BANKNIFTY', 'SENSEX']
TFS = ['15m', '1h', '1d']


def gen_box_signals(candles, zone_pct=0.20, vol_filter=False, vol_mult=1.0,
                    trend_filter=False, min_box_pct=0, rr=1, tp_target='mid'):
    if not candles or len(candles) < 2:
        return []

    closes = [c['c'] for c in candles]
    vols = [c.get('v', 0) for c in candles]

    # 200 EMA for trend filter
    ema200 = None
    if trend_filter:
        k = 2 / 201
        ema200 = [closes[0]]
        for v in closes[1:]:
            ema200.append(v * k + ema200[-1] * (1 - k))

    # Avg volume
    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        start = max(0, i - 19)
        w = vols[start:i + 1]
        avg_vol[i] = sum(w) / len(w) if w else 1

    days = {}
    for i, c in enumerate(candles):
        d = _dt.datetime.fromtimestamp(c['t'], _dt.timezone.utc).date()
        days.setdefault(d, []).append((i, c))

    sorted_days = sorted(days.keys())
    if len(sorted_days) < 2:
        return []

    signals = []
    for di in range(1, len(sorted_days)):
        prev = days[sorted_days[di - 1]]
        prev_high = max(c['h'] for _, c in prev)
        prev_low = min(c['l'] for _, c in prev)
        box_range = prev_high - prev_low
        if box_range <= 0:
            continue
        mid = (prev_high + prev_low) / 2

        # Min box size filter
        if min_box_pct > 0 and (box_range / mid * 100) < min_box_pct:
            continue

        sell_zone = prev_high - box_range * zone_pct
        buy_zone = prev_low + box_range * zone_pct

        buy_triggered = sell_triggered = False
        for idx, c in days[sorted_days[di]]:
            # Buy
            if not buy_triggered and c['l'] <= buy_zone and c['c'] > c['o'] and c['c'] > buy_zone:
                if vol_filter and avg_vol[idx] > 0 and vols[idx] < avg_vol[idx] * vol_mult:
                    continue
                if trend_filter and ema200 and c['c'] < ema200[idx]:
                    continue  # skip buys below 200 EMA
                sl = prev_low
                if tp_target == 'mid':
                    tp = mid
                elif tp_target == 'opposite':
                    tp = sell_zone
                else:  # rr-based
                    risk = c['c'] - sl
                    tp = c['c'] + risk * rr if risk > 0 else mid
                signals.append({'time': c['t'], 'type': 'buy', 'price': c['c'], 'sl': sl, 'tp1': tp, 'idx': idx})
                buy_triggered = True

            # Sell
            if not sell_triggered and c['h'] >= sell_zone and c['c'] < c['o'] and c['c'] < sell_zone:
                if vol_filter and avg_vol[idx] > 0 and vols[idx] < avg_vol[idx] * vol_mult:
                    continue
                if trend_filter and ema200 and c['c'] > ema200[idx]:
                    continue  # skip sells above 200 EMA
                sl = prev_high
                if tp_target == 'mid':
                    tp = mid
                elif tp_target == 'opposite':
                    tp = buy_zone
                else:
                    risk = sl - c['c']
                    tp = c['c'] - risk * rr if risk > 0 else mid
                signals.append({'time': c['t'], 'type': 'sell', 'price': c['c'], 'sl': sl, 'tp1': tp, 'idx': idx})
                sell_triggered = True
    return signals


def forward_test(candles, signals, max_bars=50):
    time_idx = {c['t']: i for i, c in enumerate(candles)}
    wins = losses = total_pnl = 0
    for s in signals:
        idx = s.get('idx') or time_idx.get(s['time'])
        if idx is None: continue
        entry, sl, tp = s['price'], s['sl'], s['tp1']
        risk = abs(entry - sl)
        if risk <= 0: continue
        hit = None
        for c in candles[idx + 1:idx + 1 + max_bars]:
            if s['type'] == 'buy':
                if c['l'] <= sl: hit = 'sl'; break
                if c['h'] >= tp: hit = 'tp'; break
            else:
                if c['h'] >= sl: hit = 'sl'; break
                if c['l'] <= tp: hit = 'tp'; break
        if hit == 'tp': wins += 1; total_pnl += abs(tp - entry)
        elif hit == 'sl': losses += 1; total_pnl -= risk
    total = wins + losses
    return {'total': total, 'wins': wins, 'losses': losses, 'wr': round(wins/total*100, 1) if total else 0, 'pnl': round(total_pnl, 2)} if total >= 3 else None


CONFIGS = [
    ('Baseline (20% zone, TP=mid)', {}),
    ('Zone 15%', {'zone_pct': 0.15}),
    ('Zone 25%', {'zone_pct': 0.25}),
    ('Vol > 1.0x', {'vol_filter': True, 'vol_mult': 1.0}),
    ('Vol > 1.5x', {'vol_filter': True, 'vol_mult': 1.5}),
    ('200 EMA trend', {'trend_filter': True}),
    ('TP=opposite zone', {'tp_target': 'opposite'}),
    ('Min box 0.5%', {'min_box_pct': 0.5}),
    ('Min box 1%', {'min_box_pct': 1.0}),
    ('Vol>1x + MinBox0.5%', {'vol_filter': True, 'vol_mult': 1.0, 'min_box_pct': 0.5}),
    ('Vol>1x + 200EMA', {'vol_filter': True, 'vol_mult': 1.0, 'trend_filter': True}),
    ('Vol>1x + MinBox0.5 + 200EMA', {'vol_filter': True, 'vol_mult': 1.0, 'min_box_pct': 0.5, 'trend_filter': True}),
    ('Zone15 + Vol>1x + MinBox0.5', {'zone_pct': 0.15, 'vol_filter': True, 'vol_mult': 1.0, 'min_box_pct': 0.5}),
]

print("=" * 105)
print("BOX THEORY — FILTER COMPARISON")
print("=" * 105)

all_results = []
for asset in ASSETS:
    for tf in TFS:
        candles = get_candles(asset, tf)
        if not candles or len(candles) < 30: continue
        for name, cfg in CONFIGS:
            sigs = gen_box_signals(candles, **cfg)
            bt = forward_test(candles, sigs)
            if bt:
                all_results.append({'asset': asset, 'tf': tf, 'filter': name, **bt})

print("\n" + "=" * 105)
print("SUMMARY (sorted by total PnL)")
print("=" * 105)
for name, _ in CONFIGS:
    rows = [r for r in all_results if r['filter'] == name]
    if rows:
        avg_wr = sum(r['wr'] for r in rows) / len(rows)
        total_pnl = sum(r['pnl'] for r in rows)
        profitable = sum(1 for r in rows if r['pnl'] > 0)
        total_trades = sum(r['total'] for r in rows)
        print(f"  {name:<35} AvgWR: {avg_wr:5.1f}% | Trades: {total_trades:>5} | PnL: {total_pnl:>+12.2f} | Profitable: {profitable}/{len(rows)}")

# Show top 3 best per-combo results
print("\n" + "=" * 105)
print("TOP 10 BEST INDIVIDUAL COMBOS")
print("=" * 105)
print(f"{'Filter':<35} {'Asset':<8} {'TF':<5} {'Trades':<7} {'WR%':<7} {'PnL':<12}")
print("-" * 80)
for r in sorted(all_results, key=lambda x: -x['pnl'])[:10]:
    print(f"{r['filter']:<35} {r['asset']:<8} {r['tf']:<5} {r['total']:<7} {r['wr']:<7} {r['pnl']:<+12.2f}")
