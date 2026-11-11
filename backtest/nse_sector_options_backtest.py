"""Backtest of the LIVE NSE Sector Options strategy (strategy/nse_sector_options.py).

The real strategy class and the real Sector Scanner functions make every decision; only the data
sources are replaced with historical ones:

  live source                                   backtest replacement
  ------------------------------------------    ---------------------------------------------------------
  Yahoo 5m candles (scanner + strategy)         cached Yahoo 5m bars (.nse_cache/sector_bt/yahoo, ~60 days),
                                                fed to the REAL api.sector_scanner._intraday through a fake
                                                HTTP session, and to the strategy's session_bars
  NSE allIndices / constituents (sector %,      official sector-index 5m bars for the % change; breadth from
  breadth, stock LTP / % change)                the constituents' % change (constituents = 2 Nov 2026 lists)
  NSE OI spurts / bulk deals / news             not available historically -> OI check = n/a, no deals
  Groww instruments (lot size, expiries)        real NSE F&O bhavcopy of that day
  Groww option chain / option LTP               Black-Scholes on the 5m stock price, IV solved from the real
                                                bhavcopy closes (prev-day close IV drifting to the day-close IV)
  Groww stock LTP for SL/TP                     5m bar high/low: stop or target filled at the level, SL first
                                                if both are inside one bar; square-off at the 15:15 price

Timing: the scanner + watchlist rotation + breakout check run once per 5-minute bar (live: rescan every
minute, candle check every 5 min), with the clock at bar end + 61 s like the live candle check. A
late joiner's "live price" is therefore the close of the bar that just ended.

Usage:  venv/bin/python backtest/nse_sector_options_backtest.py [--max-open 5] [--max-entries 6]
            [--stop-pct 0.5] [--target-pct 0.75] [--stop-mode pct|vwap] [--no-vwap] [--candle 5] [--log PATH]
  --stop-mode vwap: no % stop; exit when a 5m candle CLOSES below the running 5m VWAP (calls) / above (puts).
Every run overwrites .nse_cache/sector_bt/trades_nse_sector_options_orb.csv (copy it per variant).
"""

import argparse
import bisect
import contextlib
import csv
import io
import json
import math
import os
import pickle
import sys
import types
from datetime import date, datetime, timedelta
from urllib.parse import unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import api.sector_scanner as ss                      # noqa: E402
import strategy.nse_sector_options as so             # noqa: E402
from backtest.nse_bhavcopy import download_bhavcopy  # noqa: E402

BT_DIR = os.path.join(ROOT, '.nse_cache', 'sector_bt')
YDIR = os.path.join(BT_DIR, 'yahoo')
STAMP = '2026-11-02'
IST = so.IST
RF = 0.065                                            # risk-free rate for Black-Scholes
IDX_CNX = {'NIFTY AUTO': 'IDX_CNXAUTO', 'NIFTY FMCG': 'IDX_CNXFMCG', 'NIFTY IT': 'IDX_CNXIT',
           'NIFTY MEDIA': 'IDX_CNXMEDIA', 'NIFTY METAL': 'IDX_CNXMETAL', 'NIFTY PHARMA': 'IDX_CNXPHARMA',
           'NIFTY PSU BANK': 'IDX_CNXPSUBANK', 'NIFTY REALTY': 'IDX_CNXREALTY'}
RVOL_DAYS = 4                                         # live _intraday fetches range=5d -> up to 4 prior sessions


# =========================================================================== clock
class Clock:
    now = None


class FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return Clock.now


def at(d, minute, sec=0):
    return datetime(d.year, d.month, d.day, tzinfo=IST) + timedelta(minutes=minute, seconds=sec)


# =========================================================================== data
def load_json(name):
    with open(os.path.join(YDIR, f'{STAMP}_{name}.json')) as f:
        return json.load(f)


def load_data():
    with open(os.path.join(BT_DIR, f'universe_{STAMP}.json')) as f:
        uni = json.load(f)
    idx = {}
    for name, v in uni.items():
        fname = IDX_CNX.get(name) or v['code'].replace(' ', '_') + '.NS'
        idx[name] = load_json(fname)
    stocks = {}
    for sym in sorted({s for v in uni.values() for s in v['stocks']}):
        try:
            stocks[sym] = load_json(f'{sym}.NS')
        except FileNotFoundError:
            pass
    return uni, idx, stocks


