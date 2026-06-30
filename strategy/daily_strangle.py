"""0DTE BTC Short Strangle — daily recurring.

Entry: 9:00 AM IST — sell ~$100 premium call + ~$100 premium put (nearest match).
Stop Loss: 105% of entry premium per leg (independent).
Exit: 5:15 PM IST — square off any surviving legs.
Repeats daily.
"""

import time
import threading
from datetime import datetime, timedelta, timezone
from api.chain import get_expiries, get_option_chain_full
from api.orders import place_order
from api.pricing import get_current_price
from config import get_contract_value
from strategy.base import BaseStrategy

IST = timezone(timedelta(hours=5, minutes=30))

TARGET_PREMIUM = 100  # $100 per leg
SL_PCT = 2.00        # 200% of entry price = SL trigger (loss = 100% of premium)
ENTRY_HOUR = 9
ENTRY_MINUTE = 0
EXIT_HOUR = 17
EXIT_MINUTE = 15
MONITOR_INTERVAL = 10


class DailyStrangle(BaseStrategy):
    """0DTE short strangle: sell $100 call + $100 put at 9 AM, exit by 5:15 PM IST."""

    def __init__(self, asset='BTC', lot_size=100, target_premium=TARGET_PREMIUM,
                 sl_pct=SL_PCT, entry_hour=ENTRY_HOUR, entry_minute=ENTRY_MINUTE,
                 exit_hour=EXIT_HOUR, exit_minute=EXIT_MINUTE,
                 monitor_interval=MONITOR_INTERVAL):
        self.asset = asset
        self.lot_size = lot_size
        self.target_premium = target_premium
        self.sl_pct = sl_pct
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute
        self.exit_hour = exit_hour
        self.exit_minute = exit_minute
        self.monitor_interval = monitor_interval

        self.legs = []  # active legs for current day
        self._legs_lock = threading.Lock()
        self._pnl = 0.0
        self.cumulative_pnl = 0.0
        self.total_days_traded = 0
        self.trade_log = []
        self._running = False
        self._active_threads = []

    def initialize(self):
        self._running = True
        print(f"[Strangle] 0DTE Short Strangle started | Entry: {self.entry_hour}:{self.entry_minute:02d} IST | Exit: {self.exit_hour}:{self.exit_minute:02d} IST")
        print(f"[Strangle] Premium target: ${self.target_premium}/leg | SL: {self.sl_pct*100:.0f}% per leg")
        return True

    def monitor(self):
        while self._running:
            self._wait_for_next_entry()
            if not self._running:
                break

            self.total_days_traded += 1
            day_num = self.total_days_traded
            tag = f"[Strangle D{day_num}]"
            print(f"\n{tag} ═══ {datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST ═══")

            day_legs = self._open_strangle(tag)
            if not day_legs:
                print(f"{tag} No trade today")
                continue

            t = threading.Thread(target=self._monitor_day,
                                 args=(day_legs, day_num), daemon=True)
            t.start()
            self._active_threads.append(t)

    def _open_strangle(self, tag):
        """Sell call + put nearest to target premium on today's 0DTE expiry."""
        expiries = get_expiries(self.asset, min_days=0)
        if not expiries:
            print(f"{tag} No expiries found")
            return []
        expiry = expiries[0]

        chain, spot, _ = get_option_chain_full(expiry, self.asset)
        if not chain or not spot:
            print(f"{tag} Chain fetch failed")
            return []

        call_opt = self._find_nearest_premium(chain, 'call', spot)
        put_opt = self._find_nearest_premium(chain, 'put', spot)

        if not call_opt or not put_opt:
            print(f"{tag} Could not find suitable options")
            return []

        print(f"{tag} Spot: ${spot:.0f} | Call: {call_opt['strike']} @ ${call_opt['mark_price']:.2f} | Put: {put_opt['strike']} @ ${put_opt['mark_price']:.2f}")

        day_legs = []
        for opt, opt_type in [(call_opt, 'call'), (put_opt, 'put')]:
            result = place_order(opt['product_id'], opt['symbol'], self.lot_size, 'sell')
            if result:
                leg = {
                    'symbol': opt['symbol'],
                    'product_id': opt['product_id'],
                    'side': 'sell',
                    'strike': opt['strike'],
                    'type': opt_type,
                    'entry_price': opt['mark_price'],
                    'size': self.lot_size,
                    'sl_price': opt['mark_price'] * self.sl_pct,
                    'stopped': False,
                }
                day_legs.append(leg)
                print(f"{tag} ✓ SOLD {opt_type.upper()} {opt['strike']} @ ${opt['mark_price']:.2f} | SL: ${leg['sl_price']:.2f}")
            else:
                print(f"{tag} ✗ Failed to sell {opt_type.upper()}")

        if not day_legs:
            return []

        with self._legs_lock:
            self.legs.extend(day_legs)
        return day_legs

    def _monitor_day(self, day_legs, day_num):
        """Monitor legs until SL hit or exit time. Independent SL per leg."""
        if hasattr(self, '_api_key') and self._api_key:
            from config import set_thread_credentials
            set_thread_credentials(self._api_key, self._api_secret, self._broker)
        if hasattr(self, '_log_queue') and self._log_queue:
            from app import LogCapture
            LogCapture._local.log_queue = self._log_queue
            LogCapture._local.log_history = self._log_history

        cv = get_contract_value(self.asset)
        tag = f"[Strangle D{day_num}]"

        while self._running:
            now = datetime.now(IST)
            # Exit time check
            if now.hour > self.exit_hour or (now.hour == self.exit_hour and now.minute >= self.exit_minute):
                print(f"{tag} ⏰ Exit time reached — closing surviving legs")
                self._close_day_legs(day_legs)
                break

            # Check each active leg for SL
            for leg in day_legs:
                if leg['stopped']:
                    continue
                data = get_current_price(leg['product_id'], self.asset)
                if not data:
                    continue
                current = data['mark_price']
                if current >= leg['sl_price']:
                    # SL hit — close this leg
                    print(f"{tag} 🛑 {leg['type'].upper()} SL hit: ${current:.2f} >= ${leg['sl_price']:.2f}")
                    place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
                    leg['stopped'] = True
                    leg['exit_price'] = current

            # All legs stopped — done early
            if all(l['stopped'] for l in day_legs):
                print(f"{tag} Both legs stopped")
                break

            time.sleep(self.monitor_interval)

        # Calculate day PnL
        day_pnl = 0.0
        for leg in day_legs:
            exit_p = leg.get('exit_price')
            if exit_p is None:
                # Was closed at exit time — get final price
                data = get_current_price(leg['product_id'], self.asset)
                exit_p = data['mark_price'] if data else leg['entry_price']
                leg['exit_price'] = exit_p
            day_pnl += (leg['entry_price'] - exit_p) * leg['size'] * cv

        self.cumulative_pnl += day_pnl
        self._pnl = day_pnl

        # Remove from shared legs
        with self._legs_lock:
            for leg in day_legs:
                if leg in self.legs:
                    self.legs.remove(leg)

        self.trade_log.append({
            'date': datetime.now(IST).strftime('%Y-%m-%d'),
            'day': day_num,
            'pnl': round(day_pnl, 2),
            'exit_reason': self._exit_reason(day_legs),
        })
        print(f"{tag} Done | PnL: ${day_pnl:+.2f} | Cumulative: ${self.cumulative_pnl:+.2f}")
        self._persist_state()

    def _close_day_legs(self, day_legs):
        """Close all non-stopped legs."""
        for leg in day_legs:
            if leg['stopped']:
                continue
            data = get_current_price(leg['product_id'], self.asset)
            leg['exit_price'] = data['mark_price'] if data else leg['entry_price']
            place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
            leg['stopped'] = True

    def _find_nearest_premium(self, chain, opt_type, spot):
        """Find OTM option closest to target premium."""
        best = None
        best_diff = float('inf')
        for row in chain:
            opt = row.get(opt_type)
            if not opt or opt['mark_price'] <= 0:
                continue
            strike = float(row['strike'])
            # OTM filter
            if opt_type == 'call' and strike <= spot:
                continue
            if opt_type == 'put' and strike >= spot:
                continue
            diff = abs(opt['mark_price'] - self.target_premium)
            if diff < best_diff:
                best_diff = diff
                best = opt
        return best

    def _exit_reason(self, day_legs):
        both_stopped = all(l['stopped'] and l.get('exit_price', 0) >= l['sl_price'] * 0.99 for l in day_legs)
        if both_stopped:
            return 'both_sl'
        any_sl = any(l.get('exit_price', 0) >= l['sl_price'] * 0.99 for l in day_legs)
        if any_sl:
            return 'one_sl'
        return 'eod_exit'

    def close_all(self):
        self._running = False
        with self._legs_lock:
            for leg in list(self.legs):
                try:
                    place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
                except Exception as e:
                    logger.warning(f"[Strangle] Failed to close leg {leg.get('symbol')}: {e}")
            self.legs.clear()
        try:
            self._persist_state()
        except Exception:
            pass

    @property
    def pnl(self):
        cv = get_contract_value(self.asset)
        open_pnl = 0.0
        with self._legs_lock:
            for leg in list(self.legs):
                if leg.get('stopped'):
                    continue
                data = get_current_price(leg['product_id'], self.asset)
                if data:
                    open_pnl += (leg['entry_price'] - data['mark_price']) * leg['size'] * cv
        return self.cumulative_pnl + open_pnl

    def _persist_state(self):
        """Save state to DB so it survives server restarts."""
        try:
            from models import update_strategy_db
            import json
            sid = getattr(self, '_sid', None)
            if not sid:
                try:
                    from app import strangle_strategies
                    for s_id, entry in strangle_strategies.items():
                        if entry.get('strategy') is self:
                            sid = s_id
                            self._sid = sid
                            break
                except Exception:
                    pass
            if not sid:
                return
            details = {
                'asset': self.asset, 'lot_size': self.lot_size,
                'target_premium': self.target_premium,
                'sl_pct': int(self.sl_pct * 100),
                'entry_hour': self.entry_hour, 'entry_minute': self.entry_minute,
                'exit_hour': self.exit_hour, 'exit_minute': self.exit_minute,
                'monitoring_interval': self.monitor_interval,
                'cumulative_pnl': self.cumulative_pnl,
                'total_days_traded': self.total_days_traded,
                'trade_log': self.trade_log[-50:],
            }
            legs_data = []
            for leg in self.legs:
                legs_data.append({k: v for k, v in leg.items() if k != '_lock'})
            update_strategy_db(sid, details=details,
                               legs=legs_data,
                               pnl=round(self.cumulative_pnl, 2))
        except Exception as e:
            logger.warning(f"[Strangle] Persist state failed: {e}")

    def _wait_for_next_entry(self):
        now = datetime.now(IST)
        entry_today = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)
        if now < entry_today:
            target = entry_today
        else:
            target = entry_today + timedelta(days=1)
        wait = (target - now).total_seconds()
        if wait > 60:
            print(f"[Strangle] Next entry: {target.strftime('%Y-%m-%d %H:%M')} IST ({wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))
