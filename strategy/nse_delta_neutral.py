"""NSE Delta Neutral Short Strangle — Indian market.

Sells a call and put at matching deltas (short strangle) on NSE index options.
Monitors in real-time, rebalances when premiums deviate, and respects
Indian market hours (9:15 AM – 3:30 PM IST, Mon–Fri).

Supports both paper trading (NSE/Groww LTP) and live trading (Groww orders).

Key differences from crypto DeltaNeutralStrategy:
- Market hours aware: only trades/monitors during 9:15–15:30 IST
- Uses Groww API or NSE scraper for option chain + greeks
- P&L in ₹ (not $)
- Lot-size based (NSE standard lots)
- No WebSocket — polls every N seconds (NSE data has 15s cache anyway)
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone

from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# NSE lot sizes (as of 2025–2026)
NSE_LOT_SIZES = {
    'NIFTY': 65,
    'BANKNIFTY': 30,
    'FINNIFTY': 65,
    'MIDCPNIFTY': 50,
}

# Market hours (IST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30

# Defaults
DEFAULT_TARGET_DELTA = 0.20
DEFAULT_DELTA_TOLERANCE = 0.05
DEFAULT_PREMIUM_THRESHOLD = 0.40  # 40% premium increase triggers adjustment
DEFAULT_TP_PERCENT = 0.70  # 70% of premium collected
DEFAULT_SL_PERCENT = 0.70  # 70% of premium collected
DEFAULT_MAX_ADJUSTMENTS = 5
DEFAULT_MONITOR_INTERVAL = 15  # seconds
DEFAULT_ENTRY_HOUR = 9
DEFAULT_ENTRY_MINUTE = 20
DEFAULT_EXIT_HOUR = 15
DEFAULT_EXIT_MINUTE = 15


def _get_data_source():
    """Determine which data source to use based on thread-local broker setting."""
    try:
        from config import _thread_local
        broker = getattr(_thread_local, 'broker', '')
        if broker == 'groww':
            from api.groww import get_groww_expiries, get_groww_chain
            return get_groww_expiries, get_groww_chain
    except Exception:
        pass
    from api.nse import get_nse_expiries, get_nse_chain
    return get_nse_expiries, get_nse_chain


class NseDeltaNeutral(BaseStrategy):
    """NSE delta-neutral short strangle with adjustment logic and market hours awareness."""

    def __init__(self, symbol='NIFTY', lots=1, target_delta=DEFAULT_TARGET_DELTA,
                 delta_tolerance=DEFAULT_DELTA_TOLERANCE,
                 premium_threshold=DEFAULT_PREMIUM_THRESHOLD,
                 tp_percent=DEFAULT_TP_PERCENT, sl_percent=DEFAULT_SL_PERCENT,
                 max_adjustments=DEFAULT_MAX_ADJUSTMENTS,
                 monitor_interval=DEFAULT_MONITOR_INTERVAL,
                 entry_hour=DEFAULT_ENTRY_HOUR, entry_minute=DEFAULT_ENTRY_MINUTE,
                 exit_hour=DEFAULT_EXIT_HOUR, exit_minute=DEFAULT_EXIT_MINUTE,
                 trading_days=None, lot_size=None, paper_trade=True):
        # Symbol & sizing
        self.symbol = symbol
        self.lots = lots
        self.lot_size = lot_size if lot_size else NSE_LOT_SIZES.get(symbol, 50)
        self.quantity = self.lots * self.lot_size

        # Delta selection
        self.target_delta = target_delta
        self.delta_tolerance = delta_tolerance

        # Adjustment trigger
        self.premium_threshold = premium_threshold

        # TP/SL as fraction of total premium collected
        self.tp_percent = tp_percent
        self.sl_percent = sl_percent
        self.max_adjustments = max_adjustments

        # Timing
        self.monitor_interval = monitor_interval
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute
        self.exit_hour = exit_hour
        self.exit_minute = exit_minute
        self.trading_days = trading_days if trading_days is not None else [0, 1, 2, 3, 4]

        # Mode
        self.paper_trade = paper_trade

        # State
        self.call_position = None  # {symbol, strike, mark_price, delta, trading_symbol}
        self.put_position = None
        self.call_entry_price = 0.0
        self.put_entry_price = 0.0
        self.total_premium_collected = 0.0
        self.target_pnl = 0.0
        self.stop_loss = 0.0

        self.adjustment_count = 0
        self.adjustment_history = []
        self.cumulative_realized_pnl = 0.0
        self.trade_log = []
        self.total_days_traded = 0

        self._running = False
        self._legs_lock = threading.Lock()
        self._adjusting = threading.Lock()
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10
        self._pnl_history = []
        self._snap_counter = 0

        # Current prices (updated each tick)
        self._call_current = 0.0
        self._put_current = 0.0

        # Credentials (set by initialize from thread-local)
        self._api_key = None
        self._api_secret = None
        self._broker = None

        # Expiry used for current cycle
        self._expiry = None

    # ─── Initialization ─────────────────────────────────────────────────

    def initialize(self):
        """Set up the strategy: capture credentials, find options, open positions."""
        self._running = True

        # Capture thread-local credentials for child threads
        from config import get_api_key, get_api_secret, _thread_local
        self._api_key = get_api_key()
        self._api_secret = get_api_secret()
        self._broker = getattr(_thread_local, 'broker', '')

        days_str = ','.join(['Mon', 'Tue', 'Wed', 'Thu', 'Fri'][d] for d in self.trading_days)
        print(f"[NSE DN] {self.symbol} Delta Neutral Strangle {'(Paper)' if self.paper_trade else '(Live)'}")
        print(f"[NSE DN] Delta: ±{self.target_delta} | Tolerance: {self.delta_tolerance}")
        print(f"[NSE DN] Premium threshold: {self.premium_threshold*100:.0f}% | Max adj: {self.max_adjustments}")
        print(f"[NSE DN] TP: {self.tp_percent*100:.0f}% | SL: {self.sl_percent*100:.0f}% of premium")
        print(f"[NSE DN] Entry: {self.entry_hour}:{self.entry_minute:02d} | Exit: {self.exit_hour}:{self.exit_minute:02d} IST")
        print(f"[NSE DN] Lots: {self.lots} ({self.quantity} qty) | Days: {days_str}")
        return True

    def _set_thread_credentials(self):
        """Propagate credentials to the current thread (for child threads)."""
        from config import set_thread_credentials
        if self._api_key:
            set_thread_credentials(self._api_key, self._api_secret, self._broker)

    def _find_delta_options(self, chain, spot):
        """Find call and put options closest to target delta.

        Args:
            chain: list of {strike, call, put} dicts (project-standard format)
            spot: current underlying price

        Returns:
            (call_opt, put_opt) or (None, None) if not found
        """
        calls = []
        puts = []

        for row in chain:
            strike = float(row['strike'])

            # Call: OTM only (strike > spot)
            ce = row.get('call')
            if ce and ce.get('mark_price', 0) > 0 and strike > spot:
                delta = ce.get('delta', 0)
                if delta > 0:
                    calls.append({
                        'symbol': ce.get('symbol', ''),
                        'trading_symbol': ce.get('trading_symbol', ''),
                        'strike': row['strike'],
                        'mark_price': ce['mark_price'],
                        'delta': delta,
                        'iv': ce.get('iv', 0),
                        'oi': ce.get('oi', '0'),
                    })

            # Put: OTM only (strike < spot)
            pe = row.get('put')
            if pe and pe.get('mark_price', 0) > 0 and strike < spot:
                delta = pe.get('delta', 0)
                if delta < 0:
                    puts.append({
                        'symbol': pe.get('symbol', ''),
                        'trading_symbol': pe.get('trading_symbol', ''),
                        'strike': row['strike'],
                        'mark_price': pe['mark_price'],
                        'delta': delta,
                        'iv': pe.get('iv', 0),
                        'oi': pe.get('oi', '0'),
                    })

        if not calls or not puts:
            return None, None

        # Sort by closeness to target delta
        calls.sort(key=lambda x: abs(x['delta'] - self.target_delta))
        puts.sort(key=lambda x: abs(abs(x['delta']) - self.target_delta))

        # Try strict tolerance first
        best_call = next(
            (c for c in calls if abs(c['delta'] - self.target_delta) <= self.delta_tolerance),
            None
        )
        best_put = next(
            (p for p in puts if abs(abs(p['delta']) - self.target_delta) <= self.delta_tolerance),
            None
        )

        # Fallback: pick closest OTM (delta < 0.5)
        max_delta = self.target_delta * 2.5
        if not best_call:
            otm_calls = [c for c in calls if c['delta'] <= max_delta]
            if otm_calls:
                best_call = otm_calls[0]
                logger.warning(f"[NSE DN] No call within tolerance. Using delta={best_call['delta']:.4f}")

        if not best_put:
            otm_puts = [p for p in puts if abs(p['delta']) <= max_delta]
            if otm_puts:
                best_put = otm_puts[0]
                logger.warning(f"[NSE DN] No put within tolerance. Using delta={best_put['delta']:.4f}")

        return best_call, best_put

    def _open_strangle(self, tag):
        """Find options at target delta and open the strangle. Returns True on success."""
        _get_expiries, _get_chain = _get_data_source()

        expiries = _get_expiries(self.symbol)
        if not expiries:
            print(f"{tag} No expiries found")
            return False

        # Pick nearest expiry
        self._expiry = expiries[0]
        chain, spot, _ = _get_chain(self.symbol, self._expiry)
        if not chain or not spot:
            print(f"{tag} Chain fetch failed for {self.symbol} expiry {self._expiry}")
            return False

        call_opt, put_opt = self._find_delta_options(chain, spot)
        if not call_opt or not put_opt:
            print(f"{tag} Could not find options near delta ±{self.target_delta}")
            return False

        print(f"{tag} Spot: ₹{spot:.0f} | Expiry: {self._expiry}")
        print(f"{tag} Call: {call_opt['strike']} Δ={call_opt['delta']:.4f} @ ₹{call_opt['mark_price']:.2f}")
        print(f"{tag} Put:  {put_opt['strike']} Δ={put_opt['delta']:.4f} @ ₹{put_opt['mark_price']:.2f}")

        # Place orders (or paper-record)
        if not self.paper_trade:
            success = self._place_live_orders(call_opt, put_opt, tag)
            if not success:
                return False

        # Record positions
        self.call_position = call_opt
        self.put_position = put_opt
        self.call_entry_price = call_opt['mark_price']
        self.put_entry_price = put_opt['mark_price']

        # Calculate premium and TP/SL
        call_prem = self.call_entry_price * self.quantity
        put_prem = self.put_entry_price * self.quantity
        self.total_premium_collected = call_prem + put_prem
        self.target_pnl = self.total_premium_collected * self.tp_percent
        self.stop_loss = self.total_premium_collected * self.sl_percent

        print(f"{tag} ✓ SOLD Call {call_opt['strike']} @ ₹{self.call_entry_price:.2f}")
        print(f"{tag} ✓ SOLD Put  {put_opt['strike']} @ ₹{self.put_entry_price:.2f}")
        print(f"{tag} Premium: ₹{self.total_premium_collected:.2f} | TP: ₹{self.target_pnl:.2f} | SL: ₹{self.stop_loss:.2f}")

        self._persist_state()
        return True

    def _place_live_orders(self, call_opt, put_opt, tag):
        """Place actual orders via Groww. Returns True on success."""
        try:
            from api.groww import place_order
            # Sell call
            call_resp = place_order(
                trading_symbol=call_opt['trading_symbol'],
                quantity=self.quantity,
                transaction_type='SELL',
                order_type='MARKET',
                product='MIS',  # intraday
            )
            if call_resp.get('error'):
                print(f"{tag} ✗ Call order failed: {call_resp['error']}")
                return False

            # Sell put
            put_resp = place_order(
                trading_symbol=put_opt['trading_symbol'],
                quantity=self.quantity,
                transaction_type='SELL',
                order_type='MARKET',
                product='MIS',
            )
            if put_resp.get('error'):
                print(f"{tag} ✗ Put order failed: {put_resp['error']}")
                # TODO: close the call leg that already went through
                return False

            print(f"{tag} ✓ Live orders placed")
            return True
        except Exception as e:
            print(f"{tag} ✗ Order error: {e}")
            return False

    # ─── Market Hours ────────────────────────────────────────────────────

    def _is_market_open(self):
        """Check if Indian market is currently open (9:15–15:30 IST, Mon–Fri)."""
        now = datetime.now(IST)
        if now.weekday() > 4:  # Saturday/Sunday
            return False
        market_open = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0)
        market_close = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0)
        return market_open <= now <= market_close

    def _is_past_exit_time(self):
        """Check if we've passed exit time for the day."""
        now = datetime.now(IST)
        return now.hour > self.exit_hour or (now.hour == self.exit_hour and now.minute >= self.exit_minute)

    def _is_entry_time(self):
        """Check if it's time to enter (between entry time and exit time)."""
        now = datetime.now(IST)
        entry = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0)
        exit_t = now.replace(hour=self.exit_hour, minute=self.exit_minute, second=0)
        return entry <= now <= exit_t

    def _wait_for_market_open(self):
        """Sleep until next market open. Returns False if stopped."""
        now = datetime.now(IST)
        # Find the next valid trading day + entry time
        target = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)

        if now >= target or now.weekday() not in self.trading_days:
            target += timedelta(days=1)

        # Skip non-trading days
        attempts = 0
        while target.weekday() not in self.trading_days and attempts < 7:
            target += timedelta(days=1)
            attempts += 1

        wait = (target - now).total_seconds()
        if wait > 60:
            day_name = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][target.weekday()]
            print(f"[NSE DN] Next entry: {target.strftime('%Y-%m-%d %H:%M')} IST ({day_name}, {wait/3600:.1f}h)")

        return self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        """Sleep in chunks so we can respond to stop requests. Returns True if completed."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))
        return self._running

    # ─── Main Monitor Loop ───────────────────────────────────────────────

    def monitor(self):
        """Main loop: wait for entry → open strangle → monitor → exit at EOD or TP/SL."""
        while self._running:
            # Wait until trading day + entry time
            if not self._wait_for_market_open():
                break

            if not self._running:
                break

            now = datetime.now(IST)
            if now.weekday() not in self.trading_days:
                print(f"[NSE DN] Skipping {now.strftime('%A')} — not a trading day")
                continue

            self.total_days_traded += 1
            tag = f"[NSE DN D{self.total_days_traded}]"
            print(f"\n{tag} ═══ {now.strftime('%Y-%m-%d %H:%M')} IST ═══")

            # Open the strangle
            if not self._open_strangle(tag):
                print(f"{tag} No trade today — could not open strangle")
                continue

            # Monitor until exit
            self._monitor_intraday(tag)

    def _monitor_intraday(self, tag):
        """Monitor the open strangle position until exit time, TP/SL, or max adjustments."""
        self._set_thread_credentials()

        # Set up log routing for this thread
        if hasattr(self, '_log_queue') and self._log_queue:
            from app import LogCapture
            LogCapture._local.log_queue = self._log_queue
            LogCapture._local.log_history = self._log_history

        iteration = 0
        while self._running:
            now = datetime.now(IST)

            # Market closed check — just skip monitoring tick, don't close
            if not self._is_market_open():
                time.sleep(self.monitor_interval)
                continue

            # Fetch current prices
            _get_expiries, _get_chain = _get_data_source()
            chain, spot, _ = _get_chain(self.symbol, self._expiry)

            if not chain:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._max_consecutive_failures:
                    print(f"{tag} 🚨 EMERGENCY: {self._consecutive_failures} consecutive failures — closing")
                    self._close_strangle(tag, reason='data_failure')
                    break
                time.sleep(self.monitor_interval)
                continue
            self._consecutive_failures = 0

            # Find current prices from chain
            call_price = self._get_price_from_chain(chain, self.call_position)
            put_price = self._get_price_from_chain(chain, self.put_position)

            if call_price is None or put_price is None:
                time.sleep(self.monitor_interval)
                continue

            self._call_current = call_price
            self._put_current = put_price

            # Calculate unrealized P&L (short position: profit when price drops)
            call_pnl = (self.call_entry_price - call_price) * self.quantity
            put_pnl = (self.put_entry_price - put_price) * self.quantity
            unrealized = call_pnl + put_pnl
            total_pnl = self.cumulative_realized_pnl + unrealized

            # Log status
            iteration += 1
            if iteration % 4 == 1:  # Log every ~4 ticks
                call_chg = (call_price - self.call_entry_price) / self.call_entry_price if self.call_entry_price > 0 else 0
                put_chg = (put_price - self.put_entry_price) / self.put_entry_price if self.put_entry_price > 0 else 0
                print(f"{tag} #{iteration} | Adj: {self.adjustment_count}/{self.max_adjustments}")
                print(f"{tag}   Call: ₹{call_price:.2f} ({call_chg:+.1%}) | Put: ₹{put_price:.2f} ({put_chg:+.1%})")
                print(f"{tag}   PnL: R=₹{self.cumulative_realized_pnl:.2f} | U=₹{unrealized:.2f} | T=₹{total_pnl:.2f}")

            # PnL history for charting
            self._pnl_history.append((now.isoformat(), round(total_pnl, 2)))
            if len(self._pnl_history) > 500:
                self._pnl_history = self._pnl_history[-500:]

            # Check TP/SL
            if total_pnl >= self.target_pnl:
                print(f"{tag} 🎯 TARGET PROFIT! ₹{total_pnl:.2f} >= ₹{self.target_pnl:.2f}")
                self._close_strangle(tag, reason='target_profit')
                break

            if total_pnl <= -self.stop_loss:
                print(f"{tag} 🛑 STOP LOSS! ₹{total_pnl:.2f} <= -₹{self.stop_loss:.2f}")
                self._close_strangle(tag, reason='stop_loss')
                break

            # Check max adjustments
            if self.adjustment_count >= self.max_adjustments:
                print(f"{tag} ✓ Max adjustments ({self.max_adjustments}) reached — closing")
                self._close_strangle(tag, reason='max_adjustments')
                break

            # Check premium threshold for adjustment
            if self.call_entry_price > 0:
                call_change = (call_price - self.call_entry_price) / self.call_entry_price
                if call_change >= self.premium_threshold:
                    print(f"{tag} ⚠ CALL premium up {call_change:.1%} — triggering adjustment")
                    self._adjust_position('call', call_price, put_price, chain, spot, tag)

            if self.put_entry_price > 0 and self._running:
                put_change = (put_price - self.put_entry_price) / self.put_entry_price
                if put_change >= self.premium_threshold:
                    print(f"{tag} ⚠ PUT premium up {put_change:.1%} — triggering adjustment")
                    self._adjust_position('put', call_price, put_price, chain, spot, tag)

            # Snapshot
            self._snap_counter += 1
            if self._snap_counter % 6 == 0:
                self._do_snapshot(total_pnl)

            time.sleep(self.monitor_interval)

    # ─── Adjustment Logic ────────────────────────────────────────────────

    def _adjust_position(self, triggered_leg, call_price, put_price, chain, spot, tag):
        """When a leg's premium spikes, close the OPPOSITE leg and re-enter at matching delta.

        If CALL premium spikes → close PUT, sell new PUT at call's current delta.
        If PUT premium spikes → close CALL, sell new CALL at put's current delta.
        """
        if not self._adjusting.acquire(blocking=False):
            return  # another adjustment in progress
        try:
            self._adjust_inner(triggered_leg, call_price, put_price, chain, spot, tag)
        finally:
            self._adjusting.release()

    def _adjust_inner(self, triggered_leg, call_price, put_price, chain, spot, tag):
        if self.adjustment_count >= self.max_adjustments:
            return

        if triggered_leg == 'call':
            close_leg = 'put'
            close_pos = self.put_position
            close_entry = self.put_entry_price
            close_current = put_price
            # Use call's current delta to find new put
            triggered_pos = self.call_position
        else:
            close_leg = 'call'
            close_pos = self.call_position
            close_entry = self.call_entry_price
            close_current = call_price
            triggered_pos = self.put_position

        if not close_pos:
            return

        # Calculate realized P&L from closing the opposite leg
        realized = (close_entry - close_current) * self.quantity
        print(f"{tag}   [1/3] Closing {close_leg.upper()} {close_pos['strike']}: "
              f"Entry=₹{close_entry:.2f} Exit=₹{close_current:.2f} PnL=₹{realized:+.2f}")

        # Record adjustment history
        self.adjustment_history.append({
            'leg': close_leg,
            'symbol': close_pos['symbol'],
            'strike': close_pos['strike'],
            'entry': round(close_entry, 2),
            'exit': round(close_current, 2),
            'pnl': round(realized, 2),
            'size': self.quantity,
            'timestamp': datetime.now(IST).isoformat(),
            'adjustment': self.adjustment_count + 1,
        })

        # Place close order if live
        if not self.paper_trade:
            try:
                from api.groww import place_order
                resp = place_order(
                    trading_symbol=close_pos.get('trading_symbol', ''),
                    quantity=self.quantity,
                    transaction_type='BUY',
                    order_type='MARKET',
                    product='MIS',
                )
                if resp.get('error'):
                    print(f"{tag}   ✗ Close order failed: {resp['error']} — aborting adjustment")
                    return
            except Exception as e:
                print(f"{tag}   ✗ Close order error: {e} — aborting adjustment")
                return

        self.cumulative_realized_pnl += realized

        # Find replacement at the triggered leg's current delta
        # Look up triggered leg's current delta from chain
        triggered_delta = self._get_delta_from_chain(chain, triggered_pos)
        if triggered_delta is None or abs(triggered_delta) < 0.01:
            triggered_delta = self.target_delta  # fallback

        search_delta = abs(triggered_delta)
        print(f"{tag}   [2/3] Finding NEW {close_leg.upper()} at delta {search_delta:.4f}...")

        # Find the new option
        if close_leg == 'call':
            new_opt, _ = self._find_single_leg(chain, spot, 'call', search_delta)
        else:
            _, new_opt = self._find_single_leg(chain, spot, 'put', search_delta)

        if not new_opt:
            print(f"{tag}   ✗ Could not find suitable {close_leg.upper()} — keeping one-sided")
            self.adjustment_count += 1
            if close_leg == 'call':
                self.call_position = None
                self.call_entry_price = 0
            else:
                self.put_position = None
                self.put_entry_price = 0
            self._persist_state()
            return

        # Place new order if live
        if not self.paper_trade:
            try:
                from api.groww import place_order
                resp = place_order(
                    trading_symbol=new_opt.get('trading_symbol', ''),
                    quantity=self.quantity,
                    transaction_type='SELL',
                    order_type='MARKET',
                    product='MIS',
                )
                if resp.get('error'):
                    print(f"{tag}   ✗ New order failed: {resp['error']}")
                    self.adjustment_count += 1
                    self._persist_state()
                    return
            except Exception as e:
                print(f"{tag}   ✗ New order error: {e}")
                self.adjustment_count += 1
                self._persist_state()
                return

        print(f"{tag}   [3/3] Entered NEW {close_leg.upper()}: {new_opt['strike']} @ ₹{new_opt['mark_price']:.2f}")

        # Update positions and baselines
        if close_leg == 'call':
            self.call_position = new_opt
            self.call_entry_price = new_opt['mark_price']
        else:
            self.put_position = new_opt
            self.put_entry_price = new_opt['mark_price']

        # Reset the triggered leg's baseline to current price
        if triggered_leg == 'call':
            self.call_entry_price = call_price
        else:
            self.put_entry_price = put_price

        self.adjustment_count += 1
        print(f"{tag}   ✓ Adjustment #{self.adjustment_count} done | Realized PnL: ₹{self.cumulative_realized_pnl:.2f}")
        print(f"{tag}   ✓ New baselines — Call: ₹{self.call_entry_price:.2f} | Put: ₹{self.put_entry_price:.2f}")
        self._persist_state()

    def _close_strangle(self, tag, reason='manual'):
        """Close all positions and record day result."""
        final_pnl = self.cumulative_realized_pnl

        for leg_name, pos, entry_price in [
            ('CALL', self.call_position, self.call_entry_price),
            ('PUT', self.put_position, self.put_entry_price),
        ]:
            if not pos or entry_price <= 0:
                continue

            current = self._call_current if leg_name == 'CALL' else self._put_current
            if current <= 0:
                # Try one last fetch
                _get_expiries, _get_chain = _get_data_source()
                chain, _, _ = _get_chain(self.symbol, self._expiry)
                if chain:
                    current = self._get_price_from_chain(chain, pos) or entry_price
                else:
                    current = entry_price

            leg_pnl = (entry_price - current) * self.quantity
            final_pnl += leg_pnl
            print(f"{tag} ✓ Closed {leg_name} {pos['strike']} @ ₹{current:.2f} (PnL: ₹{leg_pnl:+.2f})")

            # Place close order if live
            if not self.paper_trade:
                try:
                    from api.groww import place_order
                    place_order(
                        trading_symbol=pos.get('trading_symbol', ''),
                        quantity=self.quantity,
                        transaction_type='BUY',
                        order_type='MARKET',
                        product='MIS',
                    )
                except Exception as e:
                    print(f"{tag} ⚠ Close order error for {leg_name}: {e}")

        # Reset positions
        self.call_position = None
        self.put_position = None
        self.call_entry_price = 0
        self.put_entry_price = 0
        self.cumulative_realized_pnl = final_pnl

        # Log trade
        self.trade_log.append({
            'date': datetime.now(IST).strftime('%Y-%m-%d'),
            'day': self.total_days_traded,
            'pnl': round(final_pnl, 2),
            'adjustments': self.adjustment_count,
            'exit_reason': reason,
            'premium_collected': round(self.total_premium_collected, 2),
        })
        print(f"{tag} ═══ Day PnL: ₹{final_pnl:+.2f} | Adj: {self.adjustment_count} | Reason: {reason} ═══")

        # Reset for next day
        self.adjustment_count = 0
        self.adjustment_history = []
        self.total_premium_collected = 0
        self._persist_state()

    def close_all(self):
        """Stop the strategy and close any open positions."""
        self._running = False
        if self.call_position or self.put_position:
            self._close_strangle("[NSE DN]", reason='manual_stop')
        try:
            self._persist_state()
        except Exception:
            pass

    # ─── Helper Methods ──────────────────────────────────────────────────

    def _get_price_from_chain(self, chain, position):
        """Look up current mark price for a position from chain data."""
        if not position:
            return None
        strike = str(position['strike'])
        opt_type = 'call' if position.get('delta', 0) > 0 else 'put'
        # Determine type from symbol if delta not available
        sym = position.get('symbol', '').upper()
        if 'CE' in sym:
            opt_type = 'call'
        elif 'PE' in sym:
            opt_type = 'put'

        for row in chain:
            if str(row['strike']) == strike:
                opt = row.get(opt_type)
                if opt and opt.get('mark_price', 0) > 0:
                    return opt['mark_price']
        return None

    def _get_delta_from_chain(self, chain, position):
        """Look up current delta for a position from chain data."""
        if not position:
            return None
        strike = str(position['strike'])
        sym = position.get('symbol', '').upper()
        opt_type = 'call' if 'CE' in sym else 'put'

        for row in chain:
            if str(row['strike']) == strike:
                opt = row.get(opt_type)
                if opt:
                    return opt.get('delta', 0)
        return None

    def _find_single_leg(self, chain, spot, opt_type, target_delta):
        """Find a single call or put at a target delta.

        Returns (call_opt, None) or (None, put_opt) depending on opt_type.
        """
        candidates = []
        for row in chain:
            strike = float(row['strike'])
            opt = row.get(opt_type)
            if not opt or opt.get('mark_price', 0) <= 0:
                continue
            delta = opt.get('delta', 0)

            if opt_type == 'call' and strike <= spot:
                continue  # skip ITM calls
            if opt_type == 'put' and strike >= spot:
                continue  # skip ITM puts
            if opt_type == 'call' and delta <= 0:
                continue
            if opt_type == 'put' and delta >= 0:
                continue

            candidates.append({
                'symbol': opt.get('symbol', ''),
                'trading_symbol': opt.get('trading_symbol', ''),
                'strike': row['strike'],
                'mark_price': opt['mark_price'],
                'delta': delta,
                'iv': opt.get('iv', 0),
                'oi': opt.get('oi', '0'),
            })

        if not candidates:
            return (None, None)

        # Sort by closeness to target delta
        candidates.sort(key=lambda x: abs(abs(x['delta']) - target_delta))
        best = candidates[0]

        if opt_type == 'call':
            return (best, None)
        else:
            return (None, best)

    def _do_snapshot(self, total_pnl):
        """Persist PnL snapshot to DB."""
        try:
            from models import save_pnl_snapshot, update_strategy_db
            sid = getattr(self, '_sid', None)
            user_id = getattr(self, '_user_id', None)
            if not user_id:
                try:
                    from app import nse_dn_strategies
                    for s_id, ent in nse_dn_strategies.items():
                        if ent.get('strategy') is self:
                            user_id = ent.get('user_id')
                            self._user_id = user_id
                            sid = s_id
                            self._sid = sid
                            break
                except Exception:
                    pass
            if user_id and sid:
                save_pnl_snapshot(user_id, sid, round(total_pnl, 2))
        except Exception:
            pass

    def _persist_state(self):
        """Save state to DB for resume on restart."""
        try:
            from models import update_strategy_db
            sid = getattr(self, '_sid', None)
            if not sid:
                try:
                    from app import nse_dn_strategies
                    for s_id, entry in nse_dn_strategies.items():
                        if entry.get('strategy') is self:
                            sid = s_id
                            self._sid = sid
                            break
                except Exception:
                    pass
            if not sid:
                return

            # Build legs from current positions
            legs = []
            if self.call_position:
                legs.append({
                    'symbol': self.call_position.get('symbol', ''),
                    'trading_symbol': self.call_position.get('trading_symbol', ''),
                    'strike': self.call_position['strike'],
                    'type': 'call',
                    'side': 'sell',
                    'size': self.quantity,
                    'entry_price': self.call_entry_price,
                    'delta': self.call_position.get('delta', 0),
                    'current_mark': self._call_current,
                    'expiry': self._expiry,
                })
            if self.put_position:
                legs.append({
                    'symbol': self.put_position.get('symbol', ''),
                    'trading_symbol': self.put_position.get('trading_symbol', ''),
                    'strike': self.put_position['strike'],
                    'type': 'put',
                    'side': 'sell',
                    'size': self.quantity,
                    'entry_price': self.put_entry_price,
                    'delta': self.put_position.get('delta', 0),
                    'current_mark': self._put_current,
                    'expiry': self._expiry,
                })

            details = {
                'symbol': self.symbol,
                'lots': self.lots,
                'lot_size': self.lot_size,
                'quantity': self.quantity,
                'target_delta': self.target_delta,
                'delta_tolerance': self.delta_tolerance,
                'premium_threshold': int(self.premium_threshold * 100),
                'tp_percent': int(self.tp_percent * 100),
                'sl_percent': int(self.sl_percent * 100),
                'max_adjustments': self.max_adjustments,
                'monitoring_interval': self.monitor_interval,
                'entry_hour': self.entry_hour,
                'entry_minute': self.entry_minute,
                'exit_hour': self.exit_hour,
                'exit_minute': self.exit_minute,
                'trading_days': self.trading_days,
                'paper_trade': self.paper_trade,
                'cumulative_realized_pnl': self.cumulative_realized_pnl,
                'total_days_traded': self.total_days_traded,
                'adjustment_count': self.adjustment_count,
                'total_premium_collected': self.total_premium_collected,
                'target_pnl': self.target_pnl,
                'stop_loss': self.stop_loss,
                'expiry': self._expiry,
                'trade_log': self.trade_log[-50:],
                'adjustment_history': self.adjustment_history[-20:],
            }

            update_strategy_db(sid, details=details, legs=legs,
                               pnl=round(self.pnl, 2))
        except Exception as e:
            logger.warning(f"[NSE DN] Persist state failed: {e}")

    # ─── Properties ──────────────────────────────────────────────────────

    @property
    def pnl(self):
        """Current total P&L (realized + unrealized)."""
        unrealized = 0.0
        if self.call_position and self.call_entry_price > 0 and self._call_current > 0:
            unrealized += (self.call_entry_price - self._call_current) * self.quantity
        if self.put_position and self.put_entry_price > 0 and self._put_current > 0:
            unrealized += (self.put_entry_price - self._put_current) * self.quantity
        return self.cumulative_realized_pnl + unrealized

    @property
    def legs(self):
        """Return current legs as a list (for UI compatibility)."""
        result = []
        if self.call_position:
            result.append({
                'symbol': self.call_position.get('symbol', ''),
                'strike': self.call_position['strike'],
                'type': 'call',
                'side': 'sell',
                'size': self.quantity,
                'entry_price': round(self.call_entry_price, 2),
                'current_mark': round(self._call_current, 2),
                'current_pnl': round((self.call_entry_price - self._call_current) * self.quantity, 2),
                'delta': self.call_position.get('delta', 0),
            })
        if self.put_position:
            result.append({
                'symbol': self.put_position.get('symbol', ''),
                'strike': self.put_position['strike'],
                'type': 'put',
                'side': 'sell',
                'size': self.quantity,
                'entry_price': round(self.put_entry_price, 2),
                'current_mark': round(self._put_current, 2),
                'current_pnl': round((self.put_entry_price - self._put_current) * self.quantity, 2),
                'delta': self.put_position.get('delta', 0),
            })
        return result
