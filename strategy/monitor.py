import time
import threading
from api.pricing import get_current_price
from api.orders import place_order


class StrategyMonitor:
    """Monitors a multi-leg option strategy and closes at max profit or max loss."""

    def __init__(self, legs, max_profit, max_loss, asset='BTC', lot_size=0.001, interval=10, on_complete=None):
        """
        legs: list of {product_id, symbol, type, strike, side, size, entry_price}
        max_profit / max_loss: absolute dollar thresholds (positive values)
        """
        self.legs = legs
        self.max_profit = abs(max_profit)
        self.max_loss = abs(max_loss)
        self.asset = asset
        self.lot_size = lot_size
        self.interval = interval
        self.on_complete = on_complete

        self.running = False
        self.current_pnl = 0
        self.exit_reason = None
        self._lock = threading.Lock()
        self.log = []
        self.pnl_history = []         # [(iso_ts, pnl), ...]
        self._snap_counter = 0
        self.user_id = None
        self.sid = None

    def _log(self, msg):
        with self._lock:
            self.log.append(msg)
            if len(self.log) > 200:
                self.log = self.log[-200:]
        print(msg)

    def start(self):
        self.running = True
        self._log(f"👁 Monitoring started | Max Profit: ${self.max_profit:.2f} | Max Loss: -${self.max_loss:.2f}")
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    def _monitor_loop(self):
        while self.running:
            time.sleep(self.interval)
            if not self.running:
                break
            try:
                pnl = 0
                details = []
                all_legs_ok = True
                for leg in self.legs:
                    pid = leg.get('product_id')
                    if not pid:
                        all_legs_ok = False
                        details.append(f"{leg.get('symbol', '?')}: no product_id")
                        continue
                    data = get_current_price(pid, self.asset)
                    if not data:
                        all_legs_ok = False
                        details.append(f"{leg.get('symbol', '?')}: no data")
                        continue
                    mark = data['mark_price']
                    dir = 1 if leg.get('side') == 'buy' else -1
                    leg_pnl = dir * (mark - float(leg.get('entry_price', 0))) * int(leg.get('size', 0)) * self.lot_size
                    pnl += leg_pnl
                    details.append(f"{leg['symbol']}: ${mark:.2f} (pnl ${leg_pnl:.2f})")

                self.current_pnl = pnl
                from datetime import datetime as _dt
                now_iso = _dt.now().isoformat()
                with self._lock:
                    self.pnl_history.append((now_iso, round(pnl, 2)))
                    if len(self.pnl_history) > 2000:
                        self.pnl_history = self.pnl_history[-2000:]
                self._log(f"📊 PnL: ${pnl:.2f} | " + " | ".join(details))
                # Persist snapshot every 6 ticks
                self._snap_counter += 1
                if self._snap_counter % 6 == 0 and self.user_id and self.sid:
                    try:
                        from models import save_pnl_snapshot
                        save_pnl_snapshot(self.user_id, self.sid, round(pnl, 2))
                    except Exception:
                        pass

                # Skip exit checks if any leg had no data — P&L is incomplete
                if not all_legs_ok:
                    self._log("⚠ Skipping exit check — incomplete price data")
                    continue

                if pnl >= self.max_profit:
                    self.exit_reason = 'max_profit'
                    self._log(f"🎯 Max profit ${self.max_profit:.2f} reached! Closing all legs...")
                    self._close_all()
                    return
                elif pnl <= -self.max_loss:
                    self.exit_reason = 'max_loss'
                    self._log(f"🛑 Max loss -${self.max_loss:.2f} hit! Closing all legs...")
                    self._close_all()
                    return

            except Exception as e:
                self._log(f"⚠ Monitor error: {e}")

    def _close_all(self):
        for leg in self.legs:
            close_side = 'sell' if leg['side'] == 'buy' else 'buy'
            self._log(f"📝 {close_side.upper()} {leg['symbol']} x {leg['size']}")
            place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
        self.running = False
        self._log(f"✅ All legs closed. Final PnL: ${self.current_pnl:.2f} ({self.exit_reason})")
        if self.on_complete:
            self.on_complete(self.current_pnl, self.exit_reason)

    def stop(self):
        """Manual stop — close everything."""
        if self.running:
            self._log("🛑 Manual stop — closing all legs...")
            self._close_all()
        self.running = False

    def get_status(self):
        with self._lock:
            return {
                'running': self.running,
                'current_pnl': round(self.current_pnl, 2),
                'max_profit': round(self.max_profit, 2),
                'max_loss': round(self.max_loss, 2),
                'exit_reason': self.exit_reason,
                'legs': [{
                    'symbol': l['symbol'], 'side': l['side'], 'size': l['size'],
                    'entry_price': round(l['entry_price'], 2),
                    'type': l.get('type', ''), 'strike': l.get('strike', ''),
                } for l in self.legs],
                'logs': list(self.log),
                'pnl_history': list(self.pnl_history[-500:]),
            }
