"""EMA Credit Spread — Daily recurring strategy.

Every day at 6:30 PM IST:
1. Check 1D candles, compute EMA14
2. If price < EMA14 → bearish → sell 20Δ call, buy 10Δ call (bear call spread)
3. If price > EMA14 → bullish → sell 20Δ put, buy 10Δ put (bull put spread)
4. Expiry: nearest available ≥8 days out
5. TP: 70% of net premium collected | SL: 100% of net premium (loss = premium)
6. After exit, waits for next day 6:30 PM.
"""

import time
import logging
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


class EMACreditSpread(BaseStrategy):
    """Daily EMA-based credit spread. Start once, trades every day at 6:30 PM IST."""

    def __init__(self, asset='BTC', lot_size=LOT_SIZE, sell_delta=SELL_DELTA,
                 buy_delta=BUY_DELTA, ema_period=EMA_PERIOD, tp_pct=TP_PCT,
                 sl_pct=SL_PCT, monitor_interval=MONITOR_INTERVAL,
                 entry_hour=ENTRY_HOUR, entry_minute=ENTRY_MINUTE,
                 min_expiry_days=MIN_EXPIRY_DAYS):
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

        self._running = False
        self.legs = []  # [{symbol, product_id, side, type, delta, strike, entry_price, size}]
        self.net_premium = 0.0
        self._pnl = 0.0
        self.total_days_traded = 0
        self.cumulative_pnl = 0.0
        self.trade_log = []
        self._sid = None  # Set externally after creation for DB persistence
        self._base_params = {
            'asset': asset, 'lot_size': lot_size, 'sell_delta': sell_delta,
            'buy_delta': buy_delta, 'ema_period': ema_period,
            'tp_pct': int(tp_pct * 100), 'sl_pct': int(sl_pct * 100),
            'monitoring_interval': monitor_interval,
            'entry_hour': entry_hour, 'entry_minute': entry_minute,
            'min_expiry_days': min_expiry_days,
        }

    def initialize(self):
        self._running = True
        print(f"[EMA Spread] Started | {self.entry_hour}:{self.entry_minute:02d} IST daily")
        print(f"[EMA Spread] EMA{self.ema_period} | Sell {self.sell_delta}Δ / Buy {self.buy_delta}Δ | TP: {self.tp_pct*100:.0f}% | SL: {self.sl_pct*100:.0f}%")
        return True

    def monitor(self):
        """Main daily loop — spawns a new monitored trade each day."""
        import threading
        while self._running:
            self._wait_for_next_entry()
            if not self._running:
                break

            self.total_days_traded += 1
            day_num = self.total_days_traded
            tag = f"[EMA Day{day_num}]"

            print(f"\n{tag} ═══ {datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST ═══")
            day_legs, day_premium, direction = self._open_daily_trade(tag)
            if not day_legs:
                print(f"{tag} No trade today")
                continue

            t = threading.Thread(target=self._monitor_day_trade,
                                 args=(day_legs, day_premium, day_num, direction), daemon=True)
            t.start()

    def close_all(self):
        self._running = False
        for leg in list(self.legs):
            close_side = 'buy' if leg['side'] == 'sell' else 'sell'
            place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
        self.legs.clear()

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

    def _open_daily_trade(self, tag):
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
             'strike': sell_leg['strike_price'], 'entry_price': sell_leg['mark_price'], 'size': self.lot_size},
            {'symbol': buy_leg['symbol'], 'product_id': buy_leg['product_id'],
             'side': 'buy', 'type': opt_type, 'delta': buy_leg['delta'],
             'strike': buy_leg['strike_price'], 'entry_price': buy_leg['mark_price'], 'size': self.lot_size},
        ]

        from config import get_contract_value
        cv = get_contract_value(self.asset)
        premium = (sell_leg['mark_price'] - buy_leg['mark_price']) * self.lot_size * cv

        print(f"{tag} ✓ SELL {opt_type.upper()} {sell_leg['strike_price']} (Δ{sell_leg['delta']:.2f}) @ {sell_leg['mark_price']}")
        print(f"{tag} ✓ BUY  {opt_type.upper()} {buy_leg['strike_price']} (Δ{buy_leg['delta']:.2f}) @ {buy_leg['mark_price']}")
        print(f"{tag} Net premium: ${premium:.4f} | TP: ${premium*self.tp_pct:.4f} | SL: -${premium*self.sl_pct:.4f}")

        self.legs.extend(day_legs)
        self._persist_state()
        return day_legs, premium, direction

    def _monitor_day_trade(self, day_legs, premium, day_num, direction):
        """Monitor a single day's spread in its own thread."""
        from config import get_contract_value, set_thread_credentials
        # Set thread-local credentials for API calls
        if hasattr(self, '_api_key') and self._api_key:
            set_thread_credentials(self._api_key, self._api_secret, self._broker)
        # Route logs to the strategy's log queue
        if hasattr(self, '_log_queue') and self._log_queue:
            from app import LogCapture
            LogCapture._local.log_queue = self._log_queue
            LogCapture._local.log_history = self._log_history
        cv = get_contract_value(self.asset)
        target = premium * self.tp_pct
        sl = premium * self.sl_pct
        cycle = 0

        while self._running:
            time.sleep(self.monitor_interval)
            cycle += 1

            pnl = 0.0
            leg_details = []
            for leg in day_legs:
                data = get_current_price(leg['product_id'], self.asset)
                if data:
                    if leg['side'] == 'sell':
                        leg_pnl = (leg['entry_price'] - data['mark_price']) * leg['size'] * cv
                    else:
                        leg_pnl = (data['mark_price'] - leg['entry_price']) * leg['size'] * cv
                    pnl += leg_pnl
                    leg_details.append(f"{leg['side'].upper()} {leg['strike']}: ${leg_pnl:+.4f}")

            legs_str = ' | '.join(leg_details) if leg_details else ''
            print(f"[EMA Day{day_num}] PnL: ${pnl:+.4f} ({pnl/premium*100:+.1f}%) | Cum: ${self.cumulative_pnl:+.4f} | {legs_str}")

            if pnl >= target:
                print(f"[EMA Day{day_num}] 🎯 TP hit: ${pnl:.4f}")
                self._close_day_legs(day_legs)
                self._record_day(day_num, pnl, premium, 'target', direction)
                return
            if pnl <= -sl:
                print(f"[EMA Day{day_num}] 🛑 SL hit: ${pnl:.4f}")
                self._close_day_legs(day_legs)
                self._record_day(day_num, pnl, premium, 'stoploss', direction)
                return

    def _close_day_legs(self, day_legs):
        for leg in day_legs:
            close_side = 'buy' if leg['side'] == 'sell' else 'sell'
            place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
            if leg in self.legs:
                self.legs.remove(leg)

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
        print(f"[EMA D{day_num}] Closed | PnL: ${pnl:+.4f} | Cumulative: ${self.cumulative_pnl:+.4f}")
        # Persist state to DB so it survives server restarts
        self._persist_state()

    # --- Persistence ---

    def _persist_state(self):
        """Save trade_log, cumulative_pnl, total_days_traded, and legs to DB.
        This ensures data survives server restarts."""
        try:
            from models import update_strategy_db
            import json
            # Find sid if not set (for strategies started before this code change)
            sid = getattr(self, '_sid', None)
            if not sid:
                try:
                    from app import ema_spread_strategies
                    for s_id, entry in ema_spread_strategies.items():
                        if entry.get('strategy') is self:
                            sid = s_id
                            self._sid = sid
                            break
                except Exception:
                    pass
            if not sid:
                return
            # Build base params if not set
            base_params = getattr(self, '_base_params', {
                'asset': self.asset, 'lot_size': self.lot_size,
                'sell_delta': self.sell_delta, 'buy_delta': self.buy_delta,
                'ema_period': self.ema_period,
                'tp_pct': int(self.tp_pct * 100), 'sl_pct': int(self.sl_pct * 100),
                'monitoring_interval': self.monitor_interval,
                'entry_hour': self.entry_hour, 'entry_minute': self.entry_minute,
                'min_expiry_days': self.min_expiry_days,
            })
            details = {**base_params,
                       'trade_log': self.trade_log,
                       'cumulative_pnl': self.cumulative_pnl,
                       'total_days_traded': self.total_days_traded}
            # Serialize current legs
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
                })
            update_strategy_db(sid,
                               details=json.dumps(details),
                               legs=json.dumps(legs_data),
                               pnl=round(self.cumulative_pnl, 4))
            logger.debug(f"[EMA Spread] State persisted: {self.total_days_traded} days, ${self.cumulative_pnl:.4f}")
        except Exception as e:
            logger.warning(f"[EMA Spread] Failed to persist state: {e}")

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
            print(f"[EMA Spread] Next trade at {target.strftime('%Y-%m-%d %H:%M')} IST ({wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))
