"""
SMA + Breakout v2 — second-round improvements (BTC 1h, Delta BTCUSD perp).

Base = btc_signal_strategy_report.calc_sma_vol_breakout_v2 (SMA50, vol > 1.5x avg20,
stop >= 0.5% of price, TP 4R), one position at a time, stop-entry fill, SL first on
ambiguous bars, taker 0.05% + GST + 1bp slip on every leg (same as the report engine).

Periods:
  Y1 = 2024-10-31 .. 2025-10-31, Y2 = 2025-10-31 .. 2026-10-31  (used to choose changes)
  HOLDOUT = 2024-01-01 .. 2024-10-31  (never used for choosing; shown only with --holdout)

Usage:
  python backtest/sma_v2_improve.py                 # variants on Y1 / Y2
  python backtest/sma_v2_improve.py --holdout       # also print the untouched period
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btc_signal_strategy_report as rep  # noqa: E402

QTY = 0.1
FEE = 0.0005 * 1.18
SLIP = 0.0001
EQ0 = 10_000.0
D = lambda s: int(datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())  # noqa: E731
PERIODS = {'HOLDOUT': (D('2024-01-01'), D('2024-10-31')),
           'Y1': (D('2024-10-31'), D('2025-10-31')),
           'Y2': (D('2025-10-31'), D('2026-11-01'))}


# ----------------------------------------------------------------------------- indicators
def ema(xs, p):
    k = 2 / (p + 1)
    o = [xs[0]]
    for x in xs[1:]:
        o.append(x * k + o[-1] * (1 - k))
    return o


def sma(xs, p):
    o = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= p:
            s -= xs[i - p]
        if i >= p - 1:
            o[i] = s / p
    return o


def adx(cs, p=14):
    n = len(cs)
    out = [0.0] * n
    tr_s = pdm_s = ndm_s = 0.0
    dx_s = None
    for i in range(1, n):
        h, l, pc = cs[i]['h'], cs[i]['l'], cs[i - 1]['c']
        up, dn = h - cs[i - 1]['h'], cs[i - 1]['l'] - l
        pdm = up if up > dn and up > 0 else 0.0
        ndm = dn if dn > up and dn > 0 else 0.0
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_s = tr_s - tr_s / p + tr
        pdm_s = pdm_s - pdm_s / p + pdm
        ndm_s = ndm_s - ndm_s / p + ndm
        if tr_s <= 0:
            continue
        pdi, ndi = 100 * pdm_s / tr_s, 100 * ndm_s / tr_s
        dx = 100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0
        dx_s = dx if dx_s is None else (dx_s * (p - 1) + dx) / p
        out[i] = dx_s
    return out


class Ctx:
    def __init__(self, cs):
        c = [x['c'] for x in cs]
        self.cs = cs
        self.sma50 = sma(c, 50)
        self.ema200 = ema(c, 200)
        self.ema800 = ema(c, 800)          # ~ 33-day trend on 1h (~ daily EMA33)
        self.adx = adx(cs)
        self.idx = {x['t']: i for i, x in enumerate(cs)}


# ----------------------------------------------------------------------------- engine
def run(ctx, sigs, start, end, v):
    """v: dict of variant options."""
    cs = ctx.cs
    n = len(cs)
    trades = []
    busy = -1
    for s in sigs:
        j = ctx.idx.get(s['time'])            # breakout (fill) bar
        if j is None or not (start <= cs[j]['t'] < end) or j <= busy:
            continue
        i = j - 1                              # setup bar
        buy = s['type'] == 'buy'
        lvl, sl0 = s['price'], s['sl']
        risk_pct = abs(lvl - sl0) / lvl
        # ---- entry filters (all use data up to the setup bar close) ----
        if v.get('max_risk') and risk_pct > v['max_risk']:
            continue
        if v.get('trend') == 'ema200' and ((buy and cs[i]['c'] < ctx.ema200[i]) or (not buy and cs[i]['c'] > ctx.ema200[i])):
            continue
        if v.get('trend') == 'ema800' and ((buy and cs[i]['c'] < ctx.ema800[i]) or (not buy and cs[i]['c'] > ctx.ema800[i])):
            continue
        if v.get('adx') and ctx.adx[i] < v['adx']:
            continue
        if v.get('adx_max') and ctx.adx[i] > v['adx_max']:
            continue
        if v.get('slope'):
            k = v['slope']
            sl_ = ctx.sma50[i] - ctx.sma50[i - k]
            if (buy and sl_ <= 0) or (not buy and sl_ >= 0):
                continue
        rr = v.get('rr', 4)
        risk = abs(lvl - sl0)
        # position size: fixed 0.1 BTC, or fixed $ risk per trade (lots rounded, capped)
        qty = QTY
        if v.get('risk_usd'):
            lots = max(1, min(v.get('max_lots', 300), round(v['risk_usd'] / (risk * 0.001))))
            qty = lots * 0.001
        raw = max(cs[j]['o'], lvl) if buy else min(cs[j]['o'], lvl)
        tp = lvl + rr * risk if buy else lvl - rr * risk
        entry = raw * (1 + SLIP) if buy else raw * (1 - SLIP)
        sl = sl0
        legs = []                              # (fraction, exit raw px, reason)
        open_frac = 1.0
        partial_done = False
        be_at = v.get('be')                    # move SL to entry after +be R
        trail_after = v.get('trail_after')     # after +X R trail SL at (best - trail_r R)
        best = raw
        k = j
        for k in range(j, n):
            x = cs[k]
            hi, lo = x['h'], x['l']
            # stop first (worst case)
            if (buy and lo <= sl) or (not buy and hi >= sl):
                px = (min(sl, x['o']) if buy else max(sl, x['o'])) if k > j else sl
                legs.append((open_frac, px, 'SL' if sl == sl0 else 'BE/TRAIL'))
                open_frac = 0
                break
            if v.get('partial') and not partial_done:
                p_r, p_frac = v['partial']
                p_px = lvl + p_r * risk if buy else lvl - p_r * risk
                if (buy and hi >= p_px) or (not buy and lo <= p_px):
                    legs.append((p_frac, p_px, 'PART'))
                    open_frac -= p_frac
                    partial_done = True
                    if v.get('be_after_partial'):
                        sl = raw
            if (buy and hi >= tp) or (not buy and lo <= tp):
                legs.append((open_frac, tp, 'TP'))
                open_frac = 0
                break
            best = max(best, hi) if buy else min(best, lo)
            if be_at and ((buy and best >= raw + be_at * risk) or (not buy and best <= raw - be_at * risk)):
                sl = max(sl, raw) if buy else min(sl, raw)
            if trail_after and ((buy and best >= raw + trail_after * risk) or (not buy and best <= raw - trail_after * risk)):
                t_sl = best - v['trail_r'] * risk if buy else best + v['trail_r'] * risk
                sl = max(sl, t_sl) if buy else min(sl, t_sl)
            if v.get('time_stop') and k - j + 1 >= v['time_stop']:
                legs.append((open_frac, x['c'], 'TIME'))
                open_frac = 0
                break
        if open_frac > 0:
            legs.append((open_frac, cs[-1]['c'], 'END'))
        gross = fees = 0.0
        for frac, px, _ in legs:
            xp = px * (1 - SLIP) if buy else px * (1 + SLIP)
            gross += ((xp - entry) if buy else (entry - xp)) * qty * frac
            fees += xp * qty * frac * FEE
        fees += entry * qty * FEE
        trades.append({'t': cs[j]['t'], 'xt': cs[k]['t'], 'net': gross - fees, 'side': s['type'],
                       'reason': legs[-1][2], 'lots': round(qty * 1000)})
        busy = k
    return trades


def stats(tr):
    if not tr:
        return dict(n=0, net=0, pf=0, wr=0, dd=0, ddp=0, mpos=0, mn=0, ret_dd=0)
    nets = [t['net'] for t in tr]
    gw = sum(x for x in nets if x > 0)
    gl = -sum(x for x in nets if x < 0)
    eq = pk = EQ0
    dd = ddp = 0.0
    for t in sorted(tr, key=lambda x: x['xt']):
        eq += t['net']
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
        ddp = max(ddp, (pk - eq) / pk * 100)
    mo = defaultdict(float)
    for t in tr:
        mo[datetime.fromtimestamp(t['t'], tz=timezone.utc).strftime('%Y-%m')] += t['net']
    net = sum(nets)
    return dict(n=len(tr), net=net, pf=gw / gl if gl else 99, wr=sum(1 for x in nets if x > 0) / len(tr) * 100,
                dd=dd, ddp=ddp, mpos=sum(1 for x in mo.values() if x > 0), mn=len(mo),
                ret_dd=net / dd if dd else 99)


VARIANTS = [
    ('BASE v2', {}),
    # --- trend / regime filters ---
    ('trend: EMA200', {'trend': 'ema200'}),
    ('trend: EMA800 (~33d)', {'trend': 'ema800'}),
    ('SMA50 slope 5 bars', {'slope': 5}),
    ('SMA50 slope 10 bars', {'slope': 10}),
    ('ADX >= 15', {'adx': 15}),
    ('ADX >= 20', {'adx': 20}),
    ('ADX >= 25', {'adx': 25}),
    ('ADX <= 40', {'adx_max': 40}),
    # --- risk caps ---
    ('max stop 2.5%', {'max_risk': 0.025}),
    ('max stop 2.0%', {'max_risk': 0.020}),
    ('max stop 1.5%', {'max_risk': 0.015}),
    # --- trade management ---
    ('breakeven @ 1R', {'be': 1}),
    ('breakeven @ 1.5R', {'be': 1.5}),
    ('breakeven @ 2R', {'be': 2}),
    ('breakeven @ 3R', {'be': 3}),
    ('partial 50% @ 2R', {'partial': (2, 0.5)}),
    ('partial 50% @ 2R + BE', {'partial': (2, 0.5), 'be_after_partial': True}),
    ('partial 33% @ 2R + BE', {'partial': (2, 1 / 3), 'be_after_partial': True}),
    ('trail 1.5R after 2R', {'trail_after': 2, 'trail_r': 1.5}),
    ('trail 2R after 2.5R', {'trail_after': 2.5, 'trail_r': 2}),
    ('trail 1R after 3R', {'trail_after': 3, 'trail_r': 1}),
    ('time stop 72 bars', {'time_stop': 72}),
    ('time stop 120 bars', {'time_stop': 120}),
    ('TP 5R', {'rr': 5}),
    ('TP 6R + trail 2R after 3R', {'rr': 6, 'trail_after': 3, 'trail_r': 2}),
    # --- position sizing (fixed $ risk per trade instead of fixed 100 lots) ---
    ('size: risk $100/trade', {'risk_usd': 100}),
    ('size: risk $125/trade', {'risk_usd': 125}),
    ('size: risk $150/trade', {'risk_usd': 150}),
    ('size: $125 risk + BE@2R', {'risk_usd': 125, 'be': 2}),
    ('size: $150 risk + BE@2R', {'risk_usd': 150, 'be': 2}),
]


def fmt(s):
    return (f'{s["n"]:>4} {s["wr"]:>5.1f}% {s["net"]:>+8,.0f} {s["pf"]:>5.2f} {s["dd"]:>6,.0f} '
            f'{s["ddp"]:>5.1f}% {s["ret_dd"]:>5.2f} {s["mpos"]:>2}/{s["mn"]:<2}')


def main(variants=VARIANTS):
    ap = argparse.ArgumentParser()
    ap.add_argument('--holdout', action='store_true')
    ap.add_argument('--only', default='', help='comma list of variant names to run')
    a = ap.parse_args()
    cs = rep.fetch_delta('1h', 1100)
    ctx = Ctx(cs)
    sigs = rep.calc_sma_vol_breakout_v2(cs)['signals']
    pers = ['Y1', 'Y2'] + (['HOLDOUT'] if a.holdout else [])
    h = f'{"n":>4} {"win%":>6} {"net$":>8} {"PF":>5} {"maxDD":>6} {"DD%":>6} {"R/DD":>5} {"+mo":>5}'
    print(f'{"variant":<28} | ' + ' | '.join(f'{p:<8}{h}' for p in pers) + ' | Y1+Y2 net   worst DD')
    only = [x.strip() for x in a.only.split(',') if x.strip()]
    for name, v in variants:
        if only and name not in only:
            continue
        ss = {p: stats(run(ctx, sigs, *PERIODS[p], v)) for p in pers}
        print(f'{name:<28} | ' + ' | '.join(f'{"":<8}{fmt(ss[p])}' for p in pers)
              + f' | {ss["Y1"]["net"] + ss["Y2"]["net"]:>+8,.0f}   {max(ss["Y1"]["dd"], ss["Y2"]["dd"]):>6,.0f}')


if __name__ == '__main__':
    main()
