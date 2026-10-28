"""Live Proxy PCR sampler for the dashboard.

Proxy PCR = sum of the TOP_N largest put OIs / sum of the TOP_N largest call OIs,
taken from the strikes within ATM +/- WINDOW on the nearest expiry. Same
definition as backtest/proxy_pcr_atm_sell_backtest.py.

Sources:
    BTC, ETH            Delta Exchange live (public /v2/tickers)
    NIFTY, BANKNIFTY    NSE option-chain API, falls back to Groww
    SENSEX              Groww (BSE); needs a Groww profile or GROWW_API_KEY env

A single daemon thread samples every SAMPLE_SECS. History is kept in memory and
persisted to a small JSON file so it survives restarts. A sample is only added
when the exchange data actually refreshed (Indian markets closed -> no new points).
"""
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SYMBOLS = ['BTC', 'ETH', 'NIFTY', 'BANKNIFTY', 'SENSEX']
WINDOW = 10           # strikes each side of ATM
TOP_N = 10            # largest OIs per side
HIGH, LOW = 1.6, 0.6  # bands used by the strategy (bullish / bearish)
SAMPLE_SECS = int(os.environ.get('PROXY_PCR_SAMPLE_SECS', '60'))
HISTORY_HOURS = 72
MAX_POINTS = HISTORY_HOURS * 3600 // max(SAMPLE_SECS, 1) + 10

IST = timezone(timedelta(hours=5, minutes=30))

_lock = threading.Lock()
_history = {s: deque(maxlen=MAX_POINTS) for s in SYMBOLS}
_latest = {s: None for s in SYMBOLS}
_errors = {s: None for s in SYMBOLS}
_last_src_ts = {}
_started = False

_DATA_DIR = os.path.dirname(os.environ.get('ALGOX_DB_PATH', '')) or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
_ENV = os.environ.get('ALGOX_ENV', '')
_HIST_FILE = os.path.join(_DATA_DIR, f"{_ENV + '_' if _ENV else ''}proxy_pcr_history.json")


# ── core calculation ─────────────────────────────────────────────────────

def compute_proxy_pcr(chain, spot, window=WINDOW, top=TOP_N, details=False):
    """chain = [{'strike': str, 'call': {'oi': ..}, 'put': {'oi': ..}}, ...].

    Returns dict(pcr, atm, put_oi, call_oi) or None. With details=True also
    returns the window strikes and the top-N strikes chosen on each side.
    """
    rows = []
    for r in chain or []:
        if not r.get('call') or not r.get('put'):
            continue
        try:
            rows.append((float(r['strike']), float(r['call'].get('oi') or 0),
                         float(r['put'].get('oi') or 0)))
        except (TypeError, ValueError):
            continue
    if len(rows) < 3 or not spot:
        return None
    rows.sort()
    i = min(range(len(rows)), key=lambda j: abs(rows[j][0] - float(spot)))
    win = rows[max(0, i - window): i + window + 1]
    top_ce = sorted(((k, c) for k, c, _ in win), key=lambda x: -x[1])[:top]
    top_pe = sorted(((k, p) for k, _, p in win), key=lambda x: -x[1])[:top]
    call_oi = sum(c for _, c in top_ce)
    put_oi = sum(p for _, p in top_pe)
    if call_oi <= 0:
        return None
    out = {'pcr': put_oi / call_oi, 'atm': rows[i][0],
           'put_oi': put_oi, 'call_oi': call_oi}
    if details:
        out.update(
            window=[{'strike': k, 'call_oi': c, 'put_oi': p} for k, c, p in win],
            top_calls=[{'strike': k, 'oi': c} for k, c in top_ce],
            top_puts=[{'strike': k, 'oi': p} for k, p in top_pe])
    return out


def _signal(pcr):
    if pcr > HIGH:
        return 'SELL PE'
    if pcr < LOW:
        return 'SELL CE'
    return 'SELL BOTH'


# ── data sources ─────────────────────────────────────────────────────────

