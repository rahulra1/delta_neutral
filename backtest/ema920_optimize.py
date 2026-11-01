"""
9/20 EMA Pullback — improvement search with out-of-sample validation (BTC, Delta BTCUSD perp).

Live rules (api.chart.calc_ema920_pullback):
  long : >= 2 of the previous 3 closes above both EMAs, the candle's low touches the EMA zone
         (low <= max(EMA9, EMA20)), bullish candle closing above min(EMA9, EMA20); mirror for shorts
  volume >= 20-candle average; SL = candle low/high; TP 2R; entry next open.
Variations:
  trend  : live (2 of last 3 closes beyond both EMAs) | strict (all of the last 5)
  align  : 0 | 1 (EMA9 above EMA20 for longs, below for shorts)
  emaT   : 0 | 100 | 200 (higher trend filter: close above/below that EMA)
  vol    : 0 | 1.0 (live) | 1.5
  stop   : bar (live) | swing (lowest low / highest high of the last 5 candles) | atr (1.5*ATR14)
  minrisk: 0 | 0.5%
  rr     : 2 | 3
Engine: trendline_optimize.simulate (next-open entry, SL first, taker 0.05%+GST + 1bp slip,
one position at a time, 100 lots). Selection on Y1 + Y2; --holdout ranks run 2024-01..10 once.

Usage: python backtest/ema920_optimize.py --tf 1h [--holdout 1,2]
"""
import argparse
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btc_signal_strategy_report as rep  # noqa: E402
import trendline_optimize as T  # noqa: E402
from sma_v2_improve import PERIODS, stats, ema  # noqa: E402

GRID = {
    'trend': ['live', 'strict'],
    'align': [0, 1],
    'emaT': [0, 100, 200],
    'vol': [0, 1.0, 1.5],
    'stop': ['bar', 'swing', 'atr'],
    'minrisk': [0.0, 0.005],
    'rr': [2, 3],
}
KEYS = list(GRID)
BASE = {'trend': 'live', 'align': 0, 'emaT': 0, 'vol': 1.0, 'stop': 'bar', 'minrisk': 0.0, 'rr': 2}
SIG_KEYS = ('trend', 'align', 'emaT', 'vol', 'stop')


class Ctx(T.Ctx):
    def __init__(self, cs):
        super().__init__(cs)
        self.e9 = ema(self.c, 9)
        self.e20 = ema(self.c, 20)


def gen(ctx, cfg):
    cs, c, h, l = ctx.cs, ctx.c, ctx.h, ctx.l
    out = []
    for i in range(205, len(cs) - 1):
        x = cs[i]
        hi, lo = max(ctx.e9[i], ctx.e20[i]), min(ctx.e9[i], ctx.e20[i])
        if cfg['trend'] == 'live':
            up = sum(1 for j in range(i - 3, i) if c[j] > max(ctx.e9[j], ctx.e20[j])) >= 2 if False else \
                 sum(1 for j in range(i - 3, i) if c[j] > hi) >= 2
            dn = sum(1 for j in range(i - 3, i) if c[j] < lo) >= 2
        else:
            up = all(c[j] > max(ctx.e9[j], ctx.e20[j]) for j in range(i - 5, i))
            dn = all(c[j] < min(ctx.e9[j], ctx.e20[j]) for j in range(i - 5, i))
        side = None
        if up and x['l'] <= hi and x['c'] > x['o'] and x['c'] > lo:
            side = 'buy'
        elif dn and x['h'] >= lo and x['c'] < x['o'] and x['c'] < hi:
            side = 'sell'
        if not side:
            continue
        if cfg['vol'] and ctx.v[i] < cfg['vol'] * ctx.avg[i]:
            continue
        if cfg['align'] and ((side == 'buy' and ctx.e9[i] <= ctx.e20[i]) or (side == 'sell' and ctx.e9[i] >= ctx.e20[i])):
            continue
        if cfg['emaT']:
            e = ctx.ema[cfg['emaT']][i]
            if (side == 'buy' and c[i] <= e) or (side == 'sell' and c[i] >= e):
                continue
        if cfg['stop'] == 'bar':
            sl = x['l'] if side == 'buy' else x['h']
        elif cfg['stop'] == 'swing':
            sl = min(l[i - 4:i + 1]) if side == 'buy' else max(h[i - 4:i + 1])
        else:
            sl = c[i] - 1.5 * ctx.atr[i] if side == 'buy' else c[i] + 1.5 * ctx.atr[i]
        out.append((i, side, sl))
    return out


