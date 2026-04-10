import requests
import config
from auth import get_headers


def get_positions():
    path = '/v2/positions/margined'
    query_string = '?contract_types=call_options,put_options'
    headers = get_headers('GET', path, query_string)
    try:
        response = requests.get(f'{config.BASE_URL}{path}{query_string}', headers=headers, timeout=(3, 27))
        response.raise_for_status()
        result = response.json()
        return result.get('result', []) if result.get('success') else []
    except Exception as e:
        print(f"Error fetching positions: {e}")
        return []


def get_position_entry_price(product_id):
    for pos in get_positions():
        if pos.get('product_id') == product_id:
            return float(pos.get('entry_price', 0)), int(pos.get('size', 0))
    return None, 0
