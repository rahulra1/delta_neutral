"""NSE option chain data from daily bhavcopy CSV files.
These are publicly downloadable from nsearchives.nseindia.com without any bot protection."""

import csv
import io
import zipfile
import requests
import time
import threading
from datetime import datetime, timedelta

INDIAN_ASSETS = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}

# Bhavcopy uses these ticker prefixes
BHAV_SYMBOLS = {
    'NIFTY': 'NIFTY', 'BANKNIFTY': 'BANKNIFTY',
    'FINNIFTY': 'FINNIFTY', 'MIDCPNIFTY': 'MIDCPNIFTY',
}

# Yahoo Finance for live spot prices
YAHOO_SYMBOLS = {
    'NIFTY': '%5ENSEI', 'BANKNIFTY': '%5ENSEBANK',
    'FINNIFTY': 'NIFTY_FIN_SERVICE.NS', 'SENSEX': '%5EBSESN',
    'MIDCPNIFTY': 'NIFTY_MIDCAP_SELECT.NS', 'BANKEX': 'BSE-BANK.BO',
}

_lock = threading.Lock()
_bhav_cache = {}  # {date_str: {symbol: [rows]}}
_spot_cache = {}
_yahoo_session = None
_yahoo_crumb = None
_CACHE_TTL = 300  # 5 min for bhavcopy (EOD data doesn't change)


def _get_yahoo():
    global _yahoo_session, _yahoo_crumb
    with _lock:
        if _yahoo_session and _yahoo_crumb:
            return _yahoo_session, _yahoo_crumb
        _yahoo_session = requests.Session()
        _yahoo_session.headers['User-Agent'] = 'Mozilla/5.0'
        try:
            _yahoo_session.get('https://fc.yahoo.com', timeout=8)
            r = _yahoo_session.get('https://query2.finance.yahoo.com/v1/test/getcrumb', timeout=8)
            if r.ok:
                _yahoo_crumb = r.text.strip()
        except Exception:
            pass
        return _yahoo_session, _yahoo_crumb


def _get_spot(symbol):
    cached = _spot_cache.get(symbol)
    if cached and time.time() - cached['ts'] < 30:
        return cached['price']
    ysym = YAHOO_SYMBOLS.get(symbol)
    if not ysym:
        return None
    s, crumb = _get_yahoo()
    if not crumb:
        return None
    try:
        r = s.get(f'https://query2.finance.yahoo.com/v7/finance/options/{ysym}?crumb={crumb}', timeout=8)
        if r.ok:
            q = r.json().get('optionChain', {}).get('result', [{}])[0].get('quote', {})
            price = q.get('regularMarketPrice')
            if price:
                _spot_cache[symbol] = {'price': float(price), 'ts': time.time()}
                return float(price)
    except Exception:
        pass
    return _spot_cache.get(symbol, {}).get('price')


def _fetch_bhavcopy():
    """Download the latest available FO bhavcopy and parse it."""
    today = datetime.now()
    for days_back in range(0, 5):
        d = today - timedelta(days=days_back)
        if d.weekday() >= 5:
            continue
        date_key = d.strftime('%Y%m%d')
        if date_key in _bhav_cache:
            return _bhav_cache[date_key]

        url = f'https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{date_key}_F_0000.csv.zip'
        try:
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            if r.status_code != 200 or len(r.content) < 1000:
                continue
            z = zipfile.ZipFile(io.BytesIO(r.content))
            with z.open(z.namelist()[0]) as f:
                reader = csv.DictReader(io.TextIOWrapper(f))
                data = {}
                for row in reader:
                    if row.get('OptnTp') not in ('CE', 'PE'):
                        continue
                    sym = row.get('TckrSymb', '').split('2')[0]  # e.g. NIFTY26APR... -> NIFTY
                    # Better: match against known symbols
                    for known in BHAV_SYMBOLS:
                        if row.get('TckrSymb', '').startswith(known):
                            sym = known
                            break
                    else:
                        continue
                    data.setdefault(sym, []).append(row)
                _bhav_cache[date_key] = data
                return data
        except Exception as e:
            print(f"Bhavcopy fetch error for {date_key}: {e}")
            continue
    return {}


def get_nse_expiries(symbol):
    """Return sorted expiry dates (DD-MM-YYYY) from bhavcopy data."""
    bhav = _fetch_bhavcopy()
    rows = bhav.get(symbol, [])
    if not rows:
        return _generate_expiries(symbol)
    expiries = set()
    now = datetime.now()
    for row in rows:
        raw = row.get('XpryDt', '')
        try:
            dt = datetime.strptime(raw, '%Y-%m-%d')
            if dt >= now - timedelta(days=1):
                expiries.add((dt, dt.strftime('%d-%m-%Y')))
        except Exception:
            continue
    return [e[1] for e in sorted(expiries)]


def get_nse_chain(symbol, expiry_date):
    """Return (chain, spot, expiry) from bhavcopy + live spot."""
    bhav = _fetch_bhavcopy()
    rows = bhav.get(symbol, [])

    # Convert DD-MM-YYYY to YYYY-MM-DD for matching
    try:
        exp_dt = datetime.strptime(expiry_date, '%d-%m-%Y')
        exp_iso = exp_dt.strftime('%Y-%m-%d')
    except Exception:
        return None, None, None

    # Get live spot from Yahoo, fall back to bhavcopy underlying price
    spot = _get_spot(symbol)
    bhav_spot = None

    calls = {}
    puts = {}
    for row in rows:
        if row.get('XpryDt') != exp_iso:
            continue
        strike = row.get('StrkPric', '').rstrip('0').rstrip('.')
        if not strike:
            continue
        if not bhav_spot:
            try:
                bhav_spot = float(row.get('UndrlygPric', 0))
            except (ValueError, TypeError):
                pass

        opt_type = row.get('OptnTp')
        ltp = _f(row.get('LastPric')) or _f(row.get('ClsPric'))
        r = {
            'symbol': row.get('FinInstrmNm', f"{symbol}-{opt_type}-{strike}-{expiry_date}"),
            'product_id': None,
            'strike': strike,
            'mark_price': ltp,
            'oi': str(int(_f(row.get('OpnIntrst', 0)))),
            'volume': int(_f(row.get('TtlTradgVol', 0))),
            'iv': 0,
            'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0,
            'bid': _f(row.get('LwPric', 0)),
            'ask': _f(row.get('HghPric', 0)),
            'bid_size': '0', 'ask_size': '0', 'bid_iv': 0, 'ask_iv': 0,
        }
        if opt_type == 'CE':
            calls[strike] = r
        elif opt_type == 'PE':
            puts[strike] = r

    if not spot:
        spot = bhav_spot
    if not calls and not puts:
        return None, spot, None

    strikes = sorted(set(list(calls.keys()) + list(puts.keys())), key=lambda s: float(s))
    chain = [{'strike': s, 'call': calls.get(s), 'put': puts.get(s)} for s in strikes]
    return chain, spot, expiry_date


def _f(v):
    try:
        return float(v) if v else 0
    except (ValueError, TypeError):
        return 0


def _generate_expiries(symbol):
    """Fallback: generate likely weekly Thursday expiries."""
    today = datetime.now()
    d = today + timedelta(days=(3 - today.weekday()) % 7 or 7)
    return [(d + timedelta(weeks=i)).strftime('%d-%m-%Y') for i in range(8)]
