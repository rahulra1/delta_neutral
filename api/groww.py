"""Groww API integration for NSE option chain and order execution.

Uses the official `growwapi` SDK. Falls back to direct REST calls if SDK unavailable.
Authentication: TOTP token → access_token, or pre-generated access_token stored in profile.
"""

import logging
import threading
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_lock = threading.Lock()
_client = None
_client_token = None  # track which token was used to avoid re-init
_client_created_at = 0  # timestamp of last auth
_CLIENT_TTL = 3600  # re-authenticate every 1 hour (Groww tokens expire)

# Cache
_chain_cache = {}  # {(symbol, expiry): {data, ts}}
_CACHE_TTL = 15  # seconds

# BSE symbols that need exchange='BSE' instead of 'NSE'
_BSE_SYMBOLS = {'SENSEX', 'BANKEX'}


def _get_exchange(symbol):
    """Return the correct exchange for a symbol."""
    return 'BSE' if symbol in _BSE_SYMBOLS else 'NSE'

# Auth error indicators
_AUTH_ERROR_KEYWORDS = ('authentication failed', 'expired', 'invalid', 'unauthorized', '401')


def _is_auth_error(error):
    """Check if an exception indicates an expired/invalid token."""
    err_str = str(error).lower()
    return any(kw in err_str for kw in _AUTH_ERROR_KEYWORDS)


def _get_client(api_key=None, api_secret=None, force_refresh=False):
    """Get or create GrowwAPI client. Thread-safe singleton per token.

    Re-authenticates automatically when the token TTL expires or when
    force_refresh=True (e.g., after an auth error).

    Auth flows:
    1. TOTP flow: api_key = TOTP JWT token, api_secret = TOTP secret (base32)
       → generates 6-digit OTP, exchanges for access_token
    2. API Key + Secret flow: api_key = API key, api_secret = API secret
       → uses checksum-based auth (requires daily approval on Groww dashboard)
    3. Direct access token: api_key = access_token, api_secret = empty
       → uses token directly (e.g., already exchanged externally)
    """
    global _client, _client_token, _client_created_at
    from config import get_api_key, get_api_secret

    token = api_key or get_api_key()
    secret = api_secret or get_api_secret()

    with _lock:
        # Reuse cached client if token matches, TTL hasn't expired, and no forced refresh
        if _client and _client_token == token and not force_refresh:
            if (time.time() - _client_created_at) < _CLIENT_TTL:
                return _client
            else:
                logger.info("Groww: token TTL expired, re-authenticating...")

        try:
            from growwapi import GrowwAPI
        except ImportError:
            logger.error("growwapi SDK not installed. Run: pip install growwapi")
            raise RuntimeError("growwapi SDK not installed. Run: pip install growwapi")

        access_token = None

        if secret and secret.strip():
            # Determine if secret is a base32 TOTP secret or an API secret
            is_base32 = all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=' for c in secret.strip().upper())
            is_short = len(secret.strip()) <= 64  # TOTP secrets are typically 16-32 chars

            if is_base32 and is_short:
                # TOTP flow: secret is a base32 TOTP secret
                try:
                    import pyotp
                    totp_gen = pyotp.TOTP(secret.strip())
                    totp = totp_gen.now()
                    access_token = GrowwAPI.get_access_token(api_key=token, totp=totp)
                    logger.info("Groww: authenticated via TOTP flow")
                except Exception as e:
                    logger.warning(f"Groww TOTP auth failed ({e})")
            else:
                # API Key + Secret flow (approval-based)
                try:
                    access_token = GrowwAPI.get_access_token(api_key=token, secret=secret.strip())
                    logger.info("Groww: authenticated via API Key + Secret flow")
                except Exception as e:
                    logger.warning(f"Groww API Key+Secret auth failed ({e})")

        if not access_token:
            # Last resort: use token directly as access_token
            logger.info("Groww: using token directly as access_token")
            access_token = token

        _client = GrowwAPI(access_token)
        _client_token = token
        _client_created_at = time.time()
        logger.info("Groww API client initialized")
        return _client


