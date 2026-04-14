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


def calc_rsi_divergence_mss(candles, rsi_period=14, ob=70, os_level=30, lb=5, max_age=50):
    """Detect RSI divergence + market structure shift signals."""
    rsi_data = calc_rsi(candles, rsi_period)
    if len(rsi_data) < lb * 2 + 1:
        return {'type': 'signals', 'signals': []}

    # Build aligned arrays (rsi_data starts later than candles)
    rsi_by_time = {r['time']: r['value'] for r in rsi_data}
    aligned = []
    for c in candles:
        rv = rsi_by_time.get(c['t'])
        if rv is not None:
            aligned.append({'t': c['t'], 'h': c['h'], 'l': c['l'], 'c': c['c'], 'rsi': rv})

    n = len(aligned)
    if n < lb * 2 + 1:
        return {'type': 'signals', 'signals': []}

    # Find pivot highs/lows in price and RSI
    def pivots(series, key, lb):
        pts = []
        for i in range(lb, len(series) - lb):
            window = [series[j][key] for j in range(i - lb, i + lb + 1)]
            if series[i][key] == max(window):
                pts.append(('high', i))
            if series[i][key] == min(window):
                pts.append(('low', i))
        return pts

    price_highs = [(i, aligned[i]) for t, i in pivots(aligned, 'h', lb) if t == 'high']
    price_lows = [(i, aligned[i]) for t, i in pivots(aligned, 'l', lb) if t == 'low']

    signals = []

    # Bearish divergence: price higher high + RSI lower high in OB zone
    ob_highs = [(i, d) for i, d in price_highs if d['rsi'] > ob]
    for j in range(1, len(ob_highs)):
        pi, pd = ob_highs[j - 1]
        ci, cd = ob_highs[j]
        if cd['h'] > pd['h'] and cd['rsi'] < pd['rsi']:
            # Find swing low between the two highs
            swing_low = min(aligned[k]['l'] for k in range(pi, ci + 1))
            # Look for MSS: close below swing_low within max_age bars
            for k in range(ci + 1, min(ci + max_age, n)):
                if aligned[k]['c'] < swing_low:
                    signals.append({
                        'time': aligned[k]['t'], 'type': 'sell',
                        'price': aligned[k]['c'],
                        'sl': cd['h'],
                        'tp1': aligned[k]['c'] - (cd['h'] - aligned[k]['c']),
                    })
                    break

    # Bullish divergence: price lower low + RSI higher low in OS zone
    os_lows = [(i, d) for i, d in price_lows if d['rsi'] < os_level]
    for j in range(1, len(os_lows)):
        pi, pd = os_lows[j - 1]
        ci, cd = os_lows[j]
        if cd['l'] < pd['l'] and cd['rsi'] > pd['rsi']:
            swing_high = max(aligned[k]['h'] for k in range(pi, ci + 1))
            for k in range(ci + 1, min(ci + max_age, n)):
                if aligned[k]['c'] > swing_high:
                    signals.append({
                        'time': aligned[k]['t'], 'type': 'buy',
                        'price': aligned[k]['c'],
                        'sl': cd['l'],
                        'tp1': aligned[k]['c'] + (aligned[k]['c'] - cd['l']),
                    })
                    break

    return {'type': 'signals', 'signals': signals}


