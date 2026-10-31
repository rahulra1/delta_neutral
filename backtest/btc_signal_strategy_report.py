"""
BTC signal-strategy $ report (Delta BTCUSD perp), 15m and 1h.
  --strategy ema920  : 9/20 EMA Pullback (api.chart.calc_ema920_pullback), entry next bar open
  --strategy sma_vol : SMA + Breakout Setup 1 (api.chart.calc_sma_vol_breakout), stop entry at
                       the signal-bar high/low on the bar that breaks it (or that bar's open on a gap)

Signals come from the LIVE functions (imported, not re-implemented), so signal logic is
identical to what strategy/futures_signal_trader.py trades. Notes for ema920:
  - trend: >=2 of the previous 3 closes above both EMAs (long) / below both (short)
  - pullback bar: low touches the EMA zone (long) / high touches it (short)
  - rejection: bullish (bearish) candle closing beyond the far EMA
  - volume >= 20-bar average volume
  - SL = signal-bar low (high); TP = 2R from the signal close

Execution engines:
  report : one position at a time; overlapping signals skipped.
  live   : mirrors FuturesSignalTrader — every new closed-bar signal opens a new
           leg (legs can stack), max 10 entries per UTC day, no time exit,
           market orders both sides (taker fee on TP too).
Both: entry at the NEXT bar open (live places a market order on the first scan
after the signal bar closes), SL/TP levels from the signal (not re-anchored to
the fill), SL checked first when a bar touches both, gap through SL at the open
exits at the open. Fees: taker 0.05% + 18% GST per side + 1bp slippage per side.
Funding not modelled.

Usage:
  python backtest/btc_signal_strategy_report.py --strategy ema920          # 15m + 1h, 365d, Delta
  python backtest/btc_signal_strategy_report.py --strategy sma_vol --entry-bar best
  python backtest/btc_signal_strategy_report.py --tf 1h --engine live --source yahoo
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from api.chart import calc_ema920_pullback, calc_sma_vol_breakout, calc_ema_trendline_breakout  # noqa: E402

def calc_sma_vol_breakout_v2(candles, sma_period=50, vol_mult=1.5, rr=4, min_risk_pct=0.005):
    """Improved SMA + Breakout (candidate, NOT live): stronger volume filter
    (v > 1.5x 20-bar avg, no "> previous bar" rule), skip stops tighter than 0.5%
    of price (fee drag), target 4R. Same signal format/timing as the live function."""
    n = len(candles)
    if n < sma_period + 22:
        return {'signals': []}
    c = [x['c'] for x in candles]
    v = [x.get('v', 0) for x in candles]
    sigs = []
    s = sum(c[:sma_period])
    sm = [None] * n
    sm[sma_period - 1] = s / sma_period
    for i in range(sma_period, n):
        s += c[i] - c[i - sma_period]
        sm[i] = s / sma_period
    for i in range(sma_period + 1, n - 1):
        lo = max(0, i - 19)
        avg = max(1, sum(v[lo:i + 1]) / (i - lo + 1))
        if not v[i] > vol_mult * avg:
            continue
        nxt = candles[i + 1]
        if c[i - 1] < sm[i - 1] and c[i] > sm[i]:
            entry, sl = candles[i]['h'], candles[i]['l']
            if nxt['h'] >= entry and entry - sl > 0 and (entry - sl) / entry >= min_risk_pct:
                sigs.append({'time': candles[i + 1]['t'], 'type': 'buy', 'price': entry, 'sl': sl,
                             'tp1': entry + rr * (entry - sl)})
        elif c[i - 1] > sm[i - 1] and c[i] < sm[i]:
            entry, sl = candles[i]['l'], candles[i]['h']
            if nxt['l'] <= entry and sl - entry > 0 and (sl - entry) / entry >= min_risk_pct:
                sigs.append({'time': candles[i + 1]['t'], 'type': 'sell', 'price': entry, 'sl': sl,
                             'tp1': entry - rr * (sl - entry)})
    return {'signals': sigs}


def causal_signals(cs, fn, window=1500):
    """Replay a signal fn the way FuturesSignalTrader sees it: after each bar t closes,
    call fn on the last `window` closed bars + the just-opened bar (flat at its open, as the
    forming candle looks on the first scan), take signals[-1], trade it only if it is new
    and <= 2 candles old. Returned signal 'time' = detection bar t (entry = next open)."""
    sec = cs[1]['t'] - cs[0]['t']
    out = []
    last_t = 0
    for t in range(window, len(cs) - 1):
        nb = cs[t + 1]
        forming = {'t': nb['t'], 'o': nb['o'], 'h': nb['o'], 'l': nb['o'], 'c': nb['o'], 'v': 0.0}
        sigs = fn(cs[t - window + 1:t + 1] + [forming]).get('signals', [])
        if not sigs:
            continue
        s = sigs[-1]
        now = cs[t]['t'] + sec
        if s['time'] > last_t and now - s['time'] <= 2 * sec:
            last_t = s['time']
            out.append(dict(s, time=cs[t]['t'], orig_time=s['time']))
    return out


def _ema_tl_live(cs):
    return {'signals': causal_signals(cs, calc_ema_trendline_breakout)}


STRATEGIES = {
    # key: (label, signal fn, entry mode)
    'ema920': ('9/20 EMA Pullback', calc_ema920_pullback, 'next_open'),
    'sma_vol': ('SMA + Breakout', calc_sma_vol_breakout, 'stop'),
    'sma_vol_v2': ('SMA + Breakout v2 (SMA50, vol>1.5x, minrisk 0.5%, TP 4R)', calc_sma_vol_breakout_v2, 'stop'),
    # live api.chart function (improved Oct 2026; causal, closes at signal bar, entry next open)
    'ema_tl': ('EMA + Trendline (live fn: EMA100, swing stop, 2R)', calc_ema_trendline_breakout, 'next_open'),
    # bar-by-bar replay = exactly what the live trader sees each scan
    'ema_tl_live': ('EMA + Trendline (bar-by-bar replay)', _ema_tl_live, 'next_open'),
}
ENTRY_MODE = 'next_open'   # set in main()
ENTRY_BAR = 'worst'        # stop entries: 'worst' = SL first on fill bar, 'best' = adverse wick before fill

BASE_URL = 'https://api.india.delta.exchange'
CACHE_DIR = os.path.join(ROOT, 'backtest', '.crypto_cache')
TF_SEC = {'5m': 300, '15m': 900, '1h': 3600, '4h': 14400}

LOTS = 100
CONTRACT_VALUE = 0.001          # BTC per Delta BTCUSD contract
QTY = LOTS * CONTRACT_VALUE     # 0.1 BTC
START_EQUITY = 10_000.0
TAKER = 0.0005 * 1.18           # 0.05% + 18% GST
SLIP = 0.0001                   # 1bp per side
MAX_TRADES_PER_DAY = 10         # FuturesSignalTrader default


# ----------------------------------------------------------------------------- data
def fetch_delta(tf, days):
    """Forward-paged Delta BTCUSD OHLCV, cached per (tf, start-day, end-day)."""
    sec = TF_SEC[tf]
    end = (int(time.time()) // sec) * sec          # exclude the forming bar
    start = end - days * 86400
    path = os.path.join(CACHE_DIR, f'ema920_BTCUSD_{tf}_{start // 86400}_{end // 86400}.json')
    if os.path.exists(path):
        return json.load(open(path))
    out = {}
    cur = start
    while cur < end:
        nxt = min(end, cur + 2000 * sec)
        for attempt in range(4):
            try:
                r = requests.get(f'{BASE_URL}/v2/history/candles',
                                 params={'resolution': tf, 'symbol': 'BTCUSD',
                                         'start': cur, 'end': nxt}, timeout=30)
                r.raise_for_status()
                rows = r.json().get('result', [])
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2)
        for c in rows:
            if start <= c['time'] < end:
                out[c['time']] = {'t': c['time'], 'o': float(c['open']), 'h': float(c['high']),
                                  'l': float(c['low']), 'c': float(c['close']),
                                  'v': float(c.get('volume') or 0)}
        cur = nxt
    cs = sorted(out.values(), key=lambda x: x['t'])
    expected = (end - start) // sec
    print(f'  Delta {tf}: {len(cs)} bars ({len(cs) / expected * 100:.1f}% coverage)')
    os.makedirs(CACHE_DIR, exist_ok=True)
    json.dump(cs, open(path, 'w'))
    return cs


def fetch_yahoo(tf):
    """The live feed: api.chart.get_candles('BTC', tf) uses Yahoo BTC-USD."""
    from api.chart import get_candles
    cs = get_candles('BTC', tf) or []
    sec = TF_SEC[tf]
    now = time.time()
    return [c for c in cs if c['t'] + sec <= now]   # closed bars only


# ----------------------------------------------------------------------------- engine
def fee(px):
    return px * QTY * TAKER


def run(cs, signals, engine):
    idx = {c['t']: i for i, c in enumerate(cs)}
    trades = []
    open_legs = []            # live engine
    busy_until = -1           # report engine: index of last bar of open trade
    day_count = defaultdict(int)
    skipped_busy = skipped_cap = 0
    max_conc = 0

    def simulate(sig, ei, raw):
        side = sig['type']
        sl, tp = sig['sl'], sig['tp1']
        entry = raw * (1 + SLIP) if side == 'buy' else raw * (1 - SLIP)
        reason = exit_raw = None
        xi = ei
        # gap through SL / TP at the open
        if side == 'buy' and raw <= sl:
            reason, exit_raw = 'SL', raw
        elif side == 'sell' and raw >= sl:
            reason, exit_raw = 'SL', raw
        else:
            for j in range(ei, len(cs)):
                b = cs[j]
                if j == ei and ENTRY_MODE == 'stop' and ENTRY_BAR == 'best':
                    # Stop fill mid-bar: assume the adverse extreme came BEFORE the fill
                    # (only TP, or a close beyond SL, counts on the fill bar).
                    if side == 'buy':
                        if b['h'] >= tp:
                            reason, exit_raw, xi = 'TP', tp, j
                            break
                        if b['c'] <= sl:
                            reason, exit_raw, xi = 'SL', sl, j
                            break
                    else:
                        if b['l'] <= tp:
                            reason, exit_raw, xi = 'TP', tp, j
                            break
                        if b['c'] >= sl:
                            reason, exit_raw, xi = 'SL', sl, j
                            break
                    continue
                if side == 'buy':
                    if b['l'] <= sl:
                        reason, exit_raw, xi = 'SL', min(sl, b['o']) if j > ei else sl, j
                        break
                    if b['h'] >= tp:
                        reason, exit_raw, xi = 'TP', tp, j
                        break
                else:
                    if b['h'] >= sl:
                        reason, exit_raw, xi = 'SL', max(sl, b['o']) if j > ei else sl, j
                        break
                    if b['l'] <= tp:
                        reason, exit_raw, xi = 'TP', tp, j
                        break
            if reason is None:
                reason, exit_raw, xi = 'OPEN@END', cs[-1]['c'], len(cs) - 1
        exit_px = exit_raw * (1 - SLIP) if side == 'buy' else exit_raw * (1 + SLIP)
        gross = (exit_px - entry) * QTY if side == 'buy' else (entry - exit_px) * QTY
        fees = fee(entry) + fee(exit_px)
        return {'side': side, 'sig_t': sig['time'], 'entry_t': cs[ei]['t'], 'exit_t': cs[xi]['t'],
                'ei': ei, 'xi': xi, 'entry': entry, 'exit': exit_px, 'sl': sl, 'tp': tp,
                'reason': reason, 'gross': gross, 'fees': fees, 'net': gross - fees,
                'bars': xi - ei + 1}

    for sig in signals:
        si = idx.get(sig['time'])
        if si is None:
            continue
        if ENTRY_MODE == 'stop':
            # sma_vol_breakout: signal time = the bar that broke the level; stop fill
            # at the level, or at the open if that bar gapped through it.
            ei = si
            o = cs[ei]['o']
            raw = max(o, sig['price']) if sig['type'] == 'buy' else min(o, sig['price'])
        else:
            if si + 1 >= len(cs):
                continue
            ei = si + 1
            raw = cs[ei]['o']
        day = datetime.fromtimestamp(cs[ei]['t'], tz=timezone.utc).date()
        if engine == 'report':
            if ei <= busy_until:
                skipped_busy += 1
                continue
            t = simulate(sig, ei, raw)
            busy_until = t['xi']
        else:
            if day_count[day] >= MAX_TRADES_PER_DAY:
                skipped_cap += 1
                continue
            t = simulate(sig, ei, raw)
            day_count[day] += 1
            open_legs = [x for x in open_legs if x['xi'] >= ei] + [t]
            max_conc = max(max_conc, len(open_legs))
        trades.append(t)
    return trades, {'skipped_busy': skipped_busy, 'skipped_cap': skipped_cap, 'max_conc': max_conc}


# ----------------------------------------------------------------------------- report
def ts(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')


def equity_dd(trades):
    """Realized equity curve (by exit time) -> max DD $ and %."""
    eq = peak = START_EQUITY
    mdd = mdd_pct = 0.0
    for t in sorted(trades, key=lambda x: (x['exit_t'], x['entry_t'])):
        eq += t['net']
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        mdd_pct = max(mdd_pct, (peak - eq) / peak * 100)
    return eq, mdd, mdd_pct


def pf(xs):
    gw = sum(x for x in xs if x > 0)
    gl = -sum(x for x in xs if x < 0)
    return gw / gl if gl else float('inf')


def report(label, cs, signals, trades, meta, engine):
    print('\n' + '=' * 92)
    print(f'{label}  |  engine={engine}  |  {ts(cs[0]["t"])} -> {ts(cs[-1]["t"])} UTC  |  {len(cs)} bars')
    print('=' * 92)
    n = len(trades)
    nb = sum(1 for s in signals if s['type'] == 'buy')
    print(f'Signals: {len(signals)} (buy {nb} / sell {len(signals) - nb}); trades taken {n}; '
          f'skipped-in-trade {meta["skipped_busy"]}; skipped-daily-cap {meta["skipped_cap"]}'
          + (f'; max concurrent legs {meta["max_conc"]} (= {meta["max_conc"] * LOTS} lots)' if engine == 'live' else ''))
    if not n:
        return None
    nets = [t['net'] for t in trades]
    gross = sum(t['gross'] for t in trades)
    fees = sum(t['fees'] for t in trades)
    net = sum(nets)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    eq, mdd, mdd_pct = equity_dd(trades)
    gross_pf = pf([t['gross'] for t in trades])
    risk = [abs(t['entry'] - t['sl']) / t['entry'] * 100 for t in trades]
    reasons = defaultdict(int)
    for t in trades:
        reasons[t['reason']] += 1
    print(f'\n  Size ......................... {LOTS} lots = {QTY:.1f} BTC per trade; start equity ${START_EQUITY:,.0f}')
    print(f'  Win rate (net) ............... {len(wins) / n * 100:.1f}%   ({len(wins)}W / {len(losses)}L)')
    print(f'  Exits ........................ ' + ', '.join(f'{k} {v}' for k, v in sorted(reasons.items())))
    print(f'  Gross P&L .................... ${gross:+,.2f}   (gross PF {gross_pf:.2f})')
    slip = sum((t['entry'] + t['exit']) * SLIP * QTY for t in trades)   # approx, already inside gross
    print(f'  Fees ......................... ${fees:,.2f}   (+ ~${slip:,.2f} slippage, already in gross)')
    print(f'  Net P&L ...................... ${net:+,.2f}   ({net / START_EQUITY * 100:+.2f}%)   net PF {pf(nets):.2f}')
    print(f'  End equity ................... ${eq:,.2f}')
    print(f'  Max drawdown ................. ${mdd:,.2f}   ({mdd_pct:.2f}%)')
    print(f'  Avg win / avg loss ........... ${(sum(wins) / len(wins) if wins else 0):+,.2f} / '
          f'${(sum(losses) / len(losses) if losses else 0):+,.2f}')
    print(f'  Avg stop distance ............ {sum(risk) / n:.3f}% of price '
          f'(~${sum(abs(t["entry"] - t["sl"]) for t in trades) / n * QTY:,.2f} risk/trade vs ${fees / n:,.2f} fees/trade)')
    print(f'  Avg hold ..................... {sum(t["bars"] for t in trades) / n:.1f} bars')
    for side in ('buy', 'sell'):
        st = [t for t in trades if t['side'] == side]
        if st:
            sn = [t['net'] for t in st]
            print(f'  {side.upper():<5} {len(st):>4} trades  win {sum(1 for x in sn if x > 0) / len(st) * 100:5.1f}%  '
                  f'gross ${sum(t["gross"] for t in st):+10,.2f}  net ${sum(sn):+10,.2f}  PF {pf(sn):.2f}')

    months = defaultdict(list)
    for t in trades:
        months[datetime.fromtimestamp(t['entry_t'], tz=timezone.utc).strftime('%Y-%m')].append(t)
    print(f'\n  {"Month":<8}{"Trades":>7}{"Win%":>7}{"Gross$":>11}{"Fees$":>9}{"Net$":>11}{"Cum$":>11}')
    cum = 0.0
    for m in sorted(months):
        ms = months[m]
        mn = sum(t['net'] for t in ms)
        cum += mn
        print(f'  {m:<8}{len(ms):>7}{sum(1 for t in ms if t["net"] > 0) / len(ms) * 100:>6.0f}%'
              f'{sum(t["gross"] for t in ms):>+11,.2f}{sum(t["fees"] for t in ms):>9,.2f}{mn:>+11,.2f}{cum:>+11,.2f}')
    pos_months = sum(1 for m in months if sum(t['net'] for t in months[m]) > 0)
    print(f'  Profitable months: {pos_months}/{len(months)}')
    return {'label': label, 'engine': engine, 'n': n, 'wr': len(wins) / n * 100, 'gross': gross,
            'gross_pf': gross_pf, 'fees': fees, 'net': net, 'pf': pf(nets), 'mdd': mdd, 'mdd_pct': mdd_pct}


def main():
    global ENTRY_MODE, ENTRY_BAR
    ap = argparse.ArgumentParser()
    ap.add_argument('--strategy', default='ema920', choices=list(STRATEGIES))
    ap.add_argument('--tf', default='15m,1h')
    ap.add_argument('--days', type=int, default=365)
    ap.add_argument('--engine', default='report,live')
    ap.add_argument('--source', default='delta', choices=['delta', 'yahoo'])
    ap.add_argument('--entry-bar', default='worst', choices=['worst', 'best'],
                    help='stop entries only: SL-first on the fill bar (worst) or adverse wick before fill (best)')
    ap.add_argument('--trades', action='store_true', help='print every trade')
    a = ap.parse_args()
    name, sig_fn, ENTRY_MODE = STRATEGIES[a.strategy]
    ENTRY_BAR = a.entry_bar
    print(f'Strategy: {name}  (entry: {ENTRY_MODE}{", fill-bar " + ENTRY_BAR if ENTRY_MODE == "stop" else ""})')

    summary = []
    for tf in a.tf.split(','):
        cs = fetch_delta(tf, a.days) if a.source == 'delta' else fetch_yahoo(tf)
        if len(cs) < 50:
            print(f'{tf}: not enough data ({len(cs)} bars)')
            continue
        signals = sig_fn(cs)['signals']
        for engine in a.engine.split(','):
            trades, meta = run(cs, signals, engine)
            r = report(f'BTC {tf} ({a.source})', cs, signals, trades, meta, engine)
            if r:
                summary.append(r)
            if a.trades:
                for t in trades:
                    print(f'    {ts(t["entry_t"])} {t["side"]:<4} {t["entry"]:>10,.1f} -> {t["exit"]:>10,.1f} '
                          f'{t["reason"]:<8} net ${t["net"]:+8,.2f}')

    if summary:
        print('\n' + '=' * 92)
        print('SUMMARY (100 lots = 0.1 BTC, $10k, taker 0.05%+GST + 1bp slip per side, no funding)')
        print(f'{"Run":<22}{"Engine":<8}{"Trades":>7}{"Win%":>7}{"Gross$":>11}{"GrossPF":>8}'
              f'{"Fees$":>10}{"Net$":>11}{"NetPF":>7}{"MaxDD$":>10}{"DD%":>7}')
        for r in summary:
            print(f'{r["label"]:<22}{r["engine"]:<8}{r["n"]:>7}{r["wr"]:>6.1f}%{r["gross"]:>+11,.2f}'
                  f'{r["gross_pf"]:>8.2f}{r["fees"]:>10,.2f}{r["net"]:>+11,.2f}{r["pf"]:>7.2f}'
                  f'{r["mdd"]:>10,.2f}{r["mdd_pct"]:>6.1f}%')


if __name__ == '__main__':
    main()
