import time
import logging
import requests
import config
from auth import get_headers

logger = logging.getLogger(__name__)


def _f(v):
    """Safe float conversion — returns 0 for None/empty."""
    try:
        return float(v) if v is not None else 0.0
    except (ValueError, TypeError):
        return 0.0


def get_top_coins_by_volume(limit=50, contract_type='perpetual_futures', sort_by='turnover_usd'):
    """Fetch the top N coins on Delta Exchange ranked by 24h volume.

    Uses the public /v2/tickers endpoint. Each perpetual futures ticker maps
    to one underlying coin.

    Args:
        limit: Number of top coins to return (default 50).
        contract_type: Product type to scan (default 'perpetual_futures').
        sort_by: 'turnover_usd' (USD notional, recommended) or 'volume'.

    Returns:
        List of dicts sorted descending by the chosen metric, each with:
        rank, symbol, coin, volume, turnover_usd, mark_price, oi_value_usd.
    """
    path = '/v2/tickers'
    query_string = f'?contract_types={contract_type}'
    headers = get_headers('GET', path, query_string)
    try:
        resp = requests.get(f'{config.BASE_URL}{path}{query_string}', headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get('success'):
            logger.error("Tickers request unsuccessful: %s", data)
            return []

        coins = []
        for t in data.get('result', []):
            coins.append({
                'symbol': t.get('symbol', ''),
                'coin': t.get('underlying_asset_symbol', ''),
                'volume': _f(t.get('volume')),
                'turnover_usd': _f(t.get('turnover_usd')),
                'mark_price': _f(t.get('mark_price')),
                'oi_value_usd': _f(t.get('oi_value_usd')),
            })

        key = sort_by if sort_by in ('turnover_usd', 'volume') else 'turnover_usd'
        coins.sort(key=lambda c: c[key], reverse=True)

        top = coins[:limit]
        for i, c in enumerate(top, start=1):
            c['rank'] = i
        return top
    except Exception as e:
        logger.error("Error fetching top coins by volume: %s", e)
        return []


def get_top_coins_by_change(limit=15, contract_type='perpetual_futures', direction='abs'):
    """Fetch the top N coins on Delta Exchange by 24h price change.

    Uses the public /v2/tickers endpoint. Ranks by `mark_change_24h`, the
    24-hour percentage change of the mark price.

    Args:
        limit: Number of coins to return (default 15).
        contract_type: Product type to scan (default 'perpetual_futures').
        direction: 'abs' -> biggest movers up or down (default),
                   'gainers' -> largest positive change,
                   'losers'  -> largest negative change.

    Returns:
        List of dicts sorted by the chosen ranking, each with:
        rank, coin, symbol, change_24h (signed %), mark_price. [] on failure.
    """
    path = '/v2/tickers'
    query_string = f'?contract_types={contract_type}'
    headers = get_headers('GET', path, query_string)
    try:
        resp = requests.get(f'{config.BASE_URL}{path}{query_string}', headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get('success'):
            logger.error("Tickers request unsuccessful: %s", data)
            return []

        coins = []
        for t in data.get('result', []):
            coins.append({
                'symbol': t.get('symbol', ''),
                'coin': t.get('underlying_asset_symbol', ''),
                'change_24h': _f(t.get('mark_change_24h')),
                'mark_price': _f(t.get('mark_price')),
            })

        if direction == 'gainers':
            coins.sort(key=lambda c: c['change_24h'], reverse=True)
        elif direction == 'losers':
            coins.sort(key=lambda c: c['change_24h'])
        else:  # 'abs' — biggest movers in either direction
            coins.sort(key=lambda c: abs(c['change_24h']), reverse=True)

        top = coins[:limit]
        for i, c in enumerate(top, start=1):
            c['rank'] = i
        return top
    except Exception as e:
        logger.error("Error fetching top coins by change: %s", e)
        return []


def _ema(values, period):
    """Exponential moving average of a list of closes (oldest-first)."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    # Seed with the simple average of the first `period` values.
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def get_candles(symbol, resolution='1h', lookback_bars=200):
    """Fetch OHLCV candles for a symbol, returned oldest-first.

    Uses the public /v2/history/candles endpoint (no auth required).
    """
    seconds_per_bar = {
        '1m': 60, '5m': 300, '15m': 900, '30m': 1800,
        '1h': 3600, '2h': 7200, '4h': 14400, '1d': 86400,
    }.get(resolution, 3600)
    end = int(time.time())
    # Pad the window so we reliably get enough bars despite gaps.
    start = end - seconds_per_bar * lookback_bars * 3
    path = '/v2/history/candles'
    params = {'resolution': resolution, 'symbol': symbol, 'start': start, 'end': end}
    try:
        resp = requests.get(f'{config.BASE_URL}{path}', params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data.get('success'):
            return []
        result = data.get('result', [])
        # API returns newest-first; sort oldest-first for EMA math.
        result.sort(key=lambda c: c.get('time', 0))
        return result
    except Exception as e:
        logger.error("Error fetching candles for %s: %s", symbol, e)
        return []


def ema_crossover_direction(symbol, resolution='1h', fast=20, slow=50):
    """Determine trend direction via a fast/slow EMA crossover.

    Returns a dict with ema_fast, ema_slow, and direction:
        'BULLISH' if fast EMA > slow EMA,
        'BEARISH' if fast EMA < slow EMA,
        'NEUTRAL' if effectively equal,
        'NO_DATA' if not enough candles.
    """
    candles = get_candles(symbol, resolution=resolution, lookback_bars=slow * 3)
    closes = [_f(c.get('close')) for c in candles]
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    if ema_fast is None or ema_slow is None:
        return {'symbol': symbol, 'direction': 'NO_DATA',
                'ema_fast': None, 'ema_slow': None, 'bars': len(closes)}
    if ema_slow == 0:
        spread_pct = 0.0
    else:
        spread_pct = (ema_fast - ema_slow) / ema_slow * 100
    if abs(spread_pct) < 0.05:
        direction = 'NEUTRAL'
    elif ema_fast > ema_slow:
        direction = 'BULLISH'
    else:
        direction = 'BEARISH'
    return {'symbol': symbol, 'direction': direction,
            'ema_fast': ema_fast, 'ema_slow': ema_slow,
            'spread_pct': spread_pct, 'bars': len(closes)}


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    top = get_top_coins_by_change(limit=15, direction='gainers')
    print(f"{'#':>3}  {'COIN':<10} {'EMA20':>12} {'EMA50':>12} {'SPREAD':>9}  DIRECTION")
    print('-' * 62)
    for c in top:
        r = ema_crossover_direction(c['symbol'], resolution='1d', fast=20, slow=50)
        if r['direction'] == 'NO_DATA':
            print(f"{c['rank']:>3}  {c['coin']:<10} {'-':>12} {'-':>12} {'-':>9}  NO_DATA")
            continue
        print(f"{c['rank']:>3}  {c['coin']:<10} {r['ema_fast']:>12,.4f} "
              f"{r['ema_slow']:>12,.4f} {r['spread_pct']:>+8.2f}%  {r['direction']}")
