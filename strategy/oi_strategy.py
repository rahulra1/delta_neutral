"""Open Interest based options strategy — daily recurring.

Runs continuously. Takes one trade per day based on max OI analysis,
monitors until target/SL is hit, then waits for the next day to trade again.

Logic:
- Max Put OI strike = support level
- Max Call OI strike = resistance level
- Spot between them → sell both (strangle)
- Spot near support → sell put only
- Spot near resistance → sell call only
- After exit, sleeps until next day's entry time.
"""

import time
import logging
from datetime import datetime, timedelta, timezone
from api.chain import get_expiries, get_option_chain_full
from api.orders import place_order
from api.pricing import get_current_price
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

PROXIMITY_PCT = 0.02
MONITORING_INTERVAL = 30
TARGET_PCT = 0.50
STOP_LOSS_PCT = 0.50
LOT_SIZE = 100
OI_SHIFT_THRESHOLD = 0.3
ENTRY_HOUR = 18    # 6:30 PM IST
ENTRY_MINUTE = 30


class OIStrategy(BaseStrategy):
    """Daily recurring OI-based option seller. Start once, trades every day."""

    def __init__(self, asset='BTC', lot_size=LOT_SIZE, target_pct=TARGET_PCT,
                 stop_loss_pct=STOP_LOSS_PCT, monitor_interval=MONITORING_INTERVAL,
                 entry_hour=ENTRY_HOUR, entry_minute=ENTRY_MINUTE):
        self.asset = asset
        self.lot_size = lot_size
        self.target_pct = target_pct
        self.stop_loss_pct = stop_loss_pct
        self.monitor_interval = monitor_interval
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute

        self.expiry = None
        self.spot_price = None
        self.max_call_oi_strike = None
        self.max_put_oi_strike = None
        self.legs = []
        self.max_premium = 0.0
        self._pnl = 0.0
        self._running = False
        self.total_days_traded = 0
        self.cumulative_pnl = 0.0
        self.trade_log = []  # [{date, pnl, legs, exit_reason}]

    def initialize(self):
        """Start the daily loop."""
        self._running = True
        print(f"[OI] Daily OI Strategy started | Entry: {self.entry_hour}:{self.entry_minute:02d} IST | Asset: {self.asset}")
        print(f"[OI] Target: {self.target_pct*100:.0f}% | SL: {self.stop_loss_pct*100:.0f}% of premium")
        return True

    def monitor(self):
        """Main daily loop — waits for entry time, trades, monitors, repeats."""
        while self._running:
            # Wait until entry hour
            self._wait_for_entry_time()
            if not self._running:
                break

            # Take today's trade
            print(f"\n[OI] ═══ Day {self.total_days_traded + 1} | {datetime.now().strftime('%Y-%m-%d %H:%M')} ═══")
            success = self._take_daily_trade()
            if not success:
                print("[OI] No trade today — retrying tomorrow")
                self._sleep_until_tomorrow()
                continue

            # Monitor until exit
            exit_reason = self._monitor_until_exit()

            # Record
            day_pnl = self._pnl
            self.cumulative_pnl += day_pnl
            self.total_days_traded += 1
            self.trade_log.append({
                'date': datetime.now().strftime('%Y-%m-%d'),
                'pnl': round(day_pnl, 2),
                'premium': round(self.max_premium, 2),
                'exit_reason': exit_reason,
            })
            print(f"[OI] Day done | PnL: ${day_pnl:+.2f} | Cumulative: ${self.cumulative_pnl:+.2f} | Days: {self.total_days_traded}")

            # Reset for next day
            self.legs = []
            self.max_premium = 0.0
            self._pnl = 0.0

            # Sleep until next day's entry
            self._sleep_until_tomorrow()

    def close_all(self):
        """Close all open legs."""
        self._running = False
        for leg in self.legs:
            result = place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
            if result:
                print(f"[OI] Closed {leg['type']} @ strike {leg['strike']}")
            else:
                logger.warning(f"Failed to close {leg['symbol']}")
        self.legs.clear()

    @property
    def pnl(self):
        return self.cumulative_pnl + self._pnl

    # --- Daily trade logic ---

    def _take_daily_trade(self):
        """Analyze OI and place today's trade. Returns True on success."""
        expiries = get_expiries(self.asset, min_days=1)
        if not expiries:
            logger.error("No expiries found")
            return False
        self.expiry = expiries[0]

        chain, spot, _ = get_option_chain_full(self.expiry, self.asset)
        if not chain or not spot:
            logger.error("Failed to fetch option chain")
            return False
        self.spot_price = spot
        self.max_call_oi_strike, self.max_put_oi_strike = self._find_max_oi(chain)

        if not self.max_call_oi_strike or not self.max_put_oi_strike:
            logger.error("Could not determine max OI strikes")
            return False

        print(f"[OI] Spot: {spot:.2f} | Max Call OI: {self.max_call_oi_strike} (resistance) | Max Put OI: {self.max_put_oi_strike} (support)")

        call_dist = abs(spot - float(self.max_call_oi_strike)) / spot
        put_dist = abs(spot - float(self.max_put_oi_strike)) / spot

        trades_placed = False
        if put_dist <= PROXIMITY_PCT and call_dist > PROXIMITY_PCT:
            trades_placed = self._sell_option(chain, 'put', self.max_put_oi_strike)
        elif call_dist <= PROXIMITY_PCT and put_dist > PROXIMITY_PCT:
            trades_placed = self._sell_option(chain, 'call', self.max_call_oi_strike)
        else:
            t1 = self._sell_option(chain, 'call', self.max_call_oi_strike)
            t2 = self._sell_option(chain, 'put', self.max_put_oi_strike)
            trades_placed = t1 or t2

        if not trades_placed:
            return False

        from config import get_contract_value
        cv = get_contract_value(self.asset)
        self.max_premium = sum(leg['entry_price'] * leg['size'] * cv for leg in self.legs)
        print(f"[OI] Premium: ${self.max_premium:.2f} | Target: +${self.max_premium*self.target_pct:.2f} | SL: -${self.max_premium*self.stop_loss_pct:.2f}")
        return True

    def _monitor_until_exit(self):
        """Monitor current trade until target/SL. Returns exit reason string."""
        target = self.max_premium * self.target_pct
        sl = self.max_premium * self.stop_loss_pct
        cycle = 0
        while self._running and self.legs:
            time.sleep(self.monitor_interval)
            self._update_pnl()
            cycle += 1

            if cycle % 10 == 0:
                print(f"[OI] PnL: ${self._pnl:+.2f} ({self._pnl/self.max_premium*100:+.1f}%) | Legs: {len(self.legs)}")

            if self._pnl >= target:
                print(f"[OI] 🎯 Target hit: ${self._pnl:.2f}")
                self._close_legs()
                return 'target'
            if self._pnl <= -sl:
                print(f"[OI] 🛑 Stop loss hit: ${self._pnl:.2f}")
                self._close_legs()
                return 'stoploss'

            # OI shift check every 5 min worth of cycles
            if cycle % max(1, 300 // self.monitor_interval) == 0:
                self._check_oi_shift()

        return 'manual_stop'

    def _close_legs(self):
        """Close legs without stopping the daily loop."""
        for leg in self.legs:
            place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
        self.legs.clear()

    def _wait_for_entry_time(self):
        """Sleep until today's entry time, or trade immediately on first run if past."""
        now = datetime.now(IST)
        entry_time = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)
        if now >= entry_time:
            return  # already past entry time, trade now
        wait = (entry_time - now).total_seconds()
        print(f"[OI] Waiting until {entry_time.strftime('%H:%M')} IST ({wait/60:.0f}min)...")
        self._interruptible_sleep(wait)

    def _sleep_until_tomorrow(self):
        """Sleep until tomorrow's entry time."""
        now = datetime.now(IST)
        tomorrow_entry = (now + timedelta(days=1)).replace(
            hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)
        wait = (tomorrow_entry - now).total_seconds()
        print(f"[OI] Next trade at {tomorrow_entry.strftime('%Y-%m-%d %H:%M')} IST ({wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        """Sleep in chunks so we can respond to stop signals."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))

    # --- Helpers ---

    def _find_max_oi(self, chain):
        max_call_oi, max_put_oi = 0, 0
        call_strike, put_strike = None, None
        for row in chain:
            if row.get('call'):
                oi = float(row['call'].get('oi', 0))
                if oi > max_call_oi:
                    max_call_oi = oi
                    call_strike = row['strike']
            if row.get('put'):
                oi = float(row['put'].get('oi', 0))
                if oi > max_put_oi:
                    max_put_oi = oi
                    put_strike = row['strike']
        return call_strike, put_strike

    def _sell_option(self, chain, opt_type, strike):
        for row in chain:
            if row['strike'] != strike:
                continue
            opt = row.get(opt_type)
            if not opt:
                return False
            result = place_order(opt['product_id'], opt['symbol'], self.lot_size, 'sell')
            if result:
                self.legs.append({
                    'symbol': opt['symbol'],
                    'product_id': opt['product_id'],
                    'side': 'sell',
                    'strike': strike,
                    'type': opt_type,
                    'entry_price': opt['mark_price'],
                    'size': self.lot_size,
                })
                print(f"[OI] ✓ SOLD {opt_type.upper()} @ strike {strike} | Premium: {opt['mark_price']}")
                return True
            return False
        return False

    def _update_pnl(self):
        from config import get_contract_value
        cv = get_contract_value(self.asset)
        total = 0.0
        for leg in self.legs:
            data = get_current_price(leg['product_id'], self.asset)
            if data:
                total += (leg['entry_price'] - data['mark_price']) * leg['size'] * cv
        self._pnl = total

    def _check_oi_shift(self):
        chain, spot, _ = get_option_chain_full(self.expiry, self.asset)
        if not chain:
            return
        new_call, new_put = self._find_max_oi(chain)
        if not new_call or not new_put:
            return
        shifted = False
        if new_call != self.max_call_oi_strike:
            if abs(float(new_call) - float(self.max_call_oi_strike)) / float(self.max_call_oi_strike) > OI_SHIFT_THRESHOLD:
                print(f"[OI] ⚠ Call OI shifted: {self.max_call_oi_strike} → {new_call}")
                self.max_call_oi_strike = new_call
                shifted = True
        if new_put != self.max_put_oi_strike:
            if abs(float(new_put) - float(self.max_put_oi_strike)) / float(self.max_put_oi_strike) > OI_SHIFT_THRESHOLD:
                print(f"[OI] ⚠ Put OI shifted: {self.max_put_oi_strike} → {new_put}")
                self.max_put_oi_strike = new_put
                shifted = True
        if shifted:
            print("[OI] OI structure shifted — closing and re-entering")
            self._close_legs()
            self._take_daily_trade()
