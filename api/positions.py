import logging
import requests
import config
from auth import get_headers

logger = logging.getLogger(__name__)


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
        logger.error(f"Error fetching positions: {e}")
        return None


def get_position_entry_price(product_id):
    positions = get_positions()
    if not positions:
        return None, 0
    for pos in positions:
        if pos.get('product_id') == product_id:
            return float(pos.get('entry_price', 0)), int(pos.get('size', 0))
    return None, 0
