"""NSE F&O UDiFF bhavcopy downloader + parser (real historical options data).

Downloads the daily "Final" F&O bhavcopy ZIP from NSE archives, caches it on
disk, and parses out option rows per underlying. This is REAL settled market
data (settle price, close, underlying spot, OI, volume) — no approximation.

UDiFF format (available ~Jul 2024 onward):
    https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip

CSV columns of interest:
    TradDt, TckrSymb, FinInstrmTp, XpryDt, StrkPric, OptnTp,
    ClsPric, SttlmPric, UndrlygPric, OpnIntrst, ChngInOpnIntrst, TtlTradgVol

FinInstrmTp: IDO=index option, IDF=index future, STO=stock option, STF=stock future.
"""

import os
import io
import csv
import time
import zipfile
import logging
import requests

logger = logging.getLogger(__name__)

# Per-exchange bhavcopy sources. NSE serves a ZIP from its archive host; BSE
# serves a plain CSV from its download path. Both use the same UDiFF schema.
EXCHANGES = {
    'NSE': {
        'base': 'https://www.nseindia.com',
        'url': ('https://nsearchives.nseindia.com/content/fo/'
                'BhavCopy_NSE_FO_0_0_0_{date}_F_0000.csv.zip'),
        'zipped': True,
        'prefix': 'fo',  # existing NSE cache files are named fo_YYYYMMDD.csv
    },
    'BSE': {
        'base': 'https://www.bseindia.com',
        'url': ('https://www.bseindia.com/download/Bhavcopy/Derivative/'
                'BhavCopy_BSE_FO_0_0_0_{date}_F_0000.CSV'),
        'zipped': False,
        'prefix': 'bse_fo',
    },
}

_CACHE_DIR = os.path.join(os.path.dirname(__file__), '__pycache__', '_bhav_cache')

_sessions = {}  # {exchange: (session, ts)}


def _get_session(exchange='NSE'):
    """Warm an exchange session (browser headers + a hit on the homepage first)."""
    sess, ts = _sessions.get(exchange, (None, 0))
    if sess and time.time() - ts < 300:
        return sess
    cfg = EXCHANGES[exchange]
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': cfg['base'] + '/',
    })
    try:
        s.get(cfg['base'], timeout=10)
    except Exception as e:
        logger.warning(f"{exchange} session warm failed: {e}")
    _sessions[exchange] = (s, time.time())
    return s


def _cache_path(date_str, exchange='NSE'):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    prefix = EXCHANGES[exchange]['prefix']
    return os.path.join(_CACHE_DIR, f'{prefix}_{date_str}.csv')


def _miss_path(date_str, exchange='NSE'):
    """Marker for dates with no bhavcopy (holiday/weekend/not published)."""
    prefix = EXCHANGES[exchange]['prefix']
    return os.path.join(_CACHE_DIR, f'{prefix}_{date_str}.miss')


def download_bhavcopy(date_str, exchange='NSE'):
    """Download + cache the F&O bhavcopy for a date (YYYYMMDD).

    exchange: 'NSE' (zipped CSV) or 'BSE' (plain CSV). Both UDiFF schema.
    Returns the raw CSV text, or None if unavailable (holiday/weekend/404).
    Results (both hits and misses) are cached on disk so reruns are instant.
    """
    cfg = EXCHANGES[exchange]
    cache = _cache_path(date_str, exchange)
    if os.path.exists(cache):
        with open(cache, 'r', encoding='utf-8') as f:
            return f.read()
    if os.path.exists(_miss_path(date_str, exchange)):
        return None

    url = cfg['url'].format(date=date_str)
    s = _get_session(exchange)
    try:
        r = s.get(url, timeout=30)
    except Exception as e:
        logger.warning(f"{exchange} bhavcopy {date_str} download error: {e}")
        return None

    if cfg['zipped']:
        # Expect a ZIP; anything else (HTML error page, 404) means no data.
        if r.status_code != 200 or r.content[:2] != b'PK':
            try:
                open(_miss_path(date_str, exchange), 'w').close()
            except Exception:
                pass
            return None
        try:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            name = z.namelist()[0]
            raw = z.read(name).decode('utf-8', 'replace')
        except Exception as e:
            logger.warning(f"{exchange} bhavcopy {date_str} unzip error: {e}")
            return None
    else:
        # Plain CSV; a valid UDiFF file starts with the 'TradDt' header.
        if r.status_code != 200 or r.content[:6] != b'TradDt':
            try:
                open(_miss_path(date_str, exchange), 'w').close()
            except Exception:
                pass
            return None
        raw = r.text

    with open(cache, 'w', encoding='utf-8') as f:
        f.write(raw)
    return raw


def get_option_chain(date_str, symbol='NIFTY', instrument='IDO', exchange='NSE'):
    """Parse the bhavcopy for a date and return option rows for one underlying.

    Returns a dict:
        {
          'date': 'YYYY-MM-DD',
          'spot': float,                 # underlying price (from UndrlygPric)
          'expiries': [ 'YYYY-MM-DD', ... ]  # sorted ascending, unique
          'options': {
              expiry: {
                  'CE': { strike(float): row_dict, ... },
                  'PE': { strike(float): row_dict, ... },
              }, ...
          }
        }
    where row_dict carries: strike, opt_type, expiry, close, settle, spot,
    oi, chg_oi, volume.

    Returns None if the day has no data (holiday) or no matching rows.
    """
    raw = download_bhavcopy(date_str, exchange=exchange)
    if not raw:
        return None

    rd = csv.DictReader(io.StringIO(raw))
    options = {}
    expiries = set()
    spot = None
    trad_dt = None

    for row in rd:
        if row.get('TckrSymb') != symbol:
            continue
        if row.get('FinInstrmTp') != instrument:
            continue
        opt_tp = row.get('OptnTp')
        if opt_tp not in ('CE', 'PE'):
            continue

        try:
            strike = float(row['StrkPric'])
            settle = float(row['SttlmPric'])
            close = float(row['ClsPric']) if row.get('ClsPric') else settle
            underlying = float(row['UndrlygPric']) if row.get('UndrlygPric') else None
            oi = float(row.get('OpnIntrst') or 0)
            chg_oi = float(row.get('ChngInOpnIntrst') or 0)
            volume = float(row.get('TtlTradgVol') or 0)
        except (ValueError, KeyError):
            continue

        expiry = row.get('XpryDt')  # 'YYYY-MM-DD'
        if not expiry:
            continue
        trad_dt = row.get('TradDt') or trad_dt
        if underlying and underlying > 0:
            spot = underlying

        expiries.add(expiry)
        leg = options.setdefault(expiry, {'CE': {}, 'PE': {}})
        leg[opt_tp][strike] = {
            'strike': strike,
            'opt_type': opt_tp,
            'expiry': expiry,
            'close': close,
            'settle': settle,
            'spot': underlying,
            'oi': oi,
            'chg_oi': chg_oi,
            'volume': volume,
        }

    if not options or spot is None:
        return None

    return {
        'date': (trad_dt or date_str),
        'spot': spot,
        'expiries': sorted(expiries),
        'options': options,
    }
