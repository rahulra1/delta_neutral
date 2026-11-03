"""NSE Sector Options — PAPER trades ATM stock options on opening-range breakouts of Sector Scanner picks.

Daily flow (IST, trading days only):
  1. Opening range = the stock's high / low of the first 30 minutes, 09:15-09:45 (5m Yahoo bars).
  2. At 09:45 (entry_hour:entry_minute) read the live NSE sectors (api.sector_scanner). Walk the
     BULLISH / BEARISH sectors from the biggest |% change| down and put their LONG / SHORT / WATCH
     stocks that have listed stock options on a watchlist (max max_watchlist). Direction = sector bias.
  3. Every completed 5-minute candle (candle_min, default 5: 09:50, 09:55 ... last entry, default 14:45):
       bullish stock: 5m close > 30-min high  (and > the 5-minute VWAP)  -> paper-BUY the ATM CALL
       bearish stock: 5m close < 30-min low   (and < the 5-minute VWAP)  -> paper-BUY the ATM PUT
     Expiry = nearest with >= min_dte (default 10) days left. Price = Groww option-chain LTP.
     Limits: max_open (5) positions at a time, max_entries (6) per day, one entry per stock per day.
  4. Each position exits ONLY on the STOCK price (Groww CASH LTP), checked every monitor_interval s:
       stop   = stock moves stop_pct % against the entry stock price
       target = stock moves target_pct % in favour
     else square-off at the exit time (default 15:15). Paper-SELL at the Groww option LTP.
  5. Estimated Groww F&O charges are deducted from the realized P&L.

No real orders are ever placed (paper only). Needs a Groww profile with a valid token for
the option chain / LTPs; the sector scan and candles use public NSE / Yahoo data.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
TAG = '[Sector Options]'
MAX_PRICE_FAILURES = 20          # consecutive failed price polls before we warn loudly
MARKET_OPEN = (9, 15)
CANDLE_DELAY_S = 60              # wait this long after a candle closes so Yahoo has the last 5m bar
CANDLE_CHOICES = (5, 15)         # breakout candle timeframes (minutes); both divide the 09:15 grid


def estimate_charges(buy_value, sell_value):
    """Groww F&O option-buy charges for one round trip (Rs)."""
    brokerage = 40.0                                  # Rs 20 per executed order x 2
    txn = 0.0003503 * (buy_value + sell_value)        # NSE options transaction charge
    sebi = 0.000001 * (buy_value + sell_value)
    stt = 0.001 * sell_value                          # 0.1% on sell-side premium
    stamp = 0.00003 * buy_value
    return round(brokerage + txn + sebi + stt + stamp + 0.18 * (brokerage + txn + sebi), 2)


# --------------------------------------------------------------------------- candles

def session_bars(symbol):
    """Latest session's 5m bars for an NSE stock from Yahoo: (date, [(datetime, o, h, l, c, v), ...])."""
    from api.chart import _get_session as _yahoo_session
    s, crumb = _yahoo_session()
    r = s.get(f'https://query2.finance.yahoo.com/v8/finance/chart/{quote(symbol)}.NS',
              params={'interval': '5m', 'range': '5d', 'crumb': crumb or ''}, timeout=10)
    r.raise_for_status()
    res = (r.json().get('chart', {}).get('result') or [None])[0]
    if not res or not res.get('timestamp'):
        return None, []
    q = res['indicators']['quote'][0]
    days = {}
    for i, ts in enumerate(res['timestamp']):
        o, h, l, c = q['open'][i], q['high'][i], q['low'][i], q['close'][i]
        if None in (o, h, l, c):
            continue
        t = datetime.fromtimestamp(ts, IST)
        days.setdefault(t.date(), []).append((t, o, h, l, c, q['volume'][i] or 0))
    if not days:
        return None, []
    d = max(days)
    return d, sorted(days[d])


