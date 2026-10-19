"""NSE Dual Momentum Stocks — intraday long/short equity basket (Indian market).

The Dual Momentum thesis applied to NSE stocks, intraday:

Universe : At entry time, the top-N NSE gainers (by % change) and top-N losers.
Entry    : Gainers -> BUY (long), Losers -> SELL (short), a fixed ₹ amount each.
           Share quantity is a whole number: qty = floor(amount / price). A stock
           whose single share costs more than the per-stock budget is skipped.
Exit     : Each position is HELD until its side-aware TP% / SL% hits (checked
           every price_check_interval). Any position still open at the square-off
           time (default 15:15 IST, hard EOD 15:30) is force-closed.
Schedule : Universe re-scanned every refresh_interval (hourly). No re-entry
           cooldown. New entries stop once past the square-off time for the day.
Days     : Trades on configurable weekdays (default Mon–Fri).

Modes    : dry_run=True  -> paper trading. No real orders; fills simulated at the
           last traded price; ₹ PnL tracked from live NSE prices. (default)
           dry_run=False -> places real intraday (MIS) equity orders via Groww.

Persistence mirrors NseDailyStrangle / DualMomentumBasket: state (open positions,
realized PnL, trade history) is written to the live_strategies DB row keyed by
self._sid via models.update_strategy_db, so it survives a server restart.
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone

from api.nse import get_nse_top_movers, get_nse_oi_changes
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Defaults
TOP_N = 5
AMOUNT_PER_STOCK = 10000.0      # ₹ invested per stock (whole shares)
TP_PCT = 0.10                   # +10% in the trade's favour -> take profit
SL_PCT = 0.05                   # -5% against the trade      -> stop loss
MIN_OI_CHANGE_PCT = 10.0        # only trade stocks whose OI change > +10%
ENTRY_HOUR = 9
ENTRY_MINUTE = 30
EXIT_HOUR = 15
EXIT_MINUTE = 15
MONITOR_INTERVAL = 30           # (legacy) kept for config compatibility
REFRESH_INTERVAL = 1800         # half-hourly universe rescan
PRICE_CHECK_INTERVAL = 60       # seconds between TP/SL price checks
REENTRY_COOLDOWN_HOURS = 24     # after a TP/SL exit, block re-entry this long
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30
DEFAULT_TRADING_DAYS = [0, 1, 2, 3, 4]   # 0=Mon .. 4=Fri


class NseDualMomentumStocks(BaseStrategy):
    """Long gainers / short losers basket on NSE stocks (paper or live).

    Hourly rescan; positions held until TP/SL (no intraday square-off); 24h
    re-entry cooldown after a TP/SL exit — mirroring EmaTrendFollower.
    """

    def __init__(self, top_n=TOP_N, amount_per_stock=AMOUNT_PER_STOCK,
                 entry_hour=ENTRY_HOUR, entry_minute=ENTRY_MINUTE,
                 exit_hour=EXIT_HOUR, exit_minute=EXIT_MINUTE,
                 trading_days=None, dry_run=True,
                 tp_pct=TP_PCT, sl_pct=SL_PCT,
                 min_oi_change_pct=MIN_OI_CHANGE_PCT,
                 monitor_interval=MONITOR_INTERVAL, run_immediately=False,
                 refresh_interval=REFRESH_INTERVAL,
                 price_check_interval=PRICE_CHECK_INTERVAL):
        self.top_n = top_n
        self.amount_per_stock = amount_per_stock
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.min_oi_change_pct = min_oi_change_pct
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute
        self.exit_hour = exit_hour
        self.exit_minute = exit_minute
        self.monitor_interval = monitor_interval
        self.refresh_interval = refresh_interval
        self.price_check_interval = price_check_interval
        self.trading_days = trading_days if trading_days is not None else DEFAULT_TRADING_DAYS
        self.dry_run = dry_run
        # Testing: when True, the first monitor() iteration enters immediately.
        self.run_immediately = run_immediately

        # Each leg (a position):
        #   {symbol, side('buy'|'sell'), qty(shares), entry_price, opened_at, stopped}
        self.legs = []
        self._legs_lock = threading.Lock()

        self.cumulative_pnl = 0.0
        self.total_trades = 0
        self.total_days_traded = 0
        self.trade_log = []
        self._running = False
        self._entered_today_slot = None   # 'YYYY-MM-DD' of the last entry run
        self._pnl_history = []
        self._snap_counter = 0
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10

        # App-integration hooks (set externally after creation).
        self._sid = None
        self._api_key = None
        self._api_secret = None
        self._broker = None
        self._log_queue = None
        self._log_history = None
        self._user_id = None

        self._base_params = {
            'top_n': top_n,
            'amount_per_stock': amount_per_stock,
            'tp_pct': tp_pct,
            'sl_pct': sl_pct,
            'min_oi_change_pct': min_oi_change_pct,
            'entry_hour': entry_hour,
            'entry_minute': entry_minute,
            'exit_hour': exit_hour,
            'exit_minute': exit_minute,
            'monitoring_interval': monitor_interval,
            'refresh_interval': refresh_interval,
            'price_check_interval': price_check_interval,
            'trading_days': self.trading_days,
            'dry_run': dry_run,
            'run_immediately': run_immediately,
        }

    # ---- BaseStrategy interface -------------------------------------------
    def initialize(self):
        self._running = True
        from config import get_api_key, get_api_secret, _thread_local
        self._api_key = get_api_key()
        self._api_secret = get_api_secret()
        self._broker = getattr(_thread_local, 'broker', '')
        mode = 'PAPER' if self.dry_run else 'LIVE'
        days_str = ','.join(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][d] for d in self.trading_days)
        print(f"[NSE Dual Momentum] Started [{mode}] | Top {self.top_n} gainers (LONG) "
              f"+ top {self.top_n} losers (SHORT)")
        print(f"[NSE Dual Momentum] ₹{self.amount_per_stock:.0f}/stock (whole shares) | "
              f"TP +{self.tp_pct*100:.1f}% / SL -{self.sl_pct*100:.1f}% (side-aware)")
        if self.min_oi_change_pct and self.min_oi_change_pct > 0:
            print(f"[NSE Dual Momentum] Entry filter: F&O OI change > +{self.min_oi_change_pct:.1f}%")
        print(f"[NSE Dual Momentum] Hold until TP/SL, else square-off at "
              f"{self.exit_hour}:{self.exit_minute:02d} IST | Rescan {self.refresh_interval}s | "
              f"Price check {self.price_check_interval}s | Days: {days_str}"
              + ("  | RUN-NOW (test)" if self.run_immediately else ""))
        with self._legs_lock:
            if self.legs:
                desc = ', '.join(f"{l['side'][0].upper()}:{l['symbol']}" for l in self.legs)
                print(f"[NSE Dual Momentum] Resumed with {len(self.legs)} open position(s): {desc}")
        return True

    def _set_thread_credentials(self):
        from config import set_thread_credentials
        if getattr(self, '_api_key', None):
            set_thread_credentials(self._api_key, self._api_secret, self._broker)

    def monitor(self):
        """Hourly rescan loop + a background risk thread for TP/SL (mirrors
        EmaTrendFollower). Positions are held until TP/SL — no square-off."""
        self._apply_thread_context()
        risk_thread = threading.Thread(target=self._risk_loop, daemon=True)
        risk_thread.start()
        first = True
        while self._running:
            try:
                if first and self.run_immediately:
                    print("[NSE Dual Momentum] run_immediately — scanning now (test mode)")
                self._evaluate_once()
            except Exception as e:
                logger.error("[NSE Dual Momentum] Evaluation cycle failed: %s", e)
            first = False
            self._interruptible_sleep(self.refresh_interval)

    def _risk_loop(self):
        """Fast loop: check held positions against side-aware TP/SL. If a
        position hasn't hit TP/SL by the 15:15 square-off time, force-close it."""
        self._apply_thread_context()
        while self._running:
            self._interruptible_sleep(self.price_check_interval)
            if not self._running:
                break
            try:
                if self._past_squareoff(datetime.now(IST)):
                    with self._legs_lock:
                        any_open = any(not l.get('stopped') for l in self.legs)
                    if any_open:
                        print(f"[NSE Dual Momentum] ⏰ 15:15 square-off — closing open positions")
                        self._close_all_open(reason='square_off')
                else:
                    self._check_tp_sl('[risk]')
            except Exception as e:
                logger.error("[NSE Dual Momentum] Risk check failed: %s", e)

    def _evaluate_once(self):
        """One rescan: fetch gainers/losers, enter new positions (OI-confirmed,
        not already held, not in cooldown). Held positions exit only via TP/SL."""
        now = datetime.now(IST)
        if now.weekday() not in self.trading_days:
            return
        # Don't open new positions at/after square-off time (they'd just be
        # closed again); entries only during the trading session before 15:15.
        if self._past_squareoff(now):
            return
        ts = now.strftime('%Y-%m-%d %H:%M')
        tag = f"[NSE DM {ts}]"
        gainers = get_nse_top_movers('gainers', limit=self.top_n)
        losers = get_nse_top_movers('losers', limit=self.top_n)
        if not gainers and not losers:
            print(f"{tag} No movers data — skipping cycle")
            return
        print(f"\n{tag} Scan | gainers={len(gainers)} losers={len(losers)}")
        # OI-change gate: only trade a stock whose F&O open interest rose more
        # than min_oi_change_pct (fresh positioning confirming the move).
        oi_changes = {}
        if self.min_oi_change_pct and self.min_oi_change_pct > 0:
            try:
                oi_changes = get_nse_oi_changes()
            except Exception as e:
                logger.warning("[NSE Dual Momentum] OI fetch failed: %s", e)
            if not oi_changes:
                print(f"{tag} ⚠ OI data unavailable — OI filter skipped this run")
        for m in gainers:
            if self._oi_confirms(m['symbol'], oi_changes, tag):
                self._enter(m['symbol'], 'buy', m.get('ltp'), m.get('change_pct'))
        for m in losers:
            if self._oi_confirms(m['symbol'], oi_changes, tag):
                self._enter(m['symbol'], 'sell', m.get('ltp'), m.get('change_pct'))
        self._persist_state()
        self._print_summary()

    def _oi_confirms(self, symbol, oi_changes, tag=''):
        """True if the stock's OI change exceeds min_oi_change_pct.

        Gate is active only when min_oi_change_pct > 0 AND OI data is available;
        otherwise it passes (so paper testing works when the OI feed is empty).
        A stock absent from the F&O OI list is treated as not confirmed.
        """
        if not self.min_oi_change_pct or self.min_oi_change_pct <= 0 or not oi_changes:
            return True
        oi_pct = oi_changes.get(symbol)
        if oi_pct is None:
            print(f"{tag} Skip {symbol} — no F&O OI data (not in OI-spurts list)")
            return False
        if oi_pct > self.min_oi_change_pct:
            return True
        print(f"{tag} Skip {symbol} — OI change {oi_pct:+.2f}% <= "
              f"{self.min_oi_change_pct:.1f}% threshold")
        return False

    def _past_squareoff(self, now):
        """True at/after the configured exit time (default 15:15) or hard EOD 15:30."""
        if now.hour > self.exit_hour or (now.hour == self.exit_hour and now.minute >= self.exit_minute):
            return True
        if now.hour > MARKET_CLOSE_HOUR or (now.hour == MARKET_CLOSE_HOUR and now.minute >= MARKET_CLOSE_MINUTE):
            return True
        return False

    # ---- entries / exits ---------------------------------------------------
    def _compute_qty(self, price):
        """Whole number of shares for ~amount_per_stock. 0 if one share is over budget."""
        if not price or price <= 0:
            return 0
        return int(self.amount_per_stock // price)

    def _enter(self, symbol, side, price, change_pct=None):
        with self._legs_lock:
            if any(l['symbol'] == symbol and l['side'] == side and not l.get('stopped')
                   for l in self.legs):
                return  # already holding this symbol on this side
        if not price or price <= 0:
            logger.warning("[NSE Dual Momentum] Skip %s — no price", symbol)
            return
        qty = self._compute_qty(price)
        if qty <= 0:
            print(f"[NSE Dual Momentum] Skip {symbol} — 1 share (₹{price:.2f}) "
                  f"exceeds ₹{self.amount_per_stock:.0f} budget")
            return
        est = qty * price
        tag = 'LONG' if side == 'buy' else 'SHORT'
        chg = f"{change_pct:+.2f}%" if change_pct is not None else '?'

        if self.dry_run:
            print(f"[NSE Dual Momentum] [PAPER] {tag} {side.upper()} {symbol} x{qty} "
                  f"@ ₹{price:.2f} (₹{est:.0f}) | 24h {chg}")
            fill = price
        else:
            fill = self._place(symbol, qty, 'BUY' if side == 'buy' else 'SELL')
            if fill is None:
                logger.warning("[NSE Dual Momentum] %s %s failed — not recording", side.upper(), symbol)
                return
            print(f"[NSE Dual Momentum] {tag} {side.upper()} {symbol} x{qty} "
                  f"filled @ ₹{fill:.2f} (₹{est:.0f})")

        leg = {
            'symbol': symbol, 'side': side, 'qty': qty, 'size': qty,
            'entry_price': fill, 'current_mark': fill, 'current_pnl': 0.0,
            'opened_at': datetime.now(IST).isoformat(), 'stopped': False,
        }
        with self._legs_lock:
            self.legs.append(leg)
        self.total_trades += 1
        self.trade_log.append({
            'ts': datetime.now(IST).isoformat(), 'date': datetime.now(IST).strftime('%Y-%m-%d'),
            'action': side.upper(), 'symbol': symbol, 'qty': qty,
            'price': round(fill, 2), 'amount': round(est, 2),
        })

    def _check_tp_sl(self, tag):
        """Close any open position whose favourable move hit +tp_pct or whose
        adverse move hit -sl_pct. Side-aware."""
        with self._legs_lock:
            open_legs = [l for l in self.legs if not l.get('stopped')]
        if not open_legs:
            return
        prices = self._prices([l['symbol'] for l in open_legs])
        if not prices:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_consecutive_failures:
                print(f"{tag} 🚨 EMERGENCY: {self._consecutive_failures} price failures — closing all")
                self._close_all_open(reason='emergency')
            return
        self._consecutive_failures = 0
        tick_pnl = 0.0
        for leg in open_legs:
            px = prices.get(leg['symbol'])
            if not px or px <= 0:
                continue
            entry = leg['entry_price']
            raw = (px - entry) / entry
            favour = raw if leg['side'] == 'buy' else -raw
            sign = 1 if leg['side'] == 'buy' else -1
            leg['current_mark'] = round(px, 2)
            leg['current_pnl'] = round(sign * (px - entry) * leg['qty'], 2)
            tick_pnl += leg['current_pnl']
            # Small epsilon so an exact-threshold move isn't missed by float rounding.
            _eps = 1e-9
            if favour >= self.tp_pct - _eps:
                self._exit(leg['symbol'], leg['side'], px, reason='take_profit', tag=tag)
            elif favour <= -self.sl_pct + _eps:
                self._exit(leg['symbol'], leg['side'], px, reason='stop_loss', tag=tag)
        # PnL history + periodic snapshot
        now_iso = datetime.now(IST).isoformat()
        self._pnl_history.append((now_iso, round(self.cumulative_pnl + tick_pnl, 2)))
        if len(self._pnl_history) > 500:
            self._pnl_history = self._pnl_history[-500:]
        self._snap_counter += 1
        if self._snap_counter % 6 == 0 and getattr(self, '_sid', None):
            try:
                from models import save_pnl_snapshot
                if self._user_id:
                    save_pnl_snapshot(self._user_id, self._sid, round(self.cumulative_pnl + tick_pnl, 2))
            except Exception:
                pass

    def _exit(self, symbol, side, price, reason='', tag=''):
        with self._legs_lock:
            leg = next((l for l in self.legs
                        if l['symbol'] == symbol and l['side'] == side and not l.get('stopped')), None)
        if not leg:
            return
        sign = 1 if leg['side'] == 'buy' else -1
        fill = price
        if not self.dry_run:
            close_side = 'SELL' if leg['side'] == 'buy' else 'BUY'
            res = self._place(symbol, leg['qty'], close_side)
            if res is None:
                logger.warning("[NSE Dual Momentum] CLOSE %s failed — keeping for retry", symbol)
                return
            fill = res
        realized = sign * (fill - leg['entry_price']) * leg['qty']
        with self._legs_lock:
            self.cumulative_pnl += realized
            leg['stopped'] = True
            leg['exit_price'] = round(fill, 2)
        prefix = '[PAPER] ' if self.dry_run else ''
        print(f"{tag} {prefix}CLOSE {('SELL' if leg['side']=='buy' else 'BUY')} {symbol} "
              f"x{leg['qty']} @ ₹{fill:.2f} | {reason} | PnL ₹{realized:+.2f}")
        self.trade_log.append({
            'ts': datetime.now(IST).isoformat(), 'date': datetime.now(IST).strftime('%Y-%m-%d'),
            'action': f'CLOSE_{leg["side"].upper()}', 'symbol': symbol, 'qty': leg['qty'],
            'price': round(fill, 2), 'pnl': round(realized, 2), 'reason': reason,
        })
        self._persist_state()

    def _close_all_open(self, reason='square_off'):
        with self._legs_lock:
            open_legs = [l for l in self.legs if not l.get('stopped')]
        prices = self._prices([l['symbol'] for l in open_legs]) if open_legs else {}
        for leg in open_legs:
            px = prices.get(leg['symbol']) or leg.get('current_mark') or leg['entry_price']
            self._exit(leg['symbol'], leg['side'], px, reason=reason, tag='[square-off]')

    def close_all(self):
        """Close every open position (used on manual stop / shutdown)."""
        self._running = False
        self._close_all_open(reason='shutdown')
        self._persist_state()

    @property
    def pnl(self):
        """Realized + open unrealized ₹ PnL across held longs and shorts."""
        open_pnl = 0.0
        with self._legs_lock:
            open_legs = [l for l in self.legs if not l.get('stopped')]
        if open_legs:
            prices = self._prices([l['symbol'] for l in open_legs])
            for leg in open_legs:
                px = prices.get(leg['symbol']) or leg.get('current_mark') or leg['entry_price']
                sign = 1 if leg['side'] == 'buy' else -1
                open_pnl += sign * (px - leg['entry_price']) * leg['qty']
        return self.cumulative_pnl + open_pnl

    # ---- data / broker helpers --------------------------------------------
    def _prices(self, symbols):
        """Return {symbol: last_price} for the given stock symbols.

        Uses the NSE top-movers board (gainers+losers) as a free price source
        that needs no credentials — ideal for paper mode. Symbols not currently
        on either board won't update this tick (handled by the caller).
        """
        if not symbols:
            return {}
        wanted = set(symbols)
        out = {}
        for direction in ('gainers', 'losers'):
            try:
                for m in get_nse_top_movers(direction, limit=50):
                    if m['symbol'] in wanted and m.get('ltp'):
                        out[m['symbol']] = m['ltp']
            except Exception as e:
                logger.debug("[NSE Dual Momentum] price fetch (%s) failed: %s", direction, e)
        return out

    def _place(self, symbol, qty, transaction_type):
        """Place a real intraday (MIS) equity order via Groww. Returns fill price
        or None on failure. Only called when dry_run is False."""
        try:
            from api.groww import place_order, get_option_ltp
        except Exception as e:
            logger.error("[NSE Dual Momentum] Groww unavailable: %s", e)
            return None
        try:
            resp = place_order(
                trading_symbol=symbol, quantity=int(qty),
                transaction_type=transaction_type, order_type='MARKET',
                product='MIS')
            if isinstance(resp, dict) and resp.get('error'):
                logger.warning("[NSE Dual Momentum] order error %s: %s", symbol, resp['error'])
                return None
            # Best-effort fill price; fall back to last traded price.
            fill = None
            if isinstance(resp, dict):
                fill = resp.get('average_fill_price') or resp.get('filled_price')
            if not fill:
                try:
                    fill = get_option_ltp(symbol)  # works for equity CASH too via LTP
                except Exception:
                    fill = None
            return float(fill) if fill else None
        except Exception as e:
            logger.error("[NSE Dual Momentum] place_order failed for %s: %s", symbol, e)
            return None

    # ---- reporting ---------------------------------------------------------
    def _print_summary(self):
        with self._legs_lock:
            longs = sorted(l['symbol'] for l in self.legs if l['side'] == 'buy' and not l.get('stopped'))
            shorts = sorted(l['symbol'] for l in self.legs if l['side'] == 'sell' and not l.get('stopped'))
        print(f"[NSE Dual Momentum] Open: {len(longs)+len(shorts)} "
              f"(L:{len(longs)} / S:{len(shorts)}) | Realized ₹{self.cumulative_pnl:+.2f}")
        if longs:
            print(f"[NSE Dual Momentum]   LONG : {', '.join(longs)}")
        if shorts:
            print(f"[NSE Dual Momentum]   SHORT: {', '.join(shorts)}")

    # ---- app/thread integration -------------------------------------------
    def _apply_thread_context(self):
        self._set_thread_credentials()
        try:
            if self._log_queue is not None:
                from app import LogCapture
                LogCapture._local.log_queue = self._log_queue
                LogCapture._local.log_history = self._log_history
        except Exception:
            pass

    # ---- persistence -------------------------------------------------------
    def _persist_state(self):
        try:
            from models import update_strategy_db
        except Exception:
            return
        sid = getattr(self, '_sid', None)
        if not sid:
            try:
                from app import nse_dual_momentum_strategies
                for s_id, entry in nse_dual_momentum_strategies.items():
                    if entry.get('strategy') is self:
                        sid = s_id
                        self._sid = sid
                        break
            except Exception:
                pass
        if not sid:
            return
        try:
            with self._legs_lock:
                legs_data = [dict(l) for l in self.legs if not l.get('stopped')]
                cum = self.cumulative_pnl
            details = {**self._base_params,
                       'trade_log': self.trade_log[-100:],
                       'cumulative_pnl': cum,
                       'total_trades': self.total_trades,
                       'total_days_traded': self.total_days_traded,
                       'entered_today_slot': self._entered_today_slot}
            update_strategy_db(sid, details=details, legs=legs_data, pnl=round(cum, 2))
        except Exception as e:
            logger.warning("[NSE Dual Momentum] Failed to persist state: %s", e)

    def restore_state(self, details=None, legs=None):
        details = details or {}
        self.cumulative_pnl = float(details.get('cumulative_pnl', 0) or 0)
        self.total_trades = int(details.get('total_trades', 0) or 0)
        self.total_days_traded = int(details.get('total_days_traded', 0) or 0)
        self.trade_log = details.get('trade_log', []) or []
        self._entered_today_slot = details.get('entered_today_slot')
        restored = []
        for l in (legs or []):
            restored.append({
                'symbol': l.get('symbol', ''),
                'side': l.get('side', 'buy'),
                'qty': int(l.get('qty', l.get('size', 0)) or 0),
                'size': int(l.get('qty', l.get('size', 0)) or 0),
                'entry_price': float(l.get('entry_price', 0) or 0),
                'current_mark': float(l.get('current_mark', l.get('entry_price', 0)) or 0),
                'current_pnl': float(l.get('current_pnl', 0) or 0),
                'opened_at': l.get('opened_at', ''),
                'stopped': bool(l.get('stopped', False)),
            })
        with self._legs_lock:
            self.legs = restored
        logger.info("[NSE Dual Momentum] Restored %d position(s) | Cum PnL ₹%.2f",
                    len(restored), self.cumulative_pnl)
        return self

    # ---- timing ------------------------------------------------------------
    def _wait_for_next_entry(self):
        now = datetime.now(IST)
        entry_today = now.replace(hour=self.entry_hour, minute=self.entry_minute,
                                  second=0, microsecond=0)
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
            print(f"[NSE Dual Momentum] Next entry: {target.strftime('%Y-%m-%d %H:%M')} IST "
                  f"({day_name}, {wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(15, end - time.time()))
