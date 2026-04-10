"""Chart data API — candles from Yahoo Finance + auto-detected market structure."""

import requests
import time
import threading

_lock = threading.Lock()
_session = None
_crumb = None

YAHOO_CHART_SYMBOLS = {
    'NIFTY': '%5ENSEI', 'BANKNIFTY': '%5ENSEBANK',
    'FINNIFTY': 'NIFTY_FIN_SERVICE.NS', 'MIDCPNIFTY': 'NIFTY_MIDCAP_SELECT.NS',
    'SENSEX': '%5EBSESN', 'BANKEX': 'BSE-BANK.BO',
    'BTC': 'BTC-USD', 'ETH': 'ETH-USD',
}

RANGES = {'15m': '60d', '1h': '6mo', '1d': '2y'}


def _get_session():
    global _session, _crumb
    with _lock:
        if _session and _crumb:
            return _session, _crumb
        _session = requests.Session()
        _session.headers['User-Agent'] = 'Mozilla/5.0'
        try:
            _session.get('https://fc.yahoo.com', timeout=8)
            r = _session.get('https://query2.finance.yahoo.com/v1/test/getcrumb', timeout=8)
            if r.ok:
                _crumb = r.text.strip()
        except Exception:
            pass
        return _session, _crumb


def get_candles(symbol, interval='1h'):
    """Fetch OHLCV candles from Yahoo Finance."""
    ysym = YAHOO_CHART_SYMBOLS.get(symbol)
    if not ysym:
        return None
    s, crumb = _get_session()
    if not crumb:
        return None
    rng = RANGES.get(interval, '5d')
    try:
        r = s.get(f'https://query2.finance.yahoo.com/v8/finance/chart/{ysym}?interval={interval}&range={rng}&crumb={crumb}', timeout=10)
        if not r.ok:
            return None
        result = r.json().get('chart', {}).get('result', [{}])[0]
        ts = result.get('timestamp', [])
        q = result.get('indicators', {}).get('quote', [{}])[0]
        if not ts:
            return None
        candles = []
        for i in range(len(ts)):
            o, h, l, c, v = q.get('open', [None]*len(ts))[i], q.get('high', [None]*len(ts))[i], q.get('low', [None]*len(ts))[i], q.get('close', [None]*len(ts))[i], q.get('volume', [None]*len(ts))[i]
            if o is None or c is None:
                continue
            candles.append({'t': ts[i], 'o': round(o, 2), 'h': round(h, 2), 'l': round(l, 2), 'c': round(c, 2), 'v': v or 0})
        return candles
    except Exception:
        return None