def opening_range(bars, end):
    """High / low of the bars from 09:15 up to (not incl.) `end` (a datetime)."""
    start = end.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    orb = [b for b in bars if start <= b[0] < end]
    if len(orb) < max(1, int((end - start).total_seconds() // 300) - 1):   # allow one missing 5m bar
        return None
    return {'high': round(max(b[2] for b in orb), 2), 'low': round(min(b[3] for b in orb), 2), 'bars': len(orb)}


def candle_and_vwap(bars, end, minutes=5):
    """The `minutes` candle ending at `end` and the 5-minute-bar session VWAP up to `end`."""
    c_bars = [b for b in bars if end - timedelta(minutes=minutes) <= b[0] < end]
    if not c_bars:
        return None, None
    candle = {'open': c_bars[0][1], 'high': max(b[2] for b in c_bars), 'low': min(b[3] for b in c_bars),
              'close': c_bars[-1][4], 'end': end.strftime('%H:%M')}
    upto = [b for b in bars if b[0] < end]
    vol = sum(b[5] for b in upto)
    vwap = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in upto) / vol if vol else None
    return candle, (round(vwap, 2) if vwap else None)


def floor_candle(t, minutes=5):
    """Start of the `minutes` slot containing t (5/15-minute slots are aligned to 09:15)."""
    return t.replace(minute=t.minute - t.minute % minutes, second=0, microsecond=0)


class NseSectorOptions(BaseStrategy):

    def __init__(self, entry_hour=9, entry_minute=45, exit_hour=15, exit_minute=15,
                 stop_pct=0.5, target_pct=0.75, lots=1, min_dte=10, max_premium=100000.0,
                 entry_window_min=30, sector_group='Sectoral', trading_days=None,
                 monitor_interval=15, run_immediately=False,
                 max_open=5, max_entries=6, max_watchlist=15, last_entry_hour=14, last_entry_minute=45,
                 use_vwap=True, candle_min=5):
        self.entry_hour, self.entry_minute = int(entry_hour), int(entry_minute)
        self.exit_hour, self.exit_minute = int(exit_hour), int(exit_minute)
        self.last_entry_hour, self.last_entry_minute = int(last_entry_hour), int(last_entry_minute)
        self.stop_pct, self.target_pct = float(stop_pct), float(target_pct)
        self.lots = max(1, int(lots))
        self.min_dte = int(min_dte)
        self.max_premium = float(max_premium)
        self.entry_window_min = int(entry_window_min)   # keep retrying the watchlist scan this long
        self.sector_group = sector_group
        self.trading_days = trading_days if trading_days is not None else [0, 1, 2, 3, 4]
        self.monitor_interval = max(5, int(monitor_interval))
        self.run_immediately = bool(run_immediately)
        self.max_open = max(1, int(max_open))
        self.max_entries = max(1, int(max_entries))
        self.max_watchlist = max(1, int(max_watchlist))
        self.use_vwap = bool(use_vwap)
        self.candle_min = int(candle_min) if int(candle_min) in CANDLE_CHOICES else 5

        self.legs = []
        self._legs_lock = threading.Lock()
        self.cumulative_pnl = 0.0
        self.total_charges = 0.0
        self.total_trades = 0
        self.total_days_traded = 0
        self.trade_log = []
        self._day = None                  # 'YYYY-MM-DD' the watchlist / counters belong to
        self._entered_day = None          # day on which no more entries will be made
        self._watchlist = []              # [{symbol, direction, sector, or_high, or_low, status, ...}]
        self._entries_today = 0
        self._last_candle = None          # 'HH:MM' end of the last breakout candle evaluated
        self._last_scan = None            # summary of the last scan for the UI
        self._running = False
        self._pnl_history = []
        self._price_failures = 0

        # set by app.py
        self._sid = None
        self._user_id = None
        self._api_key = None
        self._api_secret = None
        self._broker = None
        self._log_queue = None
        self._log_history = None

        self._base_params = {
            'entry_hour': self.entry_hour, 'entry_minute': self.entry_minute,
            'exit_hour': self.exit_hour, 'exit_minute': self.exit_minute,
            'last_entry_hour': self.last_entry_hour, 'last_entry_minute': self.last_entry_minute,
            'stop_pct': self.stop_pct, 'target_pct': self.target_pct, 'lots': self.lots,
            'min_dte': self.min_dte, 'max_premium': self.max_premium,
            'entry_window_min': self.entry_window_min, 'sector_group': self.sector_group,
            'trading_days': self.trading_days, 'monitoring_interval': self.monitor_interval,
            'max_open': self.max_open, 'max_entries': self.max_entries,
            'max_watchlist': self.max_watchlist, 'use_vwap': self.use_vwap, 'candle_min': self.candle_min,
            'run_immediately': self.run_immediately, 'dry_run': True,
        }

    # ------------------------------------------------------------------ lifecycle
    def initialize(self):
        self._running = True
        from config import get_api_key, get_api_secret, _thread_local
        self._api_key = get_api_key()
        self._api_secret = get_api_secret()
        self._broker = getattr(_thread_local, 'broker', '')
        days = ','.join(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][d] for d in self.trading_days)
        print(f"{TAG} Started [PAPER] | 30-min range 09:15-{self.entry_hour:02d}:{self.entry_minute:02d}, "
              f"{self.candle_min}m breakouts until {self.last_entry_hour:02d}:{self.last_entry_minute:02d}, "
              f"square-off {self.exit_hour:02d}:{self.exit_minute:02d} IST | days {days}")
        print(f"{TAG} Watchlist: up to {self.max_watchlist} {self.sector_group.lower()} LONG/SHORT/WATCH stocks | "
              f"entry: {self.candle_min}m close beyond the range" + (" + 5m VWAP" if self.use_vwap else "") +
              f" | max {self.max_open} open / {self.max_entries} entries a day | {self.lots} lot ATM CE/PE | "
              f"exit on STOCK: SL -{self.stop_pct}% / TP +{self.target_pct}% | min {self.min_dte}d to expiry"
              + (" | RUN-NOW (test)" if self.run_immediately else ""))
        if self._broker != 'groww':
            print(f"{TAG} ⚠ Profile broker is '{self._broker}', not 'groww' — option prices need a Groww profile")
        open_legs = self._open_legs()
        if open_legs:
            print(f"{TAG} Resumed with {len(open_legs)} open position(s): "
                  + ', '.join(l['symbol'] for l in open_legs))
        return True

    def monitor(self):
        self._apply_thread_context()
        first = True
        while self._running:
            try:
                now = datetime.now(IST)
                force = first and self.run_immediately
                self._roll_day(now)
                if self._open_legs():
                    self._manage_positions(now)
                if force or self._entries_allowed(now):
                    if not self._watchlist and (force or self._watchlist_due(now)):
                        self._build_watchlist(now, force=force)
                    if self._watchlist:
                        self._check_breakouts(now, force=force)
                first = False
            except Exception as e:
                logger.error("%s cycle failed: %s", TAG, e)
                print(f"{TAG} ⚠ cycle error: {e}")
            self._interruptible_sleep(self._sleep_for(datetime.now(IST)))

    def close_all(self):
        """Manual stop: paper-sell every open option at the current LTP."""
        self._running = False
        for leg in self._open_legs():
            self._exit(leg, reason='manual_stop')
        self._persist_state()

    @property
    def pnl(self):
        """Realized net P&L + open (gross) P&L from the last marks. No network calls."""
        with self._legs_lock:
            open_pnl = sum(l.get('current_pnl', 0) for l in self.legs if not l.get('stopped'))
        return self.cumulative_pnl + open_pnl

    # ------------------------------------------------------------------ timing
    def _at(self, now, h, m):
        return now.replace(hour=h, minute=m, second=0, microsecond=0)

    def _entry_time(self, now):
        return self._at(now, self.entry_hour, self.entry_minute)

    def _last_entry_time(self, now):
        return self._at(now, self.last_entry_hour, self.last_entry_minute)

    def _exit_time(self, now):
        return self._at(now, self.exit_hour, self.exit_minute)

    def _roll_day(self, now):
        today = now.strftime('%Y-%m-%d')
        if self._day != today:
            self._day = today
            self._watchlist, self._entries_today, self._last_candle = [], 0, None
            self._persist_state()

    def _entries_allowed(self, now):
        if now.weekday() not in self.trading_days or self._entered_day == self._day:
            return False
        # the last candle that may trigger closes at the last-entry time (+ the Yahoo delay)
        if now >= self._last_entry_time(now) + timedelta(seconds=CANDLE_DELAY_S * 3):
            self._finish_day(now, f"entry window closed ({self._entries_today} entries)")
            return False
        return now >= self._entry_time(now)

    def _watchlist_due(self, now):
        return now < self._entry_time(now) + timedelta(minutes=self.entry_window_min) \
            or self._last_scan is None or self._last_scan.get('day') != self._day

    def _finish_day(self, now, why):
        if self._entered_day != self._day:
            self._entered_day = self._day
            print(f"{TAG} {self._day} no more entries today — {why}")
            self._persist_state()

    def _sleep_for(self, now):
        if self._open_legs() or (self._watchlist and self._entered_day != self._day):
            return self.monitor_interval
        if now.weekday() in self.trading_days and self._entered_day != now.strftime('%Y-%m-%d'):
            start = self._entry_time(now)
            if now < start:
                return max(5, min((start - now).total_seconds(), 60))
            return 60
        return 300

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(5, max(0.1, end - time.time())))

    # ------------------------------------------------------------------ watchlist (09:45)
    def _build_watchlist(self, now, force=False):
        from api.sector_scanner import get_sectors, get_sector_stocks
        from api.groww import get_stock_option_meta
        overview = get_sectors()
        mkt = (overview.get('market') or {}).get('status')
        if mkt != 'Open' and not force:
            self._finish_day(now, f"market status '{mkt}'")
            return
        cands = [s for s in overview['sectors'] if s.get('group') == self.sector_group and s['bias'] != 'MIXED']
        cands.sort(key=lambda s: abs(s['pchange']), reverse=True)
        print(f"\n{TAG} {now:%Y-%m-%d %H:%M} scan | {len(cands)} BULLISH/BEARISH {self.sector_group.lower()} sectors: "
              + ', '.join(f"{s['index'].replace('NIFTY ', '')} {s['pchange']:+.2f}%" for s in cands[:6]))
        setups = ('LONG', 'SHORT', 'WATCH', 'LATE') if force else ('LONG', 'SHORT', 'WATCH')
        picks, seen, skipped = [], set(), []
        for sec in cands:
            if len(picks) >= self.max_watchlist:
                break
            try:
                d = get_sector_stocks(sec['index'])
            except Exception as e:
                print(f"{TAG}   {sec['index']}: unavailable ({e})")
                continue
            if d['bias'] == 'MIXED':
                skipped.append(f"{sec['index']} now MIXED")
                continue
            want = 'LONG' if d['bias'] == 'BULLISH' else 'SHORT'
            for r in d['stocks']:
                if len(picks) >= self.max_watchlist:
                    break
                if r['symbol'] in seen or r['setup'] not in setups or r['direction'] != want:
                    continue
                if not get_stock_option_meta(r['symbol']):
                    skipped.append(f"{r['symbol']} (no stock options)")
                    continue
                seen.add(r['symbol'])
                picks.append((d, r))

        def fetch(sym):
            try:
                return session_bars(sym)
            except Exception as e:
                logger.warning("%s candles %s failed: %s", TAG, sym, e)
                return None, []

        with ThreadPoolExecutor(max_workers=8) as ex:
            bars = list(ex.map(fetch, [r['symbol'] for _, r in picks]))
        watch = []
        for (d, r), (bdate, b) in zip(picks, bars):
            if not b or (bdate != now.date() and not force):
                skipped.append(f"{r['symbol']} (no candles today)")
                continue
            orng = opening_range(b, self._entry_time(b[0][0]))
            if not orng:
                skipped.append(f"{r['symbol']} (incomplete 30-min range)")
                continue
            watch.append({'symbol': r['symbol'], 'direction': r['direction'], 'setup': r['setup'],
                          'sector': d['index'], 'sector_pchg': d['pchange'], 'rvol': r.get('rvol'),
                          'or_high': orng['high'], 'or_low': orng['low'], 'status': 'watching',
                          'last_close': None, 'vwap': None, 'last_candle': None, 'note': None})
        self._watchlist = watch
        self._last_scan = {'time': now.isoformat(), 'day': self._day, 'watchlist': len(watch),
                           'sector': watch[0]['sector'] if watch else None,
                           'sector_pchg': watch[0]['sector_pchg'] if watch else None,
                           'stock': None, 'skipped': skipped[-10:]}
        if skipped:
            print(f"{TAG}   passed over: {', '.join(skipped[-8:])}")
        if watch:
            print(f"{TAG} Watchlist ({len(watch)}): " + ' | '.join(
                f"{w['symbol']} {w['direction']} [{w['or_low']}–{w['or_high']}]" for w in watch))
        elif force or now >= self._entry_time(now) + timedelta(minutes=self.entry_window_min):
            self._finish_day(now, "no stocks for the watchlist")
        else:
            print(f"{TAG} No watchlist stocks yet — rescanning every minute")
        self._persist_state()

    # ------------------------------------------------------------------ breakout checks (5m candles)
    def _check_breakouts(self, now, force=False):
        end = floor_candle(now - timedelta(seconds=CANDLE_DELAY_S), self.candle_min)
        key = end.strftime('%H:%M')
        if not force:
            if end <= self._entry_time(now) or self._last_candle == key:
                return
            if end > self._last_entry_time(now):
                self._finish_day(now, f"last entry candle passed ({self._entries_today} entries)")
                return
        pending = [w for w in self._watchlist if w['status'] == 'watching']
        if not pending:
            self._last_candle = key
            self._finish_day(now, "every watchlist stock has been traded")
            return

        def fetch(sym):
            try:
                return session_bars(sym)
            except Exception as e:
                logger.warning("%s candles %s failed: %s", TAG, sym, e)
                return None, []

        with ThreadPoolExecutor(max_workers=8) as ex:
            bars = dict(zip([w['symbol'] for w in pending], ex.map(fetch, [w['symbol'] for w in pending])))
        signals = []
        for w in pending:
            bdate, b = bars.get(w['symbol']) or (None, [])
            if not b or (bdate != now.date() and not force):
                continue
            # test run: the last fully closed candle of the session (the newest bar may still be forming)
            c_end = end if not force else floor_candle(b[-1][0], self.candle_min)
            candle, vwap = candle_and_vwap(b, c_end, self.candle_min)
            if not candle:
                continue
            w.update(last_close=round(candle['close'], 2), vwap=vwap, last_candle=candle['end'])
            long_ = w['direction'] == 'LONG'
            broke = candle['close'] > w['or_high'] if long_ else candle['close'] < w['or_low']
            vwap_ok = (not self.use_vwap or vwap is None or
                       (candle['close'] > vwap if long_ else candle['close'] < vwap))
            if broke and vwap_ok:
                signals.append((w, candle, vwap))
            elif broke:
                w['note'] = f"{candle['end']} broke range but {'below' if long_ else 'above'} 5m VWAP {vwap}"
        self._last_candle = key
        if signals or end.minute % 15 == 0:      # quiet heartbeat every 15 min, every candle with a signal
            print(f"{TAG} {key} {self.candle_min}m candle | {len(pending)} watching | {len(signals)} breakout(s)"
                  + (': ' + ', '.join(f"{w['symbol']} {c['close']:.2f}" for w, c, _ in signals) if signals else ''))

        for w, candle, vwap in signals:
            if self._entries_today >= self.max_entries:
                w['note'] = f"{candle['end']} breakout — max {self.max_entries} entries reached"
                continue
            if len(self._open_legs()) >= self.max_open:
                w['note'] = f"{candle['end']} breakout — {self.max_open} positions already open"
                continue
            contract, why = self._atm_contract(w['symbol'], 'CE' if w['direction'] == 'LONG' else 'PE')
            if not contract:
                w['note'] = f"{candle['end']} breakout — {why}"
                print(f"{TAG}   {w['symbol']} breakout skipped: {why}")
                continue
            self._enter(w, candle, vwap, contract)
            w['status'] = 'entered'
            self._last_scan = self._last_scan or {'day': self._day}
            self._last_scan.update(stock=w['symbol'], sector=w['sector'], sector_pchg=w['sector_pchg'],
                                   setup=w['direction'], time=now.isoformat())
        if self._entries_today >= self.max_entries:
            self._finish_day(now, f"max {self.max_entries} entries reached")
        self._persist_state()

    def _atm_contract(self, symbol, kind):
        """Pick the ATM contract on the nearest expiry with >= min_dte days. Returns (contract, reason)."""
        from api.groww import get_stock_option_meta, get_groww_chain
        meta = get_stock_option_meta(symbol)
        if not meta:
            return None, 'no stock options'
        today = datetime.now(IST).date()
        exps = [e for e in meta['expiries']
                if (datetime.strptime(e, '%Y-%m-%d').date() - today).days >= self.min_dte]
        if not exps:
            return None, 'no expiry'
        expiry = exps[0]
        chain, spot, _ = get_groww_chain(symbol, datetime.strptime(expiry, '%Y-%m-%d').strftime('%d-%m-%Y'))
        if not chain or not spot:
            return None, 'Groww chain unavailable'
        side_key = 'call' if kind == 'CE' else 'put'
        rows = [r for r in chain if r.get(side_key) and float(r[side_key].get('mark_price') or 0) > 0]
        if not rows:
            return None, 'no priced strikes'
        row = min(rows, key=lambda r: abs(float(r['strike']) - spot))
        leg = row[side_key]
        strike = float(row['strike'])
        ltp = float(leg['mark_price'])
        trading_symbol = leg.get('trading_symbol') or meta['contracts'].get((expiry, kind, strike))
        if not trading_symbol:
            return None, 'no trading symbol'
        qty = meta['lot_size'] * self.lots
        if ltp * qty > self.max_premium:
            return None, f'premium Rs {ltp * qty:,.0f} > max'
        return {'trading_symbol': trading_symbol, 'expiry': expiry, 'kind': kind, 'strike': strike,
                'ltp': ltp, 'spot': float(spot), 'lot_size': meta['lot_size'], 'qty': qty,
                'oi': leg.get('oi'), 'iv': leg.get('iv'), 'delta': leg.get('delta')}, None

    def _enter(self, w, candle, vwap, c):
        side = 1 if c['kind'] == 'CE' else -1
        stock_entry = c['spot']
        stop = stock_entry * (1 - side * self.stop_pct / 100)
        target = stock_entry * (1 + side * self.target_pct / 100)
        now = datetime.now(IST)
        level = w['or_high'] if side == 1 else w['or_low']
        leg = {
            'symbol': c['trading_symbol'], 'underlying': w['symbol'], 'type': 'call' if side == 1 else 'put',
            'strike': c['strike'], 'expiry': c['expiry'], 'side': 'buy', 'direction': w['direction'],
            'qty': c['qty'], 'size': self.lots, 'lot_size': c['lot_size'],
            'entry_price': c['ltp'], 'current_mark': c['ltp'], 'current_pnl': 0.0,
            'stock_entry': round(stock_entry, 2), 'stock_mark': round(stock_entry, 2),
            'stock_stop': round(stop, 2), 'stock_target': round(target, 2),
            'or_high': w['or_high'], 'or_low': w['or_low'], 'breakout_candle': candle['end'],
            'sector': w['sector'], 'sector_pchg': w['sector_pchg'],
            'opened_at': now.isoformat(), 'stopped': False,
        }
        with self._legs_lock:
            self.legs = [l for l in self.legs if not l.get('stopped')] + [leg]
        if self._entries_today == 0:
            self.total_days_traded += 1
        self._entries_today += 1
        self.total_trades += 1
        self.trade_log.append({
            'ts': now.isoformat(), 'date': now.strftime('%Y-%m-%d'), 'action': 'BUY',
            'symbol': c['trading_symbol'], 'underlying': w['symbol'], 'qty': c['qty'],
            'price': round(c['ltp'], 2), 'stock_price': round(stock_entry, 2), 'sector': w['sector'],
            'reason': f"{w['direction']} 30-min {'high' if side == 1 else 'low'} {level} break @ {candle['end']}",
        })
        print(f"{TAG} [PAPER] BUY {c['trading_symbol']} x{c['qty']} ({self.lots} lot) @ Rs {c['ltp']:.2f} "
              f"= Rs {c['ltp'] * c['qty']:,.0f} | {w['direction']} {w['symbol']} {self.candle_min}m close {candle['close']:.2f} "
              f"{'>' if side == 1 else '<'} 30-min {'high' if side == 1 else 'low'} {level} | 5m VWAP {vwap} | "
              f"{w['sector']} {w['sector_pchg']:+.2f}% | entry {self._entries_today}/{self.max_entries}, "
              f"open {len(self._open_legs())}/{self.max_open}")
        print(f"{TAG}   stock @ {stock_entry:.2f} SL {stop:.2f} ({-self.stop_pct:+.2f}%) / "
              f"TP {target:.2f} (+{self.target_pct:.2f}%) | expiry {c['expiry']} | IV {c['iv']} | delta {c['delta']}")

    # ------------------------------------------------------------------ position management
    def _open_legs(self):
        with self._legs_lock:
            return [l for l in self.legs if not l.get('stopped')]

    def _marks(self, leg):
        from api.groww import get_spot_price, get_option_ltp
        spot = get_spot_price(leg['underlying'])
        opt = get_option_ltp(leg['symbol'])
        return (float(spot) if spot else None), (float(opt) if opt else None)

    def _manage_positions(self, now):
        for leg in self._open_legs():
            try:
                self._manage_leg(leg, now)
            except Exception as e:
                logger.error("%s manage %s failed: %s", TAG, leg.get('symbol'), e)
        self._pnl_history.append((now.isoformat(), round(self.pnl, 2)))
        self._pnl_history = self._pnl_history[-500:]

    def _manage_leg(self, leg, now):
        spot, opt = self._marks(leg)
        if opt:
            leg['current_mark'] = round(opt, 2)
            leg['current_pnl'] = round((opt - leg['entry_price']) * leg['qty'], 2)
        if spot:
            leg['stock_mark'] = round(spot, 2)
        if spot and opt:
            self._price_failures = 0
        else:
            self._price_failures += 1
            if self._price_failures in (3, MAX_PRICE_FAILURES):
                print(f"{TAG} ⚠ price poll failed {self._price_failures}x ({leg['underlying']} stock={spot}, option={opt})")

        if now >= self._exit_time(now) or now.date().isoformat() > leg['opened_at'][:10]:
            self._exit(leg, reason='square_off', opt=opt)
            return
        if not spot:
            return
        long_ = leg['type'] == 'call'
        if (spot <= leg['stock_stop']) if long_ else (spot >= leg['stock_stop']):
            self._exit(leg, reason='stop_loss', opt=opt)
        elif (spot >= leg['stock_target']) if long_ else (spot <= leg['stock_target']):
            self._exit(leg, reason='take_profit', opt=opt)

    def _exit(self, leg, reason, opt=None):
        if opt is None:
            try:
                _, opt = self._marks(leg)
            except Exception:
                opt = None
        px = opt or leg.get('current_mark') or leg['entry_price']
        gross = (px - leg['entry_price']) * leg['qty']
        fees = estimate_charges(leg['entry_price'] * leg['qty'], px * leg['qty'])
        net = gross - fees
        now = datetime.now(IST)
        with self._legs_lock:
            leg.update(stopped=True, exit_price=round(px, 2), current_mark=round(px, 2),
                       current_pnl=0.0, exit_reason=reason)
            self.cumulative_pnl += net
            self.total_charges += fees
        move = ((leg.get('stock_mark') or leg['stock_entry']) / leg['stock_entry'] - 1) * 100
        self.trade_log.append({
            'ts': now.isoformat(), 'date': now.strftime('%Y-%m-%d'), 'action': 'SELL',
            'symbol': leg['symbol'], 'underlying': leg['underlying'], 'qty': leg['qty'],
            'price': round(px, 2), 'stock_price': leg.get('stock_mark'), 'gross': round(gross, 2),
            'charges': fees, 'pnl': round(net, 2), 'reason': reason,
        })
        for w in self._watchlist:
            if w['symbol'] == leg['underlying'] and w['status'] == 'entered':
                w['status'] = f'closed ({reason})'
        print(f"{TAG} [PAPER] SELL {leg['symbol']} x{leg['qty']} @ Rs {px:.2f} | {reason} | "
              f"stock {leg['underlying']} {leg.get('stock_mark')} ({move:+.2f}%) | gross Rs {gross:+,.0f} "
              f"- charges Rs {fees:,.0f} = Rs {net:+,.0f} | cumulative Rs {self.cumulative_pnl:+,.0f}")
        self._persist_state()

    # ------------------------------------------------------------------ persistence / threading
    def _apply_thread_context(self):
        from config import set_thread_credentials
        if self._api_key:
            set_thread_credentials(self._api_key, self._api_secret, self._broker)
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
            legs = [dict(l) for l in self._open_legs()]
            details = {**self._base_params, 'trade_log': self.trade_log[-200:],
                       'cumulative_pnl': self.cumulative_pnl, 'total_charges': self.total_charges,
                       'total_trades': self.total_trades, 'total_days_traded': self.total_days_traded,
                       'entered_day': self._entered_day, 'last_scan': self._last_scan,
                       'day': self._day, 'watchlist': self._watchlist, 'entries_today': self._entries_today,
                       'last_candle': self._last_candle}
            update_strategy_db(self._sid, details=details, legs=legs, pnl=round(self.pnl, 2))
        except Exception as e:
            logger.warning("%s persist failed: %s", TAG, e)

    def restore_state(self, details=None, legs=None):
        details = details or {}
        self.cumulative_pnl = float(details.get('cumulative_pnl') or 0)
        self.total_charges = float(details.get('total_charges') or 0)
        self.total_trades = int(details.get('total_trades') or 0)
        self.total_days_traded = int(details.get('total_days_traded') or 0)
        self.trade_log = details.get('trade_log') or []
        self._entered_day = details.get('entered_day')
        self._last_scan = details.get('last_scan')
        # the watchlist and daily counters only carry over within the same trading day
        if details.get('day') == datetime.now(IST).strftime('%Y-%m-%d'):
            self._day = details['day']
            self._watchlist = details.get('watchlist') or []
            self._entries_today = int(details.get('entries_today') or 0)
            self._last_candle = details.get('last_candle')
        with self._legs_lock:
            self.legs = [dict(l) for l in (legs or []) if isinstance(l, dict) and not l.get('stopped')]
        return self

    def status(self):
        """Snapshot for the status endpoint / UI."""
        return {
            'cumulative_pnl': round(self.cumulative_pnl, 2), 'total_pnl': round(self.pnl, 2),
            'total_charges': round(self.total_charges, 2), 'total_trades': self.total_trades,
            'days_traded': self.total_days_traded, 'trade_log': self.trade_log[-20:],
            'legs': [dict(l) for l in self._open_legs()], 'last_scan': self._last_scan,
            'entered_day': self._entered_day, 'watchlist': self._watchlist,
            'entries_today': self._entries_today, 'last_candle': self._last_candle,
            'params': self._base_params,
        }