def name(cfg):
    return (f'{cfg["trend"]} align{cfg["align"]} emaT{cfg["emaT"] or "off"} vol{cfg["vol"] or "off"} '
            f'{cfg["stop"]} mr{cfg["minrisk"] * 100:.1f} rr{cfg["rr"]}')


def neighbours(cfg):
    out = []
    for k in KEYS:
        vals = GRID[k]
        i = vals.index(cfg[k])
        for d in (-1, 1):
            if 0 <= i + d < len(vals):
                out.append(dict(cfg, **{k: vals[i + d]}))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tf', default='1h')
    ap.add_argument('--top', type=int, default=12)
    ap.add_argument('--min-trades', type=int, default=20)
    ap.add_argument('--holdout', default='')
    a = ap.parse_args()
    cs = rep.fetch_delta(a.tf, 1100)
    ctx = Ctx(cs)
    if a.tf == '1h':   # sanity: rebuilt baseline == live function on the same candles
        from api.chart import calc_ema920_pullback
        idx = {x['t']: i for i, x in enumerate(cs)}
        live = [(idx[s['time']], s['type']) for s in calc_ema920_pullback(cs + [dict(cs[-1])])['signals'] if idx[s['time']] >= 205]
        mine = [(i, s) for i, s, _ in gen(ctx, BASE)]
        print(f'rebuilt baseline == live fn: {live == mine} ({len(mine)} signals)')
    res, cache = {}, {}
    for vals in itertools.product(*GRID.values()):
        cfg = dict(zip(KEYS, vals))
        sk = tuple(cfg[k] for k in SIG_KEYS)
        if sk not in cache:
            cache[sk] = gen(ctx, cfg)
        res[vals] = {p: stats(T.simulate(ctx, cache[sk], cfg['rr'], cfg['minrisk'], *PERIODS[p])) for p in ('Y1', 'Y2')}

    h = f'{"n":>4} {"win%":>6} {"net$":>7} {"PF":>5} {"maxDD":>6} {"+mo":>5}'
    b = res[tuple(BASE[k] for k in KEYS)]
    print(f'\nBTC {a.tf}: {len(res)} configs. Selection on Y1 + Y2 only.')
    print(f'{"config":<48} | Y1 {h} | Y2 {h} | nbrs ok')
    print(f'{"BASELINE = live (" + name(BASE) + ")":<48} | {T.fmt(b["Y1"])} | {T.fmt(b["Y2"])} |')
    both = []
    for vals, r in res.items():
        if r['Y1']['net'] > 0 and r['Y2']['net'] > 0 and min(r['Y1']['n'], r['Y2']['n']) >= a.min_trades:
            cfg = dict(zip(KEYS, vals))
            nb = neighbours(cfg)
            ok = sum(1 for x in nb if all(res[tuple(x[k] for k in KEYS)][p]['net'] > 0 for p in ('Y1', 'Y2')))
            both.append((cfg, r, ok / len(nb)))
    print(f'Profitable in BOTH years (>= {a.min_trades} trades each): {len(both)} / {len(res)}')
    both.sort(key=lambda x: (-round(x[2], 1), -min(x[1]['Y1']['net'], x[1]['Y2']['net'])
                             / max(x[1]['Y1']['dd'], x[1]['Y2']['dd'], 1)))
    for rank, (cfg, r, nbr) in enumerate(both[:a.top], 1):
        print(f'{str(rank) + ". " + name(cfg):<48} | {T.fmt(r["Y1"])} | {T.fmt(r["Y2"])} | {nbr * 100:>4.0f}%')
    print('\nOne-factor effect (avg net over all configs with that setting, Y1 / Y2):')
    for k in KEYS:
        parts = []
        for val in GRID[k]:
            rs = [r for vals, r in res.items() if vals[KEYS.index(k)] == val]
            parts.append(f'{val}: {sum(r["Y1"]["net"] for r in rs) / len(rs):+,.0f} / {sum(r["Y2"]["net"] for r in rs) / len(rs):+,.0f}')
        print(f'  {k:<8} ' + '   '.join(parts))
    if a.holdout:
        print('\nHOLDOUT 2024-01-01..2024-10-31 (never used for selection):')
        for cfg in [BASE] + [both[int(x) - 1][0] for x in a.holdout.split(',')]:
            sg = cache[tuple(cfg[k] for k in SIG_KEYS)]
            r = {p: stats(T.simulate(ctx, sg, cfg['rr'], cfg['minrisk'], *PERIODS[p])) for p in ('Y1', 'Y2', 'HOLDOUT')}
            print(f'  {name(cfg):<48} Y1 {T.fmt(r["Y1"])} | Y2 {T.fmt(r["Y2"])} | HOLDOUT {T.fmt(r["HOLDOUT"])}')


if __name__ == '__main__':
    main()
