"""EMA Trend Follower — daily EMA time-series momentum across the most liquid Delta perpetuals.

Validated design (backtest/trend_follower_v2.py, config #1 "EMA10/30 long+short BTC-regime flip K=10";
design on Binance 2018-21, checks on Binance 2022-24 and Delta 2024-26):

Once per day, just after the 00:00 UTC daily candle closes:
  Universe : the `top_n` (10) perps with the highest 30-day average open interest (USD) that have
             more than ema_slow + 20 daily candles of history.
  Signal   : per coin, LONG while EMA(ema_fast=10) > EMA(ema_slow=30) of daily closes, SHORT while below.
  Regime   : (btc_regime) longs only when BTC's daily close > its EMA(ema_slow); shorts only when below.
  Exit     : a held position is closed when its own EMA state flips (no TP/SL, no time exit). After a
             flip the coin can be re-entered on the other side in the same pass (if it qualifies).
  Sizing   : notional = risk_per_atr_usd / ATR20% (so one average daily move ~ risk_per_atr_usd),
             capped at notional_usd per coin; new entries are skipped once total gross notional would
             exceed max_gross_usd. Integer lots (min 1; skipped if one lot exceeds the per-coin cap).
  A coin that drops out of the universe is held until its own flip.

Signals use Delta's public production market data (MARK and OI daily candles); orders go to the
strategy's broker. Fees and funding are paid by the exchange and are NOT included in the displayed
P&L (backtest: funding ~ -$420 per 2.5 years at the default size). The universe can include Delta's
tokenized gold / silver / stock-index perps (XAUT, PAXG, SLVON, QQQX, SPYX) — the Delta backtest did too.

Modes    : dry_run=True  -> logs intended orders, no real trades (default).
           dry_run=False -> places live market orders via api.orders.place_order.

Persistence: state is written to the live_strategies DB row (keyed by self._sid) via
models.update_strategy_db; app.py reconstructs the object and calls restore_state(details, legs).
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone

import requests

import config
from api.orders import place_order
from api.pricing import get_futures_price
from api.position_tracker import position_tracker
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
DAY = 86400
PUBLIC_URL = 'https://api.india.delta.exchange'   # production market data for signals

TOP_N = 10
EMA_FAST = 10
EMA_SLOW = 30
EMA_RESOLUTION = '1d'
RISK_PER_ATR_USD = 25.0          # one ATR20 move ~ $25 per position
MAX_NOTIONAL_PER_COIN = 500.0    # per-coin notional cap ($)
MAX_GROSS_USD = 7500.0           # total gross notional cap ($)
REFRESH_INTERVAL = 300           # seconds between "is a new daily candle closed?" checks
EVAL_DELAY_MIN = 5               # evaluate this many minutes after 00:00 UTC
HISTORY_DAYS = 200               # daily candles fetched per coin
POOL_MULT = 3                    # pre-filter: top_n * POOL_MULT coins by current OI, then 30d average


# ---- pure signal helpers (identical math to backtest/trend_follower_v2.py) --------------------
def ema_series(closes, period):
    """EMA seeded with the first close (k = 2/(p+1))."""
    k = 2 / (period + 1)
    out, prev = [], None
    for x in closes:
        prev = x if prev is None else x * k + prev * (1 - k)
        out.append(prev)
    return out


def atr_pct_series(highs, lows, closes, period=20):
    """ATR as a fraction of close: a = (a*(p-1) + TR)/p, seeded with the first true range."""
    out, a, pc = [], None, None
    for h, l, c in zip(highs, lows, closes):
        tr = (h - l) if pc is None else max(h - l, abs(h - pc), abs(l - pc))
        a = tr if a is None else (a * (period - 1) + tr) / period
        out.append(a / c if c else 0.0)
        pc = c
    return out


def coin_signal(candles, oi_usd, ema_fast, ema_slow):
    """Signal for the last CLOSED daily candle.

    candles: oldest-first [(t, o, h, l, c)], forming candle already removed.
    oi_usd : {day_ts: open interest in USD at that day's close}.
    Returns dict(state=+1/-1, atr_pct, liq30, age, close, day) or None if no data.
    """
    if not candles:
        return None
    closes = [c[4] for c in candles]
    ef = ema_series(closes, ema_fast)[-1]
    es = ema_series(closes, ema_slow)[-1]
    atrp = atr_pct_series([c[2] for c in candles], [c[3] for c in candles], closes)[-1]
    last_day = candles[-1][0]
    liq30 = sum(oi_usd.get(last_day - k * DAY, 0.0) for k in range(30)) / 30
    return {'state': 1 if ef > es else -1, 'atr_pct': atrp, 'liq30': liq30,
            'age': len(candles), 'close': closes[-1], 'day': last_day}


def plan_day(signals, held, btc_sig, cfg):
    """Decide exits and entries for one daily close. Pure function (used live and in parity tests).

    signals: {symbol: coin_signal(...)}; held: {symbol: {'side': +1/-1, 'notional': $}}
    btc_sig: coin_signal for BTC (regime uses BTC close vs its EMA(ema_slow)).
    cfg    : dict(top_n, ema_slow, allow_shorts, btc_regime, risk_per_atr_usd, max_notional, max_gross)
    Returns (exits [symbol], entries [(symbol, side, notional)]) in execution order.
    """
    exits = [s for s, h in held.items() if s in signals and signals[s]['state'] != h['side']]
    remaining = {s: h for s, h in held.items() if s not in exits}
    eligible = [s for s, g in signals.items() if g['age'] > cfg['ema_slow'] + 20]
    universe = sorted(eligible, key=lambda s: -signals[s]['liq30'])[:cfg['top_n']]
    btc_up = btc_sig is not None and btc_sig['_above_slow']
    gross = sum(h['notional'] for h in remaining.values())
    entries = []
    for s in universe:                                   # most liquid first
        g = signals[s]
        if s in remaining or not g['atr_pct'] or g['atr_pct'] <= 0:
            continue
        side = g['state']
        if side < 0 and not cfg['allow_shorts']:
            continue
        if cfg['btc_regime'] and ((side > 0 and not btc_up) or (side < 0 and btc_up)):
            continue
        notional = min(cfg['max_notional'], cfg['risk_per_atr_usd'] / g['atr_pct'])
        if gross + notional > cfg['max_gross']:
            continue
        entries.append((s, side, notional))
        gross += notional
    return exits, entries


class EmaTrendFollower(BaseStrategy):
    """Daily EMA10/30 long/short trend follower over the 10 most liquid Delta perps."""

    def __init__(self, top_n=TOP_N, notional_usd=MAX_NOTIONAL_PER_COIN,
                 ema_fast=EMA_FAST, ema_slow=EMA_SLOW, ema_resolution=EMA_RESOLUTION,
                 refresh_interval=REFRESH_INTERVAL, dry_run=True,
                 risk_per_atr_usd=RISK_PER_ATR_USD, max_gross_usd=MAX_GROSS_USD,
                 allow_shorts=True, btc_regime=True, **legacy):
        # Legacy kwargs from the old gainers/losers version (min_oi_change_pct, tp_pct, sl_pct, ...)
        # are accepted and ignored so old saved strategies still restart.
        self.top_n = int(top_n)
        self.notional_usd = float(notional_usd)          # max notional per coin
        self.ema_fast = int(ema_fast)
        self.ema_slow = int(ema_slow)
        self.ema_resolution = '1d'                        # validated on daily candles only
        self.refresh_interval = max(30, int(refresh_interval))
        self.monitor_interval = self.refresh_interval
        self.dry_run = dry_run
        self.risk_per_atr_usd = float(risk_per_atr_usd)
        self.max_gross_usd = float(max_gross_usd)
        self.allow_shorts = bool(allow_shorts)
        self.btc_regime = bool(btc_regime)

        # legs: {symbol, product_id, coin, side, size(lots), entry_price, contract_value,
        #        notional, opened_at}
        self.legs = []
        self._legs_lock = threading.Lock()
        self.cumulative_pnl = 0.0
        self.total_trades = 0
        self.trade_log = []
        self._running = False
        self.last_eval_day = 0           # UTC day timestamp of the last evaluated daily close
        self.last_universe = []

        # App-integration hooks (set externally after creation)
        self._sid = None
        self._api_key = None
        self._api_secret = None
        self._broker = None
        self._log_queue = None
        self._log_history = None
        self._user_id = None
        self._pnl_history = []

        self._base_params = {
            'top_n': self.top_n, 'notional_usd': self.notional_usd,
            'ema_fast': self.ema_fast, 'ema_slow': self.ema_slow, 'ema_resolution': '1d',
            'refresh_interval': self.refresh_interval, 'dry_run': dry_run,
            'risk_per_atr_usd': self.risk_per_atr_usd, 'max_gross_usd': self.max_gross_usd,
            'allow_shorts': self.allow_shorts, 'btc_regime': self.btc_regime,
        }

    def _cfg(self):
        return {'top_n': self.top_n, 'ema_slow': self.ema_slow, 'allow_shorts': self.allow_shorts,
                'btc_regime': self.btc_regime, 'risk_per_atr_usd': self.risk_per_atr_usd,
                'max_notional': self.notional_usd, 'max_gross': self.max_gross_usd}

    # ---- BaseStrategy interface -------------------------------------------
    def initialize(self):
        self._running = True
        mode = 'DRY-RUN' if self.dry_run else 'LIVE'
        print(f"[EMA Trend] Started [{mode}] | Daily EMA{self.ema_fast}/{self.ema_slow} "
              f"{'long+short' if self.allow_shorts else 'long-only'} | top {self.top_n} perps by 30d OI"
              f"{' | BTC regime' if self.btc_regime else ''}")
        print(f"[EMA Trend] Sizing: ${self.risk_per_atr_usd:g} per ATR20, max ${self.notional_usd:g}/coin, "
              f"max ${self.max_gross_usd:g} gross | Exit on EMA flip | Evaluates daily at "
              f"00:{EVAL_DELAY_MIN:02d} UTC")
        with self._legs_lock:
            if self.legs:
                print(f"[EMA Trend] Resumed with {len(self.legs)} position(s): "
                      f"{', '.join(l['symbol'] for l in self.legs)}")
        return True

    def monitor(self):
        """Blocking loop: evaluate once per closed daily candle; poll every refresh_interval."""
        self._apply_thread_context()
        while self._running:
            try:
                if self._due():
                    self.evaluate_once()
            except Exception as e:
                logger.error("[EMA Trend] Evaluation cycle failed: %s", e)
            self._interruptible_sleep(self.refresh_interval)

    def _due(self, now=None):
        now = now or time.time()
        day = int(now) // DAY * DAY                       # start of the current UTC day
        return day > self.last_eval_day and now - day >= EVAL_DELAY_MIN * 60

    def close_all(self):
        """Close every open position (used on stop/shutdown)."""
        self._running = False
        with self._legs_lock:
            legs_copy = list(self.legs)
        for leg in legs_copy:
            self._exit(leg['symbol'], reason='shutdown')
        self._persist_state()

    @property
    def pnl(self):
        """Realized + open unrealized P&L (fees/funding excluded)."""
        open_pnl = 0.0
        with self._legs_lock:
            legs_copy = list(self.legs)
        if legs_copy:
            from api.pricing import get_futures_prices_bulk
            marks = get_futures_prices_bulk([l['symbol'] for l in legs_copy])
            for leg in legs_copy:
                md = marks.get(leg['symbol'])
                if md and md.get('mark_price'):
                    sign = 1 if leg.get('side', 'buy') == 'buy' else -1
                    open_pnl += sign * (md['mark_price'] - leg['entry_price']) * leg['size'] * leg['contract_value']
        return self.cumulative_pnl + open_pnl

    # ---- market data (public production endpoint) ---------------------------
    def _get(self, path, params):
        try:
            r = requests.get(f'{PUBLIC_URL}{path}', params=params, timeout=20)
            r.raise_for_status()
            return r.json().get('result')
        except Exception as e:
            logger.warning("[EMA Trend] GET %s %s failed: %s", path, params.get('symbol', ''), e)
            return None

    def _fetch_universe(self):
        """All perpetual tickers: [{symbol, coin, oi_value_usd, contract_value}]."""
        rows = self._get('/v2/tickers', {'contract_types': 'perpetual_futures'}) or []
        out = []
        for t in rows:
            try:
                out.append({'symbol': t['symbol'], 'coin': t.get('underlying_asset_symbol', ''),
                            'oi_value_usd': float(t.get('oi_value_usd') or 0),
                            'contract_value': float(t.get('contract_value') or 0)})
            except (KeyError, ValueError, TypeError):
                continue
        return out

    def _fetch_daily(self, symbol, now=None):
        """Closed daily MARK candles [(t, o, h, l, c)] and OI closes {t: open interest in coins}."""
        now = int(now or time.time())
        start = now - HISTORY_DAYS * DAY
        mk = self._get('/v2/history/candles', {'resolution': '1d', 'symbol': f'MARK:{symbol}',
                                               'start': start, 'end': now}) or []
        oi = self._get('/v2/history/candles', {'resolution': '1d', 'symbol': f'OI:{symbol}',
                                               'start': start, 'end': now}) or []
        candles = sorted((int(c['time']), float(c['open']), float(c['high']), float(c['low']),
                          float(c['close'])) for c in mk if c.get('close') is not None)
        candles = [c for c in candles if c[0] + DAY <= now]            # drop the forming candle
        oi_map = {int(c['time']): float(c['close']) for c in oi if c.get('close') is not None}
        return candles, oi_map

    # ---- core logic --------------------------------------------------------
    def evaluate_once(self, now=None):
        """One daily pass: compute signals on the last closed daily candle, exit flips, enter."""
        now = int(now or time.time())
        ts = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
        tickers = self._fetch_universe()
        if not tickers:
            logger.warning("[EMA Trend] Ticker fetch empty — will retry")
            return
        cv = {t['symbol']: t['contract_value'] for t in tickers if t['contract_value'] > 0}
        coin_of = {t['symbol']: t['coin'] for t in tickers}
        pool = [t['symbol'] for t in sorted(tickers, key=lambda t: -t['oi_value_usd'])
                if t['symbol'] in cv][:max(self.top_n * POOL_MULT, 30)]
        with self._legs_lock:
            held_syms = [l['symbol'] for l in self.legs]
        wanted = list(dict.fromkeys(pool + held_syms + ['BTCUSD']))

        signals = {}
        for sym in wanted:
            candles, oi_map = self._fetch_daily(sym, now)
            if not candles:
                continue
            closes = {c[0]: c[4] for c in candles}
            # Delta OI candles are in underlying coins (contracts x contract_value): USD = coins x price.
            oi_usd = {t: oi_map[t] * closes[t] for t in oi_map if t in closes}
            g = coin_signal(candles, oi_usd, self.ema_fast, self.ema_slow)
            if g:
                if sym == 'BTCUSD':
                    es = ema_series([c[4] for c in candles], self.ema_slow)[-1]
                    g['_above_slow'] = candles[-1][4] > es
                signals[sym] = g
        btc = signals.get('BTCUSD')
        if self.btc_regime and (not btc or '_above_slow' not in btc):
            logger.warning("[EMA Trend] BTC daily data unavailable — skipping this pass")
            return
        day = max((g['day'] for g in signals.values()), default=0)
        pool_signals = {s: g for s, g in signals.items() if s in pool or s in held_syms}

        with self._legs_lock:
            held = {l['symbol']: {'side': 1 if l['side'] == 'buy' else -1,
                                  'notional': l.get('notional', l['entry_price'] * l['size'] * l['contract_value'])}
                    for l in self.legs}
        exits, entries = plan_day(pool_signals, held, btc, self._cfg())
        universe = sorted([s for s, g in pool_signals.items() if g['age'] > self.ema_slow + 20],
                          key=lambda s: -pool_signals[s]['liq30'])[:self.top_n]
        self.last_universe = universe
        regime = ('BTC ABOVE' if btc and btc.get('_above_slow') else 'BTC BELOW') + f' EMA{self.ema_slow}'
        print(f"\n[EMA Trend] [{ts}] Daily close {datetime.fromtimestamp(day, tz=timezone.utc):%Y-%m-%d} | "
              f"{regime} | Universe: {', '.join(universe)}")

        for sym in exits:
            self._exit(sym, reason='ema_flip')
        for sym, side, notional in entries:
            self._enter(sym, coin_of.get(sym, ''), 'buy' if side > 0 else 'sell', notional,
                        signals[sym]['atr_pct'], cv_hint=cv.get(sym, 0.0))
        self.last_eval_day = int(now) // DAY * DAY
        self._persist_state()
        self._print_summary()

    def _enter(self, symbol, coin, side, notional, atr_pct=0.0, cv_hint=0.0):
        px = get_futures_price(symbol)
        if not px or not px.get('mark_price'):
            logger.warning("[EMA Trend] Skip %s — no mark price", symbol)
            return
        mark_price = px['mark_price']
        product_id, contract_value = self._product_meta(symbol)
        if contract_value <= 0 and self.dry_run and cv_hint > 0:
            contract_value = cv_hint                     # paper mode without broker access
        if contract_value <= 0:
            logger.warning("[EMA Trend] Skip %s — unknown contract value", symbol)
            return
        lot_notional = mark_price * contract_value
        if lot_notional > self.notional_usd:
            print(f"[EMA Trend] Skip {symbol} — 1 lot (${lot_notional:.0f}) exceeds the ${self.notional_usd:g} per-coin cap")
            return
        lots = max(1, int(round(notional / lot_notional)))
        est_notional = lots * lot_notional
        tag = 'LONG' if side == 'buy' else 'SHORT'
        if self.dry_run:
            print(f"[EMA Trend] [DRY] {tag} {symbol} x{lots} @ ~{mark_price:.6f} (~${est_notional:.2f}, "
                  f"ATR {atr_pct * 100:.1f}%)")
            filled_price = mark_price
        else:
            result = place_order(product_id, symbol, lots, side)
            if not result:
                logger.warning("[EMA Trend] %s %s failed — not recording", side.upper(), symbol)
                return
            filled_price = float(result.get('average_fill_price') or mark_price)
            print(f"[EMA Trend] {tag} {symbol} x{lots} filled @ {filled_price:.6f} (~${est_notional:.2f})")
        leg = {'symbol': symbol, 'product_id': product_id, 'coin': coin, 'side': side, 'size': lots,
               'entry_price': filled_price, 'contract_value': contract_value,
               'notional': round(est_notional, 2), 'opened_at': datetime.now(IST).isoformat()}
        with self._legs_lock:
            self.legs.append(leg)
        self.total_trades += 1
        try:
            if self._user_id is not None:
                position_tracker.open(self._user_id, product_id, symbol, type='futures', strike='0',
                                      side=side, size=lots, entry_price=filled_price, asset=coin or symbol,
                                      source='EMA Trend', strategy_sid=self._sid or '',
                                      contract_value=contract_value)
        except Exception as e:
            logger.debug("[EMA Trend] position_tracker.open failed for %s: %s", symbol, e)
        self.trade_log.append({'ts': datetime.now(IST).isoformat(), 'action': side.upper(), 'symbol': symbol,
                               'lots': lots, 'price': filled_price, 'notional': round(est_notional, 2)})

    def _exit(self, symbol, reason=''):
        with self._legs_lock:
            leg = next((l for l in self.legs if l['symbol'] == symbol), None)
        if not leg:
            return
        px = get_futures_price(symbol)
        mark_price = px['mark_price'] if px and px.get('mark_price') else leg['entry_price']
        lots, cv = leg['size'], leg['contract_value']
        side = leg.get('side', 'buy')
        sign = 1 if side == 'buy' else -1
        close_side = 'sell' if side == 'buy' else 'buy'
        realized = sign * (mark_price - leg['entry_price']) * lots * cv
        if self.dry_run:
            print(f"[EMA Trend] [DRY] CLOSE {close_side.upper()} {symbol} x{lots} @ ~{mark_price:.6f} "
                  f"| {reason} | PnL ${realized:+.2f}")
        else:
            result = place_order(leg['product_id'], symbol, lots, close_side)
            if not result:
                logger.warning("[EMA Trend] CLOSE %s failed — keeping for retry", symbol)
                return
            fill = float(result.get('average_fill_price') or mark_price)
            realized = sign * (fill - leg['entry_price']) * lots * cv
            mark_price = fill
            print(f"[EMA Trend] CLOSE {close_side.upper()} {symbol} x{lots} filled @ {fill:.6f} "
                  f"| {reason} | PnL ${realized:+.2f}")
        with self._legs_lock:
            self.cumulative_pnl += realized
            if leg in self.legs:
                self.legs.remove(leg)
        try:
            if self._user_id is not None:
                position_tracker.close(self._user_id, leg['product_id'])
        except Exception as e:
            logger.debug("[EMA Trend] position_tracker.close failed for %s: %s", symbol, e)
        self.trade_log.append({'ts': datetime.now(IST).isoformat(), 'action': f'CLOSE_{side.upper()}',
                               'symbol': symbol, 'lots': lots, 'price': mark_price,
                               'pnl': round(realized, 4), 'reason': reason})

    # ---- product/contract metadata ----------------------------------------
    def _product_meta(self, symbol):
        """Return (product_id, contract_value) for a perpetual symbol on the strategy's broker."""
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
            longs = sorted(l['symbol'] for l in self.legs if l.get('side', 'buy') == 'buy')
            shorts = sorted(l['symbol'] for l in self.legs if l.get('side') == 'sell')
            gross = sum(l.get('notional', 0) for l in self.legs)
        print(f"[EMA Trend] Open: {len(longs) + len(shorts)} (L:{len(longs)} / S:{len(shorts)}) | "
              f"Gross ${gross:,.0f} | Realized PnL: ${self.cumulative_pnl:+.2f}")
        if longs:
            print(f"[EMA Trend]   LONG : {', '.join(longs)}")
        if shorts:
            print(f"[EMA Trend]   SHORT: {', '.join(shorts)}")

    # ---- app/thread integration -------------------------------------------
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

    # ---- persistence -------------------------------------------------------
    def _persist_state(self):
        try:
            from models import update_strategy_db
        except Exception:
            return
        sid = getattr(self, '_sid', None)
        if not sid:
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
                legs_data = [{k: l.get(k) for k in ('symbol', 'product_id', 'coin', 'side', 'size',
                                                     'entry_price', 'contract_value', 'notional', 'opened_at')}
                             for l in self.legs]
                cum = self.cumulative_pnl
            details = {**self._base_params, 'trade_log': self.trade_log[-500:], 'cumulative_pnl': cum,
                       'total_trades': self.total_trades, 'last_eval_day': self.last_eval_day,
                       'strategy_version': 'daily_ema_v2'}
            update_strategy_db(sid, details=details, legs=legs_data, pnl=round(cum, 4))
        except Exception as e:
            logger.warning("[EMA Trend] Failed to persist state: %s", e)

    def restore_state(self, details=None, legs=None):
        """Rehydrate from a persisted DB row (called by app.py on restart)."""
        details = details or {}
        self.cumulative_pnl = float(details.get('cumulative_pnl', 0) or 0)
        self.total_trades = int(details.get('total_trades', 0) or 0)
        self.trade_log = details.get('trade_log', []) or []
        # Only trust last_eval_day from this version (old rows evaluate on the next pass).
        if details.get('strategy_version') == 'daily_ema_v2':
            self.last_eval_day = int(details.get('last_eval_day', 0) or 0)
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
            entry = float(l.get('entry_price', 0) or 0)
            size = int(l.get('size', 0) or 0)
            restored.append({'symbol': sym, 'product_id': l.get('product_id'), 'coin': l.get('coin', ''),
                             'side': l.get('side', 'buy'), 'size': size, 'entry_price': entry,
                             'contract_value': cv,
                             'notional': float(l.get('notional') or entry * size * cv),
                             'opened_at': l.get('opened_at', '')})
        with self._legs_lock:
            self.legs = restored
        try:
            if self._user_id is not None:
                for l in restored:
                    position_tracker.open(self._user_id, l['product_id'], l['symbol'], type='futures',
                                          strike='0', side=l.get('side', 'buy'), size=l['size'],
                                          entry_price=l['entry_price'], asset=l.get('coin') or l['symbol'],
                                          source='EMA Trend', strategy_sid=self._sid or '',
                                          contract_value=l.get('contract_value'))
        except Exception as e:
            logger.debug("[EMA Trend] tracker re-register on restore failed: %s", e)
        logger.info("[EMA Trend] Restored %d position(s) | Cum PnL $%.2f", len(restored), self.cumulative_pnl)
        return self

    # ---- timing ------------------------------------------------------------
    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(15, end - time.time()))
