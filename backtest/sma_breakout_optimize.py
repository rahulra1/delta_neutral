"""
SMA + Breakout — improvement search with out-of-sample validation (BTC, Delta BTCUSD perp).

Base strategy (= live api.chart.calc_sma_vol_breakout Setup 1):
  bar i closes across the SMA (prev close on the other side), volume filter passes;
  stop entry at bar i high (long) / low (short), valid on bar i+1 only;
  SL = other extreme of bar i; TP = 2R.

Variations searched (one position at a time, stop entry = taker fee):
  sma      : 20 / 50 / 100
  vol      : none | live (v>avg20 & v>prev) | 1.5x (v>1.5*avg20)
  trend    : off | ema200 (long only above EMA200, short only below)
  stop     : bar (signal-bar extreme) | atr (entry -/+ 1.5*ATR14)
  minrisk  : 0 | 0.5% (skip trades whose stop is closer than 0.5% of price -> fee drag)
  exit     : rr2 | rr3 (fixed TP, resting limit -> maker fee)
             trail (no TP; exit on a close back across the SMA; SL stays)
             be3 (TP 3R, SL moved to breakeven after +1R)
Fees: taker 0.05% / maker 0.02%, each + 18% GST; 1bp slippage on taker legs. No funding.
Fill bar: SL checked first (worst case).

Train = first 365 days, Test = last 365 days. Configs are ranked on TRAIN only;
TEST shows what that choice would have done on unseen data.

Usage: python backtest/sma_breakout_optimize.py [--tf 1h,15m] [--top 10]
"""
import argparse
import itertools
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btc_signal_strategy_report as rep  # noqa: E402  (data loader)

QTY = 0.1
TAKER = 0.0005 * 1.18
MAKER = 0.0002 * 1.18
SLIP = 0.0001
EQUITY = 10_000.0


def sma(xs, p):
    out = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= p:
            s -= xs[i - p]
        if i >= p - 1:
            out[i] = s / p
    return out


