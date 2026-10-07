import logging
import time
import threading
import requests
import config
from auth import get_headers

logger = logging.getLogger(__name__)

_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 3  # 3 seconds — prices are volatile but we avoid hammering the API

# ─── Resilience tuning (fixes 1-5) ───────────────────────────────────────────
# How long a cached price may still be SERVED (as stale) when the API is failing,
# instead of returning None and tripping emergency closes mid-volatility (fix #4).
_STALE_TTL = 120           # seconds
# Network timeouts: (connect, read). Short read timeout so a hung request fails
# fast instead of blocking a thread for ~27s and desyncing all the pollers (fix #5).
_HTTP_TIMEOUT = (3, 8)
# Exponential backoff after failures, per asset-group (fix #3).
_BACKOFF_BASE = 2.0        # seconds
_BACKOFF_MAX = 30.0        # seconds cap

# Per asset-group (keyed by the options query) coordination for single-flight
# refresh (fixes #1 and #2): concurrent callers that miss the cache wait for ONE
# in-flight refresh of the whole option board instead of each firing their own.
_group_locks = {}          # group_key -> threading.Lock (serializes refreshers)
_group_locks_guard = threading.Lock()
_group_state = {}          # group_key -> {'last_ok': ts, 'fail_count': int, 'next_try': ts}


def _get_group_lock(group_key):
    with _group_locks_guard:
        lock = _group_locks.get(group_key)
        if lock is None:
            lock = threading.Lock()
            _group_locks[group_key] = lock
            _group_state[group_key] = {'last_ok': 0.0, 'fail_count': 0, 'next_try': 0.0}
        return lock


def _refresh_options_board(asset, group_key):
    """Fetch the FULL options ticker board for `asset` in ONE request and populate
    the shared cache. Returns True on success, False on failure. Applies
    exponential backoff: if we're inside a backoff window after recent failures,
    skips the network call and returns False immediately (fix #3)."""
    now = time.time()
    state = _group_state[group_key]

    # Respect backoff window — don't pile onto a struggling API (fix #3).
    if now < state['next_try']:
        return False

    path = '/v2/tickers'
    query_string = f'?contract_types=call_options,put_options&underlying_asset_symbols={asset}'
    headers = get_headers('GET', path, query_string)
    try:
        response = requests.get(f'{config.BASE_URL}{path}{query_string}',
                                headers=headers, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        tickers = response.json()
        if not tickers.get('success'):
            raise ValueError(f"API success=false: {tickers.get('error')}")

        fetched = time.time()
        with _cache_lock:
            for ticker in tickers.get('result', []):
                pid = ticker.get('product_id')
                if pid is None or ticker.get('mark_price') is None:
                    continue
                data = {
                    'mark_price': float(ticker['mark_price']),
                    'delta': float(ticker.get('greeks', {}).get('delta', 0)) if ticker.get('greeks') else 0
                }
                _cache[pid] = {'data': data, 'ts': fetched}
        # Success → reset backoff (fix #3).
        state['last_ok'] = fetched
        state['fail_count'] = 0
        state['next_try'] = 0.0
        return True
    except Exception as e:
        # Failure → grow backoff window so callers stop hammering (fix #3).
        state['fail_count'] += 1
        delay = min(_BACKOFF_BASE * (2 ** (state['fail_count'] - 1)), _BACKOFF_MAX)
        state['next_try'] = time.time() + delay
        logger.error(f"Error fetching current price (asset={asset}, "
                     f"fail#{state['fail_count']}, backoff {delay:.0f}s): {e}")
        return False


def get_current_price(product_id, asset='BTC'):
    """Return {'mark_price', 'delta'} for an option product_id, or None.

    Resilience behavior:
      - Fresh cache hit (<_CACHE_TTL) returns immediately.
      - On a miss, a SINGLE shared refresh of the whole option board runs under a
        per-asset lock; concurrent callers wait for it instead of stampeding the
        API (fixes #1, #2).
      - While a refresh is backing off after failures, callers skip the network
        (fix #3).
      - If the network can't be refreshed, a recent-but-stale cached value
        (<_STALE_TTL) is returned rather than None, so a transient outage doesn't
        trip emergency closes (fix #4).
    """
    now = time.time()
    with _cache_lock:
        cached = _cache.get(product_id)
        if cached and now - cached['ts'] < _CACHE_TTL:
            return cached['data']

    group_key = f'options:{asset}'
    lock = _get_group_lock(group_key)

    # Single-flight: only one thread refreshes the board at a time (fixes #1, #2).
    with lock:
        # Re-check cache — another thread may have just refreshed it while we waited.
        now = time.time()
        with _cache_lock:
            cached = _cache.get(product_id)
            if cached and now - cached['ts'] < _CACHE_TTL:
                return cached['data']
        _refresh_options_board(asset, group_key)

    # Read whatever the refresh produced (or the prior cache).
    now = time.time()
    with _cache_lock:
        cached = _cache.get(product_id)
    if not cached:
        return None
    age = now - cached['ts']
    if age < _CACHE_TTL:
        return cached['data']
    # Serve stale data during an outage rather than returning None (fix #4).
    if age < _STALE_TTL:
        logger.warning(f"Serving STALE price for product {product_id} "
                       f"(age {age:.0f}s) — API refresh unavailable")
        return cached['data']
    return None


def get_futures_price(symbol='BTCUSD'):
    """Fetch mark price for a perpetual futures contract by symbol."""
    now = time.time()
    cache_key = f'futures_{symbol}'
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached['ts'] < _CACHE_TTL:
            return cached['data']

    path = f'/v2/tickers/{symbol}'
    headers = get_headers('GET', path, '')
    try:
        response = requests.get(f'{config.BASE_URL}{path}', headers=headers, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()
        if result.get('success') and result.get('result'):
            ticker = result['result']
            data = {'mark_price': float(ticker['mark_price']), 'delta': 0}
            with _cache_lock:
                _cache[cache_key] = {'data': data, 'ts': now}
            return data
        return None
    except Exception as e:
        logger.error(f"Error fetching futures price for {symbol}: {e}")
        return None


def get_futures_prices_bulk(symbols=None, contract_type='perpetual_futures'):
    """Fetch marks for many perpetual symbols in ONE request.

    Hitting /v2/tickers/<symbol> once per position doesn't scale — pricing 15-20
    legs means 15-20 sequential HTTP calls in a single web request, and the later
    ones start failing (rate limits / connection resets), leaving those legs stuck
    at entry price. This fetches the whole perpetual ticker board in one call and
    returns {symbol: {'mark_price': float, 'delta': 0}}.

    Results are written into the same per-symbol cache used by get_futures_price,
    so subsequent get_futures_price calls are served from cache.
    """
    now = time.time()
    path = '/v2/tickers'
    query_string = f'?contract_types={contract_type}'
    headers = get_headers('GET', path, query_string)
    out = {}
    try:
        response = requests.get(f'{config.BASE_URL}{path}{query_string}',
                                headers=headers, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()
        if not result.get('success'):
            return out
        wanted = set(symbols) if symbols else None
        with _cache_lock:
            for ticker in result.get('result', []):
                sym = ticker.get('symbol')
                mp = ticker.get('mark_price')
                if not sym or mp is None:
                    continue
                if wanted is not None and sym not in wanted:
                    continue
                data = {'mark_price': float(mp), 'delta': 0}
                out[sym] = data
                _cache[f'futures_{sym}'] = {'data': data, 'ts': now}
        return out
    except Exception as e:
        logger.error(f"Error fetching bulk futures prices: {e}")
        return out
