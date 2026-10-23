"""Iron condor backtest on REAL NSE/BSE bhavcopy data — WEEKLY, expiry-day entry.

Strategy:
  - Trade ONCE PER WEEK. Enter on each NIFTY weekly EXPIRY day (detected from the
    data, not a hardcoded weekday — NIFTY expiry moved Thu->Mon->Tue over the window).
  - On expiry day, sell the NEXT weekly expiry (the fresh ~7-DTE contract) and hold
    it ~1 week until that contract expires (or TP/SL fires first).
  - Iron condor, no directional view:
        SELL sell_delta call  +  BUY buy_delta call   (upper wing)
        SELL sell_delta put   +  BUY buy_delta put    (lower wing)
    net credit = (call-spread credit) + (put-spread credit).
  - TP: take profit when condor value decays to (1 - tp_pct) of entry credit.
        tp_pct = 0.50  -> exit when you can close for 50% of credit (keep half).
  - SL: stop when loss = sl_pct of entry credit.
        sl_pct = 1.00  -> exit when cost-to-close = 2x credit (lost one full premium).
  - ONE condor open at a time (weekly cadence, no overlap).

Delta is NOT in the bhavcopy; IV is back-solved from each settle price via
Black-Scholes, then delta computed, to locate the 20d/5d strikes. The 5d buy
wings are deep-OTM and thinly traded, so those strikes are the noisiest part.

Management is checked once per day at SETTLE (bhavcopy is EOD, not intraday).

Usage:
    python backtest/nse_iron_condor_backtest.py --symbol NIFTY --lots 3 \
        --sell-delta 0.20 --buy-delta 0.05 --tp 0.50 --sl 1.00 \
        --start 2024-07-01 --end 2026-10-13
"""

import os
import sys
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.nse_bhavcopy import get_option_chain  # noqa: E402
from backtest.nse_ema_spread_backtest import (  # noqa: E402
    NSE_LOT_SIZES, _year_frac, _compute_deltas, _find_by_delta, daterange,
)


def _settle(row):
    return row['settle'] if row['settle'] > 0 else row['close']


def _read_lot_size(days, symbol, instrument, exchange):
    """Read a stock's lot size (NewBrdLotQty) straight from a cached bhavcopy.

    get_option_chain doesn't carry NewBrdLotQty into parsed rows, so re-read the
    raw CSV for the first available trading day and pull it for this symbol.
    """
    import csv as _csv
    import io as _io
    from backtest.nse_bhavcopy import download_bhavcopy
    for c in days:
        ds = c['date'].replace('-', '')
        raw = download_bhavcopy(ds, exchange=exchange)
        if not raw:
            continue
        for row in _csv.DictReader(_io.StringIO(raw)):
            if row.get('TckrSymb') == symbol and row.get('FinInstrmTp') == instrument:
                try:
                    return int(float(row.get('NewBrdLotQty') or 0))
                except (ValueError, TypeError):
                    pass
        break
    return None


# Index symbols use IDO (index options); everything else is a single stock
# using STO (stock options). Stock options are MONTHLY-only (SEBI banned weekly
# single-stock options), so cadence is forced to monthly for stocks.
INDEX_SYMBOLS = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}

# Known stock lot sizes (NewBrdLotQty). Falls back to reading it from the data.
STOCK_LOT_SIZES = {
    'RELIANCE': 500,
}


