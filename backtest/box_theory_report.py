"""
Box Theory (api.chart.calc_box_theory) — real trade backtest on Delta perps + improvement search.

Data: Delta 5m candles (history starts ~2024-03-01); 15m / 1h candles are aggregated from the
5m bars, and every exit is checked on 5m bars (SL first when a 5m bar touches both).

Modes
  --mode live   : the deployed rule, two ways
      closed   : signals on CLOSED candles only (the best case), entry at the next open
      intrabar : what strategy/futures_signal_trader.py really does. calc_box_theory does not
                 drop the forming candle, so every scan (approximated by every 5m close) it sees
                 a partial candle; a signal that appears on it is traded at once at market even
                 if the candle later closes red. Legs stack (box_theory is not in
                 ONE_POSITION_KEYS), max 10 entries per UTC day. A TP already on the wrong side
                 of the entry is closed at once (the trader's next TP check).
  --mode grid   : improvement search on closed candles, one position at a time (selection on
                  BTC Y1 + Y2 only).
  --mode check  : one-time checks of chosen configs: BTC HOLDOUT, ETH (all periods).

Costs: taker 0.05% + 18% GST per side, slippage 1bp per side, no funding.
Size: BTC 0.1 BTC (100 lots); other symbols the same $ notional as 0.1 BTC at entry time.
Periods: HOLDOUT 2024-03-01..2024-10-31 (5m history limit), Y1 2024-10-31..2025-10-31,
         Y2 2025-10-31..2026-11-01.

Usage:
  python backtest/box_theory_report.py --mode live
  python backtest/box_theory_report.py --mode grid
  python backtest/box_theory_report.py --mode check --cfg "1h,0.25,live,candle,mid,0.003,1.0,off,1"
"""
import argparse
import itertools
import multiprocessing as mp
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btc_signal_strategy_report as rep  # noqa: E402
from api.chart import calc_box_theory  # noqa: E402

D = lambda s: int(datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())  # noqa: E731
PERIODS = {'HOLDOUT': (D('2024-03-01'), D('2024-10-31')),
           'Y1': (D('2024-10-31'), D('2025-10-31')),
           'Y2': (D('2025-10-31'), D('2026-11-01'))}
DAYS = 975
FEE = 0.0005 * 1.18
SLIP = 0.0001
QTY_BTC = 0.1
EQ0 = 10_000.0
TF = {'15m': 900, '1h': 3600}


# ----------------------------------------------------------------------------- data
class Data:
    def __init__(self, sym, btc5=None):
        b = rep.fetch_delta('5m', DAYS, symbol=sym)
        self.sym = sym
        self.T = [x['t'] for x in b]
        self.O = [x['o'] for x in b]
        self.H = [x['h'] for x in b]
        self.L = [x['l'] for x in b]
        self.C = [x['c'] for x in b]
        self.V = [x['v'] for x in b]
        self.n = len(b)
        # $ notional per trade = 0.1 BTC at that time (BTC itself: fixed 0.1 BTC)
        if btc5 is not None:
            bt = {t: c for t, c in zip(btc5.T, btc5.C)}
            last = btc5.C[0]
            self.btc_px = []
            for t in self.T:
                last = bt.get(t, last)
                self.btc_px.append(last)
        else:
            self.btc_px = None
        self.tf = {}
        for name, sec in TF.items():
            cs, ranges = [], []
            k = 0
            while k < self.n:
                t0 = self.T[k] // sec * sec
                k1 = k
                while k1 + 1 < self.n and self.T[k1 + 1] < t0 + sec:
                    k1 += 1
                cs.append({'t': t0, 'o': self.O[k], 'h': max(self.H[k:k1 + 1]),
                           'l': min(self.L[k:k1 + 1]), 'c': self.C[k1], 'v': sum(self.V[k:k1 + 1])})
                ranges.append((k, k1))
                k = k1 + 1
            self.tf[name] = (cs, ranges)

    def qty(self, k, px):
        if self.btc_px is None:
            return QTY_BTC
        return QTY_BTC * self.btc_px[k] / px


def day_of(t):
    return t // 86400


def boxes(cs):
    """prev-day high/low per day (previous day present in the data, like the live fn)."""
    days = defaultdict(list)
    for c in cs:
        days[day_of(c['t'])].append(c)
    keys = sorted(days)
    out = {}
    for a, b in zip(keys, keys[1:]):
        out[b] = (max(c['h'] for c in days[a]), min(c['l'] for c in days[a]))
    return out


def avg20(vols):
    out, s = [], 0.0
    for i, v in enumerate(vols):
        s += v
        if i >= 20:
            s -= vols[i - 20]
        out.append(s / min(i + 1, 20))
    return out


