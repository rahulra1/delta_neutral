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
        self._active_threads = []

    def initialize(self):
        """Start the daily loop."""
        self._running = True
        print(f"[OI] Daily OI Strategy started | Entry: {self.entry_hour}:{self.entry_minute:02d} IST | Asset: {self.asset}")
        print(f"[OI] Target: {self.target_pct*100:.0f}% | SL: {self.stop_loss_pct*100:.0f}% of premium")
        return True

    def monitor(self):
        """Main daily loop — spawns a new monitored trade each day at entry time."""
        import threading
        while self._running:
            self._wait_for_next_entry()
            if not self._running:
                break

            self.total_days_traded += 1
            day_num = self.total_days_traded
            tag = f"[OI Day{day_num}]"

            print(f"\n{tag} ═══ {datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST ═══")

            day_legs, day_premium = self._open_daily_trade(tag)
            if not day_legs:
                print(f"{tag} No trade today")
                continue

            t = threading.Thread(target=self._monitor_day_trade,
                                 args=(day_legs, day_premium, day_num), daemon=True)
            t.start()
            self._active_threads.append(t)

    def _open_daily_trade(self, tag):
        """Analyze OI and place today's trade. Returns (legs, premium) or ([], 0)."""
        expiries = get_expiries(self.asset, min_days=1)
        if not expiries:
            return [], 0
        self.expiry = expiries[0]

        chain, spot, _ = get_option_chain_full(self.expiry, self.asset)
        if not chain or not spot:
            return [], 0
        self.spot_price = spot
        self.max_call_oi_strike, self.max_put_oi_strike = self._find_max_oi(chain)

        if not self.max_call_oi_strike or not self.max_put_oi_strike:
            return [], 0

        print(f"{tag} Spot: {spot:.2f} | Max Call OI: {self.max_call_oi_strike} (resistance) | Max Put OI: {self.max_put_oi_strike} (support)")

        call_dist = abs(spot - float(self.max_call_oi_strike)) / spot
        put_dist = abs(spot - float(self.max_put_oi_strike)) / spot

        day_legs = []
        if put_dist <= PROXIMITY_PCT and call_dist > PROXIMITY_PCT:
            day_legs = self._sell_option_return(chain, 'put', self.max_put_oi_strike, tag)
        elif call_dist <= PROXIMITY_PCT and put_dist > PROXIMITY_PCT:
            day_legs = self._sell_option_return(chain, 'call', self.max_call_oi_strike, tag)
        else:
            l1 = self._sell_option_return(chain, 'call', self.max_call_oi_strike, tag)
            l2 = self._sell_option_return(chain, 'put', self.max_put_oi_strike, tag)
            day_legs = l1 + l2

        if not day_legs:
            return [], 0

        from config import get_contract_value
        cv = get_contract_value(self.asset)
        premium = sum(l['entry_price'] * l['size'] * cv for l in day_legs)
        print(f"{tag} Premium: ${premium:.2f} | TP: +${premium*self.target_pct:.2f} | SL: -${premium*self.stop_loss_pct:.2f}")

        # Add to shared legs list for visibility
        self.legs.extend(day_legs)
        return day_legs, premium

    def _monitor_day_trade(self, day_legs, premium, day_num):
        """Monitor a single day's trade until TP/SL. Runs in its own thread."""
        from config import set_thread_credentials, get_contract_value
        # Inherit credentials if needed (thread-local)
        target = premium * self.target_pct
        sl = premium * self.stop_loss_pct
        cv = get_contract_value(self.asset)
        cycle = 0

        while self._running:
            time.sleep(self.monitor_interval)
            cycle += 1

            # Compute PnL for this day's legs
            pnl = 0.0
            for leg in day_legs:
                data = get_current_price(leg['product_id'], self.asset)
                if data:
                    pnl += (leg['entry_price'] - data['mark_price']) * leg['size'] * cv

            if cycle % 10 == 0:
                print(f"[OI D{day_num}] PnL: ${pnl:+.2f} ({pnl/premium*100:+.1f}%)")

            if pnl >= target:
                print(f"[OI D{day_num}] 🎯 Target hit: ${pnl:.2f}")
                self._close_day_legs(day_legs)
                self._record_day(day_num, pnl, premium, 'target')
                return
            if pnl <= -sl:
                print(f"[OI D{day_num}] 🛑 SL hit: ${pnl:.2f}")
                self._close_day_legs(day_legs)
                self._record_day(day_num, pnl, premium, 'stoploss')
                return

    def _close_day_legs(self, day_legs):
        """Close legs for a specific day's trade."""
        for leg in day_legs:
            place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
            # Remove from shared legs list
            if leg in self.legs:
                self.legs.remove(leg)

    def _record_day(self, day_num, pnl, premium, exit_reason):
        self.cumulative_pnl += pnl
        self.trade_log.append({
            'date': datetime.now(IST).strftime('%Y-%m-%d'),
            'day': day_num,
            'pnl': round(pnl, 2),
            'premium': round(premium, 2),
            'exit_reason': exit_reason,
        })
        print(f"[OI D{day_num}] Closed | PnL: ${pnl:+.2f} | Cumulative: ${self.cumulative_pnl:+.2f}")

    def _sell_option_return(self, chain, opt_type, strike, tag='[OI]'):
        """Sell an option and return the leg dict (or empty list)."""
        for row in chain:
            if row['strike'] != strike:
                continue
            opt = row.get(opt_type)
            if not opt:
                return []
            result = place_order(opt['product_id'], opt['symbol'], self.lot_size, 'sell')
            if result:
                leg = {
                    'symbol': opt['symbol'],
                    'product_id': opt['product_id'],
                    'side': 'sell',
                    'strike': strike,
                    'type': opt_type,
                    'entry_price': opt['mark_price'],
                    'size': self.lot_size,
                }
                print(f"{tag} ✓ SOLD {opt_type.upper()} @ strike {strike} | Premium: {opt['mark_price']}")
                return [leg]
            return []
        return []

    def close_all(self):
        """Close all open legs across all day trades."""
        self._running = False
        for leg in list(self.legs):
            place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
        self.legs.clear()

    @property
    def pnl(self):
        from config import get_contract_value
        cv = get_contract_value(self.asset)
        open_pnl = 0.0
        for leg in self.legs:
            data = get_current_price(leg['product_id'], self.asset)
            if data:
                open_pnl += (leg['entry_price'] - data['mark_price']) * leg['size'] * cv
        return self.cumulative_pnl + open_pnl

    # --- Daily trade logic ---

    def _wait_for_next_entry(self):
        """Sleep until the next occurrence of entry time (today if not yet passed, else tomorrow)."""
        now = datetime.now(IST)
        entry_today = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)
        if now < entry_today:
            target = entry_today
        else:
            target = entry_today + timedelta(days=1)
        wait = (target - now).total_seconds()
        if wait > 60:
            print(f"[OI] Next trade at {target.strftime('%Y-%m-%d %H:%M')} IST ({wait/3600:.1f}h)")
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
        spot = self.spot_price or 0
        for row in chain:
            strike_val = float(row['strike'])
            if row.get('call') and strike_val >= spot:
                oi = float(row['call'].get('oi', 0))
                if oi > max_call_oi:
                    max_call_oi = oi
                    call_strike = row['strike']
            if row.get('put') and strike_val <= spot:
                oi = float(row['put'].get('oi', 0))
                if oi > max_put_oi:
                    max_put_oi = oi
                    put_strike = row['strike']
        return call_strike, put_strike