def _crypto(asset):
    from config import set_thread_credentials
    from api.chain import get_expiries, get_option_chain_full
    set_thread_credentials('', '', 'delta_exchange')   # live public endpoints
    exps = get_expiries(asset)
    if not exps:
        raise RuntimeError('no Delta expiries')
    chain, spot, exp = get_option_chain_full(exps[0], asset)
    if not chain:
        raise RuntimeError('empty Delta chain')
    return chain, spot, exp, 'Delta', None   # 24x7 -> no source ts dedupe


def _nearest_indian_expiry(expiries):
    """First expiry not yet expired (today counts until 15:30 IST)."""
    now = datetime.now(IST)
    for e in expiries:
        try:
            d = datetime.strptime(e, '%d-%m-%Y').date()
        except ValueError:
            continue
        if d > now.date() or (d == now.date() and (now.hour, now.minute) < (15, 30)):
            return e
    return None


def _groww_creds():
    key = os.environ.get('GROWW_API_KEY')
    if key:
        return key, os.environ.get('GROWW_API_SECRET', '')
    try:
        from models import get_db
        conn = get_db()
        row = conn.execute("SELECT api_key, api_secret FROM profiles WHERE broker='groww' "
                           "AND api_key != '' ORDER BY id LIMIT 1").fetchone()
        conn.close()
        if row:
            return row[0], row[1] or ''
    except Exception as e:
        logger.debug(f'proxy_pcr groww creds lookup failed: {e}')
    return None, None


def _groww(symbol):
    from config import set_thread_credentials
    from api.groww import get_groww_expiries, get_groww_chain
    key, secret = _groww_creds()
    if not key:
        raise RuntimeError('no Groww profile/credentials')
    set_thread_credentials(key, secret, 'groww')
    exp = _nearest_indian_expiry(get_groww_expiries(symbol))
    if not exp:
        raise RuntimeError('no Groww expiries (token expired?)')
    chain, spot, exp = get_groww_chain(symbol, exp)
    if not chain:
        raise RuntimeError('empty Groww chain (token expired?)')
    return chain, spot, exp, 'Groww', None


def _nse(symbol):
    """NSE option chain via the v3 API (the old option-chain-indices URL now 404s)."""
    from api.nse import _get_session, NSE_INDEX_SYMBOLS
    s = _get_session()
    sym = NSE_INDEX_SYMBOLS.get(symbol, symbol)
    base = 'https://www.nseindia.com/api'
    info = s.get(f'{base}/option-chain-contract-info?symbol={sym}', timeout=10)
    if not info.ok:
        raise RuntimeError(f'NSE contract-info HTTP {info.status_code}')
    exps = []
    for e in info.json().get('expiryDates', []):
        try:
            exps.append(datetime.strptime(e, '%d-%b-%Y'))
        except ValueError:
            pass
    exp = _nearest_indian_expiry([d.strftime('%d-%m-%Y') for d in sorted(exps)])
    if not exp:
        raise RuntimeError('no NSE expiry')
    exp_nse = datetime.strptime(exp, '%d-%m-%Y').strftime('%d-%b-%Y')
    r = s.get(f'{base}/option-chain-v3?type=Indices&symbol={sym}&expiry={exp_nse}', timeout=10)
    if not r.ok:
        raise RuntimeError(f'NSE chain HTTP {r.status_code}')
    rec = r.json().get('records', {})
    chain = []
    for row in rec.get('data', []):
        ce, pe = row.get('CE'), row.get('PE')
        k = row.get('strikePrice') or (ce or pe or {}).get('strikePrice')
        if k is None:
            continue
        chain.append({'strike': str(k),
                      'call': {'oi': (ce or {}).get('openInterest', 0)} if ce else None,
                      'put': {'oi': (pe or {}).get('openInterest', 0)} if pe else None})
    if not chain:
        raise RuntimeError('empty NSE chain')
    return chain, rec.get('underlyingValue'), exp, 'NSE', rec.get('timestamp')


def _indian_market_open():
    now = datetime.now(IST)
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)


def _fetch(symbol):
    if symbol in ('BTC', 'ETH'):
        return _crypto(symbol)
    if symbol == 'SENSEX':
        return _groww(symbol)
    try:
        return _nse(symbol)
    except Exception as e:
        logger.info(f'proxy_pcr {symbol}: NSE failed ({e}), trying Groww')
        return _groww(symbol)


