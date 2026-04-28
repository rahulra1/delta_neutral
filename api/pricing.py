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