def check_connection(api_key=None, api_secret=None, symbol=None):
    """Verify Groww API connectivity. Returns True if working.
    If symbol is provided, checks LTP for that specific asset first.
    Falls back to NIFTY (NSE) if the symbol-specific check fails.
    """
    try:
        client = _get_client(api_key, api_secret)
        # Try the selected symbol first, then fallback to NIFTY
        symbols_to_try = []
        if symbol:
            symbols_to_try.append(symbol)
        if symbol != 'NIFTY':
            symbols_to_try.append('NIFTY')

        for check_sym in symbols_to_try:
            try:
                exchange = _get_exchange(check_sym)
                exchange_symbol = f'{exchange}_{check_sym}'
                resp = client.get_ltp(
                    segment='CASH',
                    exchange_trading_symbols=exchange_symbol
                )
                if resp and exchange_symbol in resp:
                    logger.info(f"Groww connection OK. {check_sym} LTP: {resp[exchange_symbol]}")
                    return True
            except Exception:
                continue
        return False
    except Exception as e:
        logger.warning(f"Groww connection check failed: {e}")
        return False


def get_groww_expiries(symbol, year=None, month=None):
    """Get expiry dates for a symbol from Groww API.

    Returns list of expiry dates in DD-MM-YYYY format (matching existing NSE format).
    Auto-retries with fresh authentication if the token has expired.
    """
    exchange = _get_exchange(symbol)
    for attempt in range(2):  # max 1 retry after re-auth
        try:
            client = _get_client(force_refresh=(attempt > 0))
            now = datetime.now(IST)
            y = year or now.year

            # Only pass month if explicitly provided; otherwise fetch all expiries for the year
            kwargs = {'exchange': exchange, 'underlying_symbol': symbol, 'year': y}
            if month:
                kwargs['month'] = month

            resp = client.get_expiries(**kwargs)
            expiries_raw = resp.get('expiries', [])

            # Filter to future expiries only
            today_str = now.strftime('%Y-%m-%d')
            future = [e for e in expiries_raw if e >= today_str]

            # If year is almost over and we don't have enough, also fetch next year
            if len(future) < 2 and not year and not month:
                resp2 = client.get_expiries(
                    exchange=exchange,
                    underlying_symbol=symbol,
                    year=y + 1
                )
                next_year_expiries = [e for e in resp2.get('expiries', []) if e >= today_str]
                future = sorted(set(future + next_year_expiries))

            # Convert from YYYY-MM-DD to DD-MM-YYYY (project standard)
            result = []
            for e in sorted(future):
                try:
                    dt = datetime.strptime(e, '%Y-%m-%d')
                    result.append(dt.strftime('%d-%m-%Y'))
                except Exception:
                    continue

            return result
        except Exception as e:
            if attempt == 0 and _is_auth_error(e):
                logger.warning(f"Groww token expired for get_expiries({symbol}), re-authenticating...")
                continue
            logger.error(f"Groww get_expiries error for {symbol}: {e}")
            return []


