"""
Parity check: live strategy/ema_trend_follower.py decision logic vs the validated backtest
(backtest/trend_follower_v2.py, config #1 at --scale 0.25) on the same Delta daily panel.

Replays plan_day() / coin_signal() helpers from the live module day by day and compares every
entry (day, coin, side) and exit with the backtest's trades. Also spot-checks that coin_signal()
on truncated candles equals the full-series values (i.e. it only uses data up to that day).
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trend_follower_v2 as V  # noqa: E402
from strategy import ema_trend_follower as L  # noqa: E402

DAY = 86400


def main():
    raw, btc, funds = V.load_delta()
    p = V.Panel(raw, btc, funds)
    _, start, end = V.PHASES['check2']
    V.SCALE = 0.25
    cfg_bt = dict(ema=(10, 30), shorts=1, regime=1, exit='flip', K=10)
    bt_trades, _, _ = V.run(p, cfg_bt, start, end)
    bt_entries = sorted((int(x['t']), p.syms[x['j']], x['side']) for x in bt_trades)
    bt_exits = sorted((int(x['xt']), p.syms[x['j']]) for x in bt_trades)

    # per-coin series with the LIVE helper functions (full history -> value at each day)
    T, S = p.c.shape
    ef = np.full((T, S), np.nan)
    es = np.full((T, S), np.nan)
    atr = np.full((T, S), np.nan)
    for j in range(S):
        idx = np.where(~np.isnan(p.c[:, j]))[0]
        if len(idx) == 0:
            continue
        cl = list(p.c[idx, j])
        ef[idx, j] = L.ema_series(cl, 10)
        es[idx, j] = L.ema_series(cl, 30)
        atr[idx, j] = L.atr_pct_series(list(p.h[idx, j]), list(p.l[idx, j]), cl)
    liq30 = p.liq30()
    cfg = {'top_n': 10, 'ema_slow': 30, 'allow_shorts': True, 'btc_regime': True,
           'risk_per_atr_usd': 25.0, 'max_notional': 500.0, 'max_gross': 7500.0}
    held = {}
    live_entries, live_exits = [], []
    i0, i1 = int(np.searchsorted(p.days, start)), int(np.searchsorted(p.days, end))
    for t in range(max(i0, 55), min(i1, T - 1)):
        u = t + 1
        sig = {}
        for j in range(S):
            if np.isnan(p.c[t, j]) or np.isnan(p.o[u, j]):
                continue
            sig[p.syms[j]] = {'state': 1 if ef[t, j] > es[t, j] else -1, 'atr_pct': atr[t, j],
                              'liq30': liq30[t, j], 'age': int(p.age[t, j]), 'day': int(p.days[t])}
        b = p.btc
        btc_sig = {'_above_slow': bool(p.c[t, b] > es[t, b])}
        exits, entries = L.plan_day(sig, held, btc_sig, cfg)
        for s in exits:
            live_exits.append((int(p.days[u]), s))
            del held[s]
        for s, side, notional in entries:
            live_entries.append((int(p.days[u]), s, side))
            held[s] = {'side': side, 'notional': notional}
    live_entries.sort()
    live_exits.sort()
    same_e = set(live_entries) & set(bt_entries)
    print(f'Entries: backtest {len(bt_entries)} | live logic {len(live_entries)} | identical {len(same_e)}')
    closed_bt = set(bt_exits)
    same_x = set(live_exits) & closed_bt
    print(f'Exits (closed trades): backtest {len(bt_exits)} | live logic {len(live_exits)} | identical {len(same_x)}')
    diff = sorted(set(live_entries) ^ set(bt_entries))
    open_now = {(s) for s in held}
    extra_live = [d for d in diff if d in set(live_entries)]
    print(f'Positions still open at the end (live logic): {len(held)}; extra live entries: {len(extra_live)}; '
          f'all of them still open: {all(d[1] in open_now for d in extra_live)}; backtest-only entries: '
          f'{len([d for d in diff if d in set(bt_entries)])}')

    # causality spot-check: coin_signal on candles truncated at day t == series value at t
    j = p.syms.index('SOLUSD') if 'SOLUSD' in p.syms else 0
    idx = np.where(~np.isnan(p.c[:, j]))[0]
    bad = 0
    for t in idx[100::97]:
        cand = [(int(p.days[k]), p.o[k, j], p.h[k, j], p.l[k, j], p.c[k, j]) for k in idx if k <= t]
        g = L.coin_signal(cand, {}, 10, 30)
        if g['state'] != (1 if ef[t, j] > es[t, j] else -1) or abs(g['atr_pct'] - atr[t, j]) > 1e-12:
            bad += 1
    print(f'coin_signal on truncated history matches the series: {bad == 0}')


if __name__ == '__main__':
    main()