class Bars:
    """Per (symbol, day): bars [[minute, o, h, l, c, v], ...] with a start-minute index for slicing."""

    def __init__(self, raw):
        self.raw = raw                                     # {day_str: [[m,o,h,l,c,v], ...]}
        self.mins = {d: [b[0] for b in bars] for d, bars in raw.items()}

    def upto(self, d, minute):
        """Completed bars of day d whose bar ENDS at or before `minute`."""
        bars = self.raw.get(d)
        if not bars:
            return []
        return bars[:bisect.bisect_right(self.mins[d], minute - 5)]

    def bar(self, d, start):
        bars = self.raw.get(d)
        if not bars:
            return None
        i = bisect.bisect_left(self.mins[d], start)
        return bars[i] if i < len(bars) and bars[i][0] == start else None

    def last_close(self, d, minute):
        b = self.upto(d, minute)
        return b[-1][4] if b else None

    def day_close(self, d):
        bars = self.raw.get(d)
        return bars[-1][4] if bars else None


# =========================================================================== bhavcopy / option pricing
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T, sig, kind):
    if T <= 0 or sig <= 0:
        return max(0.0, S - K) if kind == 'CE' else max(0.0, K - S)
    sq = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (RF + sig * sig / 2) * T) / sq
    d2 = d1 - sq
    if kind == 'CE':
        return S * norm_cdf(d1) - K * math.exp(-RF * T) * norm_cdf(d2)
    return K * math.exp(-RF * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def implied_vol(price, S, K, T, kind):
    if not price or price <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, S - K) if kind == 'CE' else max(0.0, K - S)
    if price <= intrinsic + 1e-6:
        return None
    lo, hi = 0.01, 4.0
    if bs_price(S, K, T, hi, kind) < price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, mid, kind) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def years_to_expiry(now, expiry):
    exp = datetime.strptime(expiry, '%Y-%m-%d').replace(hour=15, minute=30, tzinfo=IST)
    return max((exp - now).total_seconds(), 0) / (365 * 86400)


def load_bhav(day_str):
    """{symbol: {'lot': int, 'rows': {(expiry, kind, strike): (close, underlying, tsym)}}} for stock options."""
    ymd = day_str.replace('-', '')
    pk = os.path.join(BT_DIR, f'bhav_sto_{ymd}.pkl')
    if os.path.exists(pk):
        with open(pk, 'rb') as f:
            return pickle.load(f)
    raw = download_bhavcopy(ymd)
    if not raw:
        return None
    out = {}
    for r in csv.DictReader(io.StringIO(raw)):
        if r.get('FinInstrmTp') != 'STO':
            continue
        sym = r['TckrSymb']
        e = out.setdefault(sym, {'lot': int(float(r.get('NewBrdLotQty') or 0)), 'rows': {}})
        try:
            close = float(r.get('ClsPric') or 0) or float(r.get('SttlmPric') or 0)
            und = float(r.get('UndrlygPric') or 0)
            strike = float(r['StrkPric'])
        except ValueError:
            continue
        e['rows'][(r['XpryDt'], r['OptnTp'], strike)] = (close, und, r.get('FinInstrmNm'))
    with open(pk, 'wb') as f:
        pickle.dump(out, f)
    return out


