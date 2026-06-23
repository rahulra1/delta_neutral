"""Backtest: 0DTE BTC Short Strangle (9 AM - 5 PM IST).

Uses 15m Yahoo Finance BTC-USD candles to simulate daily sessions.
Approximates option premium behavior using Black-Scholes-like delta model:
- At entry: sell call + put ~$100 each at strikes where premium ≈ $100
- A $100 premium option with 0DTE has delta ~0.25-0.30
- SL triggers when underlying moves enough that premium rises to $105 (5% SL)
- For a 0DTE option with ~8h to expiry, a ~$2500 move against strike ≈ $5 premium increase

Simplification: We estimate strike distances from ATM based on $100 premium ≈ 2.5% OTM
for BTC with ~60% IV. If price moves beyond the strike, SL is definitely hit.
If price moves >50% of the distance to strike, SL is likely hit (premium rises ~105%).
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.chart import get_candles
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# Strategy params
ENTRY_PREMIUM = 100      # $100 mark price per leg
SL_PCT = 1.00            # 100% of premium = SL triggers when premium doubles to $200
LOT_SIZE = 100           # 100 contracts
CONTRACT_VALUE = 0.001   # 0.001 BTC per contract → 100 lots = 0.1 BTC
ENTRY_HOUR = 9
ENTRY_MINUTE = 0
EXIT_HOUR = 17
EXIT_MINUTE = 0

# Option model params
# For BTC at ~$100k with 60% IV, 0DTE, $100 premium ≈ strike ~2.5% OTM
OTM_PCT = 0.025  # 2.5% OTM for ~$100 premium
# Premium sensitivity: for 0DTE, premium doubles when price moves ~50% of distance to strike
SL_TRIGGER_PCT = 0.50  # if price moves 50% of distance-to-strike, SL triggers (premium doubled)


def run_backtest():
    print("Fetching BTC 15m candles (60 days)...")
    candles = get_candles('BTC', '15m')
    if not candles:
        print("❌ Failed to fetch candles")
        return

    print(f"✓ Got {len(candles)} candles")

    # Group candles by IST date
    days = {}
    for c in candles:
        dt = datetime.fromtimestamp(c['t'], tz=IST)
        date_key = dt.strftime('%Y-%m-%d')
        if date_key not in days:
            days[date_key] = []
        days[date_key].append({**c, 'dt': dt})

    results = []
    total_pnl = 0

    for date_key in sorted(days.keys()):
        day_candles = days[date_key]

        # Filter candles between 9:00 and 17:00 IST
        session = [c for c in day_candles
                   if (c['dt'].hour > ENTRY_HOUR or (c['dt'].hour == ENTRY_HOUR and c['dt'].minute >= ENTRY_MINUTE))
                   and (c['dt'].hour < EXIT_HOUR or (c['dt'].hour == EXIT_HOUR and c['dt'].minute <= EXIT_MINUTE))]

        if len(session) < 4:
            continue

        # Entry price = first candle's open
        entry_price = session[0]['o']
        call_strike = entry_price * (1 + OTM_PCT)
        put_strike = entry_price * (1 - OTM_PCT)
        call_dist = call_strike - entry_price
        put_dist = entry_price - put_strike

        call_sl_hit = False
        put_sl_hit = False
        call_sl_time = None
        put_sl_time = None

        # Simulate through session
        for c in session:
            # Check call SL: price moved up toward call_strike
            if not call_sl_hit:
                up_move = c['h'] - entry_price
                if up_move > call_dist * SL_TRIGGER_PCT:
                    call_sl_hit = True
                    call_sl_time = c['dt']

            # Check put SL: price moved down toward put_strike
            if not put_sl_hit:
                down_move = entry_price - c['l']
                if down_move > put_dist * SL_TRIGGER_PCT:
                    put_sl_hit = True
                    put_sl_time = c['dt']

        # Calculate P&L (premium × lots × contract_value)
        multiplier = LOT_SIZE * CONTRACT_VALUE
        call_pnl = -(ENTRY_PREMIUM * SL_PCT * multiplier) if call_sl_hit else ENTRY_PREMIUM * _theta_decay(session, call_strike, entry_price, 'call') * multiplier
        put_pnl = -(ENTRY_PREMIUM * SL_PCT * multiplier) if put_sl_hit else ENTRY_PREMIUM * _theta_decay(session, put_strike, entry_price, 'put') * multiplier
        day_pnl = call_pnl + put_pnl
        total_pnl += day_pnl

        outcome = 'both_kept' if not call_sl_hit and not put_sl_hit else \
                  'both_sl' if call_sl_hit and put_sl_hit else 'one_sl'

        results.append({
            'date': date_key,
            'entry': round(entry_price, 0),
            'call_strike': round(call_strike, 0),
            'put_strike': round(put_strike, 0),
            'call_sl': call_sl_hit,
            'put_sl': put_sl_hit,
            'outcome': outcome,
            'call_pnl': round(call_pnl, 2),
            'put_pnl': round(put_pnl, 2),
            'day_pnl': round(day_pnl, 2),
            'cum_pnl': round(total_pnl, 2),
        })

    # Print results
    print(f"\n{'='*80}")
    print(f"0DTE BTC SHORT STRANGLE BACKTEST — 9 AM to 5 PM IST")
    print(f"{'='*80}")
    print(f"Period: {results[0]['date']} to {results[-1]['date']} ({len(results)} trading days)")
    print(f"Premium: ${ENTRY_PREMIUM}/leg | SL: {SL_PCT*100:.0f}% per leg | OTM: {OTM_PCT*100:.1f}%")
    print(f"{'='*80}\n")

    # Daily log
    print(f"{'Date':<12} {'Entry':>8} {'Call SL':>8} {'Put SL':>8} {'Outcome':<12} {'Day P&L':>9} {'Cum P&L':>9}")
    print("-" * 72)
    for r in results:
        print(f"{r['date']:<12} ${r['entry']:>7.0f} {'🛑 HIT' if r['call_sl'] else '✓ SAFE':>8} {'🛑 HIT' if r['put_sl'] else '✓ SAFE':>8} {r['outcome']:<12} ${r['day_pnl']:>+7.2f} ${r['cum_pnl']:>+7.2f}")

    # Summary stats
    both_kept = sum(1 for r in results if r['outcome'] == 'both_kept')
    one_sl = sum(1 for r in results if r['outcome'] == 'one_sl')
    both_sl = sum(1 for r in results if r['outcome'] == 'both_sl')
    wins = sum(1 for r in results if r['day_pnl'] > 0)
    losses = sum(1 for r in results if r['day_pnl'] < 0)
    flat = sum(1 for r in results if r['day_pnl'] == 0)

    winning_days = [r['day_pnl'] for r in results if r['day_pnl'] > 0]
    losing_days = [r['day_pnl'] for r in results if r['day_pnl'] < 0]
    avg_win = sum(winning_days) / len(winning_days) if winning_days else 0
    avg_loss = sum(losing_days) / len(losing_days) if losing_days else 0

    # Streaks
    max_win_streak = max_lose_streak = cur_win = cur_lose = 0
    for r in results:
        if r['day_pnl'] > 0:
            cur_win += 1; cur_lose = 0
        elif r['day_pnl'] < 0:
            cur_lose += 1; cur_win = 0
        else:
            cur_win = cur_lose = 0
        max_win_streak = max(max_win_streak, cur_win)
        max_lose_streak = max(max_lose_streak, cur_lose)

    # Max drawdown
    peak = 0
    max_dd = 0
    cum = 0
    for r in results:
        cum += r['day_pnl']
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)

    # Sharpe (daily)
    daily_returns = [r['day_pnl'] for r in results]
    mean_ret = sum(daily_returns) / len(daily_returns)
    var = sum((x - mean_ret) ** 2 for x in daily_returns) / len(daily_returns)
    std = var ** 0.5
    sharpe = (mean_ret / std * (365 ** 0.5)) if std > 0 else 0

    profit_factor = abs(sum(winning_days) / sum(losing_days)) if losing_days else float('inf')

    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    print(f"Total Days:         {len(results)}")
    print(f"Total P&L:          ${total_pnl:+.2f}")
    print(f"Win Rate:           {wins}/{len(results)} ({wins/len(results)*100:.1f}%)")
    print(f"Profit Factor:      {profit_factor:.2f}")
    print(f"Sharpe Ratio:       {sharpe:.2f}")
    print(f"Avg Daily P&L:      ${mean_ret:.2f}")
    print(f"Avg Win:            ${avg_win:.2f}")
    print(f"Avg Loss:           ${avg_loss:.2f}")
    print(f"Best Day:           ${max(daily_returns):.2f}")
    print(f"Worst Day:          ${min(daily_returns):.2f}")
    print(f"Max Drawdown:       ${max_dd:.2f}")
    print(f"Win Streak:         {max_win_streak} days")
    print(f"Lose Streak:        {max_lose_streak} days")
    print(f"\n{'='*80}")
    print("SESSION OUTCOMES")
    print(f"{'='*80}")
    print(f"Both Legs Kept (full profit):   {both_kept:>3} days ({both_kept/len(results)*100:.1f}%)")
    print(f"One Leg Stopped (partial):      {one_sl:>3} days ({one_sl/len(results)*100:.1f}%)")
    print(f"Both Legs Stopped (max loss):   {both_sl:>3} days ({both_sl/len(results)*100:.1f}%)")

    # Monthly breakdown
    print(f"\n{'='*80}")
    print("MONTHLY BREAKDOWN")
    print(f"{'='*80}")
    months = {}
    for r in results:
        m = r['date'][:7]
        if m not in months:
            months[m] = {'pnl': 0, 'days': 0, 'wins': 0}
        months[m]['pnl'] += r['day_pnl']
        months[m]['days'] += 1
        if r['day_pnl'] > 0:
            months[m]['wins'] += 1

    print(f"{'Month':<10} {'Days':>5} {'Wins':>5} {'WR%':>6} {'P&L':>10}")
    print("-" * 40)
    for m in sorted(months.keys()):
        d = months[m]
        wr = d['wins'] / d['days'] * 100 if d['days'] else 0
        print(f"{m:<10} {d['days']:>5} {d['wins']:>5} {wr:>5.1f}% ${d['pnl']:>+8.2f}")


def _theta_decay(session, strike, entry_price, opt_type):
    """Estimate theta decay profit as fraction of premium when SL not hit.

    If price stayed far from strike, most premium decays.
    If price moved toward strike but didn't hit SL, partial decay.
    """
    # Get the closing price of the session
    close = session[-1]['c']

    if opt_type == 'call':
        dist_to_strike = strike - entry_price
        intrusion = max(0, close - entry_price) / dist_to_strike if dist_to_strike > 0 else 0
    else:
        dist_to_strike = entry_price - strike
        intrusion = max(0, entry_price - close) / dist_to_strike if dist_to_strike > 0 else 0

    # 0DTE: if price stayed away, ~80-95% of premium decays
    # If price moved toward strike (but SL not hit), less decay
    decay = max(0.3, 0.90 - intrusion * 0.6)
    return decay


if __name__ == '__main__':
    run_backtest()
