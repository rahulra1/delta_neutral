"""Groww broker configuration.

Groww uses its own REST API (growwapi SDK) for NSE/BSE trading.
No BASE_URL or WS_URL needed — the SDK handles endpoints internally.
"""

BASE_URL = 'https://groww.in/trade-api'  # placeholder, SDK handles it
WS_URL = ''  # Not used; Groww has its own feed mechanism
BROKER_NAME = 'Groww'