class OptionBook:
    """IV per contract for one trading day: prev-day close IV -> day-close IV, linear over 09:15-15:30."""

    def __init__(self, day, bhav_today, bhav_prev, prev_day=None):
        self.day, self.prev_day = day, prev_day
        self.today, self.prev = bhav_today, bhav_prev or {}
        self._iv = {}
        self.by_tsym = {}
        for sym, e in bhav_today.items():
            for key, (_, _, tsym) in e['rows'].items():
                if tsym:
                    self.by_tsym[tsym] = (sym, key)

    def ivs(self, sym, key):
        if (sym, key) in self._iv:
            return self._iv[(sym, key)]
        exp, kind, strike = key
        close_dt = at(self.day, 15 * 60 + 30)
        iv_close = iv_prev = None
        r = self.today.get(sym, {}).get('rows', {}).get(key)
        if r and r[1]:
            iv_close = implied_vol(r[0], r[1], strike, years_to_expiry(close_dt, exp), kind)
        p = self.prev.get(sym, {}).get('rows', {}).get(key)
        if p and p[1] and self.prev_day:
            prev_dt = at(self.prev_day, 15 * 60 + 30)
            iv_prev = implied_vol(p[0], p[1], strike, years_to_expiry(prev_dt, exp), kind)
        self._iv[(sym, key)] = (iv_prev, iv_close)
        return iv_prev, iv_close

    def iv_at(self, sym, key, now, mode='drift'):
        iv_prev, iv_close = self.ivs(sym, key)
        if mode == 'prev':
            return iv_prev or iv_close
        if iv_prev and iv_close:
            frac = min(max(((now.hour * 60 + now.minute) - 555) / 375, 0), 1)
            return iv_prev + (iv_close - iv_prev) * frac
        return iv_close or iv_prev

    def price(self, sym, key, spot, now, mode='drift'):
        iv = self.iv_at(sym, key, now, mode)
        if not iv or not spot:
            return None, None
        px = bs_price(spot, key[2], years_to_expiry(now, key[0]), iv, key[1])
        return round(max(px, 0.05) * 20) / 20, iv              # 0.05 tick, never 0


