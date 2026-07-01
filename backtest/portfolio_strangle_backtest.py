"""Backtest: 0DTE Portfolio Strangle — 3 time-diversified entries with recost.

Simulates the PortfolioStrangle strategy:
1. 3 entry slots per day (9:15, 10:20, 11:15 IST)
2. Sell OTM5 call + OTM5 put at each slot (30 lots each)
3. SL: premium triples (200% increase)
4. Recost: if SL'd leg's premium drops back to entry → re-enter once
5. Exit at 5:29 PM IST
6. Skip Friday & Sunday

Uses 15m BTC candles from Yahoo Finance (60 days available).
Approximates OTM5 strike as ~3.5% OTM (for BTC at 60% IV, 0DTE).
"""

import sys
import os
import math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.chart import get_candles
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# Strategy params
LOT_SIZE = 30
CONTRACT_VALUE = 0.001   # 0.001 BTC per contract
SL_MULTIPLIER = 3.0      # premium triples = SL
RECOST_ALLOWED = 1       # 1 re-entry per leg
OTM_PCT = 0.035          # OTM5 ≈ 3.5% OTM for BTC 0DTE at 60% IV

# Entry times (IST hours, minutes)
ENTRY_SLOTS = [(9, 15), (10, 20), (11, 15)]
EXIT_HOUR = 17
EXIT_MINUTE = 29

# Skip days: Friday=4, Sunday=6
SKIP_WEEKDAYS = [4, 6]

# Premium estimation for 0DTE OTM options
# At 3.5% OTM with ~8h to expiry, BTC IV ~60%: premium ≈ 0.3-0.5% of spot
PREMIUM_PCT = 0.004  # ~0.4% of spot as premium for OTM5 0DTE


def estimate_premium_change(spot_entry, spot_now, strike, opt_type, hours_remaining, hours_total=8):
    """Estimate how premium changes based on spot movement and time decay.
    
    For 0DTE options:
    - Time decay is aggressive (theta kills premium fast)
    - But adverse moves spike premium very quickly (high gamma near expiry)
    - Near/ITM 0DTE options have very high delta (0.6-0.9)
    """
    # Distance from spot to strike as percentage
    if opt_type == 'call':
        distance_pct = (strike - spot_now) / strike  # positive = OTM, negative = ITM
        intrinsic = max(0, spot_now - strike)
    else:
        distance_pct = (spot_now - strike) / strike  # positive = OTM, negative = ITM
        intrinsic = max(0, strike - spot_now)
    
    # Time decay factor
    time_ratio = math.sqrt(max(hours_remaining, 0.05) / hours_total)
    
    # Base extrinsic (time value) at entry
    base_extrinsic = strike * PREMIUM_PCT
    
    # 0DTE gamma effect: as option approaches ATM/ITM, extrinsic INCREASES before decaying
    # This is what causes SL hits — premium spikes on adverse moves
    if distance_pct > 0.02:
        # Still OTM > 2%: premium decays normally
        extrinsic = base_extrinsic * time_ratio * math.exp(-3 * distance_pct)
    elif distance_pct > 0:
        # Near ATM (0-2% OTM): premium rises significantly
        atm_factor = 1 + (0.02 - distance_pct) / 0.02 * 3  # up to 4x at ATM
        extrinsic = base_extrinsic * time_ratio * atm_factor
    else:
        # ITM: high delta, premium = intrinsic + small extrinsic
        itm_depth = abs(distance_pct)
        extrinsic = base_extrinsic * time_ratio * math.exp(-2 * itm_depth) * 0.5
    
    total_premium = intrinsic + extrinsic
    return total_premium


