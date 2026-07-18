"""Weekly Delta Neutral Strategy.

Runs DeltaNeutralStrategy in a loop. Each cycle:
- Computes the auto-expiry (3rd week Friday from today)
- Sets TP/SL at 70% of total premium collected
- Max 5 adjustments per cycle
- Can start on any day (default: Friday)
- Spawns a thread per cycle so the loop continues scheduling

All config lives here and gets passed down to DeltaNeutralStrategy.
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone

from strategy import DeltaNeutralStrategy
from strategy.base import BaseStrategy
from api.chain import get_expiries

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Day mapping: lowercase name -> weekday int (Monday=0)
DAY_MAP = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2,
    'thursday': 3, 'friday': 4, 'saturday': 5, 'sunday': 6,
}


def get_nth_week_friday(n=3):
    """Get the Friday of the Nth week from today.

    Week 1 = this week's Friday (or next Friday if today is past Friday).
    Returns expiry in DD-MM-YYYY format.
    """
    today = datetime.now(IST).date()
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0:
        # Today is Friday — count as week 1
        first_friday = today
    else:
        first_friday = today + timedelta(days=days_until_friday)

    target_friday = first_friday + timedelta(weeks=n - 1)
    return target_friday.strftime('%d-%m-%Y')


class WeeklyDeltaNeutral(BaseStrategy):
    """Runs DeltaNeutralStrategy weekly in a loop with auto-configured params.

    Config:
        asset: underlying asset (default BTC)
        target_delta: delta for option selection (default 0.20)
        delta_tolerance: acceptable deviation (default 0.05)
        lot_size: contracts per leg (default 100)
        premium_threshold: % premium rise to trigger adjustment (default 0.4)
        tp_sl_percent: TP and SL as % of total premium collected (default 0.70 = 70%)
        max_adjustments: max adjustments per cycle (default 5)
        monitoring_interval: seconds between status checks (default 5)
        expiry_week: which week's Friday to use as expiry (default 3)
        start_day: day of week to start each cycle (default 'friday')
        entry_hour: hour (IST 24h) to start (default 21 = 9 PM)
        entry_minute: minute to start (default 0)
    """

    def __init__(self, asset='BTC', target_delta=0.20, delta_tolerance=0.05,
                 lot_size=100, premium_threshold=0.4, tp_sl_percent=0.70,
                 tp_percent=None, sl_percent=None,
                 max_adjustments=5, monitoring_interval=5,
                 expiry_week=3, start_day='friday',
                 entry_hour=21, entry_minute=0):
        self.asset = asset
        self.target_delta = target_delta
        self.delta_tolerance = delta_tolerance
        self.lot_size = lot_size
        self.premium_threshold = premium_threshold
        # Support separate TP/SL; fall back to tp_sl_percent for backward compat
        self.tp_percent = (tp_percent / 100 if tp_percent and tp_percent > 1 else tp_percent) or tp_sl_percent
        self.sl_percent = (sl_percent / 100 if sl_percent and sl_percent > 1 else sl_percent) or tp_sl_percent
        self.tp_sl_percent = tp_sl_percent  # kept for backward compat
        self.max_adjustments = max_adjustments
        self.monitoring_interval = monitoring_interval
        self.expiry_week = expiry_week
        self.start_day = start_day.lower()
        self.trade_day = DAY_MAP.get(self.start_day, 4)  # default Friday
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute

        self._running = False
        self._active_strategies = []
        self._active_threads = []
        self.weeks_traded = 0
        self.cumulative_pnl = 0.0
        self.trade_log = []
        self.sessions = []  # [{session_id, week, expiry, status, pnl, started_at, ended_at, adjustments}]

    def initialize(self):
        self._running = True
        logger.info("=" * 70)
        logger.info("WEEKLY DELTA NEUTRAL STRATEGY")
        logger.info("=" * 70)
        logger.info(f"Asset: {self.asset} | Delta: ±{self.target_delta} | Lots: {self.lot_size}")
        logger.info(f"TP/SL: {int(self.tp_percent * 100)}% / {int(self.sl_percent * 100)}% of premium | Max Adj: {self.max_adjustments}")
        logger.info(f"Expiry: auto (week {self.expiry_week} Friday) | Start: {self.start_day.title()} {self.entry_hour}:{self.entry_minute:02d} IST")
        logger.info("=" * 70)
        return True

    def monitor(self):
        """Main weekly loop — waits for start day, spawns strategy thread, repeats."""
        while self._running:
            self._wait_for_start_day()
            if not self._running:
                break

            # Clean up finished threads and strategies
            self._active_threads[:] = [th for th in self._active_threads if th.is_alive()]
            self._active_strategies[:] = [s for s in self._active_strategies if s.running]

            self.weeks_traded += 1
            week_num = self.weeks_traded
            tag = f"[Weekly DN #{week_num}]"

            logger.info(f"\n{tag} ═══ {datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST ═══")

            # Compute expiry for this cycle
            expiry = self._get_expiry()
            if not expiry:
                logger.warning(f"{tag} No expiry found — skipping this cycle")
                # Sleep a day and retry
                self._interruptible_sleep(24 * 3600)
                continue

            logger.info(f"{tag} Expiry: {expiry} | TP/SL: {int(self.tp_sl_percent * 100)}% of premium")

            # Spawn strategy in a thread
            t = threading.Thread(
                target=self._run_cycle,
                args=(expiry, week_num),
                name=f"weekly-dn-{week_num}",
                daemon=True,
            )
            t.start()
            self._active_threads.append(t)

            # Clean up finished threads
            self._active_threads[:] = [th for th in self._active_threads if th.is_alive()]

            # Log active cycles
            active_count = len(self._active_strategies)
            logger.info(f"{tag} Launched. Active cycles: {active_count}. Next cycle in ~7 days.")
            self._interruptible_sleep(7 * 24 * 3600)

    def close_all(self):
        self._running = False
        for s in self._active_strategies:
            s.running = False
            s.close_all_positions()
            s.ws_manager.stop()
        self._active_strategies.clear()

    def stop(self):
        self.close_all()

    @property
    def pnl(self):
        """Total P&L: cumulative from completed cycles + unrealized from all active cycles."""
        active_pnl = sum(s.total_pnl for s in self._active_strategies if s.running)
        return self.cumulative_pnl + active_pnl

    # --- Internal ---

    def _persist_state(self):
        """Save state to DB so it survives server restarts."""
        try:
            from models import update_strategy_db
            sid = getattr(self, '_sid', None)
            if not sid:
                try:
                    from app import weekly_dn_strategies
                    for s_id, entry in weekly_dn_strategies.items():
                        if entry.get('strategy') is self:
                            sid = s_id
                            self._sid = sid
                            break
                except Exception:
                    pass
            if not sid:
                return

            # Capture active session positions for mid-cycle restore
            active_session_state = []
            for strat in self._active_strategies:
                if not strat.running:
                    continue
                sess_state = {
                    'session_id': getattr(strat, '_session_id', None),
                    'expiry_date': strat.expiry_date,
                    'call_position': strat.call_position,
                    'put_position': strat.put_position,
                    'call_entry_price': strat.call_entry_price,
                    'put_entry_price': strat.put_entry_price,
                    'call_actual_entry_price': strat.call_actual_entry_price,
                    'put_actual_entry_price': strat.put_actual_entry_price,
                    'call_contract_value': strat.call_contract_value,
                    'put_contract_value': strat.put_contract_value,
                    'total_premium_collected': strat.total_premium_collected,
                    'target_pnl': strat.target_pnl,
                    'stop_loss': strat.stop_loss,
                    'adjustment_count': strat.adjustment_count,
                    'cumulative_realized_pnl': strat.cumulative_realized_pnl,
                    'realized_pnl_snapshot': strat.realized_pnl_snapshot,
                    'adjustment_history': strat.adjustment_history[-10:],
                }
                active_session_state.append(sess_state)

            details = {
                'asset': self.asset,
                'target_delta': self.target_delta,
                'delta_tolerance': self.delta_tolerance,
                'lot_size': self.lot_size,
                'premium_threshold': int(self.premium_threshold * 100),
                'tp_sl_percent': int(self.tp_sl_percent * 100),
                'tp_percent': int(self.tp_percent * 100),
                'sl_percent': int(self.sl_percent * 100),
                'max_adjustments': self.max_adjustments,
                'monitoring_interval': self.monitoring_interval,
                'expiry_week': self.expiry_week,
                'start_day': self.start_day,
                'entry_hour': self.entry_hour,
                'entry_minute': self.entry_minute,
                'cumulative_pnl': self.cumulative_pnl,
                'weeks_traded': self.weeks_traded,
                'trade_log': self.trade_log[-50:],
                'sessions': self.sessions[-20:],
                'active_session_state': active_session_state,
            }
            update_strategy_db(sid, details=details,
                               pnl=round(self.cumulative_pnl, 2))
        except Exception as e:
            logger.warning(f"[Weekly DN] Persist state failed: {e}")

    def _restore_active_sessions(self, active_session_state):
        """Restore mid-cycle sessions after server restart.

        Re-creates DeltaNeutralStrategy instances from persisted state,
        reconnects to exchange positions, and resumes monitoring.
        """
        if not active_session_state:
            return

        for sess_state in active_session_state:
            session_id = sess_state.get('session_id')
            expiry = sess_state.get('expiry_date')
            if not expiry:
                continue

            tag = f"[Weekly DN {session_id}]"
            logger.info(f"{tag} Restoring mid-cycle session (expiry: {expiry})")

            # Rebuild session entry
            session_entry = {
                'session_id': session_id,
                'week': int(session_id.replace('S', '')) if session_id else 0,
                'expiry': expiry,
                'status': 'running',
                'pnl': 0.0,
                'started_at': datetime.now(IST).isoformat(),
                'ended_at': None,
                'adjustments': sess_state.get('adjustment_count', 0),
                'call_strike': None,
                'put_strike': None,
                'call_delta': None,
                'put_delta': None,
            }

            # Create DeltaNeutralStrategy and restore its state
            s = DeltaNeutralStrategy(
                asset=self.asset,
                expiry_date=expiry,
                target_delta=self.target_delta,
                delta_tolerance=self.delta_tolerance,
                lot_size=self.lot_size,
                premium_threshold=self.premium_threshold,
                target_pnl=sess_state.get('target_pnl', 0),
                max_adjustments=self.max_adjustments,
                monitoring_interval=self.monitoring_interval,
                tp_percent=self.tp_percent,
                sl_percent=self.sl_percent,
            )
            s._session_id = session_id
            s._on_state_change = self._persist_state  # persist to DB after each adjustment

            # Restore position state — includes updated baselines after adjustments
            # After adjustment: entry_price = current tracking baseline (not original entry)
            # call_position/put_position = the CURRENT leg (may be different from initial)
            s.call_position = sess_state.get('call_position')
            s.put_position = sess_state.get('put_position')
            s.call_entry_price = sess_state.get('call_entry_price', 0)
            s.put_entry_price = sess_state.get('put_entry_price', 0)
            s.call_actual_entry_price = sess_state.get('call_actual_entry_price', 0)
            s.put_actual_entry_price = sess_state.get('put_actual_entry_price', 0)
            s.call_contract_value = sess_state.get('call_contract_value', 0.001)
            s.put_contract_value = sess_state.get('put_contract_value', 0.001)
            s.total_premium_collected = sess_state.get('total_premium_collected', 0)
            s.target_pnl = sess_state.get('target_pnl', 0)
            s.stop_loss = sess_state.get('stop_loss', 0)
            s.adjustment_count = sess_state.get('adjustment_count', 0)
            s.cumulative_realized_pnl = sess_state.get('cumulative_realized_pnl', 0)
            s.realized_pnl_snapshot = sess_state.get('realized_pnl_snapshot', 0)
            s.adjustment_history = sess_state.get('adjustment_history', [])

            if s.call_position:
                session_entry['call_strike'] = s.call_position.get('strike_price')
            if s.put_position:
                session_entry['put_strike'] = s.put_position.get('strike_price')

            # Verify positions still exist on exchange
            # After adjustment, positions may have changed — what we persisted is the CURRENT state
            if not s.call_position and not s.put_position:
                logger.warning(f"{tag} No position data at all — cannot restore")
                session_entry['status'] = 'lost_on_restart'
                self.sessions.append(session_entry)
                continue

            if not s.call_position or not s.put_position:
                # Partial position — one leg was closed mid-adjustment
                # Still try to restore and let monitor_and_adjust handle it
                missing = 'call' if not s.call_position else 'put'
                logger.warning(f"{tag} Partial restore — {missing} position missing (may have been closed mid-adjustment)")

            # Start WebSocket and resume monitoring
            self._active_strategies.append(s)
            self.sessions.append(session_entry)

            def _resume_session(strategy=s, sess=session_entry, tag=tag):
                try:
                    strategy.running = True
                    strategy.ws_manager.start()
                    time.sleep(2)
                    symbols = []
                    if strategy.call_position:
                        symbols.append(strategy.call_position['symbol'])
                    if strategy.put_position:
                        symbols.append(strategy.put_position['symbol'])
                    if symbols:
                        strategy.ws_manager.subscribe(symbols)
                    logger.info(f"{tag} Restored — resuming monitor_and_adjust()")
                    strategy.monitor_and_adjust()
                except Exception as e:
                    logger.error(f"{tag} Restore error: {e}")
                    strategy.close_all_positions()
                finally:
                    strategy.ws_manager.stop()
                    pnl = strategy.cumulative_realized_pnl
                    adj = strategy.adjustment_count
                    if strategy in self._active_strategies:
                        self._active_strategies.remove(strategy)
                    self.cumulative_pnl += pnl
                    sess['status'] = 'completed'
                    sess['pnl'] = round(pnl, 2)
                    sess['adjustments'] = adj
                    sess['ended_at'] = datetime.now(IST).isoformat()
                    self.trade_log.append({
                        'date': datetime.now(IST).strftime('%Y-%m-%d'),
                        'week': sess.get('week', 0),
                        'session_id': session_id,
                        'expiry': expiry,
                        'pnl': round(pnl, 2),
                        'adjustments': adj,
                        'tp_sl_percent': self.tp_sl_percent,
                    })
                    logger.info(f"{tag} Restored session done | PnL: ${pnl:+.2f}")
                    self._persist_state()

            t = threading.Thread(target=_resume_session, daemon=True,
                                 name=f"weekly-dn-restore-{session_id}")
            t.start()
            self._active_threads.append(t)
            logger.info(f"{tag} ✓ Session restored and monitoring resumed")

    def _run_cycle(self, expiry, week_num):
        """Run a single cycle: create DeltaNeutralStrategy with all params, run it."""
        # Set up log routing for this thread (inherits from parent's log_queue)
        try:
            from app import LogCapture
            if hasattr(self, '_log_queue') and self._log_queue:
                LogCapture._local.log_queue = self._log_queue
                LogCapture._local.log_history = self._log_history
        except Exception:
            pass

        # Set up API credentials for this thread
        try:
            from config import set_thread_credentials, get_api_key, get_api_secret
            import config as _cfg
            api_key = getattr(_cfg._thread_local, 'api_key', None) or _cfg.get_api_key()
            api_secret = getattr(_cfg._thread_local, 'api_secret', None) or _cfg.get_api_secret()
            broker = getattr(_cfg._thread_local, 'broker', 'demo')
            if api_key and api_secret:
                set_thread_credentials(api_key, api_secret, broker)
        except Exception:
            pass

        tag = f"[Weekly DN #{week_num}]"
        session_id = f"S{week_num}"
        started_at = datetime.now(IST).isoformat()

        # Register session
        session_entry = {
            'session_id': session_id,
            'week': week_num,
            'expiry': expiry,
            'status': 'running',
            'pnl': 0.0,
            'started_at': started_at,
            'ended_at': None,
            'adjustments': 0,
            'call_strike': None,
            'put_strike': None,
            'call_delta': None,
            'put_delta': None,
        }
        self.sessions.append(session_entry)

        s = DeltaNeutralStrategy(
            asset=self.asset,
            expiry_date=expiry,
            target_delta=self.target_delta,
            delta_tolerance=self.delta_tolerance,
            lot_size=self.lot_size,
            premium_threshold=self.premium_threshold,
            target_pnl=0,  # will be overridden by initialize() based on tp/sl percent
            max_adjustments=self.max_adjustments,
            monitoring_interval=self.monitoring_interval,
            tp_percent=self.tp_percent,
            sl_percent=self.sl_percent,
        )
        s._session_id = session_id
        s._on_state_change = self._persist_state  # persist to DB after each adjustment
        self._active_strategies.append(s)

        if not s.initialize():
            logger.warning(f"{tag} ✗ Strategy init failed")
            s.ws_manager.stop()
            if s in self._active_strategies:
                self._active_strategies.remove(s)
            session_entry['status'] = 'failed'
            session_entry['ended_at'] = datetime.now(IST).isoformat()
            return

        # Update session with position info
        if s.call_position:
            session_entry['call_strike'] = s.call_position.get('strike_price')
            session_entry['call_delta'] = s.call_position.get('delta')
        if s.put_position:
            session_entry['put_strike'] = s.put_position.get('strike_price')
            session_entry['put_delta'] = s.put_position.get('delta')

        self._persist_state()

        try:
            s.monitor_and_adjust()
        except Exception as e:
            logger.error(f"{tag} Error: {e}")
            s.close_all_positions()

        s.ws_manager.stop()
        pnl = s.cumulative_realized_pnl
        adj = s.adjustment_count
        if s in self._active_strategies:
            self._active_strategies.remove(s)

        self.cumulative_pnl += pnl

        # Update session entry
        session_entry['status'] = 'completed'
        session_entry['pnl'] = round(pnl, 2)
        session_entry['adjustments'] = adj
        session_entry['ended_at'] = datetime.now(IST).isoformat()

        self.trade_log.append({
            'date': datetime.now(IST).strftime('%Y-%m-%d'),
            'week': week_num,
            'session_id': session_id,
            'expiry': expiry,
            'pnl': round(pnl, 2),
            'adjustments': adj,
            'tp_sl_percent': self.tp_sl_percent,
        })
        logger.info(f"{tag} Done | PnL: ${pnl:+.2f} | Adj: {adj} | Cumulative: ${self.cumulative_pnl:+.2f}")
        self._persist_state()

    def _get_expiry(self):
        """Get the auto-expiry: 3rd week Friday from today.

        First tries the computed date. Falls back to API expiry list
        if the computed date doesn't match an available expiry.
        """
        computed = get_nth_week_friday(self.expiry_week)
        logger.info(f"  Auto-expiry computed: {computed} (week {self.expiry_week} Friday)")

        # Verify it exists on the exchange; if not, pick closest available
        expiries = get_expiries(self.asset, min_days=(self.expiry_week - 1) * 7)
        if not expiries:
            logger.warning("  No expiries from API — using computed date")
            return computed

        if computed in expiries:
            return computed

        # Pick the one closest to computed date
        computed_dt = datetime.strptime(computed, '%d-%m-%Y').date()
        best = None
        best_diff = None
        for exp_str in expiries:
            exp_dt = datetime.strptime(exp_str, '%d-%m-%Y').date()
            diff = abs((exp_dt - computed_dt).days)
            if best_diff is None or diff < best_diff:
                best = exp_str
                best_diff = diff
        logger.info(f"  Computed expiry not available, using closest: {best} ({best_diff}d off)")
        return best

    def _wait_for_start_day(self):
        """Sleep until the configured start day and entry time.

        - If today IS the start day and entry time hasn't passed → wait until entry time today
        - If today IS the start day and entry time already passed (within 2h grace) → trade now
        - Otherwise → wait for next occurrence of start day at entry time
        """
        now = datetime.now(IST)
        entry_today = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)

        if now.weekday() == self.trade_day:
            if now < entry_today:
                # Today is start day, entry time hasn't come yet — wait for it
                wait = (entry_today - now).total_seconds()
                logger.info(f"[Weekly DN] Today is {self.start_day.title()} — waiting for entry time {self.entry_hour}:{self.entry_minute:02d} IST ({wait/60:.0f}m)")
                self._interruptible_sleep(wait)
                return
            elif (now - entry_today) <= timedelta(hours=2):
                # Entry time passed but within grace window — trade now
                return

        # Not start day or past grace window — wait for next occurrence
        days_ahead = (self.trade_day - now.weekday()) % 7
        if days_ahead == 0:
            # Same day but past the grace window — wait till next week
            days_ahead = 7
        target_date = now + timedelta(days=days_ahead)
        target_time = target_date.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)

        wait = (target_time - now).total_seconds()
        if wait > 60:
            day_name = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][target_date.weekday()]
            logger.info(f"[Weekly DN] Next trade: {target_time.strftime('%Y-%m-%d %H:%M')} IST ({day_name}, {wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        """Sleep in small chunks so we can respond to stop signals."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))
