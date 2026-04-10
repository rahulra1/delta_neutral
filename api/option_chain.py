import requests
import config
from auth import get_headers


def get_option_chain(expiry_date, asset='BTC'):
    path = '/v2/tickers'
    query_string = f'?contract_types=call_options,put_options&underlying_asset_symbols={asset}&expiry_date={expiry_date}'
    headers = get_headers('GET', path, query_string)
    try:
        response = requests.get(f'{config.BASE_URL}{path}{query_string}', headers=headers, timeout=(3, 27))
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error fetching option chain: {e}")
        return None