def run_backtest():
    print("Fetching BTC 15m candles (60 days)...")
    candles = get_candles('BTC', '15m')
    if not candles:
        print("❌ Failed to fetch candles")
        return

    print(f"✓ Got {len(candles)} candles (15m)")

    # Group candles by IST date
    days = {}
    for c in candles:
        dt = datetime.fromtimestamp(c['t'], tz=IST)
        date_key = dt.strftime('%Y-%m-%d')
        if date_key not in days:
            days[date_key] = []
        days[date_key].append({**c, 'dt': dt})

    print(f"✓ {len(days)} trading days available")

    results = []
    total_pnl = 0

    for date_key in sorted(days.keys()):
        day_candles = days[date_key]
        
        # Check weekday (skip Friday=4, Sunday=6)
        weekday = day_candles[0]['dt'].weekday()
        if weekday in SKIP_WEEKDAYS:
            continue

        result = simulate_day(day_candles, date_key)
        if result is None:
            continue
        
        results.append(result)
        total_pnl += result['day_pnl']
        result['cum_pnl'] = round(total_pnl, 4)

    print_results(results, total_pnl)


def simulate_day(day_candles, date_key):
    """Simulate one day of the portfolio strangle strategy."""
    
    # Filter candles to trading session (9:00 - 17:30 IST)
    session = [c for c in day_candles
               if 9 <= c['dt'].hour < 18]
    
    if len(session) < 16:  # need at least 4 hours of data
        return None

    # Get spot at session open for strike calculation
    spot_open = session[0]['o']
    
    # Calculate OTM5 strikes
    call_strike = spot_open * (1 + OTM_PCT)
    put_strike = spot_open * (1 - OTM_PCT)
    
    # Entry premium (theoretical at entry time)
    entry_premium = spot_open * PREMIUM_PCT
    
    day_pnl = 0
    slot_results = []
    total_sl_count = 0
    total_recost_count = 0
    
    # Simulate each entry slot
    for slot_idx, (entry_h, entry_m) in enumerate(ENTRY_SLOTS):
        # Find candle at entry time
        entry_candle = None
        for c in session:
            if c['dt'].hour == entry_h and c['dt'].minute >= entry_m:
                entry_candle = c
                break
            elif c['dt'].hour > entry_h:
                entry_candle = c
                break
        
        if entry_candle is None:
            continue
        
        spot_at_entry = entry_candle['c']
        # Recalculate strikes based on spot at this entry time
        slot_call_strike = spot_at_entry * (1 + OTM_PCT)
        slot_put_strike = spot_at_entry * (1 - OTM_PCT)
        slot_entry_premium_call = spot_at_entry * PREMIUM_PCT
        slot_entry_premium_put = spot_at_entry * PREMIUM_PCT
        
        # Track each leg
        call_leg = {
            'strike': slot_call_strike,
            'entry_premium': slot_entry_premium_call,
            'sl_price': slot_entry_premium_call * SL_MULTIPLIER,
            'stopped': False,
            'recost_used': False,
            'exit_premium': None,
            'sl_hit': False,
        }
        put_leg = {
            'strike': slot_put_strike,
            'entry_premium': slot_entry_premium_put,
            'sl_price': slot_entry_premium_put * SL_MULTIPLIER,
            'stopped': False,
            'recost_used': False,
            'exit_premium': None,
            'sl_hit': False,
        }
        
        # Get candles AFTER entry time until exit
        entry_idx = session.index(entry_candle)
        remaining_candles = session[entry_idx + 1:]
        
        # Hours in session from entry to exit (5:29 PM)
        entry_dt = entry_candle['dt']
        hours_total = (EXIT_HOUR + EXIT_MINUTE/60) - (entry_dt.hour + entry_dt.minute/60)
        
        # Simulate through remaining candles
        for c in remaining_candles:
            # Past exit time?
            if c['dt'].hour > EXIT_HOUR or (c['dt'].hour == EXIT_HOUR and c['dt'].minute >= EXIT_MINUTE):
                break
            
            hours_remaining = (EXIT_HOUR + EXIT_MINUTE/60) - (c['dt'].hour + c['dt'].minute/60)
            
            # Check call leg
            if not call_leg['stopped']:
                call_prem = estimate_premium_change(
                    spot_at_entry, c['h'], call_leg['strike'], 'call',
                    hours_remaining, hours_total)
                # Use high for adverse check (worst case for shorts)
                if call_prem >= call_leg['sl_price']:
                    call_leg['stopped'] = True
                    call_leg['sl_hit'] = True
                    call_leg['exit_premium'] = call_leg['sl_price']
                    total_sl_count += 1
            elif call_leg['sl_hit'] and not call_leg['recost_used']:
                # Check recost: has premium dropped back to entry?
                call_prem_now = estimate_premium_change(
                    spot_at_entry, c['c'], call_leg['strike'], 'call',
                    hours_remaining, hours_total)
                if call_prem_now <= call_leg['entry_premium']:
                    # Recost! Re-enter
                    call_leg['stopped'] = False
                    call_leg['recost_used'] = True
                    call_leg['entry_premium'] = call_prem_now
                    call_leg['sl_price'] = call_prem_now * SL_MULTIPLIER
                    call_leg['exit_premium'] = None
                    total_recost_count += 1
            
            # Check put leg
            if not put_leg['stopped']:
                put_prem = estimate_premium_change(
                    spot_at_entry, c['l'], put_leg['strike'], 'put',
                    hours_remaining, hours_total)
                # Use low for put adverse check
                if put_prem >= put_leg['sl_price']:
                    put_leg['stopped'] = True
                    put_leg['sl_hit'] = True
                    put_leg['exit_premium'] = put_leg['sl_price']
                    total_sl_count += 1
            elif put_leg['sl_hit'] and not put_leg['recost_used']:
                put_prem_now = estimate_premium_change(
                    spot_at_entry, c['c'], put_leg['strike'], 'put',
                    hours_remaining, hours_total)
                if put_prem_now <= put_leg['entry_premium']:
                    put_leg['stopped'] = False
                    put_leg['recost_used'] = True
                    put_leg['entry_premium'] = put_prem_now
                    put_leg['sl_price'] = put_prem_now * SL_MULTIPLIER
                    put_leg['exit_premium'] = None
                    total_recost_count += 1
        
        # EOD exit for non-stopped legs
        if not call_leg['stopped']:
            # Premium at exit: use last candle close
            last_c = remaining_candles[-1] if remaining_candles else entry_candle
            hours_left = 0.1  # almost expired
            call_leg['exit_premium'] = estimate_premium_change(
                spot_at_entry, last_c['c'], call_leg['strike'], 'call',
                hours_left, hours_total)
        
        if not put_leg['stopped']:
            last_c = remaining_candles[-1] if remaining_candles else entry_candle
            hours_left = 0.1
            put_leg['exit_premium'] = estimate_premium_change(
                spot_at_entry, last_c['c'], put_leg['strike'], 'put',
                hours_left, hours_total)
        
        # Calculate slot PnL (sold at entry, bought back at exit)
        multiplier = LOT_SIZE * CONTRACT_VALUE
        call_pnl = (call_leg['entry_premium'] - (call_leg['exit_premium'] or 0)) * multiplier
        put_pnl = (put_leg['entry_premium'] - (put_leg['exit_premium'] or 0)) * multiplier
        slot_pnl = call_pnl + put_pnl
        
        day_pnl += slot_pnl
        slot_results.append({
            'slot': slot_idx + 1,
            'call_sl': call_leg['sl_hit'],
            'put_sl': put_leg['sl_hit'],
            'call_recost': call_leg['recost_used'],
            'put_recost': put_leg['recost_used'],
            'pnl': round(slot_pnl, 4),
        })
    
    if not slot_results:
        return None
    
    # Outcome
    if total_sl_count == 0:
        outcome = 'NO_SL'
    elif total_sl_count <= 2:
        outcome = f'{total_sl_count}SL'
    else:
        outcome = f'{total_sl_count}SL_HEAVY'
    
    if total_recost_count > 0:
        outcome += f'_{total_recost_count}RC'
    
    return {
        'date': date_key,
        'weekday': day_candles[0]['dt'].strftime('%a'),
        'spot': round(spot_open, 0),
        'slots': len(slot_results),
        'sl_count': total_sl_count,
        'recost_count': total_recost_count,
        'outcome': outcome,
        'day_pnl': round(day_pnl, 4),
    }


