import logging
import time
import threading
from datetime import datetime
from api import (
    get_option_chain, find_target_delta_options, get_product_details,
    place_order, get_current_price, get_positions,
    get_position_entry_price, calculate_total_pnl
)
from websocket import WebSocketManager
from strategy.base import BaseStrategy
from strategy.state import save_state, load_state, clear_state

logger = logging.getLogger(__name__)


class DeltaNeutralStrategy(BaseStrategy):
    def __init__(self, asset='BTC', expiry_date='01-04-2026', target_delta=0.20,
                 delta_tolerance=0.05, lot_size=100, premium_threshold=0.4,
                 target_pnl=25, max_adjustments=5, monitoring_interval=5,
                 tp_sl_percent=0.70, tp_percent=None, sl_percent=None):
        self.asset = asset
        self.expiry_date = expiry_date
        self.target_delta = target_delta
        self.delta_tolerance = delta_tolerance
        self.lot_size = lot_size
        self.premium_threshold = premium_threshold
        self.target_pnl = target_pnl
        self.max_adjustments = max_adjustments
        self.monitoring_interval = monitoring_interval
        self.tp_sl_percent = tp_sl_percent
        # Separate TP/SL support; fall back to tp_sl_percent
        self.tp_percent = tp_percent if tp_percent is not None else tp_sl_percent
        self.sl_percent = sl_percent if sl_percent is not None else tp_sl_percent

        self.call_position = None
        self.put_position = None
        self.call_entry_price = 0
        self.put_entry_price = 0
        self.call_contract_value = 0.001
        self.put_contract_value = 0.001
        self.total_pnl = 0
        self.realized_pnl = 0
        self.unrealized_pnl = 0
        self.cumulative_realized_pnl = 0
        self.realized_pnl_snapshot = 0
        self.total_premium_collected = 0
        self.stop_loss = target_pnl  # default; overridden in initialize() based on tp_sl_percent
        self.adjustment_count = 0
        self.adjustment_history = []  # [{leg, symbol, entry, exit, pnl, timestamp}]
        self.call_actual_entry_price = 0
        self.put_actual_entry_price = 0
        self.running = True
        self.ws_manager = WebSocketManager(self)
        self.last_check_time_call = 0
        self.last_check_time_put = 0
        self.check_interval = self.monitoring_interval
        self._adjusting = threading.Lock()  # prevents concurrent adjustments
        self._state_lock = threading.Lock()  # protects shared mutable state
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10  # emergency exit after this many
        self._on_state_change = None  # optional callback for parent to persist state

    def _save_state(self):
        """Persist current state to disk and notify parent."""
        try:
            save_state(self)
        except Exception as e:
            logger.error(f"State save failed: {e}")
        # Notify parent (e.g., WeeklyDeltaNeutral) to persist to DB
        if self._on_state_change:
            try:
                self._on_state_change()
            except Exception as e:
                logger.debug(f"State change callback failed: {e}")

    def try_restore(self):
        """Try to restore from saved state file.

        If a state file exists and the positions are still open on the exchange,
        restores cumulative_realized_pnl, adjustment_count, positions, etc.
        Returns True if restored successfully, False otherwise.
        """
        state = load_state()
        if not state:
            return False

        # Check that expiry matches (don't restore stale state from a different cycle)
        if state.get('expiry_date') != self.expiry_date:
            logger.info(f"State file expiry ({state.get('expiry_date')}) != current ({self.expiry_date}) — ignoring")
            clear_state()
            return False

        # Verify positions still exist on exchange
        positions = get_positions()
        if positions is None:
            logger.warning("Cannot verify positions — skipping restore")
            return False

        call_pos = state.get('call_position')
        put_pos = state.get('put_position')
        if not call_pos or not put_pos:
            logger.warning("State has no positions — starting fresh")
            clear_state()
            return False

        # Check if at least one leg is still open on exchange
        open_product_ids = {pos.get('product_id') for pos in positions if int(pos.get('size', 0)) != 0}
        call_open = call_pos.get('product_id') in open_product_ids
        put_open = put_pos.get('product_id') in open_product_ids

        if not call_open and not put_open:
            logger.warning("Neither position from state is open — starting fresh")
            clear_state()
            return False

        # Restore state
        self.cumulative_realized_pnl = state.get('cumulative_realized_pnl', 0)
        self.realized_pnl_snapshot = state.get('realized_pnl_snapshot', 0)
        self.total_premium_collected = state.get('total_premium_collected', 0)
        self.target_pnl = state.get('target_pnl', self.target_pnl)
        self.stop_loss = state.get('stop_loss', self.stop_loss)
        self.adjustment_count = state.get('adjustment_count', 0)
        self.adjustment_history = state.get('adjustment_history', [])

        # Reconcile: adjustment_history is the source of truth for realized PnL.
        # If cumulative_realized_pnl is stale (e.g., server crashed after adjustment
        # but before state was fully persisted), reconstruct from history.
        if self.adjustment_history:
            history_pnl = sum(h.get('pnl', 0) for h in self.adjustment_history)
            if abs(history_pnl - self.cumulative_realized_pnl) > 0.01:
                logger.warning(f"  ⚠ Realized PnL mismatch: saved=${self.cumulative_realized_pnl:.2f} vs history=${history_pnl:.2f}")
                logger.warning(f"  ⚠ Using adjustment history as source of truth: ${history_pnl:.2f}")
                self.cumulative_realized_pnl = history_pnl
                self.realized_pnl_snapshot = history_pnl

        self.call_position = call_pos
        self.put_position = put_pos
        self.call_entry_price = state.get('call_entry_price', 0)
        self.put_entry_price = state.get('put_entry_price', 0)
        self.call_actual_entry_price = state.get('call_actual_entry_price', 0)
        self.put_actual_entry_price = state.get('put_actual_entry_price', 0)
        self.call_contract_value = state.get('call_contract_value', 0.001)
        self.put_contract_value = state.get('put_contract_value', 0.001)

        logger.info("=" * 70)
        logger.info("✓ STATE RESTORED FROM DISK")
        logger.info(f"  Realized PnL: ${self.cumulative_realized_pnl:.2f}")
        logger.info(f"  Adjustments: {self.adjustment_count}/{self.max_adjustments}")
        logger.info(f"  Call: {call_pos['symbol']} (entry ${self.call_entry_price:.2f}) {'[OPEN]' if call_open else '[CLOSED]'}")
        logger.info(f"  Put:  {put_pos['symbol']} (entry ${self.put_entry_price:.2f}) {'[OPEN]' if put_open else '[CLOSED]'}")
        logger.info(f"  TP: ${self.target_pnl:.2f} | SL: -${self.stop_loss:.2f}")
        logger.info("=" * 70)

        # Start WebSocket for open positions
        self.ws_manager.start()
        time.sleep(2)
        symbols = []
        if call_open:
            symbols.append(call_pos['symbol'])
        if put_open:
            symbols.append(put_pos['symbol'])
        if symbols:
            self.ws_manager.subscribe(symbols)
            logger.info(f"✓ Subscribed to: {symbols}")

        return True

    def on_price_update(self, symbol, mark_price, delta):
        now = time.time()
        with self._state_lock:
            call_pos = self.call_position
            put_pos = self.put_position
        if call_pos and symbol == call_pos['symbol']:
            if now - self.last_check_time_call < self.check_interval:
                return
            self.last_check_time_call = now
            self.check_adjustment('call', mark_price, delta)
        elif put_pos and symbol == put_pos['symbol']:
            if now - self.last_check_time_put < self.check_interval:
                return
            self.last_check_time_put = now
            self.check_adjustment('put', mark_price, delta)

    def _get_other_leg_price(self, leg):
        """Get current price of the opposite leg via WS or REST."""
        if leg == 'call':
            pos = self.put_position
            fallback_price = self.put_entry_price
        else:
            pos = self.call_position
            fallback_price = self.call_entry_price
        ws_data = self.ws_manager.get_latest_price(pos['symbol'])
        if ws_data:
            return ws_data['mark_price']
        from_api = get_current_price(pos['product_id'], self.asset)
        return from_api['mark_price'] if from_api else fallback_price

    def check_adjustment(self, leg, current_price, current_delta):
        if not self._adjusting.acquire(blocking=False):
            return  # another adjustment is already in progress
        try:
            self._check_adjustment_inner(leg, current_price, current_delta)
        finally:
            self._adjusting.release()

    def _check_adjustment_inner(self, leg, current_price, current_delta):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._state_lock:
            entry = self.call_entry_price if leg == 'call' else self.put_entry_price
            adj_count = self.adjustment_count
        if entry <= 0 or current_price <= 0:
            return
        if adj_count >= self.max_adjustments:
            return
        change = (current_price - entry) / entry
        if change < self.premium_threshold:
            return
        other_price = self._get_other_leg_price(leg)
        logger.warning(f"[{ts}] ⚠ ALERT: {leg.upper()} premium increased by {change:.2%}! (threshold: {self.premium_threshold:.2%})")
        logger.warning(f"  Entry: ${entry:.2f} → Current: ${current_price:.2f}")
        if leg == 'call':
            logger.info("  Action: Closing put position and re-entering NEW put with matching delta")
            self.adjust_position('call', current_delta, current_price, other_price)
        else:
            logger.info("  Action: Closing call position and re-entering NEW call with matching delta")
            self.adjust_position('put', current_delta, other_price, current_price)

    def initialize(self):
        # Try to restore from a previous run (handles server restart mid-cycle)
        if self.try_restore():
            logger.info("✓ Resumed from saved state — skipping fresh initialization")
            return True

        logger.info("=" * 70)
        logger.info("DELTA NEUTRAL OPTIONS STRATEGY (WebSocket Enabled)")
        logger.info("=" * 70)
        logger.info(f"Asset: {self.asset} | Expiry: {self.expiry_date} | Delta: ±{self.target_delta} | Lots: {self.lot_size}")
        logger.info(f"Threshold: {self.premium_threshold*100}% | Target PnL: ±${self.target_pnl}")
        logger.info("=" * 70)

        logger.info("[1/4] Fetching option chain...")
        option_chain = get_option_chain(self.expiry_date, self.asset)
        if not option_chain:
            logger.warning("✗ Failed to fetch option chain")
            return False

        logger.info(f"[2/4] Finding options with ~{self.target_delta} delta...")
        call_option, put_option = find_target_delta_options(option_chain, self.target_delta, self.delta_tolerance)
        if not call_option or not put_option:
            logger.warning("✗ Could not find suitable options with target delta")
            return False

        logger.info(f"✓ Call: {call_option['symbol']} | Strike: {call_option['strike_price']} | Δ: {call_option['delta']:.4f} | ${call_option['mark_price']:.2f}")
        logger.info(f"✓ Put:  {put_option['symbol']} | Strike: {put_option['strike_price']} | Δ: {put_option['delta']:.4f} | ${put_option['mark_price']:.2f}")

        logger.info("[3/4] Fetching contract specs...")
        for opt, attr in [(call_option, 'call'), (put_option, 'put')]:
            details = get_product_details(opt['product_id'])
            if details:
                setattr(self, f'{attr}_contract_value', details['contract_value'])
                logger.info(f"✓ {attr.title()} contract value: {details['contract_value']} {details['contract_unit_currency']}")

        call_prem = call_option['mark_price'] * self.lot_size * self.call_contract_value
        put_prem = put_option['mark_price'] * self.lot_size * self.put_contract_value
        self.total_premium_collected = call_prem + put_prem
        self.target_pnl = self.total_premium_collected * self.tp_percent
        self.stop_loss = self.total_premium_collected * self.sl_percent
        logger.info(f"Expected Premium: Call=${call_prem:.2f} + Put=${put_prem:.2f} = ${self.total_premium_collected:.2f}")
        logger.info(f"TP: ${self.target_pnl:.2f} ({int(self.tp_percent*100)}%) | SL: -${self.stop_loss:.2f} ({int(self.sl_percent*100)}%)")

        logger.info("[4/4] Placing initial orders...")
        call_order = place_order(call_option['product_id'], call_option['symbol'], self.lot_size, 'sell')
        if not call_order:
            return False
        put_order = place_order(put_option['product_id'], put_option['symbol'], self.lot_size, 'sell')
        if not put_order:
            return False

        time.sleep(2)
        call_actual, _ = get_position_entry_price(call_option['product_id'])
        put_actual, _ = get_position_entry_price(put_option['product_id'])

        self.call_position = call_option
        self.put_position = put_option
        self.call_entry_price = call_option['mark_price']
        self.put_entry_price = put_option['mark_price']
        self.call_actual_entry_price = call_actual or call_option['mark_price']
        self.put_actual_entry_price = put_actual or put_option['mark_price']

        logger.info("=" * 70)
        logger.info("✓ STRATEGY INITIALIZED")
        logger.info(f"Short Call: {call_option['symbol']} @ ${self.call_actual_entry_price:.2f}")
        logger.info(f"Short Put:  {put_option['symbol']} @ ${self.put_actual_entry_price:.2f}")
        logger.info("=" * 70)

        self.ws_manager.start()
        time.sleep(2)
        symbols = [self.call_position['symbol'], self.put_position['symbol']]
        self.ws_manager.subscribe(symbols)
        logger.info(f"✓ Subscribed to real-time updates for {symbols}")

        # Save initial state so we can recover if server restarts
        self._save_state()
        return True

    def monitor_and_adjust(self):
        logger.info(f"[MONITORING] Active — updates every {self.monitoring_interval}s. Ctrl+C to stop.")
        iteration = 0
        try:
            while self.running:
                iteration += 1
                ts = datetime.now().strftime("%H:%M:%S")
                positions = get_positions()
                if positions is None:
                    logger.info(f"[{ts}] Warning: Position fetch failed, skipping cycle")
                    time.sleep(self.monitoring_interval)
                    continue

                call_ws = self.ws_manager.get_latest_price(self.call_position['symbol'])
                put_ws = self.ws_manager.get_latest_price(self.put_position['symbol'])

                if call_ws and put_ws:
                    call_price, put_price, source = call_ws['mark_price'], put_ws['mark_price'], "WS"
                else:
                    cd = get_current_price(self.call_position['product_id'], self.asset)
                    pd = get_current_price(self.put_position['product_id'], self.asset)
                    if not cd or not pd:
                        self._consecutive_failures += 1
                        logger.warning(f"[{ts}] ⚠ Price fetch failed ({self._consecutive_failures}/{self._max_consecutive_failures})")
                        if self._consecutive_failures >= self._max_consecutive_failures:
                            logger.error(f"[{ts}] 🚨 EMERGENCY: {self._consecutive_failures} consecutive failures — closing all positions")
                            self.close_all_positions()
                            self.running = False
                            break
                        time.sleep(self.monitoring_interval)
                        continue
                    call_price, put_price, source = cd['mark_price'], pd['mark_price'], "REST"

                self._consecutive_failures = 0  # reset on successful fetch

                call_chg = (call_price - self.call_entry_price) / self.call_entry_price if self.call_entry_price > 0 else 0
                put_chg = (put_price - self.put_entry_price) / self.put_entry_price if self.put_entry_price > 0 else 0

                # Only check adjustments from polling when WS is NOT active
                # (WS callback on_price_update handles it when WS is connected)
                if source == "REST":
                    if call_chg >= self.premium_threshold:
                        call_delta = 0
                        self.check_adjustment('call', call_price, call_delta)
                    if put_chg >= self.premium_threshold:
                        put_delta = 0
                        self.check_adjustment('put', put_price, put_delta)

                self.realized_pnl, self.unrealized_pnl, self.total_pnl, c_info, p_info = calculate_total_pnl(
                    positions, call_price, put_price,
                    self.call_position['product_id'], self.put_position['product_id'],
                    self.call_contract_value, self.put_contract_value,
                    self.cumulative_realized_pnl
                )

                logger.info(f"[{ts}] #{iteration} | Adj: {self.adjustment_count}/{self.max_adjustments} | {source}")
                for label, price, chg, entry, info in [
                    ("Call", call_price, call_chg, self.call_entry_price, c_info),
                    ("Put ", put_price, put_chg, self.put_entry_price, p_info)
                ]:
                    line = f"  {label}: ${price:.2f} ({chg:+.2%} from ${entry:.2f})"
                    if info:
                        line += f" | Size:{info['size']} | UPnL:${info['unrealized_pnl']:.2f}"
                    else:
                        line += " | No position"
                    logger.info(line)
                logger.info(f"  P&L: R=${self.realized_pnl:.2f} | U=${self.unrealized_pnl:.2f} | T=${self.total_pnl:.2f}")

                if self.adjustment_count >= self.max_adjustments:
                    logger.info("=" * 70)
                    logger.info(f"✓ MAX ADJUSTMENTS REACHED! Count: {self.adjustment_count} | Total PnL: ${self.total_pnl:.2f}")
                    logger.info("=" * 70)
                    self.close_all_positions()
                    self.running = False
                    break

                if self.total_pnl >= self.target_pnl or self.total_pnl <= -self.stop_loss:
                    logger.info("=" * 70)
                    pct_label = int(self.tp_sl_percent * 100)
                    if self.total_pnl >= self.target_pnl:
                        logger.info(f"🎯 TARGET PROFIT REACHED! Total: ${self.total_pnl:.2f} | Target: ${self.target_pnl:.2f} ({int(self.tp_percent*100)}% of ${self.total_premium_collected:.2f})")
                    else:
                        logger.info(f"🛑 STOP LOSS HIT! Total: ${self.total_pnl:.2f} | SL: -${self.stop_loss:.2f} ({int(self.sl_percent*100)}% of ${self.total_premium_collected:.2f})")
                    logger.info(f"  Adjustments: {self.adjustment_count}")
                    logger.info("=" * 70)
                    self.close_all_positions()
                    self.running = False
                    break

                time.sleep(self.monitoring_interval)
        except KeyboardInterrupt:
            logger.info("[STOPPED] Strategy stopped by user")
            self.close_all_positions()

    def _rollback(self, close_leg, close_pos, realized):
        """Attempt to re-open a closed leg after a failed adjustment."""
        rollback = place_order(close_pos['product_id'], close_pos['symbol'], self.lot_size, 'sell')
        if rollback:
            logger.info(f"  ↩ Rolled back: re-opened {close_leg.upper()} {close_pos['symbol']}")
            self.cumulative_realized_pnl -= realized
            self.ws_manager.subscribe([close_pos['symbol']])
        else:
            logger.warning(f"  ⚠ ROLLBACK FAILED — {close_leg.upper()} leg is now missing!")

    def _close_leg(self, close_leg, close_pos, close_cv, close_current):
        """Close the opposite leg and record realized P&L. Returns (realized, success)."""
        entry_from_pos, size = get_position_entry_price(close_pos['product_id'])
        if entry_from_pos is None:
            logger.warning(f"  ✗ Could not fetch {close_leg.upper()} position entry price")
            return 0, False

        realized = (entry_from_pos - close_current) * abs(size) * close_cv
        logger.info(f"  [1/3] Closing {close_leg.upper()}: Entry=${entry_from_pos:.2f} Current=${close_current:.2f} PnL=${realized:+.2f}")

        self.adjustment_history.append({
            'leg': close_leg, 'symbol': close_pos['symbol'],
            'strike': close_pos.get('strike_price', ''),
            'entry': round(entry_from_pos, 2), 'exit': round(close_current, 2),
            'pnl': round(realized, 2), 'size': abs(size),
            'timestamp': datetime.now().isoformat(),
            'adjustment': self.adjustment_count + 1,
        })

        self.ws_manager.unsubscribe([close_pos['symbol']])
        close_result = place_order(close_pos['product_id'], close_pos['symbol'], self.lot_size, 'buy')
        if close_result is None:
            logger.warning(f"  ✗ Failed to close {close_leg.upper()} — aborting adjustment")
            self.ws_manager.subscribe([close_pos['symbol']])
            return 0, False
        self.cumulative_realized_pnl += realized
        time.sleep(2)
        return realized, True

    def _find_replacement(self, triggered_leg, triggered_delta, close_leg):
        """Find a replacement option for the closed leg. Returns option dict or None."""
        triggered_pos = self.call_position if triggered_leg == 'call' else self.put_position
        live_data = get_current_price(triggered_pos['product_id'], self.asset)
        if live_data and live_data.get('delta'):
            triggered_delta = live_data['delta']

        search_delta = abs(triggered_delta) if abs(triggered_delta) > self.delta_tolerance else self.target_delta
        logger.info(f"  [2/3] Finding NEW {close_leg.upper()} with delta {search_delta:.4f}...")
        option_chain = get_option_chain(self.expiry_date, self.asset)

        if triggered_leg == 'call':
            _, new_opt = find_target_delta_options(option_chain, search_delta, self.delta_tolerance)
        else:
            new_opt, _ = find_target_delta_options(option_chain, search_delta, self.delta_tolerance)
        return new_opt

    def adjust_position(self, triggered_leg, triggered_delta, call_current_price, put_current_price):
        logger.info(f"  [SNAPSHOT] Cumulative realized PnL: ${self.cumulative_realized_pnl:.2f}")

        if triggered_leg == 'call':
            close_leg, close_pos = 'put', self.put_position
            close_cv, close_current = self.put_contract_value, put_current_price
        else:
            close_leg, close_pos = 'call', self.call_position
            close_cv, close_current = self.call_contract_value, call_current_price

        realized, ok = self._close_leg(close_leg, close_pos, close_cv, close_current)
        if not ok:
            return

        new_opt = self._find_replacement(triggered_leg, triggered_delta, close_leg)
        if not new_opt:
            logger.warning(f"  ✗ Could not find suitable {close_leg.upper()} option — rolling back")
            self._rollback(close_leg, close_pos, realized)
            return

        logger.info(f"  [3/3] Entering NEW {close_leg.upper()}: {new_opt['symbol']} @ ${new_opt['mark_price']:.2f}")
        new_order = place_order(new_opt['product_id'], new_opt['symbol'], self.lot_size, 'sell')
        if new_order is None:
            logger.warning(f"  ✗ Failed to open new {close_leg.upper()} — rolling back")
            self._rollback(close_leg, close_pos, realized)
            return
        time.sleep(2)

        new_entry, _ = get_position_entry_price(new_opt['product_id'])
        details = get_product_details(new_opt['product_id'])

        if triggered_leg == 'call':
            with self._state_lock:
                self.put_position = new_opt
                if details:
                    self.put_contract_value = details['contract_value']
                self.call_entry_price = call_current_price
                self.put_entry_price = new_opt['mark_price']
                self.put_actual_entry_price = new_entry or new_opt['mark_price']
                self.call_actual_entry_price = call_current_price
        else:
            with self._state_lock:
                self.call_position = new_opt
                if details:
                    self.call_contract_value = details['contract_value']
                self.call_entry_price = new_opt['mark_price']
                self.put_entry_price = put_current_price
                self.call_actual_entry_price = new_entry or new_opt['mark_price']
                self.put_actual_entry_price = put_current_price

        with self._state_lock:
            self.realized_pnl_snapshot = self.cumulative_realized_pnl
            self.adjustment_count += 1
        self.ws_manager.subscribe([new_opt['symbol']])
        self.last_check_time_call = time.time() + 30
        self.last_check_time_put = time.time() + 30

        logger.info(f"  ✓ Adjustment #{self.adjustment_count} done | Cumulative PnL: ${self.cumulative_realized_pnl:.2f}")
        logger.info(f"  ✓ New baselines — Call: ${self.call_entry_price:.2f} | Put: ${self.put_entry_price:.2f}")

        # Persist state after every adjustment so realized PnL survives restart
        self._save_state()

    def close_all_positions(self):
        logger.info("[CLOSING] Closing all positions...")
        self.running = False
        for label, pos, cv in [
            ("CALL", self.call_position, self.call_contract_value),
            ("PUT", self.put_position, self.put_contract_value)
        ]:
            if not pos:
                continue
            data = get_current_price(pos['product_id'], self.asset)
            entry, size = get_position_entry_price(pos['product_id'])
            if data and entry and size != 0:
                pnl = (entry - data['mark_price']) * abs(size) * cv
                self.cumulative_realized_pnl += pnl
                logger.info(f"  {label}: Entry=${entry:.2f} Exit=${data['mark_price']:.2f} PnL=${pnl:+.2f}")
            place_order(pos['product_id'], pos['symbol'], self.lot_size, 'buy')

        time.sleep(2)
        self.ws_manager.stop()
        logger.info(f"✓ All positions closed | Final PnL: ${self.cumulative_realized_pnl:.2f} | Adjustments: {self.adjustment_count}")

        # Clear state file — strategy completed normally, no need to recover
        clear_state()

    def monitor(self):
        self.monitor_and_adjust()

    def close_all(self):
        self.close_all_positions()

    def stop(self):
        self.close_all_positions()
        self.ws_manager.stop()

    @property
    def pnl(self):
        return self.total_pnl