# =========================================================================== fakes for the live code
class World:
    """Holds the historical state the fake data sources read from."""

    def __init__(self, uni, idx, stocks):
        self.uni = uni
        self.idx = {k: Bars(v) for k, v in idx.items()}
        self.stocks = {k: Bars(v) for k, v in stocks.items()}
        self.days = sorted(d for d in stocks['RELIANCE'] if d < STAMP)   # 11-02 is a partial day
        self.day = None                # date
        self.dstr = None
        self.prev = None               # previous trading day str
        self.minute = None             # bar-end minute of the current step
        self.book = None
        self.books = {}                # day str -> OptionBook (for the post-hoc re-pricing)
        self.spot_override = {}
        self._intra = {}

    # ---- per-step
    def set_step(self, minute):
        self.minute = minute
        self._intra = {}

    def spot(self, sym):
        if sym in self.spot_override:
            return self.spot_override[sym]
        b = self.stocks.get(sym)
        px = b.last_close(self.dstr, self.minute) if b else None
        return round(px, 2) if px else None                 # NSE prices have 2 decimals (Yahoo floats don't)

    def vwap(self, sym, minute):
        """Session VWAP from the completed 5m bars up to `minute` (same as the strategy's candle_and_vwap)."""
        bars = self.stocks[sym].upto(self.dstr, minute)
        vol = sum(b[5] for b in bars)
        return sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in bars) / vol if vol else None

    def pchg(self, bars, minute=None):
        last = bars.last_close(self.dstr, minute or self.minute)
        prev = bars.day_close(self.prev)
        return (last / prev - 1) * 100 if last and prev else None

    # ---- Yahoo chart JSON for the REAL ss._intraday
    def yahoo_json(self, sym):
        b = self.stocks.get(sym)
        if not b:
            return {'chart': {'result': []}}
        i = self.days.index(self.dstr)
        days = [d for d in self.days[max(0, i - RVOL_DAYS):i] if d in b.raw]
        ts, o, h, l, c, v = [], [], [], [], [], []
        for d in days + [self.dstr]:
            bars = b.upto(d, self.minute) if d == self.dstr else b.raw[d]
            dd = date.fromisoformat(d)
            for m, *ohlcv in bars:
                ts.append(int(at(dd, m).timestamp()))
                o.append(ohlcv[0]); h.append(ohlcv[1]); l.append(ohlcv[2]); c.append(ohlcv[3]); v.append(ohlcv[4])
        return {'chart': {'result': [{'timestamp': ts, 'indicators': {'quote': [
            {'open': o, 'high': h, 'low': l, 'close': c, 'volume': v}]}}]}}

    def intraday(self, sym):
        if sym not in self._intra:
            try:
                self._intra[sym] = ss._intraday(sym)
            except Exception:
                self._intra[sym] = None
        return self._intra[sym]

    # ---- api.sector_scanner replacements
    def sector_row(self, name):
        pchg = self.pchg(self.idx[name])
        adv = dec = 0
        for s in self.uni[name]['stocks']:
            if s in self.stocks:
                p = self.pchg(self.stocks[s])
                if p is not None:
                    adv += p > 0
                    dec += p < 0
        breadth = ss._breadth(adv, dec)
        pchg = round(pchg, 2) if pchg is not None else 0.0
        return {'index': name, 'group': 'Sectoral', 'pchange': pchg, 'advances': adv, 'declines': dec,
                'breadth': breadth, 'bias': ss._bias(pchg, breadth)}

    def get_sectors(self):
        return {'sectors': [self.sector_row(n) for n in self.uni], 'market': {'status': 'Open'}}

    def get_sector_stocks(self, index):
        sec = self.sector_row(index)
        late = Clock.now.time() >= ss.LATE_ENTRY
        rows = []
        for s in self.uni[index]['stocks']:
            b = self.stocks.get(s)
            if not b or self.dstr not in b.raw:
                continue
            ltp, prev = self.spot(s), b.day_close(self.prev)
            if not ltp or not prev:
                continue
            today = b.upto(self.dstr, self.minute)
            stock = {'symbol': s, 'companyName': s, 'lastPrice': ltp, 'pChange': round((ltp / prev - 1) * 100, 2),
                     'open': today[0][1] if today else ltp, 'dayHigh': max(x[2] for x in today) if today else ltp,
                     'dayLow': min(x[3] for x in today) if today else ltp, 'previousClose': prev,
                     'totalTradedVolume': sum(x[5] for x in today), 'totalTradedValue': 0}
            rows.append(ss._analyse(stock, sec['pchange'], sec['bias'], None, {}, self.intraday(s), late))
        rows.sort(key=lambda r: (r['setup'] in ('LONG', 'SHORT', 'LATE'), r['score'],
                                 r['rvol'] or 0, abs(r['rel_to_sector'])), reverse=True)
        return {'index': index, 'pchange': sec['pchange'], 'bias': sec['bias'], 'stocks': rows}

    # ---- strategy.session_bars replacement
    def session_bars(self, sym):
        b = self.stocks.get(sym)
        if not b:
            return None, []
        bars = b.upto(self.dstr, self.minute)
        return self.day, [(at(self.day, m), o, h, l, c, v) for m, o, h, l, c, v in bars]

    # ---- api.groww replacements
    def option_meta(self, sym):
        e = self.book.today.get(sym)
        if not e or not e['lot']:
            return None
        rows = e['rows']
        return {'lot_size': e['lot'], 'expiries': sorted({k[0] for k in rows}),
                'contracts': {k: v[2] for k, v in rows.items()}}

    def groww_chain(self, sym, expiry_ddmmyyyy):
        exp = datetime.strptime(expiry_ddmmyyyy, '%d-%m-%Y').strftime('%Y-%m-%d')
        e = self.book.today.get(sym)
        spot = self.spot(sym)
        if not e or not spot:
            return None, None, None
        strikes = {}
        for (x, kind, k), (close, _, tsym) in e['rows'].items():
            if x != exp or close <= 0:
                continue
            px, iv = self.book.price(sym, (x, kind, k), spot, Clock.now)
            if px:
                strikes.setdefault(k, {'strike': k, 'call': None, 'put': None})[
                    'call' if kind == 'CE' else 'put'] = {'mark_price': px, 'trading_symbol': tsym,
                                                           'iv': round(iv * 100, 1), 'delta': None, 'oi': None}
        return sorted(strikes.values(), key=lambda r: r['strike']), spot, None

    def option_ltp(self, tsym):
        hit = self.book.by_tsym.get(tsym)
        if not hit:
            return None
        sym, key = hit
        return self.book.price(sym, key, self.spot(sym), Clock.now)[0]


TRADES = []
DAY_LOCK = {}          # day -> (time, 'day_target'|'day_stop', day P&L) when the daily limit fired


