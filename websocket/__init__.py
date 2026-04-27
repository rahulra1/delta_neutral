import json
import time
import threading
import websocket
import config


class WebSocketManager:
    def __init__(self, strategy):
        self.strategy = strategy
        self.ws = None
        self.ws_thread = None
        self.running = False
        self.subscribed_symbols = []
        self.latest_prices = {}
        self.reconnect_delay = 5

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
                print(f"✓ WebSocket subscribed to: {data.get('channels', [])}")
        except Exception as e:
            print(f"WebSocket message error: {e}")

    def on_error(self, ws, error):
        print(f"WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print(f"WebSocket closed: {close_status_code} - {close_msg}")
        if self.running:
            print(f"Reconnecting in {self.reconnect_delay} seconds...")
            time.sleep(self.reconnect_delay)
            self.connect()

    def on_open(self, ws):
        print("✓ WebSocket connected")
        if self.subscribed_symbols:
            self.subscribe(self.subscribed_symbols)

    def connect(self):
        try:
            self.ws = websocket.WebSocketApp(
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
            print(f"WebSocket connection error: {e}")

    def subscribe(self, symbols):
        if not self.ws or not self.ws.sock or not self.ws.sock.connected:
            print("WebSocket not connected, cannot subscribe")
            return
        self.subscribed_symbols = symbols
        for symbol in symbols:
            try:
                self.ws.send(json.dumps({
                    "type": "subscribe",
                    "payload": {"channels": [{"name": "v2/ticker", "symbols": [symbol]}]}
                }))
                print(f"Subscribing to {symbol}...")
            except Exception as e:
                print(f"Error subscribing to {symbol}: {e}")

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
                print(f"Error unsubscribing from {symbol}: {e}")
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
