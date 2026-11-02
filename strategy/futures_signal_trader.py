"""Generic Futures Signal Auto-Trader.
Scans any chart signal strategy and places futures orders on Delta Exchange.

State that survives a restart (live_strategies row, written by _persist):
  legs     open positions (fill price, lots, SL/TP, opened time)
  details  trade_log (every open AND close, with exit price / reason / P&L), realized
           cumulative P&L, fees, wins/losses, last_signal_time, pending signal, trades_today
  pnl      realized + open P&L at the last mark
"""

import time
import threading
import logging
from datetime import datetime, timezone
from api.chart import get_candles, INDICATOR_FNS
from api.orders import place_order

logger = logging.getLogger(__name__)

FUTURES_PRODUCTS = {
    'BTC': 'BTCUSD',
    'ETH': 'ETHUSD',
}

# Strategies validated with ONE position at a time (backtest/trendline_optimize.py):
# while a leg is open, new signals are skipped instead of stacking more legs.
ONE_POSITION_KEYS = {'ema_trendline', 'candle_patterns'}

TAKER_FEE = 0.0005 * 1.18          # Delta taker 0.05% + 18% GST, per side (estimate)
CANDLE_SECONDS = {'5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _fill_price(result, fallback):
    try:
        px = float((result or {}).get('average_fill_price') or 0)
    except (TypeError, ValueError):
        px = 0
    return px if px > 0 else float(fallback or 0)


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
        self._pending_signal = None
        self.trade_log = []          # opens and closes, newest last
        self.legs = []               # open legs
        self.cumulative_pnl = 0.0    # realized, net of estimated fees
        self.total_fees = 0.0
        self.wins = 0
        self.losses = 0
        self.last_mark = None        # last mark price seen by the trader thread (live account's venue)
        self.last_mark_ts = 0
        self.sid = None
        self._thread = None
        self._today = None
        self._scan_count = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self.running:
            return
        self.running = True
        self._today = datetime.utcnow().date()
        self._restore_state()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        print(f"[FST] ✓ Started | {self.signal_key} {self.asset} {self.timeframe} | Lots: {self.lots} | "
              f"Scan: {self.scan_interval}s | open legs {len(self.legs)} | trades {len(self.trade_log)} | "
              f"realized ${self.cumulative_pnl:+.2f}")

    def _restore_state(self):
        """Restore open legs, trade history, realized P&L and signal cooldown from the DB row."""
        if not self.sid:
            return
        try:
            from models import get_db
            import json
            conn = get_db()
            row = conn.execute('SELECT details, legs FROM live_strategies WHERE sid=?', (self.sid,)).fetchone()
            conn.close()
            if not row:
                return
            details = json.loads(row['details'] or '{}')
            if isinstance(details, str):
                details = json.loads(details)
            legs = json.loads(row['legs'] or '[]')
            if isinstance(legs, str):
                legs = json.loads(legs)
            self.last_signal_time = details.get('last_signal_time', 0) or 0
            self._pending_signal = details.get('pending_signal')
            if details.get('trades_today_date') == str(self._today):
                self.trades_today = details.get('trades_today', 0) or 0
            self.trade_log = details.get('trade_log') or self.trade_log
            self.cumulative_pnl = float(details.get('cumulative_pnl') or 0)
            self.total_fees = float(details.get('total_fees') or 0)
            self.wins = int(details.get('wins') or 0)
            self.losses = int(details.get('losses') or 0)
            self.last_mark = details.get('last_mark') or self.last_mark
            self.legs = [l for l in legs if isinstance(l, dict) and l.get('side') and l.get('size')]
            if self._pending_signal:
                print(f"[FST] ♻ Restored pending {self._pending_signal['type'].upper()} signal @ {self._pending_signal.get('price')}")
            if self.legs or self.trade_log:
                print(f"[FST] ♻ Restored {len(self.legs)} open leg(s), {len(self.trade_log)} trade-log entries, "
                      f"realized ${self.cumulative_pnl:+.2f}")
        except Exception as e:
            logger.warning(f"[FST] restore failed for {self.sid}: {e}")

    def stop(self):
        self.running = False
        print(f"[FST] ⏹ Stopped {self.signal_key} | Trades: {self.closed_count} closed, {len(self.legs)} open | "
              f"realized ${self.cumulative_pnl:+.2f}")
        if self.legs:
            print(f"[FST] ⚠ {len(self.legs)} position(s) are still OPEN on the exchange and are no longer managed "
                  f"(stop does not close them).")
        if self.sid:
            self._persist(stopped=True)

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
                if self._scan_count % 10 == 0 and self.sid:
                    self._persist()
            except Exception as e:
                print(f"[FST] ❌ {self.signal_key} error: {e}")
            time.sleep(self.scan_interval)
        if self.sid:
            self._persist(stopped=True)

    # ------------------------------------------------------------------ P&L
    def _cv(self):
        from config import get_contract_value
        return get_contract_value(self.asset)

    def _leg_pnl(self, leg, mark):
        if not mark:
            return 0.0
        sign = 1 if leg['side'] == 'buy' else -1
        return sign * (mark - float(leg['entry_price'])) * leg['size'] * self._cv()

    @property
    def open_pnl(self):
        return sum(self._leg_pnl(l, self.last_mark) for l in self.legs)

    @property
    def pnl(self):
        """Realized (net of est. fees) + open P&L at the last mark the trader saw."""
        return self.cumulative_pnl + self.open_pnl

    @property
    def closed_count(self):
        return self.wins + self.losses

    def legs_view(self):
        """Open legs with mark and P&L for dashboards (no network call, uses the trader's last mark)."""
        out = []
        for l in self.legs:
            out.append({'symbol': l['symbol'], 'side': l['side'], 'size': l['size'],
                        'type': 'futures', 'entry_price': l['entry_price'],
                        'current_mark': round(self.last_mark or 0, 2),
                        'current_pnl': round(self._leg_pnl(l, self.last_mark), 2),
                        'sl': l.get('sl'), 'tp': l.get('tp'), 'opened_at': l.get('opened_at') or l.get('time')})
        return out

    def _refresh_mark(self):
        from api.pricing import get_futures_price
        symbol = FUTURES_PRODUCTS.get(self.asset)
        data = get_futures_price(symbol) if symbol else None
        if data and data.get('mark_price'):
            self.last_mark = float(data['mark_price'])
            self.last_mark_ts = time.time()
        return self.last_mark if data else None

    # ------------------------------------------------------------------ exits
    def _check_tp_sl(self):
        """Close legs that hit TP or SL (on the Delta mark price)."""
        price = self._refresh_mark()
        if not self.legs or not price:
            return
        symbol = FUTURES_PRODUCTS.get(self.asset)
        changed = False
        for leg in list(self.legs):
            sl, tp = leg.get('sl'), leg.get('tp')
            if not sl and not tp:
                continue
            hit = None
            if leg['side'] == 'buy':
                if sl and price <= float(sl):
                    hit = 'SL'
                elif tp and price >= float(tp):
                    hit = 'TP'
            else:
                if sl and price >= float(sl):
                    hit = 'SL'
                elif tp and price <= float(tp):
                    hit = 'TP'
            if hit and self._close_leg(leg, symbol, price, hit):
                changed = True
        if changed and self.sid:
            self._persist()

    def _close_leg(self, leg, symbol, price, reason):
        close_side = 'sell' if leg['side'] == 'buy' else 'buy'
        result = place_order(None, symbol, leg['size'], close_side)
        if not result:
            print(f"[FST] ✗ CLOSE {leg['side'].upper()} {symbol} x{leg['size']} ({reason}) order FAILED — position kept, "
                  f"retrying next scan")
            return False
        exit_px = _fill_price(result, price)
        cv = self._cv()
        sign = 1 if leg['side'] == 'buy' else -1
        gross = sign * (exit_px - float(leg['entry_price'])) * leg['size'] * cv
        fees = (float(leg['entry_price']) + exit_px) * leg['size'] * cv * TAKER_FEE
        net = gross - fees
        with self._lock:
            if leg in self.legs:
                self.legs.remove(leg)
            self.cumulative_pnl += net
            self.total_fees += fees
            self.wins += net > 0
            self.losses += net <= 0
            self.trade_log.append({
                'action': 'CLOSE', 'time': datetime.now().strftime('%H:%M:%S'), 'ts': _now_iso(),
                'signal_key': self.signal_key, 'side': close_side, 'position_side': leg['side'],
                'price': round(exit_px, 2), 'entry_price': leg['entry_price'], 'size': leg['size'],
                'sl': leg.get('sl'), 'tp': leg.get('tp'), 'reason': reason, 'success': True,
                'gross': round(gross, 4), 'fees': round(fees, 4), 'pnl': round(net, 4),
                'opened_at': leg.get('opened_at') or leg.get('time'),
            })
        print(f"[FST] {'🎯' if reason == 'TP' else '🛑'} {reason} HIT: {leg['side'].upper()} {symbol} | "
              f"Entry: {leg['entry_price']} → Exit: {exit_px:.2f} | gross ${gross:+.4f} - fees ${fees:.4f} = "
              f"${net:+.4f} | realized ${self.cumulative_pnl:+.2f}")
        return True

    # ------------------------------------------------------------------ persistence
    def _persist(self, stopped=False):
        try:
            from models import update_strategy_db
            logs = [self.format_log(t) for t in self.trade_log[-200:]]
            status = 'completed' if stopped else 'running'
            update_strategy_db(self.sid, status=status, logs=logs, legs=self.legs, pnl=round(self.pnl, 4),
                               details={'signal_key': self.signal_key, 'asset': self.asset,
                                        'timeframe': self.timeframe, 'lots': self.lots,
                                        'scan_interval': self.scan_interval,
                                        'max_trades_per_day': self.max_trades_per_day,
                                        'broker': self._broker, 'profile_id': self.profile_id,
                                        'last_signal_time': self.last_signal_time,
                                        'pending_signal': self._pending_signal,
                                        'trades_today': self.trades_today,
                                        'trades_today_date': str(self._today),
                                        'trade_log': self.trade_log[-500:],
                                        'cumulative_pnl': self.cumulative_pnl, 'total_fees': self.total_fees,
                                        'wins': self.wins, 'losses': self.losses, 'last_mark': self.last_mark})
        except Exception as e:
            logger.warning(f"[FST] persist failed for {self.sid}: {e}")

    @staticmethod
    def format_log(t):
        if t.get('action') == 'CLOSE':
            return (f"[{t.get('ts', t['time'])}] CLOSE {t.get('position_side', '').upper()} @ {t['price']} | "
                    f"{t.get('reason')} | entry {t.get('entry_price')} | P&L ${t.get('pnl', 0):+.2f}")
        return (f"[{t.get('ts', t['time'])}] {t['side'].upper()} @ {t['price']} | SL: {t['sl']} | TP: {t['tp']} | "
                f"{'✓' if t['success'] else '✗'}")

    # ------------------------------------------------------------------ entries
    def _scan_and_trade(self):
        self._scan_count += 1

        if self.trades_today >= self.max_trades_per_day:
            print(f"[FST] {self.signal_key} {self.asset} {self.timeframe} | Max trades reached ({self.trades_today}/{self.max_trades_per_day})")
            return

        # Check pending signal price trigger (sma_vol_breakout only)
        if self._pending_signal:
            price = self._refresh_mark()
            if price:
                entry = self._pending_signal.get('price', 0)
                sl = self._pending_signal.get('sl', 0)
                # Invalidate if price hit SL before entry (like backtest: SL checked first)
                if self._pending_signal['type'] == 'buy' and price <= sl:
                    print(f"[FST] {self.signal_key} | Pending BUY invalidated (price hit SL)")
                    self._pending_signal = None
                    self._persist()
                    return
                elif self._pending_signal['type'] == 'sell' and price >= sl:
                    print(f"[FST] {self.signal_key} | Pending SELL invalidated (price hit SL)")
                    self._pending_signal = None
                    self._persist()
                    return
                triggered = ((self._pending_signal['type'] == 'buy' and price >= entry) or
                             (self._pending_signal['type'] == 'sell' and price <= entry))
                if triggered:
                    self.last_signal_time = self._pending_signal['time']
                    print(f"[FST] {self.signal_key} | ✓ Price triggered @ {price:.2f} (entry: {entry})")
                    self._place_trade(self._pending_signal)
                    self._pending_signal = None
                    return
            # Expire pending signal after 2x candle duration
            max_wait = CANDLE_SECONDS.get(self.timeframe, 900) * 2
            if (time.time() - self._pending_signal['time']) > max_wait:
                print(f"[FST] {self.signal_key} | Pending signal expired")
                self._pending_signal = None
                self._persist()
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

        recent = False
        if is_new and latest:
            max_age = CANDLE_SECONDS.get(self.timeframe, 900) * 2
            recent = (time.time() - latest['time']) <= max_age

        print(f"[FST] {self.signal_key} {self.asset} {self.timeframe} | Scan #{self._scan_count} | Signals: {len(signals)} | New: {'YES ✓' if (is_new and recent) else 'no'} | Trades: {self.trades_today}/{self.max_trades_per_day}")

        if not is_new or not recent:
            return

        if self.signal_key in ONE_POSITION_KEYS and self.legs:
            self.last_signal_time = latest['time']   # consume it; it must not fire later
            print(f"[FST] {self.signal_key} | {latest['type'].upper()} signal skipped — position already open (one at a time)")
            if self.sid:
                self._persist()
            return

        # Price trigger: sma_vol_breakout entry = breakout candle high/low (pending)
        # All other strategies entry = candle close (already confirmed, trade immediately)
        if self.signal_key == 'sma_vol_breakout':
            price = self._refresh_mark()
            if price:
                entry = latest.get('price', 0)
                if latest['type'] == 'buy' and price < entry:
                    self._pending_signal = latest
                    self._persist()
                    print(f"[FST] {self.signal_key} | BUY pending: price {price:.2f} < entry {entry}")
                    return
                elif latest['type'] == 'sell' and price > entry:
                    self._pending_signal = latest
                    self._persist()
                    print(f"[FST] {self.signal_key} | SELL pending: price {price:.2f} > entry {entry}")
                    return

        self._pending_signal = None
        self.last_signal_time = latest['time']
        self._place_trade(latest)

    def _place_trade(self, signal):
        symbol = FUTURES_PRODUCTS.get(self.asset)
        if not symbol:
            print(f"[FST] ❌ No futures product for {self.asset}")
            return

        side = 'buy' if signal['type'] == 'buy' else 'sell'
        result = place_order(None, symbol, self.lots, side)
        entry_price = _fill_price(result, signal.get('price')) if result else signal.get('price')

        trade = {
            'action': 'OPEN',
            'time': datetime.now().strftime('%H:%M:%S'),
            'ts': _now_iso(),
            'signal_key': self.signal_key,
            'side': side,
            'price': round(float(entry_price or 0), 2),
            'signal_price': signal.get('price'),
            'size': self.lots,
            'sl': signal.get('sl'),
            'tp': signal.get('tp1'),
            'success': result is not None,
        }
        with self._lock:
            self.trade_log.append(trade)

        if result:
            self.trades_today += 1
            with self._lock:
                self.legs.append({
                    'symbol': symbol,
                    'side': side,
                    'size': self.lots,
                    'entry_price': float(entry_price),
                    'sl': signal.get('sl'),
                    'tp': signal.get('tp1'),
                    'time': trade['time'],
                    'opened_at': trade['ts'],
                    'signal_time': signal.get('time'),
                })
            print(f"[FST] 🟢 ORDER PLACED: {self.signal_key} {side.upper()} {self.lots} {symbol} @ {entry_price} "
                  f"(signal {signal.get('price')}) | SL: {signal.get('sl')} | TP: {signal.get('tp1')}")
        else:
            print(f"[FST] ❌ ORDER FAILED: {self.signal_key} {side.upper()} {symbol}")
        if self.sid:
            self._persist()

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
            'total_trades': sum(1 for t in self.trade_log if t.get('action', 'OPEN') == 'OPEN' and t.get('success')),
            'closed_trades': self.closed_count,
            'wins': self.wins,
            'losses': self.losses,
            'cumulative_pnl': round(self.cumulative_pnl, 2),
            'open_pnl': round(self.open_pnl, 2),
            'total_pnl': round(self.pnl, 2),
            'total_fees': round(self.total_fees, 2),
            'last_mark': self.last_mark,
            'legs': self.legs_view(),
            'trade_log': self.trade_log[-10:],
        }
