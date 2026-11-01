"""
Div + MSS — causal rebuild + tuning with two independent checks (BTC selection, BTC holdout, ETH).

Rules (rebuilt from api.chart.calc_rsi_divergence_mss, made causal):
  - swing high/low = extreme of lb candles each side, usable only after those lb candles CLOSE
  - bearish divergence: two consecutive swing highs with RSI(14) above `ob` (both), price higher
    high, RSI lower high. MSS = first CLOSE below the lowest low between the two highs, at least
    lb candles after the 2nd high (confirmation) and within `age` candles. Bullish mirror.
  - signals sorted by time, closed candles only (fixes the live list-order and repaint bugs)
Tuning grid:
  tf    : 15m | 1h
  lb    : 3 | 5
  zone  : 70/30 (live) | 65/35 | 60/40 | none (any RSI level)
  age   : 20 | 50 (live)
  stop  : pivot (2nd swing extreme, live) | atr (1.5 x ATR14 from the close)
  rr    : 1 (live) | 1.5 | 2 | 3
  trend : off | ema200 (only take reversals in the EMA200 direction: buys above, sells below)
  minrisk: 0 | 0.5%
Engine = trendline_optimize.simulate (next-open entry, SL first, taker 0.05%+GST + 1bp slip,
one position at a time, 0.1 BTC). Selection: profitable in Y1 and Y2 with >= 12 trades each,
ranked by neighbour robustness. Then, once: BTC 2024-01..10 holdout and ETH (all periods).

Usage: python backtest/div_mss_optimize.py --tf 1h [--check 1,2,3]
"""
import argparse
import bisect
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btc_signal_strategy_report as rep  # noqa: E402
import trendline_optimize as T  # noqa: E402
from sma_v2_improve import PERIODS, stats  # noqa: E402
from api.chart import calc_rsi  # noqa: E402

GRID = {
    'lb': [3, 5],
    'zone': [70, 65, 60, 0],
    'age': [20, 50],
    'stop': ['pivot', 'atr'],
    'rr': [1, 1.5, 2, 3],
    'trend': ['off', 'ema200'],
    'minrisk': [0.0, 0.005],
}
KEYS = list(GRID)
SIG_KEYS = ('lb', 'zone', 'age', 'stop', 'trend')
BASE = {'lb': 5, 'zone': 70, 'age': 50, 'stop': 'pivot', 'rr': 1, 'trend': 'off', 'minrisk': 0.0}


class Ctx(T.Ctx):
    def __init__(self, cs):
        super().__init__(cs)
        r = {x['time']: x['value'] for x in calc_rsi(cs, 14)}
        self.rsi = [r.get(x['t']) for x in cs]


def gen(ctx, cfg):
    cs, c, h, l, rsi = ctx.cs, ctx.c, ctx.h, ctx.l, ctx.rsi
    lb, zone, age = cfg['lb'], cfg['zone'], cfg['age']
    ph, pl = ctx.pivots(lb)
    out = []
    for side, piv in (('sell', ph), ('buy', pl)):
        src = h if side == 'sell' else l
        if zone:
            piv = [i for i in piv if rsi[i] is not None and (rsi[i] > zone if side == 'sell' else rsi[i] < 100 - zone)]
        else:
            piv = [i for i in piv if rsi[i] is not None]
        for j in range(1, len(piv)):
            i1, i2 = piv[j - 1], piv[j]
            if side == 'sell' and not (h[i2] > h[i1] and rsi[i2] < rsi[i1]):
                continue
            if side == 'buy' and not (l[i2] < l[i1] and rsi[i2] > rsi[i1]):
                continue
            level = min(l[i1:i2 + 1]) if side == 'sell' else max(h[i1:i2 + 1])
            for k in range(i2 + lb, min(i2 + age, len(cs) - 1)):
                if (side == 'sell' and c[k] < level) or (side == 'buy' and c[k] > level):
                    if cfg['trend'] == 'ema200':
                        e = ctx.ema[200][k]
                        if (side == 'buy' and c[k] <= e) or (side == 'sell' and c[k] >= e):
                            break
                    sl = src[i2] if cfg['stop'] == 'pivot' else (
                        c[k] - 1.5 * ctx.atr[k] if side == 'buy' else c[k] + 1.5 * ctx.atr[k])
                    out.append((k, side, sl))
                    break
    out.sort()
    return out


def name(cfg):
    return ' '.join(f'{k}={cfg[k]}' for k in KEYS if cfg[k] != BASE[k]) or 'BASELINE (live rules, causal)'


