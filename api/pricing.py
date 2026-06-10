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


def get_current_price(product_id, asset='BTC'):
    now = time.time()
    with _cache_lock:
        cached = _cache.get(product_id)
        if cached and now - cached['ts'] < _CACHE_TTL:
            return cached['data']

    path = '/v2/tickers'
    query_string = f'?contract_types=call_options,put_options&underlying_asset_symbols={asset}'
    headers = get_headers('GET', path, query_string)
    try:
        response = requests.get(f'{config.BASE_URL}{path}{query_string}', headers=headers, timeout=(3, 27))
        response.raise_for_status()
        tickers = response.json()
        if tickers.get('success'):
            # Cache all tickers from this response
            with _cache_lock:
                for ticker in tickers.get('result', []):
                    pid = ticker.get('product_id')
                    data = {
                        'mark_price': float(ticker['mark_price']),
                        'delta': float(ticker.get('greeks', {}).get('delta', 0)) if ticker.get('greeks') else 0
                    }
                    _cache[pid] = {'data': data, 'ts': now}
                cached = _cache.get(product_id)
            return cached['data'] if cached else None
        return None
    except Exception as e:
        logger.error(f"Error fetching current price: {e}")
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
        response = requests.get(f'{config.BASE_URL}{path}', headers=headers, timeout=(3, 27))
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
