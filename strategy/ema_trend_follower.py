"""EMA Trend Follower — long-only momentum basket on Delta Exchange perpetuals.

Universe : Top-N coins by 24h USD turnover (perpetual futures), refreshed hourly.
Signal   : Daily 20/50 EMA crossover.
             EMA20 > EMA50  -> BULLISH
             EMA20 < EMA50  -> BEARISH
Entry    : When a top-N coin is BULLISH and not already held, open a LONG (buy)
           position sized to ~$100 notional (integer lots from contract value).
Exit     : When a held coin turns BEARISH, close it (long-only; no shorts).
Holding  : A held coin is kept until it turns bearish, even if it drops out of
           the top-N list.
Refresh  : Universe + signals re-evaluated every refresh_interval seconds (hourly).

Modes    : dry_run=True  -> logs intended orders, no real trades (default).
           dry_run=False -> places live market orders via api.orders.place_order.

Persistence mirrors EMACreditSpread: state is written to the live_strategies DB
row (keyed by self._sid) via models.update_strategy_db, so open positions,
realized PnL, and trade history survive a server restart. app.py reconstructs
the object and calls restore_state() with the saved details+legs.
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone

import config
from api.orders import place_order
from api.pricing import get_futures_price
from api.position_tracker import position_tracker
from api.top_coins import get_top_coins_by_volume, ema_crossover_direction
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

TOP_N = 50
POSITION_NOTIONAL_USD = 100
EMA_FAST = 20
EMA_SLOW = 50
EMA_RESOLUTION = '1d'
REFRESH_INTERVAL = 3600          # hourly
MONITOR_INTERVAL = 3600


class EmaTrendFollower(BaseStrategy):
    """Long-only daily-EMA-crossover trend follower over a top-turnover basket."""

    def __init__(self, top_n=TOP_N, notional_usd=POSITION_NOTIONAL_USD,
                 ema_fast=EMA_FAST, ema_slow=EMA_SLOW, ema_resolution=EMA_RESOLUTION,
                 refresh_interval=REFRESH_INTERVAL, dry_run=True):
        self.top_n = top_n
        self.notional_usd = notional_usd
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.ema_resolution = ema_resolution
        self.refresh_interval = refresh_interval
        self.monitor_interval = refresh_interval
        self.dry_run = dry_run

        # positions kept as a list of "legs" so they serialize like other
        # strategies. Each leg:
        #   {symbol, product_id, coin, side='buy', size(lots), entry_price,
        #    contract_value, opened_at}
        self.legs = []
        self._legs_lock = threading.Lock()

        self.cumulative_pnl = 0.0
        self.total_trades = 0
        self.trade_log = []
        self._running = False

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
            'ema_fast': ema_fast,
            'ema_slow': ema_slow,
            'ema_resolution': ema_resolution,
            'refresh_interval': refresh_interval,
            'dry_run': dry_run,
        }

    # ---- BaseStrategy interface -------------------------------------------
    def initialize(self):
        self._running = True
        mode = 'DRY-RUN' if self.dry_run else 'LIVE'
        print(f"[EMA Trend] Started [{mode}] | Universe: top {self.top_n} by turnover")
        print(f"[EMA Trend] Signal: {self.ema_fast}/{self.ema_slow} EMA on "
              f"{self.ema_resolution} | ${self.notional_usd}/coin | "
              f"Refresh {self.refresh_interval}s | Long-only")
        with self._legs_lock:
            if self.legs:
                print(f"[EMA Trend] Resumed with {len(self.legs)} position(s): "
                      f"{', '.join(l['symbol'] for l in self.legs)}")
        return True

    def monitor(self):
        """Blocking loop: re-evaluate the universe every refresh_interval."""
        self._apply_thread_context()
        while self._running:
            try:
                self.evaluate_once()
            except Exception as e:
                logger.error("[EMA Trend] Evaluation cycle failed: %s", e)
            self._interruptible_sleep(self.refresh_interval)

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
        """Realized + open unrealized PnL across held longs."""
        open_pnl = 0.0
        with self._legs_lock:
            legs_copy = list(self.legs)
        # Price all legs in ONE bulk call — pricing each leg individually means
        # many sequential HTTP calls, and the later ones fail (rate limits /
        # connection resets), leaving those legs valued at entry (PnL 0).
        from api.pricing import get_futures_prices_bulk
        marks = get_futures_prices_bulk([l['symbol'] for l in legs_copy])
        for leg in legs_copy:
            md = marks.get(leg['symbol'])
            if md and md.get('mark_price'):
                open_pnl += (md['mark_price'] - leg['entry_price']) * leg['size'] * leg['contract_value']
        return self.cumulative_pnl + open_pnl

    # ---- Core logic --------------------------------------------------------
    def evaluate_once(self):
        """One full pass: refresh universe, score signals, enter/exit."""
        ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
        top = get_top_coins_by_volume(limit=self.top_n)
        if not top:
            logger.warning("[EMA Trend] Top-coin fetch empty — skipping cycle")
            self._consecutive_failures += 1
            return
        self._consecutive_failures = 0

        universe = {c['symbol']: c for c in top}
        print(f"\n[EMA Trend] [{ts}] Universe: {len(universe)} coins")

        # 1) EXIT pass — held coins that turned bearish.
        with self._legs_lock:
            held = [l['symbol'] for l in self.legs]
        for sym in held:
            sig = ema_crossover_direction(sym, resolution=self.ema_resolution,
                                          fast=self.ema_fast, slow=self.ema_slow)
            if sig['direction'] == 'BEARISH':
                self._exit(sym, reason='turned bearish')

        # 2) ENTRY pass — bullish coins in universe not already held.
        with self._legs_lock:
            held_set = {l['symbol'] for l in self.legs}
        for sym, meta in universe.items():
            if sym in held_set:
                continue
            sig = ema_crossover_direction(sym, resolution=self.ema_resolution,
                                          fast=self.ema_fast, slow=self.ema_slow)
            if sig['direction'] == 'BULLISH':
                self._enter(sym, meta.get('coin', ''), sig)

        self._persist_state()
        self._print_summary()

    def _compute_lots(self, mark_price, contract_value):
        """Integer contracts to reach ~notional_usd, minimum 1."""
        lot_notional = mark_price * contract_value
        if lot_notional <= 0:
            return 0
        return max(1, int(round(self.notional_usd / lot_notional)))

    def _enter(self, symbol, coin, sig):
        px = get_futures_price(symbol)
        if not px or not px.get('mark_price'):
            logger.warning("[EMA Trend] Skip %s — no mark price", symbol)
            return
        mark_price = px['mark_price']
        product_id, contract_value = self._product_meta(symbol)
        if contract_value <= 0:
            logger.warning("[EMA Trend] Skip %s — unknown contract value", symbol)
            return
        lots = self._compute_lots(mark_price, contract_value)
        if lots <= 0:
            return
        est_notional = lots * mark_price * contract_value

        if self.dry_run:
            print(f"[EMA Trend] [DRY] BUY {symbol} x{lots} @ ~{mark_price:.6f} "
                  f"(~${est_notional:.2f}) | EMA{self.ema_fast}>{self.ema_slow} "
                  f"spread {sig.get('spread_pct', 0):+.2f}%")
            filled_price = mark_price
        else:
            result = place_order(product_id, symbol, lots, 'buy')
            if not result:
                logger.warning("[EMA Trend] BUY %s failed — not recording", symbol)
                return
            filled_price = float(result.get('average_fill_price') or mark_price)
            print(f"[EMA Trend] BUY {symbol} x{lots} filled @ {filled_price:.6f} "
                  f"(~${est_notional:.2f})")

        leg = {
            'symbol': symbol, 'product_id': product_id, 'coin': coin,
            'side': 'buy', 'size': lots, 'entry_price': filled_price,
            'contract_value': contract_value,
            'opened_at': datetime.now(IST).isoformat(),
        }
        with self._legs_lock:
            self.legs.append(leg)
        self.total_trades += 1
        # Register with the shared position tracker so it appears in the
        # dashboard "open positions" panel (like other strategies).
        try:
            if self._user_id is not None:
                position_tracker.open(
                    self._user_id, product_id, symbol,
                    type='futures', strike='0', side='buy',
                    size=lots, entry_price=filled_price, asset=coin or symbol,
                    source='EMA Trend', strategy_sid=self._sid or '',
                    contract_value=contract_value)
        except Exception as e:
            logger.debug("[EMA Trend] position_tracker.open failed for %s: %s", symbol, e)
        self.trade_log.append({
            'ts': datetime.now(IST).isoformat(), 'action': 'BUY', 'symbol': symbol,
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
        realized = (mark_price - leg['entry_price']) * lots * cv

        if self.dry_run:
            print(f"[EMA Trend] [DRY] SELL {symbol} x{lots} @ ~{mark_price:.6f} "
                  f"| {reason} | PnL ${realized:+.2f}")
        else:
            result = place_order(leg['product_id'], symbol, lots, 'sell')
            if not result:
                logger.warning("[EMA Trend] SELL %s failed — keeping for retry", symbol)
                return
            fill = float(result.get('average_fill_price') or mark_price)
            realized = (fill - leg['entry_price']) * lots * cv
            print(f"[EMA Trend] SELL {symbol} x{lots} filled @ {fill:.6f} "
                  f"| {reason} | PnL ${realized:+.2f}")

        with self._legs_lock:
            self.cumulative_pnl += realized
            if leg in self.legs:
                self.legs.remove(leg)
        # Deregister from the shared position tracker.
        try:
            if self._user_id is not None:
                position_tracker.close(self._user_id, leg['product_id'])
        except Exception as e:
            logger.debug("[EMA Trend] position_tracker.close failed for %s: %s", symbol, e)
        self.trade_log.append({
            'ts': datetime.now(IST).isoformat(), 'action': 'SELL', 'symbol': symbol,
            'lots': lots, 'price': mark_price, 'pnl': round(realized, 4), 'reason': reason,
        })

    # ---- product/contract metadata ----------------------------------------
    def _product_meta(self, symbol):
        """Return (product_id, contract_value) for a perpetual symbol."""
        import requests
        from auth import get_headers
        path = f'/v2/tickers/{symbol}'
        try:
            headers = get_headers('GET', path, '')
            resp = requests.get(f'{config.BASE_URL}{path}', headers=headers, timeout=10)
            resp.raise_for_status()
            r = resp.json().get('result') or {}
            return r.get('product_id'), float(r.get('contract_value') or 0)
        except Exception as e:
            logger.error("[EMA Trend] product meta fetch failed for %s: %s", symbol, e)
            return None, 0.0

    # ---- reporting ---------------------------------------------------------
    def _print_summary(self):
        with self._legs_lock:
            n = len(self.legs)
            syms = ', '.join(sorted(l['symbol'] for l in self.legs)) or '(none)'
        print(f"[EMA Trend] Open: {n} | Holding: {syms} | "
              f"Realized PnL: ${self.cumulative_pnl:+.2f}")

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

    # ---- persistence (mirrors EMACreditSpread) ----------------------------
    def _persist_state(self):
        """Persist trade_log, cumulative_pnl, positions to the DB row for _sid."""
        try:
            from models import update_strategy_db
        except Exception:
            return
        sid = getattr(self, '_sid', None)
        if not sid:
            # Try to discover our sid from the app registry (like ECS does).
            try:
                from app import ema_trend_strategies
                for s_id, entry in ema_trend_strategies.items():
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
                       'total_trades': self.total_trades}
            update_strategy_db(sid, details=details, legs=legs_data,
                               pnl=round(cum, 4))
            logger.debug("[EMA Trend] State persisted: %d legs, $%.2f",
                         len(legs_data), cum)
        except Exception as e:
            logger.warning("[EMA Trend] Failed to persist state: %s", e)

    def restore_state(self, details=None, legs=None):
        """Rehydrate from a persisted DB row (called by app.py on restart)."""
        details = details or {}
        self.cumulative_pnl = float(details.get('cumulative_pnl', 0) or 0)
        self.total_trades = int(details.get('total_trades', 0) or 0)
        self.trade_log = details.get('trade_log', []) or []
        restored = []
        for l in (legs or []):
            cv = float(l.get('contract_value', 0) or 0)
            sym = l.get('symbol', '')
            # Self-heal legs persisted before contract_value was tracked (cv=0):
            # re-fetch it from the exchange so P&L/lot math stays correct.
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
        # Re-register restored positions with the shared tracker so they show
        # on the dashboard after a restart.
        try:
            if self._user_id is not None:
                for l in restored:
                    position_tracker.open(
                        self._user_id, l['product_id'], l['symbol'],
                        type='futures', strike='0', side='buy',
                        size=l['size'], entry_price=l['entry_price'],
                        asset=l.get('coin') or l['symbol'],
                        source='EMA Trend', strategy_sid=self._sid or '',
                        contract_value=l.get('contract_value'))
        except Exception as e:
            logger.debug("[EMA Trend] tracker re-register on restore failed: %s", e)
        logger.info("[EMA Trend] Restored %d position(s) | Cum PnL $%.2f",
                    len(restored), self.cumulative_pnl)
        return self

    # ---- timing ------------------------------------------------------------
    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(15, end - time.time()))
