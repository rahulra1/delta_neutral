"""Faithful backtest of the NseEmaCreditSpread strategy on REAL NSE bhavcopy data.

Strategy rules (mirrors strategy/nse_ema_spread.py):
  - Each trading day, compute EMA(period) on daily index closes.
  - spot < EMA  -> bearish -> BEAR CALL spread  (sell SELL_DELTA call, buy BUY_DELTA call)
  - spot > EMA  -> bullish -> BULL PUT  spread  (sell SELL_DELTA put,  buy BUY_DELTA put)
  - Expiry: 2nd-nearest weekly (expiries[1]), same as live.
  - Net credit = (sell_settle - buy_settle) * qty ; require > 0.
  - Manage day-over-day on subsequent bhavcopy settle prices:
        TP: spread value decays to (1 - tp_pct) of entry credit   [tp_pct=0.90 -> 10% left]
        SL: spread value grows to (1 + sl_pct) of entry credit     [sl_pct=1.50]
        Else hold to expiry and settle at intrinsic.
  (Bhavcopy is end-of-day, so management is checked once per day at settle, not intraday.
   Entry time 9:31 is recorded for reference; entry price uses the entry-day settle, the
   only real per-strike price the bhavcopy provides.)

Delta is NOT in the bhavcopy, so we back-solve implied vol from each option's
settle price via Black-Scholes, then compute delta to locate the SELL/BUY strikes.
The deep-OTM BUY leg (e.g. 5-delta) has a tiny, sometimes stale settle price, so
its strike selection is the noisiest part of this backtest — flagged in the report.

Usage:
    python backtest/nse_ema_spread_backtest.py \
        --symbol NIFTY --lots 3 --sell-delta 0.20 --buy-delta 0.05 \
        --ema 10 --tp 0.90 --sl 1.50 --start 2024-07-01 --end 2026-10-10
    python3 
  backtest/nse_ema_spread_backtest.py --symbol SENSEX --lots 3 --sell-delta 0.20 --buy-delta 0.05 --ema 10 --tp 
  0.90 --sl 1.50 --start 2024-01-01 --end 2026-10-13 --quiet    
"""

import os
import sys
import math
import argparse
from datetime import datetime, timedelta, date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.nse_bhavcopy import get_option_chain  # noqa: E402

NSE_LOT_SIZES = {
    'NIFTY': 65, 'BANKNIFTY': 30, 'FINNIFTY': 65, 'MIDCPNIFTY': 50, 'SENSEX': 20,
}

# ─── Black-Scholes: price, implied vol, delta ────────────────────────────