def run_backtest(symbol, lots, sell_delta, buy_delta, tp_pct, sl_pct,
                 start, end, r=0.065, verbose=True, exchange='NSE',
                 cadence='weekly'):
    is_index = symbol.upper() in INDEX_SYMBOLS
    instrument = 'IDO' if is_index else 'STO'
    if not is_index and cadence != 'monthly':
        print(f"[note] {symbol} is a single stock (STO) — stock options are "
              "MONTHLY-only, forcing cadence=monthly.")
        cadence = 'monthly'

    if is_index:
        lot_size = NSE_LOT_SIZES.get(symbol, 50)
    else:
        lot_size = STOCK_LOT_SIZES.get(symbol.upper(), 0)
    qty = lots * lot_size  # may be 0 until we read lot size from data below

    # 1) Load every available trading day's chain in range (cached on disk).
    print(f"Loading {exchange} bhavcopy data {start} .. {end} ...", flush=True)
    days = []
    n_probe = 0
    for d in daterange(start, end):
        if d.weekday() >= 5:
            continue
        ds = d.strftime('%Y%m%d')
        chain = get_option_chain(ds, symbol=symbol, instrument=instrument, exchange=exchange)
        n_probe += 1
        if n_probe % 50 == 0:
            print(f"  ...probed {n_probe} weekdays, {len(days)} trading days so far", flush=True)
        if chain:
            days.append(chain)
    print(f"Loaded {len(days)} trading days with data.\n", flush=True)
    if len(days) < 5:
        print("Not enough data.")
        return []

    # For stocks, if the lot size wasn't preset, read it from the data.
    if qty == 0:
        lot_size = _read_lot_size(days, symbol, instrument, exchange) or 1
        qty = lots * lot_size
        print(f"[note] {symbol} lot size detected = {lot_size} "
              f"-> {lots} lots = {qty} qty\n", flush=True)

    chain_by_date = {c['date']: c for c in days}
    ordered_dates = [c['date'] for c in days]
    date_index = {dt: i for i, dt in enumerate(ordered_dates)}

    # 2) Detect expiry days. An expiry day = an XpryDt value that is also a
    #    trading day we have data for (so we can actually enter that morning).
    all_expiries = set()
    for c in days:
        all_expiries.update(c['expiries'])

    if cadence == 'monthly':
        # Monthly expiry = the LAST expiry within each calendar month. Enter on
        # each monthly-expiry trading day and sell the NEXT month's monthly.
        by_month = {}
        for e in all_expiries:
            by_month.setdefault(e[:7], []).append(e)
        monthly_expiries = sorted(max(v) for v in by_month.values())
        monthly_set = set(monthly_expiries)
        entry_days = sorted(e for e in monthly_expiries if e in chain_by_date)
    else:
        entry_days = sorted(e for e in all_expiries if e in chain_by_date)
        monthly_set = None

    trades = []
    skipped = 0

    for entry_date in entry_days:
        chain = chain_by_date[entry_date]
        spot = chain['spot']

        # Choose the expiry to SELL.
        future_exp = sorted(e for e in chain['expiries']
                            if datetime.strptime(e, '%Y-%m-%d').date()
                            > datetime.strptime(entry_date, '%Y-%m-%d').date())
        if cadence == 'monthly':
            # Sell the NEXT monthly expiry (next calendar month's last expiry).
            future_monthly = [e for e in future_exp if e in monthly_set]
            if not future_monthly:
                skipped += 1
                continue
            expiry = future_monthly[0]
        else:
            if not future_exp:
                skipped += 1
                continue
            expiry = future_exp[0]  # next weekly

        leg = chain['options'].get(expiry)
        if not leg:
            skipped += 1
            continue

        T = _year_frac(entry_date, expiry)

        # Call side (upper wing) and put side (lower wing).
        call_rows = _compute_deltas(leg['CE'], spot, T, r, True)
        put_rows = _compute_deltas(leg['PE'], spot, T, r, False)
        if not call_rows or not put_rows:
            skipped += 1
            continue

        sell_call = _find_by_delta(call_rows, sell_delta)
        buy_call = _find_by_delta(call_rows, buy_delta)
        sell_put = _find_by_delta(put_rows, sell_delta)
        buy_put = _find_by_delta(put_rows, buy_delta)
        if not all([sell_call, buy_call, sell_put, buy_put]):
            skipped += 1
            continue
        # wings must be distinct from the sold strikes
        if (sell_call['strike'] == buy_call['strike'] or
                sell_put['strike'] == buy_put['strike']):
            skipped += 1
            continue

        # Net credit = both spreads' credit.
        call_credit = _settle(sell_call) - _settle(buy_call)
        put_credit = _settle(sell_put) - _settle(buy_put)
        credit_per_unit = call_credit + put_credit
        if credit_per_unit <= 0:
            skipped += 1
            continue
        credit = credit_per_unit * qty

        tp_value = credit * (1 - tp_pct)   # close for this -> take profit
        sl_value = credit * (1 + sl_pct)   # close for this -> stop loss

        ks = {
            'sc': sell_call['strike'], 'bc': buy_call['strike'],
            'sp': sell_put['strike'], 'bp': buy_put['strike'],
        }

        exit_reason = 'expiry'
        exit_value = None
        exit_date = expiry

        # 3) Manage day-over-day on settle prices until TP/SL/expiry.
        start_i = date_index.get(entry_date, -1)
        for j in range(start_i + 1, len(days)):
            fday = days[j]
            fdate = fday['date']
            if datetime.strptime(fdate, '%Y-%m-%d').date() > \
               datetime.strptime(expiry, '%Y-%m-%d').date():
                break
            fleg = fday['options'].get(expiry)
            if not fleg:
                continue
            try:
                scv = _settle(fleg['CE'][ks['sc']])
                bcv = _settle(fleg['CE'][ks['bc']])
                spv = _settle(fleg['PE'][ks['sp']])
                bpv = _settle(fleg['PE'][ks['bp']])
            except KeyError:
                continue
            condor_value = ((scv - bcv) + (spv - bpv)) * qty  # cost to close now
            if condor_value <= tp_value:
                exit_reason = 'target'
                exit_value = condor_value
                exit_date = fdate
                break
            if condor_value >= sl_value:
                exit_reason = 'stoploss'
                exit_value = condor_value
                exit_date = fdate
                break

        # Settle at intrinsic if held to expiry.
        if exit_value is None:
            exp_chain = chain_by_date.get(expiry)
            es = exp_chain['spot'] if exp_chain else spot
            call_intr = max(0.0, es - ks['sc']) - max(0.0, es - ks['bc'])
            put_intr = max(0.0, ks['sp'] - es) - max(0.0, ks['bp'] - es)
            exit_value = (call_intr + put_intr) * qty

        pnl = credit - exit_value
        width = max(abs(ks['sc'] - ks['bc']), abs(ks['sp'] - ks['bp']))
        trades.append({
            'date': entry_date, 'expiry': expiry, 'spot': spot,
            'sc': ks['sc'], 'bc': ks['bc'], 'sp': ks['sp'], 'bp': ks['bp'],
            'credit': credit, 'exit_value': exit_value,
            'exit_reason': exit_reason, 'exit_date': exit_date, 'pnl': pnl,
            'width': width,
        })

    _report(trades, skipped, symbol, lots, qty, sell_delta, buy_delta,
            tp_pct, sl_pct, verbose, exchange, cadence)
    return trades


