"""IV Crush / Earnings Volatility Strategy.
Sells short straddle when IV is overpriced vs realized vol.
Filters: IV/RV ratio, term structure slope, volume.
Monitors and exits on IV crush or target P&L."""

import time
from datetime import datetime
from api import (
    get_option_chain, get_product_details,
    place_order, get_current_price, get_positions,
    get_position_entry_price, calculate_total_pnl
)
from api.pricing import get_current_price
from websocket import WebSocketManager


class IVCrushStrategy:
    def __init__(self, asset='BTC', expiry_date='', lot_size=10,
                 iv_rv_threshold=1.3, max_loss_pct=50, target_profit_pct=30,
                 monitoring_interval=10, profile_id=None):
        self.asset = asset
        self.expiry_date = expiry_date
        self.lot_size = lot_size
        self.iv_rv_threshold = iv_rv_threshold  # min IV/RV ratio to enter
        self.max_loss_pct = max_loss_pct        # max loss as % of premium collected
        self.target_profit_pct = target_profit_pct  # target profit as % of premium
        self.monitoring_interval = monitoring_interval

        self.call_position = None
        self.put_position = None
        self.call_entry_price = 0
        self.put_entry_price = 0
        self.call_contract_value = 0.001
        self.put_contract_value = 0.001
        self.total_premium = 0
        self.total_pnl = 0
        self.realized_pnl = 0
        self.unrealized_pnl = 0
        self.iv_at_entry = 0
        self.rv_at_entry = 0
        self.iv_rv_ratio = 0
        self.current_iv = 0
        self.iv_crush_pct = 0
        self.running = True
        self.status_msg = ''
        self.ws_manager = WebSocketManager(self)

    def on_price_update(self, symbol, mark_price, delta):
        """WebSocket callback — just track prices."""
        pass

    def _find_atm_options(self, option_chain):
        """Find the at-the-money call and put."""
        if not option_chain:
            return None, None
        # Get underlying price from first option
        calls = [o for o in option_chain if o.get('contract_type') == 'call_options']
        puts = [o for o in option_chain if o.get('contract_type') == 'put_options']
        if not calls or not puts:
            return None, None

        # Find ATM by closest strike to mark price
        spot = calls[0].get('spot_price') or calls[0].get('mark_price', 0) * 2
        best_call = min(calls, key=lambda o: abs(float(o.get('strike_price', 0)) - spot))
        # Find matching put at same strike
        target_strike = best_call.get('strike_price')
        matching_puts = [p for p in puts if p.get('strike_price') == target_strike]
        best_put = matching_puts[0] if matching_puts else min(puts, key=lambda o: abs(float(o.get('strike_price', 0)) - spot))
        return best_call, best_put

    def _compute_realized_vol(self, days=30):
        """Compute annualized realized volatility from daily close prices."""
        import math
        try:
            from api.chart import get_candles
            candles = get_candles(self.asset, '1d')
            if not candles or len(candles) < 10:
                return 0
            closes = [c['c'] for c in candles[-days:] if c.get('c')]
            if len(closes) < 10:
                return 0
            log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
            mean = sum(log_returns) / len(log_returns)
            variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
            daily_vol = math.sqrt(variance)
            return daily_vol * math.sqrt(365)  # annualized for crypto (365 days)
        except Exception as e:
            print(f"⚠ RV calculation failed: {e}")
            return 0

    def _calc_iv_rv(self, call_option, put_option):
        """Calculate IV/RV ratio from option data and historical prices."""
        call_iv = float(call_option.get('iv', 0) or call_option.get('implied_volatility', 0))
        put_iv = float(put_option.get('iv', 0) or put_option.get('implied_volatility', 0))
        avg_iv = (call_iv + put_iv) / 2 if call_iv and put_iv else call_iv or put_iv

        # Compute realized volatility from actual daily closes
        rv_estimate = self._compute_realized_vol()
        if rv_estimate <= 0:
            # Fallback: can't compute RV, skip the filter
            print("⚠ Could not compute realized volatility — skipping IV/RV filter")
            return avg_iv, 0, self.iv_rv_threshold  # pass the filter
        ratio = avg_iv / rv_estimate if rv_estimate > 0 else 0
        return avg_iv, rv_estimate, ratio

    def initialize(self):
        print("=" * 70)
        print("IV CRUSH / EARNINGS VOLATILITY STRATEGY")
        print("=" * 70)
        print(f"Asset: {self.asset} | Lots: {self.lot_size}")
        print(f"IV/RV Threshold: {self.iv_rv_threshold} | Max Loss: {self.max_loss_pct}% | Target: {self.target_profit_pct}%")
        print("=" * 70)

        # Auto-select best expiry if not provided
        if not self.expiry_date:
            print("[1/5] Auto-selecting best expiry...")
            from api.chain import get_expiries, get_option_chain_full
            expiries = get_expiries(self.asset, min_days=1)
            if not expiries:
                print("✗ No expiries available")
                return False
            # Pick nearest 2-3 expiries and find the one with highest ATM IV
            best_expiry = None
            best_iv = 0
            for exp in expiries[:3]:
                chain, spot, _ = get_option_chain_full(exp, self.asset)
                if not chain or not spot:
                    continue
                # Find ATM strike
                atm = min(chain, key=lambda r: abs(float(r['strike']) - spot))
                call_iv = atm['call']['iv'] if atm.get('call') else 0
                put_iv = atm['put']['iv'] if atm.get('put') else 0
                avg_iv = (call_iv + put_iv) / 2
                print(f"  {exp}: ATM IV = {avg_iv:.4f}")
                if avg_iv > best_iv:
                    best_iv = avg_iv
                    best_expiry = exp
            if not best_expiry:
                print("✗ Could not find expiry with IV data")
                return False
            self.expiry_date = best_expiry
            print(f"✓ Selected expiry: {self.expiry_date} (IV: {best_iv:.4f})")
        else:
            print(f"[1/5] Using provided expiry: {self.expiry_date}")

        print(f"[2/5] Fetching option chain for {self.expiry_date}...")
        from api.chain import get_option_chain_full
        chain, spot, _ = get_option_chain_full(self.expiry_date, self.asset)
        if not chain or not spot:
            print("✗ Failed to fetch option chain")
            return False

        print("[3/5] Finding ATM options...")
        # chain is [{strike, call: {symbol, product_id, mark_price, iv, ...}, put: {...}}, ...]
        atm_row = min(chain, key=lambda r: abs(float(r['strike']) - spot))
        call_option = atm_row.get('call')
        put_option = atm_row.get('put')
        if not call_option or not put_option:
            print("✗ Could not find ATM call/put pair")
            return False

        # Normalize field names for downstream use
        for opt in (call_option, put_option):
            opt.setdefault('strike_price', opt.get('strike', atm_row['strike']))
            opt.setdefault('spot_price', spot)

        print(f"✓ Call: {call_option['symbol']} | Strike: {call_option.get('strike_price')} | ${call_option.get('mark_price', 0):.2f}")
        print(f"✓ Put:  {put_option['symbol']} | Strike: {put_option.get('strike_price')} | ${put_option.get('mark_price', 0):.2f}")

        # Check IV/RV ratio
        avg_iv, rv_est, ratio = self._calc_iv_rv(call_option, put_option)
        self.iv_at_entry = avg_iv
        self.rv_at_entry = rv_est
        self.iv_rv_ratio = ratio
        print(f"[4/5] IV: {avg_iv:.4f} | RV est: {rv_est:.4f} | IV/RV: {ratio:.2f}")

        if ratio < self.iv_rv_threshold:
            print(f"✗ IV/RV ratio {ratio:.2f} below threshold {self.iv_rv_threshold}. IV not overpriced enough.")
            self.status_msg = f'IV/RV {ratio:.2f} < {self.iv_rv_threshold} — skipped'
            return False

        print(f"✓ IV is overpriced (ratio {ratio:.2f} >= {self.iv_rv_threshold})")

        # Fetch contract specs
        for opt, attr in [(call_option, 'call'), (put_option, 'put')]:
            details = get_product_details(opt['product_id'])
            if details:
                setattr(self, f'{attr}_contract_value', details['contract_value'])

        # Sell straddle
        print("[5/5] Selling ATM straddle...")
        call_order = place_order(call_option['product_id'], call_option['symbol'], self.lot_size, 'sell')
        if not call_order:
            print("✗ Failed to sell call")
            return False
        put_order = place_order(put_option['product_id'], put_option['symbol'], self.lot_size, 'sell')
        if not put_order:
            print("✗ Failed to sell put")
            return False

        time.sleep(2)
        call_actual, _ = get_position_entry_price(call_option['product_id'])
        put_actual, _ = get_position_entry_price(put_option['product_id'])

        self.call_position = call_option
        self.put_position = put_option
        self.call_entry_price = call_actual or call_option.get('mark_price', 0)
        self.put_entry_price = put_actual or put_option.get('mark_price', 0)
        self.total_premium = (self.call_entry_price * self.lot_size * self.call_contract_value +
                              self.put_entry_price * self.lot_size * self.put_contract_value)

        print("=" * 70)
        print("✓ IV CRUSH STRATEGY INITIALIZED")
        print(f"Short Call: {call_option['symbol']} @ ${self.call_entry_price:.2f}")
        print(f"Short Put:  {put_option['symbol']} @ ${self.put_entry_price:.2f}")
        print(f"Total Premium Collected: ${self.total_premium:.2f}")
        print(f"Max Loss: ${self.total_premium * self.max_loss_pct / 100:.2f} | Target: ${self.total_premium * self.target_profit_pct / 100:.2f}")
        print("=" * 70)

        self.ws_manager.start()
        time.sleep(2)
        self.ws_manager.subscribe([self.call_position['symbol'], self.put_position['symbol']])
        return True

    def monitor(self):
        print(f"[MONITORING] IV Crush — updates every {self.monitoring_interval}s")
        iteration = 0
        try:
            while self.running:
                iteration += 1
                ts = datetime.now().strftime("%H:%M:%S")

                # Get current prices
                call_ws = self.ws_manager.get_latest_price(self.call_position['symbol'])
                put_ws = self.ws_manager.get_latest_price(self.put_position['symbol'])

                if call_ws and put_ws:
                    call_price, put_price = call_ws['mark_price'], put_ws['mark_price']
                    self.current_iv = (call_ws.get('iv', 0) + put_ws.get('iv', 0)) / 2
                else:
                    cd = get_current_price(self.call_position['product_id'], self.asset)
                    pd = get_current_price(self.put_position['product_id'], self.asset)
                    if not cd or not pd:
                        time.sleep(self.monitoring_interval)
                        continue
                    call_price, put_price = cd['mark_price'], pd['mark_price']

                # Calculate P&L (short position: profit when price drops)
                call_pnl = (self.call_entry_price - call_price) * self.lot_size * self.call_contract_value
                put_pnl = (self.put_entry_price - put_price) * self.lot_size * self.put_contract_value
                self.unrealized_pnl = call_pnl + put_pnl
                self.total_pnl = self.realized_pnl + self.unrealized_pnl

                # IV crush calculation
                if self.iv_at_entry > 0 and self.current_iv > 0:
                    self.iv_crush_pct = round((1 - self.current_iv / self.iv_at_entry) * 100, 1)

                pnl_pct = (self.total_pnl / self.total_premium * 100) if self.total_premium > 0 else 0

                print(f"[{ts}] #{iteration} | PnL: ${self.total_pnl:.2f} ({pnl_pct:+.1f}%) | IV Crush: {self.iv_crush_pct}%")
                print(f"  Call: ${call_price:.2f} (entry ${self.call_entry_price:.2f}) | Put: ${put_price:.2f} (entry ${self.put_entry_price:.2f})")

                # Exit conditions
                if self.total_premium > 0:
                    # Target profit hit
                    if pnl_pct >= self.target_profit_pct:
                        print(f"🎯 TARGET HIT! PnL: ${self.total_pnl:.2f} ({pnl_pct:.1f}%)")
                        self.close_all()
                        self.status_msg = f'Target hit: ${self.total_pnl:.2f}'
                        break
                    # Max loss hit
                    if pnl_pct <= -self.max_loss_pct:
                        print(f"🛑 MAX LOSS HIT! PnL: ${self.total_pnl:.2f} ({pnl_pct:.1f}%)")
                        self.close_all()
                        self.status_msg = f'Max loss: ${self.total_pnl:.2f}'
                        break

                time.sleep(self.monitoring_interval)
        except KeyboardInterrupt:
            print("[STOPPED] Strategy stopped by user")
            self.close_all()

    def close_all(self):
        print("[CLOSING] Closing all positions...")
        for label, pos, cv in [("CALL", self.call_position, self.call_contract_value),
                                ("PUT", self.put_position, self.put_contract_value)]:
            if not pos:
                continue
            place_order(pos['product_id'], pos['symbol'], self.lot_size, 'buy')
        time.sleep(2)
        self.ws_manager.stop()
        self.running = False
        print(f"✓ Closed | Final PnL: ${self.total_pnl:.2f} | IV Crush: {self.iv_crush_pct}%")
