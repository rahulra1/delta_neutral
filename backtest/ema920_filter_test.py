"""Backtest 9/20 EMA Pullback — baseline vs filtered versions."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.chart import get_candles

ASSETS = ['BTC', 'ETH', 'NIFTY', 'BANKNIFTY', 'SENSEX']
TFS = ['15m', '1h']


def ema(data, p):
    k = 2 / (p + 1)
    e = [data[0]]
    for v in data[1:]:
        e.append(v * k + e[-1] * (1 - k))
    return e


def gen_signals(candles, min_rr=2, vol_filter=False, vol_mult=1.0, trend_filter=False, ema200=None, atr_filter=False, atrs=None, min_atr_pct=0):
    closes = [c['c'] for c in candles]
    ema9 = ema(closes, 9)
    ema20 = ema(closes, 20)
    vols = [c.get('v', 0) for c in candles]

    # Avg volume
    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        start = max(0, i - 20)
        window = vols[start:i + 1]
        avg_vol[i] = sum(window) / len(window) if window else 1

    signals = []
    for i in range(21, len(candles) - 1):
        c = candles[i]
        e9, e20 = ema9[i], ema20[i]
        ema_hi = max(e9, e20)
        ema_lo = min(e9, e20)

        above_count = sum(1 for j in range(i - 3, i) if closes[j] > ema_hi)
        below_count = sum(1 for j in range(i - 3, i) if closes[j] < ema_lo)

        sig = None
        if above_count >= 2 and c['l'] <= ema_hi and c['c'] > c['o'] and c['c'] > ema_lo:
            sl = c['l']
            risk = c['c'] - sl
            if risk > 0:
                sig = {'time': c['t'], 'type': 'buy', 'price': c['c'], 'sl': sl, 'tp1': c['c'] + risk * min_rr, 'vol': vols[i], 'avg_vol': avg_vol[i], 'idx': i}

        elif below_count >= 2 and c['h'] >= ema_lo and c['c'] < c['o'] and c['c'] < ema_hi:
            sl = c['h']
            risk = sl - c['c']
            if risk > 0:
                sig = {'time': c['t'], 'type': 'sell', 'price': c['c'], 'sl': sl, 'tp1': c['c'] - risk * min_rr, 'vol': vols[i], 'avg_vol': avg_vol[i], 'idx': i}

        if sig is None:
            continue

        # Filter: volume must be above average * multiplier
        if vol_filter and sig['avg_vol'] > 0 and sig['vol'] < sig['avg_vol'] * vol_mult:
            continue

        # Filter: 200 EMA trend alignment
        if trend_filter and ema200:
            if sig['type'] == 'buy' and c['c'] < ema200[i]:
                continue
            if sig['type'] == 'sell' and c['c'] > ema200[i]:
                continue

        # Filter: minimum ATR % (avoid tiny range candles)
        if atr_filter and atrs and atrs[i] > 0:
            atr_pct = atrs[i] / c['c'] * 100
            if atr_pct < min_atr_pct:
                continue

        signals.append(sig)
    return signals


def forward_test(candles, signals, max_bars=50):
    wins = losses = 0
    total_pnl = 0
    for s in signals:
        idx = s.get('idx')
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


def calc_atr(candles, period=14):
    atrs = [0.0] * len(candles)
    trs = [candles[0]['h'] - candles[0]['l']]
    for i in range(1, len(candles)):
        tr = max(candles[i]['h'] - candles[i]['l'], abs(candles[i]['h'] - candles[i - 1]['c']), abs(candles[i]['l'] - candles[i - 1]['c']))
        trs.append(tr)
    if len(trs) >= period:
        atrs[period - 1] = sum(trs[:period]) / period
        for i in range(period, len(candles)):
            atrs[i] = (atrs[i - 1] * (period - 1) + trs[i]) / period
    return atrs


CONFIGS = [
    ('Baseline (no filter)', {}),
    ('Vol > 1.0x avg', {'vol_filter': True, 'vol_mult': 1.0}),
    ('Vol > 1.5x avg', {'vol_filter': True, 'vol_mult': 1.5}),
    ('200 EMA trend', {'trend_filter': True}),
    ('Vol>1x + 200EMA', {'vol_filter': True, 'vol_mult': 1.0, 'trend_filter': True}),
    ('Vol>1.5x + 200EMA', {'vol_filter': True, 'vol_mult': 1.5, 'trend_filter': True}),
    ('ATR > 0.1%', {'atr_filter': True, 'min_atr_pct': 0.1}),
    ('Vol>1x + 200EMA + ATR', {'vol_filter': True, 'vol_mult': 1.0, 'trend_filter': True, 'atr_filter': True, 'min_atr_pct': 0.1}),
]

print("=" * 100)
print("9/20 EMA PULLBACK — FILTER COMPARISON")
print("=" * 100)

all_results = []

for asset in ASSETS:
    for tf in TFS:
        candles = get_candles(asset, tf)
        if not candles or len(candles) < 210:
            continue
        closes = [c['c'] for c in candles]
        ema200 = ema(closes, 200)
        atrs = calc_atr(candles)

        for name, cfg in CONFIGS:
            kw = dict(cfg)
            if kw.get('trend_filter'):
                kw['ema200'] = ema200
            if kw.get('atr_filter'):
                kw['atrs'] = atrs
            sigs = gen_signals(candles, **kw)
            bt = forward_test(candles, sigs)
            if bt and bt['total'] >= 5:
                all_results.append({'asset': asset, 'tf': tf, 'filter': name, **bt})

# Print grouped by filter
print(f"\n{'Filter':<25} {'Asset':<10} {'TF':<5} {'Trades':<8} {'Wins':<6} {'Loss':<6} {'WR%':<8} {'PnL':<14}")
print("-" * 100)
for name, _ in CONFIGS:
    rows = [r for r in all_results if r['filter'] == name]
    for r in sorted(rows, key=lambda x: -x['wr']):
        print(f"{r['filter']:<25} {r['asset']:<10} {r['tf']:<5} {r['total']:<8} {r['wins']:<6} {r['losses']:<6} {r['wr']:<8} {r['pnl']:<+14.2f}")
    if rows:
        avg_wr = sum(r['wr'] for r in rows) / len(rows)
        total_pnl = sum(r['pnl'] for r in rows)
        print(f"  {'>>> AVG':<23} {'':10} {'':5} {'':8} {'':6} {'':6} {avg_wr:<8.1f} {total_pnl:<+14.2f}")
    print()

# Best filter overall
print("=" * 100)
print("BEST FILTER (by avg win rate across all assets/TFs)")
print("=" * 100)
for name, _ in CONFIGS:
    rows = [r for r in all_results if r['filter'] == name]
    if rows:
        avg_wr = sum(r['wr'] for r in rows) / len(rows)
        total_pnl = sum(r['pnl'] for r in rows)
        profitable = sum(1 for r in rows if r['pnl'] > 0)
        print(f"  {name:<30} AvgWR: {avg_wr:5.1f}% | TotalPnL: {total_pnl:>+12.2f} | Profitable: {profitable}/{len(rows)}")