class BTStrategy(so.NseSectorOptions):
    """The live class, with every closed position recorded for the report."""

    def _exit(self, leg, reason, opt=None):
        super()._exit(leg, reason, opt=opt)
        sell = self.trade_log[-1]
        TRADES.append({**{k: leg.get(k) for k in (
            'underlying', 'symbol', 'type', 'strike', 'expiry', 'direction', 'qty', 'lot_size', 'entry_price',
            'exit_price', 'stock_entry', 'stock_stop', 'stock_target', 'or_high', 'or_low', 'sector',
            'sector_pchg', 'opened_at', 'entry_trigger', 'exit_reason')},
            'stock_exit': leg.get('stock_mark'), 'closed_at': Clock.now.isoformat(),
            'gross': sell['gross'], 'charges': sell['charges'], 'net': sell['pnl']})


def install(world):
    ss._yahoo_session = lambda: (types.SimpleNamespace(get=lambda url, params=None, timeout=None: types.SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda u=url: world.yahoo_json(unquote(u.split('/chart/')[1].rsplit('.NS', 1)[0])))), 'x')
    ss.datetime = FakeDT
    so.datetime = FakeDT
    ss.get_sectors = world.get_sectors
    ss.get_sector_stocks = world.get_sector_stocks
    so.session_bars = world.session_bars
    gw = types.ModuleType('api.groww')
    gw.get_stock_option_meta = world.option_meta
    gw.get_groww_chain = world.groww_chain
    gw.get_spot_price = world.spot
    gw.get_option_ltp = world.option_ltp
    sys.modules['api.groww'] = gw


# =========================================================================== engine
def _exit_at(s, world, leg, level, reason):
    world.spot_override[leg['underlying']] = level
    leg['stock_mark'] = level
    s._exit(leg, reason, opt=world.option_ltp(leg['symbol']))
    world.spot_override.pop(leg['underlying'], None)


def run(args, world):
    # vwap stop mode: no % stop (50% is never reached intraday)
    s = BTStrategy(stop_pct=50.0 if args.stop_mode == 'vwap' else args.stop_pct, target_pct=args.target_pct,
                   max_open=args.max_open, max_entries=args.max_entries, use_vwap=not args.no_vwap,
                   candle_min=args.candle, max_watchlist=args.max_watchlist)
    s._running = True
    days = world.days[RVOL_DAYS:]
    prev_bhav = None
    for i, dstr in enumerate(world.days):
        bh = load_bhav(dstr)
        if dstr not in days:
            prev_bhav = bh
            continue
        if not bh:
            print(f"!! no bhavcopy for {dstr}, day skipped", file=sys.stderr)
            continue
        world.day, world.dstr, world.prev = date.fromisoformat(dstr), dstr, world.days[i - 1]
        world.book = OptionBook(world.day, bh, prev_bhav, date.fromisoformat(world.prev))
        world.books[dstr] = world.book
        prev_bhav = bh
        world.spot_override = {}
        for minute in range(9 * 60 + 45, 15 * 60 + 20, 5):        # bar ends 09:45 .. 15:15
            world.set_step(minute)
            # 1) exits on the bar that just ended (positions opened before it)
            Clock.now = at(world.day, minute)
            for leg in s._open_legs():
                if leg['opened_at'] >= at(world.day, minute).isoformat():
                    continue
                bar = world.stocks[leg['underlying']].bar(dstr, minute - 5)
                if not bar:
                    continue
                long_ = leg['type'] == 'call'
                hit_tp = bar[2] >= leg['stock_target'] if long_ else bar[3] <= leg['stock_target']
                if args.stop_mode == 'vwap':
                    # target intrabar first; otherwise exit at the close if it closed on the wrong side of VWAP
                    vwap = world.vwap(leg['underlying'], minute)
                    if hit_tp:
                        _exit_at(s, world, leg, leg['stock_target'], 'take_profit')
                    elif vwap and ((bar[4] < vwap) if long_ else (bar[4] > vwap)):
                        _exit_at(s, world, leg, round(bar[4], 2), 'vwap_stop')
                    continue
                hit_sl = bar[3] <= leg['stock_stop'] if long_ else bar[2] >= leg['stock_stop']
                if hit_sl or hit_tp:                                # SL first if both are inside the bar
                    _exit_at(s, world, leg, leg['stock_stop'] if hit_sl else leg['stock_target'],
                             'stop_loss' if hit_sl else 'take_profit')
            # 1b) daily portfolio target / stop (net of charges): realised today + open positions marked
            #     at this bar's close (option re-priced at the stock close, minus the exit charges)
            if (args.day_tp or args.day_sl) and dstr not in DAY_LOCK:
                realised = sum(t['net'] for t in TRADES if t['closed_at'][:10] == dstr)
                open_mtm = 0.0
                marks = []
                for leg in s._open_legs():
                    opt = world.option_ltp(leg['symbol'])
                    if opt is None:
                        continue
                    buy, sell = leg['entry_price'] * leg['qty'], opt * leg['qty']
                    open_mtm += sell - buy - so.estimate_charges(buy, sell)
                    marks.append((leg, opt))
                day_pnl = realised + open_mtm
                hit = ('day_target' if args.day_tp and day_pnl >= args.day_tp else
                       'day_stop' if args.day_sl and day_pnl <= -args.day_sl else None)
                if hit:
                    for leg, opt in marks:
                        leg['stock_mark'] = world.spot(leg['underlying'])
                        s._exit(leg, hit, opt=opt)
                    DAY_LOCK[dstr] = (f"{minute // 60:02d}:{minute % 60:02d}", hit, round(day_pnl))
                    s._finish_day(Clock.now, f"daily {'target' if hit == 'day_target' else 'stop'} "
                                             f"hit (day P&L Rs {day_pnl:+,.0f})")
            # 2) the live loop, with the clock 61 s after the bar closed
            Clock.now = at(world.day, minute, 61)
            s._roll_day(Clock.now)
            if minute >= s.exit_hour * 60 + s.exit_minute:
                for leg in s._open_legs():
                    leg['stock_mark'] = round(world.spot(leg['underlying']), 2)
                s._manage_positions(Clock.now)                     # square-off at the 15:15 price
                continue
            if s._entries_allowed(Clock.now):
                s._refresh_watchlist(Clock.now)
                if s._watchlist:
                    s._check_breakouts(Clock.now)
        if s._open_legs():
            print(f"!! {dstr} positions still open after the day", file=sys.stderr)
    return s, days


