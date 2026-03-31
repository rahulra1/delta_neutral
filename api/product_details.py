import requests
from config import BASE_URL
from auth import get_headers


def get_product_details(product_id):
    path = '/v2/products'
    headers = get_headers('GET', path)
    try:
        response = requests.get(f'{BASE_URL}{path}', headers=headers, timeout=(3, 27))
        response.raise_for_status()
        result = response.json()
        if result.get('success'):
            for product in result.get('result', []):
                if product.get('id') == product_id:
                    return {
                        'contract_value': float(product.get('contract_value', 0.001)),
                        'symbol': product.get('symbol', ''),
                        'contract_unit_currency': product.get('contract_unit_currency', 'BTC')
                    }
        return None
    except Exception as e:
        print(f"Error fetching product details: {e}")
        return None
