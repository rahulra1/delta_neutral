import os

BASE_URL = 'https://api.india.delta.exchange'
WS_URL = 'wss://socket.india.delta.exchange'
API_KEY = os.environ.get('DELTA_API_KEY', 'Arj3Ey4TePkxnRbSypkUO4akKXdm4j')
API_SECRET = os.environ.get('DELTA_API_SECRET', '8xmmZ78jhvqaUmBBiohhwq95hCyQ0NUnAwQpCOHQELRag0SCbeyDDfipxM2A')

EXPIRY_DATE = '01-04-2026'
TARGET_DELTA = 0.20
DELTA_TOLERANCE = 0.05
LOT_SIZE = 100
PREMIUM_INCREASE_THRESHOLD = 0.4
TARGET_PNL = 25
MAX_ADJUSTMENTS = 5
MONITORING_INTERVAL = 5
