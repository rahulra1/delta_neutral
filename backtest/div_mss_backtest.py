"""
Div + MSS (api.chart.calc_rsi_divergence_mss) — backtest, BTC on Delta BTCUSD perp.

Live rules: RSI(14) divergence between two consecutive swing highs (both RSI > 70, price higher
high, RSI lower high) -> SELL when a close breaks below the lowest low between the two highs
within 50 bars ("market structure shift"). Bullish mirror (RSI < 30). Swing = 5 bars each side.
SL = the 2nd swing extreme; TP = 1R (tp1 = close -/+ risk, NOT 2R as the UI card says).

Two views:
  A) "as intended": the function on closed candles, signals sorted by time, entry next open.
  B) "live replay": what strategy/futures_signal_trader.py actually trades. Every SUB-candle
     (15m inside 1h, 1h inside 4h) the function is called on the last W closed candles + the
     forming candle built from the sub-candles so far (its close = current price). The trader
     takes signals[-1] of the UNSORTED list (sells are appended first, then buys), trades it if
     newer than the last one and <= 2 candles old, at the current price. Exits are checked on
     sub-candles. Max 10 entries per UTC day; positions stack (as live).
Both: 100 lots (0.1 BTC), taker 0.05% + 18% GST + 1bp slippage per side, SL first.

Usage: python backtest/div_mss_backtest.py [--tf 1h,4h] [--days 730]
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btc_signal_strategy_report as rep  # noqa: E402
from api.chart import calc_rsi_divergence_mss as fn  # noqa: E402

QTY = 0.1
FEE = 0.0005 * 1.18
SLIP = 0.0001
SUB = {'1h': '15m', '4h': '1h', '15m': '5m'}
WINDOW = 600


def exit_walk(bars, start, side, sl, tp, entry_raw):
    """Walk bars from index `start`; SL first. Returns (exit_raw, index, reason)."""
    buy = side == 'buy'
    for k in range(start, len(bars)):
        b = bars[k]
        if (buy and b['l'] <= sl) or (not buy and b['h'] >= sl):
            return (min(sl, b['o']) if buy else max(sl, b['o'])), k, 'SL'
        if (buy and b['h'] >= tp) or (not buy and b['l'] <= tp):
            return tp, k, 'TP'
    return bars[-1]['c'], len(bars) - 1, 'END'


def pnl(side, entry_raw, exit_raw):
    buy = side == 'buy'
    e = entry_raw * (1 + SLIP) if buy else entry_raw * (1 - SLIP)
    x = exit_raw * (1 - SLIP) if buy else exit_raw * (1 + SLIP)
    gross = ((x - e) if buy else (e - x)) * QTY
    return gross, (e + x) * QTY * FEE


def summarize(label, trades, extra=''):
    if not trades:
        print(f'  {label:<34} no trades')
        return
    nets = [t['net'] for t in trades]
    gw, gl = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
    eq = pk = 10_000.0
    dd = 0.0
    for t in sorted(trades, key=lambda x: x['xt']):
        eq += t['net']
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    by_y = defaultdict(float)
    for t in trades:
        by_y['Y1' if t['t'] < SPLIT else 'Y2'] += t['net']
    longs = [t for t in trades if t['side'] == 'buy']
    print(f'  {label:<34} {len(trades):>4} trades ({len(longs)} long / {len(trades) - len(longs)} short)  '
          f'win {sum(1 for x in nets if x > 0) / len(trades) * 100:4.1f}%  gross ${sum(t["gross"] for t in trades):+8,.0f}  '
          f'fees ${sum(t["fees"] for t in trades):6,.0f}  NET ${sum(nets):+8,.0f}  PF {gw / gl if gl else 99:4.2f}  '
          f'maxDD ${dd:,.0f} ({dd / 10_000 * 100:.0f}%)  Y1 ${by_y["Y1"]:+,.0f} / Y2 ${by_y["Y2"]:+,.0f}{extra}')


def as_intended(cs, one_position):
    sigs = sorted(fn(cs + [dict(cs[-1])])['signals'], key=lambda s: s['time'])
    idx = {c['t']: i for i, c in enumerate(cs)}
    trades, busy = [], -1
    for s in sigs:
        i = idx[s['time']]
        j = i + 1
        if j >= len(cs) or (one_position and j <= busy):
            continue
        raw = cs[j]['o']
        if (s['type'] == 'buy' and raw <= s['sl']) or (s['type'] == 'sell' and raw >= s['sl']):
            xr, k, why = raw, j, 'SL'
        else:
            xr, k, why = exit_walk(cs, j, s['type'], s['sl'], s['tp1'], raw)
        g, f = pnl(s['type'], raw, xr)
        trades.append({'t': cs[j]['t'], 'xt': cs[k]['t'], 'side': s['type'], 'gross': g, 'fees': f, 'net': g - f,
                       'risk': abs(s['price'] - s['sl']) / s['price']})
        busy = k
    return sigs, trades


def live_replay(cs, sub, sec, one_position=False):
    sub_by_parent = defaultdict(list)
    for i, b in enumerate(sub):
        sub_by_parent[b['t'] // sec * sec].append(i)
    trades, last_t, busy_until = [], 0, -1
    per_day = defaultdict(int)
    fired = []
    for t in range(WINDOW, len(cs)):
        parent = cs[t]
        hi, lo = -1e18, 1e18
        for si in sub_by_parent.get(parent['t'], []):
            b = sub[si]
            hi, lo = max(hi, b['h']), min(lo, b['l'])
            forming = {'t': parent['t'], 'o': parent['o'], 'h': hi, 'l': lo, 'c': b['c'], 'v': 0.0}
            sigs = fn(cs[t - WINDOW:t] + [forming])['signals']
            if not sigs:
                continue
            s = sigs[-1]                                    # live: last element, unsorted
            now = b['t'] + (sub[1]['t'] - sub[0]['t'])     # sub-candle close time
            if not (s['time'] > last_t and now - s['time'] <= 2 * sec):
                continue
            last_t = s['time']
            day = datetime.fromtimestamp(now, tz=timezone.utc).date()
            if per_day[day] >= 10 or (one_position and si <= busy_until):
                continue
            per_day[day] += 1
            raw = b['c']
            fired.append((s['time'], s['type']))
            xr, k, why = exit_walk(sub, si + 1, s['type'], s['sl'], s['tp1'], raw)
            g, f = pnl(s['type'], raw, xr)
            trades.append({'t': now, 'xt': sub[k]['t'], 'side': s['type'], 'gross': g, 'fees': f, 'net': g - f})
            busy_until = k
    return trades, fired


def main():
    global SPLIT
    ap = argparse.ArgumentParser()
    ap.add_argument('--tf', default='1h,4h')
    ap.add_argument('--days', type=int, default=730)
    a = ap.parse_args()
    for tf in a.tf.split(','):
        sec = rep.TF_SEC[tf]
        cs = rep.fetch_delta(tf, a.days)
        SPLIT = cs[0]['t'] + 365 * 86400
        print(f'\n=== BTC {tf}: {rep.ts(cs[0]["t"])} -> {rep.ts(cs[-1]["t"])}, 100 lots, $10k ===')
        sigs, tr1 = as_intended(cs, True)
        _, tr2 = as_intended(cs, False)
        nb = sum(1 for s in sigs if s['type'] == 'buy')
        print(f'  signals on closed candles: {len(sigs)} ({nb} buy / {len(sigs) - nb} sell), '
              f'avg stop {sum(abs(s["price"] - s["sl"]) / s["price"] for s in sigs) / len(sigs) * 100:.2f}% of price, TP = 1R')
        summarize('A) as intended, one position', tr1)
        summarize('A) as intended, overlapping', tr2)
        sub = rep.fetch_delta(SUB[tf], a.days)
        tr3, fired = live_replay(cs, sub, sec)
        final = {(s['time'], s['type']) for s in sigs}
        ghost = sum(1 for x in fired if x not in final)
        summarize('B) live replay (what it trades)', tr3,
                  f'\n     {ghost}/{len(fired)} live entries were on signals that do NOT exist once the candle closes (repaint)')
        tr4, _ = live_replay(cs, sub, sec, one_position=True)
        summarize('B) live replay, one position', tr4)


if __name__ == '__main__':
    main()