def detect_structure(candles, lookback=5):
    """Detect swing highs/lows, trend, and S/R zones from candles."""
    if not candles or len(candles) < lookback * 2 + 1:
        return {'swings': [], 'trend': 'Ranging', 'sr_zones': [], 'signal': ''}

    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    swings = []

    # Detect swing highs and lows
    for i in range(lookback, len(candles) - lookback):
        # Swing high: highest high in window
        if highs[i] == max(highs[i - lookback:i + lookback + 1]):
            swings.append({'type': 'HH', 'index': i, 'price': highs[i], 'time': candles[i]['t']})
        # Swing low: lowest low in window
        if lows[i] == min(lows[i - lookback:i + lookback + 1]):
            swings.append({'type': 'LL', 'index': i, 'price': lows[i], 'time': candles[i]['t']})

    # Sort by index
    swings.sort(key=lambda s: s['index'])

    # Label HH/HL/LH/LL based on sequence
    swing_highs = [s for s in swings if s['type'] == 'HH']
    swing_lows = [s for s in swings if s['type'] == 'LL']

    for i in range(1, len(swing_highs)):
        swing_highs[i]['type'] = 'HH' if swing_highs[i]['price'] > swing_highs[i - 1]['price'] else 'LH'
    for i in range(1, len(swing_lows)):
        swing_lows[i]['type'] = 'HL' if swing_lows[i]['price'] > swing_lows[i - 1]['price'] else 'LL'

    all_swings = sorted(swing_highs + swing_lows, key=lambda s: s['index'])

    # Determine trend from recent swings
    recent = all_swings[-6:] if len(all_swings) >= 6 else all_swings
    hh_count = sum(1 for s in recent if s['type'] == 'HH')
    hl_count = sum(1 for s in recent if s['type'] == 'HL')
    lh_count = sum(1 for s in recent if s['type'] == 'LH')
    ll_count = sum(1 for s in recent if s['type'] == 'LL')

    if hh_count + hl_count > lh_count + ll_count:
        trend = 'Bullish'
    elif lh_count + ll_count > hh_count + hl_count:
        trend = 'Bearish'
    else:
        trend = 'Ranging'

    # S/R zones from swing points — cluster nearby swings
    sr_zones = []
    prices = sorted(set(s['price'] for s in all_swings))
    if prices:
        threshold = (max(prices) - min(prices)) * 0.01 or 1  # 1% clustering
        clusters = []
        current_cluster = [prices[0]]
        for p in prices[1:]:
            if p - current_cluster[-1] <= threshold:
                current_cluster.append(p)
            else:
                clusters.append(current_cluster)
                current_cluster = [p]
        clusters.append(current_cluster)

        last_price = candles[-1]['c']
        for cl in clusters:
            avg = round(sum(cl) / len(cl), 2)
            strength = len(cl)
            zone_type = 'support' if avg < last_price else 'resistance'
            sr_zones.append({'price': avg, 'type': zone_type, 'strength': strength})

    # Signal summary
    last_price = candles[-1]['c']
    near_support = any(abs(z['price'] - last_price) / last_price < 0.005 and z['type'] == 'support' for z in sr_zones)
    near_resistance = any(abs(z['price'] - last_price) / last_price < 0.005 and z['type'] == 'resistance' for z in sr_zones)

    if trend == 'Bullish' and near_support:
        signal = '🟢 Bullish structure + price near support — potential long setup'
    elif trend == 'Bearish' and near_resistance:
        signal = '🔴 Bearish structure + price near resistance — potential short setup'
    elif trend == 'Bullish':
        signal = '🟢 Bullish structure (HH/HL) — look for pullbacks to support'
    elif trend == 'Bearish':
        signal = '🔴 Bearish structure (LH/LL) — look for rallies to resistance'
    else:
        signal = '🟡 Ranging — wait for structure break or trade the range'

    return {
        'swings': all_swings[-20:],  # last 20 swings
        'trend': trend,
        'sr_zones': sorted(sr_zones, key=lambda z: z['price']),
        'signal': signal,
    }


# ── Indicators ──

def calc_sma(candles, period=20):
    closes = [c['c'] for c in candles]
    out = []
    for i in range(len(closes)):
        if i < period - 1:
            out.append(None)
        else:
            out.append(round(sum(closes[i - period + 1:i + 1]) / period, 2))
    return [{'time': candles[i]['t'], 'value': out[i]} for i in range(len(candles)) if out[i] is not None]


def calc_ema(candles, period=20):
    closes = [c['c'] for c in candles]
    k = 2 / (period + 1)
    ema = [closes[0]]
    for i in range(1, len(closes)):
        ema.append(round(closes[i] * k + ema[-1] * (1 - k), 2))
    return [{'time': candles[i]['t'], 'value': ema[i]} for i in range(period - 1, len(candles))]


def calc_rsi(candles, period=14):
    closes = [c['c'] for c in candles]
    if len(closes) < period + 1:
        return []
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = []
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        out.append({'time': candles[i + 1]['t'], 'value': round(100 - 100 / (1 + rs), 2)})
    return out


