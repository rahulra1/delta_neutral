"""
EMA + Trendline breakout — causal rebuild + robust improvement search (BTC, Delta BTCUSD perp).

Causal trendline logic (no look-ahead):
  - swing high at i = highest high of i-lb..i+lb, only KNOWN at bar i+lb (confirmation)
  - down-trendline = the last two confirmed swing highs with p2 < p1 (and i2 - i1 >= lb)
  - long signal at bar t: t >= i2+lb (pivot confirmed), t <= i2 + window,
    close[t] > line(t) while close[t-1] <= line(t-1) (fresh cross), + filters
  - shorts mirror with rising swing lows
  - entry next bar open; SL first on ambiguous bars; taker 0.05%+GST + 1bp slip each leg;
    one position at a time; 100 lots (0.1 BTC); no funding.

Search dimensions:
  lb 3/5/8 | ema off/100/200 | vol none/1.2x/1.5x | stop bar/swing/atr | minrisk 0/0.5% |
  rr 2/3/4 | window 3*lb / 6*lb
Selection uses Y1 (2024-10-31..2025-10-31) and Y2 (2025-10-31..2026-10-31) only.
--holdout additionally prints 2024-01-01..2024-10-31 for the chosen configs (run once).

Usage: python backtest/trendline_optimize.py [--tf 1h] [--holdout CFG ...]
"""
import argparse
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btc_signal_strategy_report as rep  # noqa: E402
from sma_v2_improve import PERIODS, stats, ema  # noqa: E402

QTY = 0.1
FEE = 0.0005 * 1.18
SLIP = 0.0001


def atr(cs, p=14):
    out = [0.0] * len(cs)
    a = None
    for i in range(1, len(cs)):
        tr = max(cs[i]['h'] - cs[i]['l'], abs(cs[i]['h'] - cs[i - 1]['c']), abs(cs[i]['l'] - cs[i - 1]['c']))
        a = tr if a is None else (a * (p - 1) + tr) / p
        out[i] = a
    return out


class Ctx:
    def __init__(self, cs):
        self.cs = cs
        self.h = [x['h'] for x in cs]
        self.l = [x['l'] for x in cs]
        self.c = [x['c'] for x in cs]
        self.v = [x['v'] for x in cs]
        self.ema = {p: ema(self.c, p) for p in (100, 200)}
        self.atr = atr(cs)
        n = len(cs)
        self.avg = [0.0] * n
        s = 0.0
        for i, x in enumerate(self.v):
            s += x
            if i >= 20:
                s -= self.v[i - 20]
            self.avg[i] = max(1.0, s / min(i + 1, 20))
        self.piv = {}

    def pivots(self, lb):
        if lb not in self.piv:
            n = len(self.cs)
            ph, pl = [], []
            for i in range(lb, n - lb):
                w_h = self.h[i - lb:i + lb + 1]
                w_l = self.l[i - lb:i + lb + 1]
                if self.h[i] == max(w_h):
                    ph.append(i)
                if self.l[i] == min(w_l):
                    pl.append(i)
            self.piv[lb] = (ph, pl)
        return self.piv[lb]


def gen_signals(ctx, lb, ema_p, vol, stop, window, tlq='any', stats_out=None):
    """Return list of (t, side, sl) — causal.
    tlq: 'any'   = any two consecutive pivots (like live)
         'clean' = no candle CLOSE beyond the line from the 1st pivot up to the bar before the break
                   (a real trendline: price respected it until the breakout)"""
    n = len(ctx.cs)
    ph, pl = ctx.pivots(lb)
    c, h, l = ctx.c, ctx.h, ctx.l
    sigs = []
    for side, piv in (('buy', ph), ('sell', pl)):
        src = h if side == 'buy' else l
        for k in range(1, len(piv)):
            i1, i2 = piv[k - 1], piv[k]
            if i2 - i1 < lb:
                continue
            p1, p2 = src[i1], src[i2]
            if (side == 'buy' and p2 >= p1) or (side == 'sell' and p2 <= p1):
                continue
            # this line is the "current" one until the next pivot of the same kind is confirmed
            nxt_conf = piv[k + 1] + lb if k + 1 < len(piv) else n
            slope = (p2 - p1) / (i2 - i1)
            start = i2 + lb
            end = min(i2 + window, nxt_conf - 1, n - 2)
            for t in range(start, end + 1):
                line, pline = p2 + slope * (t - i2), p2 + slope * (t - 1 - i2)
                if side == 'buy':
                    cross = c[t] > line and c[t - 1] <= pline
                else:
                    cross = c[t] < line and c[t - 1] >= pline
                if not cross:
                    continue
                # trendline validity: closes between pivot 1 and the breakout must respect the line
                viol = 0
                for q in range(i1 + 1, t):
                    lq = p2 + slope * (q - i2)
                    if (side == 'buy' and c[q] > lq) or (side == 'sell' and c[q] < lq):
                        viol += 1
                if stats_out is not None:
                    stats_out.append((t, side, i1, i2, p1, p2, viol))
                if tlq == 'clean' and viol:
                    break
                ok = True
                if ema_p:
                    e = ctx.ema[ema_p][t]
                    ok = c[t] > e if side == 'buy' else c[t] < e
                if ok and vol and ctx.v[t] < vol * ctx.avg[t]:
                    ok = False
                if ok:
                    if stop == 'bar':
                        sl = l[t] if side == 'buy' else h[t]
                    elif stop == 'atr':
                        sl = c[t] - 1.5 * ctx.atr[t] if side == 'buy' else c[t] + 1.5 * ctx.atr[t]
                    else:  # last confirmed opposite swing
                        opp = pl if side == 'buy' else ph
                        cand = [j for j in opp if j + lb <= t]
                        if not cand:
                            break
                        sl = l[cand[-1]] if side == 'buy' else h[cand[-1]]
                    sigs.append((t, side, sl))
                break                              # one attempt per trendline (like live)
    sigs.sort()
    return sigs