# ----------------------------------------------------------------------------- exits on 5m
def simulate(d, ke, side, sl, tp, expire_t=None):
    """Entry at the open of 5m bar ke. Returns (exit index, raw exit px, reason)."""
    if ke >= d.n:
        return None
    e = d.O[ke]
    if (side == 'buy' and tp <= e) or (side == 'sell' and tp >= e):
        return ke, e, 'TP<entry'           # trader closes it on its next TP check
    if (side == 'buy' and e <= sl) or (side == 'sell' and e >= sl):
        return ke, e, 'SL'
    for j in range(ke, d.n):
        if expire_t is not None and d.T[j] >= expire_t:
            return j, d.O[j], 'EOD'
        o = d.O[j]
        if side == 'buy':
            if j > ke and o <= sl:
                return j, o, 'SL'
            if d.L[j] <= sl:
                return j, sl, 'SL'
            if j > ke and o >= tp:
                return j, o, 'TP'
            if d.H[j] >= tp:
                return j, tp, 'TP'
        else:
            if j > ke and o >= sl:
                return j, o, 'SL'
            if d.H[j] >= sl:
                return j, sl, 'SL'
            if j > ke and o <= tp:
                return j, o, 'TP'
            if d.L[j] <= tp:
                return j, tp, 'TP'
    return None                              # still open at the end: excluded


def make_trade(d, ke, side, sl, tp, expire_t=None):
    r = simulate(d, ke, side, sl, tp, expire_t)
    if r is None:
        return None
    xi, xraw, reason = r
    eraw = d.O[ke]
    q = d.qty(ke, eraw)
    en = eraw * (1 + SLIP) if side == 'buy' else eraw * (1 - SLIP)
    ex = xraw * (1 - SLIP) if side == 'buy' else xraw * (1 + SLIP)
    gross = (ex - en) * q if side == 'buy' else (en - ex) * q
    fees = (en + ex) * q * FEE
    return {'side': side, 'ke': ke, 'xi': xi, 'entry_t': d.T[ke], 'exit_t': d.T[xi], 'entry': en,
            'exit': ex, 'sl': sl, 'tp': tp, 'reason': reason, 'gross': gross, 'fees': fees,
            'net': gross - fees}


# ----------------------------------------------------------------------------- live rule
def live_eval(c, box, avg, flags):
    """calc_box_theory's per-candle rule. flags = [buy_triggered, sell_triggered] (mutated)."""
    hi, lo = box
    rng = hi - lo
    if rng <= 0:
        return []
    mid = round((hi + lo) / 2, 2)
    sz, bz = hi - rng * 0.25, lo + rng * 0.25
    out = []
    if not flags[0] and c['l'] <= bz and c['c'] > c['o'] and c['c'] > bz:
        if avg > 0 and c['v'] < avg:
            return out
        if c['c'] - c['l'] <= 0:
            return out
        out.append({'time': c['t'], 'type': 'buy', 'price': round(c['c'], 2), 'sl': round(c['l'], 2), 'tp1': mid})
        flags[0] = True
    if not flags[1] and c['h'] >= sz and c['c'] < c['o'] and c['c'] < sz:
        if avg > 0 and c['v'] < avg:
            return out
        if c['h'] - c['c'] <= 0:
            return out
        out.append({'time': c['t'], 'type': 'sell', 'price': round(c['c'], 2), 'sl': round(c['h'], 2), 'tp1': mid})
        flags[1] = True
    return out


def live_closed_signals(cs):
    bx = boxes(cs)
    av = avg20([c['v'] for c in cs])
    sigs, flags, cur = [], None, None
    for i, c in enumerate(cs):
        dd = day_of(c['t'])
        if dd != cur:
            cur, flags = dd, [False, False]
        if dd in bx:
            sigs.extend(live_eval(c, bx[dd], av[i], flags))
    return sigs


