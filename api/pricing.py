import requests
from config import BASE_URL
from auth import get_headers


def get_current_price(product_id):
    path = '/v2/tickers'
    headers = get_headers('GET', path)
    try:
        response = requests.get(f'{BASE_URL}{path}', headers=headers, timeout=(3, 27))
        response.raise_for_status()
        tickers = response.json()
        if tickers.get('success'):
            for ticker in tickers.get('result', []):
                if ticker.get('product_id') == product_id:
                    return {
                        'mark_price': float(ticker['mark_price']),
                        'delta': float(ticker.get('greeks', {}).get('delta', 0)) if ticker.get('greeks') else 0
                    }
        return None
    except Exception as e:
        print(f"Error fetching current price: {e}")
        return None
