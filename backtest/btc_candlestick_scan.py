"""
BTC 15-minute candlestick-pattern scan + simple P&L backtest.

Fetches ~30 days of BTCUSD 15m OHLCV from Delta Exchange's public
/v2/history/candles endpoint (no auth needed), detects the single/double/triple
candlestick patterns from the trading cheat-sheet, and simulates a basic trade
for each signal to estimate how much the patterns would have earned.

Trade model (per signal):
  - Enter at the OPEN of the candle AFTER the pattern completes.
  - Direction = pattern bias (bullish -> long, bearish -> short).
  - Fixed bracket: target = TP_PCT, stop = SL_PCT of entry price.
  - Walk forward up to MAX_HOLD bars; exit on first TP/SL touch, else exit at
    the close of the last held bar.
  - Fixed size of LOTS contracts per trade; P&L in USD from actual price move,
    net of taker/maker fees.

This is an educational estimate, not a production backtest (no fees/slippage,
intrabar TP/SL assumed fillable, one position at a time per pattern stream).
"""

import time
import requests
from datetime import datetime, timezone

BASE_URL = 'https://api.india.delta.exchange'
SYMBOL = 'BTCUSD'
RESOLUTION = '15m'
_RES_MIN = {'1m': 1, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '2h': 120,
            '4h': 240, '1d': 1440}.get(RESOLUTION, 15)  # minutes per bar
DAYS = 365

# --- trade model params ---
TP_PCT = 0.015          # +1.5% take-profit
SL_PCT = 0.015          # -1.5% stop-loss (1:1)
MAX_HOLD = 16          # bars to hold if neither TP nor SL hit (16 x 15m = 4h)

# --- trend filter params ---
EMA_FAST = 20           # fast EMA period
EMA_SLOW = 50           # slow EMA period
EMA_NEUTRAL_PCT = 0.05  # |fast-slow|/slow below this = NEUTRAL (no trade)

# --- sizing / account params ---
CONTRACT_VALUE = 0.001  # BTC per 1 Delta BTCUSD contract (config.CONTRACT_VALUES['BTC'])
LOTS = 100              # contracts (lots) per trade -> 100 * 0.001 = 0.1 BTC
START_EQUITY = 10000.0  # starting account equity in USD for the equity curve

# --- fees (fraction of traded notional per leg) ---
TAKER_FEE = 0.0002      # 0.02% taker (market orders: entry, SL, time exit)
MAKER_FEE = 0.0005      # 0.05% maker (limit order: TP exit)

# --- pattern whitelist ---
# Min body/range ratio to call a candle a Marubozu (lower = looser, more signals).
MARUBOZU_BODY = 0.90
# Set to None or [] to trade all detected patterns.
WHITELIST = None
_UNUSED_WHITELIST = [
    'Bullish Marubozu',
    'Bearish Marubozu',
]


# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------
def fetch_candles(symbol=SYMBOL, resolution=RESOLUTION, days=DAYS):
    """Return oldest-first list of {'t','o','h','l','c','v'} dicts.

    Delta caps /v2/history/candles at ~4000 bars per request, so for long
    windows we page backward in chunks and stitch the results together.
    """
    sec_per_bar = _RES_MIN * 60
    chunk_bars = 3500                      # stay safely under the ~4000 cap
    chunk_secs = chunk_bars * sec_per_bar
    end = int(time.time())
    floor = end - days * 24 * 3600         # oldest timestamp we want
    by_time = {}                           # dedupe by candle open time

    cur_end = end
    while cur_end > floor:
        cur_start = max(floor, cur_end - chunk_secs)
        r = requests.get(
            f'{BASE_URL}/v2/history/candles',
            params={'resolution': resolution, 'symbol': symbol,
                    'start': cur_start, 'end': cur_end},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get('success'):
            raise RuntimeError(f'API error: {data}')
        rows = data.get('result', [])
        if not rows:
            break
        for c in rows:
            by_time[c['time']] = c
        oldest = min(c['time'] for c in rows)
        # step the window back; stop if we didn't make progress
        new_end = oldest - sec_per_bar
        if new_end >= cur_end:
            break
        cur_end = new_end

    rows = sorted(by_time.values(), key=lambda c: c['time'])
    return [{'t': c['time'], 'o': float(c['open']), 'h': float(c['high']),
             'l': float(c['low']), 'c': float(c['close']),
             'v': float(c.get('volume', 0))} for c in rows]


# ----------------------------------------------------------------------------
# Candle helpers
# ----------------------------------------------------------------------------
def body(c):          return abs(c['c'] - c['o'])
def rng(c):           return c['h'] - c['l'] or 1e-9
def upper_wick(c):    return c['h'] - max(c['o'], c['c'])
def lower_wick(c):    return min(c['o'], c['c']) - c['l']
def is_bull(c):       return c['c'] > c['o']
def is_bear(c):       return c['c'] < c['o']
def mid(c):           return (c['o'] + c['c']) / 2


# ----------------------------------------------------------------------------
# Pattern detectors
# Each returns (name, bias) or None, evaluated at index i (pattern completes at i).
# bias: 'bull' or 'bear'
# ----------------------------------------------------------------------------
def hammer(cs, i):
    c = cs[i]
    if body(c) == 0:
        return None
    if lower_wick(c) >= 2 * body(c) and upper_wick(c) <= body(c) and body(c) / rng(c) < 0.4:
        # downtrend context
        if cs[i-1]['c'] < cs[max(0, i-3)]['c']:
            return ('Hammer', 'bull')
    return None


def hanging_man(cs, i):
    c = cs[i]
    if body(c) == 0:
        return None
    if lower_wick(c) >= 2 * body(c) and upper_wick(c) <= body(c) and body(c) / rng(c) < 0.4:
        if cs[i-1]['c'] > cs[max(0, i-3)]['c']:  # uptrend context
            return ('Hanging Man', 'bear')
    return None


def inverted_hammer(cs, i):
    c = cs[i]
    if body(c) == 0:
        return None
    if upper_wick(c) >= 2 * body(c) and lower_wick(c) <= body(c) and body(c) / rng(c) < 0.4:
        if cs[i-1]['c'] < cs[max(0, i-3)]['c']:  # downtrend
            return ('Inverted Hammer', 'bull')
    return None


def shooting_star(cs, i):
    c = cs[i]
    if body(c) == 0:
        return None
    if upper_wick(c) >= 2 * body(c) and lower_wick(c) <= body(c) and body(c) / rng(c) < 0.4:
        if cs[i-1]['c'] > cs[max(0, i-3)]['c']:  # uptrend
            return ('Shooting Star', 'bear')
    return None


def dragonfly_doji(cs, i):
    c = cs[i]
    if body(c) / rng(c) < 0.1 and lower_wick(c) >= 2 * rng(c) * 0.4 and upper_wick(c) <= rng(c) * 0.1:
        return ('Dragonfly Doji', 'bull')
    return None


def gravestone_doji(cs, i):
    c = cs[i]
    if body(c) / rng(c) < 0.1 and upper_wick(c) >= 2 * rng(c) * 0.4 and lower_wick(c) <= rng(c) * 0.1:
        return ('Gravestone Doji', 'bear')
    return None


def bull_marubozu(cs, i):
    c = cs[i]
    if is_bull(c) and body(c) / rng(c) > MARUBOZU_BODY:
        return ('Bullish Marubozu', 'bull')
    return None


def bear_marubozu(cs, i):
    c = cs[i]
    if is_bear(c) and body(c) / rng(c) > MARUBOZU_BODY:
        return ('Bearish Marubozu', 'bear')
    return None


def bullish_engulfing(cs, i):
    p, c = cs[i-1], cs[i]
    if is_bear(p) and is_bull(c) and c['c'] >= p['o'] and c['o'] <= p['c'] and body(c) > body(p):
        return ('Bullish Engulfing', 'bull')
    return None


def bearish_engulfing(cs, i):
    p, c = cs[i-1], cs[i]
    if is_bull(p) and is_bear(c) and c['o'] >= p['c'] and c['c'] <= p['o'] and body(c) > body(p):
        return ('Bearish Engulfing', 'bear')
    return None


def bullish_harami(cs, i):
    p, c = cs[i-1], cs[i]
    if is_bear(p) and is_bull(c) and body(p) > body(c) and c['o'] >= p['c'] and c['c'] <= p['o']:
        return ('Bullish Harami', 'bull')
    return None


def bearish_harami(cs, i):
    p, c = cs[i-1], cs[i]
    if is_bull(p) and is_bear(c) and body(p) > body(c) and c['o'] <= p['c'] and c['c'] >= p['o']:
        return ('Bearish Harami', 'bear')
    return None


def piercing(cs, i):
    p, c = cs[i-1], cs[i]
    if is_bear(p) and is_bull(c) and c['o'] < p['l'] and c['c'] > mid(p) and c['c'] < p['o']:
        return ('Piercing Pattern', 'bull')
    return None


def dark_cloud(cs, i):
    p, c = cs[i-1], cs[i]
    if is_bull(p) and is_bear(c) and c['o'] > p['h'] and c['c'] < mid(p) and c['c'] > p['o']:
        return ('Dark Cloud Cover', 'bear')
    return None


def tweezer_bottom(cs, i):
    p, c = cs[i-1], cs[i]
    if is_bear(p) and is_bull(c) and abs(p['l'] - c['l']) / (c['l'] or 1) < 0.0007:
        return ('Tweezer Bottom', 'bull')
    return None


def tweezer_top(cs, i):
    p, c = cs[i-1], cs[i]
    if is_bull(p) and is_bear(c) and abs(p['h'] - c['h']) / (c['h'] or 1) < 0.0007:
        return ('Tweezer Top', 'bear')
    return None


def three_white_soldiers(cs, i):
    a, b, c = cs[i-2], cs[i-1], cs[i]
    if (is_bull(a) and is_bull(b) and is_bull(c)
            and b['c'] > a['c'] and c['c'] > b['c']
            and b['o'] > a['o'] and c['o'] > b['o']
            and body(a) / rng(a) > 0.5 and body(b) / rng(b) > 0.5 and body(c) / rng(c) > 0.5):
        return ('Three White Soldiers', 'bull')
    return None


def three_black_crows(cs, i):
    a, b, c = cs[i-2], cs[i-1], cs[i]
    if (is_bear(a) and is_bear(b) and is_bear(c)
            and b['c'] < a['c'] and c['c'] < b['c']
            and b['o'] < a['o'] and c['o'] < b['o']
            and body(a) / rng(a) > 0.5 and body(b) / rng(b) > 0.5 and body(c) / rng(c) > 0.5):
        return ('Three Black Crows', 'bear')
    return None


def morning_star(cs, i):
    a, b, c = cs[i-2], cs[i-1], cs[i]
    if (is_bear(a) and body(a) / rng(a) > 0.5
            and body(b) / rng(b) < 0.3
            and is_bull(c) and c['c'] > mid(a)):
        return ('Morning Star', 'bull')
    return None


def evening_star(cs, i):
    a, b, c = cs[i-2], cs[i-1], cs[i]
    if (is_bull(a) and body(a) / rng(a) > 0.5
            and body(b) / rng(b) < 0.3
            and is_bear(c) and c['c'] < mid(a)):
        return ('Evening Star', 'bear')
    return None


def three_inside_up(cs, i):
    # bullish harami at i-1 then confirming bull close at i
    if bullish_harami(cs, i-1) and is_bull(cs[i]) and cs[i]['c'] > cs[i-1]['c']:
        return ('Three Inside Up', 'bull')
    return None


def three_inside_down(cs, i):
    if bearish_harami(cs, i-1) and is_bear(cs[i]) and cs[i]['c'] < cs[i-1]['c']:
        return ('Three Inside Down', 'bear')
    return None


def three_outside_up(cs, i):
    if bullish_engulfing(cs, i-1) and is_bull(cs[i]) and cs[i]['c'] > cs[i-1]['c']:
        return ('Three Outside Up', 'bull')
    return None


def three_outside_down(cs, i):
    if bearish_engulfing(cs, i-1) and is_bear(cs[i]) and cs[i]['c'] < cs[i-1]['c']:
        return ('Three Outside Down', 'bear')
    return None


DETECTORS = [
    hammer, hanging_man, inverted_hammer, shooting_star,
    dragonfly_doji, gravestone_doji, bull_marubozu, bear_marubozu,
    bullish_engulfing, bearish_engulfing, bullish_harami, bearish_harami,
    piercing, dark_cloud, tweezer_bottom, tweezer_top,
    three_white_soldiers, three_black_crows, morning_star, evening_star,
    three_inside_up, three_inside_down, three_outside_up, three_outside_down,
]


# ----------------------------------------------------------------------------
# Trade simulation
# ----------------------------------------------------------------------------
def simulate(cs, i, bias):
    """Enter at open of candle i+1, bracket exit.

    Fixed size = LOTS contracts (LOTS * CONTRACT_VALUE BTC). Computes gross USD
    P&L from the actual entry/exit prices, plus trading fees:
      - entry leg: taker (market order)
      - TP exit: maker (resting limit order)
      - SL / TIME exit: taker (market order)
    Returns a dict, or None if there is no next bar to enter on.
    """
    entry_idx = i + 1
    if entry_idx >= len(cs):
        return None
    entry = cs[entry_idx]['o']
    if bias == 'bull':
        tp, sl = entry * (1 + TP_PCT), entry * (1 - SL_PCT)
    else:
        tp, sl = entry * (1 - TP_PCT), entry * (1 + SL_PCT)

    # position sizing: fixed number of contracts
    qty_btc = LOTS * CONTRACT_VALUE          # BTC exposure
    contracts = LOTS

    pnl_pct, outcome, exit_idx, exit_px = None, None, None, None
    for j in range(entry_idx, min(entry_idx + MAX_HOLD, len(cs))):
        bar = cs[j]
        if bias == 'bull':
            if bar['l'] <= sl:
                pnl_pct, outcome, exit_idx, exit_px = -SL_PCT, 'SL', j, sl
                break
            if bar['h'] >= tp:
                pnl_pct, outcome, exit_idx, exit_px = TP_PCT, 'TP', j, tp
                break
        else:
            if bar['h'] >= sl:
                pnl_pct, outcome, exit_idx, exit_px = -SL_PCT, 'SL', j, sl
                break
            if bar['l'] <= tp:
                pnl_pct, outcome, exit_idx, exit_px = TP_PCT, 'TP', j, tp
                break
    if outcome is None:
        # time exit at last held bar close
        exit_idx = min(entry_idx + MAX_HOLD - 1, len(cs) - 1)
        exit_px = cs[exit_idx]['c']
        pnl_pct = (exit_px - entry) / entry if bias == 'bull' else (entry - exit_px) / entry
        outcome = 'TIME'

    # gross USD P&L from actual price move on the BTC quantity
    if bias == 'bull':
        gross_usd = (exit_px - entry) * qty_btc
    else:
        gross_usd = (entry - exit_px) * qty_btc

    # fees: entry always taker (market); exit maker if TP (limit), else taker
    entry_fee = entry * qty_btc * TAKER_FEE
    exit_rate = MAKER_FEE if outcome == 'TP' else TAKER_FEE
    exit_fee = exit_px * qty_btc * exit_rate
    fees = entry_fee + exit_fee
    net_usd = gross_usd - fees

    return {
        'pnl_pct': pnl_pct, 'outcome': outcome,
        'entry': entry, 'exit': exit_px,
        'entry_idx': entry_idx, 'exit_idx': exit_idx,
        'qty_btc': qty_btc, 'contracts': contracts,
        'gross_usd': gross_usd, 'fees': fees, 'usd': net_usd,
    }


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')


def trend_series(cs, fast=EMA_FAST, slow=EMA_SLOW, neutral_pct=EMA_NEUTRAL_PCT):
    """Causal per-bar trend from a fast/slow EMA crossover.

    Returns a list the same length as `cs`; each entry is 'bull', 'bear', or
    'neutral' based ONLY on closes up to and including that bar (no look-ahead).
    Entries before `slow` closes exist are 'neutral'.
    """
    closes = [c['c'] for c in cs]
    kf = 2.0 / (fast + 1)
    ks = 2.0 / (slow + 1)
    out = []
    ema_f = ema_s = None
    for idx, px in enumerate(closes):
        # seed each EMA with the simple average once enough bars exist
        if idx == fast - 1:
            ema_f = sum(closes[:fast]) / fast
        elif idx >= fast:
            ema_f = px * kf + ema_f * (1 - kf)
        if idx == slow - 1:
            ema_s = sum(closes[:slow]) / slow
        elif idx >= slow:
            ema_s = px * ks + ema_s * (1 - ks)

        if ema_f is None or ema_s is None or ema_s == 0:
            out.append('neutral')
            continue
        spread_pct = (ema_f - ema_s) / ema_s * 100
        if abs(spread_pct) < neutral_pct:
            out.append('neutral')
        elif ema_f > ema_s:
            out.append('bull')
        else:
            out.append('bear')
    return out


def main():
    print(f'Fetching {SYMBOL} {RESOLUTION} candles for last {DAYS} days from Delta Exchange (paged)...')
    cs = fetch_candles()
    if not cs:
        print('No candles returned.')
        return
    print(f'Got {len(cs)} candles: {fmt_ts(cs[0]["t"])} -> {fmt_ts(cs[-1]["t"])} UTC')
    print(f'Price range: {min(c["l"] for c in cs):,.0f} - {max(c["h"] for c in cs):,.0f} USD\n')

    trend = trend_series(cs)
    n_bull = trend.count('bull')
    n_bear = trend.count('bear')
    n_neu = trend.count('neutral')
    print(f'Trend filter: EMA{EMA_FAST}/EMA{EMA_SLOW} crossover, per-bar (causal).')
    print(f'  Bars by regime -> bull: {n_bull}  bear: {n_bear}  neutral: {n_neu}')
    print(f'  Rule: take bullish patterns only in bull regime, bearish only in bear regime.\n')

    if WHITELIST:
        print(f'Pattern whitelist ACTIVE ({len(WHITELIST)} patterns): {", ".join(WHITELIST)}\n')

    signals = []       # all taken trades (dicts) in chronological order
    filtered = 0       # signals skipped because bias != trend
    busy_until = -1    # exit_idx of the currently open trade (sequential account)
    skipped_busy = 0   # signals skipped because a trade was already open

    for i in range(3, len(cs) - 1):   # need 3 prior bars and 1 forward bar
        regime = trend[i]
        for det in DETECTORS:
            res = det(cs, i)
            if res:
                name, bias = res
                if WHITELIST and name not in WHITELIST:
                    continue
                if bias != regime:        # trend filter
                    filtered += 1
                    continue
                if i + 1 <= busy_until:   # a position is still open -> skip
                    skipped_busy += 1
                    continue
                sim = simulate(cs, i, bias)
                if sim:
                    sim['name'] = name
                    sim['bias'] = bias
                    sim['entry_t'] = cs[sim['entry_idx']]['t']
                    signals.append(sim)
                    busy_until = sim['exit_idx']
                    break  # one signal per bar for the sequential account

    if not signals:
        print(f'No patterns passed the trend filter ({filtered} raw signals filtered out).')
        return

    # ---- sequential equity curve + drawdown ----
    equity = START_EQUITY
    peak = START_EQUITY
    max_dd = 0.0          # max drawdown in USD
    max_dd_pct = 0.0      # max drawdown in %
    curve = [START_EQUITY]
    for s in signals:
        equity += s['usd']
        curve.append(equity)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
        dd_pct = dd / peak * 100 if peak else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
    total_usd = equity - START_EQUITY

    # Per-pattern aggregation
    agg = {}
    for s in signals:
        a = agg.setdefault(s['name'], {'bias': s['bias'], 'n': 0, 'wins': 0,
                                       'gross': 0.0, 'fees': 0.0, 'usd': 0.0,
                                       'contracts': 0.0, 'tp': 0, 'sl': 0, 'time': 0})
        a['n'] += 1
        a['gross'] += s['gross_usd']
        a['fees'] += s['fees']
        a['usd'] += s['usd']
        a['contracts'] += s['contracts']
        if s['usd'] > 0:
            a['wins'] += 1
        a[s['outcome'].lower()] += 1

    print('=' * 104)
    print(f'PATTERN DETECTION + SEQUENTIAL P&L  (TP {TP_PCT*100:.1f}% / SL {SL_PCT*100:.1f}% / '
          f'max hold {MAX_HOLD} bars = {MAX_HOLD*_RES_MIN}min)')
    print(f'Position size: {LOTS} lots = {LOTS*CONTRACT_VALUE:.3f} BTC per trade '
          f'(1 contract = {CONTRACT_VALUE} BTC)')
    print(f'Fees: taker {TAKER_FEE*100:.2f}% (entry, SL, time exit), maker {MAKER_FEE*100:.2f}% (TP exit)')
    print(f'Account: one position at a time, start equity ${START_EQUITY:,.0f}')
    print(f'Trend filter removed {filtered} counter-trend signals; '
          f'{skipped_busy} skipped while in a trade; {len(signals)} trades taken.\n')
    header = (f'{"Pattern":<22}{"Bias":<6}{"Trades":>7}{"Win%":>6}{"TP":>4}{"SL":>4}{"Time":>5}'
              f'{"Gross$":>11}{"Fees$":>10}{"Net$":>11}')
    print(header)
    print('-' * len(header))

    total_n = 0
    total_wins = 0
    for name, a in sorted(agg.items(), key=lambda kv: kv[1]['usd'], reverse=True):
        win_rate = a['wins'] / a['n'] * 100
        total_n += a['n']
        total_wins += a['wins']
        print(f'{name:<22}{a["bias"]:<6}{a["n"]:>7}{win_rate:>5.0f}%{a["tp"]:>4}{a["sl"]:>4}'
              f'{a["time"]:>5}{a["gross"]:>+11,.2f}{a["fees"]:>10,.2f}{a["usd"]:>+11,.2f}')

    print('-' * len(header))
    overall_win = (total_wins / total_n * 100) if total_n else 0
    avg_ctr_all = sum(s['contracts'] for s in signals) / len(signals)
    total_gross = sum(s['gross_usd'] for s in signals)
    total_fees = sum(s['fees'] for s in signals)
    print(f'{"TOTAL":<22}{"":<6}{total_n:>7}{overall_win:>5.0f}%{"":>4}{"":>4}{"":>5}'
          f'{total_gross:>+11,.2f}{total_fees:>10,.2f}{total_usd:>+11,.2f}')
    print('=' * 104)

    # ---- account summary ----
    print('\nACCOUNT SUMMARY (single sequential account)')
    print(f'  Trades taken ................ {len(signals)}')
    print(f'  Win rate (net) .............. {overall_win:.1f}%')
    print(f'  Position size ............... {LOTS} lots = {LOTS*CONTRACT_VALUE:.3f} BTC per trade')
    print(f'  Total BTC traded (sum) ...... {sum(s["qty_btc"] for s in signals):.4f} BTC')
    print(f'  Total volume traded ......... ${sum((s["entry"]+s["exit"])*s["qty_btc"] for s in signals):,.0f} '
          f'(both legs)')
    print(f'  Start equity ................ ${START_EQUITY:,.2f}')
    print(f'  Gross P&L (before fees) ..... ${total_gross:+,.2f}')
    print(f'  Total fees paid ............. ${total_fees:,.2f}')
    print(f'  Net P&L (after fees) ........ ${total_usd:+,.2f}  ({total_usd/START_EQUITY*100:+.2f}%)')
    print(f'  End equity .................. ${equity:,.2f}')
    print(f'  Peak equity ................. ${peak:,.2f}')
    print(f'  Max drawdown ................ ${max_dd:,.2f}  ({max_dd_pct:.2f}%)')

    # ---- month-by-month breakdown ----
    months = {}  # 'YYYY-MM' -> aggregates, trades listed in chronological order
    for s in signals:
        key = datetime.fromtimestamp(s['entry_t'], tz=timezone.utc).strftime('%Y-%m')
        m = months.setdefault(key, {'n': 0, 'wins': 0, 'gross': 0.0,
                                     'fees': 0.0, 'net': 0.0, 'pnls': []})
        m['n'] += 1
        m['gross'] += s['gross_usd']
        m['fees'] += s['fees']
        m['net'] += s['usd']
        if s['usd'] > 0:
            m['wins'] += 1
        m['pnls'].append(s['usd'])

    print('\nMONTH-BY-MONTH BREAKDOWN (bucketed by trade entry month)')
    mh = (f'  {"Month":<9}{"Trades":>7}{"Win%":>6}{"Gross$":>11}{"Fees$":>10}'
          f'{"Net$":>11}{"MaxDD$":>10}')
    print(mh)
    print('  ' + '-' * (len(mh) - 2))
    for key in sorted(months):
        m = months[key]
        # intra-month drawdown from the sequence of trade P&Ls
        eq = pk = 0.0
        mdd = 0.0
        for p in m['pnls']:
            eq += p
            pk = max(pk, eq)
            mdd = max(mdd, pk - eq)
        win = m['wins'] / m['n'] * 100 if m['n'] else 0
        print(f'  {key:<9}{m["n"]:>7}{win:>5.0f}%{m["gross"]:>+11,.2f}'
              f'{m["fees"]:>10,.2f}{m["net"]:>+11,.2f}{mdd:>10,.2f}')
    print('  ' + '-' * (len(mh) - 2))

    print('\nNotes:')
    print(f' - Size = {LOTS} lots x {CONTRACT_VALUE} BTC = {LOTS*CONTRACT_VALUE:.3f} BTC per trade; '
          f'P&L = price move x {LOTS*CONTRACT_VALUE:.3f} BTC.')
    print(f' - Fees: entry taker {TAKER_FEE*100:.2f}%; exit maker {MAKER_FEE*100:.2f}% on TP, '
          f'taker {TAKER_FEE*100:.2f}% on SL/time. Charged on price x BTC qty per leg.')
    print(' - Single account: only one position open at a time; overlapping signals are skipped.')
    print(' - Max drawdown is the largest peak-to-trough drop of the equity curve.')
    print(' - No slippage/funding modeled; intrabar TP/SL assumed fillable.')
    print(' - This is an educational estimate of edge, not a live trading result.')


if __name__ == '__main__':
    main()