def calc_bollinger(candles, period=20, std_dev=2):
    closes = [c['c'] for c in candles]
    upper, lower, mid = [], [], []
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        m = sum(window) / period
        variance = sum((x - m) ** 2 for x in window) / period
        sd = variance ** 0.5
        t = candles[i]['t']
        mid.append({'time': t, 'value': round(m, 2)})
        upper.append({'time': t, 'value': round(m + std_dev * sd, 2)})
        lower.append({'time': t, 'value': round(m - std_dev * sd, 2)})
    return {'mid': mid, 'upper': upper, 'lower': lower}


def calc_vwap(candles):
    cum_vol = 0
    cum_tp_vol = 0
    out = []
    day = None
    for c in candles:
        import datetime
        d = datetime.datetime.utcfromtimestamp(c['t']).date()
        if d != day:
            cum_vol = 0
            cum_tp_vol = 0
            day = d
        tp = (c['h'] + c['l'] + c['c']) / 3
        cum_tp_vol += tp * c['v']
        cum_vol += c['v']
        out.append({'time': c['t'], 'value': round(cum_tp_vol / cum_vol, 2) if cum_vol > 0 else round(tp, 2)})
    return out


def calc_supertrend(candles, period=10, multiplier=3):
    if len(candles) < period:
        return []
    # ATR
    trs = [candles[0]['h'] - candles[0]['l']]
    for i in range(1, len(candles)):
        tr = max(candles[i]['h'] - candles[i]['l'],
                 abs(candles[i]['h'] - candles[i - 1]['c']),
                 abs(candles[i]['l'] - candles[i - 1]['c']))
        trs.append(tr)
    atr = [0] * len(candles)
    atr[period - 1] = sum(trs[:period]) / period
    for i in range(period, len(candles)):
        atr[i] = (atr[i - 1] * (period - 1) + trs[i]) / period

    upper = [0.0] * len(candles)
    lower = [0.0] * len(candles)
    st = [0.0] * len(candles)
    direction = [1] * len(candles)

    for i in range(period - 1, len(candles)):
        hl2 = (candles[i]['h'] + candles[i]['l']) / 2
        upper[i] = hl2 + multiplier * atr[i]
        lower[i] = hl2 - multiplier * atr[i]
        if i == period - 1:
            st[i] = lower[i]
            direction[i] = 1
            continue
        if candles[i]['c'] > upper[i - 1]:
            direction[i] = 1
        elif candles[i]['c'] < lower[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
        if direction[i] == 1:
            lower[i] = max(lower[i], lower[i - 1]) if direction[i - 1] == 1 else lower[i]
            st[i] = lower[i]
        else:
            upper[i] = min(upper[i], upper[i - 1]) if direction[i - 1] == -1 else upper[i]
            st[i] = upper[i]

    return [{'time': candles[i]['t'], 'value': round(st[i], 2), 'dir': direction[i]}
            for i in range(period - 1, len(candles)) if st[i] > 0]


INDICATOR_FNS = {
    'sma20': lambda c: {'type': 'overlay', 'data': calc_sma(c, 20), 'color': '#6366f1', 'label': 'SMA 20'},
    'sma50': lambda c: {'type': 'overlay', 'data': calc_sma(c, 50), 'color': '#f59e0b', 'label': 'SMA 50'},
    'ema20': lambda c: {'type': 'overlay', 'data': calc_ema(c, 20), 'color': '#8b5cf6', 'label': 'EMA 20'},
    'rsi': lambda c: {'type': 'panel', 'data': calc_rsi(c, 14), 'label': 'RSI 14', 'levels': [30, 70]},
    'bb': lambda c: {**{'type': 'overlay', 'label': 'Bollinger Bands'}, **calc_bollinger(c, 20, 2)},
    'vwap': lambda c: {'type': 'overlay', 'data': calc_vwap(c), 'color': '#ec4899', 'label': 'VWAP'},
    'supertrend': lambda c: {'type': 'overlay_st', 'data': calc_supertrend(c, 10, 3), 'label': 'Supertrend'},
}


def calc_indicators(candles, indicator_list):
    results = {}
    for name in indicator_list:
        fn = INDICATOR_FNS.get(name)
        if fn:
            results[name] = fn(candles)
    return results