def live_intrabar_signals(d, tfname):
    """Replay of the 30s trader scan on the forming candle (approximated at each 5m close).
    Returns (signal, entry 5m index) in the order the trader would take them."""
    cs, ranges = d.tf[tfname]
    sec = TF[tfname]
    bx = boxes(cs)
    vols = [c['v'] for c in cs]
    out = []
    last_sig_t = 0
    cur = None
    for j, c in enumerate(cs):
        dd = day_of(c['t'])
        if dd != cur:
            cur, closed_flags, last_closed_sig = dd, [False, False], None
        if dd not in bx:
            continue
        box = bx[dd]
        k0, k1 = ranges[j]
        prev19 = vols[max(0, j - 19):j]
        for k in range(k0, k1 + 1):
            part = {'t': c['t'], 'o': d.O[k0], 'h': max(d.H[k0:k + 1]), 'l': min(d.L[k0:k + 1]),
                    'c': d.C[k], 'v': sum(d.V[k0:k + 1])}
            avg = (sum(prev19) + part['v']) / (len(prev19) + 1)
            new = live_eval(part, box, avg, list(closed_flags))
            latest = new[-1] if new else last_closed_sig
            now = d.T[k] + 300
            if latest and latest['time'] > last_sig_t and now - latest['time'] <= 2 * sec:
                last_sig_t = latest['time']
                out.append((latest, k + 1))
        full = live_eval(c, box, (sum(prev19) + c['v']) / (len(prev19) + 1), closed_flags)
        if full:
            last_closed_sig = full[-1]
    return out


# ----------------------------------------------------------------------------- stats/report
def pf(xs):
    gw = sum(x for x in xs if x > 0)
    gl = -sum(x for x in xs if x < 0)
    return gw / gl if gl else (99.0 if gw else 0.0)


def stats(trades):
    nets = [t['net'] for t in trades]
    eq = peak = EQ0
    mdd = 0.0
    for t in sorted(trades, key=lambda x: (x['exit_t'], x['entry_t'])):
        eq += t['net']
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    n = len(trades)
    return {'n': n, 'wr': sum(1 for x in nets if x > 0) / n * 100 if n else 0.0,
            'gross': sum(t['gross'] for t in trades), 'fees': sum(t['fees'] for t in trades),
            'net': sum(nets), 'pf': pf(nets), 'mdd': mdd}


def split(trades):
    return {p: [t for t in trades if s <= t['entry_t'] < e] for p, (s, e) in PERIODS.items()}


def print_report(title, trades, months=True):
    print('\n' + '=' * 96)
    print(title)
    print('=' * 96)
    print(f'{"Period":<9}{"Trades":>7}{"Win%":>7}{"Gross$":>11}{"Fees$":>10}{"Net$":>11}{"PF":>6}{"MaxDD$":>10}')
    for p, ts in split(trades).items():
        s = stats(ts)
        print(f'{p:<9}{s["n"]:>7}{s["wr"]:>6.1f}%{s["gross"]:>+11,.0f}{s["fees"]:>10,.0f}{s["net"]:>+11,.0f}'
              f'{s["pf"]:>6.2f}{s["mdd"]:>10,.0f}')
    s = stats(trades)
    print(f'{"ALL":<9}{s["n"]:>7}{s["wr"]:>6.1f}%{s["gross"]:>+11,.0f}{s["fees"]:>10,.0f}{s["net"]:>+11,.0f}'
          f'{s["pf"]:>6.2f}{s["mdd"]:>10,.0f}')
    if not trades:
        return
    rs = defaultdict(int)
    for t in trades:
        rs[t['reason']] += 1
    risk = [abs(t['entry'] - t['sl']) / t['entry'] * 100 for t in trades]
    print('  Exits: ' + ', '.join(f'{k} {v}' for k, v in sorted(rs.items()))
          + f' | avg stop {sum(risk) / len(risk):.3f}% | avg fees/trade ${s["fees"] / s["n"]:.2f}')
    for side in ('buy', 'sell'):
        st = stats([t for t in trades if t['side'] == side])
        print(f'  {side.upper():<5}{st["n"]:>5} trades  win {st["wr"]:5.1f}%  net ${st["net"]:+,.0f}  PF {st["pf"]:.2f}')
    if months:
        mm = defaultdict(list)
        for t in trades:
            mm[datetime.fromtimestamp(t['entry_t'], tz=timezone.utc).strftime('%Y-%m')].append(t)
        line, cum, pos = [], 0.0, 0
        for m in sorted(mm):
            v = sum(t['net'] for t in mm[m])
            cum += v
            pos += v > 0
            line.append(f'{m[2:]}:{v:+.0f}')
        print(f'  Months (net $) — profitable {pos}/{len(mm)}:')
        for i in range(0, len(line), 8):
            print('    ' + '  '.join(f'{x:<14}' for x in line[i:i + 8]))


# ----------------------------------------------------------------------------- live mode
def engine(d, entries, mode, cap=10):
    """entries: list of (sig, ke) sorted by time. mode 'stack' (live) or 'one'."""
    trades, busy, per_day = [], -1, defaultdict(int)
    for sig, ke in entries:
        if ke >= d.n:
            continue
        if mode == 'one' and ke <= busy:
            continue
        day = day_of(d.T[ke])
        if mode == 'stack':
            if per_day[day] >= cap:
                continue
        t = make_trade(d, ke, sig['type'], sig['sl'], sig['tp1'])
        if t is None:
            continue
        per_day[day] += 1
        busy = t['xi']
        trades.append(t)
    return trades