def simulate(ctx, sigs, rr, minrisk, start, end, be=None):
    cs = ctx.cs
    n = len(cs)
    tr = []
    busy = -1
    for t, side, sl0 in sigs:
        if not (start <= cs[t]['t'] < end):
            continue
        j = t + 1
        if j >= n or j <= busy:
            continue
        buy = side == 'buy'
        ref = cs[t]['c']
        risk = (ref - sl0) if buy else (sl0 - ref)
        if risk <= 0 or risk / ref < minrisk:
            continue
        tp = ref + rr * risk if buy else ref - rr * risk
        raw = cs[j]['o']
        entry = raw * (1 + SLIP) if buy else raw * (1 - SLIP)
        sl = sl0
        reason = xraw = None
        k = j
        if (buy and raw <= sl) or (not buy and raw >= sl):
            reason, xraw = 'SL', raw
        else:
            for k in range(j, n):
                x = cs[k]
                if buy:
                    if x['l'] <= sl:
                        reason, xraw = 'SL', min(sl, x['o'])
                        break
                    if x['h'] >= tp:
                        reason, xraw = 'TP', max(tp, x['o']) if False else tp
                        break
                    if be and x['h'] >= ref + be * risk:
                        sl = max(sl, entry)
                else:
                    if x['h'] >= sl:
                        reason, xraw = 'SL', max(sl, x['o'])
                        break
                    if x['l'] <= tp:
                        reason, xraw = 'TP', tp
                        break
                    if be and x['l'] <= ref - be * risk:
                        sl = min(sl, entry)
            if reason is None:
                reason, xraw, k = 'END', cs[-1]['c'], n - 1
        xp = xraw * (1 - SLIP) if buy else xraw * (1 + SLIP)
        gross = ((xp - entry) if buy else (entry - xp)) * QTY
        fees = (entry + xp) * QTY * FEE
        tr.append({'t': cs[j]['t'], 'xt': cs[k]['t'], 'net': gross - fees, 'side': side})
        busy = k
    return tr


GRID = {
    'lb': [3, 5, 8],
    'ema': [0, 100, 200],
    'vol': [0, 1.2, 1.5],
    'stop': ['bar', 'swing', 'atr'],
    'minrisk': [0.0, 0.005],
    'rr': [2, 3, 4],
    'win': [3, 6],          # window = win * lb bars after the 2nd pivot
    'tlq': ['any', 'clean'],
}
KEYS = list(GRID)
BASE = {'lb': 5, 'ema': 200, 'vol': 1.5, 'stop': 'bar', 'minrisk': 0.0, 'rr': 2, 'win': 3, 'tlq': 'any'}


def name(cfg):
    return (f'lb{cfg["lb"]} ema{cfg["ema"] or "off"} vol{cfg["vol"] or "off"} {cfg["stop"]} '
            f'mr{cfg["minrisk"] * 100:.1f} rr{cfg["rr"]} w{cfg["win"]} {cfg["tlq"]}')


def neighbours(cfg):
    out = []
    for k in KEYS:
        vals = GRID[k]
        i = vals.index(cfg[k])
        for d in (-1, 1):
            if 0 <= i + d < len(vals):
                out.append(dict(cfg, **{k: vals[i + d]}))
    return out


