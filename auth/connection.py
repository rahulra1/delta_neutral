import requests
import config
from auth.headers import get_headers


def check_api_connection():
    print("=" * 60)
    print("CHECKING API CONNECTION")
    print("=" * 60)

    try:
        response = requests.get(f'{config.BASE_URL}/v2/tickers/BTCUSD', timeout=(3, 27))
        if response.status_code == 200:
            print("✓ Public API accessible")
        else:
            print(f"✗ Public API error: {response.status_code}")
    except Exception as e:
        print(f"✗ Cannot reach Delta Exchange API: {e}")
        return False

    try:
        path = '/v2/wallet/balances'
        headers = get_headers('GET', path)
        response = requests.get(f'{config.BASE_URL}{path}', headers=headers, timeout=(3, 27))

        if response.status_code == 200:
            print("✓ API authentication successful")
            print("✓ IP address is whitelisted")
            return True
        elif response.status_code == 401:
            data = response.json()
            error_code = data.get('error', {}).get('code', '')
            if error_code == 'ip_not_whitelisted_for_api_key':
                client_ip = data.get('error', {}).get('context', {}).get('client_ip', 'unknown')
                print(f"✗ IP NOT WHITELISTED")
                print(f"Your current IP: {client_ip}")
                print(f"To fix: Go to delta.exchange → Account Settings → API Keys → Add IP: {client_ip}")
                return False
            else:
                print(f"✗ Authentication failed: {error_code}")
                return False
        else:
            print(f"✗ Unexpected response: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"✗ Authentication check failed: {e}")
        return False