def max_conc(trades):
    ev = sorted([(t['ke'], 1) for t in trades] + [(t['xi'], -1) for t in trades], key=lambda e: (e[0], -e[1]))
    cur = mx = 0
    for _, x in ev:
        cur += x
        mx = max(mx, cur)
    return mx


def mode_live(syms):
    btc = Data('BTCUSD')
    for sym in syms:
        d = btc if sym == 'BTCUSD' else Data(sym, btc)
        for tfn in ('15m', '1h'):
            cs, ranges = d.tf[tfn]
            # parity: my rule re-implementation == the live function on closed candles
            mine = live_closed_signals(cs)
            live = calc_box_theory(cs)['signals']
            key = lambda s: (s['time'], s['type'], s['sl'], s['tp1'])  # noqa: E731
            ok = [key(s) for s in mine] == [key(s) for s in live]
            print(f'\n[{sym} {tfn}] parity with api.chart.calc_box_theory on closed candles: '
                  f'{"IDENTICAL" if ok else "MISMATCH"} ({len(live)} signals)')
            kmap = {c['t']: ranges[j][1] + 1 for j, c in enumerate(cs)}
            closed = [(s, kmap[s['time']]) for s in live]
            intra = live_intrabar_signals(d, tfn)
            for lbl, ent, md in (('LIVE rule, closed candles, one position', closed, 'one'),
                                 ('LIVE rule, closed candles, legs stack (trader)', closed, 'stack'),
                                 ('LIVE TRADER replay: forming candle + legs stack', intra, 'stack')):
                tr = engine(d, ent, md)
                print_report(f'{sym} {tfn} | {lbl} | signals {len(ent)} | max open legs {max_conc(tr)}', tr)


# ----------------------------------------------------------------------------- improved rule
GRID = {
    'tf': ['15m', '1h'],
    'zone': [0.15, 0.25, 0.35],
    'vol': ['off', 'live'],
    'stop': ['candle', 'box'],
    'target': ['mid', 'far', 'r2'],
    'minrisk': [0.0, 0.003, 0.006],
    'minrr': [0.0, 1.0],
    'trend': ['off', 'with'],
    'eod': [0, 1],
}
KEYS = list(GRID)


def parse(s):
    vals = s.split(',')
    out = {}
    for k, v in zip(KEYS, vals):
        typ = type(GRID[k][0])
        out[k] = typ(v) if typ is not str else v
    return out


def fmt(p):
    return ','.join(str(p[k]) for k in KEYS)


def ema(xs, p):
    k = 2 / (p + 1)
    o = [xs[0]]
    for x in xs[1:]:
        o.append(x * k + o[-1] * (1 - k))
    return o


_PREP = {}


def prep(d, tfn):
    key = (d.sym, tfn)
    if key not in _PREP:
        cs, ranges = d.tf[tfn]
        _PREP[key] = (cs, ranges, boxes(cs), avg20([c['v'] for c in cs]), ema([c['c'] for c in cs], 200))
    return _PREP[key]


def improved_trades(d, p):
    """Closed candles only; first touch per side per day; entry next open; one position."""
    cs, ranges, bx, av, em = prep(d, p['tf'])
    trades, busy = [], -1
    cur = None
    for i, c in enumerate(cs):
        dd = day_of(c['t'])
        if dd != cur:
            cur, flags = dd, [False, False]
        if dd not in bx or i < 200:
            continue
        hi, lo = bx[dd]
        rng = hi - lo
        if rng <= 0:
            continue
        z = p['zone']
        bz, sz, mid = lo + rng * z, hi - rng * z, (hi + lo) / 2
        for side in ('buy', 'sell'):
            fi = 0 if side == 'buy' else 1
            if flags[fi]:
                continue
            if side == 'buy':
                touch = c['l'] <= bz and c['c'] > c['o'] and c['c'] > bz
            else:
                touch = c['h'] >= sz and c['c'] < c['o'] and c['c'] < sz
            if not touch:
                continue
            flags[fi] = True                    # first touch per side per day, filters or not
            if p['vol'] == 'live' and av[i] > 0 and c['v'] < av[i]:
                continue
            if p['trend'] == 'with' and ((side == 'buy' and c['c'] <= em[i]) or (side == 'sell' and c['c'] >= em[i])):
                continue
            e = c['c']
            if side == 'buy':
                sl = c['l'] if p['stop'] == 'candle' else min(c['l'], lo - 0.1 * rng)
                risk = e - sl
                tp = {'mid': mid, 'far': sz, 'r2': e + 2 * risk}[p['target']]
                reward = tp - e
            else:
                sl = c['h'] if p['stop'] == 'candle' else max(c['h'], hi + 0.1 * rng)
                risk = sl - e
                tp = {'mid': mid, 'far': bz, 'r2': e - 2 * risk}[p['target']]
                reward = e - tp
            if risk <= 0 or reward <= 0 or risk / e < p['minrisk'] or reward / risk < p['minrr']:
                continue
            ke = ranges[i][1] + 1
            if ke <= busy:
                continue
            t = make_trade(d, ke, side, sl, tp, (dd + 1) * 86400 if p['eod'] else None)
            if t is None:
                continue
            busy = t['xi']
            trades.append(t)
    return trades


