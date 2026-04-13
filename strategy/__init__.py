import time
from datetime import datetime
from api import (
    get_option_chain, find_target_delta_options, get_product_details,
    place_order, get_current_price, get_positions,
    get_position_entry_price, calculate_total_pnl
)
from websocket import WebSocketManager


class DeltaNeutralStrategy:
    def __init__(self, asset='BTC', expiry_date='01-04-2026', target_delta=0.20,
                 delta_tolerance=0.05, lot_size=100, premium_threshold=0.4,
                 target_pnl=25, max_adjustments=5, monitoring_interval=5):
        self.asset = asset
        self.expiry_date = expiry_date
        self.target_delta = target_delta
        self.delta_tolerance = delta_tolerance
        self.lot_size = lot_size
        self.premium_threshold = premium_threshold
        self.target_pnl = target_pnl
        self.max_adjustments = max_adjustments
        self.monitoring_interval = monitoring_interval

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
        self.adjustment_count = 0
        self.adjustment_history = []  # [{leg, symbol, entry, exit, pnl, timestamp}]
        self.call_actual_entry_price = 0
        self.put_actual_entry_price = 0
        self.running = True
        self.ws_manager = WebSocketManager(self)
        self.last_check_time_call = 0
        self.last_check_time_put = 0
        self.check_interval = self.monitoring_interval

    def on_price_update(self, symbol, mark_price, delta):
        now = time.time()
        if self.call_position and symbol == self.call_position['symbol']:
            if now - self.last_check_time_call < self.check_interval:
                return
            self.last_check_time_call = now
            self.check_adjustment('call', mark_price, delta)
        elif self.put_position and symbol == self.put_position['symbol']:
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
        ts = datetime.now().strftime("%H:%M:%S")
        entry = self.call_entry_price if leg == 'call' else self.put_entry_price
        if entry <= 0 or current_price <= 0:
            return
        if self.adjustment_count >= self.max_adjustments:
            return
        change = (current_price - entry) / entry
        if change < self.premium_threshold:
            return
        other_price = self._get_other_leg_price(leg)
        print(f"[{ts}] ⚠ ALERT: {leg.upper()} premium increased by {change:.2%}! (threshold: {self.premium_threshold:.2%})")
        print(f"  Entry: ${entry:.2f} → Current: ${current_price:.2f}")
        if leg == 'call':
            print("  Action: Closing put position and re-entering NEW put with matching delta")
            self.adjust_position('call', current_delta, current_price, other_price)
        else:
            print("  Action: Closing call position and re-entering NEW call with matching delta")
            self.adjust_position('put', current_delta, other_price, current_price)

    def initialize(self):
        print("=" * 70)
        print("DELTA NEUTRAL OPTIONS STRATEGY (WebSocket Enabled)")
        print("=" * 70)
        print(f"Asset: {self.asset} | Expiry: {self.expiry_date} | Delta: ±{self.target_delta} | Lots: {self.lot_size}")
        print(f"Threshold: {self.premium_threshold*100}% | Target PnL: ±${self.target_pnl}")
        print("=" * 70)

        print("[1/4] Fetching option chain...")
        option_chain = get_option_chain(self.expiry_date, self.asset)
        if not option_chain:
            print("✗ Failed to fetch option chain")
            return False

        print(f"[2/4] Finding options with ~{self.target_delta} delta...")
        call_option, put_option = find_target_delta_options(option_chain, self.target_delta, self.delta_tolerance)
        if not call_option or not put_option:
            print("✗ Could not find suitable options with target delta")
            return False

        print(f"✓ Call: {call_option['symbol']} | Strike: {call_option['strike_price']} | Δ: {call_option['delta']:.4f} | ${call_option['mark_price']:.2f}")
        print(f"✓ Put:  {put_option['symbol']} | Strike: {put_option['strike_price']} | Δ: {put_option['delta']:.4f} | ${put_option['mark_price']:.2f}")

        print("[3/4] Fetching contract specs...")
        for opt, attr in [(call_option, 'call'), (put_option, 'put')]:
            details = get_product_details(opt['product_id'])
            if details:
                setattr(self, f'{attr}_contract_value', details['contract_value'])
                print(f"✓ {attr.title()} contract value: {details['contract_value']} {details['contract_unit_currency']}")

        call_prem = call_option['mark_price'] * self.lot_size * self.call_contract_value
        put_prem = put_option['mark_price'] * self.lot_size * self.put_contract_value
        print(f"Expected Premium: Call=${call_prem:.2f} + Put=${put_prem:.2f} = ${call_prem+put_prem:.2f}")

        print("[4/4] Placing initial orders...")
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

        print("=" * 70)
        print("✓ STRATEGY INITIALIZED")
        print(f"Short Call: {call_option['symbol']} @ ${self.call_actual_entry_price:.2f}")
        print(f"Short Put:  {put_option['symbol']} @ ${self.put_actual_entry_price:.2f}")
        print("=" * 70)

        self.ws_manager.start()
        time.sleep(2)
        symbols = [self.call_position['symbol'], self.put_position['symbol']]
        self.ws_manager.subscribe(symbols)
        print(f"✓ Subscribed to real-time updates for {symbols}")
        return True

    def monitor_and_adjust(self):
        print(f"[MONITORING] Active — updates every {self.monitoring_interval}s. Ctrl+C to stop.")
        iteration = 0
        try:
            while self.running:
                iteration += 1
                ts = datetime.now().strftime("%H:%M:%S")
                positions = get_positions()

                call_ws = self.ws_manager.get_latest_price(self.call_position['symbol'])
                put_ws = self.ws_manager.get_latest_price(self.put_position['symbol'])

                if call_ws and put_ws:
                    call_price, put_price, source = call_ws['mark_price'], put_ws['mark_price'], "WS"
                else:
                    cd = get_current_price(self.call_position['product_id'], self.asset)
                    pd = get_current_price(self.put_position['product_id'], self.asset)
                    if not cd or not pd:
                        print(f"[{ts}] Warning: Could not fetch prices")
                        time.sleep(self.monitoring_interval)
                        continue
                    call_price, put_price, source = cd['mark_price'], pd['mark_price'], "REST"

                call_chg = (call_price - self.call_entry_price) / self.call_entry_price if self.call_entry_price > 0 else 0
                put_chg = (put_price - self.put_entry_price) / self.put_entry_price if self.put_entry_price > 0 else 0

                if call_chg >= self.premium_threshold:
                    call_delta = call_ws['delta'] if call_ws else 0
                    self.check_adjustment('call', call_price, call_delta)
                if put_chg >= self.premium_threshold:
                    put_delta = put_ws['delta'] if put_ws else 0
                    self.check_adjustment('put', put_price, put_delta)

                self.realized_pnl, self.unrealized_pnl, self.total_pnl, c_info, p_info = calculate_total_pnl(
                    positions, call_price, put_price,
                    self.call_position['product_id'], self.put_position['product_id'],
                    self.call_contract_value, self.put_contract_value,
                    self.cumulative_realized_pnl
                )

                print(f"[{ts}] #{iteration} | Adj: {self.adjustment_count}/{self.max_adjustments} | {source}")
                for label, price, chg, entry, info in [
                    ("Call", call_price, call_chg, self.call_entry_price, c_info),
                    ("Put ", put_price, put_chg, self.put_entry_price, p_info)
                ]:
                    line = f"  {label}: ${price:.2f} ({chg:+.2%} from ${entry:.2f})"
                    if info:
                        line += f" | Size:{info['size']} | UPnL:${info['unrealized_pnl']:.2f}"
                    else:
                        line += " | No position"
                    print(line)
                print(f"  P&L: R=${self.realized_pnl:.2f} | U=${self.unrealized_pnl:.2f} | T=${self.total_pnl:.2f}")

                if self.adjustment_count >= self.max_adjustments:
                    print("=" * 70)
                    print(f"✓ MAX ADJUSTMENTS REACHED! Count: {self.adjustment_count} | Total PnL: ${self.total_pnl:.2f}")
                    print("=" * 70)
                    self.close_all_positions()
                    self.running = False
                    break

                if abs(self.total_pnl) >= self.target_pnl:
                    print("=" * 70)
                    print(f"✓ TARGET P&L REACHED! Total: ${self.total_pnl:.2f} | Adjustments: {self.adjustment_count}")
                    print("=" * 70)
                    self.close_all_positions()
                    self.running = False
                    break

                time.sleep(self.monitoring_interval)
        except KeyboardInterrupt:
            print("[STOPPED] Strategy stopped by user")
            self.close_all_positions()

    def adjust_position(self, triggered_leg, triggered_delta, call_current_price, put_current_price):
        print(f"  [SNAPSHOT] Cumulative realized PnL: ${self.cumulative_realized_pnl:.2f}")

        if triggered_leg == 'call':
            close_leg, close_pos = 'put', self.put_position
            close_cv = self.put_contract_value
            close_current = put_current_price
        else:
            close_leg, close_pos = 'call', self.call_position
            close_cv = self.call_contract_value
            close_current = call_current_price

        entry_from_pos, size = get_position_entry_price(close_pos['product_id'])
        if entry_from_pos is None:
            print(f"  ✗ Could not fetch {close_leg.upper()} position entry price")
            return

        realized = (entry_from_pos - close_current) * abs(size) * close_cv
        print(f"  [1/3] Closing {close_leg.upper()}: Entry=${entry_from_pos:.2f} Current=${close_current:.2f} PnL=${realized:+.2f}")

        self.adjustment_history.append({
            'leg': close_leg, 'symbol': close_pos['symbol'],
            'strike': close_pos.get('strike_price', ''),
            'entry': round(entry_from_pos, 2), 'exit': round(close_current, 2),
            'pnl': round(realized, 2), 'size': abs(size),
            'timestamp': datetime.now().isoformat(),
            'adjustment': self.adjustment_count + 1,
        })

        self.ws_manager.unsubscribe([close_pos['symbol']])
        place_order(close_pos['product_id'], close_pos['symbol'], self.lot_size, 'buy')
        self.cumulative_realized_pnl += realized
        time.sleep(2)

        # Fetch live delta from REST API instead of relying on WS delta (may be 0)
        triggered_pos = self.call_position if triggered_leg == 'call' else self.put_position
        live_data = get_current_price(triggered_pos['product_id'], self.asset)
        if live_data and live_data.get('delta'):
            triggered_delta = live_data['delta']

        search_delta = abs(triggered_delta) if abs(triggered_delta) > self.delta_tolerance else self.target_delta
        print(f"  [2/3] Finding NEW {close_leg.upper()} with delta {search_delta:.4f}...")
        option_chain = get_option_chain(self.expiry_date, self.asset)

        if triggered_leg == 'call':
            _, new_opt = find_target_delta_options(option_chain, search_delta, self.delta_tolerance)
        else:
            new_opt, _ = find_target_delta_options(option_chain, search_delta, self.delta_tolerance)

        if not new_opt:
            print(f"  ✗ Could not find suitable {close_leg.upper()} option")
            return

        print(f"  [3/3] Entering NEW {close_leg.upper()}: {new_opt['symbol']} @ ${new_opt['mark_price']:.2f}")
        place_order(new_opt['product_id'], new_opt['symbol'], self.lot_size, 'sell')
        time.sleep(2)

        new_entry, _ = get_position_entry_price(new_opt['product_id'])
        details = get_product_details(new_opt['product_id'])

        if triggered_leg == 'call':
            self.put_position = new_opt
            if details:
                self.put_contract_value = details['contract_value']
            self.call_entry_price = call_current_price
            self.put_entry_price = new_opt['mark_price']
            self.put_actual_entry_price = new_entry or new_opt['mark_price']
            self.call_actual_entry_price = call_current_price
        else:
            self.call_position = new_opt
            if details:
                self.call_contract_value = details['contract_value']
            self.call_entry_price = new_opt['mark_price']
            self.put_entry_price = put_current_price
            self.call_actual_entry_price = new_entry or new_opt['mark_price']
            self.put_actual_entry_price = put_current_price

        self.realized_pnl_snapshot = self.cumulative_realized_pnl
        self.adjustment_count += 1
        self.ws_manager.subscribe([new_opt['symbol']])
        # Cooldown: prevent immediate re-trigger after adjustment
        self.last_check_time_call = time.time() + 30
        self.last_check_time_put = time.time() + 30

        print(f"  ✓ Adjustment #{self.adjustment_count} done | Cumulative PnL: ${self.cumulative_realized_pnl:.2f}")
        print(f"  ✓ New baselines — Call: ${self.call_entry_price:.2f} | Put: ${self.put_entry_price:.2f}")

    def close_all_positions(self):
        print("[CLOSING] Closing all positions...")
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
                print(f"  {label}: Entry=${entry:.2f} Exit=${data['mark_price']:.2f} PnL=${pnl:+.2f}")
            place_order(pos['product_id'], pos['symbol'], self.lot_size, 'buy')

        time.sleep(2)
        self.ws_manager.stop()
        print(f"✓ All positions closed | Final PnL: ${self.cumulative_realized_pnl:.2f} | Adjustments: {self.adjustment_count}")
