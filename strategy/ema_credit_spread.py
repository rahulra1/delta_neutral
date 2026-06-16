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
MONITOR_INTERVAL = 30
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

    def initialize(self):
        self._running = True
        print(f"[EMA Spread] Started | {self.entry_hour}:{self.entry_minute:02d} IST daily")
        print(f"[EMA Spread] EMA{self.ema_period} | Sell {self.sell_delta}Δ / Buy {self.buy_delta}Δ | TP: {self.tp_pct*100:.0f}% | SL: {self.sl_pct*100:.0f}%")
        return True

    def monitor(self):
        """Main daily loop."""
        while self._running:
            self._wait_for_entry_time()
            if not self._running:
                break

            print(f"\n[EMA Spread] ═══ Day {self.total_days_traded + 1} | {datetime.now().strftime('%Y-%m-%d %H:%M')} ═══")
            success = self._take_daily_trade()
            if not success:
                print("[EMA Spread] No trade today — retrying tomorrow")
                self._sleep_until_tomorrow()
                continue

            exit_reason = self._monitor_until_exit()

            day_pnl = self._pnl
            self.cumulative_pnl += day_pnl
            self.total_days_traded += 1
            self.trade_log.append({
                'date': datetime.now().strftime('%Y-%m-%d'),
                'pnl': round(day_pnl, 2),
                'premium': round(self.net_premium, 4),
                'exit_reason': exit_reason,
                'direction': 'bear_call' if any(l['type'] == 'call' for l in self.legs) else 'bull_put',
            })
            print(f"[EMA Spread] Day done | PnL: ${day_pnl:+.4f} | Cumulative: ${self.cumulative_pnl:+.4f} | Days: {self.total_days_traded}")

            self.legs = []
            self.net_premium = 0.0
            self._pnl = 0.0
            self._sleep_until_tomorrow()

    def close_all(self):
        self._running = False
        self._close_legs()

    @property
    def pnl(self):
        return self.cumulative_pnl + self._pnl

    # --- Daily trade ---

    def _take_daily_trade(self):
        """Check EMA direction and place credit spread."""
        # 1. Get daily candles and EMA14
        candles = get_candles(self.asset, '1d')
        if not candles or len(candles) < self.ema_period + 1:
            print("[EMA Spread] ✗ Not enough candle data")
            return False

        ema_values = calc_ema(candles, self.ema_period)
        if not ema_values:
            print("[EMA Spread] ✗ EMA calculation failed")
            return False

        current_price = candles[-1]['c']
        ema_current = ema_values[-1]['value']
        bearish = current_price < ema_current

        direction = "BEARISH (price < EMA)" if bearish else "BULLISH (price > EMA)"
        print(f"[EMA Spread] Price: {current_price} | EMA{self.ema_period}: {ema_current} | {direction}")

        # 2. Get expiry ≥8 days out
        expiries = get_expiries(self.asset, min_days=self.min_expiry_days)
        if not expiries:
            print("[EMA Spread] ✗ No expiry found")
            return False
        expiry = expiries[0]
        print(f"[EMA Spread] Expiry: {expiry}")

        # 3. Get option chain and find legs
        chain = get_option_chain(expiry, self.asset)
        if not chain:
            print("[EMA Spread] ✗ Failed to fetch option chain")
            return False

        sell_call, sell_put = find_target_delta_options(chain, self.sell_delta, 0.05)
        buy_call, buy_put = find_target_delta_options(chain, self.buy_delta, 0.05)

        if bearish:
            # Bear call spread: sell 20Δ call, buy 10Δ call
            sell_leg, buy_leg = sell_call, buy_call
            opt_type = 'call'
        else:
            # Bull put spread: sell 20Δ put, buy 10Δ put
            sell_leg, buy_leg = sell_put, buy_put
            opt_type = 'put'

        if not sell_leg or not buy_leg:
            print(f"[EMA Spread] ✗ Could not find {opt_type} legs at required deltas")
            return False

        if sell_leg['product_id'] == buy_leg['product_id']:
            print(f"[EMA Spread] ✗ Sell and buy legs are the same option — skipping")
            return False

        # 4. Place orders: sell first, then buy
        sell_result = place_order(sell_leg['product_id'], sell_leg['symbol'], self.lot_size, 'sell')
        if not sell_result:
            print(f"[EMA Spread] ✗ Failed to sell {sell_leg['symbol']}")
            return False

        buy_result = place_order(buy_leg['product_id'], buy_leg['symbol'], self.lot_size, 'buy')
        if not buy_result:
            # Rollback sell
            place_order(sell_leg['product_id'], sell_leg['symbol'], self.lot_size, 'buy')
            print(f"[EMA Spread] ✗ Failed to buy hedge — rolled back sell")
            return False

        self.legs = [
            {'symbol': sell_leg['symbol'], 'product_id': sell_leg['product_id'],
             'side': 'sell', 'type': opt_type, 'delta': sell_leg['delta'],
             'strike': sell_leg['strike_price'], 'entry_price': sell_leg['mark_price'], 'size': self.lot_size},
            {'symbol': buy_leg['symbol'], 'product_id': buy_leg['product_id'],
             'side': 'buy', 'type': opt_type, 'delta': buy_leg['delta'],
             'strike': buy_leg['strike_price'], 'entry_price': buy_leg['mark_price'], 'size': self.lot_size},
        ]

        # Net premium = sold premium - bought premium
        from config import get_contract_value
        cv = get_contract_value(self.asset)
        self.net_premium = (sell_leg['mark_price'] - buy_leg['mark_price']) * self.lot_size * cv

        print(f"[EMA Spread] ✓ SELL {opt_type.upper()} {sell_leg['strike_price']} (Δ{sell_leg['delta']:.2f}) @ {sell_leg['mark_price']}")
        print(f"[EMA Spread] ✓ BUY  {opt_type.upper()} {buy_leg['strike_price']} (Δ{buy_leg['delta']:.2f}) @ {buy_leg['mark_price']}")
        print(f"[EMA Spread] Net premium: ${self.net_premium:.4f} | TP: ${self.net_premium*self.tp_pct:.4f} | SL: -${self.net_premium*self.sl_pct:.4f}")
        return True

    def _monitor_until_exit(self):
        """Monitor spread PnL until TP/SL."""
        target = self.net_premium * self.tp_pct
        sl = self.net_premium * self.sl_pct
        cycle = 0
        while self._running and self.legs:
            time.sleep(self.monitor_interval)
            self._update_pnl()
            cycle += 1

            if cycle % 10 == 0:
                print(f"[EMA Spread] PnL: ${self._pnl:+.4f} ({self._pnl/self.net_premium*100:+.1f}%) | TP: ${target:.4f} | SL: -${sl:.4f}")

            if self._pnl >= target:
                print(f"[EMA Spread] 🎯 TP hit: ${self._pnl:.4f} ({self._pnl/self.net_premium*100:.0f}%)")
                self._close_legs()
                return 'target'
            if self._pnl <= -sl:
                print(f"[EMA Spread] 🛑 SL hit: ${self._pnl:.4f} ({self._pnl/self.net_premium*100:.0f}%)")
                self._close_legs()
                return 'stoploss'
        return 'manual_stop'

    def _close_legs(self):
        for leg in self.legs:
            close_side = 'buy' if leg['side'] == 'sell' else 'sell'
            place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
        self.legs = []

    def _update_pnl(self):
        from config import get_contract_value
        cv = get_contract_value(self.asset)
        total = 0.0
        for leg in self.legs:
            data = get_current_price(leg['product_id'], self.asset)
            if data:
                current = data['mark_price']
                if leg['side'] == 'sell':
                    total += (leg['entry_price'] - current) * leg['size'] * cv
                else:
                    total += (current - leg['entry_price']) * leg['size'] * cv
        self._pnl = total

    # --- Timing ---

    def _wait_for_entry_time(self):
        now = datetime.now(IST)
        entry_time = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)
        if now >= entry_time:
            return  # already past entry time, trade now
        wait = (entry_time - now).total_seconds()
        print(f"[EMA Spread] Waiting until {entry_time.strftime('%H:%M')} IST ({wait/60:.0f}min)...")
        self._interruptible_sleep(wait)

    def _sleep_until_tomorrow(self):
        now = datetime.now(IST)
        tomorrow = (now + timedelta(days=1)).replace(
            hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)
        wait = (tomorrow - now).total_seconds()
        print(f"[EMA Spread] Next trade: {tomorrow.strftime('%Y-%m-%d %H:%M')} IST ({wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))
