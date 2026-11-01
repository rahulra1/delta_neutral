"""
EMA Trend Follower (strategy/ema_trend_follower.py) — market-wide hourly replay on Delta perps.

Live rules replayed:
  - every hour: rank all perpetuals by 24h MARK-price change; top 50 = "gainers", bottom 50 = "losers"
  - BTC regime: BTCUSD 1h close > EMA200 -> only LONG gainers this hour, else only SHORT losers
  - entry filter: 6h open-interest change (contracts) > +10%
  - size: integer lots ~ $100 notional (min 1 lot); entry at the hour's mark close
  - skip if already holding that symbol on that side, or if in a 24h re-entry cooldown
  - exits: +10% TP / -5% SL vs entry, checked every minute live -> here on the hourly mark
    high/low, SL first when both are touched; no time exit; cooldown 24h after any exit
Costs (the live code ignores them): taker 0.05% + 18% GST per side + slippage (default 5bp/side,
alts are thin). Funding NOT modelled. Universe = perps listed today (delisted coins missing ->
survivorship bias, optimistic).

Data: MARK:<sym> and OI:<sym> hourly candles from Delta's public history (cached under
backtest/.crypto_cache/etf/). First run downloads ~250 x 2 series.

Usage: python backtest/ema_trend_follower_backtest.py [--days 730] [--slip-bp 5] [--workers 8]
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, 'backtest', '.crypto_cache', 'etf')
B = 'https://api.india.delta.exchange'
H = 3600

TOP_N, NOTIONAL, TP, SL, COOLDOWN_H, MIN_OI = 50, 100.0, 0.10, 0.05, 24, 10.0
FEE = 0.0005 * 1.18


def _get(params, tries=5):
    for a in range(tries):
        try:
            r = requests.get(f'{B}/v2/history/candles', params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + a * 2)
                continue
            r.raise_for_status()
            return r.json().get('result') or []
        except Exception:
            time.sleep(1 + a)
    return None


def series(sym, start, end):
    """Hourly closes/highs/lows for sym (e.g. 'MARK:SOLUSD'), cached, forward-paged."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f'{sym.replace(":", "_")}_{start // 86400}_{end // 86400}.json')
    if os.path.exists(path):
        return {int(k): v for k, v in json.load(open(path)).items()}
    out = {}
    cur = start
    while cur < end:
        nxt = min(end, cur + 2000 * H)
        rows = _get({'resolution': '1h', 'symbol': sym, 'start': cur, 'end': nxt})
        if rows is None:
            return None                      # don't cache a failed download
        for c in rows:
            if start <= c['time'] < end:
                out[int(c['time'])] = (float(c['close']), float(c['high']), float(c['low']))
        cur = nxt
    json.dump(out, open(path, 'w'))
    return out