def neighbours(cfg):
    out = []
    for k in KEYS:
        vals = GRID[k]
        i = vals.index(cfg[k])
        for d in (-1, 1):
            if 0 <= i + d < len(vals):
                out.append(dict(cfg, **{k: vals[i + d]}))
    return out


def run_all(ctx, cache, cfg, pers):
    sk = tuple(cfg[k] for k in SIG_KEYS)
    if sk not in cache:
        cache[sk] = gen(ctx, cfg)
    return {p: stats(T.simulate(ctx, cache[sk], cfg['rr'], cfg['minrisk'], *PERIODS[p])) for p in pers}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tf', default='1h')
    ap.add_argument('--top', type=int, default=12)
    ap.add_argument('--min-trades', type=int, default=12)
    ap.add_argument('--check', default='', help='ranks to run once on BTC holdout + ETH')
    a = ap.parse_args()
    cs = rep.fetch_delta(a.tf, 1100)
    ctx = Ctx(cs)
    cache, res = {}, {}
    for vals in itertools.product(*GRID.values()):
        cfg = dict(zip(KEYS, vals))
        res[vals] = run_all(ctx, cache, cfg, ('Y1', 'Y2'))
    h = f'{"n":>4} {"win%":>6} {"net$":>7} {"PF":>5} {"maxDD":>6} {"+mo":>5}'
    print(f'\nBTC {a.tf}: {len(res)} configs. Selection on Y1 + Y2 only.')
    print(f'{"config":<52} | Y1 {h} | Y2 {h} | nbrs')
    b = res[tuple(BASE[k] for k in KEYS)]
    print(f'{"BASELINE (live rules, causal)":<52} | {T.fmt(b["Y1"])} | {T.fmt(b["Y2"])} |')
    good = []
    for vals, r in res.items():
        if all(r[p]['net'] > 0 and r[p]['n'] >= a.min_trades for p in ('Y1', 'Y2')):
            cfg = dict(zip(KEYS, vals))
            nb = neighbours(cfg)
            ok = sum(1 for x in nb if all(res[tuple(x[k] for k in KEYS)][p]['net'] > 0 for p in ('Y1', 'Y2')))
            good.append((cfg, r, ok / len(nb)))
    print(f'Profitable in BOTH years (>= {a.min_trades} trades each): {len(good)} / {len(res)}')
    good.sort(key=lambda g: (-round(g[2], 1), -min(g[1][p]['net'] / max(g[1][p]['dd'], 1) for p in ('Y1', 'Y2'))))
    for rank, (cfg, r, nbr) in enumerate(good[:a.top], 1):
        print(f'{str(rank) + ". " + name(cfg):<52} | {T.fmt(r["Y1"])} | {T.fmt(r["Y2"])} | {nbr * 100:3.0f}%')
    print('\nOne-factor effect (avg net, Y1 / Y2; avg trades/yr):')
    for k in KEYS:
        parts = []
        for v in GRID[k]:
            rs = [r for vals, r in res.items() if vals[KEYS.index(k)] == v]
            parts.append(f'{v}: {sum(r["Y1"]["net"] for r in rs) / len(rs):+,.0f} / {sum(r["Y2"]["net"] for r in rs) / len(rs):+,.0f} '
                         f'({sum(r["Y1"]["n"] + r["Y2"]["n"] for r in rs) / len(rs) / 2:.0f}/yr)')
        print(f'  {k:<7} ' + '   '.join(parts))

    if a.check:
        picks = [BASE] + [good[int(x) - 1][0] for x in a.check.split(',')]
        print('\n=== ONE-TIME CHECKS: BTC holdout 2024-01..10, and ETH (never used for this strategy) ===')
        ecs = rep.fetch_delta(a.tf, 1100, symbol='ETHUSD')
        ectx, ecache = Ctx(ecs), {}
        for cfg in picks:
            rb = run_all(ctx, cache, cfg, ('HOLDOUT',))
            re_ = run_all(ectx, ecache, cfg, ('HOLDOUT', 'Y1', 'Y2'))
            eth_tot = sum(re_[p]['net'] for p in re_)
            eth_n = sum(re_[p]['n'] for p in re_)
            print(f'  {name(cfg):<52} | BTC HO {T.fmt(rb["HOLDOUT"])} | ETH(0.1 ETH/trade, PF matters) '
                  f'{eth_n} trades, PF HO/Y1/Y2 {re_["HOLDOUT"]["pf"]:.2f}/{re_["Y1"]["pf"]:.2f}/{re_["Y2"]["pf"]:.2f}, net ${eth_tot:+,.0f}')


if __name__ == '__main__':
    main()
