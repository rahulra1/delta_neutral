import re
import requests
from datetime import datetime, timedelta
from config import BASE_URL
from auth import get_headers


def get_expiries(asset='BTC', min_days=0):
    """Fetch available option expiry dates."""
    path = '/v2/products'
    query_string = '?contract_types=call_options&states=live&page_size=500'
    headers = get_headers('GET', path, query_string)
    try:
        resp = requests.get(f'{BASE_URL}{path}{query_string}', headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get('success'):
            return []
        cutoff = datetime.utcnow() + timedelta(days=min_days)
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
            if exp_dt >= cutoff:
                expiries.add((exp_dt, exp_dt.strftime('%d-%m-%Y')))
        return [e[1] for e in sorted(expiries, key=lambda x: x[0])]
    except Exception as e:
        print(f"Error fetching expiries: {e}")
        return []


def get_option_chain_full(expiry_date, asset='BTC'):
    """Fetch full option chain with OI, greeks, bid/ask for a given expiry."""
    path = '/v2/tickers'
    query_string = f'?contract_types=call_options,put_options&underlying_asset_symbols={asset}&expiry_date={expiry_date}'
    headers = get_headers('GET', path, query_string)
    try:
        resp = requests.get(f'{BASE_URL}{path}{query_string}', headers=headers, timeout=10)
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
                spot_price = float(t['spot_price'])

            row = {
                'symbol': t.get('symbol', ''),
                'product_id': t.get('product_id'),
                'strike': strike,
                'mark_price': float(t.get('mark_price', 0)),
                'oi': t.get('oi', '0'),
                'volume': t.get('volume', 0),
                'iv': float(t.get('mark_vol', 0)),
                'delta': float(greeks.get('delta', 0)),
                'gamma': float(greeks.get('gamma', 0)),
                'theta': float(greeks.get('theta', 0)),
                'vega': float(greeks.get('vega', 0)),
                'bid': float(quotes.get('best_bid', 0)),
                'ask': float(quotes.get('best_ask', 0)),
                'bid_size': quotes.get('bid_size', '0'),
                'ask_size': quotes.get('ask_size', '0'),
                'bid_iv': float(quotes.get('bid_iv', 0)),
                'ask_iv': float(quotes.get('ask_iv', 0)),
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
        print(f"Error fetching option chain: {e}")
        return None, None, None
