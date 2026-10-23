"""Recurring scheduler for the visual Strategy Builder.

The one-shot builder deploy places legs once, immediately. This scheduler instead
RUNS the same builder config on a repeating schedule:

  - only on the configured weekdays (execution.days: int list, Mon=0..Sun=6),
  - at the configured entry time (execution.square_off is the exit time; entry
    time comes from the first time-based entry condition, else 09:20 IST),
  - optionally only when within `days_before_expiry_entry` days of the resolved
    expiry,
  - each entry's position is handed to a StrategyMonitor for TP/SL, and is also
    force-closed at the square-off time or when within `days_before_expiry_exit`
    days of expiry.

It reuses strategy.builder_common for expiry + strike resolution so behavior
matches the one-shot deploy exactly.
"""

import sys
import time
import logging
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def _parse_hhmm(s, default_h, default_m):
    try:
        h, m = str(s).split(':')
        return int(h), int(m)
    except (ValueError, AttributeError, TypeError):
        return default_h, default_m


class BuilderScheduler:
    """Repeating executor for a Strategy Builder config."""

    def __init__(self, config, asset='BTC', profile_id=None):
        self.config = config or {}
        self.asset = asset
        self.profile_id = profile_id
        self.legs_cfg = self.config.get('legs', [])
        self.expiry_key = self.config.get('expiry', 'weekly')

        execu = self.config.get('execution', {}) or {}
        self.days = execu.get('days', [0, 1, 2, 3, 4])
        if not isinstance(self.days, list):
            self.days = [0, 1, 2, 3, 4]
        self.lots_per_leg = int(execu.get('lots', 1) or 1)
        self.exit_hour, self.exit_minute = _parse_hhmm(execu.get('square_off', '15:15'), 15, 15)
        self.days_before_entry = self._opt_int(execu.get('days_before_expiry_entry'))
        self.days_before_exit = self._opt_int(execu.get('days_before_expiry_exit'))

        # Entry time from the first time-based entry condition, else 09:20.
        entry_time = '09:20'
        for c in self.config.get('entry_conditions', []) or []:
            if c.get('type') == 'time' and c.get('value'):
                entry_time = c['value']
                break
        self.entry_hour, self.entry_minute = _parse_hhmm(entry_time, 9, 20)

        risk = self.config.get('risk', {}) or {}
        # Default TP 50% / SL 100% of premium when the config OMITS them (matches
        # the builder UI defaults, so a position always has a risk exit). An
        # explicit 0 is respected as "disabled" (rely on square-off/expiry).
        _sl = risk.get('sl_pct', None)
        _tp = risk.get('target_pct', None)
        self.sl_pct = float(100 if _sl is None else _sl)
        self.target_pct = float(50 if _tp is None else _tp)
        self._cv = 0.001  # contract value; refreshed per entry from config

        self._running = False
        self._monitors = []           # active StrategyMonitor objects
        self._open_positions = []     # [{entry_id, expiry, legs:[...]}] currently live
        self._open_lock = threading.Lock()
        self.cumulative_pnl = 0.0
        self.total_entries = 0
        self.trade_log = []
        self._sid = None
        self._user_id = None
        self._log_queue = None
        self._log_history = None
        self._api_key = self._api_secret = self._broker = None

    @staticmethod
    def _opt_int(v):
        if v in (None, ''):
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    # ── BaseStrategy-ish interface ──────────────────────────────────────

    def initialize(self):
        self._running = True
        # Credentials/broker are normally set at construction (from the deploying
        # request's profile). Only fall back to this thread's locals if they were
        # NOT pre-set — otherwise a fresh monitor thread (whose thread-locals are
        # empty) would clobber the correct broker and silently route orders to the
        # default 'demo'/testnet endpoint.
        from config import get_api_key, get_api_secret, _thread_local
        if not self._api_key:
            self._api_key = get_api_key()
        if not self._api_secret:
            self._api_secret = get_api_secret()
        if not self._broker:
            self._broker = getattr(_thread_local, 'broker', '')
        days_str = ','.join(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][d]
                            for d in self.days)
        self._print(f"[Builder Sched] {self.asset} | {len(self.legs_cfg)} legs | "
                    f"expiry={self.expiry_key} | days={days_str} | "
                    f"entry {self.entry_hour}:{self.entry_minute:02d} / "
                    f"exit {self.exit_hour}:{self.exit_minute:02d} IST | broker={self._broker or '(default)'}")
        if self.days_before_entry is not None:
            self._print(f"[Builder Sched] Enter only within {self.days_before_entry}d of expiry")
        if self.days_before_exit is not None:
            self._print(f"[Builder Sched] Force exit within {self.days_before_exit}d of expiry")
        # Persist a fresh row at startup so the DB reflects a running strategy
        # even before the first entry (and after a resume) — like EMA Spread's
        # deploy-time row stays authoritative.
        self._persist_state()
        return True

    def monitor(self):
        self._apply_thread_context()
        while self._running:
            self._wait_for_next_entry()
            if not self._running:
                break
            # Keep the DB row + PnL snapshot fresh each cycle (even when flat),
            # so a long-running idle strategy stays current and resumable.
            self._persist_state()
            self._save_snapshot()
            now = datetime.now(IST)
            if now.weekday() not in self.days:
                continue
            try:
                self._run_one_entry()
            except Exception as e:
                logger.error("[Builder Sched] entry failed: %s", e)

    def _save_snapshot(self):
        """Write a PnL snapshot for the UI chart (best-effort, like EMA Spread)."""
        try:
            from models import save_pnl_snapshot
            if self._user_id and self._sid:
                save_pnl_snapshot(self._user_id, self._sid, round(self.pnl, 2))
        except Exception:
            pass

    def close_all(self):
        self._running = False
        for mon in list(self._monitors):
            try:
                mon.running = False
            except Exception:
                pass
        self._persist_state()

    @property
    def running_pnl(self):
        """Live UNREALIZED P&L of currently-open positions only. This is what
        TP/SL act on. Does NOT include realized/cumulative P&L."""
        from api.pricing import get_current_price
        total = 0.0
        with self._open_lock:
            positions = [p for p in self._open_positions]
        for pos in positions:
            for l in pos.get('legs', []):
                try:
                    data = get_current_price(l['product_id'], self.asset)
                    mark = data['mark_price'] if data else l['entry_price']
                except Exception:
                    mark = l['entry_price']
                sign = 1 if l['side'] == 'sell' else -1
                total += sign * (l['entry_price'] - mark) * l['size'] * self._cv
        return total

    @property
    def pnl(self):
        """Total = realized cumulative + live running (unrealized)."""
        return self.cumulative_pnl + self.running_pnl

    def status(self):
        """Monitoring snapshot for the dashboard, with live per-leg marks/PnL."""
        from api.pricing import get_current_price
        with self._open_lock:
            positions = [dict(p) for p in self._open_positions]
        open_legs = []
        running = 0.0
        for pos in positions:
            for l in pos.get('legs', []):
                try:
                    data = get_current_price(l['product_id'], self.asset)
                    mark = data['mark_price'] if data else l['entry_price']
                except Exception:
                    mark = l['entry_price']
                sign = 1 if l['side'] == 'sell' else -1
                leg_pnl = sign * (l['entry_price'] - mark) * l['size'] * self._cv
                running += leg_pnl
                open_legs.append({**l, 'entry_id': pos.get('entry_id'),
                                  'expiry': pos.get('expiry'),
                                  'current_mark': round(mark, 4),
                                  'current_pnl': round(leg_pnl, 4)})
        running = round(running, 2)
        return {
            'running': self._running,
            'cumulative_pnl': round(self.cumulative_pnl, 2),
            'running_pnl': running,
            'total_pnl': round(self.cumulative_pnl + running, 2),
            'total_entries': self.total_entries,
            'open_positions': len(positions),
            'legs': open_legs,
            'trade_log': self.trade_log[-20:],
            'expiry_type': self.expiry_key,
            'days': self.days,
        }

    # ── Core ────────────────────────────────────────────────────────────

    def _run_one_entry(self):
        from api.chain import get_option_chain_full, get_expiries
        from api.orders import place_order
        from strategy.builder_common import (
            resolve_builder_expiry, resolve_leg_to_option, atm_index)
        from strategy.monitor import StrategyMonitor

        expiries = get_expiries(self.asset)
        expiry = resolve_builder_expiry(self.expiry_key, expiries)
        if not expiry:
            self._print("[Builder Sched] Could not resolve expiry — skipping")
            return

        # Days-before-expiry ENTRY gate.
        if self.days_before_entry is not None:
            dte = self._days_to_expiry(expiry)
            if dte is None or dte > self.days_before_entry:
                self._print(f"[Builder Sched] {dte}d to expiry > {self.days_before_entry} — not entering")
                return

        chain, spot, _ = get_option_chain_full(expiry, self.asset)
        if not chain or not spot:
            self._print("[Builder Sched] Chain fetch failed — skipping")
            return
        atm_idx = atm_index(chain, spot)

        placed = []
        for leg in self.legs_cfg:
            opt = resolve_leg_to_option(leg, chain, atm_idx)
            if not opt:
                self._print(f"[Builder Sched] No option for leg {leg} — skipping it")
                continue
            size = int(leg.get('lots', 1)) * self.lots_per_leg
            order = place_order(opt['product_id'], opt['symbol'], size, leg['side'])
            if order is None:
                self._print(f"[Builder Sched] Order FAILED {leg['side']} {opt['symbol']}")
                continue
            placed.append({'product_id': opt['product_id'], 'symbol': opt['symbol'],
                           'type': leg['type'], 'strike': opt['strike'],
                           'side': leg['side'], 'size': size,
                           'entry_price': float(opt.get('mark_price', 0))})

        if not placed:
            self._print("[Builder Sched] No legs placed this cycle")
            return

        self.total_entries += 1
        entry_id = self.total_entries
        with self._open_lock:
            self._open_positions.append({'entry_id': entry_id, 'expiry': expiry, 'legs': placed})
        self._print(f"[Builder Sched] Entry #{entry_id}: {len(placed)} legs @ {expiry}")

        # Hand to a StrategyMonitor for TP/SL (if risk set); force-close on
        # square-off / days-before-expiry handled by a lightweight watcher.
        total_prem = sum(l['entry_price'] * l['size'] for l in placed if l['side'] == 'sell')
        from config import get_contract_value
        try:
            cv = get_contract_value(self.asset)
        except Exception:
            cv = 0.001
        self._cv = cv
        max_profit = total_prem * cv * self.target_pct / 100 if self.target_pct else 0
        max_loss = total_prem * cv * self.sl_pct / 100 if self.sl_pct else 0

        mon = None
        if max_profit > 0 or max_loss > 0:
            # Arm the monitor if EITHER side is set; use a very large threshold
            # for the unset side so only the configured exit can trigger.
            _HUGE = 1e12
            mon = StrategyMonitor(legs=placed,
                                  max_profit=max_profit if max_profit > 0 else _HUGE,
                                  max_loss=max_loss if max_loss > 0 else _HUGE,
                                  asset=self.asset, lot_size=cv)
            mon.user_id = self._user_id
            mon.sid = self._sid
            mon.profile_id = self.profile_id
            mon.on_complete = lambda pnl, reason, eid=entry_id: self._on_position_closed(eid, pnl, reason)
            mon.start()
            self._monitors.append(mon)

        # Watcher thread: force-close at square-off time or within exit window.
        t = threading.Thread(target=self._expiry_exit_watch,
                             args=(entry_id, placed, expiry, mon), daemon=True)
        t.start()
        self._persist_state()

    def _expiry_exit_watch(self, entry_id, placed, expiry, mon):
        from api.orders import place_order
        from api.pricing import get_current_price
        self._apply_thread_context()
        opened_at = datetime.now(IST)
        exit_target, rule = self._resolve_exit_target(opened_at, expiry)
        self._print(f"[Builder Sched] Position #{entry_id} exit at "
                    f"{exit_target.strftime('%Y-%m-%d %H:%M')} IST ({rule})")
        while self._running:
            time.sleep(30)
            if mon is not None and not getattr(mon, 'running', False):
                return  # monitor already closed via TP/SL (it books its own PnL)
            # If this position is no longer open (closed by TP/SL, another
            # watcher, or a duplicate loop), stop — do NOT place close orders.
            with self._open_lock:
                still_open = any(p.get('entry_id') == entry_id for p in self._open_positions)
            if not still_open:
                return
            if datetime.now(IST) < exit_target:
                continue

            # Atomically CLAIM the position so only one closer acts on it.
            with self._open_lock:
                claimed = any(p.get('entry_id') == entry_id for p in self._open_positions)
                if claimed:
                    self._open_positions = [p for p in self._open_positions
                                            if p.get('entry_id') != entry_id]
            if not claimed:
                return  # someone else already closed it

            # Book realized PnL from current marks BEFORE closing, then close.
            realized = 0.0
            for l in placed:
                try:
                    data = get_current_price(l['product_id'], self.asset)
                    mark = data['mark_price'] if data else l['entry_price']
                except Exception:
                    mark = l['entry_price']
                sign = 1 if l['side'] == 'sell' else -1
                realized += sign * (l['entry_price'] - mark) * l['size'] * self._cv
                close_side = 'sell' if l['side'] == 'buy' else 'buy'
                try:
                    place_order(l['product_id'], l['symbol'], l['size'], close_side)
                except Exception as e:
                    logger.warning("[Builder Sched] close fail %s: %s", l['symbol'], e)
            if mon is not None:
                mon.running = False
            self._print(f"[Builder Sched] Force-closed {len(placed)} legs (square-off/expiry)")
            self._book_close(entry_id, realized, 'expiry_exit')
            return

    def _resolve_exit_target(self, opened_at, expiry):
        """When a position opened at `opened_at` must be force-closed.

        - days_before_expiry_exit set: exit on (expiry - N days) at the exit
          time. The position is held across days until then -- the square-off
          time is the time-of-day of that exit, not a daily close.
        - not set: daily square-off -- the next exit time after opening.
        An expiry-relative target that is already past at entry falls back to
        the daily rule (warned), so a position is never closed the instant it
        opens.
        """
        daily = opened_at.replace(hour=self.exit_hour, minute=self.exit_minute,
                                  second=0, microsecond=0)
        if daily <= opened_at:
            daily += timedelta(days=1)
        if self.days_before_exit is None:
            return daily, 'daily square-off'
        try:
            exp = datetime.strptime(expiry, '%d-%m-%Y').date()
        except (ValueError, TypeError):
            return daily, 'daily square-off (bad expiry)'
        target = datetime.combine(exp - timedelta(days=self.days_before_exit),
                                  datetime.min.time(), tzinfo=IST).replace(
            hour=self.exit_hour, minute=self.exit_minute)
        if target <= opened_at:
            self._print(f"[Builder Sched] ⚠ {self.days_before_exit}d-before-expiry exit "
                        f"({target.strftime('%Y-%m-%d %H:%M')}) already passed at entry — "
                        f"using daily square-off")
            return daily, 'daily square-off (expiry exit already passed)'
        return target, f'{self.days_before_exit}d before expiry {expiry}'

    def _on_position_closed(self, entry_id, pnl, reason):
        """Close callback for the TP/SL monitor. Atomically claims the position
        (so a racing watcher/duplicate can't double-book or double-close), then
        books the realized PnL."""
        with self._open_lock:
            claimed = any(p.get('entry_id') == entry_id for p in self._open_positions)
            if claimed:
                self._open_positions = [p for p in self._open_positions
                                        if p.get('entry_id') != entry_id]
        if not claimed:
            return  # already closed by the expiry watcher or a duplicate loop
        self._book_close(entry_id, pnl, reason)

    def _book_close(self, entry_id, pnl, reason):
        """Book realized PnL, log, and persist. Caller must have already removed
        the position from _open_positions (the claim) to guarantee single-book."""
        # Drop any finished monitors so the active list stays clean.
        self._monitors = [m for m in self._monitors if getattr(m, 'running', False)]
        self.cumulative_pnl += (pnl or 0)
        self.trade_log.append({'ts': datetime.now(IST).isoformat(),
                               'entry_id': entry_id,
                               'pnl': round(pnl or 0, 2), 'reason': reason})
        self._print(f"[Builder Sched] Position #{entry_id} closed ({reason}) "
                    f"PnL ${pnl:+.2f} | Cum ${self.cumulative_pnl:+.2f}")
        self._persist_state()

    def _days_to_expiry(self, expiry):
        try:
            exp = datetime.strptime(expiry, '%d-%m-%Y').date()
        except (ValueError, TypeError):
            return None
        return (exp - datetime.now(IST).date()).days

    # ── timing ──────────────────────────────────────────────────────────

    def _wait_for_next_entry(self):
        now = datetime.now(IST)
        entry_today = now.replace(hour=self.entry_hour, minute=self.entry_minute,
                                  second=0, microsecond=0)
        if now < entry_today and now.weekday() in self.days:
            target = entry_today
        else:
            # find the next allowed weekday at entry time
            for add in range(1, 8):
                cand = (now + timedelta(days=add)).replace(
                    hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)
                if cand.weekday() in self.days:
                    target = cand
                    break
            else:
                target = entry_today + timedelta(days=1)
        wait = (target - now).total_seconds()
        if wait > 60:
            dn = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][target.weekday()]
            self._print(f"[Builder Sched] Next entry: {target.strftime('%Y-%m-%d %H:%M')} IST ({dn}, {wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        last_persist = time.time()
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))
            # Refresh the DB row + snapshot every ~15 min during a long idle wait
            # so a flat, waiting strategy keeps a current row (resumable + live PnL).
            if time.time() - last_persist >= 900:
                last_persist = time.time()
                try:
                    self._persist_state()
                    self._save_snapshot()
                except Exception:
                    pass

    # ── plumbing ────────────────────────────────────────────────────────

    def _apply_thread_context(self):
        try:
            from config import set_thread_credentials
            if self._api_key:
                set_thread_credentials(self._api_key, self._api_secret, self._broker)
        except Exception:
            pass
        try:
            if self._log_queue is not None:
                from app import LogCapture
                LogCapture._local.log_queue = self._log_queue
                LogCapture._local.log_history = self._log_history
        except Exception:
            pass

    # ── persistence ─────────────────────────────────────────────────────

    def _persist_state(self):
        """Save schedule config, realized cumulative PnL, trade log, and the
        currently-open positions to the DB row keyed by _sid, so a restart can
        resume monitoring open legs and keep booking into cumulative_pnl."""
        sid = getattr(self, '_sid', None)
        if not sid:
            # Fallback: discover our sid from the app registry (mirrors
            # EMACreditSpread) so persistence survives a missing _sid.
            try:
                from app import builder_scheduler_strategies
                for s_id, entry in builder_scheduler_strategies.items():
                    if entry.get('strategy') is self:
                        sid = s_id
                        self._sid = sid
                        break
            except Exception:
                pass
        if not sid:
            return
        try:
            from models import update_strategy_db
            with self._open_lock:
                open_positions = [dict(p) for p in self._open_positions]
            # Flatten open legs for the generic legs column / dashboard.
            flat_legs = []
            for p in open_positions:
                for l in p.get('legs', []):
                    flat_legs.append({**l, 'entry_id': p.get('entry_id'),
                                      'expiry': p.get('expiry')})
            details = {**self.config,
                       'cumulative_pnl': round(self.cumulative_pnl, 4),
                       'total_entries': self.total_entries,
                       'trade_log': self.trade_log[-200:],
                       'open_positions': open_positions}
            update_strategy_db(sid, details=details, legs=flat_legs,
                               pnl=round(self.cumulative_pnl, 4))
        except Exception as e:
            logger.warning("[Builder Sched] persist failed: %s", e)

    def restore_state(self, details=None, legs=None):
        """Rehydrate from a persisted DB row and re-attach a StrategyMonitor +
        expiry watcher to each previously-open position so TP/SL and the
        square-off/expiry exit resume after a restart."""
        details = details or {}
        self.cumulative_pnl = float(details.get('cumulative_pnl', 0) or 0)
        self.total_entries = int(details.get('total_entries', 0) or 0)
        self.trade_log = details.get('trade_log', []) or []
        restored = details.get('open_positions') or []
        with self._open_lock:
            self._open_positions = [dict(p) for p in restored]
        # Re-arm monitors/watchers for each restored open position.
        for pos in list(self._open_positions):
            try:
                self._rearm_position(pos)
            except Exception as e:
                logger.warning("[Builder Sched] re-arm failed for %s: %s",
                               pos.get('entry_id'), e)
        self._print(f"[Builder Sched] Restored {len(self._open_positions)} open "
                    f"position(s) | Cum ${self.cumulative_pnl:+.2f}")
        return self

    def _rearm_position(self, pos):
        """Re-create the TP/SL monitor + expiry watcher for a restored position."""
        from strategy.monitor import StrategyMonitor
        from config import get_contract_value
        placed = pos.get('legs', [])
        expiry = pos.get('expiry')
        entry_id = pos.get('entry_id')
        if not placed:
            return
        try:
            cv = get_contract_value(self.asset)
        except Exception:
            cv = 0.001
        self._cv = cv
        total_prem = sum(l['entry_price'] * l['size'] for l in placed if l['side'] == 'sell')
        max_profit = total_prem * cv * self.target_pct / 100 if self.target_pct else 0
        max_loss = total_prem * cv * self.sl_pct / 100 if self.sl_pct else 0
        mon = None
        if max_profit > 0 or max_loss > 0:
            _HUGE = 1e12
            mon = StrategyMonitor(legs=placed,
                                  max_profit=max_profit if max_profit > 0 else _HUGE,
                                  max_loss=max_loss if max_loss > 0 else _HUGE,
                                  asset=self.asset, lot_size=cv)
            mon.user_id = self._user_id
            mon.sid = self._sid
            mon.profile_id = self.profile_id
            mon.on_complete = lambda pnl, reason, eid=entry_id: self._on_position_closed(eid, pnl, reason)
            mon.start()
            self._monitors.append(mon)
        t = threading.Thread(target=self._expiry_exit_watch,
                             args=(entry_id, placed, expiry, mon), daemon=True)
        t.start()

    def _print(self, msg):
        print(msg)
        # When app.py installs LogCapture as stdout and this thread is bound to
        # our queue (via _apply_thread_context), print() above has ALREADY
        # routed the line into our log queue + history. Appending again here
        # was what made every "[Builder Sched]" line appear twice. Only append
        # manually when print() isn't captured for this strategy on this thread.
        cap = type(sys.stdout)
        local = getattr(cap, '_local', None)
        if cap.__name__ == 'LogCapture' and self._log_queue is not None \
                and getattr(local, 'log_queue', None) is self._log_queue:
            return
        try:
            if self._log_history is not None:
                self._log_history.append(msg)
                if len(self._log_history) > 500:
                    del self._log_history[:len(self._log_history) - 500]
            if self._log_queue is not None:
                self._log_queue.put_nowait(msg)
        except Exception:
            pass