def _report(trades, skipped, symbol, lots, qty, sell_delta, buy_delta,
            tp_pct, sl_pct, verbose, exchange='NSE', cadence='weekly'):
    label = cadence.upper()
    print("=" * 80)
    print(f"  IRON CONDOR BACKTEST — {symbol} [{exchange}]  (REAL bhavcopy, {label}/expiry-day)")
    print("=" * 80)
    print(f"  Lots: {lots} ({qty} qty) | Sell {sell_delta}d / Buy {buy_delta}d "
          f"both wings | TP {tp_pct*100:.0f}% / SL {sl_pct*100:.0f}% of credit")
    print("-" * 80)

    if not trades:
        print(f"  No trades generated. (skipped {skipped} expiry days)")
        return

    if verbose:
        print(f"  {'ENTRY':<11}{'EXPIRY':<11}{'PUTS sp/bp':<16}{'CALLS sc/bc':<16}"
              f"{'CREDIT':>9}{'PNL':>10}  EXIT")
        for t in trades:
            puts = f"{int(t['sp'])}/{int(t['bp'])}"
            calls = f"{int(t['sc'])}/{int(t['bc'])}"
            print(f"  {t['date']:<11}{t['expiry']:<11}{puts:<16}{calls:<16}"
                  f"{t['credit']:>9.0f}{t['pnl']:>10.0f}  {t['exit_reason']}@{t['exit_date']}")
        print("-" * 80)

    total = sum(t['pnl'] for t in trades)
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    n = len(trades)
    win_rate = len(wins) / n * 100 if n else 0

    cum = peak = max_dd = 0.0
    for t in trades:
        cum += t['pnl']
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    by_reason = {}
    for t in trades:
        by_reason[t['exit_reason']] = by_reason.get(t['exit_reason'], 0) + 1

    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0

    unit = 'months' if cadence == 'monthly' else 'weeks'
    print(f"  Trades ({unit}): {n}   (skipped {skipped} expiry days)")
    print(f"  Win rate:       {win_rate:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"  Total P&L:      Rs {total:,.0f}")
    print(f"  Avg win:        Rs {avg_win:,.0f}    Avg loss: Rs {avg_loss:,.0f}")
    print(f"  Max drawdown:   Rs {max_dd:,.0f}")
    print(f"  Exit reasons:   {by_reason}")

    # Capital: one condor at a time, so peak fund = max single-condor defined risk.
    # Iron condor max loss = widest wing width * qty - credit (only one side can
    # finish ITM beyond the short strike).
    per_risk = [max(t['width'] * qty - t['credit'], 0.0) for t in trades]
    max_risk = max(per_risk) if per_risk else 0
    avg_risk = sum(per_risk) / len(per_risk) if per_risk else 0
    print("-" * 80)
    print(f"  CAPITAL / MARGIN REQUIRED (one condor at a time, {cadence})")
    print(f"  Defined max risk per condor (avg): Rs {avg_risk:,.0f}")
    print(f"  PEAK FUND REQUIRED:                Rs {max_risk:,.0f}  "
          "(worst single condor's max loss)")

    # Monthly breakdown
    months = {}
    for t in trades:
        key = t['date'][:7]
        m = months.setdefault(key, {'pnl': 0.0, 'n': 0, 'w': 0, 'l': 0})
        m['pnl'] += t['pnl']; m['n'] += 1
        if t['pnl'] > 0:
            m['w'] += 1
        else:
            m['l'] += 1
    print("-" * 80)
    print("  MONTHLY BREAKDOWN")
    print(f"  {'MONTH':<10}{'WEEKS':>7}{'W/L':>9}{'WIN%':>7}{'P&L':>13}{'CUM':>13}")
    cum_m = 0.0
    for key in sorted(months):
        m = months[key]
        cum_m += m['pnl']
        wr = m['w'] / m['n'] * 100 if m['n'] else 0
        print(f"  {key:<10}{m['n']:>7}{(str(m['w'])+'/'+str(m['l'])):>9}"
              f"{wr:>6.0f}%{m['pnl']:>13,.0f}{cum_m:>13,.0f}")
    print("-" * 80)
    print("  NOTE: Management checked once per day at SETTLE (bhavcopy is EOD, not intraday).")
    print(f"  NOTE: {buy_delta}d buy wings are deep-OTM; their settle prices are small and")
    print("        sometimes stale, so those strikes (delta via Black-Scholes) are approximate.")
    print("=" * 80)


def main():
    ap = argparse.ArgumentParser(description="Iron condor backtest on real bhavcopy data (weekly)")
    ap.add_argument('--symbol', default='NIFTY')
    ap.add_argument('--lots', type=int, default=3)
    ap.add_argument('--sell-delta', type=float, default=0.20)
    ap.add_argument('--buy-delta', type=float, default=0.05)
    ap.add_argument('--tp', type=float, default=0.50, help='take-profit as fraction of credit')
    ap.add_argument('--sl', type=float, default=1.00, help='stop-loss as fraction of credit')
    ap.add_argument('--start', default='2024-07-01')
    ap.add_argument('--end', default=datetime.now().strftime('%Y-%m-%d'))
    ap.add_argument('--rate', type=float, default=0.065)
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--exchange', default=None, choices=['NSE', 'BSE'])
    ap.add_argument('--cadence', default='weekly', choices=['weekly', 'monthly'],
                    help='expiry cadence: weekly (default) or monthly')
    args = ap.parse_args()

    exchange = args.exchange
    if exchange is None:
        exchange = 'BSE' if args.symbol.upper() in ('SENSEX', 'BANKEX') else 'NSE'

    start = datetime.strptime(args.start, '%Y-%m-%d').date()
    end = datetime.strptime(args.end, '%Y-%m-%d').date()

    run_backtest(args.symbol, args.lots, args.sell_delta, args.buy_delta,
                 args.tp, args.sl, start, end, r=args.rate,
                 verbose=not args.quiet, exchange=exchange, cadence=args.cadence)


if __name__ == '__main__':
    main()
