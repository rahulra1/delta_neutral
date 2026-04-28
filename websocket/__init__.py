import json
import time
import weakref
import threading
import logging
import websocket as _ws_lib
import config

logger = logging.getLogger(__name__)


class WebSocketManager:
    def __init__(self, strategy):
        self._strategy_ref = weakref.ref(strategy)
        self.ws = None
        self.ws_thread = None
        self.running = False
        self._reconnecting = False
        self.subscribed_symbols = []
        self.latest_prices = {}
        self.reconnect_delay = 5

    @property
    def strategy(self):
        return self._strategy_ref()

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get('type') == 'ticker':
                symbol = data.get('symbol')
                mark_price = float(data.get('mark_price', 0))
                delta = float(data.get('greeks', {}).get('delta', 0))
                self.latest_prices[symbol] = {
                    'mark_price': mark_price, 'delta': delta, 'timestamp': time.time()
                }
                if self.strategy:
                    self.strategy.on_price_update(symbol, mark_price, delta)
            elif data.get('type') == 'subscriptions':
                logger.info(f"✓ WebSocket subscribed to: {data.get('channels', [])}")
        except Exception as e:
            logger.info(f"WebSocket message error: {e}")

    def on_error(self, ws, error):
        logger.error(f"WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        logger.info(f"WebSocket closed: {close_status_code} - {close_msg}")
        if self.running and not self._reconnecting:
            self._reconnecting = True
            logger.info(f"Reconnecting in {self.reconnect_delay} seconds...")
            threading.Thread(target=self._reconnect, daemon=True).start()

    def _reconnect(self):
        try:
            time.sleep(self.reconnect_delay)
            if self.running:
                self.connect()
        finally:
            self._reconnecting = False

    def on_open(self, ws):
        logger.info("✓ WebSocket connected")
        if self.subscribed_symbols:
            self.subscribe(self.subscribed_symbols)

    def connect(self):
        try:
            # Close existing connection if any
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass
            if self.ws_thread and self.ws_thread.is_alive():
                self.ws_thread.join(timeout=3)
            self.ws = _ws_lib.WebSocketApp(
                config.WS_URL,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
                on_open=self.on_open
            )
            self.ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
            self.ws_thread.start()
            time.sleep(2)
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")

    def subscribe(self, symbols):
        if not self.ws or not self.ws.sock or not self.ws.sock.connected:
            logger.info("WebSocket not connected, cannot subscribe")
            return
        self.subscribed_symbols = symbols
        for symbol in symbols:
            try:
                self.ws.send(json.dumps({
                    "type": "subscribe",
                    "payload": {"channels": [{"name": "v2/ticker", "symbols": [symbol]}]}
                }))
                logger.info(f"Subscribing to {symbol}...")
            except Exception as e:
                logger.warning(f"Error subscribing to {symbol}: {e}")

    def unsubscribe(self, symbols):
        if not self.ws or not self.ws.sock or not self.ws.sock.connected:
            return
        for symbol in symbols:
            try:
                self.ws.send(json.dumps({
                    "type": "unsubscribe",
                    "payload": {"channels": [{"name": "v2/ticker", "symbols": [symbol]}]}
                }))
            except Exception as e:
                logger.warning(f"Error unsubscribing from {symbol}: {e}")
        self.subscribed_symbols = [s for s in self.subscribed_symbols if s not in symbols]

    def get_latest_price(self, symbol, max_age=30):
        data = self.latest_prices.get(symbol)
        if data and (time.time() - data.get('timestamp', 0)) > max_age:
            return None  # stale data
        return data

    def start(self):
        self.running = True
        self.connect()

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()
