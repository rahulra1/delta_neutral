import logging
import time
import threading
import requests
import config
from auth import get_headers

logger = logging.getLogger(__name__)

_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 300  # 5 minutes — product details rarely change


def get_product_details(product_id):
    now = time.time()
    with _cache_lock:
        cached = _cache.get(product_id)
        if cached and now - cached['ts'] < _CACHE_TTL:
            return cached['data']

    path = '/v2/products'
    headers = get_headers('GET', path)
    try:
        response = requests.get(f'{config.BASE_URL}{path}', headers=headers, timeout=(3, 27))
        response.raise_for_status()
        result = response.json()
        if result.get('success'):
            # Cache all products from this response
            with _cache_lock:
                for product in result.get('result', []):
                    pid = product.get('id')
                    data = {
                        'contract_value': float(product.get('contract_value', 0.001)),
                        'symbol': product.get('symbol', ''),
                        'contract_unit_currency': product.get('contract_unit_currency', 'BTC')
                    }
                    _cache[pid] = {'data': data, 'ts': now}
                cached = _cache.get(product_id)
            return cached['data'] if cached else None
        return None
    except Exception as e:
        logger.error(f"Error fetching product details: {e}")
        return None
