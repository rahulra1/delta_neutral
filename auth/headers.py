import time
from config import get_api_key, get_api_secret
from auth.signature import generate_signature


def get_headers(method, path, query_string='', payload=''):
    timestamp = str(int(time.time()))
    signature_data = method + timestamp + path + query_string + payload
    signature = generate_signature(get_api_secret(), signature_data)
    return {
        'api-key': get_api_key(),
        'timestamp': timestamp,
        'signature': signature,
        'User-Agent': 'delta-neutral-bot',
        'Content-Type': 'application/json'
    }
