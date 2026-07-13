"""Pivot + SuperTrend Intraday Options Selling — 0DTE BTC on Delta Exchange.

Strategy (adapted from Riyanshu Upadhyay / Theta Gainers):
- 5-min timeframe, SuperTrend (7, 3), Daily Pivot Points (R1/S1)
- Sell PUT ATM when candle closes above SuperTrend AND above R1
- Sell CALL ATM when candle closes below SuperTrend AND below S1
- Exit on SuperTrend flip or 5:00 PM IST
- Max 3 trades per day
- Skip wide-pivot days (previous day was very volatile)

Entry: 9:20 AM IST (after opening volatility settles)
Exit: 5:00 PM IST hard close (before 0DTE expiry at 5:30 PM)
Expiry: Today's 0DTE
"""

import time
import logging
import threading
import requests
import numpy as np
from datetime import datetime, timedelta, timezone
from api.chain import get_expiries, get_option_chain_full
from api.orders import place_order
from api.pricing import get_current_price
from config import get_contract_value
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Strategy parameters
SUPERTREND_PERIOD = 7
SUPERTREND_MULTIPLIER = 3
MAX_TRADES_PER_DAY = 3
WIDE_PIVOT_THRESHOLD = 0.04  # Skip day if pivot range > 4% of pivot
MONITOR_INTERVAL = 10  # seconds between price checks

# Session times (IST)
ENTRY_HOUR = 9
ENTRY_MINUTE = 20
EXIT_HOUR = 17
EXIT_MINUTE = 0

# Option selection — ATM
TARGET_DELTA = 0.50
DELTA_TOLERANCE = 0.15
LOT_SIZE = 100

# Binance API for 5-min candles
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_SYMBOL = "BTCUSDT"


