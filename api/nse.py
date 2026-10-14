"""NSE live option chain data via nseindia.com API."""

import requests
import time
import threading
import logging

logger = logging.getLogger(__name__)
import json
import os
from datetime import datetime

INDIAN_ASSETS = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}

NSE_INDEX_SYMBOLS = {'NIFTY': 'NIFTY', 'BANKNIFTY': 'BANKNIFTY', 'FINNIFTY': 'FINNIFTY', 'MIDCPNIFTY': 'NIFTY MID SELECT'}
NSE_BASE = 'https://www.nseindia.com'
_DISK_CACHE_DIR = os.path.join(os.path.dirname(__file__), '..', '.nse_cache')

_lock = threading.Lock()
_session = None
_session_ts = 0
_chain_cache = {}  # {(symbol, expiry): {data, ts}}
_CACHE_TTL = 15  # 15 sec cache for live data


def _get_session():
    """Get or refresh NSE session with cookies."""
    global _session, _session_ts
    with _lock:
        if _session and time.time() - _session_ts < 120:
            return _session
        _session = requests.Session()
        _session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.nseindia.com/option-chain',
        })
        try:
            _session.get(NSE_BASE, timeout=10)
            _session_ts = time.time()
        except Exception as e:
            logger.warning(f"NSE session init failed: {e}")
        return _session


def _disk_path(symbol):
    os.makedirs(_DISK_CACHE_DIR, exist_ok=True)
    return os.path.join(_DISK_CACHE_DIR, f'{symbol}.json')

def _save_disk(symbol, data):
    try:
        with open(_disk_path(symbol), 'w') as f:
            json.dump({'data': data, 'ts': time.time()}, f)
    except Exception:
        pass

def _load_disk(symbol):
    try:
        with open(_disk_path(symbol)) as f:
            return json.load(f).get('data')
    except Exception:
        return None

def _fetch_chain_raw(symbol):
    """Fetch raw option chain JSON from NSE. Falls back to last cached data if market closed."""
    s = _get_session()
    nse_sym = NSE_INDEX_SYMBOLS.get(symbol, symbol)
    url = f'{NSE_BASE}/api/option-chain-indices?symbol={nse_sym}'
    try:
        r = s.get(url, timeout=10)
        if r.status_code in (401, 403):
            global _session_ts
            _session_ts = 0
            s = _get_session()
            r = s.get(url, timeout=10)
        if r.ok:
            data = r.json()
            if data and data.get('records', {}).get('data'):
                _save_disk(symbol, data)
                return data
    except Exception as e:
        logger.warning(f"NSE chain fetch error: {e}")
    # Fallback to last saved data
    cached = _load_disk(symbol)
    if cached:
        logger.warning(f"NSE: serving cached data for {symbol} (market closed or error)")
    return cached


def get_nse_expiries(symbol):
    """Return sorted expiry dates (DD-MM-YYYY) from live NSE data."""
    data = _fetch_chain_raw(symbol)
    if not data:
        return _generate_expiries()
    expiries = data.get('records', {}).get('expiryDates', [])
    result = []
    for e in expiries:
        try:
            dt = datetime.strptime(e, '%d-%b-%Y')
            result.append(dt.strftime('%d-%m-%Y'))
        except Exception:
            continue
    return result


