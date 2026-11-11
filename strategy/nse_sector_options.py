"""NSE Sector Options — PAPER trades ATM stock options on opening-range breakouts of Sector Scanner picks.

Daily flow (IST, trading days only):
  1. Opening range = the stock's high / low of the first 30 minutes, 09:15-09:45 (5m Yahoo bars).
  2. From 09:45 (entry_hour:entry_minute) until the last entry time, EVERY rescan_sec (default 60 s)
     re-read the live NSE sectors (api.sector_scanner) and rebuild the watchlist: walk the CURRENT
     BULLISH / BEARISH sectors from the biggest |% change| down and take their LONG / SHORT / WATCH
     stocks that have listed stock options (max max_watchlist). Direction = sector bias. Stocks that
     leave the top sectors drop off; new ones join. Stocks already traded today are not re-added.
  3. Every completed 5-minute candle (candle_min, default 5: 09:50, 09:55 ... last entry, default 14:45):
       bullish stock: 5m close > 30-min high  (and > the 5-minute VWAP)  -> paper-BUY the ATM CALL
       bearish stock: 5m close < 30-min low   (and < the 5-minute VWAP)  -> paper-BUY the ATM PUT
     Expiry = nearest with MORE than min_dte (default 10) days left. Price = Groww option-chain LTP.
     LATE JOINERS: a stock that joins the rotating watchlist after 09:50 and is ALREADY beyond its range
     (and the 5m VWAP) on the live price is entered immediately, without waiting for a candle close
     (the Groww spot used for the fill must confirm it).
     Limits: max_open (5) positions at a time, max_entries (6) per day, one entry per stock per day.
  4. Each position exits ONLY on the STOCK price (Groww CASH LTP), checked every monitor_interval s:
       stop   = stock moves stop_pct % against the entry stock price
       target = stock moves target_pct % in favour
     else square-off at the exit time (default 15:15). Paper-SELL at the Groww option LTP.
  5. Estimated Groww F&O charges are deducted from the realized P&L.
  6. Optional DAILY limit on the whole book (day_tp / day_sl, Rs, 0 = off): today's realised net P&L +
     open positions marked at their LTP minus the charges to sell them. When it reaches +day_tp or
     -day_sl, every open position is closed and no more entries are made that day.

Modes (dry_run):
  PAPER (default): no orders; fills at the Groww option LTP.
  LIVE: real Groww MARKET orders, product NRML, quantity = lots x lot size. An entry is recorded only
     after Groww confirms the BUY filled (fill price from the order, else the LTP). An exit marks the
     position closed only after the SELL fills; a failed / rejected SELL keeps it open (exit_pending)
     and is retried every monitor_interval s. Needs a Groww profile with a valid token.
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
    DTE_STRICT = True          # expiry must be MORE than min_dte calendar days away (not equal)

    def __init__(self, entry_hour=9, entry_minute=45, exit_hour=15, exit_minute=15,
                 stop_pct=0.5, target_pct=0.75, lots=1, min_dte=10, max_premium=100000.0,
                 rescan_sec=60, sector_group='Sectoral', trading_days=None,
                 monitor_interval=15, run_immediately=False,
                 max_open=5, max_entries=6, max_watchlist=15, last_entry_hour=14, last_entry_minute=45,
                 use_vwap=True, candle_min=5, dry_run=True, day_tp=0.0, day_sl=0.0):
        self.entry_hour, self.entry_minute = int(entry_hour), int(entry_minute)
        self.exit_hour, self.exit_minute = int(exit_hour), int(exit_minute)
        self.last_entry_hour, self.last_entry_minute = int(last_entry_hour), int(last_entry_minute)
        self.stop_pct, self.target_pct = float(stop_pct), float(target_pct)
        self.lots = max(1, int(lots))
        self.min_dte = int(min_dte)
        self.max_premium = float(max_premium)
        self.rescan_sec = max(30, int(rescan_sec))      # rotate the watchlist this often (default 1 min)
        self.sector_group = sector_group
        self.trading_days = trading_days if trading_days is not None else [0, 1, 2, 3, 4]
        self.monitor_interval = max(5, int(monitor_interval))
        self.run_immediately = bool(run_immediately)
        self.max_open = max(1, int(max_open))
        self.max_entries = max(1, int(max_entries))
        self.max_watchlist = max(1, int(max_watchlist))
        self.use_vwap = bool(use_vwap)
        self.candle_min = int(candle_min) if int(candle_min) in CANDLE_CHOICES else 5
        # LIVE = real Groww MARKET orders (product NRML, quantity in units = lots x lot size)
        self.dry_run = bool(dry_run)
        # daily limits on the whole book, NET of charges (realised today + open positions marked to LTP);
        # 0 = off. On a hit every open position is closed and no more entries are made that day.
        self.day_tp = max(0.0, float(day_tp or 0))
        self.day_sl = max(0.0, float(day_sl or 0))
        self._day_start_pnl = 0.0         # cumulative_pnl when the current day started
        self._day_limit = None            # {'day', 'kind': 'target'|'stop', 'pnl', 'time'} once hit today

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
        self._or_cache = {}               # symbol -> today's 09:15-09:45 range (None if incomplete)
        self._next_scan_ts = 0.0
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
            'rescan_sec': self.rescan_sec, 'sector_group': self.sector_group,
            'trading_days': self.trading_days, 'monitoring_interval': self.monitor_interval,
            'max_open': self.max_open, 'max_entries': self.max_entries,
            'max_watchlist': self.max_watchlist, 'use_vwap': self.use_vwap, 'candle_min': self.candle_min,
            'run_immediately': self.run_immediately, 'dry_run': self.dry_run,
            'day_tp': self.day_tp, 'day_sl': self.day_sl,
        }

    @property
    def _mode(self):
        return 'PAPER' if self.dry_run else 'LIVE'

    # ------------------------------------------------------------------ lifecycle
    def initialize(self):
        self._running = True
        from config import get_api_key, get_api_secret, _thread_local
        self._api_key = get_api_key()
        self._api_secret = get_api_secret()
        self._broker = getattr(_thread_local, 'broker', '')
        days = ','.join(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][d] for d in self.trading_days)
        print(f"{TAG} Started [{self._mode}] | 30-min range 09:15-{self.entry_hour:02d}:{self.entry_minute:02d}, "
              f"{self.candle_min}m breakouts until {self.last_entry_hour:02d}:{self.last_entry_minute:02d}, "
              f"square-off {self.exit_hour:02d}:{self.exit_minute:02d} IST | days {days}")
        print(f"{TAG} Watchlist: up to {self.max_watchlist} {self.sector_group.lower()} LONG/SHORT/WATCH stocks, "
              f"rotated every {self.rescan_sec}s from the current top sectors | "
              f"entry: {self.candle_min}m close beyond the range" + (" + 5m VWAP" if self.use_vwap else "") +
              f" | max {self.max_open} open / {self.max_entries} entries a day | {self.lots} lot ATM CE/PE | "
              f"exit on STOCK: SL -{self.stop_pct}% / TP +{self.target_pct}% | expiry > {self.min_dte}d away"
              + (" | RUN-NOW (test)" if self.run_immediately else ""))
        if self.day_tp or self.day_sl:
            print(f"{TAG} Daily limit on the whole book (net of charges): "
                  + (f"target +Rs {self.day_tp:,.0f}" if self.day_tp else "no target")
                  + " / " + (f"stop -Rs {self.day_sl:,.0f}" if self.day_sl else "no stop")
                  + " -> close everything, no more entries that day")
        if self._broker != 'groww':
            if not self.dry_run:
                print(f"{TAG} ❌ LIVE mode needs a Groww profile (profile broker is '{self._broker}') — not starting")
                self._running = False
                return False
            print(f"{TAG} ⚠ Profile broker is '{self._broker}', not 'groww' — option prices need a Groww profile")
        if not self.dry_run:
            print(f"{TAG} 🔴 LIVE MODE — real Groww MARKET orders (NRML), {self.lots} lot per trade. "
                  f"A failed exit order keeps the position open and is retried every {self.monitor_interval}s.")
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
                self._check_day_limit(now)
                if force or self._entries_allowed(now):
                    if force or time.time() >= self._next_scan_ts:
                        self._next_scan_ts = time.time() + self.rescan_sec
                        self._refresh_watchlist(now, force=force)
                    if self._watchlist:
                        self._check_breakouts(now, force=force)
                first = False
            except Exception as e:
                logger.error("%s cycle failed: %s", TAG, e)
                print(f"{TAG} ⚠ cycle error: {e}")
            self._interruptible_sleep(self._sleep_for(datetime.now(IST)))

    def close_all(self):
        """Manual stop: sell every open option at the current LTP (LIVE: market orders, 3 attempts)."""
        self._running = False
        self._apply_thread_context()          # may be called from a request thread
        for leg in self._open_legs():
            for _ in range(1 if self.dry_run else 3):
                if self._exit(leg, reason='manual_stop'):
                    break
                time.sleep(2)
            else:
                print(f"{TAG} 🚨 {leg['symbol']} x{leg['qty']} could NOT be closed — CLOSE IT MANUALLY in Groww")
        self._persist_state()

    # ------------------------------------------------------------------ daily limit (whole book)
    def day_pnl(self):
        """Today's NET P&L: realised since the day started + open positions marked at their last LTP
        minus the charges a sell now would cost. No network calls."""
        open_net = 0.0
        for l in self._open_legs():
            buy = l['entry_price'] * l['qty']
            sell = (l.get('current_mark') or l['entry_price']) * l['qty']
            open_net += sell - buy - estimate_charges(buy, sell)
        return self.cumulative_pnl - self._day_start_pnl + open_net

    def _check_day_limit(self, now):
        if not (self.day_tp or self.day_sl) or self._day is None:
            return
        hit = self._day_limit if (self._day_limit or {}).get('day') == self._day else None
        if not hit:
            pnl = self.day_pnl()
            kind = ('target' if self.day_tp and pnl >= self.day_tp else
                    'stop' if self.day_sl and pnl <= -self.day_sl else None)
            if not kind:
                return
            hit = self._day_limit = {'day': self._day, 'kind': kind, 'pnl': round(pnl, 2),
                                     'time': now.strftime('%H:%M:%S')}
            limit = self.day_tp if kind == 'target' else -self.day_sl
            print(f"{TAG} {'🎯' if kind == 'target' else '🛑'} DAILY {kind.upper()} hit at {hit['time']}: day P&L "
                  f"Rs {pnl:+,.0f} (limit Rs {limit:+,.0f}) — closing {len(self._open_legs())} position(s), "
                  f"no more entries today")
            self._finish_day(now, f"daily {kind} hit (day P&L Rs {pnl:+,.0f})")
        # close (and in LIVE keep retrying) whatever is still open after the limit fired
        for leg in self._open_legs():
            self._exit(leg, reason=f"day_{hit['kind']}")
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
            self._or_cache = {}
            self._day_start_pnl = self.cumulative_pnl
            self._persist_state()

    def _entries_allowed(self, now):
        if now.weekday() not in self.trading_days or self._entered_day == self._day:
            return False
        # the last candle that may trigger closes at the last-entry time (+ the Yahoo delay)
        if now >= self._last_entry_time(now) + timedelta(seconds=CANDLE_DELAY_S * 3):
            self._finish_day(now, f"entry window closed ({self._entries_today} entries)")
            return False
        return now >= self._entry_time(now)

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
            return min(60, self.rescan_sec)
        return 300

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(5, max(0.1, end - time.time())))

    # ------------------------------------------------------------------ watchlist (rotated every rescan_sec)
    def _refresh_watchlist(self, now, force=False):
        """Rebuild the watchlist from the CURRENT top BULLISH / BEARISH sectors (runs every rescan_sec).

        Stocks that dropped out of the top sectors leave the list; new ones join with their 09:15-09:45
        range. Stocks already traded today stay on the list (for the UI) but are never re-entered."""
        from api.sector_scanner import get_sectors, get_sector_stocks
        from api.groww import get_stock_option_meta
        overview = get_sectors()
        mkt = (overview.get('market') or {}).get('status')
        if mkt != 'Open' and not force:
            self._finish_day(now, f"market status '{mkt}'")
            return
        cands = [s for s in overview['sectors'] if s.get('group') == self.sector_group and s['bias'] != 'MIXED']
        cands.sort(key=lambda s: abs(s['pchange']), reverse=True)
        traded = {w['symbol'] for w in self._watchlist if w['status'] != 'watching'}
        setups = ('LONG', 'SHORT', 'WATCH', 'LATE') if force else ('LONG', 'SHORT', 'WATCH')
        picks, seen, skipped = [], set(traded), []
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

        # the 30-min range never changes during the day -> only fetch candles for stocks not seen yet
        new_syms = [r['symbol'] for _, r in picks if r['symbol'] not in self._or_cache]
        if new_syms:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for sym, (bdate, b) in zip(new_syms, ex.map(fetch, new_syms)):
                    if not b or (bdate != now.date() and not force):
                        continue                                  # not cached -> retried next scan
                    self._or_cache[sym] = opening_range(b, self._entry_time(b[0][0]))
        old = {w['symbol']: w for w in self._watchlist}
        watch = []
        for d, r in picks:
            orng = self._or_cache.get(r['symbol'])
            if not orng:
                skipped.append(f"{r['symbol']} ({'incomplete 30-min range' if r['symbol'] in self._or_cache else 'no candles today'})")
                continue
            prev = old.get(r['symbol'])
            keep = prev if prev and prev['direction'] == r['direction'] else {}
            watch.append({'symbol': r['symbol'], 'direction': r['direction'], 'setup': r['setup'],
                          'sector': d['index'], 'sector_pchg': d['pchange'], 'rvol': r.get('rvol'),
                          'or_high': orng['high'], 'or_low': orng['low'], 'status': 'watching',
                          'last_close': keep.get('last_close'), 'vwap': keep.get('vwap'),
                          'last_candle': keep.get('last_candle'), 'note': keep.get('note'),
                          'added': keep.get('added') or now.strftime('%H:%M'),
                          'ltp': r.get('ltp'), 'scan_vwap': r.get('vwap')})
        added = [w['symbol'] for w in watch if w['symbol'] not in old or old[w['symbol']]['direction'] != w['direction']]
        removed = [s for s, w in old.items() if w['status'] == 'watching' and s not in {x['symbol'] for x in watch}]
        self._watchlist = watch + [w for w in self._watchlist if w['status'] != 'watching']
        top = ', '.join(f"{s['index'].replace('NIFTY ', '')} {s['pchange']:+.2f}%" for s in cands[:4]) or 'none'
        self._last_scan = {**(self._last_scan if (self._last_scan or {}).get('day') == self._day else {}),
                           'time': now.isoformat(), 'day': self._day, 'watchlist': len(watch),
                           'top_sectors': top, 'skipped': skipped[-10:]}
        if added or removed or force:
            print(f"\n{TAG} {now:%H:%M} rescan | top sectors: {top} | watching {len(watch)}"
                  + (f" | + {', '.join(added)}" if added else '') + (f" | − {', '.join(removed)}" if removed else ''))
            if watch:
                print(f"{TAG}   watchlist: " + ' | '.join(
                    f"{w['symbol']} {w['direction']} [{w['or_low']}–{w['or_high']}]" for w in watch))
            elif not cands:
                print(f"{TAG}   no BULLISH/BEARISH {self.sector_group.lower()} sector right now — rescanning every "
                      f"{self.rescan_sec}s")
        if added and not force:
            self._enter_late_joiners(now, [w for w in watch if w['symbol'] in added])
        self._persist_state()

    def _enter_late_joiners(self, now, joiners):
        """A stock that JOINS the watchlist after the first breakout candle could close and is ALREADY beyond
        its 30-min range (and the 5m VWAP) on the live price is entered immediately — no candle-close wait.
        Stocks added before the first breakout candle can close (09:45–09:50, i.e. the opening watchlist)
        keep the normal candle-close rule. A strategy started mid-day treats its first list as late joiners."""
        first_close = self._entry_time(now) + timedelta(minutes=self.candle_min)
        if now < first_close or now > self._last_entry_time(now) + timedelta(minutes=self.candle_min):
            return
        for w in joiners:
            px, vwap = w.get('ltp'), w.get('scan_vwap')
            if not px:
                continue
            long_ = w['direction'] == 'LONG'
            broke = px > w['or_high'] if long_ else px < w['or_low']
            vwap_ok = not self.use_vwap or vwap is None or (px > vwap if long_ else px < vwap)
            if not broke:
                continue
            if not vwap_ok:
                w['note'] = f"{now:%H:%M} joined beyond range but {'below' if long_ else 'above'} 5m VWAP {vwap}"
                continue
            if self._entries_today >= self.max_entries or len(self._open_legs()) >= self.max_open:
                full = (f"max {self.max_entries} entries reached" if self._entries_today >= self.max_entries
                        else f"{self.max_open} positions already open")
                w['note'] = f"{now:%H:%M} joined beyond range — {full}"
                continue
            print(f"{TAG} {now:%H:%M} late joiner {w['symbol']} {w['direction']} already beyond its range "
                  f"(live {px} vs [{w['or_low']}–{w['or_high']}], 5m VWAP {vwap}) — entering now")
            w.update(last_close=px, vwap=vwap)
            self._try_enter(w, {'close': px, 'end': now.strftime('%H:%M')}, vwap, now, live=True)
        if self._entries_today >= self.max_entries:
            self._finish_day(now, f"max {self.max_entries} entries reached")

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
            self._last_candle = key          # nothing to watch right now; the next rescan may add stocks
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
            self._try_enter(w, candle, vwap, now)
        if self._entries_today >= self.max_entries:
            self._finish_day(now, f"max {self.max_entries} entries reached")
        self._persist_state()

    def _try_enter(self, w, candle, vwap, now, live=False):
        """Apply the position / entry limits, pick the contract and paper-buy. Returns True if entered.

        live=True is a late joiner taken on the LIVE price (no candle close): the Groww spot used for the
        fill must itself still be beyond the range (and the VWAP), otherwise the entry is skipped."""
        what = 'live break' if live else 'breakout'
        if self._entries_today >= self.max_entries:
            w['note'] = f"{candle['end']} {what} — max {self.max_entries} entries reached"
            return False
        if len(self._open_legs()) >= self.max_open:
            w['note'] = f"{candle['end']} {what} — {self.max_open} positions already open"
            return False
        contract, why = self._atm_contract(w['symbol'], 'CE' if w['direction'] == 'LONG' else 'PE')
        if not contract:
            w['note'] = f"{candle['end']} {what} — {why}"
            print(f"{TAG}   {w['symbol']} {what} skipped: {why}")
            return False
        if live:
            spot, long_ = contract['spot'], w['direction'] == 'LONG'
            ok = (spot > w['or_high'] if long_ else spot < w['or_low']) and \
                 (not self.use_vwap or vwap is None or (spot > vwap if long_ else spot < vwap))
            if not ok:
                w['note'] = f"{candle['end']} live break not confirmed by Groww spot {spot}"
                print(f"{TAG}   {w['symbol']} live break skipped: Groww spot {spot} not beyond "
                      f"{w['or_high'] if long_ else w['or_low']} / VWAP {vwap}")
                return False
            candle = dict(candle, close=spot)
        if not self.dry_run:
            fill, oid, why = self._live_order(contract['trading_symbol'], contract['qty'], 'BUY',
                                              fallback=contract['ltp'])
            if fill is None:
                w['order_failures'] = w.get('order_failures', 0) + 1
                w['note'] = f"{candle['end']} {what} — LIVE buy failed: {why}"
                print(f"{TAG}   ✗ {w['symbol']} LIVE BUY {contract['trading_symbol']} x{contract['qty']} failed: {why}"
                      + (" — giving up on this stock today" if w['order_failures'] >= 2 else ""))
                if w['order_failures'] >= 2:
                    w['status'] = 'order failed'
                return False
            contract = dict(contract, ltp=fill, order_id=oid)
        self._enter(w, candle, vwap, contract, live=live)
        w['status'] = 'entered'
        self._last_scan = self._last_scan or {'day': self._day}
        self._last_scan.update(stock=w['symbol'], sector=w['sector'], sector_pchg=w['sector_pchg'],
                               setup=w['direction'], time=now.isoformat())
        return True

    def _atm_contract(self, symbol, kind):
        """Pick the ATM contract on the nearest expiry with >= min_dte days. Returns (contract, reason)."""
        from api.groww import get_stock_option_meta, get_groww_chain
        meta = get_stock_option_meta(symbol)
        if not meta:
            return None, 'no stock options'
        today = datetime.now(IST).date()
        # NSE Sector Options: expiry MORE than min_dte days away (user rule); subclasses may use >=
        exps = [e for e in meta['expiries']
                if ((datetime.strptime(e, '%Y-%m-%d').date() - today).days > self.min_dte if self.DTE_STRICT
                    else (datetime.strptime(e, '%Y-%m-%d').date() - today).days >= self.min_dte)]
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

    def _enter(self, w, candle, vwap, c, live=False):
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
            'entry_trigger': 'late_join_live' if live else f'{self.candle_min}m_close',
            'sector': w['sector'], 'sector_pchg': w['sector_pchg'],
            'opened_at': now.isoformat(), 'stopped': False,
            'mode': self._mode, 'entry_order_id': c.get('order_id'),
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
            'reason': f"{w['direction']} 30-min {'high' if side == 1 else 'low'} {level} break @ {candle['end']}"
                      + (' (late joiner, live price)' if live else ''),
        })
        how = 'LIVE price' if live else f"{self.candle_min}m close"
        fill_txt = f" (filled, order {c['order_id']})" if c.get('order_id') else ''
        print(f"{TAG} [{self._mode}] BUY {c['trading_symbol']} x{c['qty']} ({self.lots} lot) @ Rs {c['ltp']:.2f}{fill_txt} "
              f"= Rs {c['ltp'] * c['qty']:,.0f} | {w['direction']} {w['symbol']} {how} {candle['close']:.2f} "
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

        if leg.get('exit_pending'):                 # LIVE exit order failed earlier -> keep retrying
            self._exit(leg, reason=leg['exit_pending'], opt=opt)
            return

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
        """Close one position. PAPER: at the option LTP. LIVE: SELL MARKET order; the position is only
        marked closed once Groww confirms the fill — otherwise it stays open with exit_pending set and
        is retried every cycle. Returns True when closed."""
        if not self.dry_run:
            fill, oid, why = self._live_order(leg['symbol'], leg['qty'], 'SELL',
                                              fallback=opt or leg.get('current_mark') or leg['entry_price'])
            if fill is None:
                leg['exit_pending'] = reason
                leg['exit_failures'] = leg.get('exit_failures', 0) + 1
                n = leg['exit_failures']
                if n in (1, 3) or n % 20 == 0:
                    print(f"{TAG} 🚨 LIVE SELL {leg['symbol']} x{leg['qty']} ({reason}) failed #{n}: {why} — "
                          f"position still OPEN, retrying every {self.monitor_interval}s"
                          + (" — CHECK GROWW MANUALLY" if n >= 3 else ""))
                self._persist_state()
                return False
            px, exit_oid = fill, oid
        else:
            if opt is None:
                try:
                    _, opt = self._marks(leg)
                except Exception:
                    opt = None
            px, exit_oid = opt or leg.get('current_mark') or leg['entry_price'], None
        gross = (px - leg['entry_price']) * leg['qty']
        fees = estimate_charges(leg['entry_price'] * leg['qty'], px * leg['qty'])
        net = gross - fees
        now = datetime.now(IST)
        with self._legs_lock:
            leg.update(stopped=True, exit_price=round(px, 2), current_mark=round(px, 2),
                       current_pnl=0.0, exit_reason=reason, exit_order_id=exit_oid, exit_pending=None)
            self.cumulative_pnl += net
            self.total_charges += fees
        move = ((leg.get('stock_mark') or leg['stock_entry']) / leg['stock_entry'] - 1) * 100
        self.trade_log.append({
            'ts': now.isoformat(), 'date': now.strftime('%Y-%m-%d'), 'action': 'SELL',
            'symbol': leg['symbol'], 'underlying': leg['underlying'], 'qty': leg['qty'],
            'price': round(px, 2), 'stock_price': leg.get('stock_mark'), 'gross': round(gross, 2),
            'charges': fees, 'pnl': round(net, 2), 'reason': reason, 'mode': self._mode,
        })
        for w in self._watchlist:
            if w['symbol'] == leg['underlying'] and w['status'] == 'entered':
                w['status'] = f'closed ({reason})'
        print(f"{TAG} [{self._mode}] SELL {leg['symbol']} x{leg['qty']} @ Rs {px:.2f} | {reason} | "
              f"stock {leg['underlying']} {leg.get('stock_mark')} ({move:+.2f}%) | gross Rs {gross:+,.0f} "
              f"- charges Rs {fees:,.0f} = Rs {net:+,.0f} | cumulative Rs {self.cumulative_pnl:+,.0f}")
        self._persist_state()
        return True

    # ------------------------------------------------------------------ LIVE orders (Groww)
    def _live_order(self, trading_symbol, qty, side, fallback=None):
        """Place a Groww MARKET order (NRML, quantity in units) and wait for the fill.

        Returns (fill_price, order_id, None) on a confirmed fill, else (None, order_id, reason).
        A pending order that times out is cancelled; if the cancel loses the race and the order filled
        anyway, the fill is still returned so the position is never lost track of."""
        from api.groww import place_order, confirm_order_filled, cancel_order, get_order_status, get_option_ltp
        resp = place_order(trading_symbol=trading_symbol, quantity=int(qty), transaction_type=side,
                           order_type='MARKET', product='NRML')
        if not isinstance(resp, dict) or resp.get('error'):
            return None, None, (resp.get('error') if isinstance(resp, dict) else str(resp))
        oid = resp.get('groww_order_id') or resp.get('growwOrderId')
        filled, status = confirm_order_filled(oid)
        last = None
        if not filled and oid and str(status).startswith('timeout'):
            try:
                cancel_order(oid)
            except Exception:
                pass
            last = get_order_status(oid)
            st = str((last or {}).get('order_status') or (last or {}).get('orderStatus') or '').upper()
            filled = st in ('EXECUTED', 'COMPLETED')
            status = st or status
        if not filled:
            return None, oid, f"order {oid} {status}"
        if last is None:
            last = get_order_status(oid)
        px = None
        for src in (last, resp):
            if isinstance(src, dict):
                for k in ('average_fill_price', 'avg_fill_price', 'average_price', 'filled_price', 'price'):
                    try:
                        v = float(src.get(k) or 0)
                    except (TypeError, ValueError):
                        v = 0
                    if v > 0:
                        px = v
                        break
            if px:
                break
        if not px:                                    # fill price not in the response -> use the LTP now
            try:
                px = float(get_option_ltp(trading_symbol) or 0) or None
            except Exception:
                px = None
        if not px:
            px = float(fallback or 0)
            print(f"{TAG} ⚠ {side} {trading_symbol} filled (order {oid}) but Groww returned no fill price/LTP — "
                  f"recorded at Rs {px:.2f} (last known); check the real price in Groww")
        return px, oid, None

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
                       'last_candle': self._last_candle, 'day_start_pnl': self._day_start_pnl,
                       'day_limit': self._day_limit}
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
            self._day_start_pnl = float(details.get('day_start_pnl') or 0)
            self._day_limit = details.get('day_limit')
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
            'params': self._base_params, 'mode': self._mode,
            'day_pnl': round(self.day_pnl(), 2), 'day_tp': self.day_tp, 'day_sl': self.day_sl,
            'day_limit': self._day_limit if (self._day_limit or {}).get('day') == self._day else None,
        }