def get_groww_chain(symbol, expiry_date):
    """Fetch option chain from Groww API.

    Args:
        symbol: Underlying symbol (NIFTY, BANKNIFTY, SENSEX, etc.)
        expiry_date: Date in DD-MM-YYYY format (project standard)

    Returns:
        (chain, spot, expiry) matching the format of api.nse.get_nse_chain
        chain: list of {strike, call, put} dicts
        spot: underlying LTP
        expiry: expiry date string

    Auto-retries with fresh authentication if the token has expired.
    """
    # Check cache
    cache_key = (symbol, expiry_date)
    cached = _chain_cache.get(cache_key)
    if cached and time.time() - cached['ts'] < _CACHE_TTL:
        return cached['chain'], cached['spot'], cached['expiry']

    exchange = _get_exchange(symbol)
    for attempt in range(2):  # max 1 retry after re-auth
        try:
            client = _get_client(force_refresh=(attempt > 0))

            # Convert DD-MM-YYYY → YYYY-MM-DD for Groww API
            try:
                exp_dt = datetime.strptime(expiry_date, '%d-%m-%Y')
                groww_expiry = exp_dt.strftime('%Y-%m-%d')
            except Exception:
                logger.error(f"Invalid expiry format: {expiry_date}")
                return None, None, None

            resp = client.get_option_chain(
                exchange=exchange,
                underlying=symbol,
                expiry_date=groww_expiry
            )

            if not resp:
                logger.warning(f"Groww: empty option chain response for {symbol} {expiry_date}")
                return None, None, None

            spot = resp.get('underlying_ltp', 0)
            strikes_data = resp.get('strikes', {})

            if not strikes_data:
                logger.warning(f"Groww: no strikes for {symbol} expiry {expiry_date}")
                return None, spot, None

            # Convert to project-standard chain format
            chain = []
            for strike_price, contracts in strikes_data.items():
                row = {'strike': str(strike_price), 'call': None, 'put': None}

                ce = contracts.get('CE')
                if ce:
                    greeks = ce.get('greeks', {})
                    row['call'] = {
                        'symbol': ce.get('trading_symbol', f"{symbol}-CE-{strike_price}-{expiry_date}"),
                        'trading_symbol': ce.get('trading_symbol', ''),
                        'product_id': None,
                        'strike': str(strike_price),
                        'mark_price': ce.get('ltp', 0),
                        'oi': str(int(ce.get('open_interest', 0))),
                        'volume': int(ce.get('volume', 0)),
                        'iv': greeks.get('iv', 0) / 100 if greeks.get('iv', 0) > 1 else greeks.get('iv', 0),
                        'delta': greeks.get('delta', 0),
                        'gamma': greeks.get('gamma', 0),
                        'theta': greeks.get('theta', 0),
                        'vega': greeks.get('vega', 0),
                        'bid': 0,
                        'ask': 0,
                        'bid_size': '0',
                        'ask_size': '0',
                        'change': 0,
                        'pchange': 0,
                    }

                pe = contracts.get('PE')
                if pe:
                    greeks = pe.get('greeks', {})
                    row['put'] = {
                        'symbol': pe.get('trading_symbol', f"{symbol}-PE-{strike_price}-{expiry_date}"),
                        'trading_symbol': pe.get('trading_symbol', ''),
                        'product_id': None,
                        'strike': str(strike_price),
                        'mark_price': pe.get('ltp', 0),
                        'oi': str(int(pe.get('open_interest', 0))),
                        'volume': int(pe.get('volume', 0)),
                        'iv': greeks.get('iv', 0) / 100 if greeks.get('iv', 0) > 1 else greeks.get('iv', 0),
                        'delta': greeks.get('delta', 0),
                        'gamma': greeks.get('gamma', 0),
                        'theta': greeks.get('theta', 0),
                        'vega': greeks.get('vega', 0),
                        'bid': 0,
                        'ask': 0,
                        'bid_size': '0',
                        'ask_size': '0',
                        'change': 0,
                        'pchange': 0,
                    }

                chain.append(row)

            # Sort by strike
            chain.sort(key=lambda r: float(r['strike']))

            # Cache
            _chain_cache[cache_key] = {'chain': chain, 'spot': spot, 'expiry': expiry_date, 'ts': time.time()}

            return chain, spot, expiry_date

        except Exception as e:
            if attempt == 0 and _is_auth_error(e):
                logger.warning(f"Groww token expired for get_option_chain({symbol}), re-authenticating...")
                continue
            logger.error(f"Groww get_option_chain error for {symbol} {expiry_date}: {e}")
            return None, None, None


def get_spot_price(symbol):
    """Get current spot/LTP for an underlying symbol."""
    exchange = _get_exchange(symbol)
    exchange_symbol = f'{exchange}_{symbol}'
    for attempt in range(2):
        try:
            client = _get_client(force_refresh=(attempt > 0))
            resp = client.get_ltp(
                segment='CASH',
                exchange_trading_symbols=exchange_symbol
            )
            return resp.get(exchange_symbol, 0)
        except Exception as e:
            if attempt == 0 and _is_auth_error(e):
                logger.warning(f"Groww token expired for get_spot({symbol}), re-authenticating...")
                continue
            logger.error(f"Groww get_spot error for {symbol}: {e}")
            return None


def get_option_ltp(trading_symbol):
    """Get LTP for a specific FNO trading symbol."""
    for attempt in range(2):
        try:
            client = _get_client(force_refresh=(attempt > 0))
            resp = client.get_ltp(
                segment='FNO',
                exchange_trading_symbols=f'NSE_{trading_symbol}'
            )
            return resp.get(f'NSE_{trading_symbol}', 0)
        except Exception as e:
            if attempt == 0 and _is_auth_error(e):
                logger.warning(f"Groww token expired for get_option_ltp({trading_symbol}), re-authenticating...")
                continue
            logger.error(f"Groww get_option_ltp error for {trading_symbol}: {e}")
            return None


def get_option_ltps(trading_symbols):
    """Get LTPs for multiple FNO trading symbols. Max 50 per call."""
    for attempt in range(2):
        try:
            client = _get_client(force_refresh=(attempt > 0))
            keys = tuple(f'NSE_{ts}' for ts in trading_symbols)
            resp = client.get_ltp(
                segment='FNO',
                exchange_trading_symbols=keys
            )
            # Return {original_symbol: ltp}
            result = {}
            for ts in trading_symbols:
                result[ts] = resp.get(f'NSE_{ts}', 0)
            return result
        except Exception as e:
            if attempt == 0 and _is_auth_error(e):
                logger.warning("Groww token expired for get_option_ltps, re-authenticating...")
                continue
            logger.error(f"Groww get_option_ltps error: {e}")
            return {}


