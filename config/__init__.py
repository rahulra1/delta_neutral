import os

BASE_URL = 'https://api.india.delta.exchange'
WS_URL = 'wss://socket.india.delta.exchange'
API_KEY = os.environ.get('DELTA_API_KEY', 'XUWEGMr1URrzzme1o6jBrEzAkUuNH4')
API_SECRET = os.environ.get('DELTA_API_SECRET', 'gNfRcBC2FEeMFlQhwcMI5re8F6ob5KQS70AjjvZw3GPCTfUprDX485NPVDqW')

EXPIRY_DATE = '01-04-2026'
TARGET_DELTA = 0.20
DELTA_TOLERANCE = 0.05
LOT_SIZE = 10
PREMIUM_INCREASE_THRESHOLD = 0.4
TARGET_PNL = 10
MONITORING_INTERVAL = 5