def get_nse_chain(symbol, expiry_date):
    """Return (chain, spot, expiry) from live NSE option chain API."""
    # Check cache
    cache_key = (symbol, expiry_date)
    cached = _chain_cache.get(cache_key)
    if cached and time.time() - cached['ts'] < _CACHE_TTL:
        return cached['chain'], cached['spot'], cached['expiry']

    data = _fetch_chain_raw(symbol)
    if not data:
        return None, None, None

    records = data.get('records', {})
    spot = records.get('underlyingValue', 0)

    # Convert DD-MM-YYYY to DD-Mon-YYYY for matching
    try:
        exp_dt = datetime.strptime(expiry_date, '%d-%m-%Y')
        exp_nse = exp_dt.strftime('%d-%b-%Y')  # e.g. "17-Apr-2026"
    except Exception:
        return None, spot, None

    calls = {}
    puts = {}
    for row in records.get('data', []):
        if row.get('expiryDate') != exp_nse:
            continue
        strike = str(row.get('strikePrice', ''))

        ce = row.get('CE')
        if ce:
            calls[strike] = _parse_opt(ce, symbol, 'CE', strike, expiry_date)

        pe = row.get('PE')
        if pe:
            puts[strike] = _parse_opt(pe, symbol, 'PE', strike, expiry_date)

    if not calls and not puts:
        logger.warning(f"NSE: no data for {symbol} expiry {exp_nse} (matched 0 rows out of {len(records.get('data', []))})")
        return None, spot, None

    strikes = sorted(set(list(calls.keys()) + list(puts.keys())), key=lambda s: float(s))
    chain = [{'strike': s, 'call': calls.get(s), 'put': puts.get(s)} for s in strikes]

    _chain_cache[cache_key] = {'chain': chain, 'spot': spot, 'expiry': expiry_date, 'ts': time.time()}
    return chain, spot, expiry_date


def _parse_opt(d, symbol, opt_type, strike, expiry):
    """Parse a single CE/PE entry from NSE response."""
    ltp = d.get('lastPrice', 0)
    iv = d.get('impliedVolatility', 0)
    return {
        'symbol': f"{symbol}-{opt_type}-{strike}-{expiry}",
        'product_id': None,
        'strike': strike,
        'mark_price': ltp,
        'oi': str(int(d.get('openInterest', 0))),
        'volume': int(d.get('totalTradedVolume', 0)),
        'iv': iv / 100 if iv else 0,  # NSE gives IV as percentage
        'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0,
        'bid': d.get('bidprice', 0),
        'ask': d.get('askPrice', 0),
        'bid_size': str(d.get('bidQty', 0)),
        'ask_size': str(d.get('askQty', 0)),
        'change': d.get('change', 0),
        'pchange': d.get('pChange', 0),
    }


def get_nse_daily_candles(symbol, days=15):
    """Fetch daily OHLCV candles for an NSE index using Groww API.

    Returns list of dicts: [{'close': float, 'open': float, 'high': float, 'low': float, 'time': str}, ...]
    ordered oldest-first, limited to last `days` candles.
    """
    try:
        from api.groww import _get_client
        from datetime import timedelta, timezone as tz
        IST = tz(timedelta(hours=5, minutes=30))

        client = _get_client()
        now = datetime.now(IST)
        # Fetch extra days to account for weekends/holidays
        start = (now - timedelta(days=days + 10)).strftime('%Y-%m-%d %H:%M:%S')
        end = now.strftime('%Y-%m-%d %H:%M:%S')

        # Groww symbol format: NSE-<SYMBOL>
        groww_symbol = f'NSE-{symbol}'

        resp = client.get_historical_candles(
            exchange='NSE',
            segment='CASH',
            groww_symbol=groww_symbol,
            start_time=start,
            end_time=end,
            candle_interval='1day'
        )

        candles_raw = resp.get('candles', [])
        if not candles_raw:
            logger.warning(f"Groww daily candles: no data for {symbol}")
            return []

        # Convert to expected format
        result = []
        for c in candles_raw:
            # Groww candle format: [timestamp, open, high, low, close, volume]
            if isinstance(c, (list, tuple)) and len(c) >= 5:
                result.append({
                    'close': float(c[4]),
                    'open': float(c[1]),
                    'high': float(c[2]),
                    'low': float(c[3]),
                    'time': c[0],
                })
            elif isinstance(c, dict):
                result.append({
                    'close': float(c.get('close', c.get('c', 0))),
                    'open': float(c.get('open', c.get('o', 0))),
                    'high': float(c.get('high', c.get('h', 0))),
                    'low': float(c.get('low', c.get('l', 0))),
                    'time': c.get('time', c.get('t', '')),
                })

        return result[-days:] if len(result) > days else result
    except Exception as e:
        logger.warning(f"Groww daily candles fetch failed for {symbol}: {e}")
        return []


