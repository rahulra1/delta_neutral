"""
Trend Follower v2 — daily EMA time-series momentum across liquid coins (a true "EMA trend follower").

Rules (signal at the daily close, execution at the next daily open):
  - universe each day: the K most liquid coins (Binance: 30-day avg quote volume; Delta: open
    interest in USD), with enough history; stablecoins / leveraged tokens excluded
  - LONG while EMA(fast) > EMA(slow); SHORT while below (if shorts are enabled)
  - optional BTC regime: longs only when BTC close > its EMA(slow), shorts only when below
  - exit: the EMA state flips (at next open), or a trailing stop 3 x ATR(20) from the best close
    (checked on the daily low/high; gap fills at the open)
  - sizing: notional = $100 / ATR20% (an ATR move ~ $100), capped at $2,000 per coin; total gross
    exposure capped at $30,000 (3x a $10k account) -> new entries skipped above the cap
  - one position per coin; a coin that leaves the universe is kept until its exit
Costs: taker 0.05% + 18% GST + 5bp slippage per side; funding: Delta = real FUNDING:<sym> history
(daily candle close = rate in % per 8h, x3 per day); Binance (spot data) = 0.01% per 8h charged to
longs and paid to shorts (typical perp funding).
Protocol (run each phase once):
  --phase design   : Binance 2018-01..2021-12, 48 configs, pick robust settings
  --phase check1   : Binance 2022-01..2024-03 for the given --pick ranks
  --phase check2   : Delta 2024-04..2026-10 for the given --pick ranks
"""
import argparse
import itertools
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, 'backtest', '.crypto_cache', 'tf2')
BIN = 'https://data-api.binance.vision/api/v3'
DELTA = 'https://api.india.delta.exchange'
DAY = 86400
FEE = 0.0005 * 1.18
SLIP = 0.0005
SCALE = 1.0                  # position-size multiplier (--scale)
STABLE = {'USDC', 'BUSD', 'TUSD', 'USDP', 'DAI', 'FDUSD', 'USDD', 'PAX', 'UST', 'EUR', 'GBP', 'AUD',
          'TRY', 'BRL', 'RUB', 'USDS', 'USDSB', 'SUSD', 'PYUSD', 'AEUR', 'EURI', 'XUSD', 'USD1', 'BKRW',
          'IDRT', 'BIDR', 'NGN', 'UAH', 'ZAR', 'PAXG', 'WBTC', 'WBETH', 'BFUSD', 'RLUSD', 'USDE'}


def d2t(s):
    return int(datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())


PHASES = {'design': ('binance', d2t('2018-01-01'), d2t('2022-01-01')),
          'check1': ('binance', d2t('2022-01-01'), d2t('2024-04-01')),
          'check2': ('delta', d2t('2024-04-15'), d2t('2026-11-01'))}

GRID = {
    'ema': [(10, 30), (20, 50), (50, 100)],
    'shorts': [0, 1],
    'regime': [0, 1],
    'exit': ['flip', 'trail3'],
    'K': [10, 20],
}
KEYS = list(GRID)


# ----------------------------------------------------------------------------- data
def _json(url, params, tries=6):
    for a in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code in (418, 429):
                time.sleep(5 + 5 * a)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(1 + a)
    return None


