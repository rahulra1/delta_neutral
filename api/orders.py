import json
import requests
from config import BASE_URL
from auth import get_headers


def place_order(product_id, product_symbol, size, side):
    path = '/v2/orders'
    payload = {
        "product_id": product_id,
        "product_symbol": product_symbol,
        "size": size,
        "side": side,
        "order_type": "market_order"
    }
    payload_str = json.dumps(payload)
    headers = get_headers('POST', path, '', payload_str)
    try:
        response = requests.post(f'{BASE_URL}{path}', data=payload_str, headers=headers, timeout=(3, 27))
        response.raise_for_status()
        result = response.json()
        if result.get('success'):
            print(f"✓ Order placed: {side.upper()} {size} lots of {product_symbol}")
            return result.get('result')
        else:
            print(f"✗ Order failed: {result.get('error')}")
            return None
    except Exception as e:
        print(f"✗ Error placing order: {e}")
        return None
