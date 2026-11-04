"""Option-chain replay from REAL Groww historical data (no pricing model).

Every price shown is a real traded price from Groww's historical candles API
(https://groww.in/trade-api/docs/python-sdk/backtesting — equities, indices and F&O incl. EXPIRED
contracts, from 2020):

  expiries   client.get_expiries(exchange, underlying, year, month)          -> ['YYYY-MM-DD', ...]
  contracts  client.get_contracts(exchange, underlying, expiry)              -> ['NSE-NIFTY-28Oct25-25900-CE', ...]
  candles    client.get_historical_candles(exchange, segment, groww_symbol,
                                           start, end, '1minute')           -> [ts, o, h, l, c, volume, oi]

Price of a contract "at" HH:MM = close of the last 1-minute candle that had COMPLETED by HH:MM
(i.e. the last trade up to then). If the contract has not traded yet that day it is reported as
"no trade yet" (the chain) or the previous session's last close (positions, flagged).

Past data never changes, so everything is cached on disk under .nse_cache/replay/ and each contract-day
is downloaded once. Lot sizes come from the official NSE / BSE F&O bhavcopy of that day.
"""

import bisect
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, '.nse_cache', 'replay')
OPEN_MIN, CLOSE_MIN = 9 * 60 + 15, 15 * 60 + 30
BSE_SYMBOLS = {'SENSEX', 'BANKEX', 'SENSEX50'}
INDICES = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50', 'SENSEX', 'BANKEX']
MAX_WORKERS = 6

_mem = {}                      # in-memory cache: key -> value
_mem_lock = threading.Lock()


class ReplayError(Exception):
    """User-facing error; `code` lets the UI react (e.g. 'groww_auth')."""

    def __init__(self, msg, code='error', status=400):
        super().__init__(msg)
        self.code, self.status = code, status


# --------------------------------------------------------------------------- helpers
def exchange_of(symbol):
    return 'BSE' if symbol in BSE_SYMBOLS else 'NSE'


def today_ist():
    return datetime.now(IST).date()


def hhmm(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


def parse_hhmm(s):
    h, m = str(s).split(':')[:2]
    return int(h) * 60 + int(m)


def _path(*parts):
    p = os.path.join(CACHE, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _read(path, max_age=None):
    try:
        if max_age is not None and time.time() - os.path.getmtime(path) > max_age:
            return None
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, separators=(',', ':'))
    os.replace(tmp, path)


def _memo(key, fn):
    with _mem_lock:
        if key in _mem:
            return _mem[key]
    val = fn()
    with _mem_lock:
        if len(_mem) > 5000:
            _mem.clear()
        _mem[key] = val
    return val


def _call(fn, *args, **kwargs):
    """Groww call with retries on rate limits; auth failures become ReplayError('groww_auth')."""
    from api.groww import _is_auth_error
    delay = 1.0
    for attempt in range(4):
        try:
            return fn(*args, **kwargs)
        except Exception as e:                      # noqa: BLE001 - SDK raises several types
            msg = str(e)
            if _is_auth_error(e):
                raise ReplayError('Groww token has expired or is invalid — refresh the Groww profile '
                                  '(Broker page) and try again.', code='groww_auth', status=424)
            low = msg.lower()
            if attempt < 3 and ('429' in low or 'rate' in low or 'too many' in low or 'timed out' in low
                                or 'timeout' in low):
                time.sleep(delay)
                delay *= 2
                continue
            raise ReplayError(f'Groww API error: {msg[:200]}', code='groww_error', status=502)


def parse_contract(gsym):
    """'NSE-NIFTY-28Oct25-25900-CE' -> {'gsym', 'expiry_code', 'strike', 'type'} (underlying may contain '-')."""
    parts = gsym.split('-')
    if len(parts) < 5 or parts[-1] not in ('CE', 'PE'):
        return None
    try:
        strike = float(parts[-2])
    except ValueError:
        return None
    return {'gsym': gsym, 'strike': strike, 'type': parts[-1], 'expiry_code': parts[-3]}


def _ts_minute(ts):
    """Candle timestamp (string 'YYYY-MM-DD HH:MM:SS' / ISO, or epoch seconds) -> (date, minute of day IST)."""
    if isinstance(ts, (int, float)):
        t = datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts, IST)
    else:
        s = str(ts).replace('T', ' ')[:19]
        t = datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    return t.date(), t.hour * 60 + t.minute