# ── Order Placement ──


def place_order(trading_symbol, quantity, transaction_type, order_type='MARKET',
                product='NRML', price=None, trigger_price=None):
    """Place an order through Groww.

    Args:
        trading_symbol: Exchange trading symbol (e.g., NIFTY25JUL24500CE, SENSEX26JUL78000CE)
        quantity: Number of contracts
        transaction_type: 'BUY' or 'SELL'
        order_type: 'MARKET', 'LIMIT', 'SL', 'SL_M'
        product: 'NRML' (overnight) or 'MIS' (intraday)
        price: Required for LIMIT/SL orders
        trigger_price: Required for SL/SL_M orders

    Returns:
        dict with groww_order_id, order_status, remark
    """
    # Determine exchange from trading symbol — BSE symbols start with SENSEX or BANKEX
    exchange = 'BSE' if any(trading_symbol.startswith(s) for s in _BSE_SYMBOLS) else 'NSE'

    for attempt in range(2):
        try:
            client = _get_client(force_refresh=(attempt > 0))
            kwargs = dict(
                trading_symbol=trading_symbol,
                quantity=quantity,
                validity='DAY',
                exchange=exchange,
                segment='FNO',
                product=product,
                order_type=order_type,
                transaction_type=transaction_type,
            )
            if price is not None:
                kwargs['price'] = price
            if trigger_price is not None:
                kwargs['trigger_price'] = trigger_price

            resp = client.place_order(**kwargs)
            logger.info(f"Groww order placed: {resp}")
            return resp
        except Exception as e:
            if attempt == 0 and _is_auth_error(e):
                logger.warning(f"Groww token expired for place_order({trading_symbol}), re-authenticating...")
                continue
            logger.error(f"Groww place_order error: {e}")
            return {'error': str(e)}


def cancel_order(groww_order_id):
    """Cancel an open order."""
    try:
        client = _get_client()
        resp = client.cancel_order(
            segment='FNO',
            groww_order_id=groww_order_id
        )
        return resp
    except Exception as e:
        logger.error(f"Groww cancel_order error: {e}")
        return {'error': str(e)}


def get_order_status(groww_order_id):
    """Get status of an order."""
    try:
        client = _get_client()
        resp = client.get_order_status(
            groww_order_id=groww_order_id,
            segment='FNO'
        )
        return resp
    except Exception as e:
        logger.error(f"Groww get_order_status error: {e}")
        return {'error': str(e)}


# Order status buckets (Groww StocksOrderStatus enum).
_FILLED_STATUSES = {'EXECUTED', 'COMPLETED'}
_FAILED_STATUSES = {'REJECTED', 'FAILED', 'CANCELLED'}
# NEW, ACKED, APPROVED, TRIGGER_PENDING, DELIVERY_AWAITED, *_REQUESTED -> still pending


def _extract_status(resp):
    """Pull the order_status string out of a place/status response dict."""
    if not isinstance(resp, dict):
        return None
    return (resp.get('order_status') or resp.get('orderStatus')
            or resp.get('status'))


def confirm_order_filled(groww_order_id, timeout=20, poll_interval=2):
    """Poll Groww until an order reaches a terminal state.

    Returns (filled: bool, status: str). `filled` is True only when the broker
    confirms EXECUTED/COMPLETED. On REJECTED/FAILED/CANCELLED or timeout it
    returns False, so callers must NOT record a position or start monitoring.
    """
    import time as _time
    if not groww_order_id:
        return False, 'no_order_id'
    deadline = _time.time() + timeout
    last_status = 'UNKNOWN'
    while _time.time() < deadline:
        resp = get_order_status(groww_order_id)
        if isinstance(resp, dict) and resp.get('error'):
            last_status = f"error:{resp['error']}"
            _time.sleep(poll_interval)
            continue
        status = _extract_status(resp)
        if status:
            last_status = status
            up = status.upper()
            if up in _FILLED_STATUSES:
                return True, up
            if up in _FAILED_STATUSES:
                return False, up
        _time.sleep(poll_interval)
    return False, f'timeout:{last_status}'


def get_positions():
    """Get open positions from Groww."""
    try:
        client = _get_client()
        resp = client.get_positions()
        return resp
    except Exception as e:
        logger.error(f"Groww get_positions error: {e}")
        return None


def reset_client():
    """Reset the cached client (e.g., after token refresh)."""
    global _client, _client_token, _client_created_at
    with _lock:
        _client = None
        _client_token = None
        _client_created_at = 0
