"""
9/20 EMA Pullback — round 3: structural enhancements, new validation protocol.

The 2024-01..10 holdout was already used once (round 2), so it is now part of SELECTION:
  SELECT  : BTC 1h, must be profitable in all three periods (2024-01..10, Y1, Y2) with
            >= 15 trades each; ranked by neighbour robustness then worst-period net/DD.
  VALIDATE: ETH 1h (Delta ETHUSD), never used for any strategy before — run once, --validate.

Enhancement dimensions (on top of round-2 learnings: min stop 0.5% fixed):
  htf   : off | 4h  (only longs when the last COMPLETED 4h close > 4h EMA50; shorts mirror)
  trend : live (>= 2 of last 3 closes beyond both EMAs) | strict (all of last 5)
  touch : zone (low <= max(EMA9,EMA20), live) | ema20 (pullback must reach the EMA20)
  entry : open (market at next open, live) | stop (stop order at the pullback candle's
          high/low, valid 3 candles; cancelled if the SL is hit first)
  exit  : rr2 | rr3 | trail (no TP; exit at the close of the first candle that closes back
          through the EMA20; SL still active)
  stop  : bar (pullback candle extreme) | swing (lowest low / highest high of last 5)
  adx   : 0 | 20 (ADX14 >= 20 at the signal candle)
  vol   : 0 | 1.0 (volume >= 20-candle average)
Sizing: fixed $8,000 notional per trade (= 0.1 BTC at $80k) so BTC and ETH are comparable.
Fees: taker 0.05% + 18% GST + 1bp slippage on every leg; one position at a time; SL first.

Usage:
  python backtest/ema920_v3.py               # selection on BTC
  python backtest/ema920_v3.py --validate 1,2,3   # + one-time ETH validation of those ranks
"""
import argparse
import itertools
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btc_signal_strategy_report as rep  # noqa: E402
from sma_v2_improve import PERIODS, ema, adx  # noqa: E402
import trendline_optimize as T  # noqa: E402

NOTIONAL = 8000.0
FEE = 0.0005 * 1.18
SLIP = 0.0001
MINRISK = 0.005
EQ0 = 10_000.0

GRID = {
    'htf': ['off', '4h'],
    'trend': ['live', 'strict'],
    'touch': ['zone', 'ema20'],
    'entry': ['open', 'stop'],
    'exit': ['rr2', 'rr3', 'trail'],
    'stop': ['bar', 'swing'],
    'adx': [0, 20],
    'vol': [0, 1.0],
}
KEYS = list(GRID)
SIG_KEYS = ('htf', 'trend', 'touch', 'stop', 'adx', 'vol')
BASE = {'htf': 'off', 'trend': 'live', 'touch': 'zone', 'entry': 'open', 'exit': 'rr2',
        'stop': 'bar', 'adx': 0, 'vol': 1.0}