# --------------------------------------------------------------------------- Groww data (cached)
def expiries(client, symbol, on_date):
    """Expiries that are still alive on `on_date` (this month + the next 3), oldest first."""
    exch = exchange_of(symbol)
    out = set()
    d = date(on_date.year, on_date.month, 1)
    for _ in range(4):
        cur = (d.year, d.month) >= (today_ist().year, today_ist().month)
        path = _path('expiries', f'{exch}_{symbol}_{d.year}_{d.month:02d}.json')
        data = _read(path, max_age=86400 if cur else None)
        if data is None:
            resp = _call(client.get_expiries, exchange=exch, underlying_symbol=symbol, year=d.year, month=d.month)
            data = sorted((resp or {}).get('expiries') or [])
            _write(path, data)
        out.update(x for x in data if x >= on_date.isoformat())
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return sorted(out)


def contracts(client, symbol, expiry):
    """All option contracts of one expiry: [{'gsym', 'strike', 'type'}], sorted by strike."""
    exch = exchange_of(symbol)

    def load():
        path = _path('contracts', f'{exch}_{symbol}_{expiry}.json')
        live = expiry >= today_ist().isoformat()
        data = _read(path, max_age=86400 if live else None)
        if data is None:
            resp = _call(client.get_contracts, exchange=exch, underlying_symbol=symbol, expiry_date=expiry)
            data = (resp or {}).get('contracts') or []
            _write(path, data)
        rows = [c for c in (parse_contract(g) for g in data) if c]
        return sorted(rows, key=lambda c: (c['strike'], c['type']))
    return _memo(('contracts', exch, symbol, expiry), load)


def day_candles(client, exchange, segment, gsym, day):
    """1-minute candles of one instrument for one session: [(minute, o, h, l, c, volume, oi), ...]."""
    dstr = day.isoformat()

    def load():
        path = _path('candles', exchange, dstr, f'{gsym}.json')
        data = _read(path)
        if data is None:
            resp = _call(client.get_historical_candles, exchange=exchange, segment=segment, groww_symbol=gsym,
                         start_time=f'{dstr} 09:15:00', end_time=f'{dstr} 15:30:00', candle_interval='1minute')
            data = []
            for c in (resp or {}).get('candles') or []:
                d, m = _ts_minute(c[0])
                if d != day or not (OPEN_MIN <= m < CLOSE_MIN):
                    continue
                vals = [float(x) if x is not None else None for x in c[1:7]] + [None] * (7 - len(c))
                data.append([m] + vals[:6])
            data.sort(key=lambda r: r[0])
            complete = day < today_ist() or datetime.now(IST).hour * 60 + datetime.now(IST).minute > CLOSE_MIN + 15
            if complete:
                _write(path, data)
        return data
    if day >= today_ist():                 # today's session is still growing: never memoise it
        return load()
    return _memo(('candles', exchange, dstr, gsym), load)


def snapshot(candles, minute):
    """State of an instrument at `minute` from its 1-minute candles (only candles COMPLETED by then)."""
    starts = [c[0] for c in candles]
    i = bisect.bisect_right(starts, minute - 1)
    if i == 0:
        return None
    done = candles[:i]
    last = done[-1]
    oi = next((c[6] for c in reversed(done) if c[6]), None)
    return {'ltp': last[4], 'last_trade': hhmm(last[0] + 1), 'open': done[0][1],
            'high': max(c[2] for c in done), 'low': min(c[3] for c in done),
            'volume': sum(c[5] or 0 for c in done), 'oi': oi}


def spot_candles(client, symbol, day):
    exch = exchange_of(symbol)
    return day_candles(client, exch, 'CASH', f'{exch}-{symbol}', day)


def is_trading_day(client, symbol, day):
    if day.weekday() >= 5 or day > today_ist():
        return False
    return bool(spot_candles(client, symbol, day))


def step_day(client, symbol, day, direction):
    """Next (+1) / previous (-1) trading day, found from the real index candles."""
    d = day
    for _ in range(12):
        d += timedelta(days=direction)
        if d > today_ist():
            return None
        if is_trading_day(client, symbol, d):
            return d
    return None


