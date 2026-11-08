"""Crypto Scanner Futures — trades Delta Exchange perpetual futures picked by the Crypto Scanner.

Paper mode (default) records simulated fills at the live mark price. Live mode places real
market orders on Delta Exchange through api.orders.place_order (needs a 'delta_exchange' profile).

Loop (24/7):
  every scan_interval minutes, while fewer than max_positions are open and today's trade count
  is below max_trades_per_day:
    1. api.crypto_scanner.get_overview(): sectors from Delta tags.  If require_sector_bias, only
       BULLISH / BEARISH sectors are used, biggest |median 24h move| first.
    2. get_sector_coins(sector): take LONG / SHORT setups (RS + 24h VWAP + RVOL, not THIN) whose
       24h turnover >= min_turnover_usd, not already held and not traded in the last cooldown_hours.
    3. Size: lots = floor(amount_usd / (price x contract_value))  -> the dollars you set per trade
       buy that many lots.  Skipped when one lot already costs more than amount_usd.
    4. Stop: the scanner plan (24h VWAP, clamped 0.75-3%) or a fixed stop_pct.  Target = rr x stop.
  every price_check_sec: mark price of each open position -> exit on stop / target / max_hold_hours.

P&L (both modes): side-aware (exit - entry) x lots x contract_value, minus estimated taker fees
(0.05% + 18% GST per side).  Funding payments are NOT included.  Leverage / margin come from your
Delta account settings for the product (this strategy does not change leverage).
"""

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
DELTA_PROD = 'https://api.india.delta.exchange'
TAG = '[Crypto Futures]'
TAKER_FEE = 0.0005 * 1.18
MAX_PRICE_FAILURES = 20


def est_fee(notional):
    return abs(notional) * TAKER_FEE


