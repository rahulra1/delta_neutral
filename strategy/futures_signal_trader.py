"""Generic Futures Signal Auto-Trader.
Scans any chart signal strategy and places futures orders on Delta Exchange."""

import time
import threading
import logging
from datetime import datetime
from api.chart import get_candles, INDICATOR_FNS
from api.orders import place_order

logger = logging.getLogger(__name__)

FUTURES_PRODUCTS = {
    'BTC': 'BTCUSD',
    'ETH': 'ETHUSD',
}


class FuturesSignalTrader:
    """Scans for signals from any indicator strategy and auto-places futures orders."""

    def __init__(self, signal_key, asset='BTC', timeframe='15m', lots=1,
                 scan_interval=30, max_trades_per_day=10,
                 api_key='', api_secret='', broker=None, profile_id=None):
        self.signal_key = signal_key
        self.asset = asset
        self.timeframe = timeframe
        self.lots = lots
        self.scan_interval = scan_interval
        self.max_trades_per_day = max_trades_per_day
        self._api_key = api_key
        self._api_secret = api_secret
        self._broker = broker
        self.profile_id = profile_id

        self.running = False
        self.trades_today = 0
        self.last_signal_time = 0
        self.trade_log = []
        self.legs = []  # open legs [{symbol, side, size, entry_price, time}]
        self.sid = None
        self._thread = None
        self._today = None
        self._scan_count = 0

    def start(self):
        if self.running:
            return
        self.running = True
        self.trades_today = 0
        self._today = datetime.utcnow().date()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        print(f"[FST] ✓ Started | {self.signal_key} {self.asset} {self.timeframe} | Lots: {self.lots} | Scan: {self.scan_interval}s")

    def stop(self):
        self.running = False
        print(f"[FST] ⏹ Stopped {self.signal_key} | Trades: {len(self.trade_log)}")

    def _run_loop(self):
        from config import set_thread_credentials
        set_thread_credentials(self._api_key, self._api_secret, self._broker)
        while self.running:
            try:
                # Reset daily counter
                today = datetime.utcnow().date()
                if today != self._today:
                    self._today = today
                    self.trades_today = 0
                self._check_tp_sl()
                self._scan_and_trade()
                # Persist to DB every 10 scans
                if self._scan_count % 10 == 0 and self.sid:
                    self._persist()
            except Exception as e:
                print(f"[FST] ❌ {self.signal_key} error: {e}")
            time.sleep(self.scan_interval)
        # Final persist on stop
        if self.sid:
            self._persist(stopped=True)

    def _check_tp_sl(self):
        """Close legs that hit TP or SL."""
        if not self.legs:
            return
        from api.pricing import get_futures_price
        symbol = FUTURES_PRODUCTS.get(self.asset)
        if not symbol:
            return
        data = get_futures_price(symbol)
        if not data:
            return
        price = data['mark_price']
        closed = []
        for i, leg in enumerate(self.legs):
            sl = leg.get('sl')
            tp = leg.get('tp')
            if not sl and not tp:
                continue
            hit = None
            if leg['side'] == 'buy':
                if tp and price >= float(tp):
                    hit = 'TP'
                elif sl and price <= float(sl):
                    hit = 'SL'
            else:  # sell
                if tp and price <= float(tp):
                    hit = 'TP'
                elif sl and price >= float(sl):
                    hit = 'SL'
            if hit:
                close_side = 'sell' if leg['side'] == 'buy' else 'buy'
                place_order(None, symbol, leg['size'], close_side)
                from config import get_contract_value
                cv = get_contract_value(self.asset)
                pnl = ((price - leg['entry_price']) if leg['side'] == 'buy' else (leg['entry_price'] - price)) * leg['size'] * cv
                print(f"[FST] {'🎯' if hit == 'TP' else '🛑'} {hit} HIT: {leg['side'].upper()} {symbol} | Entry: {leg['entry_price']} → Exit: {price:.2f} | PnL: ${pnl:+.4f}")
                closed.append(i)
        for i in reversed(closed):
            self.legs.pop(i)
        if closed and self.sid:
            self._persist()

    def _persist(self, stopped=False):
        try:
            from models import update_strategy_db
            logs = [f"[{t['time']}] {t['side'].upper()} @ {t['price']} | SL: {t['sl']} | TP: {t['tp']} | {'✓' if t['success'] else '✗'}" for t in self.trade_log]
            status = 'completed' if stopped else 'running'
            update_strategy_db(self.sid, status=status, logs=logs, legs=self.legs,
                               details={'signal_key': self.signal_key, 'asset': self.asset,
                                        'timeframe': self.timeframe, 'lots': self.lots,
                                        'scan_interval': self.scan_interval,
                                        'max_trades_per_day': self.max_trades_per_day,
                                        'last_signal_time': self.last_signal_time})
        except Exception:
            pass

    def _scan_and_trade(self):
        self._scan_count += 1

        if self.trades_today >= self.max_trades_per_day:
            print(f"[FST] {self.signal_key} {self.asset} {self.timeframe} | Max trades reached ({self.trades_today}/{self.max_trades_per_day})")
            return

        fn = INDICATOR_FNS.get(self.signal_key)
        if not fn:
            print(f"[FST] ❌ Unknown signal_key: {self.signal_key}")
            return

        candles = get_candles(self.asset, self.timeframe)
        if not candles:
            print(f"[FST] {self.signal_key} {self.asset} {self.timeframe} | ⚠ No candle data")
            return

        result = fn(candles)
        signals = result.get('signals', [])

        latest = signals[-1] if signals else None
        is_new = latest and latest['time'] > self.last_signal_time

        # Check if signal is recent enough
        recent = False
        if is_new and latest:
            now = time.time()
            candle_seconds = {'5m': 300, '15m': 900, '1h': 3600, '1d': 86400}
            max_age = candle_seconds.get(self.timeframe, 900) * 2
            recent = (now - latest['time']) <= max_age

        print(f"[FST] {self.signal_key} {self.asset} {self.timeframe} | Scan #{self._scan_count} | Signals: {len(signals)} | New: {'YES ✓' if (is_new and recent) else 'no'} | Trades: {self.trades_today}/{self.max_trades_per_day}")

        if not is_new or not recent:
            return

        self.last_signal_time = latest['time']
        self._place_trade(latest)

    def _place_trade(self, signal):
        symbol = FUTURES_PRODUCTS.get(self.asset)
        if not symbol:
            print(f"[FST] ❌ No futures product for {self.asset}")
            return

        side = 'buy' if signal['type'] == 'buy' else 'sell'
        result = place_order(None, symbol, self.lots, side)

        trade = {
            'time': datetime.now().strftime('%H:%M:%S'),
            'signal_key': self.signal_key,
            'side': side,
            'price': signal.get('price'),
            'sl': signal.get('sl'),
            'tp': signal.get('tp1'),
            'success': result is not None,
        }
        self.trade_log.append(trade)

        if result:
            self.trades_today += 1
            entry_price = signal.get('price', 0)
            self.legs.append({
                'symbol': symbol,
                'side': side,
                'size': self.lots,
                'entry_price': entry_price,
                'sl': signal.get('sl'),
                'tp': signal.get('tp1'),
                'time': datetime.now().strftime('%H:%M:%S'),
            })
            print(f"[FST] 🟢 ORDER PLACED: {self.signal_key} {side.upper()} {self.lots} {symbol} @ ~{signal.get('price')} | SL: {signal.get('sl')} | TP: {signal.get('tp1')}")
            if self.sid:
                self._persist()
        else:
            print(f"[FST] ❌ ORDER FAILED: {self.signal_key} {side.upper()} {symbol}")

    @property
    def status(self):
        return {
            'running': self.running,
            'signal_key': self.signal_key,
            'asset': self.asset,
            'timeframe': self.timeframe,
            'lots': self.lots,
            'trades_today': self.trades_today,
            'max_trades_per_day': self.max_trades_per_day,
            'total_trades': len(self.trade_log),
            'trade_log': self.trade_log[-10:],
        }
