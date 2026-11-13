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

# Strategies whose SL / TP are triggered EXACTLY like the UI backtest (frontend/src/signalBacktest.js):
# on the SIGNAL feed's candles (the same get_candles data the signals come from), any candle from the
# entry candle onward whose low/high touches the level, SL checked first. The exchange mark price
# (testnet on demo profiles, and a basis away from Yahoo on prod) is only used for display / fallback.
BACKTEST_EXIT_KEYS = {'ema_trendline'}

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
        self.first_started_ts = None # first deploy time: signals closed after it are never dropped
        self.last_exit_bar_t = 0     # open time of the candle the last leg exited on (backtest busyUntil)
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
        self.trades_today = 0            # only a same-day count is restored (resume used to carry yesterday's)
        self._restore_state()
        if not self.first_started_ts:
            self.first_started_ts = time.time()
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
            self.first_started_ts = details.get('first_started_ts') or self.first_started_ts
            self.last_exit_bar_t = int(details.get('last_exit_bar_t') or 0)
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
                self._loop_candles = None            # one candle fetch per loop, shared by exits + scan
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
    def _candles(self):
        """Signal-feed candles, fetched at most once per loop iteration."""
        if getattr(self, '_loop_candles', None) is None:
            self._loop_candles = get_candles(self.asset, self.timeframe) or []
        return self._loop_candles

    def _entry_bar_t(self, leg):
        """Open time of the candle the leg was entered on (the candle after the signal candle)."""
        if leg.get('entry_bar_t'):
            return int(leg['entry_bar_t'])
        tf = CANDLE_SECONDS.get(self.timeframe, 900)
        if leg.get('signal_time'):
            return int(leg['signal_time']) + tf
        try:
            ts = datetime.fromisoformat(leg.get('opened_at')).timestamp()
            return int(ts // tf * tf)
        except Exception:
            return None

    def _candle_hit(self, leg, candles):
        """Backtest rule: first candle from the entry candle on that touches SL (checked first) or TP.
        Returns (reason, level, candle_open_time) or (None, None, None)."""
        start = self._entry_bar_t(leg)
        sl, tp = leg.get('sl'), leg.get('tp')
        if start is None:
            return None, None, None
        buy = leg['side'] == 'buy'
        for c in candles:
            if c['t'] < start:
                continue
            if sl and (c['l'] <= float(sl) if buy else c['h'] >= float(sl)):
                return 'SL', float(sl), c['t']
            if tp and (c['h'] >= float(tp) if buy else c['l'] <= float(tp)):
                return 'TP', float(tp), c['t']
        return None, None, None

    def _check_tp_sl(self):
        """Close legs that hit TP or SL.

        BACKTEST_EXIT_KEYS: triggered on the signal-feed candles exactly like the backtest (the forming
        candle's high/low carries the live price). Others / feed outage: on the exchange mark price."""
        price = self._refresh_mark()
        if not self.legs:
            return
        symbol = FUTURES_PRODUCTS.get(self.asset)
        candles = self._candles() if self.signal_key in BACKTEST_EXIT_KEYS else None
        if self.signal_key in BACKTEST_EXIT_KEYS and not candles:
            print(f"[FST] {self.signal_key} | ⚠ no candle data for SL/TP — falling back to the mark price")
        changed = False
        for leg in list(self.legs):
            sl, tp = leg.get('sl'), leg.get('tp')
            if not sl and not tp:
                continue
            hit, level, bar_t = None, None, None
            if candles:
                hit, level, bar_t = self._candle_hit(leg, candles)
            elif price:
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
            if hit and self._close_leg(leg, symbol, price or level, hit, level=level):
                tf = CANDLE_SECONDS.get(self.timeframe, 900)
                self.last_exit_bar_t = max(self.last_exit_bar_t, int(bar_t or time.time() // tf * tf))
                changed = True
        if changed and self.sid:
            self._persist()

    def _close_leg(self, leg, symbol, price, reason, level=None):
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
                'trigger_level': level,
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
                                        'wins': self.wins, 'losses': self.losses, 'last_mark': self.last_mark,
                                        'first_started_ts': self.first_started_ts,
                                        'last_exit_bar_t': self.last_exit_bar_t})
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

        candles = self._candles()
        if not candles:
            print(f"[FST] {self.signal_key} {self.asset} {self.timeframe} | ⚠ No candle data")
            return

        result = fn(candles)
        signals = result.get('signals', [])

        if self.signal_key in BACKTEST_EXIT_KEYS:
            self._scan_backtest(signals, candles)
            return

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

    def _scan_backtest(self, signals, candles):
        """Entries exactly as frontend/src/signalBacktest.js decides them (one position at a time).

        Every signal not handled yet is replayed in time order with the backtest's rules:
          - its entry candle = the candle after the signal candle; skipped while a position is open
            and when entry candle <= the candle the previous trade exited on (busyUntil);
          - from its entry candle up to now, if the SL / TP was already touched, the backtest trade is
            already over: not entered, but it still blocks later signals until its exit candle;
          - the one whose backtest trade is STILL OPEN is entered now at market.
        Normally that is the signal of the candle that just closed (entered ~30 s after the backtest's
        next-candle open). After downtime / a restart, the backtest's open trade is joined late instead
        of being lost. Signals that closed before the first deploy are only taken within 2 candles."""
        tf = CANDLE_SECONDS.get(self.timeframe, 900)
        now = time.time()
        pending = [s for s in signals if s['time'] > self.last_signal_time and
                   (now - s['time'] <= 2 * tf or s['time'] + tf >= (self.first_started_ts or now))]
        print(f"[FST] {self.signal_key} {self.asset} {self.timeframe} | Scan #{self._scan_count} | "
              f"Signals: {len(signals)} | New: {len(pending) or 'no'} | Trades: {self.trades_today}/"
              f"{self.max_trades_per_day}")
        if not pending:
            return
        self.last_signal_time = max(s['time'] for s in pending)       # every one is decided now
        busy = self.last_exit_bar_t
        join = None
        for s in pending:
            entry_bar = s['time'] + tf
            if self.legs:
                print(f"[FST] {self.signal_key} | {s['type'].upper()} signal skipped — position already open "
                      f"(one at a time)")
                continue
            if entry_bar <= busy:
                print(f"[FST] {self.signal_key} | {s['type'].upper()} signal skipped — its entry candle is the "
                      f"previous trade's exit candle (backtest rule)")
                continue
            probe = {'side': s['type'], 'sl': s.get('sl'), 'tp': s.get('tp1'), 'entry_bar_t': entry_bar}
            hit, level, bar_t = self._candle_hit(probe, candles)
            # touched already on its own entry candle that is still forming -> the backtest takes it and
            # books the stop / target on that candle; enter, the next exit check closes it the same way
            fresh = bar_t == entry_bar and now - entry_bar <= tf / 2
            if hit and not fresh:
                busy = bar_t
                self.last_exit_bar_t = max(self.last_exit_bar_t, bar_t)
                print(f"[FST] {self.signal_key} | {s['type'].upper()} signal not entered — the backtest trade "
                      f"already hit {hit} @ {level}")
                continue
            join = s
            break                                   # the backtest is in this trade now; later ones are skipped
        if join:
            late = now - (join['time'] + tf) > tf / 2
            if late:
                print(f"[FST] {self.signal_key} | ♻ joining the backtest's open {join['type'].upper()} trade late "
                      f"(signal candle {datetime.fromtimestamp(join['time'], timezone.utc):%Y-%m-%d %H:%M} UTC)")
            self._pending_signal = None
            self._place_trade(join)
        elif self.sid:
            self._persist()

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
                    # backtest entry candle = the one after the signal candle
                    'entry_bar_t': (int(signal['time']) + CANDLE_SECONDS.get(self.timeframe, 900))
                                   if signal.get('time') else None,
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