def binance_daily(sym):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f'bn_{sym}.json')
    if os.path.exists(path):
        return json.load(open(path))
    out, start = [], 1500000000000
    while True:
        rows = _json(f'{BIN}/klines', {'symbol': sym, 'interval': '1d', 'startTime': start, 'limit': 1000})
        if rows is None:
            return None
        if not rows:
            break
        out += [[r[0] // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7])] for r in rows]
        if len(rows) < 1000:
            break
        start = rows[-1][0] + DAY * 1000
    json.dump(out, open(path, 'w'))
    return out


def load_binance():
    ei = _json(f'{BIN}/exchangeInfo', {})
    syms = []
    for s in ei['symbols']:
        b = s['baseAsset']
        if s['quoteAsset'] != 'USDT' or b in STABLE or any(b.endswith(x) for x in ('UP', 'DOWN', 'BULL', 'BEAR')):
            continue
        syms.append(s['symbol'])
    with ThreadPoolExecutor(4) as ex:
        raw = dict(zip(syms, ex.map(binance_daily, syms)))
    raw = {k: v for k, v in raw.items() if v}
    return raw, 'BTCUSDT'


def load_delta():
    """Daily OHLC from cached hourly MARK history (backtest/.crypto_cache/etf), OI$ and funding."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ema_trend_follower_backtest import series
    end = int(time.time()) // 3600 * 3600
    start = end - 930 * DAY
    tick = _json(f'{DELTA}/v2/tickers', {'contract_types': 'perpetual_futures'})['result']
    cv = {t['symbol']: float(t.get('contract_value') or 0) for t in tick}
    syms = sorted(s for s in cv if cv[s] > 0)

    def fund(sym):
        os.makedirs(CACHE, exist_ok=True)
        path = os.path.join(CACHE, f'fund_{sym}.json')
        if os.path.exists(path):
            return json.load(open(path))
        r = _json(f'{DELTA}/v2/history/candles', {'resolution': '1d', 'symbol': f'FUNDING:{sym}', 'start': start, 'end': end})
        if r is None:
            return None
        out = {str(c['time']): float(c['close']) for c in (r.get('result') or [])}
        json.dump(out, open(path, 'w'))
        return out

    with ThreadPoolExecutor(8) as ex:
        marks = dict(zip(syms, ex.map(lambda s: series(f'MARK:{s}', start, end), syms)))
        ois = dict(zip(syms, ex.map(lambda s: series(f'OI:{s}', start, end), syms)))
        funds = dict(zip(syms, ex.map(fund, syms)))
    raw = {}
    for s in syms:
        m = marks.get(s) or {}
        days = {}
        for t in sorted(m):
            dk = t // DAY * DAY
            c, h, l = m[t]
            if dk not in days:
                days[dk] = [dk, c, h, l, c, 0.0]          # open approximated by first hourly close
            else:
                d = days[dk]
                d[2], d[3], d[4] = max(d[2], h), min(d[3], l), c
        o = ois.get(s) or {}
        for dk, d in days.items():
            oi_last = o.get(dk + DAY - 3600) or o.get(dk + DAY - 7200)
            # Delta OI candles are in underlying coins (= contracts x contract_value), so
            # USD OI = coins x price (verified against /v2/tickers oi_value_usd).
            d[5] = (oi_last[0] * d[4]) if oi_last else 0.0     # liquidity = OI in USD
        if days:
            raw[s] = [days[k] for k in sorted(days)]
    return raw, 'BTCUSD', funds


class Panel:
    def __init__(self, raw, btc, funds=None):
        self.syms = sorted(raw)
        t0 = min(r[0][0] for r in raw.values())
        t1 = max(r[-1][0] for r in raw.values())
        self.days = np.arange(t0, t1 + DAY, DAY)
        T, S = len(self.days), len(self.syms)
        self.o, self.h, self.l, self.c, self.liq = (np.full((T, S), np.nan) for _ in range(5))
        pos = {int(d): i for i, d in enumerate(self.days)}
        for j, s in enumerate(self.syms):
            for t, o, h, l, c, q in raw[s]:
                i = pos.get(int(t))
                if i is not None:
                    self.o[i, j], self.h[i, j], self.l[i, j], self.c[i, j], self.liq[i, j] = o, h, l, c, q
        self.btc = self.syms.index(btc)
        self.fund = np.zeros((T, S))                     # funding fraction per day paid by longs
        if funds is None:
            self.fund[:] = 0.0001 * 3
        else:
            for j, s in enumerate(self.syms):
                for t, rate in (funds.get(s) or {}).items():
                    i = pos.get(int(t))
                    if i is not None:
                        self.fund[i, j] = rate / 100 * 3
        self.age = np.cumsum(~np.isnan(self.c), axis=0)
        self._ema, self._atr, self._liq30 = {}, None, None

    def ema(self, p):
        if p not in self._ema:
            k = 2 / (p + 1)
            out = np.full_like(self.c, np.nan)
            prev = np.full(self.c.shape[1], np.nan)
            for t in range(len(self.days)):
                x = self.c[t]
                prev = np.where(np.isnan(prev), x, np.where(np.isnan(x), prev, x * k + prev * (1 - k)))
                out[t] = prev
            self._ema[p] = out
        return self._ema[p]

    def atr_pct(self):
        if self._atr is None:
            pc = np.vstack([np.full((1, self.c.shape[1]), np.nan), self.c[:-1]])
            tr = np.nanmax(np.stack([self.h - self.l, np.abs(self.h - pc), np.abs(self.l - pc)]), axis=0)
            out = np.full_like(tr, np.nan)
            a = np.full(tr.shape[1], np.nan)
            for t in range(len(self.days)):
                x = tr[t]
                a = np.where(np.isnan(a), x, np.where(np.isnan(x), a, (a * 19 + x) / 20))
                out[t] = a
            self._atr = out / self.c
        return self._atr

    def liq30(self):
        if self._liq30 is None:
            q = np.nan_to_num(self.liq)
            cs = np.cumsum(q, axis=0)
            out = cs.copy()
            out[30:] = cs[30:] - cs[:-30]
            self._liq30 = out / 30
        return self._liq30


# ----------------------------------------------------------------------------- simulation
def run(p, cfg, start, end):
    fast, slow = cfg['ema']
    ef, es = p.ema(fast), p.ema(slow)
    atrp = p.atr_pct()
    liq = p.liq30()
    T, S = p.c.shape
    pos = {}                                   # j -> dict(side, qty, entry, best, notional, t)
    trades, eq_curve = [], []
    realized = 0.0
    i0 = int(np.searchsorted(p.days, start))
    i1 = int(np.searchsorted(p.days, end))
    for t in range(max(i0, slow + 25), min(i1, T - 1)):
        # ---- intraday (day t+1) trailing stops first, using day t+1 range ----
        u = t + 1
        for j in list(pos):
            P = pos[j]
            if cfg['exit'] != 'trail3' or np.isnan(p.l[u, j]):
                continue
            stop = P['best'] * (1 - 3 * P['atr']) if P['side'] > 0 else P['best'] * (1 + 3 * P['atr'])
            if (P['side'] > 0 and p.l[u, j] <= stop) or (P['side'] < 0 and p.h[u, j] >= stop):
                o = p.o[u, j]
                px = min(stop, o) if P['side'] > 0 else max(stop, o)
                realized += close(P, px, trades, p.days[u], 'TRAIL')
                del pos[j]
        # ---- signals at close t ----
        btc_up = p.c[t, p.btc] > es[t, p.btc]
        live = np.where((p.age[t] > slow + 20) & ~np.isnan(p.c[t]) & ~np.isnan(p.o[u]))[0]
        uni = set(live[np.argsort(-liq[t, live])][:cfg['K']].tolist())
        for j in list(pos):                    # state-flip exits at next open
            P = pos[j]
            state = 1 if ef[t, j] > es[t, j] else -1
            if state != P['side'] and not np.isnan(p.o[u, j]):
                realized += close(P, p.o[u, j], trades, p.days[u], 'FLIP')
                del pos[j]
        gross = sum(P['notional'] for P in pos.values())
        for j in sorted(uni, key=lambda x: -liq[t, x]):
            if j in pos or np.isnan(atrp[t, j]) or atrp[t, j] <= 0:
                continue
            state = 1 if ef[t, j] > es[t, j] else -1
            if state < 0 and not cfg['shorts']:
                continue
            if cfg['regime'] and ((state > 0 and not btc_up) or (state < 0 and btc_up)):
                continue
            notional = min(2000.0 * SCALE, 100.0 * SCALE / atrp[t, j])
            if gross + notional > 30000 * SCALE:
                continue
            o = p.o[u, j]
            fill = o * (1 + SLIP) if state > 0 else o * (1 - SLIP)
            pos[j] = {'side': state, 'qty': notional / fill, 'entry': fill, 'best': o, 'atr': atrp[t, j],
                      'notional': notional, 't': p.days[u], 'j': j, 'fund': 0.0}
            gross += notional
        # ---- end of day t+1: funding, trailing reference, mark-to-market ----
        mtm = 0.0
        for j, P in pos.items():
            c = p.c[u, j]
            if np.isnan(c):
                continue
            P['fund'] += P['side'] * P['qty'] * c * p.fund[u, j]          # longs pay positive funding
            P['best'] = max(P['best'], c) if P['side'] > 0 else min(P['best'], c)
            P['atr'] = atrp[u, j] if not np.isnan(atrp[u, j]) else P['atr']
            mtm += P['side'] * (c - P['entry']) * P['qty'] - P['fund'] - P['entry'] * P['qty'] * FEE
        eq_curve.append((p.days[u], realized + mtm, sum(P['notional'] for P in pos.values())))
    for P in list(pos.values()):               # mark open positions at the end (not closed)
        pass
    return trades, eq_curve, realized


def close(P, raw_px, trades, t, why):
    side = P['side']
    xp = raw_px * (1 - SLIP) if side > 0 else raw_px * (1 + SLIP)
    gross = side * (xp - P['entry']) * P['qty']
    fees = (P['entry'] + xp) * P['qty'] * FEE
    net = gross - fees - P['fund']
    trades.append({'t': P['t'], 'xt': t, 'j': P['j'], 'side': side, 'gross': gross, 'fees': fees,
                   'fund': P['fund'], 'net': net, 'why': why})
    return net


def summary(trades, curve, start, end):
    yrs = (end - start) / (365 * DAY)
    total = curve[-1][1] if curve else 0.0
    eq = np.array([x[1] for x in curve]) if curve else np.zeros(1)
    peak = np.maximum.accumulate(np.maximum(eq, 0))
    dd = float(np.max(peak - eq)) if len(eq) else 0.0
    ddp = float(np.max((peak - eq) / (10_000 + peak)) * 100) if len(eq) else 0.0   # % of peak account equity
    nets = np.array([x['net'] for x in trades]) if trades else np.zeros(0)
    gw, gl = nets[nets > 0].sum(), -nets[nets < 0].sum()
    top5 = np.sort(nets)[-5:].sum() if len(nets) >= 5 else nets.sum()
    by_year = {}
    for x in trades:
        y = datetime.fromtimestamp(x['xt'], tz=timezone.utc).year
        by_year[y] = by_year.get(y, 0) + x['net']
    return dict(total=total, per_yr=total / yrs, dd=dd, ddp=ddp, n=len(trades), pf=gw / gl if gl else 99,
                wr=float((nets > 0).mean() * 100) if len(nets) else 0, ex_top5=float(nets.sum() - top5),
                fees=sum(x['fees'] for x in trades), fund=sum(x['fund'] for x in trades),
                exp=max((x[2] for x in curve), default=0), by_year=by_year)


def fmt(s):
    return (f'{s["n"]:>5} tr  win {s["wr"]:4.1f}%  PF {s["pf"]:4.2f}  total ${s["total"]:+8,.0f} '
            f'(${s["per_yr"]:+7,.0f}/yr = {s["per_yr"] / 100:+5.1f}%/yr on $10k)  maxDD ${s["dd"]:7,.0f} '
            f'({s["ddp"]:4.1f}% of peak equity)  ex-top5 ${s["ex_top5"]:+8,.0f}')


def name(cfg):
    return (f'EMA{cfg["ema"][0]}/{cfg["ema"][1]} {"long+short" if cfg["shorts"] else "long-only "} '
            f'{"BTC-regime" if cfg["regime"] else "no-regime "} {cfg["exit"]:<6} K={cfg["K"]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--phase', default='design', choices=list(PHASES))
    ap.add_argument('--pick', default='', help='design ranks to evaluate in check phases')
    ap.add_argument('--top', type=int, default=48)
    ap.add_argument('--scale', type=float, default=1.0, help='size multiplier (1 = $100 per ATR, cap $2k/coin, $30k total)')
    a = ap.parse_args()
    global SCALE
    SCALE = a.scale
    venue, start, end = PHASES[a.phase]
    t0 = time.time()
    if venue == 'binance':
        raw, btc = load_binance()
        p = Panel(raw, btc)
    else:
        raw, btc, funds = load_delta()
        p = Panel(raw, btc, funds)
    print(f'{a.phase}: {venue} {len(p.syms)} coins, {datetime.fromtimestamp(start, tz=timezone.utc):%Y-%m-%d} -> '
          f'{datetime.fromtimestamp(end, tz=timezone.utc):%Y-%m-%d} (loaded in {time.time() - t0:.0f}s)')
    cfgs = [dict(zip(KEYS, v)) for v in itertools.product(*GRID.values())]
    rank_file = os.path.join(CACHE, 'design_ranking.json')
    if a.phase == 'design':
        rows = []
        for cfg in cfgs:
            tr, cv, _ = run(p, cfg, start, end)
            rows.append((cfg, summary(tr, cv, start, end)))
        rows.sort(key=lambda r: -(r[1]['per_yr'] / max(r[1]['dd'], 1)))
        json.dump([{**{k: list(v) if isinstance(v, tuple) else v for k, v in c.items()}} for c, _ in rows],
                  open(rank_file, 'w'))
        for i, (cfg, s) in enumerate(rows[:a.top], 1):
            print(f'{i:>3}. {name(cfg)} | {fmt(s)}')
        print('\nOne-factor effect (avg %/yr on $10k, avg maxDD):')
        for k in KEYS:
            parts = []
            for v in GRID[k]:
                ss = [s for c, s in rows if c[k] == v]
                parts.append(f'{v}: {np.mean([s["per_yr"] for s in ss]) / 100:+.1f}%/yr DD {np.mean([s["dd"] for s in ss]) / 100:.0f}%')
            print(f'  {k:<7} ' + '   '.join(parts))
    else:
        ranking = json.load(open(rank_file))
        picks = [int(x) for x in a.pick.split(',')]
        for r in picks:
            cfg = {k: (tuple(v) if k == 'ema' else v) for k, v in ranking[r - 1].items()}
            tr, cv, _ = run(p, cfg, start, end)
            s = summary(tr, cv, start, end)
            yrs = ' '.join(f'{y}: ${v:+,.0f}' for y, v in sorted(s['by_year'].items()))
            print(f'#{r:<2} {name(cfg)} | {fmt(s)}\n     fees ${s["fees"]:,.0f} funding ${s["fund"]:+,.0f} '
                  f'max exposure ${s["exp"]:,.0f} | by year: {yrs}')


if __name__ == '__main__':
    main()
