"""NSE Daily Short Strangle — Indian market paper trading.

Entry: Configurable time (default 9:20 AM IST) on selected weekdays.
Sell OTM call + OTM put nearest to target premium (in ₹).
Stop Loss: Configurable % of entry premium per leg (independent).
Exit: Configurable time (default 3:15 PM IST) — square off surviving legs.
Re-entry: After SL hit, re-enter same side once with nearest premium option.
Market hours: 9:15 AM – 3:30 PM IST, Mon–Fri only.
Symbols: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY.
Paper trading: No real orders — tracks positions via NSE LTP data.
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from api.nse import get_nse_expiries, get_nse_chain
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


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
    return get_nse_expiries, get_nse_chain

# NSE lot sizes (as of 2025–2026)
NSE_LOT_SIZES = {
    'NIFTY': 65,
    'BANKNIFTY': 30,
    'FINNIFTY': 65,
    'MIDCPNIFTY': 50,
}

# Default parameters
TARGET_PREMIUM = 100       # ₹100 per leg
SL_PCT = 2.00             # 200% of entry price = SL trigger
ENTRY_HOUR = 9
ENTRY_MINUTE = 20
EXIT_HOUR = 15
EXIT_MINUTE = 15
MONITOR_INTERVAL = 15     # NSE data has 15s cache, no point polling faster
# Default: trade all weekdays (0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri)
DEFAULT_TRADING_DAYS = [0, 1, 2, 3, 4]


class NseDailyStrangle(BaseStrategy):
    """NSE short strangle: paper trade on Indian index options with configurable days."""

    def __init__(self, symbol='NIFTY', lots=1, target_premium=TARGET_PREMIUM,
                 sl_pct=SL_PCT, entry_hour=ENTRY_HOUR, entry_minute=ENTRY_MINUTE,
                 exit_hour=EXIT_HOUR, exit_minute=EXIT_MINUTE,
                 monitor_interval=MONITOR_INTERVAL, trading_days=None,
                 lot_size=None):
        self.symbol = symbol
        self.lots = lots
        self.lot_size = lot_size if lot_size else NSE_LOT_SIZES.get(symbol, 50)
        self.quantity = self.lots * self.lot_size  # total qty for PnL calc
        self.target_premium = target_premium
        self.sl_pct = sl_pct
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute
        self.exit_hour = exit_hour
        self.exit_minute = exit_minute
        self.monitor_interval = monitor_interval
        self.trading_days = trading_days if trading_days is not None else DEFAULT_TRADING_DAYS

        self.legs = []  # active legs for current day
        self._legs_lock = threading.Lock()
        self._pnl = 0.0
        self.cumulative_pnl = 0.0
        self.total_days_traded = 0
        self.trade_log = []
        self._running = False
        self._active_threads = []
        self._pnl_history = []
        self._snap_counter = 0
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10

    def initialize(self):
        self._running = True
        # Capture thread-local credentials so child threads can inherit them
        from config import get_api_key, get_api_secret, _thread_local
        self._api_key = get_api_key()
        self._api_secret = get_api_secret()
        self._broker = getattr(_thread_local, 'broker', '')
        days_str = ','.join(['Mon', 'Tue', 'Wed', 'Thu', 'Fri'][d] for d in self.trading_days)
        print(f"[NSE Strangle] {self.symbol} Paper Strangle started")
        print(f"[NSE Strangle] Entry: {self.entry_hour}:{self.entry_minute:02d} | Exit: {self.exit_hour}:{self.exit_minute:02d} IST")
        print(f"[NSE Strangle] Premium: ₹{self.target_premium}/leg | SL: {self.sl_pct*100:.0f}% | Lots: {self.lots} ({self.quantity} qty)")
        print(f"[NSE Strangle] Trading days: {days_str}")
        return True

    def _set_thread_credentials(self):
        """Propagate credentials to the current thread (for child threads)."""
        from config import set_thread_credentials
        if getattr(self, '_api_key', None):
            set_thread_credentials(self._api_key, self._api_secret, self._broker)

    def monitor(self):
        while self._running:
            self._wait_for_next_entry()
            if not self._running:
                break

            # Check if today is a trading day
            now = datetime.now(IST)
            if now.weekday() not in self.trading_days:
                print(f"[NSE Strangle] Skipping {now.strftime('%A')} — not a trading day")
                continue

            self.total_days_traded += 1
            day_num = self.total_days_traded
            tag = f"[NSE D{day_num}]"
            print(f"\n{tag} ═══ {now.strftime('%Y-%m-%d %H:%M')} IST ═══")

            day_legs = self._open_strangle(tag)
            if not day_legs:
                print(f"{tag} No trade today")
                continue

            t = threading.Thread(target=self._monitor_day,
                                 args=(day_legs, day_num), daemon=True)
            t.start()
            self._active_threads.append(t)

    def _open_strangle(self, tag):
        """Sell call + put nearest to target premium on nearest expiry."""
        _get_expiries, _get_chain = _get_data_source()
        expiries = _get_expiries(self.symbol)
        if not expiries:
            print(f"{tag} No expiries found")
            return []

        # Always pick the nearest expiry (includes today if it's expiry day)
        expiry = expiries[0]

        chain, spot, _ = _get_chain(self.symbol, expiry)
        if not chain or not spot:
            print(f"{tag} Chain fetch failed for {self.symbol} expiry {expiry}")
            return []

        call_opt = self._find_nearest_premium(chain, 'call', spot)
        put_opt = self._find_nearest_premium(chain, 'put', spot)

        if not call_opt or not put_opt:
            print(f"{tag} Could not find suitable options near ₹{self.target_premium}")
            return []

        print(f"{tag} Spot: ₹{spot:.0f} | Expiry: {expiry}")
        print(f"{tag} Call: {call_opt['strike']} @ ₹{call_opt['mark_price']:.2f} | Put: {put_opt['strike']} @ ₹{put_opt['mark_price']:.2f}")

        day_legs = []
        for opt, opt_type in [(call_opt, 'call'), (put_opt, 'put')]:
            leg = {
                'symbol': opt['symbol'],
                'product_id': opt.get('product_id'),  # None for NSE paper
                'side': 'sell',
                'strike': opt['strike'],
                'type': opt_type,
                'entry_price': opt['mark_price'],
                'size': self.quantity,
                'sl_price': opt['mark_price'] * self.sl_pct,
                'stopped': False,
                'expiry': expiry,
            }
            day_legs.append(leg)
            print(f"{tag} ✓ SOLD {opt_type.upper()} {opt['strike']} @ ₹{opt['mark_price']:.2f} | SL: ₹{leg['sl_price']:.2f}")

        with self._legs_lock:
            self.legs.extend(day_legs)
        self._persist_state()
        return day_legs

    def _monitor_day(self, day_legs, day_num):
        """Monitor legs until SL hit or exit time. Independent SL per leg with re-entry."""
        # Set up credentials for this thread
        self._set_thread_credentials()

        # Set up log routing for this thread
        if hasattr(self, '_log_queue') and self._log_queue:
            from app import LogCapture
            LogCapture._local.log_queue = self._log_queue
            LogCapture._local.log_history = self._log_history

        tag = f"[NSE D{day_num}]"
        reentry_used = {'call': False, 'put': False}

        while self._running:
            now = datetime.now(IST)

            # Market closed check (before 9:15 or after 15:30)
            if now.hour < 9 or (now.hour == 9 and now.minute < 15):
                time.sleep(30)
                continue
            if now.hour > 15 or (now.hour == 15 and now.minute > 30):
                print(f"{tag} ⏰ Market closed — closing surviving legs")
                self._close_day_legs(day_legs, tag)
                break

            # Exit time check
            if now.hour > self.exit_hour or (now.hour == self.exit_hour and now.minute >= self.exit_minute):
                print(f"{tag} ⏰ Exit time reached — closing surviving legs")
                self._close_day_legs(day_legs, tag)
                break

            # Get current prices from NSE chain
            expiry = day_legs[0].get('expiry')
            _get_expiries, _get_chain = _get_data_source()
            chain, spot, _ = _get_chain(self.symbol, expiry)
            if not chain:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._max_consecutive_failures:
                    print(f"{tag} 🚨 EMERGENCY: {self._consecutive_failures} consecutive failures — closing")
                    self._close_day_legs(day_legs, tag)
                    break
                time.sleep(self.monitor_interval)
                continue
            self._consecutive_failures = 0

            # Build strike→price lookup from chain
            price_map = {}
            for row in chain:
                strike = str(row['strike'])
                if row.get('call'):
                    price_map[('call', strike)] = row['call']['mark_price']
                if row.get('put'):
                    price_map[('put', strike)] = row['put']['mark_price']

            # Check each active leg
            for leg in day_legs:
                if leg.get('stopped', False):
                    continue
                current = price_map.get((leg['type'], str(leg['strike'])))
                if current is None or current <= 0:
                    continue

                # Enrich for UI
                leg_pnl = (leg['entry_price'] - current) * leg['size']
                leg['current_mark'] = round(current, 2)
                leg['current_pnl'] = round(leg_pnl, 2)

                # Check SL
                if current >= leg['sl_price']:
                    print(f"{tag} 🛑 {leg['type'].upper()} SL hit: ₹{current:.2f} >= ₹{leg['sl_price']:.2f}")
                    leg['stopped'] = True
                    leg['exit_price'] = current
                    self._persist_state()

                    # Re-entry
                    if not reentry_used.get(leg['type'], False):
                        self._immediate_reentry(leg['type'], day_legs, reentry_used, tag, chain, spot)

            # All legs stopped — done early
            all_stopped = all(l.get('stopped', False) for l in day_legs)
            if all_stopped:
                print(f"{tag} All legs stopped")
                break

            # PnL tracking
            tick_pnl = sum(l.get('current_pnl', 0) for l in day_legs if not l.get('stopped', False))

            now_iso = now.isoformat()
            self._pnl_history.append((now_iso, round(self.cumulative_pnl + tick_pnl, 2)))
            if len(self._pnl_history) > 500:
                self._pnl_history = self._pnl_history[-500:]

            # Snapshot every 6 ticks
            self._snap_counter += 1
            if self._snap_counter % 6 == 0 and getattr(self, '_sid', None):
                try:
                    from models import save_pnl_snapshot
                    user_id = getattr(self, '_user_id', None)
                    if not user_id:
                        try:
                            from app import nse_strangle_strategies
                            for s_id, ent in nse_strangle_strategies.items():
                                if ent.get('strategy') is self:
                                    user_id = ent.get('user_id')
                                    self._user_id = user_id
                                    break
                        except Exception:
                            pass
                    if user_id:
                        save_pnl_snapshot(user_id, self._sid, round(self.cumulative_pnl + tick_pnl, 2))
                except Exception:
                    pass

            time.sleep(self.monitor_interval)

        # Calculate day PnL
        day_pnl = 0.0
        for leg in day_legs:
            exit_p = leg.get('exit_price')
            if exit_p is None:
                # Get final price
                exit_p = leg.get('current_mark', leg['entry_price'])
                leg['exit_price'] = exit_p
            day_pnl += (leg['entry_price'] - exit_p) * leg['size']

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
        print(f"{tag} Done | PnL: ₹{day_pnl:+.2f} | Cumulative: ₹{self.cumulative_pnl:+.2f}")
        self._persist_state()

    def _immediate_reentry(self, opt_type, day_legs, reentry_used, tag, chain, spot):
        """After SL, immediately re-enter the same side with nearest premium option."""
        print(f"{tag} 🔄 {opt_type.upper()} RE-ENTRY: scanning for ~₹{self.target_premium} {opt_type}...")

        new_opt = self._find_nearest_premium(chain, opt_type, spot)
        if not new_opt:
            print(f"{tag} ✗ Re-entry failed: no suitable {opt_type.upper()} found")
            reentry_used[opt_type] = True
            return

        expiry = day_legs[0].get('expiry', '')
        new_leg = {
            'symbol': new_opt['symbol'],
            'product_id': new_opt.get('product_id'),
            'side': 'sell',
            'strike': new_opt['strike'],
            'type': opt_type,
            'entry_price': new_opt['mark_price'],
            'size': self.quantity,
            'sl_price': new_opt['mark_price'] * self.sl_pct,
            'stopped': False,
            'is_reentry': True,
            'expiry': expiry,
        }
        day_legs.append(new_leg)
        with self._legs_lock:
            self.legs.append(new_leg)
        reentry_used[opt_type] = True
        print(f"{tag} ✓ Re-entered {opt_type.upper()} {new_opt['strike']} @ ₹{new_opt['mark_price']:.2f} | SL: ₹{new_leg['sl_price']:.2f}")
        self._persist_state()

    def _close_day_legs(self, day_legs, tag):
        """Close all non-stopped legs (paper: just record exit price from current LTP)."""
        expiry = day_legs[0].get('expiry') if day_legs else None
        if expiry:
            _get_expiries, _get_chain = _get_data_source()
            chain, _, _ = _get_chain(self.symbol, expiry)
            if chain:
                price_map = {}
                for row in chain:
                    strike = str(row['strike'])
                    if row.get('call'):
                        price_map[('call', strike)] = row['call']['mark_price']
                    if row.get('put'):
                        price_map[('put', strike)] = row['put']['mark_price']

                for leg in day_legs:
                    if leg.get('stopped', False):
                        continue
                    current = price_map.get((leg['type'], str(leg['strike'])))
                    leg['exit_price'] = current if current else leg.get('current_mark', leg['entry_price'])
                    leg['stopped'] = True
                    print(f"{tag} ✓ Closed {leg['type'].upper()} {leg['strike']} @ ₹{leg['exit_price']:.2f}")
                return

        # Fallback if chain fetch fails
        for leg in day_legs:
            if not leg.get('stopped', False):
                leg['exit_price'] = leg.get('current_mark', leg['entry_price'])
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
        sl_count = sum(1 for l in day_legs if l.get('exit_price', 0) >= l.get('sl_price', 0) * 0.99)
        reentry_count = sum(1 for l in day_legs if l.get('is_reentry', False))
        if sl_count == 0:
            return 'eod_exit'
        reason = 'both_sl' if sl_count >= 2 and not reentry_count else f'{sl_count}sl'
        if reentry_count:
            reason += f'_{reentry_count}re'
        return reason

    def close_all(self):
        self._running = False
        with self._legs_lock:
            self.legs.clear()
        try:
            self._persist_state()
        except Exception:
            pass

    @property
    def pnl(self):
        open_pnl = 0.0
        with self._legs_lock:
            for leg in list(self.legs):
                if leg.get('stopped'):
                    continue
                mark = leg.get('current_mark')
                if mark:
                    open_pnl += (leg['entry_price'] - mark) * leg['size']
        return self.cumulative_pnl + open_pnl

    def _persist_state(self):
        """Save state to DB so it survives server restarts."""
        try:
            from models import update_strategy_db
            sid = getattr(self, '_sid', None)
            if not sid:
                try:
                    from app import nse_strangle_strategies
                    for s_id, entry in nse_strangle_strategies.items():
                        if entry.get('strategy') is self:
                            sid = s_id
                            self._sid = sid
                            break
                except Exception:
                    pass
            if not sid:
                return
            details = {
                'symbol': self.symbol, 'lots': self.lots,
                'lot_size': self.lot_size, 'quantity': self.quantity,
                'target_premium': self.target_premium,
                'sl_pct': int(self.sl_pct * 100),
                'entry_hour': self.entry_hour, 'entry_minute': self.entry_minute,
                'exit_hour': self.exit_hour, 'exit_minute': self.exit_minute,
                'monitoring_interval': self.monitor_interval,
                'trading_days': self.trading_days,
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
            logger.warning(f"[NSE Strangle] Persist state failed: {e}")

    def _wait_for_next_entry(self):
        """Wait until next trading day entry time. Skips weekends and non-trading days."""
        now = datetime.now(IST)
        entry_today = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)

        if now < entry_today and now.weekday() in self.trading_days:
            target = entry_today
        else:
            # Find next valid trading day
            target = entry_today + timedelta(days=1)
            attempts = 0
            while target.weekday() not in self.trading_days and attempts < 7:
                target += timedelta(days=1)
                attempts += 1

        wait = (target - now).total_seconds()
        if wait > 60:
            day_name = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][target.weekday()]
            print(f"[NSE Strangle] Next entry: {target.strftime('%Y-%m-%d %H:%M')} IST ({day_name}, {wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))
