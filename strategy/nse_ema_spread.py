"""NSE EMA Credit Spread — Daily recurring strategy for Indian market.

Every trading day at 10:25 AM IST:
1. Fetch daily candles for the symbol, compute EMA10
2. If price < EMA10 → bearish → sell OTM call, buy further OTM call (bear call spread)
3. If price > EMA10 → bullish → sell OTM put, buy further OTM put (bull put spread)
4. Expiry: nearest available weekly
5. TP: 90% of net premium collected | SL: 150% of net premium
6. After exit, waits for next trading day.

Market hours: 9:15 AM – 3:30 PM IST, Mon–Fri.
Skips weekends and non-trading days.
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone

from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# NSE lot sizes
NSE_LOT_SIZES = {
    'NIFTY': 65,
    'BANKNIFTY': 30,
    'FINNIFTY': 65,
    'MIDCPNIFTY': 50,
}

# Market hours
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30

# Defaults
DEFAULT_ENTRY_HOUR = 10
DEFAULT_ENTRY_MINUTE = 25
DEFAULT_EXIT_HOUR = 15
DEFAULT_EXIT_MINUTE = 15
DEFAULT_EMA_PERIOD = 10
DEFAULT_SELL_DELTA = 0.20
DEFAULT_BUY_DELTA = 0.10
DEFAULT_TP_PCT = 0.90   # 90% of premium
DEFAULT_SL_PCT = 1.50   # 150% of premium
DEFAULT_MONITOR_INTERVAL = 15
DEFAULT_TRADING_DAYS = [0, 1, 2, 3, 4]  # Mon-Fri


def _get_data_source():
    """Determine which data source to use based on thread-local broker setting."""
    try:
        from config import _thread_local
        broker = getattr(_thread_local, 'broker', '')
        if broker == 'groww':
            from api.groww import get_groww_expiries, get_groww_chain
            return get_groww_expiries, get_groww_chain
    except Exception:
        pass
    from api.nse import get_nse_expiries, get_nse_chain
    return get_nse_expiries, get_nse_chain


class NseEmaCreditSpread(BaseStrategy):
    """NSE EMA-based credit spread. Trades daily at configured entry time during market hours."""

    def __init__(self, symbol='NIFTY', lots=1, ema_period=DEFAULT_EMA_PERIOD,
                 sell_delta=DEFAULT_SELL_DELTA, buy_delta=DEFAULT_BUY_DELTA,
                 tp_pct=DEFAULT_TP_PCT, sl_pct=DEFAULT_SL_PCT,
                 monitor_interval=DEFAULT_MONITOR_INTERVAL,
                 entry_hour=DEFAULT_ENTRY_HOUR, entry_minute=DEFAULT_ENTRY_MINUTE,
                 exit_hour=DEFAULT_EXIT_HOUR, exit_minute=DEFAULT_EXIT_MINUTE,
                 trading_days=None, lot_size=None, paper_trade=True):
        self.symbol = symbol
        self.lots = lots
        self.lot_size = lot_size if lot_size else NSE_LOT_SIZES.get(symbol, 50)
        self.quantity = self.lots * self.lot_size
        self.ema_period = ema_period
        self.sell_delta = sell_delta
        self.buy_delta = buy_delta
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.monitor_interval = monitor_interval
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute
        self.exit_hour = exit_hour
        self.exit_minute = exit_minute
        self.trading_days = trading_days if trading_days is not None else DEFAULT_TRADING_DAYS
        self.paper_trade = paper_trade

        # State
        self._running = False
        self.legs = []
        self._legs_lock = threading.Lock()
        self.cumulative_pnl = 0.0
        self.total_days_traded = 0
        self.trade_log = []
        self._pnl_history = []
        self._snap_counter = 0
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10

        # Credentials (captured at initialize)
        self._api_key = None
        self._api_secret = None
        self._broker = None

    def initialize(self):
        self._running = True
        # Capture thread-local credentials
        from config import get_api_key, get_api_secret, _thread_local
        self._api_key = get_api_key()
        self._api_secret = get_api_secret()
        self._broker = getattr(_thread_local, 'broker', '')

        days_str = ','.join(['Mon', 'Tue', 'Wed', 'Thu', 'Fri'][d] for d in self.trading_days)
        print(f"[NSE EMA] {self.symbol} Credit Spread {'(Paper)' if self.paper_trade else '(Live)'}")
        print(f"[NSE EMA] EMA{self.ema_period} | Sell {self.sell_delta}Δ / Buy {self.buy_delta}Δ")
        print(f"[NSE EMA] TP: {self.tp_pct*100:.0f}% | SL: {self.sl_pct*100:.0f}% of premium")
        print(f"[NSE EMA] Entry: {self.entry_hour}:{self.entry_minute:02d} | Exit: {self.exit_hour}:{self.exit_minute:02d} IST")
        print(f"[NSE EMA] Lots: {self.lots} ({self.quantity} qty) | Days: {days_str}")
        return True

    def _set_thread_credentials(self):
        """Propagate credentials to the current thread."""
        from config import set_thread_credentials
        if self._api_key:
            set_thread_credentials(self._api_key, self._api_secret, self._broker)

    # ─── Main Loop ───────────────────────────────────────────────────────

    def monitor(self):
        """Main daily loop — opens a trade each trading day and monitors it."""
        while self._running:
            self._wait_for_next_entry()
            if not self._running:
                break

            now = datetime.now(IST)
            if now.weekday() not in self.trading_days:
                print(f"[NSE EMA] Skipping {now.strftime('%A')} — not a trading day")
                continue

            self.total_days_traded += 1
            day_num = self.total_days_traded
            tag = f"[NSE EMA D{day_num}]"
            print(f"\n{tag} ═══ {now.strftime('%Y-%m-%d %H:%M')} IST ═══")

            day_legs, premium, direction = self._open_daily_trade(tag, day_num)
            if not day_legs:
                print(f"{tag} No trade today")
                continue

            # Monitor in a child thread so main loop can proceed next day
            t = threading.Thread(target=self._monitor_day_trade,
                                 args=(day_legs, premium, day_num, direction), daemon=True)
            t.start()

    # ─── EMA Direction ───────────────────────────────────────────────────

    def _get_ema_direction(self, spot):
        """Compute EMA from daily candles and determine direction.

        Returns ('bearish', ema_value) or ('bullish', ema_value) or (None, None).
        Uses NSE historical data or Groww daily candles.
        """
        try:
            # Try to get daily candles from NSE/Groww
            from api.nse import get_nse_daily_candles
            candles = get_nse_daily_candles(self.symbol, days=self.ema_period + 5)
        except (ImportError, Exception):
            candles = None

        if not candles or len(candles) < self.ema_period:
            # Fallback: use spot price vs a rough EMA from chain data
            # If we can't compute EMA, just use spot relative to recent avg
            logger.warning(f"[NSE EMA] Not enough candle data ({len(candles) if candles else 0}), using fallback")
            return None, None

        # Compute EMA from close prices
        closes = [c['close'] for c in candles]
        ema = self._calc_ema(closes, self.ema_period)
        if ema is None:
            return None, None

        direction = 'bearish' if spot < ema else 'bullish'
        return direction, ema

    def _calc_ema(self, closes, period):
        """Calculate EMA from a list of close prices. Returns latest EMA value."""
        if len(closes) < period:
            return None
        multiplier = 2 / (period + 1)
        ema = sum(closes[:period]) / period  # SMA seed
        for price in closes[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    # ─── Open Trade ──────────────────────────────────────────────────────

    def _open_daily_trade(self, tag, day_num):
        """Check EMA direction, find delta options, open spread.

        Returns (day_legs, premium, direction) or ([], 0, '').
        """
        _get_expiries, _get_chain = _get_data_source()

        # Get expiries and chain
        expiries = _get_expiries(self.symbol)
        if not expiries or len(expiries) < 2:
            print(f"{tag} ✗ Not enough expiries found (need at least 2)")
            return [], 0, ''

        # Pick second nearest expiry (next week, not current)
        expiry = expiries[1]
        chain, spot, _ = _get_chain(self.symbol, expiry)
        if not chain or not spot:
            print(f"{tag} ✗ Chain fetch failed")
            return [], 0, ''

        # Determine direction from EMA
        direction, ema_value = self._get_ema_direction(spot)
        if direction is None:
            # Fallback: if EMA can't be computed, skip or use a simple heuristic
            # Try using ATM IV skew or just skip
            print(f"{tag} ✗ Could not determine EMA direction, skipping")
            return [], 0, ''

        print(f"{tag} Spot: ₹{spot:.0f} | EMA{self.ema_period}: ₹{ema_value:.0f} | {direction.upper()}")
        print(f"{tag} Expiry: {expiry}")

        # Find sell and buy legs based on direction
        if direction == 'bearish':
            # Bear call spread: sell OTM call, buy further OTM call
            opt_type = 'call'
            sell_opt = self._find_by_delta(chain, spot, 'call', self.sell_delta)
            buy_opt = self._find_by_delta(chain, spot, 'call', self.buy_delta)
        else:
            # Bull put spread: sell OTM put, buy further OTM put
            opt_type = 'put'
            sell_opt = self._find_by_delta(chain, spot, 'put', self.sell_delta)
            buy_opt = self._find_by_delta(chain, spot, 'put', self.buy_delta)

        if not sell_opt or not buy_opt:
            print(f"{tag} ✗ Could not find suitable options for spread")
            return [], 0, ''

        # Avoid same strike
        if sell_opt['strike'] == buy_opt['strike']:
            print(f"{tag} ✗ Sell and buy at same strike — skipping")
            return [], 0, ''

        # Calculate net premium (sell - buy)
        premium = (sell_opt['mark_price'] - buy_opt['mark_price']) * self.quantity
        if premium <= 0:
            print(f"{tag} ✗ No net credit (sell ₹{sell_opt['mark_price']:.2f} <= buy ₹{buy_opt['mark_price']:.2f})")
            return [], 0, ''

        # Place orders if live
        if not self.paper_trade:
            success = self._place_spread_orders(sell_opt, buy_opt, tag)
            if not success:
                return [], 0, ''

        # Build leg records
        day_legs = [
            {'symbol': sell_opt.get('symbol', ''), 'trading_symbol': sell_opt.get('trading_symbol', ''),
             'side': 'sell', 'type': opt_type, 'delta': sell_opt.get('delta', 0),
             'strike': sell_opt['strike'], 'entry_price': sell_opt['mark_price'],
             'size': self.quantity, 'day_num': day_num, 'expiry': expiry,
             'opened_at': datetime.now(IST).strftime('%Y-%m-%d')},
            {'symbol': buy_opt.get('symbol', ''), 'trading_symbol': buy_opt.get('trading_symbol', ''),
             'side': 'buy', 'type': opt_type, 'delta': buy_opt.get('delta', 0),
             'strike': buy_opt['strike'], 'entry_price': buy_opt['mark_price'],
             'size': self.quantity, 'day_num': day_num, 'expiry': expiry,
             'opened_at': datetime.now(IST).strftime('%Y-%m-%d')},
        ]

        target = premium * self.tp_pct
        sl = premium * self.sl_pct

        print(f"{tag} ✓ SELL {opt_type.upper()} {sell_opt['strike']} (Δ{sell_opt.get('delta', 0):.3f}) @ ₹{sell_opt['mark_price']:.2f}")
        print(f"{tag} ✓ BUY  {opt_type.upper()} {buy_opt['strike']} (Δ{buy_opt.get('delta', 0):.3f}) @ ₹{buy_opt['mark_price']:.2f}")
        print(f"{tag} Net premium: ₹{premium:.2f} | TP: ₹{target:.2f} | SL: -₹{sl:.2f}")

        with self._legs_lock:
            self.legs.extend(day_legs)
        self._persist_state()
        return day_legs, premium, direction

    def _find_by_delta(self, chain, spot, opt_type, target_delta):
        """Find an OTM option closest to target delta."""
        candidates = []
        for row in chain:
            strike = float(row['strike'])
            opt = row.get(opt_type)
            if not opt or opt.get('mark_price', 0) <= 0:
                continue
            delta = opt.get('delta', 0)
            # OTM filter
            if opt_type == 'call' and strike <= spot:
                continue
            if opt_type == 'put' and strike >= spot:
                continue
            if opt_type == 'call' and delta <= 0:
                continue
            if opt_type == 'put' and delta >= 0:
                continue
            candidates.append({
                'symbol': opt.get('symbol', ''),
                'trading_symbol': opt.get('trading_symbol', ''),
                'strike': row['strike'],
                'mark_price': opt['mark_price'],
                'delta': delta,
            })

        if not candidates:
            return None

        candidates.sort(key=lambda x: abs(abs(x['delta']) - target_delta))
        return candidates[0]

    def _place_spread_orders(self, sell_opt, buy_opt, tag):
        """Place live orders via Groww for the spread."""
        try:
            from api.groww import place_order
            sell_resp = place_order(
                trading_symbol=sell_opt.get('trading_symbol', ''),
                quantity=self.quantity,
                transaction_type='SELL', order_type='MARKET', product='MIS')
            if sell_resp.get('error'):
                print(f"{tag} ✗ Sell order failed: {sell_resp['error']}")
                return False
            buy_resp = place_order(
                trading_symbol=buy_opt.get('trading_symbol', ''),
                quantity=self.quantity,
                transaction_type='BUY', order_type='MARKET', product='MIS')
            if buy_resp.get('error'):
                print(f"{tag} ✗ Buy order failed: {buy_resp['error']}")
                return False
            return True
        except Exception as e:
            print(f"{tag} ✗ Order error: {e}")
            return False

    # ─── Day Trade Monitor ───────────────────────────────────────────────

    def _monitor_day_trade(self, day_legs, premium, day_num, direction):
        """Monitor a single day's spread until TP/SL/EOD."""
        self._set_thread_credentials()

        # Set up log routing for this thread
        if hasattr(self, '_log_queue') and self._log_queue:
            from app import LogCapture
            LogCapture._local.log_queue = self._log_queue
            LogCapture._local.log_history = self._log_history

        target = premium * self.tp_pct
        sl = premium * self.sl_pct
        tag = f"[NSE EMA D{day_num}]"
        expiry = day_legs[0].get('expiry', '')
        cycle = 0

        while self._running:
            time.sleep(self.monitor_interval)
            cycle += 1
            now = datetime.now(IST)

            # Market closed — skip tick
            if not self._is_market_open():
                continue

            # Fetch current prices
            _get_expiries, _get_chain = _get_data_source()
            chain, spot, _ = _get_chain(self.symbol, expiry)
            if not chain:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._max_consecutive_failures:
                    print(f"{tag} 🚨 EMERGENCY: {self._consecutive_failures} failures — closing")
                    self._close_day_legs(day_legs, tag)
                    pnl = self._calc_spread_pnl(day_legs)
                    self._record_day(day_num, pnl, premium, 'data_failure', direction)
                    return
                continue
            self._consecutive_failures = 0

            # Calculate spread P&L
            pnl = 0.0
            all_ok = True
            for leg in day_legs:
                current = self._get_price_from_chain(chain, leg)
                if current is None:
                    all_ok = False
                    continue
                leg['current_mark'] = round(current, 2)
                if leg['side'] == 'sell':
                    leg_pnl = (leg['entry_price'] - current) * self.quantity
                else:
                    leg_pnl = (current - leg['entry_price']) * self.quantity
                leg['current_pnl'] = round(leg_pnl, 2)
                pnl += leg_pnl

            if not all_ok:
                continue

            # PnL history
            self._pnl_history.append((now.isoformat(), round(self.cumulative_pnl + pnl, 2)))
            if len(self._pnl_history) > 500:
                self._pnl_history = self._pnl_history[-500:]

            # Snapshot every 6 ticks
            self._snap_counter += 1
            if self._snap_counter % 6 == 0:
                self._do_snapshot(self.cumulative_pnl + pnl)

            # Log every 4 ticks
            if cycle % 4 == 1:
                pct = (pnl / premium * 100) if premium > 0 else 0
                print(f"{tag} PnL: ₹{pnl:+.2f} ({pct:+.1f}%) | Cum: ₹{self.cumulative_pnl:+.2f}")

            # Check TP/SL
            if pnl >= target:
                print(f"{tag} 🎯 TP hit: ₹{pnl:.2f} >= ₹{target:.2f}")
                self._close_day_legs(day_legs, tag)
                self._record_day(day_num, pnl, premium, 'target', direction)
                return
            if pnl <= -sl:
                print(f"{tag} 🛑 SL hit: ₹{pnl:.2f} <= -₹{sl:.2f}")
                self._close_day_legs(day_legs, tag)
                self._record_day(day_num, pnl, premium, 'stoploss', direction)
                return

    def _calc_spread_pnl(self, day_legs):
        """Calculate P&L from leg current marks."""
        pnl = 0.0
        for leg in day_legs:
            mark = leg.get('current_mark', leg['entry_price'])
            if leg['side'] == 'sell':
                pnl += (leg['entry_price'] - mark) * self.quantity
            else:
                pnl += (mark - leg['entry_price']) * self.quantity
        return pnl

    def _close_day_legs(self, day_legs, tag):
        """Close spread legs (live orders or just record exit)."""
        if not self.paper_trade:
            try:
                from api.groww import place_order
                for leg in day_legs:
                    close_side = 'BUY' if leg['side'] == 'sell' else 'SELL'
                    place_order(
                        trading_symbol=leg.get('trading_symbol', ''),
                        quantity=self.quantity,
                        transaction_type=close_side, order_type='MARKET', product='MIS')
            except Exception as e:
                print(f"{tag} ⚠ Close order error: {e}")

        # Remove from shared legs
        with self._legs_lock:
            for leg in day_legs:
                if leg in self.legs:
                    self.legs.remove(leg)

    def _record_day(self, day_num, pnl, premium, exit_reason, direction):
        """Record trade result and update cumulative P&L."""
        self.cumulative_pnl += pnl
        self.trade_log.append({
            'date': datetime.now(IST).strftime('%Y-%m-%d'),
            'day': day_num,
            'pnl': round(pnl, 2),
            'premium': round(premium, 2),
            'exit_reason': exit_reason,
            'direction': direction,
        })
        print(f"[NSE EMA D{day_num}] Closed | PnL: ₹{pnl:+.2f} | Cumulative: ₹{self.cumulative_pnl:+.2f}")
        self._persist_state()

    # ─── Close All ───────────────────────────────────────────────────────

    def close_all(self):
        """Stop the strategy and close any open legs."""
        self._running = False
        with self._legs_lock:
            legs_copy = list(self.legs)
        if legs_copy and not self.paper_trade:
            try:
                from api.groww import place_order
                for leg in legs_copy:
                    close_side = 'BUY' if leg['side'] == 'sell' else 'SELL'
                    place_order(
                        trading_symbol=leg.get('trading_symbol', ''),
                        quantity=self.quantity,
                        transaction_type=close_side, order_type='MARKET', product='MIS')
            except Exception:
                pass
        with self._legs_lock:
            self.legs.clear()
        try:
            self._persist_state()
        except Exception:
            pass

    # ─── Properties ──────────────────────────────────────────────────────

    @property
    def pnl(self):
        """Current total P&L (realized + unrealized from open legs)."""
        open_pnl = 0.0
        with self._legs_lock:
            for leg in self.legs:
                mark = leg.get('current_mark')
                if mark is None:
                    continue
                if leg['side'] == 'sell':
                    open_pnl += (leg['entry_price'] - mark) * self.quantity
                else:
                    open_pnl += (mark - leg['entry_price']) * self.quantity
        return self.cumulative_pnl + open_pnl

    # ─── Helpers ─────────────────────────────────────────────────────────

    def _is_market_open(self):
        now = datetime.now(IST)
        if now.weekday() > 4:
            return False
        market_open = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0)
        market_close = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0)
        return market_open <= now <= market_close

    def _get_price_from_chain(self, chain, leg):
        """Look up current mark price for a leg from chain data."""
        strike = str(leg['strike'])
        opt_type = leg['type']
        for row in chain:
            if str(row['strike']) == strike:
                opt = row.get(opt_type)
                if opt and opt.get('mark_price', 0) > 0:
                    return opt['mark_price']
        return None

    def _wait_for_next_entry(self):
        """Wait until next trading day entry time. Skips weekends and non-trading days."""
        now = datetime.now(IST)
        entry_today = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)

        if now < entry_today and now.weekday() in self.trading_days:
            target = entry_today
        else:
            target = entry_today + timedelta(days=1)
            attempts = 0
            while target.weekday() not in self.trading_days and attempts < 7:
                target += timedelta(days=1)
                attempts += 1

        wait = (target - now).total_seconds()
        if wait > 60:
            day_name = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][target.weekday()]
            print(f"[NSE EMA] Next entry: {target.strftime('%Y-%m-%d %H:%M')} IST ({day_name}, {wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))

    def _do_snapshot(self, total_pnl):
        """Save PnL snapshot to DB."""
        try:
            from models import save_pnl_snapshot
            sid = getattr(self, '_sid', None)
            user_id = getattr(self, '_user_id', None)
            if not user_id:
                try:
                    from app import nse_ema_strategies
                    for s_id, ent in nse_ema_strategies.items():
                        if ent.get('strategy') is self:
                            user_id = ent.get('user_id')
                            self._user_id = user_id
                            sid = s_id
                            self._sid = sid
                            break
                except Exception:
                    pass
            if user_id and sid:
                save_pnl_snapshot(user_id, sid, round(total_pnl, 2))
        except Exception:
            pass

    def _persist_state(self):
        """Save state to DB for resume on restart."""
        try:
            from models import update_strategy_db
            sid = getattr(self, '_sid', None)
            if not sid:
                try:
                    from app import nse_ema_strategies
                    for s_id, entry in nse_ema_strategies.items():
                        if entry.get('strategy') is self:
                            sid = s_id
                            self._sid = sid
                            break
                except Exception:
                    pass
            if not sid:
                return

            legs_data = []
            with self._legs_lock:
                for leg in self.legs:
                    legs_data.append({k: v for k, v in leg.items()})

            details = {
                'symbol': self.symbol, 'lots': self.lots,
                'lot_size': self.lot_size, 'quantity': self.quantity,
                'ema_period': self.ema_period,
                'sell_delta': self.sell_delta, 'buy_delta': self.buy_delta,
                'tp_pct': int(self.tp_pct * 100),
                'sl_pct': int(self.sl_pct * 100),
                'monitoring_interval': self.monitor_interval,
                'entry_hour': self.entry_hour, 'entry_minute': self.entry_minute,
                'exit_hour': self.exit_hour, 'exit_minute': self.exit_minute,
                'trading_days': self.trading_days,
                'paper_trade': self.paper_trade,
                'cumulative_pnl': self.cumulative_pnl,
                'total_days_traded': self.total_days_traded,
                'trade_log': self.trade_log[-50:],
            }
            update_strategy_db(sid, details=details, legs=legs_data,
                               pnl=round(self.pnl, 2))
        except Exception as e:
            logger.warning(f"[NSE EMA] Persist failed: {e}")
