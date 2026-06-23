import re
import logging
import requests
from datetime import datetime, timedelta
import config
from auth import get_headers

logger = logging.getLogger(__name__)


def get_expiries(asset='BTC', min_days=0):
    """Fetch available option expiry dates.

    Daily options on Delta Exchange expire at 5:30 PM IST (12:00 UTC).
    Today's expiry is included if current time is before 5:30 PM IST.
    """
    path = '/v2/products'
    query_string = '?contract_types=call_options&states=live&page_size=500'
    headers = get_headers('GET', path, query_string)
    try:
        resp = requests.get(f'{config.BASE_URL}{path}{query_string}', headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get('success'):
            return []
        now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        # Today's expiry is valid until 5:30 PM IST
        expiry_cutoff_hour = 17
        expiry_cutoff_minute = 30
        if now_ist.hour < expiry_cutoff_hour or (now_ist.hour == expiry_cutoff_hour and now_ist.minute < expiry_cutoff_minute):
            cutoff_date = now_ist.date()
        else:
            cutoff_date = now_ist.date() + timedelta(days=1)
        cutoff_date += timedelta(days=min_days)
        expiries = set()
        for p in data.get('result', []):
            sym = p.get('symbol', '')
            if asset not in sym:
                continue
            m = re.search(r'-(\d{6})$', sym)
            if not m:
                continue
            try:
                exp_dt = datetime.strptime(m.group(1), '%d%m%y')
            except ValueError:
                continue
            if exp_dt.date() >= cutoff_date:
                expiries.add((exp_dt, exp_dt.strftime('%d-%m-%Y')))
        return [e[1] for e in sorted(expiries, key=lambda x: x[0])]
    except Exception as e:
        logger.error(f"Error fetching expiries: {e}")
        return []


def _f(v):
    """Safe float conversion — returns 0 for None/empty."""
    try:
        return float(v) if v is not None else 0
    except (ValueError, TypeError):
        return 0


def get_option_chain_full(expiry_date, asset='BTC'):
    """Fetch full option chain with OI, greeks, bid/ask for a given expiry."""
    path = '/v2/tickers'
    query_string = f'?contract_types=call_options,put_options&underlying_asset_symbols={asset}&expiry_date={expiry_date}'
    headers = get_headers('GET', path, query_string)
    try:
        resp = requests.get(f'{config.BASE_URL}{path}{query_string}', headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get('success'):
            return None, None, None

        calls = {}
        puts = {}
        spot_price = None

        for t in data.get('result', []):
            strike = t.get('strike_price')
            if not strike:
                continue
            strike = str(strike)
            greeks = t.get('greeks') or {}
            quotes = t.get('quotes') or {}
            if spot_price is None and t.get('spot_price'):
                spot_price = _f(t['spot_price'])

            row = {
                'symbol': t.get('symbol', ''),
                'product_id': t.get('product_id'),
                'strike': strike,
                'mark_price': _f(t.get('mark_price')),
                'oi': t.get('oi') or '0',
                'volume': t.get('volume') or 0,
                'iv': _f(t.get('mark_vol')),
                'delta': _f(greeks.get('delta')),
                'gamma': _f(greeks.get('gamma')),
                'theta': _f(greeks.get('theta')),
                'vega': _f(greeks.get('vega')),
                'bid': _f(quotes.get('best_bid')),
                'ask': _f(quotes.get('best_ask')),
                'bid_size': quotes.get('bid_size') or '0',
                'ask_size': quotes.get('ask_size') or '0',
                'bid_iv': _f(quotes.get('bid_iv')),
                'ask_iv': _f(quotes.get('ask_iv')),
            }

            ct = t.get('contract_type', '')
            if 'call' in ct:
                calls[strike] = row
            elif 'put' in ct:
                puts[strike] = row

        # Build unified chain sorted by strike
        strikes = sorted(set(list(calls.keys()) + list(puts.keys())), key=lambda s: float(s))
        chain = []
        for s in strikes:
            chain.append({
                'strike': s,
                'call': calls.get(s),
                'put': puts.get(s),
            })

        return chain, spot_price, expiry_date
    except Exception as e:
        logger.error(f"Error fetching option chain: {e}")
        return None, None, None