def calc_sma_vol_breakout(candles, sma_period=50, vol_lookback=20, vol_threshold=1.2):
    """
    SETUP 1: STRONG TREND ENTRY
    BUY: Price crosses above 50 SMA + candle closes above + volume > avg + volume increasing
    Entry: Next candle break of high | SL: Below breakout candle low | TP: 1:2 RR
    SELL: Mirror (crosses below + high volume)

    SETUP 2: FAKE BREAKOUT REVERSAL
    SHORT: Price crosses above SMA on LOW volume (weak) → then falls back below SMA → entry
    SL: Above fake breakout high | TP: Previous support (recent swing low)
    LONG: Mirror (weak breakdown → price recovers above SMA)
    """
    if len(candles) < sma_period + vol_lookback + 2:
        return {'signals': [], 'sma': []}

    closes = [c['c'] for c in candles]
    opens = [c['o'] for c in candles]
    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    volumes = [c.get('v', 0) for c in candles]
    times = [c['t'] for c in candles]

    # SMA
    sma = [None] * len(closes)
    for i in range(sma_period - 1, len(closes)):
        sma[i] = sum(closes[i - sma_period + 1:i + 1]) / sma_period

    # Average volume
    avg_vol = [0] * len(volumes)
    for i in range(len(volumes)):
        start = max(0, i - vol_lookback + 1)
        avg_vol[i] = max(1, sum(volumes[start:i + 1]) / (i - start + 1))

    signals = []
    sma_data = []

    for i in range(sma_period + 1, len(candles) - 1):
        if sma[i] is None or sma[i - 1] is None:
            continue
        sma_data.append({'time': times[i], 'value': round(sma[i], 2)})

        prev_below = closes[i - 1] < sma[i - 1]
        prev_above = closes[i - 1] > sma[i - 1]
        curr_above = closes[i] > sma[i]
        curr_below = closes[i] < sma[i]

        vol = volumes[i]
        prev_vol = volumes[i - 1]
        avg = avg_vol[i]
        vol_ratio = vol / avg if avg > 0 else 0
        high_vol = vol > avg and vol > prev_vol

        # ═══ SETUP 1: STRONG TREND ENTRY ═══
        if prev_below and curr_above and closes[i] > sma[i] and high_vol:
            next_candle = candles[i + 1]
            entry = highs[i]
            if next_candle['h'] >= entry:
                sl = lows[i]
                risk = entry - sl
                if risk > 0:
                    signals.append({
                        'time': times[i + 1], 'type': 'buy', 'price': round(entry, 2),
                        'sl': round(sl, 2), 'tp1': round(entry + risk * 2, 2),
                        'setup': 1, 'strength': 'strong', 'vol_ratio': round(vol_ratio, 2),
                        'volume': vol, 'avg_volume': round(avg, 0),
                        'label': 'Strong Trend Entry',
                    })

        elif prev_above and curr_below and closes[i] < sma[i] and high_vol:
            next_candle = candles[i + 1]
            entry = lows[i]
            if next_candle['l'] <= entry:
                sl = highs[i]
                risk = sl - entry
                if risk > 0:
                    signals.append({
                        'time': times[i + 1], 'type': 'sell', 'price': round(entry, 2),
                        'sl': round(sl, 2), 'tp1': round(entry - risk * 2, 2),
                        'setup': 1, 'strength': 'strong', 'vol_ratio': round(vol_ratio, 2),
                        'volume': vol, 'avg_volume': round(avg, 0),
                        'label': 'Strong Trend Entry',
                    })

    return {'signals': signals, 'sma': sma_data}


INDICATOR_FNS = {
    'sma20': lambda c: {'type': 'overlay', 'data': calc_sma(c, 20), 'color': '#6366f1', 'label': 'SMA 20'},
    'sma50': lambda c: {'type': 'overlay', 'data': calc_sma(c, 50), 'color': '#f59e0b', 'label': 'SMA 50'},
    'ema20': lambda c: {'type': 'overlay', 'data': calc_ema(c, 20), 'color': '#8b5cf6', 'label': 'EMA 20'},
    'rsi': lambda c: {'type': 'panel', 'data': calc_rsi(c, 14), 'label': 'RSI 14', 'levels': [30, 70]},
    'bb': lambda c: {**{'type': 'overlay', 'label': 'Bollinger Bands'}, **calc_bollinger(c, 20, 2)},
    'vwap': lambda c: {'type': 'overlay', 'data': calc_vwap(c), 'color': '#ec4899', 'label': 'VWAP'},
    'supertrend': lambda c: {'type': 'overlay_st', 'data': calc_supertrend(c, 10, 3), 'label': 'Supertrend'},
    'rsi_div_mss': lambda c: calc_rsi_divergence_mss(c),
    'sma_vol_breakout': lambda c: calc_sma_vol_breakout(c),
}


def calc_indicators(candles, indicator_list):
    results = {}
    for name in indicator_list:
        fn = INDICATOR_FNS.get(name)
        if fn:
            results[name] = fn(candles)
    return results