def ema(xs, p):
    k = 2 / (p + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def atr(cs, p=14):
    out = [0.0] * len(cs)
    tr = [cs[0]['h'] - cs[0]['l']]
    for i in range(1, len(cs)):
        tr.append(max(cs[i]['h'] - cs[i]['l'], abs(cs[i]['h'] - cs[i - 1]['c']), abs(cs[i]['l'] - cs[i - 1]['c'])))
    a = sum(tr[:p]) / p
    for i in range(p, len(cs)):
        a = (a * (p - 1) + tr[i]) / p
        out[i] = a
    return out


class Data:
    def __init__(self, cs):
        self.cs = cs
        self.c = [x['c'] for x in cs]
        self.v = [x['v'] for x in cs]
        self.avg = [0.0] * len(cs)
        s = 0.0
        for i, x in enumerate(self.v):           # 20-bar avg incl. current bar (as live)
            s += x
            if i >= 20:
                s -= self.v[i - 20]
            self.avg[i] = max(1.0, s / min(i + 1, 20))
        self.ema200 = ema(self.c, 200)
        self.atr = atr(cs)
        self.sma = {p: sma(self.c, p) for p in (20, 50, 100)}


def signals(d, p, vol, trend):
    cs, c, v, avg, m = d.cs, d.c, d.v, d.avg, d.sma[p]
    out = []
    for i in range(max(p, 200) + 1, len(cs) - 1):
        if m[i - 1] is None:
            continue
        up = c[i - 1] < m[i - 1] and c[i] > m[i]
        dn = c[i - 1] > m[i - 1] and c[i] < m[i]
        if not (up or dn):
            continue
        if vol == 'live' and not (v[i] > avg[i] and v[i] > v[i - 1]):
            continue
        if vol == '1.5x' and not v[i] > 1.5 * avg[i]:
            continue
        if trend == 'ema200':
            if up and c[i] < d.ema200[i]:
                continue
            if dn and c[i] > d.ema200[i]:
                continue
        out.append((i, 'buy' if up else 'sell'))
    return out


def simulate(d, sigs, p, stop, minrisk, exit_, start_t, end_t):
    cs, m = d.cs, d.sma[p]
    trades = []
    busy = -1
    n = len(cs)
    for i, side in sigs:
        t0 = cs[i]['t']
        if t0 < start_t or t0 >= end_t:
            continue
        j = i + 1
        if j <= busy:
            continue
        b = cs[j]
        buy = side == 'buy'
        lvl = cs[i]['h'] if buy else cs[i]['l']
        if (buy and b['h'] < lvl) or (not buy and b['l'] > lvl):
            continue                                  # breakout never triggered
        raw = max(b['o'], lvl) if buy else min(b['o'], lvl)
        if stop == 'bar':
            sl = cs[i]['l'] if buy else cs[i]['h']
        else:
            a = d.atr[i] * 1.5
            sl = raw - a if buy else raw + a
        risk = (raw - sl) if buy else (sl - raw)
        if risk <= 0 or risk / raw < minrisk:
            continue
        rr = {'rr2': 2, 'rr3': 3, 'be3': 3}.get(exit_)
        tp = (raw + rr * risk if buy else raw - rr * risk) if rr else None
        entry = raw * (1 + SLIP) if buy else raw * (1 - SLIP)
        cur_sl = sl
        reason = xraw = None
        k = j
        for k in range(j, n):
            x = cs[k]
            if buy:
                if x['l'] <= cur_sl:
                    reason, xraw = 'SL', (min(cur_sl, x['o']) if k > j else cur_sl)
                    break
                if tp and x['h'] >= tp:
                    reason, xraw = 'TP', tp
                    break
            else:
                if x['h'] >= cur_sl:
                    reason, xraw = 'SL', (max(cur_sl, x['o']) if k > j else cur_sl)
                    break
                if tp and x['l'] <= tp:
                    reason, xraw = 'TP', tp
                    break
            if exit_ == 'trail' and k > j and m[k] is not None:
                if (buy and x['c'] < m[k]) or (not buy and x['c'] > m[k]):
                    reason, xraw = 'TRAIL', x['c']
                    break
            if exit_ == 'be3':
                if buy and x['h'] >= raw + risk:
                    cur_sl = max(cur_sl, raw)
                if not buy and x['l'] <= raw - risk:
                    cur_sl = min(cur_sl, raw)
        if reason is None:
            reason, xraw, k = 'END', cs[-1]['c'], n - 1
        if reason == 'TP':
            xpx, xfee = xraw, MAKER
        else:
            xpx, xfee = (xraw * (1 - SLIP) if buy else xraw * (1 + SLIP)), TAKER
        gross = ((xpx - entry) if buy else (entry - xpx)) * QTY
        fees = entry * QTY * TAKER + xpx * QTY * xfee
        trades.append({'t': cs[j]['t'], 'xt': cs[k]['t'], 'side': side, 'gross': gross,
                       'fees': fees, 'net': gross - fees, 'reason': reason})
        busy = k
    return trades


def stats(tr):
    if not tr:
        return {'n': 0, 'net': 0, 'gross': 0, 'pf': 0, 'wr': 0, 'dd': 0, 'months_pos': 0, 'months': 0}
    nets = [t['net'] for t in tr]
    gw = sum(x for x in nets if x > 0)
    gl = -sum(x for x in nets if x < 0)
    eq = pk = EQUITY
    dd = 0.0
    for t in sorted(tr, key=lambda x: x['xt']):
        eq += t['net']
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    mo = defaultdict(float)
    for t in tr:
        mo[datetime.fromtimestamp(t['t'], tz=timezone.utc).strftime('%Y-%m')] += t['net']
    return {'n': len(tr), 'net': sum(nets), 'gross': sum(t['gross'] for t in tr),
            'pf': gw / gl if gl else float('inf'), 'wr': sum(1 for x in nets if x > 0) / len(tr) * 100,
            'dd': dd, 'months_pos': sum(1 for x in mo.values() if x > 0), 'months': len(mo),
            'long': sum(t['net'] for t in tr if t['side'] == 'buy'),
            'short': sum(t['net'] for t in tr if t['side'] == 'sell')}


def fmt(s):
    return (f'{s["n"]:>4} {s["wr"]:>5.1f}% {s["gross"]:>+9,.0f} {s["net"]:>+9,.0f} {s["pf"]:>5.2f} '
            f'{s["dd"]:>7,.0f} {s["months_pos"]:>2}/{s["months"]:<2}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tf', default='1h,15m')
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--min-trades', type=int, default=30)
    a = ap.parse_args()

    grid = list(itertools.product([20, 50, 100], ['none', 'live', '1.5x'], ['off', 'ema200'],
                                  ['bar', 'atr'], [0.0, 0.005], ['rr2', 'rr3', 'trail', 'be3']))
    hdr = f'{"n":>4} {"win%":>6} {"gross$":>9} {"net$":>9} {"PF":>5} {"maxDD":>7} {"+mo":>5}'
    for tf in a.tf.split(','):
        cs = rep.fetch_delta(tf, 730)
        d = Data(cs)
        split = cs[0]['t'] + 365 * 86400
        end = cs[-1]['t'] + 1
        print(f'\n{"=" * 118}\nBTC {tf}: train {rep.ts(cs[0]["t"])} -> {rep.ts(split)}  |  test {rep.ts(split)} -> {rep.ts(cs[-1]["t"])}'
              f'  |  {len(grid)} configs\n{"=" * 118}')
        cache = {}
        rows = []
        for p, vol, trend, stop, mr, ex in grid:
            key = (p, vol, trend)
            if key not in cache:
                cache[key] = signals(d, p, vol, trend)
            sg = cache[key]
            tr = simulate(d, sg, p, stop, mr, ex, cs[0]['t'], split)
            te = simulate(d, sg, p, stop, mr, ex, split, end)
            rows.append(((p, vol, trend, stop, mr, ex), stats(tr), stats(te)))

        base = [r for r in rows if r[0] == (50, 'live', 'off', 'bar', 0.0, 'rr2')][0]
        print(f'{"config (sma/vol/trend/stop/minrisk/exit)":<40} | TRAIN {hdr} | TEST  {hdr}')
        print(f'{"BASELINE = live strategy":<40} | {fmt(base[1]):<52} | {fmt(base[2])}')
        print('-' * 118)
        ok = [r for r in rows if r[1]['n'] >= a.min_trades]
        ok.sort(key=lambda r: -r[1]['net'])
        for cfg, s1, s2 in ok[:a.top]:
            name = f'{cfg[0]}/{cfg[1]}/{cfg[2]}/{cfg[3]}/{cfg[4] * 100:.1f}%/{cfg[5]}'
            print(f'{name:<40} | {fmt(s1):<52} | {fmt(s2)}')

        # robustness: how many configs are profitable in BOTH halves, and test of train-top-N
        both = [r for r in ok if r[1]['net'] > 0 and r[2]['net'] > 0]
        topn = ok[:a.top]
        print(f'\n  Configs with >= {a.min_trades} train trades: {len(ok)}; profitable in train: '
              f'{sum(1 for r in ok if r[1]["net"] > 0)}; in test: {sum(1 for r in ok if r[2]["net"] > 0)}; '
              f'in BOTH: {len(both)}')
        print(f'  Train-top-{a.top}: avg TEST net ${sum(r[2]["net"] for r in topn) / len(topn):+,.0f}, '
              f'{sum(1 for r in topn if r[2]["net"] > 0)}/{len(topn)} profitable out-of-sample')
        if both:
            print('  Profitable in BOTH halves:')
            for cfg, s1, s2 in sorted(both, key=lambda r: -(r[1]['net'] + r[2]['net'])):
                name = f'{cfg[0]}/{cfg[1]}/{cfg[2]}/{cfg[3]}/{cfg[4] * 100:.1f}%/{cfg[5]}'
                print(f'    {name:<38} | {fmt(s1):<52} | {fmt(s2)}  long {s2["long"]:+,.0f} short {s2["short"]:+,.0f}')

        # single-factor effects (avg TEST net across all configs having that value)
        print('\n  One-factor effect (average net over all configs with that setting; train / test):')
        names = ['sma', 'vol', 'trend', 'stop', 'minrisk', 'exit']
        for fi, fname in enumerate(names):
            vals = sorted({r[0][fi] for r in rows}, key=str)
            parts = []
            for val in vals:
                rs = [r for r in rows if r[0][fi] == val]
                parts.append(f'{val}: {sum(r[1]["net"] for r in rs) / len(rs):+,.0f} / {sum(r[2]["net"] for r in rs) / len(rs):+,.0f}')
            print(f'    {fname:<8} ' + '   '.join(parts))


if __name__ == '__main__':
    main()
