"""
EMA + Trendline (LIVE api.chart.calc_ema_trendline_breakout) on 50 Delta crypto perps, 1h.

Universe: the 50 most liquid Delta crypto perps by current 24h turnover (tokenized stocks,
gold/silver/oil excluded) that were listed >= 1 year before the end of the test, so every coin
has a full last-12-month window. (Current-liquidity ranking = mild survivorship bias.)

Signals: the live function on each coin's full history (parity with the bar-by-bar replay was
verified on BTC). Engine: btc_signal_strategy_report.run, 'report' mode = one position per
coin at a time (the live ONE_POSITION_KEYS rule), entry next open, SL first, gap fills at the
open, taker 0.05% + 18% GST per side, slippage 1bp (BTC/ETH) or 5bp (alts) per side, no funding.

Sizing: every trade = fixed NOTIONAL $ (default $8,000, about 100 lots of BTCUSD = 0.1 BTC).
Coins trade independently, so positions can overlap across coins (max concurrency is printed).

Periods: LAST 1Y = 2025-10-31..2026-10-31 (main), PREV 1Y = 2024-10-31..2025-10-31 (coins
with history only).

Usage: python backtest/ema_trendline_multicoin.py [--n 50] [--notional 8000]
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btc_signal_strategy_report as rep  # noqa: E402

BASE = rep.BASE_URL
NON_CRYPTO = ('bstocks', 'ondo', 'gold', 'silver', 'oil', 'xaut', 'paxg')
PERIODS = {
    'PREV 1Y': (datetime(2024, 10, 31, tzinfo=timezone.utc).timestamp(),
                datetime(2025, 10, 31, tzinfo=timezone.utc).timestamp()),
    'LAST 1Y': (datetime(2025, 10, 31, tzinfo=timezone.utc).timestamp(),
                datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp()),
}


def universe(n):
    prods = requests.get(f'{BASE}/v2/products', params={'contract_types': 'perpetual_futures',
                                                       'page_size': 500}, timeout=30).json()['result']
    tick = requests.get(f'{BASE}/v2/tickers', params={'contract_types': 'perpetual_futures'},
                        timeout=30).json()['result']
    turn = {t['symbol']: float(t.get('turnover_usd') or 0) for t in tick}
    cutoff = PERIODS['LAST 1Y'][0]
    out = []
    for p in prods:
        desc = (p.get('description') or '').lower() + ' ' + p['symbol'].lower()
        if any(k in desc for k in NON_CRYPTO) or p.get('state') not in (None, 'live'):
            continue
        lt = datetime.strptime(p['launch_time'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
        if lt > cutoff:
            continue
        out.append((turn.get(p['symbol'], 0), p['symbol']))
    return [s for _, s in sorted(out, reverse=True)[:n]]


def scale(trades, notional):
    """run() was called with QTY=1 coin; rescale each trade to a fixed $ notional."""
    for t in trades:
        k = notional / t['entry']
        t['gross'] *= k
        t['fees'] *= k
        t['net'] *= k
    return trades


def stats(trades, start_eq):
    nets = [t['net'] for t in trades]
    eq = peak = start_eq
    mdd = 0.0
    for t in sorted(trades, key=lambda x: x['exit_t']):
        eq += t['net']
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    w = sum(1 for x in nets if x > 0)
    return {'n': len(trades), 'wr': w / len(trades) * 100 if trades else 0,
            'gross': sum(t['gross'] for t in trades), 'fees': sum(t['fees'] for t in trades),
            'net': sum(nets), 'pf': rep.pf(nets) if trades else 0, 'mdd': mdd}


def max_concurrent(trades):
    ev = sorted([(t['entry_t'], 1) for t in trades] + [(t['exit_t'], -1) for t in trades],
                key=lambda e: (e[0], -e[1]))
    cur = mx = 0
    for _, d in ev:
        cur += d
        mx = max(mx, cur)
    return mx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=50)
    ap.add_argument('--notional', type=float, default=8000)
    ap.add_argument('--days', type=int, default=740)
    ap.add_argument('--live-rounding', action='store_true',
                    help='keep the live fn 2-decimal rounding of sl/tp (breaks low-priced coins)')
    a = ap.parse_args()

    rep.ENTRY_MODE = 'next_open'
    rep.QTY = 1.0
    if not a.live_rounding:
        # The live fn rounds price/sl/tp to 2 decimals: fine for BTC, broken for coins under
        # ~$10 (a $0.0015 coin gets sl = tp = 0.00). Shadow the builtin inside api.chart so
        # the backtest measures the strategy rule itself.
        import api.chart as chart
        chart.round = lambda x, nd=None: x
    syms = universe(a.n)
    print(f'Universe ({len(syms)}): {", ".join(syms)}\n')

    per = {p: {} for p in PERIODS}
    for sym in syms:
        cs = rep.fetch_delta('1h', a.days, symbol=sym)
        cs = [c for c in cs if c['c'] > 0]
        if len(cs) < 500:
            print(f'  {sym}: only {len(cs)} bars, skipped')
            continue
        rep.SLIP = 0.0001 if sym in ('BTCUSD', 'ETHUSD') else 0.0005
        sigs = rep.calc_ema_trendline_breakout(cs)['signals']
        for p, (s, e) in PERIODS.items():
            sub = [c for c in cs if s <= c['t'] < e]
            if len(sub) < 0.9 * (e - s) / 3600:      # need ~a full year of bars
                continue
            psig = [x for x in sigs if s <= x['time'] < e]
            trades, _ = rep.run(sub, psig, 'report')
            trades = [t for t in trades if t['reason'] != 'OPEN@END']
            per[p][sym] = scale(trades, a.notional)

    for p in ('LAST 1Y', 'PREV 1Y'):
        res = per[p]
        if not res:
            continue
        s0, e0 = PERIODS[p]
        print('=' * 96)
        print(f'{p}: {datetime.fromtimestamp(s0, tz=timezone.utc):%Y-%m-%d} -> '
              f'{datetime.fromtimestamp(e0, tz=timezone.utc):%Y-%m-%d}  |  {len(res)} coins  |  '
              f'${a.notional:,.0f} notional per trade, 1h')
        print('=' * 96)
        rows = sorted(((stats(t, 10_000), sym) for sym, t in res.items()), key=lambda x: -x[0]['net'])
        print(f'{"Coin":<14}{"Trades":>7}{"Win%":>7}{"Gross$":>11}{"Fees$":>10}{"Net$":>11}{"PF":>6}{"MaxDD$":>10}')
        for r, sym in rows:
            print(f'{sym:<14}{r["n"]:>7}{r["wr"]:>6.1f}%{r["gross"]:>+11,.0f}{r["fees"]:>10,.0f}'
                  f'{r["net"]:>+11,.0f}{r["pf"]:>6.2f}{r["mdd"]:>10,.0f}')
        allt = [t for ts_ in res.values() for t in ts_]
        tot = stats(allt, 10_000)
        prof = sum(1 for r, _ in rows if r['net'] > 0)
        print('-' * 96)
        print(f'{"PORTFOLIO":<14}{tot["n"]:>7}{tot["wr"]:>6.1f}%{tot["gross"]:>+11,.0f}{tot["fees"]:>10,.0f}'
              f'{tot["net"]:>+11,.0f}{tot["pf"]:>6.2f}{tot["mdd"]:>10,.0f}')
        print(f'Profitable coins: {prof}/{len(rows)}   max concurrent positions: {max_concurrent(allt)} '
              f'(= ${max_concurrent(allt) * a.notional:,.0f} notional)')
        for side in ('buy', 'sell'):
            st = stats([t for t in allt if t['side'] == side], 10_000)
            print(f'  {side.upper():<5}{st["n"]:>6} trades  win {st["wr"]:5.1f}%  net ${st["net"]:+,.0f}  PF {st["pf"]:.2f}')
        ex = [t for sym, ts_ in res.items() if sym not in ('BTCUSD', 'ETHUSD') for t in ts_]
        if ex:
            st = stats(ex, 10_000)
            print(f'  Alts only (ex BTC/ETH): {st["n"]} trades, net ${st["net"]:+,.0f}, PF {st["pf"]:.2f}')
        months = defaultdict(list)
        for t in allt:
            months[datetime.fromtimestamp(t['entry_t'], tz=timezone.utc).strftime('%Y-%m')].append(t)
        print(f'\n  {"Month":<8}{"Trades":>7}{"Win%":>7}{"Net$":>11}{"Cum$":>11}')
        cum = 0.0
        for m in sorted(months):
            mn = sum(t['net'] for t in months[m])
            cum += mn
            print(f'  {m:<8}{len(months[m]):>7}{sum(1 for t in months[m] if t["net"] > 0) / len(months[m]) * 100:>6.0f}%'
                  f'{mn:>+11,.0f}{cum:>+11,.0f}')
        print()


if __name__ == '__main__':
    main()
