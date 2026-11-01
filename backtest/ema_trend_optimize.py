"""
EMA Trend Follower — improvement search (market-wide Delta perps, hourly), two independent checks.

Protocol:
  SELECT : coin half A (every other symbol in sorted order), Y1 (2024-11..2025-10) + Y2 (2025-11..2026-10)
  CHECK 1: coin half B, Y1 + Y2 (other coins, same period)      } run once with --check
  CHECK 2: all coins, 2024-04..2024-10 (earliest Delta history)  }
Dimensions:
  look   : 24h | 168h ranking window (mark-price change)
  mode   : mom (live: long gainers / short losers) | rev (long losers / short gainers = buy dips, fade rallies)
  regime : btc200 (live: BTC 1h close vs EMA200 picks the side) | off (both sides every evaluation)
  topn   : 5 | 20 | 50
  oi     : 0 | 10 (6h OI-contract change > +10%, live)
  liq    : 0 | 250k (open interest >= $250k USD)
  exit   : tp/sl/max-hold — 10/5/none (live) | 5/5/none | 10/10/none | 20/10/none | 10/5/72h
  freq   : 1h (live) | 24h (re-rank once a day at 00:00 UTC)
Execution as the live code: entry at the evaluation hour's mark close, $100 in integer lots, skip if the
same symbol+side is open, 24h cooldown after an exit, TP/SL on hourly mark high/low (SL first).
Costs: taker 0.05% + 18% GST + 5bp slippage per side. No funding. Universe = perps listed today.

Usage: python backtest/ema_trend_optimize.py [--check 1,2,3]
"""
import argparse
import itertools
import os
import sys
import time

import numpy as np
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ema_trend_follower_backtest import series, ema_series, B, H  # noqa: E402

FEE = 0.0005 * 1.18
SLIP = 0.0005
NOTIONAL = 100.0
COOLDOWN = 24
DAY = 86400


def ts_of(s):
    import datetime as dt
    return int(dt.datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=dt.timezone.utc).timestamp())


PERIODS = {'HO': (ts_of('2024-04-01'), ts_of('2024-11-01')),
           'Y1': (ts_of('2024-11-01'), ts_of('2025-11-01')),
           'Y2': (ts_of('2025-11-01'), ts_of('2026-11-02'))}

GRID = {
    'look': [24, 168],
    'mode': ['mom', 'rev'],
    'regime': ['btc200', 'off'],
    'topn': [5, 20, 50],
    'oi': [0, 10],
    'liq': [0, 250_000],
    'exit': [(0.10, 0.05, 0), (0.05, 0.05, 0), (0.10, 0.10, 0), (0.20, 0.10, 0), (0.10, 0.05, 72)],
    'freq': [1, 24],
}
KEYS = list(GRID)
BASE = {'look': 24, 'mode': 'mom', 'regime': 'btc200', 'topn': 50, 'oi': 10, 'liq': 0,
        'exit': (0.10, 0.05, 0), 'freq': 1}
SIG_KEYS = ('look', 'mode', 'regime', 'topn', 'oi', 'liq', 'freq')


class Data:
    def __init__(self, days=930, workers=8):
        end = int(time.time()) // H * H
        start = end - days * DAY
        tick = requests.get(f'{B}/v2/tickers', params={'contract_types': 'perpetual_futures'}, timeout=30).json()['result']
        cv = {t['symbol']: float(t.get('contract_value') or 0) for t in tick}
        self.syms = sorted(s for s in cv if cv[s] > 0)
        from concurrent.futures import ThreadPoolExecutor
        jobs = [f'MARK:{s}' for s in self.syms] + [f'OI:{s}' for s in self.syms] + ['BTCUSD']
        with ThreadPoolExecutor(workers) as ex:
            raw = dict(zip(jobs, ex.map(lambda j: series(j, start, end), jobs)))
        self.hours = np.arange(start, end, H)
        T, S = len(self.hours), len(self.syms)
        self.c = np.full((T, S), np.nan)
        self.h = np.full((T, S), np.nan)
        self.l = np.full((T, S), np.nan)
        self.oi = np.full((T, S), np.nan)
        pos = {int(h): i for i, h in enumerate(self.hours)}
        for j, s in enumerate(self.syms):
            for t, (c, hi, lo) in (raw.get(f'MARK:{s}') or {}).items():
                i = pos.get(t)
                if i is not None:
                    self.c[i, j], self.h[i, j], self.l[i, j] = c, hi, lo
            for t, (o, _, _) in (raw.get(f'OI:{s}') or {}).items():
                i = pos.get(t)
                if i is not None:
                    self.oi[i, j] = o
        self.cv = np.array([cv[s] for s in self.syms])
        btc = raw['BTCUSD'] or {}
        bc, last = [], np.nan
        for h in self.hours:
            if int(h) in btc:
                last = btc[int(h)][0]
            bc.append(last)
        bc = np.array(bc)
        first = int(np.argmax(~np.isnan(bc)))
        e = np.full(T, np.nan)
        e[first:] = [np.nan if v is None else v for v in ema_series(list(bc[first:]), 200)]
        self.btc_up = np.where(np.isnan(e), 0, np.where(bc > e, 1, -1))       # +1 longs / -1 shorts
        with np.errstate(invalid='ignore', divide='ignore'):
            self.chg = {L: np.vstack([np.full((L, S), np.nan), self.c[L:] / self.c[:-L] - 1]) for L in (24, 168)}
            self.oi6 = np.vstack([np.full((6, S), np.nan), self.oi[6:] / self.oi[:-6] - 1]) * 100
            self.oi_usd = self.oi * self.cv[None, :] * self.c
        self.half = np.array([j % 2 for j in range(S)])                     # 0 = A, 1 = B