_D = None


def _init(sym):
    global _D
    _D = Data(sym)


def _job(cfg):
    p = parse(cfg)
    sp = split(improved_trades(_D, p))
    return cfg, {k: stats(v) for k, v in sp.items() if k != 'HOLDOUT'}


def neighbours(p):
    for k in KEYS:
        vals = GRID[k]
        i = vals.index(p[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                q = dict(p)
                q[k] = vals[j]
                yield fmt(q)


def mode_grid():
    cfgs = [fmt(dict(zip(KEYS, v))) for v in itertools.product(*GRID.values())]
    Data('BTCUSD')                                   # warm the cache once
    print(f'{len(cfgs)} configs, selecting on BTC Y1 + Y2 (HOLDOUT not computed)')
    with mp.get_context('fork').Pool(max(1, os.cpu_count() - 1), _init, ('BTCUSD',)) as pool:
        res = dict(pool.imap_unordered(_job, cfgs, chunksize=8))

    def good(c):
        r = res[c]
        return all(r[y]['net'] > 0 and r[y]['n'] >= 30 for y in ('Y1', 'Y2'))

    passed = [c for c in cfgs if good(c)]
    print(f'Profitable in BOTH years (>= 30 trades each): {len(passed)}/{len(cfgs)}')
    rows = []
    for c in passed:
        nb = list(neighbours(parse(c)))
        rob = sum(good(x) for x in nb) / len(nb)
        r = res[c]
        worst = min(r[y]['net'] / max(r[y]['mdd'], 1) for y in ('Y1', 'Y2'))
        rows.append((rob, worst, c))
    rows.sort(reverse=True)
    print(f'\n{"config (" + ",".join(KEYS) + ")":<58}{"robust":>7}  {"Y1 n/net/PF/DD":<26}{"Y2 n/net/PF/DD":<26}')
    for rob, worst, c in rows[:30]:
        r = res[c]
        f = lambda s: f'{s["n"]}/{s["net"]:+.0f}/{s["pf"]:.2f}/{s["mdd"]:.0f}'  # noqa: E731
        print(f'{c:<58}{rob:>6.0%}  {f(r["Y1"]):<26}{f(r["Y2"]):<26}')
    # how often each option appears among the passing configs (what actually helps)
    print('\nShare of configs that pass, per option value:')
    for k in KEYS:
        parts = []
        for v in GRID[k]:
            tot = [c for c in cfgs if parse(c)[k] == v]
            parts.append(f'{v}: {sum(good(c) for c in tot) / len(tot):.0%}')
        print(f'  {k:<8} ' + '  '.join(parts))
    # live-like baseline inside this engine
    for tfn in ('15m', '1h'):
        b = f'{tfn},0.25,live,candle,mid,0.0,0.0,off,0'
        r = res[b]
        print(f'Baseline-like ({b}): Y1 net {r["Y1"]["net"]:+.0f} PF {r["Y1"]["pf"]:.2f} | '
              f'Y2 net {r["Y2"]["net"]:+.0f} PF {r["Y2"]["pf"]:.2f}')


def mode_check(cfgs):
    btc = Data('BTCUSD')
    eth = Data('ETHUSD', btc)
    for c in cfgs:
        p = parse(c)
        print_report(f'BTC | {c}', improved_trades(btc, p))
        print_report(f'ETH (one-time check, $ notional = 0.1 BTC) | {c}', improved_trades(eth, p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', default='live', choices=['live', 'grid', 'check'])
    ap.add_argument('--syms', default='BTCUSD,ETHUSD')
    ap.add_argument('--cfg', action='append', default=[])
    a = ap.parse_args()
    if a.mode == 'live':
        mode_live(a.syms.split(','))
    elif a.mode == 'grid':
        mode_grid()
    else:
        mode_check(a.cfg)


if __name__ == '__main__':
    main()
