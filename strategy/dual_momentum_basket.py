"""Dual Momentum Basket — twice-daily long/short momentum basket on Delta perps.

A scheduled long/short basket that rebalances at fixed clock times (default
06:00 and 18:00 IST):

Universe : At each run, the top-N perpetuals by 24h price change in each
           direction — top-N *gainers* and top-N *losers*.
Entry    : Gainers  -> open LONG  (buy), ~notional_usd each.
           Losers   -> open SHORT (sell), ~notional_usd each.
           A coin already held on the same side is left alone (no double-open).
Exit (per-position, tracked continuously):
           TP : +tp_pct in the trade's favour (long: price up; short: price down)
           SL : -sl_pct against the trade    (long: price down; short: price up)
Exit (list rotation, at each scheduled run):
           If a currently-open coin is no longer in the fresh list *for its side*
           (a held long not in the new gainers list, or a held short not in the
           new losers list), it is closed. Coins still in the fresh list stay.

Modes    : dry_run=True  -> logs intended orders, no real trades (default).
           dry_run=False -> places live market orders via api.orders.place_order.

Persistence mirrors EmaTrendFollower / EMACreditSpread: state (open legs,
realized PnL, trade history) is written to the live_strategies DB row keyed by
self._sid via models.update_strategy_db, so positions survive a restart.
app.py reconstructs the object and calls restore_state() with saved details+legs.
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone

import config
from api.orders import place_order
from api.pricing import get_futures_price, get_futures_prices_bulk
from api.position_tracker import position_tracker
from api.top_coins import get_top_coins_by_change
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

TOP_N = 10
POSITION_NOTIONAL_USD = 10        # fixed USD invested per coin
RUN_HOURS_IST = (6, 18)          # scheduled rebalance times, IST hour-of-day
TP_PCT = 0.10                    # +10% in the trade's favour -> take profit
SL_PCT = 0.05                    # -5% against the trade       -> stop loss
MIN_OI_CHANGE_PCT = 10.0         # only trade coins whose 6h OI change > +10%
PRICE_CHECK_INTERVAL = 60        # seconds between TP/SL price checks
SCHEDULE_CHECK_INTERVAL = 30     # seconds between schedule-time checks


class DualMomentumBasket(BaseStrategy):
    """Twice-daily long/short momentum basket (long gainers, short losers)."""

    def __init__(self, top_n=TOP_N, notional_usd=POSITION_NOTIONAL_USD,
                 run_hours=RUN_HOURS_IST, rotate_on_runs='all', dry_run=True,
                 tp_pct=TP_PCT, sl_pct=SL_PCT,
                 min_oi_change_pct=MIN_OI_CHANGE_PCT,
                 price_check_interval=PRICE_CHECK_INTERVAL):
        self.top_n = top_n
        self.notional_usd = notional_usd
        # Normalize run hours to a sorted unique tuple of ints 0..23.
        self.run_hours = tuple(sorted({int(h) % 24 for h in run_hours}))
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.min_oi_change_pct = min_oi_change_pct
        self.price_check_interval = price_check_interval
        # 'all'  -> rotate (close delisted) on every scheduled run (6 AM & 6 PM)
        # 'evening' -> only on the last run hour of the day
        self.rotate_on_runs = rotate_on_runs
        self.dry_run = dry_run

        # Each leg (a position):
        #   {symbol, product_id, coin, side('buy'|'sell'), size(lots),
        #    entry_price, contract_value, opened_at}
        self.legs = []
        self._legs_lock = threading.Lock()

        self.cumulative_pnl = 0.0
        self.total_trades = 0
        self.trade_log = []
        self._running = False

        # Guard so each scheduled hour fires at most once per calendar day.
        # Stores the "YYYY-MM-DD HH" slot string of the last completed run.
        self._last_run_slot = None

        # product_id/contract_value cache: {symbol: (product_id, contract_value)}
        self._meta_cache = {}
        self._meta_lock = threading.Lock()

        # App-integration hooks (set externally after creation, like ECS)
        self._sid = None
        self._api_key = None
        self._api_secret = None
        self._broker = None
        self._log_queue = None
        self._log_history = None
        self._user_id = None
        self._pnl_history = []
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10

        self._base_params = {
            'top_n': top_n,
            'notional_usd': notional_usd,
            'run_hours': list(self.run_hours),
            'tp_pct': tp_pct,
            'sl_pct': sl_pct,
            'min_oi_change_pct': min_oi_change_pct,
            'price_check_interval': price_check_interval,
            'rotate_on_runs': rotate_on_runs,
            'dry_run': dry_run,
        }

    # ---- BaseStrategy interface -------------------------------------------
    def initialize(self):
        self._running = True
        mode = 'DRY-RUN' if self.dry_run else 'LIVE'
        hours = ', '.join(f'{h:02d}:00' for h in self.run_hours)
        print(f"[Dual Momentum] Started [{mode}] | Runs at {hours} IST")
        print(f"[Dual Momentum] Basket: top {self.top_n} gainers (LONG) + "
              f"top {self.top_n} losers (SHORT) | ${self.notional_usd}/coin")
        print(f"[Dual Momentum] Risk: TP +{self.tp_pct*100:.1f}% / "
              f"SL -{self.sl_pct*100:.1f}% (side-aware) | "
              f"Price check {self.price_check_interval}s | "
              f"Rotate: {self.rotate_on_runs}")
        print(f"[Dual Momentum] Entry filter: 6h OI change > "
              f"+{self.min_oi_change_pct:.1f}%")
        with self._legs_lock:
            if self.legs:
                desc = ', '.join(f"{l['side'][0].upper()}:{l['symbol']}" for l in self.legs)
                print(f"[Dual Momentum] Resumed with {len(self.legs)} position(s): {desc}")
        return True

    def monitor(self):
        """Blocking loop: wait for scheduled run times; a daemon thread runs the
        faster TP/SL price checks so exits are acted on within
        price_check_interval seconds rather than only at the next scheduled run.
        """
        self._apply_thread_context()
        risk_thread = threading.Thread(target=self._risk_loop, daemon=True)
        risk_thread.start()
        while self._running:
            try:
                if self._due_now():
                    self.run_rebalance()
            except Exception as e:
                logger.error("[Dual Momentum] Schedule check failed: %s", e)
            self._interruptible_sleep(SCHEDULE_CHECK_INTERVAL)

    def _due_now(self):
        """True once per scheduled hour-slot per day (edge-triggered)."""
        now = datetime.now(IST)
        if now.hour not in self.run_hours:
            return False
        slot = now.strftime('%Y-%m-%d %H')
        if slot == self._last_run_slot:
            return False
        self._last_run_slot = slot
        return True

    def _risk_loop(self):
        """Fast loop: check held positions against side-aware TP/SL thresholds."""
        self._apply_thread_context()
        while self._running:
            self._interruptible_sleep(self.price_check_interval)
            if not self._running:
                break
            try:
                self._check_tp_sl()
            except Exception as e:
                logger.error("[Dual Momentum] TP/SL check failed: %s", e)

    def _check_tp_sl(self):
        """Close any held position whose favourable move hit +tp_pct or whose
        adverse move hit -sl_pct. Direction is side-aware."""
        with self._legs_lock:
            legs_copy = list(self.legs)
        if not legs_copy:
            return
        marks = get_futures_prices_bulk([l['symbol'] for l in legs_copy])
        for leg in legs_copy:
            md = marks.get(leg['symbol'])
            if not md or not md.get('mark_price'):
                continue
            entry = leg['entry_price']
            if entry <= 0:
                continue
            raw = (md['mark_price'] - entry) / entry
            # favourable move: +raw for longs, -raw for shorts.
            favour = raw if leg['side'] == 'buy' else -raw
            if favour >= self.tp_pct:
                self._exit(leg['symbol'], reason='take_profit')
            elif favour <= -self.sl_pct:
                self._exit(leg['symbol'], reason='stop_loss')

    def close_all(self):
        """Close every open position (used on shutdown)."""
        self._running = False
        with self._legs_lock:
            legs_copy = list(self.legs)
        for leg in legs_copy:
            self._exit(leg['symbol'], reason='shutdown')
        self._persist_state()

    @property
    def pnl(self):
        """Realized + open unrealized PnL across held longs and shorts."""
        open_pnl = 0.0
        with self._legs_lock:
            legs_copy = list(self.legs)
        marks = get_futures_prices_bulk([l['symbol'] for l in legs_copy])
        for leg in legs_copy:
            md = marks.get(leg['symbol'])
            if md and md.get('mark_price'):
                sign = 1 if leg['side'] == 'buy' else -1
                open_pnl += sign * (md['mark_price'] - leg['entry_price']) * leg['size'] * leg['contract_value']
        return self.cumulative_pnl + open_pnl

    # ---- Core logic --------------------------------------------------------
    def run_rebalance(self):
        """One scheduled run: fetch fresh gainers/losers, rotate out delisted
        positions, then open new long/short positions."""
        ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
        gainers = get_top_coins_by_change(limit=self.top_n, direction='gainers')
        losers = get_top_coins_by_change(limit=self.top_n, direction='losers')
        if not gainers and not losers:
            logger.warning("[Dual Momentum] Top-coin fetch empty — skipping run")
            self._consecutive_failures += 1
            return
        self._consecutive_failures = 0

        gain_syms = {c['symbol']: c for c in gainers}
        lose_syms = {c['symbol']: c for c in losers}
        print(f"\n[Dual Momentum] [{ts}] Run | gainers={len(gain_syms)} "
              f"losers={len(lose_syms)}")

        # 1) ROTATION pass — close held coins no longer valid for their side.
        if self._rotation_enabled_now():
            with self._legs_lock:
                legs_copy = list(self.legs)
            for leg in legs_copy:
                sym, side = leg['symbol'], leg['side']
                valid = (side == 'buy' and sym in gain_syms) or \
                        (side == 'sell' and sym in lose_syms)
                if not valid:
                    self._exit(sym, reason='delisted from basket')

        # 2) ENTRY pass — open longs for gainers, shorts for losers, but only
        #    for coins whose 6h open-interest change exceeds the threshold
        #    (rising OI = fresh positioning confirming the move).
        with self._legs_lock:
            held = {(l['symbol'], l['side']) for l in self.legs}
        for sym, meta in gain_syms.items():
            if (sym, 'buy') not in held and self._oi_confirms(sym, meta):
                self._enter(sym, meta.get('coin', ''), 'buy', meta.get('change_24h'))
        for sym, meta in lose_syms.items():
            if (sym, 'sell') not in held and self._oi_confirms(sym, meta):
                self._enter(sym, meta.get('coin', ''), 'sell', meta.get('change_24h'))

        self._persist_state()
        self._print_summary()

    def _oi_confirms(self, symbol, meta):
        """True if the coin's 6h OI change exceeds min_oi_change_pct.

        Rising open interest means new positions are being opened behind the
        price move (genuine buildup) rather than an existing position unwinding.
        A non-positive or threshold-missing OI change skips the entry.
        """
        oi_pct = meta.get('oi_change_6h_pct', 0.0)
        if oi_pct > self.min_oi_change_pct:
            return True
        print(f"[Dual Momentum] Skip {symbol} — 6h OI change {oi_pct:+.2f}% "
              f"<= {self.min_oi_change_pct:.1f}% threshold")
        return False

    def _rotation_enabled_now(self):
        if self.rotate_on_runs == 'all':
            return True
        if self.rotate_on_runs == 'evening':
            # Only on the latest scheduled hour of the day.
            return datetime.now(IST).hour == max(self.run_hours)
        return True

    def _compute_lots(self, mark_price, contract_value):
        """Integer contracts to reach ~notional_usd, minimum 1."""
        lot_notional = mark_price * contract_value
        if lot_notional <= 0:
            return 0
        return max(1, int(round(self.notional_usd / lot_notional)))

    def _enter(self, symbol, coin, side, change_24h=None):
        px = get_futures_price(symbol)
        if not px or not px.get('mark_price'):
            logger.warning("[Dual Momentum] Skip %s — no mark price", symbol)
            return
        mark_price = px['mark_price']
        product_id, contract_value = self._product_meta(symbol)
        if contract_value <= 0:
            logger.warning("[Dual Momentum] Skip %s — unknown contract value", symbol)
            return
        lots = self._compute_lots(mark_price, contract_value)
        if lots <= 0:
            return
        est_notional = lots * mark_price * contract_value
        tag = 'LONG' if side == 'buy' else 'SHORT'
        chg = f"{change_24h:+.2f}%" if change_24h is not None else '?'

        if self.dry_run:
            print(f"[Dual Momentum] [DRY] {tag} {side.upper()} {symbol} x{lots} "
                  f"@ ~{mark_price:.6f} (~${est_notional:.2f}) | 24h {chg}")
            filled_price = mark_price
        else:
            result = place_order(product_id, symbol, lots, side)
            if not result:
                logger.warning("[Dual Momentum] %s %s failed — not recording", side.upper(), symbol)
                return
            filled_price = float(result.get('average_fill_price') or mark_price)
            print(f"[Dual Momentum] {tag} {side.upper()} {symbol} x{lots} "
                  f"filled @ {filled_price:.6f} (~${est_notional:.2f})")

        leg = {
            'symbol': symbol, 'product_id': product_id, 'coin': coin,
            'side': side, 'size': lots, 'entry_price': filled_price,
            'contract_value': contract_value,
            'opened_at': datetime.now(IST).isoformat(),
        }
        with self._legs_lock:
            self.legs.append(leg)
        self.total_trades += 1
        try:
            if self._user_id is not None:
                position_tracker.open(
                    self._user_id, product_id, symbol,
                    type='futures', strike='0', side=side,
                    size=lots, entry_price=filled_price, asset=coin or symbol,
                    source='Dual Momentum', strategy_sid=self._sid or '',
                    contract_value=contract_value)
        except Exception as e:
            logger.debug("[Dual Momentum] position_tracker.open failed for %s: %s", symbol, e)
        self.trade_log.append({
            'ts': datetime.now(IST).isoformat(), 'action': side.upper(), 'symbol': symbol,
            'lots': lots, 'price': filled_price, 'notional': round(est_notional, 2),
        })

    def _exit(self, symbol, reason=''):
        with self._legs_lock:
            leg = next((l for l in self.legs if l['symbol'] == symbol), None)
        if not leg:
            return
        px = get_futures_price(symbol)
        mark_price = px['mark_price'] if px and px.get('mark_price') else leg['entry_price']
        lots, cv = leg['size'], leg['contract_value']
        # Closing order is the opposite side of the position.
        close_side = 'sell' if leg['side'] == 'buy' else 'buy'
        sign = 1 if leg['side'] == 'buy' else -1
        realized = sign * (mark_price - leg['entry_price']) * lots * cv

        if self.dry_run:
            print(f"[Dual Momentum] [DRY] CLOSE {close_side.upper()} {symbol} x{lots} "
                  f"@ ~{mark_price:.6f} | {reason} | PnL ${realized:+.2f}")
        else:
            result = place_order(leg['product_id'], symbol, lots, close_side)
            if not result:
                logger.warning("[Dual Momentum] CLOSE %s failed — keeping for retry", symbol)
                return
            fill = float(result.get('average_fill_price') or mark_price)
            realized = sign * (fill - leg['entry_price']) * lots * cv
            print(f"[Dual Momentum] CLOSE {close_side.upper()} {symbol} x{lots} "
                  f"filled @ {fill:.6f} | {reason} | PnL ${realized:+.2f}")

        with self._legs_lock:
            self.cumulative_pnl += realized
            if leg in self.legs:
                self.legs.remove(leg)
        try:
            if self._user_id is not None:
                position_tracker.close(self._user_id, leg['product_id'])
        except Exception as e:
            logger.debug("[Dual Momentum] position_tracker.close failed for %s: %s", symbol, e)
        self.trade_log.append({
            'ts': datetime.now(IST).isoformat(), 'action': f'CLOSE_{leg["side"].upper()}',
            'symbol': symbol, 'lots': lots, 'price': mark_price,
            'pnl': round(realized, 4), 'reason': reason,
        })

    # ---- product/contract metadata ----------------------------------------
    def _product_meta(self, symbol):
        """Return (product_id, contract_value) for a perpetual symbol, cached.

        contract_value rarely changes, so fetch once per symbol and reuse.
        """
        with self._meta_lock:
            cached = self._meta_cache.get(symbol)
        if cached is not None:
            return cached
        import requests
        from auth import get_headers
        path = f'/v2/tickers/{symbol}'
        try:
            headers = get_headers('GET', path, '')
            resp = requests.get(f'{config.BASE_URL}{path}', headers=headers, timeout=10)
            resp.raise_for_status()
            r = resp.json().get('result') or {}
            meta = (r.get('product_id'), float(r.get('contract_value') or 0))
            if meta[1] > 0:
                with self._meta_lock:
                    self._meta_cache[symbol] = meta
            return meta
        except Exception as e:
            logger.error("[Dual Momentum] product meta fetch failed for %s: %s", symbol, e)
            return None, 0.0

    # ---- reporting ---------------------------------------------------------
    def _print_summary(self):
        with self._legs_lock:
            longs = sorted(l['symbol'] for l in self.legs if l['side'] == 'buy')
            shorts = sorted(l['symbol'] for l in self.legs if l['side'] == 'sell')
        print(f"[Dual Momentum] Open: {len(longs)+len(shorts)} "
              f"(L:{len(longs)} / S:{len(shorts)}) | "
              f"Realized PnL: ${self.cumulative_pnl:+.2f}")
        if longs:
            print(f"[Dual Momentum]   LONG : {', '.join(longs)}")
        if shorts:
            print(f"[Dual Momentum]   SHORT: {', '.join(shorts)}")

    # ---- app/thread integration -------------------------------------------
    def _apply_thread_context(self):
        """Bind thread-local credentials + log routing, mirroring ECS."""
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

    # ---- persistence (mirrors EmaTrendFollower) ---------------------------
    def _persist_state(self):
        """Persist trade_log, cumulative_pnl, positions to the DB row for _sid."""
        try:
            from models import update_strategy_db
        except Exception:
            return
        sid = getattr(self, '_sid', None)
        if not sid:
            try:
                from app import dual_momentum_strategies
                for s_id, entry in dual_momentum_strategies.items():
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
                legs_data = [{
                    'symbol': l.get('symbol', ''),
                    'product_id': l.get('product_id'),
                    'coin': l.get('coin', ''),
                    'side': l.get('side', 'buy'),
                    'size': l.get('size', 0),
                    'entry_price': l.get('entry_price', 0),
                    'contract_value': l.get('contract_value', 0),
                    'opened_at': l.get('opened_at', ''),
                } for l in self.legs]
                cum = self.cumulative_pnl
            details = {**self._base_params,
                       'trade_log': self.trade_log[-500:],
                       'cumulative_pnl': cum,
                       'total_trades': self.total_trades,
                       'last_run_slot': self._last_run_slot}
            update_strategy_db(sid, details=details, legs=legs_data,
                               pnl=round(cum, 4))
            logger.debug("[Dual Momentum] State persisted: %d legs, $%.2f",
                         len(legs_data), cum)
        except Exception as e:
            logger.warning("[Dual Momentum] Failed to persist state: %s", e)

    def restore_state(self, details=None, legs=None):
        """Rehydrate from a persisted DB row (called by app.py on restart)."""
        details = details or {}
        self.cumulative_pnl = float(details.get('cumulative_pnl', 0) or 0)
        self.total_trades = int(details.get('total_trades', 0) or 0)
        self.trade_log = details.get('trade_log', []) or []
        self._last_run_slot = details.get('last_run_slot')
        restored = []
        for l in (legs or []):
            cv = float(l.get('contract_value', 0) or 0)
            sym = l.get('symbol', '')
            if cv <= 0 and sym:
                try:
                    _, fetched_cv = self._product_meta(sym)
                    if fetched_cv and fetched_cv > 0:
                        cv = fetched_cv
                except Exception:
                    pass
            restored.append({
                'symbol': sym,
                'product_id': l.get('product_id'),
                'coin': l.get('coin', ''),
                'side': l.get('side', 'buy'),
                'size': int(l.get('size', 0) or 0),
                'entry_price': float(l.get('entry_price', 0) or 0),
                'contract_value': cv,
                'opened_at': l.get('opened_at', ''),
            })
        with self._legs_lock:
            self.legs = restored
        try:
            if self._user_id is not None:
                for l in restored:
                    position_tracker.open(
                        self._user_id, l['product_id'], l['symbol'],
                        type='futures', strike='0', side=l['side'],
                        size=l['size'], entry_price=l['entry_price'],
                        asset=l.get('coin') or l['symbol'],
                        source='Dual Momentum', strategy_sid=self._sid or '',
                        contract_value=l.get('contract_value'))
        except Exception as e:
            logger.debug("[Dual Momentum] tracker re-register on restore failed: %s", e)
        logger.info("[Dual Momentum] Restored %d position(s) | Cum PnL $%.2f",
                    len(restored), self.cumulative_pnl)
        return self

    # ---- timing ------------------------------------------------------------
    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(5, end - time.time()))