def candidates(d, cfg):
    """Return list of (t_index, sym_index, side) entry candidates, time-ordered."""
    chg = d.chg[cfg['look']]
    T, S = chg.shape
    out = []
    n = cfg['topn']
    for t in range(200, T - 1):
        if cfg['freq'] == 24 and d.hours[t] % DAY != DAY - H:          # evaluate at the 23:00 close
            continue
        row = chg[t]
        ok = ~np.isnan(row)
        if cfg['oi']:
            ok &= np.nan_to_num(d.oi6[t], nan=-1e9) > cfg['oi']
        if cfg['liq']:
            ok &= np.nan_to_num(d.oi_usd[t], nan=0) >= cfg['liq']
        valid = np.where(~np.isnan(row))[0]
        if len(valid) == 0:
            continue
        order = valid[np.argsort(-row[valid])]                         # biggest gainers first
        gainers, losers = order[:n], order[::-1][:n]
        sides = []
        reg = d.btc_up[t]
        if cfg['regime'] == 'off' or reg == 1:
            sides.append(('buy', gainers if cfg['mode'] == 'mom' else losers))
        if cfg['regime'] == 'off' or reg == -1:
            sides.append(('sell', losers if cfg['mode'] == 'mom' else gainers))
        for side, idx in sides:
            for j in idx:
                if ok[j]:
                    out.append((t, int(j), side))
    return out


def simulate(d, cands, exit_cfg):
    tp, sl, maxh = exit_cfg
    free = {}                                                           # (j, side) -> first t allowed
    trades = []
    T = len(d.hours)
    for t, j, side in cands:
        if free.get((j, side), -1) > t:
            continue
        px = d.c[t, j]
        if not np.isfinite(px) or px <= 0:
            continue
        lots = max(1, int(round(NOTIONAL / (px * d.cv[j]))))
        qty = lots * d.cv[j]
        buy = side == 'buy'
        fill = px * (1 + SLIP) if buy else px * (1 - SLIP)
        sl_px = px * (1 - sl) if buy else px * (1 + sl)
        tp_px = px * (1 + tp) if buy else px * (1 - tp)
        stop_at = min(T - 1, t + maxh) if maxh else T - 1
        k0 = t + 1
        hit_k, reason, xraw = None, None, None
        while k0 <= stop_at:
            k1 = min(stop_at, k0 + 720)
            hi, lo = d.h[k0:k1 + 1, j], d.l[k0:k1 + 1, j]
            with np.errstate(invalid='ignore'):
                s_hit = (lo <= sl_px) if buy else (hi >= sl_px)
                t_hit = (hi >= tp_px) if buy else (lo <= tp_px)
            any_hit = s_hit | t_hit
            if any_hit.any():
                i = int(np.argmax(any_hit))
                hit_k = k0 + i
                reason, xraw = ('SL', sl_px) if s_hit[i] else ('TP', tp_px)
                break
            k0 = k1 + 1
        if hit_k is None:
            hit_k = stop_at
            seg = d.c[t + 1:stop_at + 1, j]
            fin = seg[np.isfinite(seg)]
            xraw = fin[-1] if len(fin) else px
            reason = 'TIME' if maxh and stop_at < T - 1 else 'END'
        xp = xraw * (1 - SLIP) if buy else xraw * (1 + SLIP)
        gross = ((xp - fill) if buy else (fill - xp)) * qty
        net = gross - (fill + xp) * qty * FEE
        trades.append((int(d.hours[t]), int(d.hours[hit_k]), j, side, net, reason))
        free[(j, side)] = hit_k + COOLDOWN
    return trades


def stats(trades, period, half=None, d=None):
    a, b = PERIODS[period]
    sel = [tr for tr in trades if a <= tr[0] < b and (half is None or d.half[tr[2]] == half)]
    if not sel:
        return dict(n=0, net=0.0, pf=0.0, wr=0.0, dd=0.0)
    nets = np.array([tr[4] for tr in sel])
    order = np.argsort([tr[1] for tr in sel])
    eq = np.cumsum(nets[order])
    dd = float(np.max(np.maximum.accumulate(np.maximum(eq, 0)) - eq)) if len(eq) else 0.0
    gw, gl = nets[nets > 0].sum(), -nets[nets < 0].sum()
    return dict(n=len(sel), net=float(nets.sum()), pf=float(gw / gl) if gl else 99.0,
                wr=float((nets > 0).mean() * 100), dd=dd)


