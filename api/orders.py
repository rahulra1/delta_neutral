import logging
import json
import requests
import config
from auth import get_headers

logger = logging.getLogger(__name__)


def place_order(product_id, product_symbol, size, side, max_retries=3):
    path = '/v2/orders'
    payload = {
        "product_symbol": product_symbol,
        "size": int(size),
        "side": side,
        "order_type": "market_order"
    }
    if product_id is not None:
        payload["product_id"] = int(product_id)
    payload_str = json.dumps(payload)
    for attempt in range(1, max_retries + 1):
        headers = get_headers('POST', path, '', payload_str)
        try:
            response = requests.post(f'{config.BASE_URL}{path}', data=payload_str, headers=headers, timeout=(3, 27))
            if not response.ok:
                logger.warning(f"✗ Order HTTP {response.status_code} for {side.upper()} {size}x {product_symbol} (id={product_id})")
            response.raise_for_status()
            result = response.json()
            if result.get('success'):
                logger.info(f"✓ Order placed: {side.upper()} {size} lots of {product_symbol}")
                return result.get('result')
            else:
                logger.warning(f"✗ Order failed: {result.get('error')}")
                return None
        except Exception as e:
            try:
                detail = e.response.text if hasattr(e, 'response') and e.response else str(e)
                if hasattr(e, 'response') and e.response and e.response.status_code == 401:
                    logger.warning(f"✗ Order auth error (no retry): {detail}")
                    return None
            except Exception:
                detail = str(e)
            if attempt < max_retries:
                wait = 2 ** attempt
                logger.warning(f"✗ Order attempt {attempt}/{max_retries} failed: {detail} — retrying in {wait}s")
                import time; time.sleep(wait)
            else:
                logger.warning(f"✗ Order failed after {max_retries} attempts: {detail}")
                return None