class Ctx(T.Ctx):
    def __init__(self, cs):
        super().__init__(cs)
        c = self.c
        self.e9, self.e20 = ema(c, 9), ema(c, 20)
        self.adx14 = adx(cs)
        # 4h trend from completed 4h candles built out of the 1h candles (causal)
        groups = defaultdict(list)
        for i, x in enumerate(cs):
            groups[x['t'] // 14400].append(i)
        keys = sorted(groups)
        closes4 = [c[groups[k][-1]] for k in keys]
        e50 = ema(closes4, 50)
        state = {}
        for n, k in enumerate(keys):
            state[k] = (closes4[n] > e50[n]) - (closes4[n] < e50[n])   # +1 up, -1 down
        self.htf = [0] * len(cs)
        for i, x in enumerate(cs):
            k = x['t'] // 14400
            # last COMPLETED 4h block: if bar i closes its block, that block is complete
            done = k if (x['t'] + 3600) % 14400 == 0 else k - 1
            self.htf[i] = state.get(done, 0)


def gen(ctx, cfg):
    cs, c, h, l = ctx.cs, ctx.c, ctx.h, ctx.l
    out = []
    for i in range(205, len(cs) - 1):
        x = cs[i]
        hi, lo = max(ctx.e9[i], ctx.e20[i]), min(ctx.e9[i], ctx.e20[i])
        if cfg['trend'] == 'live':
            up = sum(1 for j in range(i - 3, i) if c[j] > hi) >= 2
            dn = sum(1 for j in range(i - 3, i) if c[j] < lo) >= 2
        else:
            up = all(c[j] > max(ctx.e9[j], ctx.e20[j]) for j in range(i - 5, i))
            dn = all(c[j] < min(ctx.e9[j], ctx.e20[j]) for j in range(i - 5, i))
        t_up = hi if cfg['touch'] == 'zone' else ctx.e20[i]
        t_dn = lo if cfg['touch'] == 'zone' else ctx.e20[i]
        side = None
        if up and x['l'] <= t_up and x['c'] > x['o'] and x['c'] > lo:
            side = 'buy'
        elif dn and x['h'] >= t_dn and x['c'] < x['o'] and x['c'] < hi:
            side = 'sell'
        if not side:
            continue
        if cfg['vol'] and ctx.v[i] < cfg['vol'] * ctx.avg[i]:
            continue
        if cfg['adx'] and ctx.adx14[i] < cfg['adx']:
            continue
        if cfg['htf'] == '4h' and ctx.htf[i] != (1 if side == 'buy' else -1):
            continue
        if cfg['stop'] == 'bar':
            sl = x['l'] if side == 'buy' else x['h']
        else:
            sl = min(l[i - 4:i + 1]) if side == 'buy' else max(h[i - 4:i + 1])
        out.append((i, side, sl))
    return out


def simulate(ctx, sigs, cfg, start, end):
    cs, n = ctx.cs, len(ctx.cs)
    trades, busy = [], -1
    rr = {'rr2': 2, 'rr3': 3}.get(cfg['exit'])
    for i, side, sl in sigs:
        if not (start <= cs[i]['t'] < end) or i + 1 <= busy:
            continue
        buy = side == 'buy'
        # ---- entry ----
        if cfg['entry'] == 'open':
            j, raw = i + 1, cs[i + 1]['o']
            ref = cs[i]['c']
        else:
            lvl = cs[i]['h'] if buy else cs[i]['l']
            j = raw = None
            for k in range(i + 1, min(i + 4, n)):
                b = cs[k]
                if (buy and b['h'] >= lvl) or (not buy and b['l'] <= lvl):
                    j, raw = k, (max(b['o'], lvl) if buy else min(b['o'], lvl))
                    break
                if (buy and b['l'] <= sl) or (not buy and b['h'] >= sl):
                    break                                   # invalidated before trigger
            if j is None:
                continue
            ref = lvl
        risk = (ref - sl) if buy else (sl - ref)
        if risk <= 0 or risk / ref < MINRISK:
            continue
        tp = (ref + rr * risk if buy else ref - rr * risk) if rr else None
        entry = raw * (1 + SLIP) if buy else raw * (1 - SLIP)
        qty = NOTIONAL / entry
        reason = xraw = None
        k = j
        if (buy and raw <= sl) or (not buy and raw >= sl):
            reason, xraw = 'SL', raw
        else:
            for k in range(j, n):
                b = cs[k]
                if (buy and b['l'] <= sl) or (not buy and b['h'] >= sl):
                    reason = 'SL'
                    xraw = (min(sl, b['o']) if buy else max(sl, b['o'])) if k > j else sl
                    break
                if tp and ((buy and b['h'] >= tp) or (not buy and b['l'] <= tp)):
                    reason, xraw = 'TP', tp
                    break
                if cfg['exit'] == 'trail' and ((buy and b['c'] < ctx.e20[k]) or (not buy and b['c'] > ctx.e20[k])):
                    reason, xraw = 'TRAIL', b['c']
                    break
            if reason is None:
                reason, xraw, k = 'END', cs[-1]['c'], n - 1
        xp = xraw * (1 - SLIP) if buy else xraw * (1 + SLIP)
        gross = ((xp - entry) if buy else (entry - xp)) * qty
        fees = (entry + xp) * qty * FEE
        trades.append({'t': cs[j]['t'], 'xt': cs[k]['t'], 'net': gross - fees, 'side': side})
        busy = k
    return trades


def stats(tr):
    if not tr:
        return dict(n=0, net=0, pf=0, wr=0, dd=0, mpos=0, mn=0)
    nets = [t['net'] for t in tr]
    gw, gl = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
    eq = pk = EQ0
    dd = 0.0
    for t in sorted(tr, key=lambda x: x['xt']):
        eq += t['net']
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    mo = defaultdict(float)
    for t in tr:
        mo[datetime.fromtimestamp(t['t'], tz=timezone.utc).strftime('%Y-%m')] += t['net']
    return dict(n=len(tr), net=sum(nets), pf=gw / gl if gl else 99, wr=sum(1 for x in nets if x > 0) / len(tr) * 100,
                dd=dd, mpos=sum(1 for x in mo.values() if x > 0), mn=len(mo))


PERS = ('HOLDOUT', 'Y1', 'Y2')


def name(cfg):
    return ' '.join(f'{k}={cfg[k]}' for k in KEYS if cfg[k] != BASE[k]) or 'BASELINE (live rules)'


def neighbours(cfg):
    out = []
    for k in KEYS:
        vals = GRID[k]
        i = vals.index(cfg[k])
        for d in (-1, 1):
            if 0 <= i + d < len(vals):
                out.append(dict(cfg, **{k: vals[i + d]}))
    return out


def evaluate(ctx, cfg, cache):
    sk = tuple(cfg[k] for k in SIG_KEYS)
    if sk not in cache:
        cache[sk] = gen(ctx, cfg)
    return {p: stats(simulate(ctx, cache[sk], cfg, *PERIODS[p])) for p in PERS}


def row(r):
    return ' | '.join(f'{p[:2]} {T.fmt(r[p])}' for p in PERS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--top', type=int, default=12)
    ap.add_argument('--validate', default='', help='ranks to run once on ETH 1h')
    a = ap.parse_args()
    cs = rep.fetch_delta('1h', 1100)
    ctx = Ctx(cs)
    cache, res = {}, {}
    for vals in itertools.product(*GRID.values()):
        cfg = dict(zip(KEYS, vals))
        res[vals] = evaluate(ctx, cfg, cache)
    print(f'BTC 1h, {len(res)} configs, $8k notional/trade. Periods: HO=2024-01..10, Y1, Y2 (all used for selection)')
    print(f'{"config":<62} | ' + ' | '.join(f'{p[:2]} {"n":>4} {"win%":>6} {"net$":>7} {"PF":>5} {"maxDD":>6} {"+mo":>5}' for p in PERS) + ' | nbrs')
    print(f'{"BASELINE (live rules)":<62} | {row(res[tuple(BASE[k] for k in KEYS)])} |')
    good = []
    for vals, r in res.items():
        if all(r[p]['net'] > 0 and r[p]['n'] >= 15 for p in PERS):
            cfg = dict(zip(KEYS, vals))
            nb = neighbours(cfg)
            ok = sum(1 for x in nb if all(res[tuple(x[k] for k in KEYS)][p]['net'] > 0 for p in PERS))
            good.append((cfg, r, ok / len(nb)))
    print(f'Profitable in ALL THREE BTC periods: {len(good)} / {len(res)}')
    good.sort(key=lambda g: (-round(g[2], 1), -min(g[1][p]['net'] / max(g[1][p]['dd'], 1) for p in PERS)))
    for rank, (cfg, r, nbr) in enumerate(good[:a.top], 1):
        print(f'{str(rank) + ". " + name(cfg):<62} | {row(r)} | {nbr * 100:3.0f}%')
    print('\nOne-factor effect (avg net over all configs, HO / Y1 / Y2):')
    for k in KEYS:
        parts = []
        for v in GRID[k]:
            rs = [r for vals, r in res.items() if vals[KEYS.index(k)] == v]
            parts.append(f'{v}: ' + ' / '.join(f'{sum(r[p]["net"] for r in rs) / len(rs):+,.0f}' for p in PERS))
        print(f'  {k:<6} ' + '   '.join(parts))

    if a.validate:
        print('\n=== ETH 1h VALIDATION (Delta ETHUSD; never used before; run once) ===')
        ecs = rep.fetch_delta('1h', 1100, symbol='ETHUSD')
        ectx = Ctx(ecs)
        ecache = {}
        for cfg in [BASE] + [good[int(x) - 1][0] for x in a.validate.split(',')]:
            r = evaluate(ectx, cfg, ecache)
            tot = sum(r[p]['net'] for p in PERS)
            print(f'  {name(cfg):<62} | {row(r)} | total {tot:+,.0f}')


if __name__ == '__main__':
    main()