# =========================================================================== report
def equity_charges(buy, sell):
    """Groww intraday equity charges for one round trip (Rs)."""
    brokerage = min(20, 0.001 * buy) + min(20, 0.001 * sell)
    txn = 0.0000297 * (buy + sell)
    sebi = 0.000001 * (buy + sell)
    stt = 0.00025 * sell
    stamp = 0.00003 * buy
    return round(brokerage + txn + sebi + stt + stamp + 0.18 * (brokerage + txn + sebi), 2)


def reprice(books, t, mode, slip):
    """Re-price one trade's option under another IV mode / slippage. Returns net P&L."""
    book = books[t['opened_at'][:10]]
    key = (t['expiry'], 'CE' if t['type'] == 'call' else 'PE', float(t['strike']))
    tin, tout = datetime.fromisoformat(t['opened_at']), datetime.fromisoformat(t['closed_at'])
    pin = book.price(t['underlying'], key, t['stock_entry'], tin, mode)[0]
    pout = book.price(t['underlying'], key, t['stock_exit'], tout, mode)[0]
    if pin is None or pout is None:
        return None
    buy, sell = pin * (1 + slip) * t['qty'], pout * (1 - slip) * t['qty']
    return sell - buy - so.estimate_charges(buy, sell)


def stats(nets):
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    pf = sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float('inf')
    return {'n': len(nets), 'win': len(wins) / len(nets) * 100 if nets else 0, 'net': sum(nets), 'pf': pf, 'dd': dd,
            'avg_win': sum(wins) / len(wins) if wins else 0, 'avg_loss': sum(losses) / len(losses) if losses else 0}


def fmt(x):
    return f"{x:+,.0f}"