def lot_size(symbol, expiry, day):
    """Official lot size from the NSE / BSE F&O bhavcopy of that day (None if not available)."""
    def load():
        try:
            import csv
            import io
            from backtest.nse_bhavcopy import download_bhavcopy
            raw = download_bhavcopy(day.strftime('%Y%m%d'), exchange=exchange_of(symbol))
            if not raw:
                return None
            for r in csv.DictReader(io.StringIO(raw)):
                if r.get('TckrSymb') == symbol and r.get('XpryDt') == expiry and r.get('NewBrdLotQty'):
                    return int(float(r['NewBrdLotQty']))
        except Exception as e:                      # noqa: BLE001
            logger.warning("replay lot size %s %s %s: %s", symbol, expiry, day, e)
        return None
    return _memo(('lot', symbol, expiry, day.isoformat()), load)


# --------------------------------------------------------------------------- public API
def chain(client, symbol, day, minute, expiry=None, width=10):
    symbol = symbol.upper()
    exch = exchange_of(symbol)
    if day > today_ist():
        raise ReplayError('Date is in the future.')
    spot_c = spot_candles(client, symbol, day)
    if not spot_c:
        raise ReplayError(f'{day} is not a trading day for {symbol} (Groww has no index candles).',
                          code='not_trading_day', status=404)
    spot = snapshot(spot_c, minute)
    exps = expiries(client, symbol, day)
    if not exps:
        raise ReplayError(f'No live expiries for {symbol} on {day}.', status=404)
    expiry = expiry if expiry in exps else exps[0]
    cons = contracts(client, symbol, expiry)
    if not cons:
        raise ReplayError(f'No contracts for {symbol} {expiry}.', status=404)
    strikes = sorted({c['strike'] for c in cons})
    ref = spot['ltp'] if spot else spot_c[0][1]
    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - ref))
    lo, hi = max(0, atm_i - width), min(len(strikes), atm_i + width + 1)
    pick = set(strikes[lo:hi])
    chosen = [c for c in cons if c['strike'] in pick]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        data = list(ex.map(lambda c: day_candles(client, exch, 'FNO', c['gsym'], day), chosen))
    by = {}
    for c, cd in zip(chosen, data):
        snap = snapshot(cd, minute)
        by.setdefault(c['strike'], {})[c['type']] = {'gsym': c['gsym'], **(snap or {'ltp': None})}
    rows = [{'strike': k, 'ce': by.get(k, {}).get('CE'), 'pe': by.get(k, {}).get('PE')} for k in sorted(pick)]
    exp_d = date.fromisoformat(expiry)
    return {
        'symbol': symbol, 'exchange': exch, 'date': day.isoformat(), 'time': hhmm(minute),
        'spot': spot['ltp'] if spot else None, 'spot_last': spot['last_trade'] if spot else None,
        'spot_open': spot_c[0][1], 'expiries': exps, 'expiry': expiry, 'dte': (exp_d - day).days,
        'atm': strikes[atm_i], 'lot_size': lot_size(symbol, expiry, day), 'rows': rows,
        'source': 'Groww historical 1-minute candles (real trades)',
    }


def quote(client, legs, day, minute):
    """Current price of each position leg at (day, minute).

    leg: {'symbol', 'gsym', 'strike', 'type', 'expiry'}. Expired legs settle at intrinsic value against the
    underlying's last price on expiry day (index options are cash-settled on the closing value)."""
    out = []
    for leg in legs:
        sym, exp = leg['symbol'].upper(), leg['expiry']
        exch = exchange_of(sym)
        exp_d = date.fromisoformat(exp)
        if day > exp_d or (day == exp_d and minute >= CLOSE_MIN):
            sc = spot_candles(client, sym, exp_d)
            settle_spot = sc[-1][4] if sc else None
            if settle_spot is None:
                out.append({'price': None, 'status': 'no expiry data'})
                continue
            k = float(leg['strike'])
            val = max(0.0, settle_spot - k) if leg['type'] == 'CE' else max(0.0, k - settle_spot)
            out.append({'price': round(val, 2), 'status': 'expired', 'note': f'settled vs {sym} {settle_spot:.2f} on {exp}'})
            continue
        snap = snapshot(day_candles(client, exch, 'FNO', leg['gsym'], day), minute)
        if snap:
            out.append({'price': snap['ltp'], 'status': 'live', 'note': f"last trade {snap['last_trade']}"})
            continue
        prev = step_day(client, sym, day, -1)
        pc = day_candles(client, exch, 'FNO', leg['gsym'], prev) if prev else []
        if pc:
            out.append({'price': pc[-1][4], 'status': 'prev_close', 'note': f'no trade yet today — {prev} close'})
        else:
            out.append({'price': None, 'status': 'no data', 'note': 'no trades found'})
    return out
