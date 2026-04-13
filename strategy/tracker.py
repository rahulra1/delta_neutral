"""
Unified Strategy Tracker — tracks any strategy (Delta Neutral, Option Chain, Strategy Builder)
with independent log threads and real-time P&L monitoring.
"""
import time
import threading
import uuid
from datetime import datetime
from api.pricing import get_current_price
from api.orders import place_order


class TrackedStrategy:
    """A single tracked strategy with its own log thread and P&L monitoring."""

    def __init__(self, sid=None, source='', name='', user_id=None, legs=None,
                 asset='BTC', lot_size=0.001, max_profit=0, max_loss=0,
                 profile_id=None, interval=10, details=None):
        self.sid = sid or str(uuid.uuid4())[:8]
        self.source = source          # 'AlgoX DN', 'Option Chain', 'Strategy Builder', 'Div+MSS'
        self.name = name
        self.user_id = user_id
        self.profile_id = profile_id
        self.asset = asset
        self.lot_size = lot_size
        self.max_profit = abs(max_profit) if max_profit else 0
        self.max_loss = abs(max_loss) if max_loss else 0
        self.interval = interval
        self.details = details or {}
        self.started_at = datetime.now().isoformat()

        # Legs: [{product_id, symbol, type, strike, side, size, entry_price}]
        self.legs = legs or []

        # State
        self.status = 'running'       # running, completed, closed, error
        self.current_pnl = 0
        self.exit_reason = None
        self.adjustment_count = 0
        self.running = False

        # Independent log
        self._lock = threading.Lock()
        self._logs = []
        self._monitor_thread = None

        # Callbacks
        self.on_complete = None       # fn(pnl, reason)

    def log(self, msg):
        ts = datetime.now().strftime('%H:%M:%S')
        entry = f"[{ts}] {msg}"
        with self._lock:
            self._logs.append(entry)
            if len(self._logs) > 500:
                self._logs = self._logs[-500:]
        print(f"[{self.sid}] {entry}")

    def get_logs(self, last_n=100):
        with self._lock:
            return list(self._logs[-last_n:])

    def start_monitoring(self):
        if not self.legs:
            self.log("⚠ No legs to monitor")
            return
        self.running = True
        self.status = 'running'
        self.log(f"👁 Monitoring started | {self.source} | {self.name}")
        self.log(f"   Legs: {len(self.legs)} | Asset: {self.asset} | Lot: {self.lot_size}")
        if self.max_profit:
            self.log(f"   Target: +${self.max_profit:.2f} | SL: -${self.max_loss:.2f}")
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def _monitor_loop(self):
        while self.running:
            time.sleep(self.interval)
            if not self.running:
                break
            try:
                pnl = 0
                leg_details = []
                for leg in self.legs:
                    data = get_current_price(leg['product_id'], self.asset)
                    if not data:
                        leg_details.append(f"{leg['symbol']}: no data")
                        continue
                    mark = data['mark_price']
                    d = 1 if leg['side'] == 'buy' else -1
                    leg_pnl = d * (mark - leg['entry_price']) * leg['size'] * self.lot_size
                    pnl += leg_pnl
                    leg['current_mark'] = mark
                    leg['current_pnl'] = round(leg_pnl, 2)
                    leg_details.append(f"{leg['symbol']}: {mark:.2f} ({'+' if leg_pnl >= 0 else ''}{leg_pnl:.2f})")

                self.current_pnl = round(pnl, 2)
                self.log(f"📊 PnL: ${pnl:.2f} | " + " | ".join(leg_details))

                # Check exit conditions
                if self.max_profit > 0 and pnl >= self.max_profit:
                    self.log(f"🎯 Target +${self.max_profit:.2f} reached!")
                    self._exit('target_hit')
                    return
                elif self.max_loss > 0 and pnl <= -self.max_loss:
                    self.log(f"🛑 SL -${self.max_loss:.2f} hit!")
                    self._exit('sl_hit')
                    return

            except Exception as e:
                self.log(f"⚠ Error: {e}")

    def _exit(self, reason):
        self.exit_reason = reason
        self.log(f"📝 Closing all legs — reason: {reason}")
        self._close_legs()
        self.running = False
        self.status = 'completed'
        self.log(f"✅ Strategy completed | PnL: ${self.current_pnl:.2f} | Reason: {reason}")
        if self.on_complete:
            self.on_complete(self.current_pnl, reason)

    def _close_legs(self):
        for leg in self.legs:
            close_side = 'sell' if leg['side'] == 'buy' else 'buy'
            self.log(f"   {close_side.upper()} {leg['symbol']} x {leg['size']}")
            try:
                place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
            except Exception as e:
                self.log(f"   ⚠ Failed to close {leg['symbol']}: {e}")

    def close(self):
        """Manual close."""
        if self.running:
            self.log("🛑 Manual close requested")
            self._exit('manual')
        else:
            self.status = 'closed'

    def get_status(self):
        with self._lock:
            return {
                'sid': self.sid,
                'source': self.source,
                'name': self.name,
                'user_id': self.user_id,
                'status': self.status,
                'started_at': self.started_at,
                'pnl': self.current_pnl,
                'exit_reason': self.exit_reason,
                'adjustment_count': self.adjustment_count,
                'running': self.running,
                'max_profit': self.max_profit,
                'max_loss': self.max_loss,
                'details': self.details,
                'legs': [{
                    'symbol': l.get('symbol', ''),
                    'product_id': l.get('product_id'),
                    'type': l.get('type', ''),
                    'strike': l.get('strike', ''),
                    'side': l.get('side', ''),
                    'size': l.get('size', 0),
                    'entry_price': round(l.get('entry_price', 0), 2),
                    'current_mark': round(l.get('current_mark', l.get('entry_price', 0)), 2),
                    'current_pnl': round(l.get('current_pnl', 0), 2),
                } for l in self.legs],
                'logs': list(self._logs[-100:]),
            }


class StrategyRegistry:
    """Global registry of all tracked strategies with monitoring."""

    def __init__(self):
        self._strategies = {}  # {sid: TrackedStrategy}
        self._lock = threading.Lock()

    def register(self, strategy):
        with self._lock:
            self._strategies[strategy.sid] = strategy
        return strategy.sid

    def get(self, sid):
        return self._strategies.get(sid)

    def get_user_strategies(self, user_id):
        return [s for s in self._strategies.values() if s.user_id == user_id]

    def get_running(self, user_id=None):
        strats = self._strategies.values()
        if user_id:
            strats = [s for s in strats if s.user_id == user_id]
        return [s for s in strats if s.running]

    def close(self, sid):
        s = self._strategies.get(sid)
        if s:
            s.close()
            return True
        return False

    def close_all(self, user_id):
        count = 0
        for s in self.get_running(user_id):
            s.close()
            count += 1
        return count

    def all_statuses(self, user_id):
        return [s.get_status() for s in self._strategies.values() if s.user_id == user_id]


# Global singleton
registry = StrategyRegistry()
