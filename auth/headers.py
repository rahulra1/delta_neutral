import time
from config import API_KEY, API_SECRET
from auth.signature import generate_signature


def get_headers(method, path, query_string='', payload=''):
    timestamp = str(int(time.time()))
    signature_data = method + timestamp + path + query_string + payload
    signature = generate_signature(API_SECRET, signature_data)
    return {
        'api-key': API_KEY,
        'timestamp': timestamp,
        'signature': signature,
        'User-Agent': 'delta-neutral-bot',
        'Content-Type': 'application/json'
    }