def ema_series(xs, p):
    """EMA seeded with the SMA of the first p values (as api.top_coins._ema)."""
    out = [None] * len(xs)
    if len(xs) < p:
        return out
    k = 2 / (p + 1)
    e = sum(xs[:p]) / p
    out[p - 1] = e
    for i in range(p, len(xs)):
        e = xs[i] * k + e * (1 - k)
        out[i] = e
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=730)
    ap.add_argument('--slip-bp', type=float, default=5.0)
    ap.add_argument('--workers', type=int, default=8)
    a = ap.parse_args()
    slip = a.slip_bp / 10_000
    end = int(time.time()) // H * H
    start = end - a.days * 86400
    warm = start - 12 * 86400                       # warm-up for EMA200 / 24h change

    tick = requests.get(f'{B}/v2/tickers', params={'contract_types': 'perpetual_futures'}, timeout=30).json()['result']
    cv = {t['symbol']: float(t.get('contract_value') or 0) for t in tick}
    syms = sorted(s for s in cv if cv[s] > 0)
    print(f'Universe: {len(syms)} perps listed today. Downloading/caching MARK + OI hourly history...')
    t0 = time.time()
    jobs = [f'MARK:{s}' for s in syms] + [f'OI:{s}' for s in syms] + ['BTCUSD']
    with ThreadPoolExecutor(a.workers) as ex:
        data = dict(zip(jobs, ex.map(lambda j: series(j, warm, end), jobs)))
    missing = [j for j, v in data.items() if v is None]
    print(f'  done in {time.time() - t0:.0f}s; failed downloads: {len(missing)} {missing[:5]}')

    mark = {s: data[f'MARK:{s}'] for s in syms if data.get(f'MARK:{s}')}
    oi = {s: data[f'OI:{s}'] for s in syms if data.get(f'OI:{s}')}
    btc = data['BTCUSD']
    hours = list(range(warm, end, H))
    btc_closes, btc_t = [], []
    last = None
    for h in hours:                                  # forward-fill BTC gaps
        if h in btc:
            last = btc[h][0]
        if last:
            btc_closes.append(last)
            btc_t.append(h)
    btc_ema = dict(zip(btc_t, ema_series(btc_closes, 200)))
    btc_close = dict(zip(btc_t, btc_closes))

    open_legs = []                                   # dicts
    cooldown = {}                                    # sym -> until ts
    trades = []
    equity_curve = []
    realized = 0.0
    max_conc = max_notional = 0
    regime_hours = defaultdict(int)
    candidates = defaultdict(int)

    for h in range(start, end, H):
        # ---- 1) TP/SL on this hour's mark high/low (SL first) ----
        still = []
        for L in open_legs:
            bar = mark[L['sym']].get(h)
            if not bar:
                still.append(L)
                continue
            _, hi, lo = bar
            buy = L['side'] == 'buy'
            sl_px = L['entry'] * (1 - SL) if buy else L['entry'] * (1 + SL)
            tp_px = L['entry'] * (1 + TP) if buy else L['entry'] * (1 - TP)
            hit = None
            if (buy and lo <= sl_px) or (not buy and hi >= sl_px):
                hit, px = 'SL', sl_px
            elif (buy and hi >= tp_px) or (not buy and lo <= tp_px):
                hit, px = 'TP', tp_px
            if not hit:
                still.append(L)
                continue
            xp = px * (1 - slip) if buy else px * (1 + slip)
            gross = ((xp - L['fill']) if buy else (L['fill'] - xp)) * L['qty']
            fees = (L['fill'] + xp) * L['qty'] * FEE
            realized += gross - fees
            trades.append(dict(L, exit_t=h, reason=hit, gross=gross, fees=fees, net=gross - fees))
            cooldown[L['sym']] = h + COOLDOWN_H * H
        open_legs = still

        # ---- 2) hourly evaluation at the close of hour h ----
        if h in btc_close and btc_ema.get(h):
            above = btc_close[h] > btc_ema[h]
            regime_hours['LONG gainers' if above else 'SHORT losers'] += 1
            chg = []
            for s, m in mark.items():
                now, prev = m.get(h), m.get(h - 24 * H)
                if now and prev and prev[0] > 0:
                    chg.append(((now[0] / prev[0] - 1) * 100, s))
            chg.sort(reverse=True)
            pick = chg[:TOP_N] if above else sorted(chg)[:TOP_N]
            side = 'buy' if above else 'sell'
            held = {(L['sym'], L['side']) for L in open_legs}
            for _, s in pick:
                if (s, side) in held or cooldown.get(s, 0) > h:
                    continue
                o_now, o_6 = oi.get(s, {}).get(h), oi.get(s, {}).get(h - 6 * H)
                if not o_now or not o_6 or o_6[0] <= 0 or (o_now[0] / o_6[0] - 1) * 100 <= MIN_OI:
                    continue
                candidates[side] += 1
                px = mark[s][h][0]
                lots = max(1, int(round(NOTIONAL / (px * cv[s])))) if px * cv[s] > 0 else 0
                if not lots:
                    continue
                qty = lots * cv[s]
                fill = px * (1 + slip) if side == 'buy' else px * (1 - slip)
                open_legs.append({'sym': s, 'side': side, 'entry': px, 'fill': fill, 'qty': qty,
                                  'notional': qty * px, 'entry_t': h})
        # mark-to-market equity
        mtm = 0.0
        for L in open_legs:
            bar = mark[L['sym']].get(h)
            if bar:
                p = bar[0]
                mtm += ((p - L['fill']) if L['side'] == 'buy' else (L['fill'] - p)) * L['qty'] - L['fill'] * L['qty'] * FEE
        equity_curve.append((h, realized + mtm))
        max_conc = max(max_conc, len(open_legs))
        max_notional = max(max_notional, sum(L['notional'] for L in open_legs))

    # ---- report ----
    def ts(t):
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime('%Y-%m-%d')
    nets = [t['net'] for t in trades]
    gw, gl = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
    pk = dd = 0.0
    for _, e in equity_curve:
        pk = max(pk, e)
        dd = max(dd, pk - e)
    open_mtm = equity_curve[-1][1] - realized
    print(f'\n=== EMA Trend Follower replay {ts(start)} -> {ts(end)} | {len(mark)} perps | $100/position | '
          f'slippage {a.slip_bp:g}bp + fees 0.05%+GST per side | no funding ===')
    print(f'Regime hours: ' + ', '.join(f'{k} {v}' for k, v in regime_hours.items()))
    print(f'Entries: {len(trades) + len(open_legs)} ({candidates["buy"]} long, {candidates["sell"]} short); '
          f'closed {len(trades)}, still open {len(open_legs)}')
    if trades:
        print(f'Closed trades: win {sum(1 for x in nets if x > 0) / len(trades) * 100:.1f}%  '
              f'TP {sum(1 for t in trades if t["reason"] == "TP")} / SL {sum(1 for t in trades if t["reason"] == "SL")}  '
              f'gross ${sum(t["gross"] for t in trades):+,.0f}  fees ${sum(t["fees"] for t in trades):,.0f}  '
              f'NET ${sum(nets):+,.0f}  PF {gw / gl if gl else 99:.2f}  '
              f'avg hold {sum(t["exit_t"] - t["entry_t"] for t in trades) / len(trades) / 3600:.0f}h')
    print(f'Open positions at the end: {len(open_legs)}, unrealized ${open_mtm:+,.0f}')
    print(f'TOTAL P&L (realized + open): ${equity_curve[-1][1]:+,.0f} | max drawdown (hourly mark-to-market) ${dd:,.0f}')
    print(f'Max concurrent positions {max_conc} | max capital deployed (notional) ${max_notional:,.0f}')
    for side in ('buy', 'sell'):
        st = [t for t in trades if t['side'] == side]
        if st:
            n = [t['net'] for t in st]
            print(f'  {"LONG " if side == "buy" else "SHORT"} {len(st):>5} closed  win {sum(1 for x in n if x > 0) / len(st) * 100:4.1f}%  '
                  f'net ${sum(n):+8,.0f}  PF {sum(x for x in n if x > 0) / max(1e-9, -sum(x for x in n if x < 0)):.2f}')
    by_m = defaultdict(lambda: [0, 0.0])
    for t in trades:
        k = datetime.fromtimestamp(t['exit_t'], tz=timezone.utc).strftime('%Y-%m')
        by_m[k][0] += 1
        by_m[k][1] += t['net']
    print('\n  Month    closed    net$      cum$')
    cum = 0.0
    for k in sorted(by_m):
        cum += by_m[k][1]
        print(f'  {k}  {by_m[k][0]:>6}  {by_m[k][1]:>+8,.0f}  {cum:>+8,.0f}')
    print(f'  Profitable months: {sum(1 for v in by_m.values() if v[1] > 0)}/{len(by_m)}')


if __name__ == '__main__':
    main()