class PivotSuperTrend(BaseStrategy):
    """0DTE intraday options selling: Pivot + SuperTrend signals on 5-min BTC.

    Sells ATM option directionally, exits on SuperTrend flip or 5:00 PM IST.
    """

    def __init__(self, asset='BTC', lot_size=LOT_SIZE, target_delta=TARGET_DELTA,
                 delta_tolerance=DELTA_TOLERANCE, st_period=SUPERTREND_PERIOD,
                 st_multiplier=SUPERTREND_MULTIPLIER, max_trades=MAX_TRADES_PER_DAY,
                 monitor_interval=MONITOR_INTERVAL,
                 entry_hour=ENTRY_HOUR, entry_minute=ENTRY_MINUTE,
                 exit_hour=EXIT_HOUR, exit_minute=EXIT_MINUTE):
        self.asset = asset
        self.lot_size = lot_size
        self.target_delta = target_delta
        self.delta_tolerance = delta_tolerance
        self.st_period = st_period
        self.st_multiplier = st_multiplier
        self.max_trades = max_trades
        self.monitor_interval = monitor_interval
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute
        self.exit_hour = exit_hour
        self.exit_minute = exit_minute

        # State
        self._running = False
        self.legs = []
        self._legs_lock = threading.Lock()
        self.cumulative_pnl = 0.0
        self.total_days_traded = 0
        self.today_trade_count = 0
        self.trade_log = []
        self._pnl_history = []
        self._snap_counter = 0
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10
        self._sid = None
        self._active_threads = []

        # Indicator state
        self._candle_history = []
        self._st_direction = None
        self._prev_st_direction = None
        self._pivot = None
        self._r1 = None
        self._s1 = None

        self._base_params = {
            'asset': asset, 'lot_size': lot_size, 'target_delta': target_delta,
            'st_period': st_period, 'st_multiplier': st_multiplier,
            'max_trades': max_trades, 'monitor_interval': monitor_interval,
            'entry_time': f"{entry_hour}:{entry_minute:02d}",
            'exit_time': f"{exit_hour}:{exit_minute:02d}",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # BaseStrategy Interface
    # ─────────────────────────────────────────────────────────────────────────

    def initialize(self):
        self._running = True
        print(f"[PivotST] Pivot + SuperTrend 0DTE strategy started")
        print(f"[PivotST] SuperTrend({self.st_period},{self.st_multiplier}) | Max {self.max_trades} trades/day")
        print(f"[PivotST] Entry: {self.entry_hour}:{self.entry_minute:02d} IST | Exit: {self.exit_hour}:{self.exit_minute:02d} IST")
        print(f"[PivotST] ATM target delta: {self.target_delta} ± {self.delta_tolerance}")
        return True

    def monitor(self):
        """Main daily loop — waits for entry time, runs intraday session."""
        while self._running:
            self._wait_for_next_entry()
            if not self._running:
                break

            self.total_days_traded += 1
            self.today_trade_count = 0
            day_num = self.total_days_traded
            tag = f"[PivotST D{day_num}]"

            now = datetime.now(IST)
            print(f"\n{tag} ═══ {now.strftime('%Y-%m-%d %H:%M')} IST ═══")

            # Load candle history and compute pivots
            if not self._load_initial_data(tag):
                print(f"{tag} ✗ Failed to load candle data, skipping day")
                continue

            # Wide-pivot filter
            if self._pivot and self._r1 and self._s1:
                pivot_range_pct = (self._r1 - self._s1) / self._pivot
                if pivot_range_pct > WIDE_PIVOT_THRESHOLD:
                    print(f"{tag} ⚠ Wide pivot range ({pivot_range_pct:.1%}) — skipping day")
                    continue
                print(f"{tag} Pivot: ${self._pivot:.0f} | R1: ${self._r1:.0f} | S1: ${self._s1:.0f} ({pivot_range_pct:.2%})")

            # Run intraday in a thread (like DailyStrangle pattern)
            t = threading.Thread(target=self._run_intraday_session,
                                 args=(tag, day_num), daemon=True)
            t.start()
            self._active_threads.append(t)

    def close_all(self):
        """Close any open position and stop."""
        self._running = False
        with self._legs_lock:
            legs_copy = list(self.legs)
        for leg in legs_copy:
            try:
                place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
                logger.info(f"[PivotST] Closed {leg['symbol']}")
            except Exception as e:
                logger.warning(f"[PivotST] Failed to close {leg.get('symbol')}: {e}")
        with self._legs_lock:
            self.legs.clear()
        self._persist_state()

    @property
    def pnl(self):
        cv = get_contract_value(self.asset)
        open_pnl = 0.0
        with self._legs_lock:
            for leg in list(self.legs):
                data = get_current_price(leg['product_id'], self.asset)
                if data:
                    open_pnl += (leg['entry_price'] - data['mark_price']) * leg['size'] * cv
        return self.cumulative_pnl + open_pnl

    # ─────────────────────────────────────────────────────────────────────────
    # Intraday Session (runs in thread, like DailyStrangle._monitor_day)
    # ─────────────────────────────────────────────────────────────────────────

    def _run_intraday_session(self, tag, day_num):
        """Scan 5-min candles, enter on signals, exit on ST flip or session end."""
        # Set thread-local credentials for API calls
        if hasattr(self, '_api_key') and self._api_key:
            from config import set_thread_credentials
            set_thread_credentials(self._api_key, self._api_secret, self._broker)
        if hasattr(self, '_log_queue') and self._log_queue:
            from app import LogCapture
            LogCapture._local.log_queue = self._log_queue
            LogCapture._local.log_history = self._log_history

        cv = get_contract_value(self.asset)
        last_candle_time = self._candle_history[-1]['t'] if self._candle_history else 0
        day_pnl = 0.0
        day_trades = []  # All trades this day (for P&L tracking)

        while self._running:
            now = datetime.now(IST)

            # ─── Session end: hard exit at 5:00 PM IST ───
            if now.hour > self.exit_hour or (now.hour == self.exit_hour and now.minute >= self.exit_minute):
                if self.legs:
                    print(f"{tag} ⏰ 5:00 PM — closing position")
                    pnl = self._close_current_leg(tag, 'session_end', cv)
                    day_pnl += pnl
                    day_trades.append(('session_end', pnl))
                break

            # ─── Fetch latest 5-min candle ───
            new_candle = self._fetch_latest_candle()
            if new_candle and new_candle['t'] > last_candle_time:
                last_candle_time = new_candle['t']
                self._candle_history.append(new_candle)
                # Keep history manageable
                if len(self._candle_history) > 300:
                    self._candle_history = self._candle_history[-300:]

                # Recalculate SuperTrend
                self._update_supertrend()

                close = new_candle['c']
                st_dir = self._st_direction
                prev_dir = self._prev_st_direction

                # ─── EXIT: SuperTrend flipped ───
                if self.legs and prev_dir is not None and st_dir != prev_dir:
                    print(f"{tag} 🔄 SuperTrend flipped ({'↑' if st_dir == 1 else '↓'}) — exiting")
                    pnl = self._close_current_leg(tag, 'st_flip', cv)
                    day_pnl += pnl
                    day_trades.append(('st_flip', pnl))

                # ─── ENTRY: Check for new signal ───
                if not self.legs and self.today_trade_count < self.max_trades:
                    signal = self._check_entry_signal(close, st_dir)
                    if signal:
                        print(f"{tag} 📊 Signal: {signal.upper()} | BTC: ${close:,.0f} | ST: {'↑' if st_dir == 1 else '↓'} | R1: ${self._r1:,.0f} | S1: ${self._s1:,.0f}")
                        success = self._execute_entry(tag, signal)
                        if success:
                            self.today_trade_count += 1

            # ─── Monitor open position ───
            elif self.legs:
                leg = self.legs[0]
                data = get_current_price(leg['product_id'], self.asset)
                if data:
                    current_pnl = (leg['entry_price'] - data['mark_price']) * leg['size'] * cv
                    leg['current_mark'] = round(data['mark_price'], 4)
                    leg['current_pnl'] = round(current_pnl, 4)
                    self._consecutive_failures = 0
                else:
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= self._max_consecutive_failures:
                        print(f"{tag} 🚨 {self._consecutive_failures} API failures — emergency close")
                        pnl = self._close_current_leg(tag, 'api_failure', cv)
                        day_pnl += pnl
                        day_trades.append(('api_failure', pnl))
                        break

                # PnL history for UI
                now_iso = now.isoformat()
                self._pnl_history.append((now_iso, round(self.cumulative_pnl + day_pnl + current_pnl, 4)))
                if len(self._pnl_history) > 500:
                    self._pnl_history = self._pnl_history[-500:]

                # DB snapshot every 6 ticks
                self._snap_counter += 1
                if self._snap_counter % 6 == 0 and getattr(self, '_sid', None):
                    try:
                        from models import save_pnl_snapshot
                        user_id = getattr(self, '_user_id', None)
                        if user_id:
                            save_pnl_snapshot(user_id, self._sid,
                                              round(self.cumulative_pnl + day_pnl + current_pnl, 4))
                    except Exception:
                        pass

            time.sleep(self.monitor_interval)

        # Day complete — record results
        self.cumulative_pnl += day_pnl
        self.trade_log.append({
            'date': datetime.now(IST).strftime('%Y-%m-%d'),
            'day': day_num,
            'trades': self.today_trade_count,
            'pnl': round(day_pnl, 4),
            'details': day_trades,
        })
        print(f"{tag} Session done | Trades: {self.today_trade_count} | Day P&L: ${day_pnl:+.4f} | Cum: ${self.cumulative_pnl:+.4f}")
        self._persist_state()

    # ─────────────────────────────────────────────────────────────────────────
    # Signal & Execution
    # ─────────────────────────────────────────────────────────────────────────

    def _check_entry_signal(self, close, st_direction):
        """Returns 'sell_put', 'sell_call', or None."""
        if self._r1 is None or self._s1 is None:
            return None
        # Bullish: price above SuperTrend AND above R1 → Sell PUT (ATM)
        if st_direction == 1 and close > self._r1:
            return 'sell_put'
        # Bearish: price below SuperTrend AND below S1 → Sell CALL (ATM)
        if st_direction == -1 and close < self._s1:
            return 'sell_call'
        return None

    def _execute_entry(self, tag, signal):
        """Place ATM option sell order on today's 0DTE expiry. Returns True on success."""
        expiries = get_expiries(self.asset, min_days=0)
        if not expiries:
            print(f"{tag} ✗ No expiries available")
            return False
        expiry = expiries[0]

        chain, spot_price, _ = get_option_chain_full(expiry, self.asset)
        if not chain:
            print(f"{tag} ✗ No option chain for {expiry}")
            return False

        # Find ATM option
        opt_type = 'put' if signal == 'sell_put' else 'call'
        option = self._find_atm_option(chain, opt_type)
        if not option:
            print(f"{tag} ✗ No suitable ATM {opt_type} found")
            return False

        # Place sell order
        result = place_order(option['product_id'], option['symbol'], self.lot_size, 'sell')
        if not result:
            print(f"{tag} ✗ Order failed for {option['symbol']}")
            return False

        leg = {
            'symbol': option['symbol'],
            'product_id': option['product_id'],
            'side': 'sell',
            'type': opt_type,
            'delta': option.get('delta', 0),
            'strike': option['strike'],
            'entry_price': option['mark_price'],
            'size': self.lot_size,
            'entry_time': datetime.now(IST).isoformat(),
            'signal': signal,
        }

        with self._legs_lock:
            self.legs.append(leg)

        print(f"{tag} ✓ SELL {opt_type.upper()} {option['strike']} (Δ{option.get('delta', 0):.2f}) @ ${option['mark_price']:.2f}")
        print(f"{tag} Trade #{self.today_trade_count + 1}/{self.max_trades} today")
        self._persist_state()
        return True

    def _find_atm_option(self, chain, opt_type):
        """Find ATM option (closest to 0.50 delta) from chain."""
        candidates = []
        for row in chain:
            opt = row.get(opt_type)
            if not opt or not opt.get('mark_price') or opt['mark_price'] <= 0:
                continue
            delta = abs(opt.get('delta', 0))
            if abs(delta - self.target_delta) <= self.delta_tolerance:
                candidates.append(opt)

        if not candidates:
            # Fallback: pick closest to 0.50 delta from all available
            for row in chain:
                opt = row.get(opt_type)
                if not opt or not opt.get('mark_price') or opt['mark_price'] <= 0:
                    continue
                candidates.append(opt)

        if not candidates:
            return None

        candidates.sort(key=lambda x: abs(abs(x.get('delta', 0)) - self.target_delta))
        return candidates[0]

    def _close_current_leg(self, tag, exit_reason, cv):
        """Close current open position. Returns realized P&L."""
        with self._legs_lock:
            if not self.legs:
                return 0.0
            leg = self.legs[0]

        # Get exit price
        data = get_current_price(leg['product_id'], self.asset)
        exit_price = data['mark_price'] if data else leg['entry_price']

        # Close order
        place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')

        # Calculate P&L
        pnl = (leg['entry_price'] - exit_price) * leg['size'] * cv

        with self._legs_lock:
            self.legs.clear()

        emoji = '🎯' if pnl > 0 else '🛑'
        print(f"{tag} {emoji} Closed {leg['type'].upper()} {leg['strike']} @ ${exit_price:.2f} | P&L: ${pnl:+.4f} ({exit_reason})")
        self._persist_state()
        return pnl

    # ─────────────────────────────────────────────────────────────────────────
    # Indicators (SuperTrend + Pivot from Binance 5-min data)
    # ─────────────────────────────────────────────────────────────────────────

    def _load_initial_data(self, tag):
        """Fetch 200 historical 5-min candles for indicator warmup + compute pivots."""
        try:
            candles = self._fetch_binance_klines(limit=200)
            if not candles or len(candles) < self.st_period + 10:
                return False
            self._candle_history = candles
            print(f"{tag} Loaded {len(candles)} candles for indicator warmup")
            self._compute_daily_pivots()
            self._compute_full_supertrend()
            return True
        except Exception as e:
            logger.error(f"[PivotST] Load initial data failed: {e}")
            return False

    def _fetch_binance_klines(self, limit=200):
        """Fetch historical 5-min BTCUSDT klines from Binance."""
        try:
            resp = requests.get(BINANCE_KLINES_URL, params={
                "symbol": BINANCE_SYMBOL, "interval": "5m", "limit": limit,
            }, timeout=10)
            if resp.status_code != 200:
                return None
            return [{'t': k[0] / 1000, 'o': float(k[1]), 'h': float(k[2]),
                     'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5])}
                    for k in resp.json()]
        except Exception as e:
            logger.error(f"Binance klines fetch failed: {e}")
            return None

    def _fetch_latest_candle(self):
        """Fetch most recent CLOSED 5-min candle."""
        try:
            resp = requests.get(BINANCE_KLINES_URL, params={
                "symbol": BINANCE_SYMBOL, "interval": "5m", "limit": 2,
            }, timeout=10)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if len(data) < 2:
                return None
            # Index 0 = last closed candle, index 1 = currently forming
            k = data[0]
            return {'t': k[0] / 1000, 'o': float(k[1]), 'h': float(k[2]),
                    'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5])}
        except Exception:
            return None

    def _compute_daily_pivots(self):
        """Standard pivot points from previous UTC day's OHLC."""
        if not self._candle_history:
            return
        from collections import defaultdict
        daily = defaultdict(list)
        for c in self._candle_history:
            day_key = datetime.utcfromtimestamp(c['t']).strftime('%Y-%m-%d')
            daily[day_key].append(c)

        sorted_days = sorted(daily.keys())
        if len(sorted_days) < 2:
            return

        prev_candles = daily[sorted_days[-2]]
        day_high = max(c['h'] for c in prev_candles)
        day_low = min(c['l'] for c in prev_candles)
        day_close = prev_candles[-1]['c']

        self._pivot = (day_high + day_low + day_close) / 3
        self._r1 = 2 * self._pivot - day_low
        self._s1 = 2 * self._pivot - day_high

    def _compute_full_supertrend(self):
        """Compute SuperTrend on full candle history using numpy."""
        candles = self._candle_history
        n = len(candles)
        if n < self.st_period:
            return

        highs = np.array([c['h'] for c in candles])
        lows = np.array([c['l'] for c in candles])
        closes = np.array([c['c'] for c in candles])

        # True Range
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(np.abs(highs[1:] - closes[:-1]),
                       np.abs(lows[1:] - closes[:-1]))
        )
        tr = np.concatenate([[highs[0] - lows[0]], tr])

        # ATR (RMA / Wilder's smoothing)
        atr = np.zeros(n)
        atr[:self.st_period] = tr[:self.st_period].mean()
        for i in range(self.st_period, n):
            atr[i] = (atr[i-1] * (self.st_period - 1) + tr[i]) / self.st_period

        hl2 = (highs + lows) / 2
        upper_band = hl2 + (self.st_multiplier * atr)
        lower_band = hl2 - (self.st_multiplier * atr)

        direction = np.zeros(n, dtype=int)
        direction[0] = -1

        for i in range(1, n):
            # Band continuity
            if lower_band[i] <= lower_band[i-1] and closes[i-1] >= lower_band[i-1]:
                lower_band[i] = lower_band[i-1]
            if upper_band[i] >= upper_band[i-1] and closes[i-1] <= upper_band[i-1]:
                upper_band[i] = upper_band[i-1]

            # Direction
            if direction[i-1] == 1:  # was bullish
                direction[i] = -1 if closes[i] < lower_band[i] else 1
            else:  # was bearish
                direction[i] = 1 if closes[i] > upper_band[i] else -1

        self._prev_st_direction = self._st_direction
        self._st_direction = int(direction[-1])
        if self._prev_st_direction is None:
            self._prev_st_direction = int(direction[-2]) if n > 1 else self._st_direction

    def _update_supertrend(self):
        """Recompute SuperTrend with updated candle history."""
        self._compute_full_supertrend()

    # ─────────────────────────────────────────────────────────────────────────
    # Timing & Persistence
    # ─────────────────────────────────────────────────────────────────────────

    def _wait_for_next_entry(self):
        """Wait until 9:20 AM IST next trading session."""
        now = datetime.now(IST)
        target = now.replace(hour=self.entry_hour, minute=self.entry_minute,
                             second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        if wait > 60:
            print(f"[PivotST] Next session: {target.strftime('%Y-%m-%d %H:%M')} IST ({wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))

    def _persist_state(self):
        """Save state to DB for server restart survival."""
        try:
            from models import update_strategy_db
            sid = getattr(self, '_sid', None)
            if not sid:
                try:
                    from app import pivot_st_strategies
                    for s_id, entry in pivot_st_strategies.items():
                        if entry.get('strategy') is self:
                            sid = s_id
                            self._sid = sid
                            break
                except Exception:
                    pass
            if not sid:
                return
            details = {
                **self._base_params,
                'cumulative_pnl': self.cumulative_pnl,
                'total_days_traded': self.total_days_traded,
                'trade_log': self.trade_log[-50:],
            }
            legs_data = [{k: v for k, v in leg.items()} for leg in self.legs]
            update_strategy_db(sid, details=details, legs=legs_data,
                               pnl=round(self.cumulative_pnl, 4))
        except Exception as e:
            logger.warning(f"[PivotST] Persist state failed: {e}")