# ── sampler ──────────────────────────────────────────────────────────────

def sample_once(symbol):
    try:
        chain, spot, exp, source, src_ts = _fetch(symbol)
        res = compute_proxy_pcr(chain, spot)
        if not res:
            raise RuntimeError('not enough strikes/OI')
    except Exception as e:
        with _lock:
            _errors[symbol] = str(e)
        return None

    indian = symbol not in ('BTC', 'ETH')
    # Skip duplicates when the exchange snapshot didn't change / market closed.
    dedupe_key = src_ts or (f"{res['put_oi']:.0f}/{res['call_oi']:.0f}" if indian else None)
    stale = bool(indian and dedupe_key and _last_src_ts.get(symbol) == dedupe_key)
    point = {
        'ts': int(time.time()), 'pcr': round(res['pcr'], 4), 'atm': res['atm'],
        'spot': float(spot or 0), 'expiry': exp, 'put_oi': res['put_oi'],
        'call_oi': res['call_oi'], 'source': source, 'signal': _signal(res['pcr']),
    }
    with _lock:
        _errors[symbol] = None
        _latest[symbol] = dict(point, stale=stale or (indian and not _indian_market_open()))
        if not stale:
            _history[symbol].append(point)
            if dedupe_key:
                _last_src_ts[symbol] = dedupe_key
    return point


def _save():
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with _lock:
            data = {s: list(_history[s]) for s in SYMBOLS}
        tmp = _HIST_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, _HIST_FILE)
    except Exception as e:
        logger.debug(f'proxy_pcr save failed: {e}')


def _load():
    try:
        with open(_HIST_FILE) as f:
            data = json.load(f)
        cutoff = time.time() - HISTORY_HOURS * 3600
        with _lock:
            for s in SYMBOLS:
                pts = [p for p in data.get(s, []) if p.get('ts', 0) >= cutoff]
                _history[s].extend(pts)
                if pts:
                    _latest[s] = dict(pts[-1], stale=True)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f'proxy_pcr history load failed: {e}')


def _loop():
    _load()
    while True:
        start = time.time()
        for s in SYMBOLS:
            sample_once(s)
        _save()
        time.sleep(max(5, SAMPLE_SECS - (time.time() - start)))


def start_sampler():
    """Idempotent: start the background sampler thread once per process."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name='proxy-pcr-sampler', daemon=True).start()
    logger.info(f'Proxy PCR sampler started ({SAMPLE_SECS}s, history -> {_HIST_FILE})')


def calculate(symbol, source='auto', window=WINDOW, top=TOP_N):
    """Fresh on-demand calculation with full strike breakdown.

    source: 'auto' (default routing), 'nse' or 'groww' (Indian indices only).
    """
    symbol = symbol.upper()
    if source == 'nse':
        fetched = _nse(symbol)
    elif source == 'groww':
        fetched = _groww(symbol)
    else:
        fetched = _fetch(symbol)
    chain, spot, exp, src, src_ts = fetched
    res = compute_proxy_pcr(chain, spot, window, top, details=True)
    if not res:
        raise RuntimeError('not enough strikes/OI')
    res.update(symbol=symbol, spot=float(spot or 0), expiry=exp, source=src,
               source_time=src_ts, window_strikes=window, top=top,
               signal=_signal(res['pcr']), pcr=round(res['pcr'], 4),
               computed_at=datetime.now(IST).isoformat(timespec='seconds'))
    return res


def snapshot(hours=24):
    """Data for the dashboard API."""
    cutoff = time.time() - hours * 3600
    with _lock:
        out = {}
        for s in SYMBOLS:
            hist = [{'ts': p['ts'], 'pcr': p['pcr'], 'spot': p['spot']}
                    for p in _history[s] if p['ts'] >= cutoff]
            out[s] = {'latest': _latest[s], 'error': _errors[s], 'history': hist}
    return {'symbols': out, 'window': WINDOW, 'top': TOP_N, 'high': HIGH, 'low': LOW,
            'sample_secs': SAMPLE_SECS, 'order': SYMBOLS}
