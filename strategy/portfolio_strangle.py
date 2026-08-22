"""0DTE BTC Portfolio Strangle — 3 time-diversified entries with recost re-entry.

Based on Delta Exchange + AlgoTest strategy:
1. Entry at 3 different times (9:15, 10:20, 11:15) with 30 lots each
2. Sell OTM5 call + OTM5 put (strangle) on 0DTE expiry
3. SL: 200% of premium (premium triples → exit)
4. Recost re-entry: if SL hits and premium drops back to entry level, re-enter once
5. Exit: 5:29 PM IST (1 min before expiry)
6. Skip: Friday & Sunday (historically loss-making days)
7. Focus: minimize drawdown over maximizing profit
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from api.chain import get_expiries, get_option_chain_full
from api.orders import place_order
from api.pricing import get_current_price
from config import get_contract_value
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Default params (from video)
LOT_SIZE = 30             # 30 lots per entry slot
SL_PCT = 3.00             # 200% SL = premium triples (entry $100 → SL at $300)
RECOST_ENTRIES = 1        # 1 re-entry per leg per slot
OTM_INDEX = 5             # OTM5 strike selection
ENTRY_TIMES = [(9, 15), (10, 20), (11, 15)]  # 3 entry times
EXIT_HOUR = 17
EXIT_MINUTE = 29
MONITOR_INTERVAL = 10
SKIP_WEEKDAYS = [4, 6]   # Friday=4, Sunday=6


class PortfolioStrangle(BaseStrategy):
    """0DTE portfolio strangle: 3 time-diversified entries with recost re-entry."""

    def __init__(self, asset='BTC', lot_size=LOT_SIZE, sl_pct=SL_PCT,
                 recost_entries=RECOST_ENTRIES, otm_index=OTM_INDEX,
                 entry_times=None, exit_hour=EXIT_HOUR, exit_minute=EXIT_MINUTE,
                 monitor_interval=MONITOR_INTERVAL, skip_weekdays=None):
        self.asset = asset
        self.lot_size = lot_size
        self.sl_pct = sl_pct
        self.recost_entries = recost_entries
        self.otm_index = otm_index
        self.entry_times = entry_times or ENTRY_TIMES
        self.exit_hour = exit_hour
        self.exit_minute = exit_minute
        self.monitor_interval = monitor_interval
        self.skip_weekdays = skip_weekdays or SKIP_WEEKDAYS

        self.legs = []
        self._legs_lock = threading.Lock()
        self._pnl = 0.0
        self.cumulative_pnl = 0.0
        self.total_days_traded = 0
        self.trade_log = []
        self._running = False
        self._session_threads = []
        self._sid = None
        self._pnl_history = []            # [(iso_ts, pnl), ...] for UI chart
        self._snap_counter = 0
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10
        self._base_params = {
            'asset': asset, 'lot_size': lot_size,
            'sl_pct': int(sl_pct * 100), 'recost_entries': recost_entries,
            'otm_index': otm_index,
            'entry_times': [f"{h}:{m:02d}" for h, m in (entry_times or ENTRY_TIMES)],
            'exit_hour': exit_hour, 'exit_minute': exit_minute,
            'monitoring_interval': monitor_interval,
            'skip_weekdays': (skip_weekdays or SKIP_WEEKDAYS),
        }

    def initialize(self):
        self._running = True
        times_str = ', '.join(f"{h}:{m:02d}" for h, m in self.entry_times)
        skip_str = ', '.join(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][d] for d in self.skip_weekdays)
        print(f"[Portfolio] 0DTE Portfolio Strangle started")
        print(f"[Portfolio] Entries: {times_str} IST × {self.lot_size} lots each")
        print(f"[Portfolio] OTM{self.otm_index} | SL: {(self.sl_pct-1)*100:.0f}% | Recost: {self.recost_entries}")
        print(f"[Portfolio] Exit: {self.exit_hour}:{self.exit_minute:02d} | Skip: {skip_str}")
        return True

    def monitor(self):
        """Main daily loop — waits for first entry time each day."""
        while self._running:
            self._wait_for_next_entry()
            if not self._running:
                break

            now = datetime.now(IST)
            # Skip configured weekdays
            if now.weekday() in self.skip_weekdays:
                print(f"[Portfolio] Skipping {now.strftime('%A')} (configured)")
                continue

            self.total_days_traded += 1
            day_num = self.total_days_traded
            tag = f"[Portfolio D{day_num}]"
            print(f"\n{tag} ═══ {now.strftime('%Y-%m-%d %H:%M')} IST ═══")

            # Start a session thread for the whole day
            t = threading.Thread(target=self._run_day_session,
                                 args=(tag, day_num), daemon=True)
            t.start()
            self._session_threads = [th for th in self._session_threads if th.is_alive()]
            self._session_threads.append(t)

    def _run_day_session(self, tag, day_num):
        """Manage all 3 entry slots for the day."""
        from config import set_thread_credentials
        if hasattr(self, '_api_key') and self._api_key:
            set_thread_credentials(self._api_key, self._api_secret, self._broker)
        if hasattr(self, '_log_queue') and self._log_queue:
            from app import LogCapture
            LogCapture._local.log_queue = self._log_queue
            LogCapture._local.log_history = self._log_history

        cv = get_contract_value(self.asset)
        day_legs_all = []  # list of (slot_legs, slot_info) tuples
        # Concurrency guards so monitoring can start after the FIRST slot fills
        # while later entry slots are still being placed.
        day_legs_lock = threading.Lock()
        entries_done = threading.Event()
        monitor_thread = [None]  # boxed so inner scope can assign

        def _start_monitor_if_needed():
            if monitor_thread[0] is None:
                mt = threading.Thread(
                    target=self._monitor_all_slots,
                    args=(tag, day_legs_all, day_num),
                    kwargs={'day_legs_lock': day_legs_lock, 'entries_done': entries_done},
                    daemon=True,
                )
                monitor_thread[0] = mt
                print(f"\n{tag} Monitoring started (first legs live) until "
                      f"{self.exit_hour}:{self.exit_minute:02d}...")
                mt.start()

        # Place trades at each entry time
        for slot_idx, (entry_h, entry_m) in enumerate(self.entry_times):
            # Wait for this entry time
            self._wait_until_time(entry_h, entry_m)
            if not self._running:
                break

            slot_tag = f"{tag} Slot{slot_idx+1}({entry_h}:{entry_m:02d})"
            print(f"\n{slot_tag} ─── Entering ───")

            slot_legs = self._open_strangle_slot(slot_tag)
            if slot_legs:
                with day_legs_lock:
                    day_legs_all.append({
                        'legs': slot_legs,
                        'slot': slot_idx + 1,
                        'entry_time': f"{entry_h}:{entry_m:02d}",
                        'recost_used': {leg['type']: False for leg in slot_legs},
                        'sl_hit': {leg['type']: False for leg in slot_legs},
                    })
                # Begin monitoring as soon as the first slot's legs are executed
                _start_monitor_if_needed()

        # Signal that no more entries will be added, so the monitor loop may
        # honour its "all stopped / all recost done" early-exit.
        entries_done.set()

        if monitor_thread[0] is not None:
            # Entries finished; wait for the monitor loop to complete the day.
            monitor_thread[0].join()
        else:
            # No slots ever filled (all entries failed) — nothing to monitor.
            print(f"\n{tag} No legs were executed; skipping monitoring.")

    def _open_strangle_slot(self, tag):
        """Sell OTM5 call + put for one time slot."""
        expiries = get_expiries(self.asset, min_days=0)
        if not expiries:
            print(f"{tag} No expiries found")
            return []
        expiry = expiries[0]

        chain, spot, _ = get_option_chain_full(expiry, self.asset)
        if not chain or not spot:
            print(f"{tag} Chain fetch failed")
            return []

        call_opt = self._find_otm_option(chain, 'call', spot)
        put_opt = self._find_otm_option(chain, 'put', spot)

        if not call_opt or not put_opt:
            print(f"{tag} Could not find OTM{self.otm_index} options")
            return []

        slot_legs = []
        for opt, opt_type in [(call_opt, 'call'), (put_opt, 'put')]:
            result = place_order(opt['product_id'], opt['symbol'], self.lot_size, 'sell')
            if result:
                leg = {
                    'symbol': opt['symbol'],
                    'product_id': opt['product_id'],
                    'side': 'sell',
                    'strike': opt['strike'],
                    'type': opt_type,
                    'entry_price': opt['mark_price'],
                    'size': self.lot_size,
                    'sl_price': opt['mark_price'] * self.sl_pct,
                    'stopped': False,
                    'recost_entry_price': opt['mark_price'],  # original for recost comparison
                }
                slot_legs.append(leg)
                print(f"{tag} ✓ SELL {opt_type.upper()} {opt['strike']} @ ${opt['mark_price']:.2f} | SL: ${leg['sl_price']:.2f}")
            else:
                print(f"{tag} ✗ Failed to sell {opt_type.upper()}")

        if slot_legs:
            with self._legs_lock:
                self.legs.extend(slot_legs)
            self._persist_state()
        return slot_legs

    def _monitor_all_slots(self, tag, day_legs_all, day_num,
                           day_legs_lock=None, entries_done=None):
        """Monitor all slots until exit time. Handle SL and recost.

        Runs concurrently with entry placement: monitoring begins as soon as the
        first slot's legs are executed. `day_legs_lock` guards `day_legs_all`
        while later slots are still being appended; `entries_done` (an Event)
        signals that no further slots will be added, so the "all stopped / all
        recost done" early-exit only applies once every entry has been placed.
        """
        cv = get_contract_value(self.asset)

        def _snapshot():
            if day_legs_lock is not None:
                with day_legs_lock:
                    return list(day_legs_all)
            return list(day_legs_all)

        while self._running:
            now = datetime.now(IST)

            # Exit time check
            if now.hour > self.exit_hour or (now.hour == self.exit_hour and now.minute >= self.exit_minute):
                print(f"{tag} ⏰ Exit time — closing all surviving legs")
                for slot in _snapshot():
                    self._close_slot_legs(slot['legs'])
                break

            # Check each slot — SL, recost, and enrich legs in one pass
            tick_pnl = 0.0
            all_legs_ok = True
            slots_now = _snapshot()
            for slot in slots_now:
                for leg in list(slot['legs']):
                    if leg.get('stopped', False):
                        # Include realized loss from stopped legs in tick_pnl
                        exit_p = leg.get('exit_price', leg['entry_price'])
                        if leg['side'] == 'sell':
                            leg_pnl = (leg['entry_price'] - exit_p) * leg['size'] * cv
                        else:
                            leg_pnl = (exit_p - leg['entry_price']) * leg['size'] * cv
                        tick_pnl += leg_pnl
                        continue

                    data = get_current_price(leg['product_id'], self.asset)
                    if not data:
                        all_legs_ok = False
                        continue
                    current = data['mark_price']

                    # Enrich leg with live data for UI
                    if leg['side'] == 'sell':
                        leg_pnl = (leg['entry_price'] - current) * leg['size'] * cv
                    else:
                        leg_pnl = (current - leg['entry_price']) * leg['size'] * cv
                    leg['current_mark'] = round(current, 4)
                    leg['current_pnl'] = round(leg_pnl, 4)
                    tick_pnl += leg_pnl

                    # Check SL
                    if current >= leg['sl_price']:
                        print(f"{tag} 🛑 Slot{slot['slot']} {leg['type'].upper()} SL: ${current:.2f} >= ${leg['sl_price']:.2f}")
                        place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
                        leg['stopped'] = True
                        leg['exit_price'] = current
                        slot['sl_hit'][leg['type']] = True
                        self._persist_state()

                        # Immediate re-entry: scan chain for OTM5 on same side
                        if not slot['recost_used'].get(leg['type'], True):
                            self._immediate_reentry(tag, slot, leg)

            # Handle consecutive failures
            if not all_legs_ok:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._max_consecutive_failures:
                    print(f"{tag} 🚨 EMERGENCY: {self._consecutive_failures} consecutive failures — closing all")
                    for slot in _snapshot():
                        self._close_slot_legs(slot['legs'])
                    break
            else:
                self._consecutive_failures = 0

            # Early-exit only once ALL entries have been placed; otherwise a
            # single stopped-out first slot would end monitoring before slots
            # 2 and 3 are even entered.
            entries_complete = entries_done is None or entries_done.is_set()
            if entries_complete and slots_now:
                # Check if everything is stopped (no point monitoring)
                all_stopped = all(
                    all(l.get('stopped', False) for l in s['legs'])
                    for s in slots_now
                )
                # But don't exit early — recost might trigger
                all_recost_done = all(
                    all(s['recost_used'].get(l['type'], True) or not s['sl_hit'].get(l['type'], False)
                        for l in s['legs'])
                    for s in slots_now
                )
                if all_stopped and all_recost_done:
                    print(f"{tag} All legs stopped, all recosts done/unavailable")
                    break

            # PnL history for UI chart
            now_iso = datetime.now(IST).isoformat()
            self._pnl_history.append((now_iso, round(self.cumulative_pnl + tick_pnl, 4)))
            if len(self._pnl_history) > 500:
                self._pnl_history = self._pnl_history[-500:]

            # Save snapshot every 6 ticks
            self._snap_counter += 1
            if self._snap_counter % 6 == 0 and self._sid:
                try:
                    from models import save_pnl_snapshot
                    user_id = getattr(self, '_user_id', None)
                    if not user_id:
                        try:
                            from app import portfolio_strangle_strategies
                            for s_id, ent in portfolio_strangle_strategies.items():
                                if ent.get('strategy') is self:
                                    user_id = ent.get('user_id')
                                    self._user_id = user_id
                                    break
                        except Exception:
                            pass
                    if user_id:
                        save_pnl_snapshot(user_id, self._sid, round(self.cumulative_pnl + tick_pnl, 4))
                except Exception:
                    pass

            time.sleep(self.monitor_interval)

        # Calculate day PnL  (entries are complete by now; take a stable snapshot)
        final_slots = _snapshot()
        day_pnl = 0.0
        for slot in final_slots:
            for leg in slot['legs']:
                exit_p = leg.get('exit_price')
                if exit_p is None:
                    data = get_current_price(leg['product_id'], self.asset)
                    exit_p = data['mark_price'] if data else leg['entry_price']
                    leg['exit_price'] = exit_p
                if leg['side'] == 'sell':
                    day_pnl += (leg['entry_price'] - exit_p) * leg['size'] * cv
                else:
                    day_pnl += (exit_p - leg['entry_price']) * leg['size'] * cv

        self.cumulative_pnl += day_pnl
        self._pnl = day_pnl

        # Remove day legs from shared legs
        with self._legs_lock:
            for slot in final_slots:
                for leg in slot['legs']:
                    if leg in self.legs:
                        self.legs.remove(leg)

        # Record trade
        sl_count = sum(1 for s in final_slots for l in s['legs'] if s['sl_hit'].get(l['type']))
        recost_count = sum(1 for s in final_slots if any(s['recost_used'].values()))
        self.trade_log.append({
            'date': datetime.now(IST).strftime('%Y-%m-%d'),
            'day': day_num,
            'pnl': round(day_pnl, 4),
            'slots': len(day_legs_all),
            'sl_count': sl_count,
            'recost_count': recost_count,
            'exit_reason': 'eod' if sl_count == 0 else f'{sl_count}sl_{recost_count}recost',
        })
        print(f"{tag} Done | PnL: ${day_pnl:+.4f} | SLs: {sl_count} | Recost: {recost_count} | Cum: ${self.cumulative_pnl:+.4f}")
        self._persist_state()

    def _immediate_reentry(self, tag, slot, leg):
        """After SL, immediately scan option chain and re-enter OTM5 on the same side."""
        opt_type = leg['type']
        slot_tag = f"{tag} Slot{slot['slot']}"
        print(f"{slot_tag} 🔄 {opt_type.upper()} RE-ENTRY: scanning chain for OTM{self.otm_index} {opt_type}...")

        expiries = get_expiries(self.asset, min_days=0)
        if not expiries:
            print(f"{slot_tag} ✗ Re-entry failed: no expiries available")
            slot['recost_used'][opt_type] = True
            return
        expiry = expiries[0]

        chain, spot, _ = get_option_chain_full(expiry, self.asset)
        if not chain or not spot:
            print(f"{slot_tag} ✗ Re-entry failed: chain fetch failed")
            slot['recost_used'][opt_type] = True
            return

        new_opt = self._find_otm_option(chain, opt_type, spot)
        if not new_opt:
            print(f"{slot_tag} ✗ Re-entry failed: no OTM{self.otm_index} {opt_type.upper()} found")
            slot['recost_used'][opt_type] = True
            return

        result = place_order(new_opt['product_id'], new_opt['symbol'], self.lot_size, 'sell')
        if result:
            new_leg = {
                'symbol': new_opt['symbol'],
                'product_id': new_opt['product_id'],
                'side': 'sell',
                'strike': new_opt['strike'],
                'type': opt_type,
                'entry_price': new_opt['mark_price'],
                'size': self.lot_size,
                'sl_price': new_opt['mark_price'] * self.sl_pct,
                'stopped': False,
                'is_reentry': True,
            }
            slot['legs'].append(new_leg)
            with self._legs_lock:
                self.legs.append(new_leg)
            slot['recost_used'][opt_type] = True
            print(f"{slot_tag} ✓ Re-entered {opt_type.upper()} {new_opt['strike']} @ ${new_opt['mark_price']:.2f} | SL: ${new_leg['sl_price']:.2f}")
            self._persist_state()
        else:
            print(f"{slot_tag} ✗ Re-entry order failed for {opt_type.upper()}")
            slot['recost_used'][opt_type] = True

    def _close_slot_legs(self, slot_legs):
        """Close all non-stopped legs in a slot."""
        for leg in slot_legs:
            if leg.get('stopped', False):
                continue
            data = get_current_price(leg['product_id'], self.asset)
            leg['exit_price'] = data['mark_price'] if data else leg['entry_price']
            try:
                place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
            except Exception as e:
                logger.warning(f"[Portfolio] Close failed: {e}")
            leg['stopped'] = True

    def _find_otm_option(self, chain, opt_type, spot):
        """Find OTM{n} option from chain."""
        otm_options = []
        for row in chain:
            opt = row.get(opt_type)
            if not opt or opt['mark_price'] <= 0:
                continue
            strike = float(row['strike'])
            if opt_type == 'call' and strike > spot:
                otm_options.append((strike, opt))
            elif opt_type == 'put' and strike < spot:
                otm_options.append((strike, opt))

        if opt_type == 'call':
            otm_options.sort(key=lambda x: x[0])  # lowest OTM first
        else:
            otm_options.sort(key=lambda x: -x[0])  # highest OTM first (closest to ATM)

        if len(otm_options) >= self.otm_index:
            return otm_options[self.otm_index - 1][1]
        elif otm_options:
            return otm_options[-1][1]
        return None

    def close_all(self):
        self._running = False
        with self._legs_lock:
            for leg in list(self.legs):
                try:
                    place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
                except Exception as e:
                    logger.warning(f"[Portfolio] Failed to close {leg.get('symbol')}: {e}")
            self.legs.clear()
        for t in self._session_threads:
            t.join(timeout=2)
        self._session_threads.clear()
        try:
            self._persist_state()
        except Exception:
            pass

    @property
    def pnl(self):
        cv = get_contract_value(self.asset)
        open_pnl = 0.0
        with self._legs_lock:
            for leg in list(self.legs):
                if leg.get('stopped'):
                    # Include realized loss from stopped legs
                    exit_p = leg.get('exit_price', leg['entry_price'])
                    if leg['side'] == 'sell':
                        open_pnl += (leg['entry_price'] - exit_p) * leg['size'] * cv
                    else:
                        open_pnl += (exit_p - leg['entry_price']) * leg['size'] * cv
                    continue
                data = get_current_price(leg['product_id'], self.asset)
                if data:
                    if leg['side'] == 'sell':
                        open_pnl += (leg['entry_price'] - data['mark_price']) * leg['size'] * cv
                    else:
                        open_pnl += (data['mark_price'] - leg['entry_price']) * leg['size'] * cv
        return self.cumulative_pnl + open_pnl

    def _persist_state(self):
        """Save state to DB so it survives server restarts."""
        try:
            from models import update_strategy_db
            sid = getattr(self, '_sid', None)
            if not sid:
                try:
                    from app import portfolio_strangle_strategies
                    for s_id, entry in portfolio_strangle_strategies.items():
                        if entry.get('strategy') is self:
                            sid = s_id
                            self._sid = sid
                            break
                except Exception:
                    pass
            if not sid:
                return
            details = {
                **self._base_params,
                'cumulative_pnl': self.cumulative_pnl,
                'total_days_traded': self.total_days_traded,
                'trade_log': self.trade_log[-50:],
            }
            legs_data = []
            for leg in self.legs:
                legs_data.append({k: v for k, v in leg.items() if k != '_lock'})
            update_strategy_db(sid, details=details, legs=legs_data,
                               pnl=round(self.cumulative_pnl, 4))
        except Exception as e:
            logger.warning(f"[Portfolio] Persist state failed: {e}")

    def _wait_for_next_entry(self):
        """Wait until the first entry time of the next valid day."""
        now = datetime.now(IST)
        first_h, first_m = self.entry_times[0]
        entry_today = now.replace(hour=first_h, minute=first_m, second=0, microsecond=0)

        if now < entry_today:
            target = entry_today
        else:
            target = entry_today + timedelta(days=1)

        # Skip configured weekdays
        while target.weekday() in self.skip_weekdays:
            target += timedelta(days=1)

        wait = (target - now).total_seconds()
        if wait > 60:
            print(f"[Portfolio] Next entry: {target.strftime('%Y-%m-%d %H:%M')} IST ({wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _wait_until_time(self, hour, minute):
        """Wait until specific time today."""
        now = datetime.now(IST)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            return  # already past this time
        wait = (target - now).total_seconds()
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))
