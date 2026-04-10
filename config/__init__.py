import os
import threading
from config import delta_exchange, demo

# Registry of available brokers
BROKERS = {
    'delta_exchange': delta_exchange,
    'demo': demo,
}

DEFAULT_BROKER = 'demo'

# Strategy defaults
EXPIRY_DATE = '01-04-2026'
TARGET_DELTA = 0.20
DELTA_TOLERANCE = 0.05
LOT_SIZE = 100
PREMIUM_INCREASE_THRESHOLD = 0.4
TARGET_PNL = 25
MAX_ADJUSTMENTS = 5
MONITORING_INTERVAL = 5

# Thread-local credentials + broker
_thread_local = threading.local()


def set_thread_credentials(api_key, api_secret, broker=None):
    _thread_local.api_key = api_key
    _thread_local.api_secret = api_secret
    if broker:
        _thread_local.broker = broker


def set_thread_broker(broker):
    _thread_local.broker = broker


def _get_broker_module():
    name = getattr(_thread_local, 'broker', DEFAULT_BROKER)
    return BROKERS.get(name, BROKERS[DEFAULT_BROKER])


def get_api_key():
    return getattr(_thread_local, 'api_key',
                   os.environ.get('DELTA_API_KEY', ''))


def get_api_secret():
    return getattr(_thread_local, 'api_secret',
                   os.environ.get('DELTA_API_SECRET', ''))


# Dynamic properties — every module that does `from config import BASE_URL`
# gets the value at import time, so we need a different approach.
# We use a module-level __getattr__ so `config.BASE_URL` resolves per-thread.

def __getattr__(name):
    if name == 'BASE_URL':
        return _get_broker_module().BASE_URL
    if name == 'WS_URL':
        return _get_broker_module().WS_URL
    raise AttributeError(f"module 'config' has no attribute {name}")