def _generate_expiries():
    """Fallback: generate likely weekly Thursday expiries."""
    from datetime import timedelta
    today = datetime.now()
    d = today + timedelta(days=(3 - today.weekday()) % 7 or 7)
    return [(d + timedelta(weeks=i)).strftime('%d-%m-%Y') for i in range(8)]


# ── Stock top-movers (gainers / losers) — for the Dual Momentum Stocks strategy ──

_MOVERS_URL = NSE_BASE + '/api/live-analysis-variations?index={direction}'
_movers_cache = {}        # {direction: {'data': [...], 'ts': float}}
_MOVERS_TTL = 30          # seconds


def _f_num(v):
    try:
        return float(v) if v is not None else 0.0
    except (ValueError, TypeError):
        return 0.0


def _extract_mover_rows(payload):
    """NSE returns gainers grouped by index (NIFTY/allSec/...) but losers as a
    flat {'data': [...]}. Normalize both to a single list of raw rows.

    Preference order for grouped payloads: NIFTY (the liquid NIFTY-50 names)
    first, then allSec as a fallback, else the first group that has data.
    """
    if not isinstance(payload, dict):
        return []
    # Flat shape (losers): {'data': [...]}
    if isinstance(payload.get('data'), list) and payload['data']:
        return payload['data']
    # Grouped shape (gainers): pick the most useful group that has rows.
    for key in ('NIFTY', 'allSec', 'SecGtr20', 'FOSec', 'NIFTYNEXT50', 'BANKNIFTY'):
        grp = payload.get(key)
        if isinstance(grp, dict) and isinstance(grp.get('data'), list) and grp['data']:
            return grp['data']
    # Last resort: any group with a non-empty data list.
    for key, grp in payload.items():
        if key == 'legends':
            continue
        if isinstance(grp, dict) and isinstance(grp.get('data'), list) and grp['data']:
            return grp['data']
    return []


def get_nse_top_movers(direction='gainers', limit=5):
    """Return the top-N NSE stock movers for the given direction.

    Args:
        direction: 'gainers' (largest positive % change) or 'losers'.
        limit: number of stocks to return.

    Returns:
        List of dicts sorted most-extreme first, each:
            {rank, symbol, ltp, change_pct, prev_price}
        Empty list on failure (caller should skip the cycle).

    Uses the live NSE live-analysis-variations endpoint via the shared cookie
    session, with a 30s in-memory cache and a disk fallback (like the chain).
    """
    direction = 'losers' if str(direction).lower().startswith('los') else 'gainers'

    cached = _movers_cache.get(direction)
    if cached and time.time() - cached['ts'] < _MOVERS_TTL:
        rows = cached['data']
    else:
        rows = None
        s = _get_session()
        url = _MOVERS_URL.format(direction=direction)
        try:
            r = s.get(url, timeout=10)
            if r.status_code in (401, 403):
                global _session_ts
                _session_ts = 0
                s = _get_session()
                r = s.get(url, timeout=10)
            if r.ok:
                payload = r.json()
                rows = _extract_mover_rows(payload)
                if rows:
                    _movers_cache[direction] = {'data': rows, 'ts': time.time()}
                    _save_disk(f'movers_{direction}', rows)
        except Exception as e:
            logger.warning(f"NSE top-movers fetch error ({direction}): {e}")
        if not rows:
            disk = _load_disk(f'movers_{direction}')
            if disk:
                logger.warning(f"NSE: serving cached {direction} (market closed or error)")
                rows = disk
    if not rows:
        return []

    out = []
    for row in rows:
        sym = row.get('symbol')
        if not sym:
            continue
        out.append({
            'symbol': sym,
            'ltp': _f_num(row.get('ltp')),
            'change_pct': _f_num(row.get('perChange')),
            'prev_price': _f_num(row.get('prev_price')),
        })
    # Gainers: largest positive first; losers: most negative first.
    out.sort(key=lambda c: c['change_pct'], reverse=(direction == 'gainers'))
    top = out[:limit]
    for i, c in enumerate(top, start=1):
        c['rank'] = i
    return top
