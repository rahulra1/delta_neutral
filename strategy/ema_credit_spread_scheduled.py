"""EMA Credit Spread (Scheduled) — day-selectable recurring credit spread.

Same logic as EMA Credit Spread, but you choose WHICH weekdays it runs instead
of trading every day. On a selected day at entry time (default 6:30 PM IST):
1. Check 1D candles, compute EMA(period)
2. If price < EMA → bearish → sell sellΔ call, buy buyΔ call (bear call spread)
3. If price > EMA → bullish → sell sellΔ put,  buy buyΔ put  (bull put spread)
4. Expiry: nearest available >= min_expiry_days out
5. TP: tp_pct of net premium | SL: sl_pct of net premium
6. On a NON-selected weekday, it skips and waits for the next day.

trading_days: list of weekday ints (0=Mon .. 6=Sun). Default = all weekdays.
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from api.chart import get_candles, calc_ema
from api.chain import get_expiries
from api.option_chain import get_option_chain
from api.delta_finder import find_target_delta_options
from api.orders import place_order
from api.pricing import get_current_price
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

ENTRY_HOUR = 18
ENTRY_MINUTE = 30
SELL_DELTA = 0.20
BUY_DELTA = 0.10
EMA_PERIOD = 14
TP_PCT = 0.90
SL_PCT = 1.00
LOT_SIZE = 100
MONITOR_INTERVAL = 5
MIN_EXPIRY_DAYS = 8
DEFAULT_TRADING_DAYS = [0, 1, 2, 3, 4]   # 0=Mon .. 6=Sun

_DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


class EMACreditSpreadScheduled(BaseStrategy):
    """EMA credit spread that trades only on selected weekdays (trading_days)."""

    def __init__(self, asset='BTC', lot_size=LOT_SIZE, sell_delta=SELL_DELTA,
                 buy_delta=BUY_DELTA, ema_period=EMA_PERIOD, tp_pct=TP_PCT,
                 sl_pct=SL_PCT, monitor_interval=MONITOR_INTERVAL,
                 entry_hour=ENTRY_HOUR, entry_minute=ENTRY_MINUTE,
                 min_expiry_days=MIN_EXPIRY_DAYS, trading_days=None):
        self.asset = asset
        self.lot_size = lot_size
        self.sell_delta = sell_delta
        self.buy_delta = buy_delta
        self.ema_period = ema_period
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.monitor_interval = monitor_interval
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute
        self.min_expiry_days = min_expiry_days
        # Weekdays this strategy opens a trade (0=Mon .. 6=Sun).
        self.trading_days = trading_days if trading_days is not None else DEFAULT_TRADING_DAYS

        self._running = False
        self.legs = []
        self.net_premium = 0.0
        self._pnl = 0.0
        self.total_days_traded = 0
        self.cumulative_pnl = 0.0
        self.trade_log = []
        self._sid = None
        self._pnl_history = []
        self._legs_lock = threading.Lock()
        self._snap_counter = 0
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10
        self._base_params = {
            'asset': asset, 'lot_size': lot_size, 'sell_delta': sell_delta,
            'buy_delta': buy_delta, 'ema_period': ema_period,
            'tp_pct': int(tp_pct * 100), 'sl_pct': int(sl_pct * 100),
            'monitoring_interval': monitor_interval,
            'entry_hour': entry_hour, 'entry_minute': entry_minute,
            'min_expiry_days': min_expiry_days,
            'trading_days': self.trading_days,
        }

    def initialize(self):
        self._running = True
        days_str = ','.join(_DAY_NAMES[d] for d in self.trading_days)
        print(f"[EMA Spread Sched] Started | {self.entry_hour}:{self.entry_minute:02d} IST | Days: {days_str}")
        print(f"[EMA Spread Sched] EMA{self.ema_period} | Sell {self.sell_delta}Δ / Buy {self.buy_delta}Δ | "
              f"TP: {self.tp_pct*100:.0f}% | SL: {self.sl_pct*100:.0f}%")
        return True

    def monitor(self):
        """Main loop — waits for the next entry time, and only opens a trade if
        today is one of the selected trading_days; otherwise skips to next day."""
        while self._running:
            self._wait_for_next_entry()
            if not self._running:
                break

            now = datetime.now(IST)
            if now.weekday() not in self.trading_days:
                print(f"[EMA Spread Sched] Skipping {_DAY_NAMES[now.weekday()]} — not a selected trading day")
                continue

            self.total_days_traded += 1
            day_num = self.total_days_traded
            tag = f"[EMA Sched Day{day_num}]"

            print(f"\n{tag} ═══ {datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST ═══")
            day_legs, day_premium, direction = self._open_daily_trade(tag, day_num)
            if not day_legs:
                print(f"{tag} No trade today")
                continue

            t = threading.Thread(target=self._monitor_day_trade,
                                 args=(day_legs, day_premium, day_num, direction), daemon=True)
            t.start()

    def close_all(self):
        self._running = False
        with self._legs_lock:
            legs_copy = list(self.legs)
        max_close_attempts = 3
        for attempt in range(1, max_close_attempts + 1):
            if not legs_copy:
                break
            remaining = []
            for leg in legs_copy:
                close_side = 'buy' if leg['side'] == 'sell' else 'sell'
                try:
                    result = place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
                except Exception as e:
                    result = None
                    logger.warning(f"[EMA Spread Sched] Failed to close leg {leg.get('symbol')}: {e}")
                if result:
                    with self._legs_lock:
                        if leg in self.legs:
                            self.legs.remove(leg)
                else:
                    remaining.append(leg)
            legs_copy = remaining
            if legs_copy and attempt < max_close_attempts:
                logger.warning(f"[EMA Spread Sched] {len(legs_copy)} leg(s) failed to close "
                               f"(attempt {attempt}/{max_close_attempts}) — retrying")
                time.sleep(2 ** attempt)
        if legs_copy:
            logger.error(f"[EMA Spread Sched] ✗ {len(legs_copy)} leg(s) could NOT be closed after "
                         f"{max_close_attempts} attempts — leaving them under tracking: "
                         f"{[l.get('symbol') for l in legs_copy]}")
        try:
            self._persist_state()
        except Exception:
            pass

    @property
    def pnl(self):
        from config import get_contract_value
        cv = get_contract_value(self.asset)
        open_pnl = 0.0
        for leg in self.legs:
            data = get_current_price(leg['product_id'], self.asset)
            if data:
                if leg['side'] == 'sell':
                    open_pnl += (leg['entry_price'] - data['mark_price']) * leg['size'] * cv
                else:
                    open_pnl += (data['mark_price'] - leg['entry_price']) * leg['size'] * cv
        return self.cumulative_pnl + open_pnl

    # --- Daily trade ---

    def _open_daily_trade(self, tag, day_num):
        """Check EMA, place spread. Returns (legs, premium, direction) or ([], 0, '')."""
        candles = get_candles(self.asset, '1d')
        if not candles or len(candles) < self.ema_period + 1:
            print(f"{tag} ✗ Not enough candle data")
            return [], 0, ''

        ema_values = calc_ema(candles, self.ema_period)
        if not ema_values:
            return [], 0, ''

        current_price = candles[-1]['c']
        ema_current = ema_values[-1]['value']
        bearish = current_price < ema_current
        direction = 'bear_call' if bearish else 'bull_put'
        print(f"{tag} Price: {current_price} | EMA{self.ema_period}: {ema_current} | {'BEARISH' if bearish else 'BULLISH'}")

        expiries = get_expiries(self.asset, min_days=self.min_expiry_days)
        if not expiries:
            return [], 0, ''
        expiry = expiries[0]
        print(f"{tag} Expiry: {expiry}")

        chain = get_option_chain(expiry, self.asset)
        if not chain:
            return [], 0, ''

        sell_call, sell_put = find_target_delta_options(chain, self.sell_delta, 0.05)
        buy_call, buy_put = find_target_delta_options(chain, self.buy_delta, 0.05)

        if bearish:
            sell_leg, buy_leg, opt_type = sell_call, buy_call, 'call'
        else:
            sell_leg, buy_leg, opt_type = sell_put, buy_put, 'put'

        if not sell_leg or not buy_leg or sell_leg['product_id'] == buy_leg['product_id']:
            return [], 0, ''

        sell_result = place_order(sell_leg['product_id'], sell_leg['symbol'], self.lot_size, 'sell')
        if not sell_result:
            return [], 0, ''

        buy_result = place_order(buy_leg['product_id'], buy_leg['symbol'], self.lot_size, 'buy')
        if not buy_result:
            place_order(sell_leg['product_id'], sell_leg['symbol'], self.lot_size, 'buy')
            return [], 0, ''

        day_legs = [
            {'symbol': sell_leg['symbol'], 'product_id': sell_leg['product_id'],
             'side': 'sell', 'type': opt_type, 'delta': sell_leg['delta'],
             'strike': sell_leg['strike_price'], 'entry_price': sell_leg['mark_price'],
             'size': self.lot_size, 'day_num': day_num,
             'opened_at': datetime.now(IST).strftime('%Y-%m-%d')},
            {'symbol': buy_leg['symbol'], 'product_id': buy_leg['product_id'],
             'side': 'buy', 'type': opt_type, 'delta': buy_leg['delta'],
             'strike': buy_leg['strike_price'], 'entry_price': buy_leg['mark_price'],
             'size': self.lot_size, 'day_num': day_num,
             'opened_at': datetime.now(IST).strftime('%Y-%m-%d')},
        ]

        from config import get_contract_value
        cv = get_contract_value(self.asset)
        premium = (sell_leg['mark_price'] - buy_leg['mark_price']) * self.lot_size * cv

        print(f"{tag} ✓ SELL {opt_type.upper()} {sell_leg['strike_price']} (Δ{sell_leg['delta']:.2f}) @ {sell_leg['mark_price']}")
        print(f"{tag} ✓ BUY  {opt_type.upper()} {buy_leg['strike_price']} (Δ{buy_leg['delta']:.2f}) @ {buy_leg['mark_price']}")
        print(f"{tag} Net premium: ${premium:.4f} | TP: ${premium*self.tp_pct:.4f} | SL: -${premium*self.sl_pct:.4f}")

        with self._legs_lock:
            self.legs.extend(day_legs)
        self._persist_state()
        return day_legs, premium, direction

    def _monitor_day_trade(self, day_legs, premium, day_num, direction):
        """Monitor a single day's spread in its own thread."""
        from config import get_contract_value, set_thread_credentials
        if hasattr(self, '_api_key') and self._api_key:
            set_thread_credentials(self._api_key, self._api_secret, self._broker)
        if hasattr(self, '_log_queue') and self._log_queue:
            from app import LogCapture
            LogCapture._local.log_queue = self._log_queue
            LogCapture._local.log_history = self._log_history
        cv = get_contract_value(self.asset)
        target = premium * self.tp_pct
        sl = premium * self.sl_pct
        cycle = 0

        opened_date = ''
        for leg in day_legs:
            if leg.get('opened_at'):
                opened_date = leg['opened_at']
                break
        day_label = f"[EMA Sched Day{day_num} ({opened_date})]" if opened_date else f"[EMA Sched Day{day_num}]"

        while self._running:
            time.sleep(self.monitor_interval)
            cycle += 1

            pnl = 0.0
            leg_details = []
            all_legs_ok = True
            for leg in day_legs:
                data = get_current_price(leg['product_id'], self.asset)
                if not data:
                    all_legs_ok = False
                    leg_details.append(f"{leg.get('symbol', '?')}: no data")
                    continue
                mark = data['mark_price']
                if leg['side'] == 'sell':
                    leg_pnl = (leg['entry_price'] - mark) * leg['size'] * cv
                else:
                    leg_pnl = (mark - leg['entry_price']) * leg['size'] * cv
                pnl += leg_pnl
                leg['current_mark'] = round(mark, 4)
                leg['current_pnl'] = round(leg_pnl, 4)
                leg_details.append(f"{leg['side'].upper()} {leg['strike']}: ${leg_pnl:+.4f}")

            if not all_legs_ok:
                self._consecutive_failures += 1
                print(f"{day_label} ⚠ Price fetch failed ({self._consecutive_failures}/{self._max_consecutive_failures})")
                if self._consecutive_failures >= self._max_consecutive_failures:
                    print(f"{day_label} 🚨 EMERGENCY: {self._consecutive_failures} consecutive failures — closing legs")
                    still_open = self._close_day_legs(day_legs)
                    if still_open:
                        print(f"{day_label} ⚠ {len(still_open)} leg(s) still open after close attempt — "
                              f"continuing to monitor and retry")
                        continue
                    self._record_day(day_num, pnl, premium, 'api_failure', direction)
                    return
                continue
            self._consecutive_failures = 0

            now_iso = datetime.now(IST).isoformat()
            self._pnl_history.append((now_iso, round(self.cumulative_pnl + pnl, 4)))
            if len(self._pnl_history) > 500:
                self._pnl_history = self._pnl_history[-500:]

            self._snap_counter += 1
            if self._snap_counter % 6 == 0 and self._sid:
                try:
                    from models import save_pnl_snapshot
                    user_id = getattr(self, '_user_id', None)
                    if not user_id:
                        try:
                            from app import ema_spread_scheduled_strategies
                            for s_id, entry in ema_spread_scheduled_strategies.items():
                                if entry.get('strategy') is self:
                                    user_id = entry.get('user_id')
                                    self._user_id = user_id
                                    break
                        except Exception:
                            pass
                    if user_id:
                        save_pnl_snapshot(user_id, self._sid, round(self.cumulative_pnl + pnl, 4))
                except Exception:
                    pass

            legs_str = ' | '.join(leg_details) if leg_details else ''
            print(f"{day_label} PnL: ${pnl:+.4f} ({pnl/premium*100:+.1f}%) | Cum: ${self.cumulative_pnl:+.4f} | {legs_str}")

            if pnl >= target:
                print(f"{day_label} 🎯 TP hit: ${pnl:.4f}")
                still_open = self._close_day_legs(day_legs)
                if still_open:
                    print(f"{day_label} ⚠ TP close incomplete — {len(still_open)} leg(s) still open; "
                          f"keeping under monitoring and retrying")
                    continue
                self._record_day(day_num, pnl, premium, 'target', direction)
                return
            if pnl <= -sl:
                print(f"{day_label} 🛑 SL hit: ${pnl:.4f}")
                still_open = self._close_day_legs(day_legs)
                if still_open:
                    print(f"{day_label} ⚠ SL close incomplete — {len(still_open)} leg(s) still open; "
                          f"keeping under monitoring and retrying")
                    continue
                self._record_day(day_num, pnl, premium, 'stoploss', direction)
                return

    def _close_day_legs(self, day_legs):
        """Close each leg; a leg is only removed once its close order succeeds.
        Returns the list of legs that failed to close."""
        still_open = []
        for leg in list(day_legs):
            close_side = 'buy' if leg['side'] == 'sell' else 'sell'
            try:
                result = place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
            except Exception as e:
                result = None
                logger.warning(f"[EMA Spread Sched] Close order raised for {leg.get('symbol')}: {e}")
            if result:
                with self._legs_lock:
                    if leg in self.legs:
                        self.legs.remove(leg)
                if leg in day_legs:
                    day_legs.remove(leg)
            else:
                logger.warning(f"[EMA Spread Sched] ✗ Close FAILED for {leg.get('symbol')} — keeping under monitoring")
                still_open.append(leg)
        return still_open

    def _record_day(self, day_num, pnl, premium, exit_reason, direction):
        self.cumulative_pnl += pnl
        self.trade_log.append({
            'date': datetime.now(IST).strftime('%Y-%m-%d'),
            'day': day_num,
            'pnl': round(pnl, 4),
            'premium': round(premium, 4),
            'exit_reason': exit_reason,
            'direction': direction,
        })
        print(f"[EMA Sched D{day_num}] Closed | PnL: ${pnl:+.4f} | Cumulative: ${self.cumulative_pnl:+.4f}")
        self._persist_state()

    # --- Persistence ---

    def _persist_state(self):
        try:
            from models import update_strategy_db
            sid = getattr(self, '_sid', None)
            if not sid:
                try:
                    from app import ema_spread_scheduled_strategies
                    for s_id, entry in ema_spread_scheduled_strategies.items():
                        if entry.get('strategy') is self:
                            sid = s_id
                            self._sid = sid
                            break
                except Exception:
                    pass
            if not sid:
                return
            base_params = getattr(self, '_base_params', {
                'asset': self.asset, 'lot_size': self.lot_size,
                'sell_delta': self.sell_delta, 'buy_delta': self.buy_delta,
                'ema_period': self.ema_period,
                'tp_pct': int(self.tp_pct * 100), 'sl_pct': int(self.sl_pct * 100),
                'monitoring_interval': self.monitor_interval,
                'entry_hour': self.entry_hour, 'entry_minute': self.entry_minute,
                'min_expiry_days': self.min_expiry_days,
                'trading_days': self.trading_days,
            })
            details = {**base_params,
                       'trade_log': self.trade_log,
                       'cumulative_pnl': self.cumulative_pnl,
                       'total_days_traded': self.total_days_traded}
            legs_data = []
            for leg in self.legs:
                legs_data.append({
                    'symbol': leg.get('symbol', ''),
                    'product_id': leg.get('product_id'),
                    'side': leg.get('side', ''),
                    'type': leg.get('type', ''),
                    'delta': leg.get('delta', 0),
                    'strike': leg.get('strike', 0),
                    'entry_price': leg.get('entry_price', 0),
                    'size': leg.get('size', 0),
                    'day_num': leg.get('day_num', 0),
                    'opened_at': leg.get('opened_at', ''),
                })
            update_strategy_db(sid, details=details, legs=legs_data,
                               pnl=round(self.cumulative_pnl, 4))
            logger.debug(f"[EMA Spread Sched] State persisted: {self.total_days_traded} days, ${self.cumulative_pnl:.4f}")
        except Exception as e:
            logger.warning(f"[EMA Spread Sched] Failed to persist state: {e}")

    def restore_state(self, details=None, legs=None):
        """Rehydrate cumulative pnl / trade log / open legs from a persisted row."""
        details = details or {}
        self.cumulative_pnl = float(details.get('cumulative_pnl', 0) or 0)
        self.total_days_traded = int(details.get('total_days_traded', 0) or 0)
        self.trade_log = details.get('trade_log', []) or []
        restored = []
        for l in (legs or []):
            restored.append({
                'symbol': l.get('symbol', ''), 'product_id': l.get('product_id'),
                'side': l.get('side', ''), 'type': l.get('type', ''),
                'delta': l.get('delta', 0), 'strike': l.get('strike', 0),
                'entry_price': float(l.get('entry_price', 0) or 0),
                'size': int(l.get('size', 0) or 0),
                'day_num': l.get('day_num', 0), 'opened_at': l.get('opened_at', ''),
            })
        with self._legs_lock:
            self.legs = restored
        logger.info(f"[EMA Spread Sched] Restored {len(restored)} leg(s) | Cum PnL ${self.cumulative_pnl:.4f}")
        return self

    def resume_monitoring(self):
        """Start a TP/SL monitor thread for each restored open spread.

        After a restart, restore_state() reloads legs but nothing watches them.
        Group legs by day_num (a spread = sell + buy) and spawn one
        _monitor_day_trade thread per group, same as a freshly opened trade.
        Returns the number of monitor threads started.
        """
        from config import get_contract_value
        cv = get_contract_value(self.asset)
        with self._legs_lock:
            legs = list(self.legs)

        groups = {}
        legacy = []
        for leg in legs:
            dn = leg.get('day_num', 0) or 0
            if dn > 0:
                groups.setdefault(dn, []).append(leg)
            else:
                legacy.append(leg)
        # Legacy legs without a day_num: pair each sell with a same-type buy
        buys = [l for l in legacy if l.get('side') == 'buy']
        neg = 0
        for sl in [l for l in legacy if l.get('side') == 'sell']:
            neg -= 1
            pair = [sl]
            for bl in buys:
                if bl.get('type') == sl.get('type'):
                    pair.append(bl)
                    buys.remove(bl)
                    break
            groups[neg] = pair
        for bl in buys:
            neg -= 1
            groups[neg] = [bl]

        started = 0
        for day_num, day_legs in sorted(groups.items()):
            sells = [l for l in day_legs if l['side'] == 'sell']
            buys_ = [l for l in day_legs if l['side'] == 'buy']
            premium = (sum(l['entry_price'] for l in sells)
                       - sum(l['entry_price'] for l in buys_)) * self.lot_size * cv
            if premium <= 0:
                logger.warning(f"[EMA Spread Sched] Not monitoring group {day_num}: "
                               f"non-positive premium {premium:.4f} (incomplete spread?)")
                continue
            direction = 'bear_call' if any(l.get('type') == 'call' for l in day_legs) else 'bull_put'
            threading.Thread(target=self._monitor_day_trade,
                             args=(day_legs, premium, day_num, direction), daemon=True).start()
            started += 1
            print(f"[EMA Spread Sched] Resumed monitoring spread #{day_num} "
                  f"({len(day_legs)} legs, premium ${premium:.4f})")
        return started

    # --- Timing ---

    def _wait_for_next_entry(self):
        now = datetime.now(IST)
        entry_today = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)
        if now < entry_today:
            target = entry_today
        else:
            target = entry_today + timedelta(days=1)
        wait = (target - now).total_seconds()
        if wait > 60:
            print(f"[EMA Spread Sched] Next check at {target.strftime('%Y-%m-%d %H:%M')} IST ({wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))