def print_results(results, total_pnl):
    if not results:
        print("No results")
        return
    
    print(f"\n{'='*95}")
    print(f"0DTE PORTFOLIO STRANGLE BACKTEST — 3 Slots × 30 Lots × OTM5")
    print(f"{'='*95}")
    print(f"Period: {results[0]['date']} to {results[-1]['date']} ({len(results)} trading days)")
    print(f"Position: {LOT_SIZE} lots × {CONTRACT_VALUE} BTC = {LOT_SIZE * CONTRACT_VALUE} BTC per leg per slot")
    print(f"Entries: {len(ENTRY_SLOTS)} slots/day | SL: {(SL_MULTIPLIER-1)*100:.0f}% | Recost: {RECOST_ALLOWED}")
    print(f"OTM: ~{OTM_PCT*100:.1f}% | Skip: Fri, Sun")
    print(f"{'='*95}\n")

    # Daily log
    print(f"{'Date':<12} {'Day':<4} {'Spot':>8} {'Slots':>5} {'SLs':>4} {'RC':>3} {'Outcome':<14} {'Day PnL':>10} {'Cum PnL':>10}")
    print("-" * 80)
    
    for r in results:
        icon = '🟢' if r['day_pnl'] > 0 else '🔴' if r['day_pnl'] < 0 else '⚪'
        print(f"{r['date']:<12} {r['weekday']:<4} ${r['spot']:>7.0f} {r['slots']:>5} {r['sl_count']:>4} {r['recost_count']:>3} "
              f"{r['outcome']:<14} {icon}${r['day_pnl']:>+8.4f} ${r['cum_pnl']:>+8.4f}")

    # Summary
    wins = [r for r in results if r['day_pnl'] > 0]
    losses = [r for r in results if r['day_pnl'] < 0]
    daily_pnls = [r['day_pnl'] for r in results]
    avg_daily = sum(daily_pnls) / len(daily_pnls)
    avg_win = sum(r['day_pnl'] for r in wins) / len(wins) if wins else 0
    avg_loss = sum(r['day_pnl'] for r in losses) / len(losses) if losses else 0
    
    # Max drawdown
    peak = max_dd = cum = 0
    for r in results:
        cum += r['day_pnl']
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    
    # Sharpe (daily → annualized)
    if len(daily_pnls) > 1:
        mean_ret = sum(daily_pnls) / len(daily_pnls)
        var = sum((x - mean_ret) ** 2 for x in daily_pnls) / (len(daily_pnls) - 1)
        std = var ** 0.5
        sharpe = (mean_ret / std * math.sqrt(252)) if std > 0 else 0
    else:
        sharpe = 0
    
    gross_profit = sum(r['day_pnl'] for r in wins)
    gross_loss = abs(sum(r['day_pnl'] for r in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Streaks
    max_win_streak = max_lose_streak = cur_win = cur_lose = 0
    for r in results:
        if r['day_pnl'] > 0:
            cur_win += 1; cur_lose = 0
        elif r['day_pnl'] < 0:
            cur_lose += 1; cur_win = 0
        max_win_streak = max(max_win_streak, cur_win)
        max_lose_streak = max(max_lose_streak, cur_lose)
    
    # SL stats
    no_sl_days = sum(1 for r in results if r['sl_count'] == 0)
    recost_days = sum(1 for r in results if r['recost_count'] > 0)
    total_sls = sum(r['sl_count'] for r in results)
    total_recosts = sum(r['recost_count'] for r in results)
    
    print(f"\n{'='*95}")
    print("PERFORMANCE SUMMARY")
    print(f"{'='*95}")
    print(f"Total P&L:           ${total_pnl:>+10.4f}")
    print(f"Avg Daily P&L:       ${avg_daily:>+10.4f}")
    print(f"Win Rate:            {len(wins)}/{len(results)} days ({len(wins)/len(results)*100:.1f}%)")
    print(f"Profit Factor:       {profit_factor:.2f}")
    print(f"Sharpe Ratio:        {sharpe:.2f} (annualized)")
    print(f"Max Drawdown:        ${max_dd:.4f}")
    print(f"Avg Win Day:         ${avg_win:>+.4f}")
    print(f"Avg Loss Day:        ${avg_loss:>+.4f}")
    print(f"Best Day:            ${max(daily_pnls):>+.4f}")
    print(f"Worst Day:           ${min(daily_pnls):>+.4f}")
    print(f"Win Streak:          {max_win_streak} days")
    print(f"Lose Streak:         {max_lose_streak} days")
    
    print(f"\n{'='*95}")
    print("SL & RECOST STATS")
    print(f"{'='*95}")
    print(f"Clean days (no SL):  {no_sl_days}/{len(results)} ({no_sl_days/len(results)*100:.0f}%)")
    print(f"Days with SL hit:    {len(results)-no_sl_days}/{len(results)} ({(len(results)-no_sl_days)/len(results)*100:.0f}%)")
    print(f"Days with recost:    {recost_days}/{len(results)} ({recost_days/len(results)*100:.0f}%)")
    print(f"Total SLs hit:       {total_sls} across all days")
    print(f"Total recosts:       {total_recosts}")
    print(f"Avg SLs per day:     {total_sls/len(results):.1f}")
    
    # Weekly breakdown
    print(f"\n{'='*95}")
    print("WEEKDAY BREAKDOWN")
    print(f"{'='*95}")
    weekday_stats = {}
    for r in results:
        wd = r['weekday']
        if wd not in weekday_stats:
            weekday_stats[wd] = {'pnl': 0, 'days': 0, 'wins': 0}
        weekday_stats[wd]['pnl'] += r['day_pnl']
        weekday_stats[wd]['days'] += 1
        if r['day_pnl'] > 0:
            weekday_stats[wd]['wins'] += 1
    
    print(f"{'Day':<5} {'Trading Days':>13} {'Wins':>6} {'WR%':>6} {'Total PnL':>12} {'Avg PnL':>10}")
    print("-" * 55)
    for wd in ['Mon', 'Tue', 'Wed', 'Thu', 'Sat']:
        if wd in weekday_stats:
            d = weekday_stats[wd]
            wr = d['wins'] / d['days'] * 100 if d['days'] else 0
            avg = d['pnl'] / d['days'] if d['days'] else 0
            icon = '🟢' if d['pnl'] > 0 else '🔴'
            print(f"{wd:<5} {d['days']:>11}   {d['wins']:>4}   {wr:>5.1f}% {icon}${d['pnl']:>+9.4f} ${avg:>+8.4f}")
    
    # Monthly summary
    print(f"\n{'='*95}")
    print("MONTHLY BREAKDOWN")
    print(f"{'='*95}")
    monthly = {}
    for r in results:
        m = r['date'][:7]
        if m not in monthly:
            monthly[m] = {'pnl': 0, 'days': 0, 'wins': 0, 'sls': 0}
        monthly[m]['pnl'] += r['day_pnl']
        monthly[m]['days'] += 1
        if r['day_pnl'] > 0:
            monthly[m]['wins'] += 1
        monthly[m]['sls'] += r['sl_count']
    
    print(f"{'Month':<9} {'Days':>5} {'Wins':>5} {'WR%':>6} {'SLs':>5} {'P&L':>12}")
    print("-" * 48)
    for m in sorted(monthly.keys()):
        d = monthly[m]
        wr = d['wins'] / d['days'] * 100
        icon = '🟢' if d['pnl'] > 0 else '🔴'
        print(f"{m:<9} {d['days']:>5} {d['wins']:>5} {wr:>5.1f}% {d['sls']:>5} {icon}${d['pnl']:>+9.4f}")


if __name__ == '__main__':
    run_backtest()