def report(args, days, books):
    T = sorted(TRADES, key=lambda t: t['closed_at'])
    stop_txt = 'stop = 5m close beyond VWAP' if args.stop_mode == 'vwap' else f"stop {args.stop_pct}%"
    print("\nNSE Sector Options — backtest of the live strategy code")
    print(f"{days[0]} .. {days[-1]}  ({len(days)} sessions)  |  {stop_txt} / target {args.target_pct}% on the "
          f"STOCK, square-off 15:15  |  max {args.max_open} open, {args.max_entries} entries/day  |  "
          f"{args.candle}m close + {'5m VWAP' if not args.no_vwap else 'no VWAP'}  |  1 lot ATM, expiry > 10 days"
          + (f"  |  DAILY target Rs {args.day_tp:,.0f} / stop Rs {args.day_sl:,.0f} (net, checked every 5m bar close)"
             if (args.day_tp or args.day_sl) else ''))
    if not T:
        print("No trades.")
        return
    nets = [t['net'] for t in T]
    st = stats(nets)
    gross, fees = sum(t['gross'] for t in T), sum(t['charges'] for t in T)
    days_traded = len({t['opened_at'][:10] for t in T})
    events = sorted([(t['opened_at'], t['entry_price'] * t['qty']) for t in T] +
                    [(t['closed_at'], -t['entry_price'] * t['qty']) for t in T])
    cur = peak_cap = 0.0
    for _, v in events:
        cur += v
        peak_cap = max(peak_cap, cur)
    print("\nSUMMARY (option, paper fills at the modelled LTP)")
    print("```")
    print(f"Trades              {st['n']}  on {days_traded} of {len(days)} days  (avg {st['n'] / len(days):.1f}/day)")
    print(f"Win rate            {st['win']:.1f}%")
    print(f"Gross P&L           Rs {fmt(gross)}")
    print(f"Charges             Rs {fees:,.0f}  (Rs {fees / st['n']:.0f}/trade)")
    print(f"Net P&L             Rs {fmt(st['net'])}")
    print(f"Profit factor       {st['pf']:.2f}")
    print(f"Avg win / avg loss  Rs {fmt(st['avg_win'])} / Rs {fmt(st['avg_loss'])}")
    print(f"Max drawdown        Rs {st['dd']:,.0f}")
    print(f"Peak premium held   Rs {peak_cap:,.0f}  (capital needed for up to {args.max_open} positions at once)")
    print("```")

    print("\nMONTHLY (Net = after charges; Cumulative = running total)")
    print("```")
    print(f"{'Month':8} | {'Trades':>6} | {'Win%':>5} | {'Gross':>9} | {'Charges':>8} | {'Net':>9} | {'Cumulative':>10}")
    cum = 0.0
    for m in sorted({t['opened_at'][:7] for t in T}):
        mt = [t for t in T if t['opened_at'][:7] == m]
        n = sum(t['net'] for t in mt)
        cum += n
        print(f"{m:8} | {len(mt):6} | {sum(t['net'] > 0 for t in mt) / len(mt) * 100:5.1f} | "
              f"{fmt(sum(t['gross'] for t in mt)):>9} | {sum(t['charges'] for t in mt):8,.0f} | {fmt(n):>9} | {fmt(cum):>10}")
    print("```")

    print("\nDAILY (Net per session; Limit = daily target/stop that closed everything, with the time)")
    print("```")
    print(f"{'Day':10} | {'Trades':>6} | {'Net':>9} | {'Cumulative':>10} | Limit")
    cum = 0.0
    for d in days:
        dt = [t for t in T if t['opened_at'][:10] == d]
        n = sum(t['net'] for t in dt)
        cum += n
        lim = DAY_LOCK.get(d)
        lim_txt = f"{'TARGET' if lim[1] == 'day_target' else 'STOP'} @ {lim[0]} (Rs {lim[2]:+,})" if lim else ''
        print(f"{d:10} | {len(dt):6} | {fmt(n):>9} | {fmt(cum):>10} | {lim_txt}")
    if args.day_tp or args.day_sl:
        nt = sum(1 for v in DAY_LOCK.values() if v[1] == 'day_target')
        ns = sum(1 for v in DAY_LOCK.values() if v[1] == 'day_stop')
        print(f"Daily target hit on {nt} days, daily stop hit on {ns} days, of {len(days)}")
    print("```")

    def table(title, keyfn):
        print(f"\n{title}")
        print("```")
        print(f"{'Group':22} | {'Trades':>6} | {'Win%':>5} | {'Net':>9} | {'Avg/trade':>9}")
        for k in sorted({keyfn(t) for t in T}):
            g = [t for t in T if keyfn(t) == k]
            n = sum(t['net'] for t in g)
            print(f"{str(k):22} | {len(g):6} | {sum(t['net'] > 0 for t in g) / len(g) * 100:5.1f} | {fmt(n):>9} | {fmt(n / len(g)):>9}")
        print("```")

    table("BY EXIT", lambda t: {'take_profit': 'Target', 'stop_loss': 'Stop', 'square_off': '15:15 square-off',
                                'vwap_stop': 'VWAP close stop', 'day_target': 'Daily target (all closed)',
                                'day_stop': 'Daily stop (all closed)'}.get(t['exit_reason'], t['exit_reason']))
    table("BY ENTRY TYPE", lambda t: {'late_join_live': 'Late joiner (live)'}.get(t['entry_trigger'], f"{args.candle}m close"))
    table("BY SIDE", lambda t: f"{t['direction']} ({'CE' if t['type'] == 'call' else 'PE'})")
    table("BY ENTRY HOUR", lambda t: t['opened_at'][11:13] + ':00')

    print("\nSAME SIGNALS, OTHER ASSUMPTIONS")
    print("```")
    print(f"{'Variant':46} | {'Net':>9} | {'PF':>5} | {'Win%':>5} | {'Max DD':>8}")
    print(f"{'Option, paper fills (as run)':46} | {fmt(st['net']):>9} | {st['pf']:5.2f} | {st['win']:5.1f} | {st['dd']:8,.0f}")
    for label, mode, slip in (('Option, 1% bid-ask slippage per side', 'drift', 0.01),
                              ('Option, IV frozen at prev close (no lookahead)', 'prev', 0.0)):
        v = [x for x in (reprice(books, t, mode, slip) for t in T) if x is not None]
        sv = stats(v)
        print(f"{label:46} | {fmt(sv['net']):>9} | {sv['pf']:5.2f} | {sv['win']:5.1f} | {sv['dd']:8,.0f}")
    stock = []
    for t in T:
        q = t['lot_size']
        buy, sell = (t['stock_entry'], t['stock_exit']) if t['direction'] == 'LONG' else (t['stock_exit'], t['stock_entry'])
        stock.append((sell - buy) * q - equity_charges(buy * q, sell * q))
    sv = stats(stock)
    print(f"{'Stock instead, 1 lot of shares (MIS)':46} | {fmt(sv['net']):>9} | {sv['pf']:5.2f} | {sv['win']:5.1f} | {sv['dd']:8,.0f}")
    print("```")

    out = os.path.join(BT_DIR, 'trades_nse_sector_options_orb.csv')
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(T[0].keys()))
        w.writeheader()
        w.writerows(T)
    print(f"\nTrades: {os.path.relpath(out, ROOT)}  |  strategy log: {os.path.relpath(args.log, ROOT)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stop-pct', type=float, default=0.5)
    ap.add_argument('--target-pct', type=float, default=0.75)
    ap.add_argument('--stop-mode', choices=('pct', 'vwap'), default='pct',
                    help="pct = fixed stop-pct; vwap = exit when a 5m candle CLOSES below VWAP (calls) / above (puts)")
    ap.add_argument('--max-open', type=int, default=5)
    ap.add_argument('--max-entries', type=int, default=6)
    ap.add_argument('--max-watchlist', type=int, default=15)
    ap.add_argument('--candle', type=int, default=5, choices=(5, 15))
    ap.add_argument('--no-vwap', action='store_true')
    ap.add_argument('--day-tp', type=float, default=0, help='daily net target Rs (0 = off): close all, no more entries')
    ap.add_argument('--day-sl', type=float, default=0, help='daily net stop Rs (0 = off): close all, no more entries')
    ap.add_argument('--log', default=os.path.join(BT_DIR, 'nse_sector_options_orb.log'))
    args = ap.parse_args()

    uni, idx, stocks = load_data()
    world = World(uni, idx, stocks)
    install(world)
    with open(args.log, 'w') as logf, contextlib.redirect_stdout(logf):
        _, days = run(args, world)
    report(args, days, world.books)


if __name__ == '__main__':
    main()
