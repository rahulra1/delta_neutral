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
    """Fetch an order by id (GET /v2/orders/{id}). Returns the order dict or
    None. Raises nothing — errors are logged and None returned."""
    if order_id is None:
        return None
    path = f'/v2/orders/{order_id}'
    try:
        headers = get_headers('GET', path, '', '')
        r = requests.get(f'{config.BASE_URL}{path}', headers=headers, timeout=(3, 10))
        if not r.ok:
            logger.warning(f"✗ get_order HTTP {r.status_code} for order {order_id}")
            return None
        j = r.json()
        return j.get('result') if j.get('success') else None
    except Exception as e:
        logger.warning(f"✗ get_order failed for {order_id}: {e}")
        return None


def _parse_fill(order):
    """(avg_fill_price, filled_size, commission) from an order dict, or
    (None, ...) when it has no fill yet."""
    if not order:
        return None, 0, 0.0
    try:
        px = order.get('average_fill_price')
        px = float(px) if px not in (None, '', '0', 0) else None
    except (TypeError, ValueError):
        px = None
    try:
        size = int(order.get('size') or 0)
        unfilled = int(order.get('unfilled_size') or 0)
        filled = max(size - unfilled, 0)
    except (TypeError, ValueError):
        filled = 0
    try:
        comm = float(order.get('paid_commission') or 0)
    except (TypeError, ValueError):
        comm = 0.0
    return px, filled, comm


def get_fill_price(order, fallback=None, attempts=5, delay=0.6):
    """Resolve the EXECUTED price of an order placed via place_order().

    `order` is the dict returned by place_order (must contain 'id'). A market
    order's create response may not yet carry average_fill_price, so the order
    is re-fetched by id until it reports a fill (or attempts run out).

    Returns a dict:
        {'order_id', 'price', 'filled_size', 'commission', 'source'}
    where source is 'fill' when the executed price was obtained, otherwise
    'fallback' (price = `fallback`, e.g. the mark at order time) — logged so a
    fallback never happens silently.
    """
    import time
    order_id = (order or {}).get('id')
    px, filled, comm = _parse_fill(order)
    state = (order or {}).get('state')
    n = 0
    # Re-poll by id while no fill price, or while still partially filled/open.
    while order_id is not None and n < attempts and (
            px is None or (state == 'open' and filled < int((order or {}).get('size') or 0))):
        n += 1
        time.sleep(delay)
        fresh = get_order(order_id)
        if fresh:
            order = fresh
            px, filled, comm = _parse_fill(fresh)
            state = fresh.get('state')
    if px is not None:
        return {'order_id': order_id, 'price': px, 'filled_size': filled,
                'commission': comm, 'source': 'fill'}
    logger.warning(f"⚠ No executed price for order {order_id} after {n} polls — "
                   f"falling back to {fallback}")
    return {'order_id': order_id, 'price': float(fallback or 0),
            'filled_size': filled, 'commission': comm, 'source': 'fallback'}
