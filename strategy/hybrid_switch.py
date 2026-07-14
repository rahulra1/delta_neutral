"""Hybrid Switch BTST Strategy — daily recurring.

Entry: 7:15 PM IST — sell OTM5 call + put (1 lot each), 100% SL.
On SL hit: switch to buying same option type at 10x lots with 50% SL + trailing.
Exit: 5:15 PM next day.
Gap: 5:30-6:30 PM — no decisions made.
Expiry: D2 (next day's expiry).
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from api.chain import get_expiries, get_option_chain_full
from api.orders import place_order
from api.pricing import get_current_price
from config import get_contract_value
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

ENTRY_HOUR = 19
ENTRY_MINUTE = 15
EXIT_HOUR = 17
EXIT_MINUTE = 15
SELL_SL_PCT = 2.00       # 100% SL = premium doubles
BUY_SL_PCT = 0.50        # 50% SL on buy legs
BUY_MULTIPLIER = 10      # 10x lots on buy switch
TRAIL_POINTS = 10        # trailing SL in $ on buy legs
OTM_INDEX = 5            # OTM5
MONITOR_INTERVAL = 10


class HybridSwitch(BaseStrategy):
    """BTST Hybrid: sell strangle, switch to 10x buying on SL hit with trailing SL."""

    def __init__(self, asset='BTC', lot_size=1, buy_multiplier=BUY_MULTIPLIER,
                 sell_sl_pct=SELL_SL_PCT, buy_sl_pct=BUY_SL_PCT,
                 trail_points=TRAIL_POINTS, otm_index=OTM_INDEX,
                 entry_hour=ENTRY_HOUR, entry_minute=ENTRY_MINUTE,
                 exit_hour=EXIT_HOUR, exit_minute=EXIT_MINUTE,
                 monitor_interval=MONITOR_INTERVAL):
        self.asset = asset
        self.lot_size = lot_size
        self.buy_multiplier = buy_multiplier
        self.sell_sl_pct = sell_sl_pct
        self.buy_sl_pct = buy_sl_pct
        self.trail_points = trail_points
        self.otm_index = otm_index
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute
        self.exit_hour = exit_hour
        self.exit_minute = exit_minute
        self.monitor_interval = monitor_interval

        self.legs = []
        self._legs_lock = threading.Lock()
        self._session_threads = []
        self._pnl = 0.0
        self.cumulative_pnl = 0.0
        self.total_days_traded = 0
        self.trade_log = []
        self._running = False
        self._pnl_history = []            # [(iso_ts, pnl), ...] for UI chart
        self._snap_counter = 0
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10

    def initialize(self):
        self._running = True
        print(f"[Hybrid] BTST Hybrid Switch started | Entry: {self.entry_hour}:{self.entry_minute:02d} | Exit: {self.exit_hour}:{self.exit_minute:02d}")
        print(f"[Hybrid] Sell: {self.lot_size} lot OTM{self.otm_index} | Buy: {self.lot_size * self.buy_multiplier} lots | Trail: ${self.trail_points}")
        return True

    def monitor(self):
        # On restore: if there are active legs from a previous session, resume monitoring them
        if self.legs:
            active_legs = [l for l in self.legs if l.get('active', True)]
            if active_legs:
                print(f"[Hybrid] Resuming {len(active_legs)} active legs from interrupted session")
                day_num = self.total_days_traded or 1
                tag = f"[Hybrid D{day_num}]"
                t = threading.Thread(target=self._resume_session, args=(tag, day_num, active_legs))
                t.start()
                self._session_threads.append(t)
                t.join()  # wait for resumed session to finish before starting new cycle
                if not self._running:
                    return

        while self._running:
            self._wait_for_next_entry()
            if not self._running:
                break
            self.total_days_traded += 1
            day_num = self.total_days_traded
            tag = f"[Hybrid D{day_num}]"
            print(f"\n{tag} ═══ {datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST ═══")
            t = threading.Thread(target=self._run_session, args=(tag, day_num))
            t.start()
            self._session_threads = [th for th in self._session_threads if th.is_alive()]
            self._session_threads.append(t)

    def _resume_session(self, tag, day_num, active_legs):
        """Resume monitoring active legs from a previous session that was interrupted."""
        from config import set_thread_credentials
        if hasattr(self, '_api_key') and self._api_key:
            set_thread_credentials(self._api_key, self._api_secret, self._broker)
        if hasattr(self, '_log_queue') and self._log_queue:
            from app import LogCapture
            LogCapture._local.log_queue = self._log_queue
            LogCapture._local.log_history = self._log_history

        cv = get_contract_value(self.asset)

        # Separate sell and buy legs
        sell_legs = [l for l in active_legs if l.get('role') == 'sell' or l.get('side') == 'sell']
        buy_legs = [l for l in active_legs if l.get('role') == 'buy_switch' or (l.get('side') == 'buy' and l.get('role') != 'sell')]

        for leg in active_legs:
            print(f"{tag} ↻ Resumed {leg['side'].upper()} {leg.get('type','').upper()} {leg.get('strike','')} | Entry: ${leg.get('entry_price',0):.2f} | SL: ${leg.get('sl_price',0):.2f}")

        session_pnl = 0.0

        # Determine exit time: if we're past entry time (19:15+), exit is TOMORROW at 17:15
        # If we're before exit time on the exit day, exit is TODAY at 17:15
        now = datetime.now(IST)
        entry_time_today = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)
        if now.hour >= self.entry_hour:
            # We're in the evening after entry — exit is tomorrow
            exit_dt = (now + timedelta(days=1)).replace(hour=self.exit_hour, minute=self.exit_minute, second=0, microsecond=0)
        else:
            # We're on the exit day (morning/afternoon) — exit is today
            exit_dt = now.replace(hour=self.exit_hour, minute=self.exit_minute, second=0, microsecond=0)
            # But if we're already past exit time today, close immediately
            if now >= exit_dt:
                print(f"{tag} ⏰ Already past exit time — closing all resumed legs immediately")
                exit_dt = now  # will trigger exit on first loop iteration

        print(f"{tag} Exit target: {exit_dt.strftime('%Y-%m-%d %H:%M')} IST")

        while self._running:
            now = datetime.now(IST)

            # Gap period — no decisions
            if now.hour == 17 and now.minute >= 30:
                time.sleep(self.monitor_interval)
                continue
            if now.hour == 18 and now.minute < 30:
                time.sleep(self.monitor_interval)
                continue

            # Exit time check
            if now >= exit_dt:
                print(f"{tag} ⏰ Exit time — closing all resumed legs")
                break

            # Check sell legs for SL
            for leg in sell_legs:
                if not leg.get('active', True):
                    continue
                data = get_current_price(leg['product_id'], self.asset)
                if not data:
                    continue
                current = data['mark_price']
                leg_pnl = (leg['entry_price'] - current) * leg['size'] * cv
                leg['current_mark'] = round(current, 2)
                leg['current_pnl'] = round(leg_pnl, 2)
                if current >= leg.get('sl_price', float('inf')):
                    print(f"{tag} 🛑 SELL {leg.get('type','').upper()} SL hit: ${current:.2f} >= ${leg['sl_price']:.2f}")
                    place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
                    leg['active'] = False
                    leg['exit_price'] = current
                    # Activate buy leg — use same expiry as the sell leg
                    import re
                    sell_expiry = None
                    m = re.search(r'-(\d{6})$', leg.get('symbol', ''))
                    if m:
                        try:
                            sell_expiry = datetime.strptime(m.group(1), '%d%m%y').strftime('%d-%m-%Y')
                        except ValueError:
                            pass
                    if not sell_expiry:
                        expiries = get_expiries(self.asset, min_days=1)
                        sell_expiry = expiries[0] if expiries else None
                    if sell_expiry:
                        buy_leg = self._activate_buy_leg(tag, sell_expiry, leg.get('type', 'call'), current)
                        if buy_leg:
                            buy_legs.append(buy_leg)
                            with self._legs_lock:
                                self.legs.append(buy_leg)
                    self._persist_state()

            # Check buy legs for SL / trailing
            for leg in buy_legs:
                if not leg.get('active', True):
                    continue
                data = get_current_price(leg['product_id'], self.asset)
                if not data:
                    continue
                current = data['mark_price']
                leg_pnl = (current - leg['entry_price']) * leg['size'] * cv
                leg['current_mark'] = round(current, 2)
                leg['current_pnl'] = round(leg_pnl, 2)
                move = current - leg['entry_price']
                if move > 0 and 'base_sl' in leg:
                    new_sl = leg['base_sl'] + move
                    if new_sl > leg.get('sl_price', 0):
                        leg['sl_price'] = new_sl
                if current <= leg.get('sl_price', 0):
                    print(f"{tag} 🛑 BUY {leg.get('type','').upper()} SL/Trail hit: ${current:.2f} <= ${leg['sl_price']:.2f}")
                    place_order(leg['product_id'], leg['symbol'], leg['size'], 'sell')
                    leg['active'] = False
                    leg['exit_price'] = current
                    self._persist_state()

            # All done?
            all_sell_done = all(not l.get('active', True) for l in sell_legs) if sell_legs else True
            all_buy_done = (all(not l.get('active', True) for l in buy_legs)) if buy_legs else not sell_legs
            if all_sell_done and all_buy_done:
                print(f"{tag} All resumed legs closed")
                break

            time.sleep(self.monitor_interval)

        # Close remaining active legs
        for leg in sell_legs + buy_legs:
            if leg.get('active', True):
                data = get_current_price(leg['product_id'], self.asset)
                leg['exit_price'] = data['mark_price'] if data else leg.get('entry_price', 0)
                close_side = 'buy' if leg['side'] == 'sell' else 'sell'
                place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
                leg['active'] = False

        # Calculate PnL for this resumed session
        for leg in sell_legs:
            exit_p = leg.get('exit_price', leg.get('entry_price', 0))
            session_pnl += (leg.get('entry_price', 0) - exit_p) * leg['size'] * cv
        for leg in buy_legs:
            exit_p = leg.get('exit_price', leg.get('entry_price', 0))
            session_pnl += (exit_p - leg.get('entry_price', 0)) * leg['size'] * cv

        self.cumulative_pnl += session_pnl
        self._pnl = session_pnl

        # Clean shared legs
        with self._legs_lock:
            for leg in sell_legs + buy_legs:
                if leg in self.legs:
                    self.legs.remove(leg)

        # Update existing trade_log entry if it exists for today, else add new
        today_str = datetime.now(IST).strftime('%Y-%m-%d')
        existing = next((t for t in self.trade_log if t.get('date') == today_str), None)
        if existing:
            existing['pnl'] = round(existing.get('pnl', 0) + session_pnl, 2)
        else:
            self.trade_log.append({
                'date': today_str,
                'day': day_num,
                'pnl': round(session_pnl, 2),
                'sell_legs': len(sell_legs),
                'buy_legs_activated': len(buy_legs),
                'resumed': True,
            })

        print(f"{tag} Resumed session done | PnL: ${session_pnl:+.2f} | Cumulative: ${self.cumulative_pnl:+.2f}")
        self._persist_state()

    def _run_session(self, tag, day_num):
        """Run one full BTST session in its own thread."""
        from config import set_thread_credentials
        if hasattr(self, '_api_key') and self._api_key:
            set_thread_credentials(self._api_key, self._api_secret, self._broker)
        if hasattr(self, '_log_queue') and self._log_queue:
            from app import LogCapture
            LogCapture._local.log_queue = self._log_queue
            LogCapture._local.log_history = self._log_history

        cv = get_contract_value(self.asset)
        expiries = get_expiries(self.asset, min_days=1)
        if not expiries:
            print(f"{tag} No expiries found")
            return
        expiry = expiries[0]

        chain, spot, _ = get_option_chain_full(expiry, self.asset)
        if not chain or not spot:
            print(f"{tag} Chain fetch failed")
            return

        # Find OTM5 call and put
        call_opt = self._find_otm(chain, 'call', spot, self.otm_index)
        put_opt = self._find_otm(chain, 'put', spot, self.otm_index)
        if not call_opt or not put_opt:
            print(f"{tag} Could not find OTM{self.otm_index} options")
            return

        # Sell both legs
        sell_legs = []
        for opt, opt_type in [(call_opt, 'call'), (put_opt, 'put')]:
            result = place_order(opt['product_id'], opt['symbol'], self.lot_size, 'sell')
            if result:
                leg = {
                    'symbol': opt['symbol'], 'product_id': opt['product_id'],
                    'side': 'sell', 'type': opt_type, 'strike': opt['strike'],
                    'entry_price': opt['mark_price'], 'size': self.lot_size,
                    'sl_price': opt['mark_price'] * self.sell_sl_pct,
                    'active': True, 'role': 'sell',
                }
                sell_legs.append(leg)
                print(f"{tag} ✓ SELL {opt_type.upper()} {opt['strike']} @ ${opt['mark_price']:.2f} | SL: ${leg['sl_price']:.2f}")

        if not sell_legs:
            print(f"{tag} Failed to place sell orders")
            return

        with self._legs_lock:
            self.legs.extend(sell_legs)
        self._persist_state()

        # Monitor session
        buy_legs = []  # activated lazy legs
        session_pnl = 0.0
        expiry_date_obj = datetime.strptime(expiry, '%d-%m-%Y').date()

        while self._running:
            now = datetime.now(IST)

            # Gap period — no decisions
            if now.hour == 17 and now.minute >= 30:
                time.sleep(self.monitor_interval)
                continue
            if now.hour == 18 and now.minute < 30:
                time.sleep(self.monitor_interval)
                continue

            # Exit on expiry day (D-0) at 5:15 PM
            if now.date() >= expiry_date_obj:
                if now.hour > self.exit_hour or (now.hour == self.exit_hour and now.minute >= self.exit_minute):
                    print(f"{tag} ⏰ Exit time (expiry day) — closing all")
                    break

            # Check sell legs for SL + enrich
            for leg in sell_legs:
                if not leg['active']:
                    continue
                data = get_current_price(leg['product_id'], self.asset)
                if not data:
                    continue
                current = data['mark_price']
                # Enrich for UI
                leg_pnl = (leg['entry_price'] - current) * leg['size'] * cv
                leg['current_mark'] = round(current, 2)
                leg['current_pnl'] = round(leg_pnl, 2)
                # Check SL
                if current >= leg['sl_price']:
                    print(f"{tag} 🛑 SELL {leg['type'].upper()} SL hit: ${current:.2f} >= ${leg['sl_price']:.2f}")
                    place_order(leg['product_id'], leg['symbol'], leg['size'], 'buy')
                    leg['active'] = False
                    leg['exit_price'] = current
                    # Activate buy leg (lazy leg)
                    buy_leg = self._activate_buy_leg(tag, expiry, leg['type'], current)
                    if buy_leg:
                        buy_legs.append(buy_leg)
                        with self._legs_lock:
                            self.legs.append(buy_leg)
                    self._persist_state()

            # Check buy legs for SL / trailing + enrich
            for leg in buy_legs:
                if not leg['active']:
                    continue
                data = get_current_price(leg['product_id'], self.asset)
                if not data:
                    continue
                current = data['mark_price']
                # Enrich for UI
                leg_pnl = (current - leg['entry_price']) * leg['size'] * cv
                leg['current_mark'] = round(current, 2)
                leg['current_pnl'] = round(leg_pnl, 2)

                # Trail SL point-for-point: if price moves up by X, SL moves up by X
                move = current - leg['entry_price']
                if move > 0:
                    new_sl = leg['base_sl'] + move
                    if new_sl > leg['sl_price']:
                        leg['sl_price'] = new_sl

                # Check SL
                if current <= leg['sl_price']:
                    print(f"{tag} 🛑 BUY {leg['type'].upper()} SL/Trail hit: ${current:.2f} <= ${leg['sl_price']:.2f}")
                    place_order(leg['product_id'], leg['symbol'], leg['size'], 'sell')
                    leg['active'] = False
                    leg['exit_price'] = current
                    self._persist_state()

            # All done early?
            all_sell_done = all(not l['active'] for l in sell_legs)
            all_buy_done = buy_legs and all(not l['active'] for l in buy_legs)
            if all_sell_done and all_buy_done:
                print(f"{tag} All legs closed")
                break

            # --- SOP: Track pnl_history, save snapshots, handle failures ---
            tick_pnl = 0.0
            all_legs_ok = True
            for leg in sell_legs + buy_legs:
                if not leg.get('active', False):
                    continue
                if leg.get('current_mark'):
                    tick_pnl += leg.get('current_pnl', 0)
                else:
                    all_legs_ok = False

            if not all_legs_ok:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._max_consecutive_failures:
                    print(f"{tag} 🚨 EMERGENCY: {self._consecutive_failures} consecutive failures — closing all")
                    break
            else:
                self._consecutive_failures = 0

            # PnL history for UI chart
            now_iso = now.isoformat()
            self._pnl_history.append((now_iso, round(self.cumulative_pnl + tick_pnl, 2)))
            if len(self._pnl_history) > 500:
                self._pnl_history = self._pnl_history[-500:]

            # Save snapshot every 6 ticks
            self._snap_counter += 1
            if self._snap_counter % 6 == 0 and getattr(self, '_sid', None):
                try:
                    from models import save_pnl_snapshot
                    user_id = getattr(self, '_user_id', None)
                    if not user_id:
                        try:
                            from app import hybrid_strategies
                            for s_id, ent in hybrid_strategies.items():
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

        # Close remaining active legs
        for leg in sell_legs + buy_legs:
            if leg['active']:
                data = get_current_price(leg['product_id'], self.asset)
                leg['exit_price'] = data['mark_price'] if data else leg['entry_price']
                close_side = 'buy' if leg['side'] == 'sell' else 'sell'
                place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
                leg['active'] = False

        # Calculate PnL
        for leg in sell_legs:
            exit_p = leg.get('exit_price', leg['entry_price'])
            session_pnl += (leg['entry_price'] - exit_p) * leg['size'] * cv
        for leg in buy_legs:
            exit_p = leg.get('exit_price', leg['entry_price'])
            session_pnl += (exit_p - leg['entry_price']) * leg['size'] * cv

        self.cumulative_pnl += session_pnl
        self._pnl = session_pnl

        # Clean shared legs
        with self._legs_lock:
            for leg in sell_legs + buy_legs:
                if leg in self.legs:
                    self.legs.remove(leg)

        # Build description of strikes traded
        sell_strikes = [f"{l.get('type','')[0].upper()}{l.get('strike','')}" for l in sell_legs]
        buy_strikes = [f"{l.get('type','')[0].upper()}{l.get('strike','')}" for l in buy_legs]
        direction = "/".join(sell_strikes) if sell_strikes else ""
        exit_reason = "SL→Buy " + "/".join(buy_strikes) if buy_legs else "held"

        self.trade_log.append({
            'date': datetime.now(IST).strftime('%Y-%m-%d'),
            'day': day_num,
            'pnl': round(session_pnl, 2),
            'sell_legs': len(sell_legs),
            'buy_legs_activated': len(buy_legs),
            'direction': direction,
            'exit_reason': exit_reason,
        })
        print(f"{tag} Done | PnL: ${session_pnl:+.2f} | Cumulative: ${self.cumulative_pnl:+.2f}")
        self._persist_state()

    def _activate_buy_leg(self, tag, expiry, opt_type, current_price):
        """Activate a lazy buy leg after sell SL hit."""
        chain, spot, _ = get_option_chain_full(expiry, self.asset)
        if not chain or not spot:
            return None
        opt = self._find_otm(chain, opt_type, spot, self.otm_index)
        if not opt:
            return None

        buy_size = self.lot_size * self.buy_multiplier
        result = place_order(opt['product_id'], opt['symbol'], buy_size, 'buy')
        if not result:
            return None

        sl_price = opt['mark_price'] * self.buy_sl_pct
        leg = {
            'symbol': opt['symbol'], 'product_id': opt['product_id'],
            'side': 'buy', 'type': opt_type, 'strike': opt['strike'],
            'entry_price': opt['mark_price'], 'size': buy_size,
            'sl_price': sl_price, 'base_sl': sl_price,
            'active': True, 'role': 'buy_switch',
        }
        print(f"{tag} ⚡ BUY {buy_size} lots {opt_type.upper()} {opt['strike']} @ ${opt['mark_price']:.2f} | SL: ${sl_price:.2f} | Trail: ${self.trail_points}")
        return leg

    def _find_otm(self, chain, opt_type, spot, n):
        """Find the Nth OTM option."""
        if opt_type == 'call':
            otms = [r for r in chain if r.get('call') and float(r['strike']) > spot]
            otms.sort(key=lambda r: float(r['strike']))
        else:
            otms = [r for r in chain if r.get('put') and float(r['strike']) < spot]
            otms.sort(key=lambda r: float(r['strike']), reverse=True)
        if len(otms) >= n:
            return otms[n - 1].get(opt_type)
        return otms[-1].get(opt_type) if otms else None

    def close_all(self):
        self._running = False
        with self._legs_lock:
            for leg in list(self.legs):
                if leg.get('active', True):
                    try:
                        close_side = 'buy' if leg['side'] == 'sell' else 'sell'
                        place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
                    except Exception as e:
                        logger.warning(f"[Hybrid] Failed to close leg {leg.get('symbol')}: {e}")
            self.legs.clear()
        # Don't block Flask thread — let session threads exit on their own
        for t in self._session_threads:
            t.join(timeout=2)
        self._session_threads.clear()
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
                if not leg.get('active', True):
                    continue
                data = get_current_price(leg['product_id'], self.asset)
                if data:
                    if leg['side'] == 'sell':
                        open_pnl += (leg['entry_price'] - data['mark_price']) * leg['size'] * cv
                    else:
                        open_pnl += (data['mark_price'] - leg['entry_price']) * leg['size'] * cv
        return self.cumulative_pnl + open_pnl

    def _persist_state(self):
        """Save state to DB so it survives server restarts."""
        try:
            from models import update_strategy_db
            import json
            sid = getattr(self, '_sid', None)
            if not sid:
                try:
                    from app import hybrid_strategies
                    for s_id, entry in hybrid_strategies.items():
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
                'buy_multiplier': self.buy_multiplier,
                'sell_sl_pct': int(self.sell_sl_pct * 100),
                'buy_sl_pct': int(self.buy_sl_pct * 100),
                'trail_points': self.trail_points,
                'otm_index': self.otm_index,
                'entry_hour': self.entry_hour, 'entry_minute': self.entry_minute,
                'exit_hour': self.exit_hour, 'exit_minute': self.exit_minute,
                'monitoring_interval': self.monitor_interval,
                'cumulative_pnl': self.cumulative_pnl,
                'total_days_traded': self.total_days_traded,
                'trade_log': self.trade_log[-50:],
            }
            legs_data = []
            for leg in self.legs:
                legs_data.append({k: v for k, v in leg.items()
                                  if not callable(v) and k != '_lock'})
            update_strategy_db(sid, details=details,
                               legs=legs_data,
                               pnl=round(self.cumulative_pnl, 2))
        except Exception as e:
            logger.warning(f"[Hybrid] Persist state failed: {e}")

    def _wait_for_next_entry(self):
        now = datetime.now(IST)
        entry_today = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)
        if now < entry_today:
            target = entry_today
        else:
            target = entry_today + timedelta(days=1)
        wait = (target - now).total_seconds()
        if wait > 60:
            print(f"[Hybrid] Next entry: {target.strftime('%Y-%m-%d %H:%M')} IST ({wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))
