"""Weekly Delta Neutral Strategy.

Runs the DeltaNeutralStrategy every Friday at 9:00 PM IST.
After the strategy exits (target/SL/max adjustments), it sleeps
until the next Friday 9 PM and repeats.
"""

import time
import logging
from datetime import datetime, timedelta
from strategy import DeltaNeutralStrategy
from strategy.base import BaseStrategy
from api.chain import get_expiries

logger = logging.getLogger(__name__)

# Friday = 4 in weekday() (Monday=0)
TRADE_DAY = 4  # Friday
ENTRY_HOUR = 21  # 9 PM IST
ENTRY_MINUTE = 0


class WeeklyDeltaNeutral(BaseStrategy):
    """Runs DeltaNeutralStrategy every Friday 9 PM IST, repeats weekly."""

    def __init__(self, asset='BTC', target_delta=0.20, delta_tolerance=0.05,
                 lot_size=100, premium_threshold=0.4, target_pnl=25,
                 max_adjustments=5, monitoring_interval=5,
                 trade_day=TRADE_DAY, entry_hour=ENTRY_HOUR, entry_minute=ENTRY_MINUTE):
        self.asset = asset
        self.target_delta = target_delta
        self.delta_tolerance = delta_tolerance
        self.lot_size = lot_size
        self.premium_threshold = premium_threshold
        self.target_pnl = target_pnl
        self.max_adjustments = max_adjustments
        self.monitoring_interval = monitoring_interval
        self.trade_day = trade_day
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute

        self._running = False
        self._current_strategy = None
        self.weeks_traded = 0
        self.cumulative_pnl = 0.0
        self.trade_log = []  # [{date, pnl, adjustments, exit_reason}]

    def initialize(self):
        self._running = True
        print(f"[Weekly DN] Started | Every Friday {self.entry_hour}:{self.entry_minute:02d} IST")
        print(f"[Weekly DN] Asset: {self.asset} | Delta: ±{self.target_delta} | Lots: {self.lot_size} | Target: ${self.target_pnl}")
        return True

    def monitor(self):
        """Main weekly loop."""
        while self._running:
            self._wait_for_next_friday()
            if not self._running:
                break

            print(f"\n[Weekly DN] ═══ Week {self.weeks_traded + 1} | {datetime.now().strftime('%Y-%m-%d %H:%M')} ═══")

            # Pick nearest expiry for this week's trade
            expiry = self._get_expiry()
            if not expiry:
                print("[Weekly DN] No expiry found — skipping this week")
                continue

            # Run the delta neutral strategy
            pnl, adjustments = self._run_weekly_trade(expiry)

            self.cumulative_pnl += pnl
            self.weeks_traded += 1
            self.trade_log.append({
                'date': datetime.now().strftime('%Y-%m-%d'),
                'pnl': round(pnl, 2),
                'adjustments': adjustments,
            })
            print(f"[Weekly DN] Week done | PnL: ${pnl:+.2f} | Cumulative: ${self.cumulative_pnl:+.2f} | Weeks: {self.weeks_traded}")

    def close_all(self):
        self._running = False
        if self._current_strategy:
            self._current_strategy.running = False
            self._current_strategy.close_all_positions()
            self._current_strategy = None

    @property
    def pnl(self):
        current = 0
        if self._current_strategy:
            current = self._current_strategy.total_pnl
        return self.cumulative_pnl + current

    # --- Internal ---

    def _run_weekly_trade(self, expiry):
        """Create and run a DeltaNeutralStrategy for this week. Returns (pnl, adjustments)."""
        s = DeltaNeutralStrategy(
            asset=self.asset,
            expiry_date=expiry,
            target_delta=self.target_delta,
            delta_tolerance=self.delta_tolerance,
            lot_size=self.lot_size,
            premium_threshold=self.premium_threshold,
            target_pnl=self.target_pnl,
            max_adjustments=self.max_adjustments,
            monitoring_interval=self.monitoring_interval,
        )
        self._current_strategy = s

        if not s.initialize():
            print("[Weekly DN] ✗ Strategy init failed this week")
            s.ws_manager.stop()
            self._current_strategy = None
            return 0, 0

        try:
            s.monitor_and_adjust()
        except Exception as e:
            print(f"[Weekly DN] Error: {e}")
            s.close_all_positions()

        s.ws_manager.stop()
        pnl = s.cumulative_realized_pnl
        adj = s.adjustment_count
        self._current_strategy = None
        return pnl, adj

    def _get_expiry(self):
        """Get expiry approximately 3 weeks out (2 weekly expiries ahead)."""
        expiries = get_expiries(self.asset, min_days=15)
        return expiries[0] if expiries else None

    def _wait_for_next_friday(self):
        """Sleep until the next Friday at entry time."""
        now = datetime.now()
        # Find next Friday (or today if it's Friday and before entry time)
        days_ahead = self.trade_day - now.weekday()
        if days_ahead < 0:
            days_ahead += 7
        target_date = now + timedelta(days=days_ahead)
        target_time = target_date.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)

        # If we're already past this Friday's entry time, go to next week
        if target_time <= now:
            target_time += timedelta(days=7)

        wait = (target_time - now).total_seconds()
        if wait > 60:
            print(f"[Weekly DN] Next trade: {target_time.strftime('%A %Y-%m-%d %H:%M')} ({wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))
