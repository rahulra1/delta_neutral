"""
Open Positions Tracker — tracks all open positions across strategies.
Auto-registers positions when orders are placed.
"""
import threading
from datetime import datetime
from api.pricing import get_current_price
from api.live_pnl import compute_leg_pnl
from config import get_contract_value


class Position:
    """A single open position."""
    __slots__ = ['product_id', 'symbol', 'type', 'strike', 'side', 'size',
                 'entry_price', 'current_mark', 'current_pnl', 'asset',
                 'source', 'strategy_sid', 'opened_at', '_user_id', '_track_id']

    def __init__(self, product_id, symbol, type='', strike='', side='sell',
                 size=0, entry_price=0, asset='BTC', source='', strategy_sid=''):
        self.product_id = product_id
        self.symbol = symbol
        self.type = type
        self.strike = strike
        self.side = side
        self.size = size
        self.entry_price = float(entry_price)
        self.current_mark = float(entry_price)
        self.current_pnl = 0.0
        self.asset = asset
        self.source = source
        self.strategy_sid = strategy_sid
        self.opened_at = datetime.now().isoformat()

    def update_price(self, mark):
        self.current_mark = float(mark)
        cv = get_contract_value(self.asset)
        self.current_pnl = round(compute_leg_pnl(self.entry_price, self.current_mark, self.size, self.side, cv), 2)

    def to_dict(self):
        return {
            'product_id': self.product_id, 'symbol': self.symbol,
            'type': self.type, 'strike': self.strike,
            'side': self.side, 'size': self.size,
            'entry_price': round(self.entry_price, 2),
            'current_mark': round(self.current_mark, 2),
            'current_pnl': self.current_pnl,
            'asset': self.asset, 'source': self.source,
            'strategy_sid': self.strategy_sid,
            'opened_at': self.opened_at,
        }


class PositionTracker:
    """Global tracker for all open positions."""

    def __init__(self):
        self._positions = []  # [Position, ...]
        self._lock = threading.Lock()
        self._id_counter = 0

    def open(self, user_id, product_id, symbol, **kwargs):
        pos = Position(product_id=product_id, symbol=symbol, **kwargs)
        pos._user_id = user_id
        with self._lock:
            self._id_counter += 1
            pos._track_id = self._id_counter
            self._positions.append(pos)
        return pos

    def close(self, user_id, product_id):
        with self._lock:
            for i, pos in enumerate(self._positions):
                if pos._user_id == user_id and pos.product_id == product_id:
                    return self._positions.pop(i)
        return None

    def get_user_positions(self, user_id):
        with self._lock:
            return [p for p in self._positions if p._user_id == user_id]

    def refresh_prices(self, user_id):
        positions = self.get_user_positions(user_id)
        for pos in positions:
            try:
                data = get_current_price(pos.product_id, pos.asset)
                if data and data.get('mark_price'):
                    pos.update_price(data['mark_price'])
            except Exception:
                pass
        return positions

    def get_total_pnl(self, user_id):
        return sum(p.current_pnl for p in self.get_user_positions(user_id))

    def to_list(self, user_id, refresh=False):
        if refresh:
            self.refresh_prices(user_id)
        return [p.to_dict() for p in self.get_user_positions(user_id)]


# Global singleton
position_tracker = PositionTracker()
