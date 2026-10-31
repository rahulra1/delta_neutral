"""
Candlestick Patterns — improvement search with out-of-sample validation (BTC, Delta BTCUSD perp).

Patterns = exactly the live api.chart.calc_candlestick_patterns rules and precedence:
  bullish engulfing, bearish engulfing, hammer, shooting star.
Variations:
  pat     : all | engulf | pin (hammer + shooting star)
  ctx     : 0 (any) | N = the pattern candle's low (bullish) / high (bearish) is the lowest low /
            highest high of the last N candles (a reversal AT a swing extreme, not mid-move)
  ema     : 0 | 50 | 100 | 200 — bullish patterns only above the EMA, bearish only below
  vol     : 0 | 1.0 (live: v >= avg20) | 1.5
  stop    : bar (pattern candle low/high, as live) | atr (close -/+ 1.5*ATR14)
  minrisk : 0 | 0.5% of price
  rr      : 2 | 3
Engine (trendline_optimize.simulate): entry next candle open, SL first, gap through SL at open,
taker 0.05%+GST + 1bp slip per side, one position at a time, 100 lots.
Selection on Y1 (2024-10-31..2025-10-31) + Y2 (2025-10-31..2026-10-31) only; --holdout runs
2024-01-01..2024-10-31 once for the final picks.

Usage: python backtest/candle_optimize.py [--tf 1h] [--holdout]
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
    'pat': ['all', 'engulf', 'pin'],
    'ctx': [0, 10, 20],
    'ema': [0, 50, 100, 200],
    'vol': [0, 1.0, 1.5],
    'stop': ['bar', 'atr'],
    'minrisk': [0.0, 0.005],
    'rr': [2, 3],
}
KEYS = list(GRID)
BASE = {'pat': 'all', 'ctx': 0, 'ema': 0, 'vol': 1.0, 'stop': 'bar', 'minrisk': 0.0, 'rr': 2}


def classify(cs):
    """Live pattern rules (same precedence). Returns {i: (side, pattern)}."""
    out = {}
    for i in range(1, len(cs) - 1):
        p, c = cs[i - 1], cs[i]
        body, rng, pb = abs(c['c'] - c['o']), c['h'] - c['l'], abs(p['c'] - p['o'])
        if rng <= 0:
            continue
        if p['c'] < p['o'] and c['c'] > c['o'] and c['c'] > p['o'] and c['o'] < p['c'] and body > pb:
            out[i] = ('buy', 'engulf')
        elif p['c'] > p['o'] and c['c'] < c['o'] and c['c'] < p['o'] and c['o'] > p['c'] and body > pb:
            out[i] = ('sell', 'engulf')
        elif c['c'] > c['o'] and (min(c['o'], c['c']) - c['l']) > body * 2 and (c['h'] - max(c['o'], c['c'])) < body * 0.5:
            out[i] = ('buy', 'pin')
        elif c['c'] < c['o'] and (c['h'] - max(c['o'], c['c'])) > body * 2 and (min(c['o'], c['c']) - c['l']) < body * 0.5:
            out[i] = ('sell', 'pin')
    return out


class Ctx(T.Ctx):
    def __init__(self, cs):
        super().__init__(cs)
        self.ema[50] = ema(self.c, 50)
        self.pats = classify(cs)


def gen(ctx, cfg):
    cs, c, h, l = ctx.cs, ctx.c, ctx.h, ctx.l
    sigs = []
    for i, (side, kind) in ctx.pats.items():
        if i < 200:
            continue
        if cfg['pat'] != 'all' and kind != cfg['pat']:
            continue
        if cfg['vol'] and ctx.v[i] < cfg['vol'] * ctx.avg[i]:
            continue
        n = cfg['ctx']
        if n:
            if side == 'buy' and l[i] > min(l[i - n + 1:i + 1]):
                continue
            if side == 'sell' and h[i] < max(h[i - n + 1:i + 1]):
                continue
        if cfg['ema']:
            e = ctx.ema[cfg['ema']][i]
            if (side == 'buy' and c[i] <= e) or (side == 'sell' and c[i] >= e):
                continue
        if cfg['stop'] == 'bar':
            sl = l[i] if side == 'buy' else h[i]
        else:
            sl = c[i] - 1.5 * ctx.atr[i] if side == 'buy' else c[i] + 1.5 * ctx.atr[i]
        sigs.append((i, side, sl))
    sigs.sort()
    return sigs


def name(cfg):
    return (f'{cfg["pat"]} ctx{cfg["ctx"] or "off"} ema{cfg["ema"] or "off"} vol{cfg["vol"] or "off"} '
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
    ap.add_argument('--holdout', default='', help='comma-separated config ranks (1-based) to run on the holdout once')
    a = ap.parse_args()
    cs = rep.fetch_delta(a.tf, 1100)
    ctx = Ctx(cs)
    res, cache = {}, {}
    for vals in itertools.product(*GRID.values()):
        cfg = dict(zip(KEYS, vals))
        sk = tuple(cfg[k] for k in ('pat', 'ctx', 'ema', 'vol', 'stop'))
        if sk not in cache:
            cache[sk] = gen(ctx, cfg)
        res[vals] = {p: stats(T.simulate(ctx, cache[sk], cfg['rr'], cfg['minrisk'], *PERIODS[p])) for p in ('Y1', 'Y2')}

    h = f'{"n":>4} {"win%":>6} {"net$":>7} {"PF":>5} {"maxDD":>6} {"+mo":>5}'
    b = res[tuple(BASE[k] for k in KEYS)]
    print(f'\nBTC {a.tf}: {len(res)} configs. Selection on Y1 + Y2 only.')
    print(f'{"config":<46} | Y1 {h} | Y2 {h} | nbrs ok')
    print(f'{"BASELINE = live (" + name(BASE) + ")":<46} | {T.fmt(b["Y1"])} | {T.fmt(b["Y2"])} |')
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
        print(f'{str(rank) + ". " + name(cfg):<46} | {T.fmt(r["Y1"])} | {T.fmt(r["Y2"])} | {nbr * 100:>4.0f}%')

    print('\nOne-factor effect (avg net over all configs with that setting, Y1 / Y2):')
    for k in KEYS:
        parts = []
        for val in GRID[k]:
            rs = [r for vals, r in res.items() if vals[KEYS.index(k)] == val]
            parts.append(f'{val}: {sum(r["Y1"]["net"] for r in rs) / len(rs):+,.0f} / {sum(r["Y2"]["net"] for r in rs) / len(rs):+,.0f}')
        print(f'  {k:<8} ' + '   '.join(parts))

    if a.holdout:
        print('\nHOLDOUT 2024-01-01..2024-10-31 (never used for selection):')
        picks = [BASE] + [both[int(x) - 1][0] for x in a.holdout.split(',')]
        for cfg in picks:
            sg = cache[tuple(cfg[k] for k in ('pat', 'ctx', 'ema', 'vol', 'stop'))]
            r = {p: stats(T.simulate(ctx, sg, cfg['rr'], cfg['minrisk'], *PERIODS[p])) for p in ('Y1', 'Y2', 'HOLDOUT')}
            print(f'  {name(cfg):<46} Y1 {T.fmt(r["Y1"])} | Y2 {T.fmt(r["Y2"])} | HOLDOUT {T.fmt(r["HOLDOUT"])}')


if __name__ == '__main__':
    main()