SQRT_2PI = math.sqrt(2 * math.pi)


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs_price(S, K, T, r, sigma, is_call):
    """Black-Scholes European option price (no dividend)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        # Intrinsic at/near expiry.
        intrinsic = (S - K) if is_call else (K - S)
        return max(0.0, intrinsic)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_delta(S, K, T, r, sigma, is_call):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        # Degenerate: delta is 1/0 for calls, -1/0 for puts.
        if is_call:
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) if is_call else (_norm_cdf(d1) - 1.0)


def _implied_vol(price, S, K, T, r, is_call):
    """Back-solve IV from a market price via bisection. Returns None if infeasible."""
    if price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None
    intrinsic = max(0.0, (S - K) if is_call else (K - S))
    if price < intrinsic - 0.01:
        return None  # price below intrinsic -> no real IV (stale/bad print)
    lo, hi = 1e-4, 6.0  # 0.01% .. 600% vol
    plo = _bs_price(S, K, T, r, lo, is_call)
    phi = _bs_price(S, K, T, r, hi, is_call)
    if price <= plo:
        return lo
    if price >= phi:
        return hi
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        pm = _bs_price(S, K, T, r, mid, is_call)
        if abs(pm - price) < 1e-4:
            return mid
        if pm < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ─── EMA ─────────────────────────────────────────────────────────────────

def _calc_ema(closes, period):
    if len(closes) < period:
        return None
    mult = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = (p - ema) * mult + ema
    return ema


# ─── Delta-based strike selection ─────────────────────────────────────────

def _year_frac(d0, d1):
    """Calendar-day year fraction between two 'YYYY-MM-DD' dates."""
    a = datetime.strptime(d0, '%Y-%m-%d').date()
    b = datetime.strptime(d1, '%Y-%m-%d').date()
    return max((b - a).days, 0) / 365.0


def _compute_deltas(leg_dict, spot, T, r, is_call):
    """Attach a BS delta (abs value) to each strike row in a CE/PE dict.

    Returns a list of (abs_delta, row) with a usable IV. Rows whose settle
    price yields no feasible IV are skipped.
    """
    out = []
    for strike, row in leg_dict.items():
        price = row['settle'] if row['settle'] > 0 else row['close']
        if price <= 0:
            continue
        iv = _implied_vol(price, spot, strike, T, r, is_call)
        if iv is None:
            continue
        delta = _bs_delta(spot, strike, T, r, iv, is_call)
        row['_iv'] = iv
        row['_delta'] = delta
        out.append((abs(delta), row))
    return out


def _find_by_delta(delta_rows, target):
    """Pick the row whose |delta| is closest to target. Returns row or None."""
    if not delta_rows:
        return None
    best = min(delta_rows, key=lambda t: abs(t[0] - target))
    return best[1]


# ─── Backtest ──────────────────────────────────────────────────────────────

def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def run_backtest(symbol, lots, sell_delta, buy_delta, ema_period,
                 tp_pct, sl_pct, start, end, r=0.065, verbose=True,
                 exchange='NSE'):
    lot_size = NSE_LOT_SIZES.get(symbol, 50)
    qty = lots * lot_size
    # 1) Load every available trading day's chain in range (cached on disk).
    print(f"Loading {exchange} bhavcopy data {start} .. {end} ...", flush=True)
    days = []  # list of chain dicts, in date order
    index_closes = []  # (date, spot) for EMA
    n_probe = 0
    for d in daterange(start, end):
        if d.weekday() >= 5:
            continue  # skip Sat/Sun fast (no bhavcopy)
        ds = d.strftime('%Y%m%d')
        chain = get_option_chain(ds, symbol=symbol, instrument='IDO', exchange=exchange)
        n_probe += 1
        if n_probe % 25 == 0:
            print(f"  ...probed {n_probe} weekdays, {len(days)} trading days so far", flush=True)
        if not chain:
            continue
        days.append(chain)
        index_closes.append((chain['date'], chain['spot']))

    print(f"Loaded {len(days)} trading days with data.\n", flush=True)
    if len(days) <= ema_period:
        print("Not enough trading days to seed the EMA. Widen the date range.")
        return

    # Index closes keyed by date for EMA lookups.
    closes_by_date = {dt: sp for dt, sp in index_closes}
    ordered_dates = [c['date'] for c in days]
    chain_by_date = {c['date']: c for c in days}

    trades = []
    skipped = 0

    # 2) Walk each trading day; need >= ema_period prior closes to compute EMA.
    for i in range(ema_period, len(days)):
        chain = days[i]
        today = chain['date']
        spot = chain['spot']

        # EMA from the closes up to and including today.
        closes_seq = [closes_by_date[dt] for dt in ordered_dates[:i + 1]]
        ema = _calc_ema(closes_seq, ema_period)
        if ema is None:
            continue

        bearish = spot < ema
        is_call = bearish  # bearish -> call spread ; bullish -> put spread

        # Pick 2nd-nearest weekly expiry (expiries[1]), same as live.
        future_exp = [e for e in chain['expiries']
                      if datetime.strptime(e, '%Y-%m-%d').date()
                      >= datetime.strptime(today, '%Y-%m-%d').date()]
        if len(future_exp) < 2:
            skipped += 1
            continue
        expiry = future_exp[1]

        leg = chain['options'].get(expiry)
        if not leg:
            skipped += 1
            continue

        T = _year_frac(today, expiry)
        side = 'CE' if is_call else 'PE'
        delta_rows = _compute_deltas(leg[side], spot, T, r, is_call)
        if not delta_rows:
            skipped += 1
            continue

        sell_opt = _find_by_delta(delta_rows, sell_delta)
        buy_opt = _find_by_delta(delta_rows, buy_delta)
        if not sell_opt or not buy_opt or sell_opt['strike'] == buy_opt['strike']:
            skipped += 1
            continue

        sell_entry = sell_opt['settle'] if sell_opt['settle'] > 0 else sell_opt['close']
        buy_entry = buy_opt['settle'] if buy_opt['settle'] > 0 else buy_opt['close']
        credit_per_unit = sell_entry - buy_entry
        if credit_per_unit <= 0:
            skipped += 1
            continue
        credit = credit_per_unit * qty

        # 3) Manage day-over-day on subsequent settle prices until TP/SL/expiry.
        tp_value = credit * (1 - tp_pct)   # spread value at which we take profit
        sl_value = credit * (1 + sl_pct)   # spread value at which we stop out
        exit_reason = 'expiry'
        exit_value = None
        exit_date = expiry

        sell_strike = sell_opt['strike']
        buy_strike = buy_opt['strike']

        for j in range(i + 1, len(days)):
            fday = days[j]
            fdate = fday['date']
            if datetime.strptime(fdate, '%Y-%m-%d').date() > \
               datetime.strptime(expiry, '%Y-%m-%d').date():
                break
            fleg = fday['options'].get(expiry)
            if not fleg:
                continue
            srow = fleg[side].get(sell_strike)
            brow = fleg[side].get(buy_strike)
            if not srow or not brow:
                continue
            sv = srow['settle'] if srow['settle'] > 0 else srow['close']
            bv = brow['settle'] if brow['settle'] > 0 else brow['close']
            spread_value = (sv - bv) * qty  # cost to close now
            if spread_value <= tp_value:
                exit_reason = 'target'
                exit_value = spread_value
                exit_date = fdate
                break
            if spread_value >= sl_value:
                exit_reason = 'stoploss'
                exit_value = spread_value
                exit_date = fdate
                break

        if exit_value is None:
            # Held to expiry: settle at intrinsic on the underlying's expiry spot.
            exp_chain = chain_by_date.get(expiry)
            exp_spot = exp_chain['spot'] if exp_chain else spot
            if is_call:
                sell_intr = max(0.0, exp_spot - sell_strike)
                buy_intr = max(0.0, exp_spot - buy_strike)
            else:
                sell_intr = max(0.0, sell_strike - exp_spot)
                buy_intr = max(0.0, buy_strike - exp_spot)
            exit_value = (sell_intr - buy_intr) * qty

        pnl = credit - exit_value  # credit received minus cost to close
        trades.append({
            'date': today, 'direction': 'bearish' if bearish else 'bullish',
            'type': 'bear_call' if is_call else 'bull_put',
            'expiry': expiry, 'spot': spot, 'ema': ema,
            'sell_strike': sell_strike, 'buy_strike': buy_strike,
            'sell_delta': sell_opt.get('_delta', 0), 'buy_delta': buy_opt.get('_delta', 0),
            'sell_entry': sell_entry, 'buy_entry': buy_entry,
            'credit': credit, 'exit_value': exit_value,
            'exit_reason': exit_reason, 'exit_date': exit_date, 'pnl': pnl,
        })

    _report(trades, skipped, symbol, lots, qty, sell_delta, buy_delta,
            ema_period, tp_pct, sl_pct, verbose, exchange)
    return trades


def _capital_report(trades, qty):
    """Estimate the capital needed to actually run the strategy.

    The strategy opens a NEW spread every trading day and holds each until its
    own TP/SL/expiry, so spreads OVERLAP. Peak fund = the worst simultaneous
    sum of (defined max risk) across all spreads open on the same day, since a
    defined-risk credit spread's broker margin is capped at that max loss.

    Defined max risk per spread = strike_width * qty - credit_received.
    (Worst case: both legs finish fully in-the-money; you lose the width, offset
    by the credit you took in.)
    """
    from datetime import datetime as _dt

    def d(s):
        return _dt.strptime(s[:10], '%Y-%m-%d').date()

    # Build per-spread risk + its open interval [entry, exit].
    spreads = []
    for t in trades:
        width = abs(t['sell_strike'] - t['buy_strike'])
        max_risk = max(width * qty - t['credit'], 0.0)
        spreads.append({
            'start': d(t['date']),
            'end': d(t['exit_date']),
            'risk': max_risk,
            'credit': t['credit'],
        })

    # Sweep every date on which any spread is open; sum risk of all spreads
    # whose [start, end] interval covers that date.
    all_dates = sorted({s['start'] for s in spreads} | {s['end'] for s in spreads})
    peak_risk = 0.0
    peak_count = 0
    peak_date = None
    max_count = 0
    for day in all_dates:
        open_here = [s for s in spreads if s['start'] <= day <= s['end']]
        tot_risk = sum(s['risk'] for s in open_here)
        if len(open_here) > max_count:
            max_count = len(open_here)
        if tot_risk > peak_risk:
            peak_risk = tot_risk
            peak_count = len(open_here)
            peak_date = day

    avg_risk = sum(s['risk'] for s in spreads) / len(spreads) if spreads else 0

    print("-" * 78)
    print("  CAPITAL / MARGIN REQUIRED")
    print(f"  Defined max risk per spread (avg): ₹{avg_risk:,.0f}")
    print(f"  Max concurrent open spreads:       {max_count}")
    print(f"  PEAK FUND REQUIRED (worst day):    ₹{peak_risk:,.0f}  "
          f"({peak_count} spreads open on {peak_date})")
    print("  (Peak fund = sum of defined max-risk of all spreads open that day; "
          "for a")
    print("   defined-risk credit spread the broker margin is capped at that max "
          "loss.)")


def _report(trades, skipped, symbol, lots, qty, sell_delta, buy_delta,
            ema_period, tp_pct, sl_pct, verbose, exchange='NSE'):
    print("=" * 78)
    print(f"  EMA CREDIT SPREAD BACKTEST — {symbol} [{exchange}]  (REAL bhavcopy data)")
    print("=" * 78)
    print(f"  Lots: {lots} ({qty} qty) | EMA{ema_period} | "
          f"Sell {sell_delta}Δ / Buy {buy_delta}Δ | TP {tp_pct*100:.0f}% / SL {sl_pct*100:.0f}%")
    print("-" * 78)

    if not trades:
        print(f"  No trades generated. (skipped {skipped} days for data/expiry/credit reasons)")
        return

    if verbose:
        print(f"  {'DATE':<11}{'DIR':<9}{'TYPE':<10}{'SELL/BUY K':<16}"
              f"{'CREDIT':>10}{'PNL':>11}  EXIT")
        for t in trades:
            ks = f"{int(t['sell_strike'])}/{int(t['buy_strike'])}"
            print(f"  {t['date']:<11}{t['direction']:<9}{t['type']:<10}{ks:<16}"
                  f"{t['credit']:>10.0f}{t['pnl']:>11.0f}  "
                  f"{t['exit_reason']}@{t['exit_date']}")
        print("-" * 78)

    total = sum(t['pnl'] for t in trades)
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    n = len(trades)
    win_rate = len(wins) / n * 100 if n else 0

    # Max drawdown on cumulative equity curve.
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum += t['pnl']
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    by_reason = {}
    for t in trades:
        by_reason[t['exit_reason']] = by_reason.get(t['exit_reason'], 0) + 1

    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0

    print(f"  Trades:        {n}   (skipped {skipped} days)")
    print(f"  Win rate:      {win_rate:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"  Total P&L:     ₹{total:,.0f}")
    print(f"  Avg win:       ₹{avg_win:,.0f}    Avg loss: ₹{avg_loss:,.0f}")
    print(f"  Max drawdown:  ₹{max_dd:,.0f}")
    print(f"  Exit reasons:  {by_reason}")

    _capital_report(trades, qty)

    # ─── Monthly breakdown (grouped by entry month) ──────────────────────
    months = {}
    for t in trades:
        key = t['date'][:7]  # 'YYYY-MM'
        m = months.setdefault(key, {'pnl': 0.0, 'n': 0, 'w': 0, 'l': 0})
        m['pnl'] += t['pnl']
        m['n'] += 1
        if t['pnl'] > 0:
            m['w'] += 1
        else:
            m['l'] += 1
    print("-" * 78)
    print("  MONTHLY BREAKDOWN")
    print(f"  {'MONTH':<10}{'TRADES':>8}{'W/L':>10}{'WIN%':>8}{'P&L':>14}{'CUM P&L':>14}")
    cum_m = 0.0
    for key in sorted(months):
        m = months[key]
        cum_m += m['pnl']
        wr = m['w'] / m['n'] * 100 if m['n'] else 0
        print(f"  {key:<10}{m['n']:>8}{(str(m['w'])+'/'+str(m['l'])):>10}"
              f"{wr:>7.0f}%{m['pnl']:>14,.0f}{cum_m:>14,.0f}")

    print("-" * 78)
    print("  NOTE: Management checked once per day at SETTLE (bhavcopy is EOD, "
          "not intraday).")
    print(f"  NOTE: Buy leg ({buy_delta}Δ) is deep-OTM; its settle price is small "
          "and sometimes")
    print("        stale, so that strike's delta (back-solved via Black-Scholes) "
          "is approximate.")
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser(description="NSE EMA credit spread backtest on real bhavcopy data")
    ap.add_argument('--symbol', default='NIFTY')
    ap.add_argument('--lots', type=int, default=3)
    ap.add_argument('--sell-delta', type=float, default=0.20)
    ap.add_argument('--buy-delta', type=float, default=0.05)
    ap.add_argument('--ema', type=int, default=10, help='EMA period on daily closes')
    ap.add_argument('--tp', type=float, default=0.90, help='take-profit as fraction of credit')
    ap.add_argument('--sl', type=float, default=1.50, help='stop-loss as fraction of credit')
    ap.add_argument('--start', default='2024-07-01')
    ap.add_argument('--end', default=datetime.now().strftime('%Y-%m-%d'))
    ap.add_argument('--rate', type=float, default=0.065, help='risk-free rate for BS')
    ap.add_argument('--quiet', action='store_true', help='suppress per-trade table')
    ap.add_argument('--exchange', default=None, choices=['NSE', 'BSE'],
                    help='data source; defaults by symbol (SENSEX/BANKEX->BSE, else NSE)')
    args = ap.parse_args()

    # Auto-select exchange: SENSEX/BANKEX are BSE instruments; the rest are NSE.
    exchange = args.exchange
    if exchange is None:
        exchange = 'BSE' if args.symbol.upper() in ('SENSEX', 'BANKEX') else 'NSE'

    start = datetime.strptime(args.start, '%Y-%m-%d').date()
    end = datetime.strptime(args.end, '%Y-%m-%d').date()

    run_backtest(args.symbol, args.lots, args.sell_delta, args.buy_delta,
                 args.ema, args.tp, args.sl, start, end, r=args.rate,
                 verbose=not args.quiet, exchange=exchange)


if __name__ == '__main__':
    main()