def fmt(s):
    return (f'{s["n"]:>4} {s["wr"]:>5.1f}% {s["net"]:>+7,.0f} {s["pf"]:>5.2f} {s["dd"]:>6,.0f} {s["mpos"]:>2}/{s["mn"]:<2}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tf', default='1h')
    ap.add_argument('--top', type=int, default=12)
    ap.add_argument('--holdout', action='store_true', help='print holdout for the final picks (run once)')
    a = ap.parse_args()
    cs = rep.fetch_delta(a.tf, 1100)
    ctx = Ctx(cs)
    sig_cache = {}
    res = {}
    for vals in itertools.product(*GRID.values()):
        cfg = dict(zip(KEYS, vals))
        sk = (cfg['lb'], cfg['ema'], cfg['vol'], cfg['stop'], cfg['win'], cfg['tlq'])
        if sk not in sig_cache:
            sig_cache[sk] = gen_signals(ctx, cfg['lb'], cfg['ema'], cfg['vol'], cfg['stop'], cfg['win'] * cfg['lb'], cfg['tlq'])
        sg = sig_cache[sk]
        res[tuple(vals)] = {p: stats(simulate(ctx, sg, cfg['rr'], cfg['minrisk'], *PERIODS[p]))
                            for p in ('Y1', 'Y2')}

    def score(cfg):
        r = res[tuple(cfg[k] for k in KEYS)]
        return r
    h = f'{"n":>4} {"win%":>6} {"net$":>7} {"PF":>5} {"maxDD":>6} {"+mo":>5}'
    print(f'\nBTC {a.tf}: {len(res)} configs.  Selection on Y1 + Y2 only.')
    b = score(BASE)
    print(f'{"config":<44} | Y1 {h} | Y2 {h} | nbrs ok')
    print(f'{"BASELINE ~ live (" + name(BASE) + ")":<44} | {fmt(b["Y1"])} | {fmt(b["Y2"])} |')
    both = []
    for vals, r in res.items():
        if r['Y1']['net'] > 0 and r['Y2']['net'] > 0 and min(r['Y1']['n'], r['Y2']['n']) >= 20:
            cfg = dict(zip(KEYS, vals))
            nb = neighbours(cfg)
            ok = sum(1 for x in nb if all(res[tuple(x[k] for k in KEYS)][p]['net'] > 0 for p in ('Y1', 'Y2')))
            both.append((cfg, r, ok / len(nb)))
    print(f'Profitable in BOTH years (>= 20 trades each): {len(both)} / {len(res)}')
    # rank: robust neighbourhood first, then the weaker year's return / worst drawdown
    both.sort(key=lambda x: (-round(x[2], 1), -min(x[1]['Y1']['net'], x[1]['Y2']['net'])
                             / max(x[1]['Y1']['dd'], x[1]['Y2']['dd'], 1)))
    for cfg, r, nbr in both[:a.top]:
        print(f'{name(cfg):<44} | {fmt(r["Y1"])} | {fmt(r["Y2"])} | {nbr * 100:>4.0f}%')

    # one-factor effects
    print('\nOne-factor effect (avg net over all configs with that setting, Y1 / Y2):')
    for k in KEYS:
        parts = []
        for val in GRID[k]:
            rs = [r for vals, r in res.items() if vals[KEYS.index(k)] == val]
            parts.append(f'{val}: {sum(r["Y1"]["net"] for r in rs) / len(rs):+,.0f} / {sum(r["Y2"]["net"] for r in rs) / len(rs):+,.0f}')
        print(f'  {k:<8} ' + '   '.join(parts))

    if a.holdout and both:
        print('\nHOLDOUT 2024-01-01..2024-10-31 (never used for selection):')
        picks = [BASE] + [x[0] for x in both[:3]]
        for cfg in picks:
            sg = sig_cache[(cfg['lb'], cfg['ema'], cfg['vol'], cfg['stop'], cfg['win'], cfg['tlq'])]
            for be in (None, 2):
                s = stats(simulate(ctx, sg, cfg['rr'], cfg['minrisk'], *PERIODS['HOLDOUT'], be=be))
                s1 = stats(simulate(ctx, sg, cfg['rr'], cfg['minrisk'], *PERIODS['Y1'], be=be))
                s2 = stats(simulate(ctx, sg, cfg['rr'], cfg['minrisk'], *PERIODS['Y2'], be=be))
                print(f'  {name(cfg) + (" +BE@2R" if be else ""):<50} Y1 {fmt(s1)} | Y2 {fmt(s2)} | HOLDOUT {fmt(s)}')


if __name__ == '__main__':
    main()
