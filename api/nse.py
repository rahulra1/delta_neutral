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


def _generate_expiries():
    """Fallback: generate likely weekly Thursday expiries."""
    from datetime import timedelta
    today = datetime.now()
    d = today + timedelta(days=(3 - today.weekday()) % 7 or 7)
    return [(d + timedelta(weeks=i)).strftime('%d-%m-%Y') for i in range(8)]
