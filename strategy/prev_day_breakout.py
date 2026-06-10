"""Previous Day Breakout + Retest auto-trader.
Monitors for signals on a loop and places futures orders on Delta Exchange."""

import time
import threading
import logging
from datetime import datetime
from api.chart import get_candles, calc_prev_day_breakout_retest
from api.orders import place_order

logger = logging.getLogger(__name__)

# Delta Exchange perpetual futures symbols
FUTURES_PRODUCTS = {
    'BTC': 'BTCUSD',
    'ETH': 'ETHUSD',
}


class PrevDayBreakoutTrader:
    """Scans for prev day breakout+retest signals and auto-places futures orders."""

    def __init__(self, asset='BTC', timeframe='15m', lots=1, rr=3, scan_interval=60,
                 max_trades_per_day=10, profile_id=None, api_key='', api_secret='', broker=None):
        self.asset = asset
        self.timeframe = timeframe
        self.lots = lots
        self.rr = rr
        self.scan_interval = scan_interval
        self.max_trades_per_day = max_trades_per_day
        self.profile_id = profile_id
        self._api_key = api_key
        self._api_secret = api_secret
        self._broker = broker

        self.running = False
        self.trades_today = 0
        self.last_signal_time = 0
        self.trade_log = []
        self.legs = []
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
        logger.info(f"[PDB] Started | {self.asset} {self.timeframe} | Lots: {self.lots} | R:R 1:{self.rr} | Scan: {self.scan_interval}s")

    def stop(self):
        self.running = False
        if self.sid:
            self._persist(stopped=True)
        logger.info(f"[PDB] Stopped | Trades placed: {len(self.trade_log)}")

    def _run_loop(self):
        from config import set_thread_credentials
        set_thread_credentials(self._api_key, self._api_secret, self._broker)
        while self.running:
            try:
                today = datetime.utcnow().date()
                if today != self._today:
                    self._today = today
                    self.trades_today = 0
                self._check_tp_sl()
                self._scan_and_trade()
                self._scan_count += 1
                if self._scan_count % 10 == 0 and self.sid:
                    self._persist()
            except Exception as e:
                logger.error(f"[PDB] Scan error: {e}")
            time.sleep(self.scan_interval)
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
            else:
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
                logger.info(f"[PDB] {'🎯' if hit == 'TP' else '🛑'} {hit} HIT: {leg['side'].upper()} {symbol} | Entry: {leg['entry_price']} → Exit: {price:.2f} | PnL: ${pnl:+.4f}")
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
                               details={'asset': self.asset, 'timeframe': self.timeframe,
                                        'lots': self.lots, 'rr': self.rr,
                                        'scan_interval': self.scan_interval,
                                        'max_trades_per_day': self.max_trades_per_day,
                                        'last_signal_time': self.last_signal_time})
        except Exception:
            pass

    def _scan_and_trade(self):
        if self.trades_today >= self.max_trades_per_day:
            return

        candles = get_candles(self.asset, self.timeframe)
        if not candles:
            return

        result = calc_prev_day_breakout_retest(candles, min_rr=self.rr)
        signals = result.get('signals', [])
        if not signals:
            return

        latest = signals[-1]
        if latest['time'] <= self.last_signal_time:
            return

        now = time.time()
        candle_seconds = {'15m': 900, '1h': 3600, '5m': 300, '1d': 86400}
        max_age = candle_seconds.get(self.timeframe, 900) * 2
        if now - latest['time'] > max_age:
            return

        self.last_signal_time = latest['time']
        self._place_trade(latest)

    def _place_trade(self, signal):
        symbol = FUTURES_PRODUCTS.get(self.asset)
        if not symbol:
            logger.warning(f"[PDB] No futures product for {self.asset}")
            return

        side = 'buy' if signal['type'] == 'buy' else 'sell'
        result = place_order(None, symbol, self.lots, side)

        trade = {
            'time': datetime.now().strftime('%H:%M:%S'),
            'side': side,
            'price': signal['price'],
            'sl': signal['sl'],
            'tp': signal['tp1'],
            'success': result is not None,
        }
        self.trade_log.append(trade)

        if result:
            self.trades_today += 1
            self.legs.append({
                'symbol': symbol,
                'side': side,
                'size': self.lots,
                'entry_price': signal['price'],
                'sl': signal['sl'],
                'tp': signal['tp1'],
                'time': datetime.now().strftime('%H:%M:%S'),
            })
            logger.info(f"[PDB] ✓ {side.upper()} {self.lots} {symbol} @ ~{signal['price']} | SL: {signal['sl']} | TP: {signal['tp1']}")
            if self.sid:
                self._persist()
        else:
            logger.warning(f"[PDB] ✗ Order failed: {side.upper()} {symbol}")

    @property
    def status(self):
        return {
            'running': self.running,
            'asset': self.asset,
            'timeframe': self.timeframe,
            'lots': self.lots,
            'rr': self.rr,
            'trades_today': self.trades_today,
            'max_trades_per_day': self.max_trades_per_day,
            'total_trades': len(self.trade_log),
            'trade_log': self.trade_log[-10:],
        }