def fmt(s):
    return f'{s["n"]:>6} {s["wr"]:>5.1f}% {s["net"]:>+8,.0f} {s["pf"]:>5.2f} {s["dd"]:>6,.0f}'


def name(cfg):
    parts = []
    for k in KEYS:
        if cfg[k] != BASE[k]:
            v = cfg[k]
            parts.append(f'{k}={v[0]*100:g}/{v[1]*100:g}/{v[2] or "-"}' if k == 'exit' else f'{k}={v}')
    return ' '.join(parts) or 'BASELINE (live rules)'


def neighbours(cfg):
    out = []
    for k in KEYS:
        vals = GRID[k]
        i = vals.index(cfg[k])
        for dlt in (-1, 1):
            if 0 <= i + dlt < len(vals):
                out.append(dict(cfg, **{k: vals[i + dlt]}))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--top', type=int, default=15)
    ap.add_argument('--min-trades', type=int, default=150)
    ap.add_argument('--check', default='')
    a = ap.parse_args()
    t0 = time.time()
    d = Data()
    print(f'Loaded {len(d.syms)} perps x {len(d.hours)} hours in {time.time() - t0:.0f}s '
          f'(half A {int((d.half == 0).sum())} coins, half B {int((d.half == 1).sum())})')
    cache, res, trades_of = {}, {}, {}
    t0 = time.time()
    for vals in itertools.product(*GRID.values()):
        cfg = dict(zip(KEYS, vals))
        sk = tuple(cfg[k] for k in SIG_KEYS)
        if sk not in cache:
            cache[sk] = candidates(d, cfg)
        tr = simulate(d, cache[sk], cfg['exit'])
        trades_of[vals] = tr
        res[vals] = {p: stats(tr, p, 0, d) for p in ('Y1', 'Y2')}
    print(f'{len(res)} configs simulated in {time.time() - t0:.0f}s')

    base_tr = trades_of[tuple(BASE[k] for k in KEYS)]
    print('\nSanity: live rules, ALL coins: ' + ' | '.join(f'{p} {fmt(stats(base_tr, p))}' for p in ('Y1', 'Y2')))
    h = f'{"n":>6} {"win%":>6} {"net$":>8} {"PF":>5} {"maxDD":>6}'
    print(f'\nSELECTION on coin half A.  {"config":<58} | Y1 {h} | Y2 {h} | nbrs')
    b = res[tuple(BASE[k] for k in KEYS)]
    print(f'{"":<27}{"BASELINE (live rules)":<58} | {fmt(b["Y1"])} | {fmt(b["Y2"])} |')
    good = []
    for vals, r in res.items():
        if all(r[p]['net'] > 0 and r[p]['n'] >= a.min_trades for p in ('Y1', 'Y2')):
            cfg = dict(zip(KEYS, vals))
            nb = neighbours(cfg)
            ok = sum(1 for x in nb if all(res[tuple(x[k] for k in KEYS)][p]['net'] > 0 for p in ('Y1', 'Y2')))
            good.append((cfg, r, ok / len(nb)))
    print(f'Profitable in both years on half A (>= {a.min_trades} trades each): {len(good)} / {len(res)}')
    good.sort(key=lambda g: (-round(g[2], 1), -min(g[1][p]['pf'] for p in ('Y1', 'Y2'))))
    for rank, (cfg, r, nbr) in enumerate(good[:a.top], 1):
        print(f'{rank:>3}. {name(cfg):<80} | {fmt(r["Y1"])} | {fmt(r["Y2"])} | {nbr * 100:3.0f}%')
    print('\nOne-factor effect on half A (avg net per config, Y1 / Y2):')
    for k in KEYS:
        parts = []
        for v in GRID[k]:
            rs = [r for vals, r in res.items() if vals[KEYS.index(k)] == v]
            lab = f'{v[0]*100:g}/{v[1]*100:g}/{v[2] or "-"}' if k == 'exit' else v
            parts.append(f'{lab}: {np.mean([r["Y1"]["net"] for r in rs]):+,.0f} / {np.mean([r["Y2"]["net"] for r in rs]):+,.0f}')
        print(f'  {k:<7} ' + '   '.join(parts))

    if a.check:
        print('\n=== ONE-TIME CHECKS: coin half B (Y1, Y2) and ALL coins Apr-Oct 2024 (HO) ===')
        for cfg in [BASE] + [good[int(x) - 1][0] for x in a.check.split(',')]:
            tr = trades_of[tuple(cfg[k] for k in KEYS)]
            print(f'  {name(cfg):<80} | B-Y1 {fmt(stats(tr, "Y1", 1, d))} | B-Y2 {fmt(stats(tr, "Y2", 1, d))} | HO {fmt(stats(tr, "HO"))}')


if __name__ == '__main__':
    main()
