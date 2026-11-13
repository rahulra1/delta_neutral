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
                logger.warning(f"✗ Order HTTP {response.status_code} for {side.upper()} {size}x {product_symbol}")
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


def get_order(order_id):
    """GET /v2/orders/{id} -> the order dict (or None)."""
    if not order_id:
        return None
    path = f'/v2/orders/{order_id}'
    try:
        headers = get_headers('GET', path, '')
        r = requests.get(f'{config.BASE_URL}{path}', headers=headers, timeout=10)
        r.raise_for_status()
        res = r.json()
        return res.get('result') if res.get('success') else None
    except Exception as e:
        logger.warning(f"get_order {order_id} failed: {e}")
        return None


def order_fill_price(order, fallback=0.0, retries=3):
    """Average EXECUTED price of a (market) order returned by place_order.

    Uses the order response's average_fill_price; if the response does not carry
    it yet, re-reads the order a few times. Falls back to `fallback` (e.g. the
    mark) only when the broker reports no fill price at all."""
    import time as _t

    def _px(o):
        try:
            return float((o or {}).get('average_fill_price') or 0)
        except (TypeError, ValueError):
            return 0.0

    px = _px(order)
    oid = (order or {}).get('id')
    for _ in range(retries if px <= 0 and oid else 0):
        _t.sleep(1)
        px = _px(get_order(oid))
        if px > 0:
            break
    if px <= 0:
        logger.warning(f"⚠ No fill price for order {oid} — using {fallback}")
        return float(fallback or 0)
    return px