class CryptoScannerFutures:

    def __init__(self, amount_usd=100.0, max_positions=3, max_trades_per_day=6, stop_mode='vwap',
                 stop_pct=1.5, rr=2.0, max_hold_hours=24, min_turnover_usd=1_000_000,
                 require_sector_bias=True, sectors='', allow_long=True, allow_short=True,
                 cooldown_hours=24, scan_interval_min=15, price_check_sec=15, dry_run=True,
                 rating_mode='off', min_rating=0.0, rating_weights=None):
        self.amount_usd = float(amount_usd)
        self.max_positions = max(1, int(max_positions))
        self.max_trades_per_day = max(1, int(max_trades_per_day))
        self.stop_mode = stop_mode if stop_mode in ('vwap', 'fixed') else 'vwap'
        self.stop_pct = float(stop_pct)
        self.rr = float(rr)
        self.max_hold_hours = float(max_hold_hours)
        self.min_turnover_usd = float(min_turnover_usd)
        self.require_sector_bias = bool(require_sector_bias)
        self.sectors = [s.strip() for s in (sectors or '').split(',') if s.strip()]
        self.allow_long, self.allow_short = bool(allow_long), bool(allow_short)
        self.cooldown_hours = float(cooldown_hours)
        self.scan_interval = max(1, int(scan_interval_min)) * 60
        self.price_check_sec = max(5, int(price_check_sec))
        self.dry_run = bool(dry_run)
        # Weighted rating (api.crypto_scanner: News / Twitter / RSI / EMA 7-20, 0-100, 50 = neutral):
        #   'off'    -> old behaviour: scanner LONG/SHORT setups only, scanner order
        #   'filter' -> scanner LONG/SHORT setups whose rating >= min_rating, best rating first
        #   'rating' -> ANY coin (setup, WATCH or none) whose rating >= min_rating, in its trade direction,
        #               best rating first (still needs a VWAP stop plan, min turnover, cooldown, sides)
        self.rating_mode = rating_mode if rating_mode in ('off', 'filter', 'rating') else 'off'
        self.min_rating = float(min_rating or 0)
        self.rating_weights = ({k: float(v) for k, v in rating_weights.items()}
                               if isinstance(rating_weights, dict) and rating_weights else None)

        self.legs = []
        self._legs_lock = threading.Lock()
        self.cumulative_pnl = 0.0
        self.total_fees = 0.0
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.trade_log = []
        self._recent = {}               # symbol -> last entry epoch (cooldown)
        self._day_trades = {}           # 'YYYY-MM-DD' (IST) -> entries
        self._last_scan = None
        self._next_scan_ts = 0
        self._price_failures = 0
        self._running = False
        self._pnl_history = []

        self._sid = None
        self._user_id = None
        self._api_key = None
        self._api_secret = None
        self._broker = None
        self._log_queue = None
        self._log_history = None

        self._base_params = {
            'amount_usd': self.amount_usd, 'max_positions': self.max_positions,
            'max_trades_per_day': self.max_trades_per_day, 'stop_mode': self.stop_mode, 'stop_pct': self.stop_pct,
            'rr': self.rr, 'max_hold_hours': self.max_hold_hours, 'min_turnover_usd': self.min_turnover_usd,
            'require_sector_bias': self.require_sector_bias, 'sectors': ','.join(self.sectors),
            'allow_long': self.allow_long, 'allow_short': self.allow_short, 'cooldown_hours': self.cooldown_hours,
            'scan_interval_min': self.scan_interval // 60, 'price_check_sec': self.price_check_sec,
            'dry_run': self.dry_run, 'rating_mode': self.rating_mode, 'min_rating': self.min_rating,
            'rating_weights': self.rating_weights,
        }

    # ------------------------------------------------------------------ lifecycle
    @property
    def mode(self):
        return 'PAPER' if self.dry_run else 'LIVE'

    def initialize(self):
        from config import get_api_key, get_api_secret, _thread_local
        self._api_key, self._api_secret = get_api_key(), get_api_secret()
        self._broker = getattr(_thread_local, 'broker', '')
        if not self.dry_run and self._broker != 'delta_exchange':
            print(f"{TAG} ❌ LIVE mode needs a Delta Exchange (production) profile — selected profile broker is "
                  f"'{self._broker or 'none'}'. Not starting.")
            return False
        self._running = True
        stop = (f"24h VWAP stop (0.75-3%)" if self.stop_mode == 'vwap' else f"fixed {self.stop_pct}% stop")
        print(f"{TAG} Started [{self.mode}] | ${self.amount_usd:,.0f} per trade -> lots = ${self.amount_usd:,.0f} ÷ "
              f"(price × coins per lot) | max {self.max_positions} open, {self.max_trades_per_day} entries/day")
        print(f"{TAG} {stop}, target {self.rr}R, max hold {self.max_hold_hours:g}h | sectors: "
              f"{', '.join(self.sectors) or 'all'} ({'BULLISH/BEARISH only' if self.require_sector_bias else 'incl. MIXED'}) | "
              f"min turnover ${self.min_turnover_usd:,.0f} | sides: "
              f"{'/'.join(s for s, ok in (('LONG', self.allow_long), ('SHORT', self.allow_short)) if ok)} | "
              f"scan every {self.scan_interval // 60} min")
        if self.rating_mode != 'off':
            from api.crypto_scanner import WEIGHTS
            w = self.rating_weights or WEIGHTS
            print(f"{TAG} Rating {'filter on scanner setups' if self.rating_mode == 'filter' else 'decides entries (any coin)'}"
                  f": rating >= {self.min_rating:g}/100, best first | weights "
                  + ', '.join(f"{k} {v:g}%" for k, v in w.items() if v))
        if not self.dry_run:
            print(f"{TAG} ⚠ LIVE: real market orders on Delta Exchange. Margin/leverage follow your Delta account "
                  f"settings for each product.")
        with self._legs_lock:
            if self.legs:
                print(f"{TAG} Resumed with {len(self.legs)} open position(s): "
                      + ', '.join(f"{l['side'].upper()} {l['symbol']} x{l['lots']}" for l in self.legs))
        return True

    def monitor(self):
        self._apply_thread_context()
        while self._running:
            try:
                self._manage_positions()
                if time.time() >= self._next_scan_ts:
                    self._next_scan_ts = time.time() + self.scan_interval
                    self._scan_and_enter()
            except Exception as e:
                logger.error("%s cycle failed: %s", TAG, e)
                print(f"{TAG} ⚠ cycle error: {e}")
            self._interruptible_sleep(self.price_check_sec)

    def close_all(self):
        """Manual stop: close every open position (market order in LIVE)."""
        self._running = False
        for leg in list(self.legs):
            self._exit(leg, 'manual_stop')
        self._persist_state()

    @property
    def pnl(self):
        with self._legs_lock:
            open_pnl = sum(l.get('current_pnl', 0.0) for l in self.legs)
        return self.cumulative_pnl + open_pnl

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(5, max(0.1, end - time.time())))

    # ------------------------------------------------------------------ sizing
    def lots_for(self, price, contract_value):
        """Lots that $amount_usd buys (whole lots, rounded down). 0 if one lot costs more."""
        lot_usd = price * contract_value
        return int(math.floor(self.amount_usd / lot_usd)) if lot_usd > 0 else 0

    # ------------------------------------------------------------------ entries
    def _today(self):
        return datetime.now(IST).strftime('%Y-%m-%d')

    def _scan_and_enter(self):
        with self._legs_lock:
            held = {l['symbol'] for l in self.legs}
            free = self.max_positions - len(self.legs)
        today = self._today()
        left_today = self.max_trades_per_day - self._day_trades.get(today, 0)
        if free <= 0 or left_today <= 0:
            return
        from api.crypto_scanner import get_overview, get_sector_coins
        ov = get_overview()
        secs = [s for s in ov['sectors'] if (not self.sectors or s['sector'] in self.sectors)
                and (s['bias'] != 'MIXED' or not self.require_sector_bias)]
        secs.sort(key=lambda s: abs(s['pchange']), reverse=True)
        now = datetime.now(IST)
        summary = ', '.join(f"{s['sector']} {s['pchange']:+.2f}% {s['bias'][0]}" for s in secs[:5]) or 'none'
        print(f"\n{TAG} {now:%Y-%m-%d %H:%M} IST scan | free slots {free}, entries left today {left_today} | "
              f"sectors: {summary}")
        skipped, taken = [], []
        for sec in secs:
            if free <= 0 or left_today <= 0:
                break
            try:
                d = get_sector_coins(sec['sector'])
            except Exception as e:
                print(f"{TAG}   {sec['sector']}: unavailable ({e})")
                continue
            for r in self._candidates(d['stocks']):
                if free <= 0 or left_today <= 0:
                    break
                side_key = r['direction'] if self.rating_mode == 'rating' else r['setup']
                if (side_key == 'LONG' and not self.allow_long) or (side_key == 'SHORT' and not self.allow_short):
                    continue
                sym = r['symbol']
                if sym in held or sym in taken:
                    continue
                if time.time() - self._recent.get(sym, 0) < self.cooldown_hours * 3600:
                    skipped.append(f"{r['ticker']} (cooldown)")
                    continue
                if r['turnover_usd'] < self.min_turnover_usd:
                    skipped.append(f"{r['ticker']} (turnover ${r['turnover_usd'] / 1e6:.2f}M)")
                    continue
                if self._enter(sec, r):
                    taken.append(sym)
                    free -= 1
                    left_today -= 1
        if skipped:
            print(f"{TAG}   passed over: {', '.join(skipped[:10])}")
        self._last_scan = {'time': now.isoformat(), 'taken': taken, 'skipped': skipped[:10],
                           'sectors': [{'sector': s['sector'], 'pchange': s['pchange'], 'bias': s['bias']} for s in secs[:6]]}
        if not taken:
            print(f"{TAG}   no new entry this scan")
        self._persist_state()

    def _rating(self, r):
        """The coin's weighted rating with this strategy's weights (falls back to the scanner's default weights)."""
        if self.rating_weights:
            from api.crypto_scanner import rating
            return rating(r.get('components') or {}, self.rating_weights)
        return r.get('rating')

    def _candidates(self, rows):
        """Coins of one sector to try, in order, according to rating_mode (see __init__)."""
        if self.rating_mode == 'off':
            return [r for r in rows if r['setup'] in ('LONG', 'SHORT') and r.get('plan')]
        # the VWAP stop needs the scanner's stop plan (price on the trade side of VWAP); a fixed % stop does not
        has_stop = (lambda r: bool(r.get('plan'))) if self.stop_mode == 'vwap' else (lambda r: True)
        pool = [r for r in rows if has_stop(r)
                and (self.rating_mode == 'rating' or r['setup'] in ('LONG', 'SHORT'))]
        out = []
        for r in pool:
            rt = self._rating(r)
            if rt is None or rt < self.min_rating:
                continue
            out.append(dict(r, _rating=rt))
        return sorted(out, key=lambda r: -r['_rating'])

    def _enter(self, sec, r):
        side = 'buy' if (r['direction'] if self.rating_mode == 'rating' else r['setup']) == 'LONG' else 'sell'
        sign = 1 if side == 'buy' else -1
        price, cv = r['price'], r['contract_value']
        lots = self.lots_for(price, cv)
        if lots <= 0:
            print(f"{TAG}   skip {r['ticker']}: 1 lot = ${price * cv:,.2f} > ${self.amount_usd:,.0f} per trade")
            return False
        fill = price
        if not self.dry_run:
            from api.orders import place_order
            res = place_order(r.get('product_id'), r['symbol'], lots, side)
            if not res:
                print(f"{TAG}   ✗ {side.upper()} {r['symbol']} x{lots} order FAILED — not recorded")
                return False
            fill = float(res.get('average_fill_price') or price)
        stop_pct = r['plan']['stop_pct'] if self.stop_mode == 'vwap' else self.stop_pct
        stop = fill * (1 - sign * stop_pct / 100)
        target = fill * (1 + sign * self.rr * stop_pct / 100)
        notional = fill * cv * lots
        fee = est_fee(notional)
        now = datetime.now(IST)
        leg = {'symbol': r['symbol'], 'product_id': r.get('product_id'), 'coin': r['ticker'], 'side': side,
               'lots': lots, 'size': lots, 'contract_value': cv, 'entry_price': fill, 'current_mark': fill,
               'current_pnl': 0.0, 'stop': stop, 'target': target, 'stop_pct': round(stop_pct, 3),
               'notional': round(notional, 2), 'entry_fee': round(fee, 4), 'sector': sec['sector'],
               'opened_at': now.isoformat(), 'opened_ts': time.time()}
        with self._legs_lock:
            self.legs.append(leg)
        self.total_trades += 1
        self._recent[r['symbol']] = time.time()
        today = self._today()
        self._day_trades = {today: self._day_trades.get(today, 0) + 1}
        self.trade_log.append({'ts': now.isoformat(), 'action': side.upper(), 'symbol': r['symbol'], 'lots': lots,
                               'price': round(fill, 8), 'notional': round(notional, 2), 'sector': sec['sector'],
                               'reason': f"{r['setup'] or 'no setup'} {r['direction']}"
                                         + (f", rating {r['_rating']:g}" if r.get('_rating') is not None else ''),
                               'rating': r.get('_rating'), 'components': r.get('components'), 'mode': self.mode})
        leg['rating'] = r.get('_rating')
        self._track_open(leg)
        risk = notional * stop_pct / 100
        print(f"{TAG} [{self.mode}] {'LONG' if side == 'buy' else 'SHORT'} {r['symbol']} x{lots} lots "
              f"@ {fill:.6g} = ${notional:,.2f} ({cv:g} {r['ticker']}/lot) | {sec['sector']} {sec['pchange']:+.2f}% "
              f"| RVOL {r['rvol']} | OI 6h {r['oi_chg_6h_pct']}%"
              + (f" | rating {r['_rating']:g} (RSI {r.get('rsi')}, EMA7/20 {r.get('ema_state')}, "
                 f"news {(r.get('components') or {}).get('news')}, X {(r.get('components') or {}).get('twitter')})"
                 if r.get('_rating') is not None else ''))
        print(f"{TAG}   stop {stop:.6g} (-{stop_pct:.2f}% ≈ -${risk:,.2f}) / target {target:.6g} "
              f"(+{self.rr * stop_pct:.2f}% ≈ +${risk * self.rr:,.2f}) | est. fees ${fee * 2:,.2f} round trip")
        return True

    # ------------------------------------------------------------------ exits
    def _marks(self):
        r = requests.get(f'{DELTA_PROD}/v2/tickers', params={'contract_types': 'perpetual_futures'}, timeout=15)
        r.raise_for_status()
        return {t['symbol']: float(t.get('mark_price') or t.get('close') or 0) for t in r.json()['result']}

    def _manage_positions(self):
        with self._legs_lock:
            legs = list(self.legs)
        if not legs:
            return
        try:
            marks = self._marks()
            self._price_failures = 0
        except Exception as e:
            self._price_failures += 1
            if self._price_failures in (3, MAX_PRICE_FAILURES):
                print(f"{TAG} ⚠ price fetch failed {self._price_failures}x: {e}")
            return
        for leg in legs:
            px = marks.get(leg['symbol'])
            if not px:
                continue
            sign = 1 if leg['side'] == 'buy' else -1
            leg['current_mark'] = px
            leg['current_pnl'] = round(sign * (px - leg['entry_price']) * leg['lots'] * leg['contract_value'], 4)
            hit_stop = px <= leg['stop'] if sign == 1 else px >= leg['stop']
            hit_tgt = px >= leg['target'] if sign == 1 else px <= leg['target']
            if hit_stop:
                self._exit(leg, 'stop_loss', px)
            elif hit_tgt:
                self._exit(leg, 'take_profit', px)
            elif time.time() - leg.get('opened_ts', time.time()) >= self.max_hold_hours * 3600:
                self._exit(leg, 'time_stop', px)
        self._pnl_history.append((datetime.now(IST).isoformat(), round(self.pnl, 2)))
        self._pnl_history = self._pnl_history[-500:]

    def _exit(self, leg, reason, px=None):
        if px is None:
            try:
                px = self._marks().get(leg['symbol']) or leg['current_mark']
            except Exception:
                px = leg.get('current_mark') or leg['entry_price']
        close_side = 'sell' if leg['side'] == 'buy' else 'buy'
        fill = px
        if not self.dry_run:
            from api.orders import place_order
            res = place_order(leg.get('product_id'), leg['symbol'], leg['lots'], close_side)
            if not res:
                print(f"{TAG} ✗ CLOSE {leg['symbol']} x{leg['lots']} order FAILED ({reason}) — position kept, "
                      f"will retry next check. Check Delta if this repeats.")
                return False
            fill = float(res.get('average_fill_price') or px)
        sign = 1 if leg['side'] == 'buy' else -1
        gross = sign * (fill - leg['entry_price']) * leg['lots'] * leg['contract_value']
        fees = leg.get('entry_fee', 0) + est_fee(fill * leg['lots'] * leg['contract_value'])
        net = gross - fees
        with self._legs_lock:
            if leg in self.legs:
                self.legs.remove(leg)
            self.cumulative_pnl += net
            self.total_fees += fees
        self.wins += net > 0
        self.losses += net <= 0
        now = datetime.now(IST)
        held_h = (time.time() - leg.get('opened_ts', time.time())) / 3600
        self.trade_log.append({'ts': now.isoformat(), 'action': f"CLOSE_{leg['side'].upper()}", 'symbol': leg['symbol'],
                               'lots': leg['lots'], 'price': round(fill, 8), 'entry': round(leg['entry_price'], 8),
                               'gross': round(gross, 2), 'fees': round(fees, 2), 'pnl': round(net, 2),
                               'reason': reason, 'held_h': round(held_h, 1), 'mode': self.mode})
        self._track_close(leg)
        print(f"{TAG} [{self.mode}] CLOSE {leg['symbol']} x{leg['lots']} @ {fill:.6g} | {reason} after {held_h:.1f}h | "
              f"gross ${gross:+,.2f} - fees ${fees:,.2f} = ${net:+,.2f} | cumulative ${self.cumulative_pnl:+,.2f}")
        self._persist_state()
        return True

    # ------------------------------------------------------------------ app integration
    def _track_open(self, leg):
        try:
            if self._user_id is not None:
                from api.position_tracker import position_tracker
                position_tracker.open(self._user_id, leg['product_id'], leg['symbol'], type='futures', strike='0',
                                      side=leg['side'], size=leg['lots'], entry_price=leg['entry_price'],
                                      asset=leg['coin'], source='Crypto Scanner Futures',
                                      strategy_sid=self._sid or '', contract_value=leg['contract_value'])
        except Exception as e:
            logger.debug("%s position_tracker.open failed: %s", TAG, e)

    def _track_close(self, leg):
        try:
            if self._user_id is not None:
                from api.position_tracker import position_tracker
                position_tracker.close(self._user_id, leg['product_id'])
        except Exception as e:
            logger.debug("%s position_tracker.close failed: %s", TAG, e)

    def _apply_thread_context(self):
        from config import set_thread_credentials
        if self._api_key or self._broker:
            set_thread_credentials(self._api_key or '', self._api_secret or '', self._broker)
        try:
            if self._log_queue is not None:
                from app import LogCapture
                LogCapture._local.log_queue = self._log_queue
                LogCapture._local.log_history = self._log_history
        except Exception:
            pass

    def _persist_state(self):
        if not self._sid:
            return
        try:
            from models import update_strategy_db
            with self._legs_lock:
                legs = [dict(l) for l in self.legs]
            details = {**self._base_params, 'trade_log': self.trade_log[-200:], 'cumulative_pnl': self.cumulative_pnl,
                       'total_fees': self.total_fees, 'total_trades': self.total_trades, 'wins': self.wins,
                       'losses': self.losses, 'recent': self._recent, 'day_trades': self._day_trades,
                       'last_scan': self._last_scan, 'asset': 'CRYPTO'}
            update_strategy_db(self._sid, details=details, legs=legs, pnl=round(self.pnl, 2))
        except Exception as e:
            logger.warning("%s persist failed: %s", TAG, e)

    def restore_state(self, details=None, legs=None):
        d = details or {}
        self.cumulative_pnl = float(d.get('cumulative_pnl') or 0)
        self.total_fees = float(d.get('total_fees') or 0)
        self.total_trades = int(d.get('total_trades') or 0)
        self.wins, self.losses = int(d.get('wins') or 0), int(d.get('losses') or 0)
        self.trade_log = d.get('trade_log') or []
        self._recent = {k: float(v) for k, v in (d.get('recent') or {}).items()}
        self._day_trades = d.get('day_trades') or {}
        self._last_scan = d.get('last_scan')
        keep = []
        for l in legs or []:
            if isinstance(l, dict) and l.get('symbol') and l.get('lots') and l.get('stop') and l.get('target'):
                keep.append(dict(l, opened_ts=float(l.get('opened_ts') or time.time())))
        with self._legs_lock:
            self.legs = keep
        return self

    def status(self):
        with self._legs_lock:
            legs = [dict(l) for l in self.legs]
        closed = self.wins + self.losses
        return {'mode': self.mode, 'cumulative_pnl': round(self.cumulative_pnl, 2), 'total_pnl': round(self.pnl, 2),
                'total_fees': round(self.total_fees, 2), 'total_trades': self.total_trades,
                'win_rate': round(self.wins / closed * 100, 1) if closed else None, 'closed_trades': closed,
                'legs': legs, 'trade_log': self.trade_log[-30:], 'last_scan': self._last_scan,
                'next_scan_in_s': max(0, int(self._next_scan_ts - time.time())), 'params': self._base_params}
