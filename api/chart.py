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
    import datetime
    cum_vol = 0
    cum_tp_vol = 0
    out = []
    day = None
    for c in candles:
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


def calc_box_theory(candles):
    """Box Theory: previous day's high/low form a box. Buy at bottom, sell at top, avoid middle.
    Optimized: 25% zone width + volume > 20-period average filter.
    Only signals on first zone touch per day with candle rejection confirmation."""
    import datetime as _dt
    if not candles or len(candles) < 2:
        return {'type': 'signals', 'signals': []}

    vols = [c.get('v', 0) for c in candles]
    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        start = max(0, i - 19)
        w = vols[start:i + 1]
        avg_vol[i] = sum(w) / len(w) if w else 1

    days = {}
    for i, c in enumerate(candles):
        d = _dt.datetime.fromtimestamp(c['t'], _dt.timezone.utc).date()
        days.setdefault(d, []).append((i, c))

    sorted_days = sorted(days.keys())
    if len(sorted_days) < 2:
        return {'type': 'signals', 'signals': []}

    signals = []
    for di in range(1, len(sorted_days)):
        prev = days[sorted_days[di - 1]]
        prev_high = max(c['h'] for _, c in prev)
        prev_low = min(c['l'] for _, c in prev)
        box_range = prev_high - prev_low
        if box_range <= 0:
            continue
        mid = round((prev_high + prev_low) / 2, 2)
        sell_zone = prev_high - box_range * 0.25
        buy_zone = prev_low + box_range * 0.25

        buy_triggered = sell_triggered = False
        for idx, c in days[sorted_days[di]]:
            # Buy: wick into bottom zone + bullish close + volume above average
            if not buy_triggered and c['l'] <= buy_zone and c['c'] > c['o'] and c['c'] > buy_zone:
                if avg_vol[idx] > 0 and vols[idx] < avg_vol[idx]:
                    continue
                sl_price = c['l']  # SL at rejection candle low (tighter than box boundary)
                risk = c['c'] - sl_price
                if risk <= 0:
                    continue
                signals.append({
                    'time': c['t'], 'type': 'buy', 'price': round(c['c'], 2),
                    'sl': round(sl_price, 2), 'tp1': mid,
                    'box_high': round(prev_high, 2), 'box_low': round(prev_low, 2), 'box_mid': mid,
                })
                buy_triggered = True
            # Sell: wick into top zone + bearish close + volume above average
            if not sell_triggered and c['h'] >= sell_zone and c['c'] < c['o'] and c['c'] < sell_zone:
                if avg_vol[idx] > 0 and vols[idx] < avg_vol[idx]:
                    continue
                sl_price = c['h']  # SL at rejection candle high (tighter than box boundary)
                risk = sl_price - c['c']
                if risk <= 0:
                    continue
                signals.append({
                    'time': c['t'], 'type': 'sell', 'price': round(c['c'], 2),
                    'sl': round(sl_price, 2), 'tp1': mid,
                    'box_high': round(prev_high, 2), 'box_low': round(prev_low, 2), 'box_mid': mid,
                })
                sell_triggered = True

    return {'type': 'signals', 'signals': signals}


def calc_ema_trendline_breakout(candles, ema_period=200, lb=5, min_rr=2):
    """200 EMA + Trendline Breakout strategy with volume filter.
    - Above 200 EMA: only longs (descending TL breakout)
    - Below 200 EMA: only shorts (ascending TL breakdown)
    - Breakout candle must have volume > 1.5x 20-period average
    """
    if len(candles) < ema_period + lb * 3:
        return {'type': 'signals', 'signals': [], 'ema200': []}

    closes = [c['c'] for c in candles]
    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    vols = [c.get('v', 0) for c in candles]

    k = 2 / (ema_period + 1)
    ema = [closes[0]]
    for i in range(1, len(closes)):
        ema.append(closes[i] * k + ema[-1] * (1 - k))
    ema_data = [{'time': candles[i]['t'], 'value': round(ema[i], 2)} for i in range(ema_period - 1, len(candles))]

    # 20-period average volume
    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        start = max(0, i - 19)
        w = vols[start:i + 1]
        avg_vol[i] = sum(w) / len(w) if w else 1

    swing_highs, swing_lows = [], []
    for i in range(lb, len(candles) - lb):
        if highs[i] == max(highs[i - lb:i + lb + 1]):
            swing_highs.append(i)
        if lows[i] == min(lows[i - lb:i + lb + 1]):
            swing_lows.append(i)

    signals = []

    def find_trendline_signals(pivots, direction):
        sigs = []
        for j in range(1, len(pivots)):
            i1, i2 = pivots[j - 1], pivots[j]
            if i2 - i1 < lb or i2 >= len(candles) - 2:
                continue
            if direction == 'long':
                p1, p2 = highs[i1], highs[i2]
                if p2 >= p1:
                    continue
                slope = (p2 - p1) / (i2 - i1)
                for ki in range(i2 + 1, min(i2 + lb * 3, len(candles) - 1)):
                    tl_val = p2 + slope * (ki - i2)
                    if candles[ki]['c'] > tl_val and closes[ki] > ema[ki]:
                        if avg_vol[ki] > 0 and vols[ki] < avg_vol[ki] * 1.5:
                            break
                        sl = candles[ki]['l']
                        risk = candles[ki]['c'] - sl
                        if risk <= 0:
                            break
                        sigs.append({
                            'time': candles[ki]['t'], 'type': 'buy',
                            'price': round(candles[ki]['c'], 2),
                            'sl': round(sl, 2), 'tp1': round(candles[ki]['c'] + risk * min_rr, 2),
                            'tl_start_time': candles[i1]['t'], 'tl_start_price': round(p1, 2),
                            'tl_end_time': candles[i2]['t'], 'tl_end_price': round(p2, 2),
                        })
                        break
            else:
                p1, p2 = lows[i1], lows[i2]
                if p2 <= p1:
                    continue
                slope = (p2 - p1) / (i2 - i1)
                for ki in range(i2 + 1, min(i2 + lb * 3, len(candles) - 1)):
                    tl_val = p2 + slope * (ki - i2)
                    if candles[ki]['c'] < tl_val and closes[ki] < ema[ki]:
                        if avg_vol[ki] > 0 and vols[ki] < avg_vol[ki] * 1.5:
                            break
                        sl = candles[ki]['h']
                        risk = sl - candles[ki]['c']
                        if risk <= 0:
                            break
                        sigs.append({
                            'time': candles[ki]['t'], 'type': 'sell',
                            'price': round(candles[ki]['c'], 2),
                            'sl': round(sl, 2), 'tp1': round(candles[ki]['c'] - risk * min_rr, 2),
                            'tl_start_time': candles[i1]['t'], 'tl_start_price': round(p1, 2),
                            'tl_end_time': candles[i2]['t'], 'tl_end_price': round(p2, 2),
                        })
                        break
        return sigs

    signals += find_trendline_signals(swing_highs, 'long')
    signals += find_trendline_signals(swing_lows, 'short')
    signals.sort(key=lambda s: s['time'])

    return {'type': 'signals', 'signals': signals, 'ema200': ema_data}


def calc_ema920_pullback(candles, min_rr=2):
    """9/20 EMA Pullback strategy with volume filter.
    - Price above both EMAs → buy on pullback to EMA zone + bullish rejection
    - Price below both EMAs → sell on pullback to EMA zone + bearish rejection
    - Price crossing EMAs back and forth → no trade (range)
    - Volume must be above 20-period average (filters weak/fake pullbacks)
    """
    if len(candles) < 25:
        return {'type': 'signals', 'signals': [], 'ema9': [], 'ema20': []}

    closes = [c['c'] for c in candles]
    vols = [c.get('v', 0) for c in candles]

    def _ema(data, p):
        k = 2 / (p + 1)
        e = [data[0]]
        for v in data[1:]:
            e.append(v * k + e[-1] * (1 - k))
        return e

    ema9 = _ema(closes, 9)
    ema20 = _ema(closes, 20)

    # 20-period average volume
    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        start = max(0, i - 19)
        w = vols[start:i + 1]
        avg_vol[i] = sum(w) / len(w) if w else 1

    ema9_data = [{'time': candles[i]['t'], 'value': round(ema9[i], 2)} for i in range(8, len(candles))]
    ema20_data = [{'time': candles[i]['t'], 'value': round(ema20[i], 2)} for i in range(19, len(candles))]

    signals = []
    for i in range(21, len(candles) - 1):
        c = candles[i]
        e9, e20 = ema9[i], ema20[i]
        ema_hi = max(e9, e20)
        ema_lo = min(e9, e20)

        # Volume filter: skip low-volume candles
        if avg_vol[i] > 0 and vols[i] < avg_vol[i]:
            continue

        above_count = sum(1 for j in range(i - 3, i) if closes[j] > ema_hi)
        below_count = sum(1 for j in range(i - 3, i) if closes[j] < ema_lo)

        if above_count >= 2 and c['l'] <= ema_hi and c['c'] > c['o'] and c['c'] > ema_lo:
            sl = c['l']
            risk = c['c'] - sl
            if risk > 0:
                signals.append({
                    'time': c['t'], 'type': 'buy', 'price': round(c['c'], 2),
                    'sl': round(sl, 2), 'tp1': round(c['c'] + risk * min_rr, 2),
                })

        elif below_count >= 2 and c['h'] >= ema_lo and c['c'] < c['o'] and c['c'] < ema_hi:
            sl = c['h']
            risk = sl - c['c']
            if risk > 0:
                signals.append({
                    'time': c['t'], 'type': 'sell', 'price': round(c['c'], 2),
                    'sl': round(sl, 2), 'tp1': round(c['c'] - risk * min_rr, 2),
                })

    return {'type': 'signals', 'signals': signals, 'ema9': ema9_data, 'ema20': ema20_data}


def calc_darvas_box(candles, confirm_bars=3, min_rr=2):
    """Darvas Box breakout strategy with volume + trend confirmation.
    1. Identify box: new high followed by consolidation (no new high for confirm_bars)
    2. Box bottom = lowest low during consolidation
    3. Buy on breakout above box top with:
       - Volume > 1.5x average
       - Price above 50 EMA (trend filter)
       - Breakout candle closes above box top (not just wick)
    4. SL at box bottom, TP at 1:2 R:R
    """
    if len(candles) < 55:
        return {'type': 'signals', 'signals': [], 'boxes': []}

    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    closes = [c['c'] for c in candles]
    vols = [c.get('v', 0) for c in candles]

    # 50 EMA for trend filter
    k50 = 2 / 51
    ema50 = [closes[0]]
    for v in closes[1:]:
        ema50.append(v * k50 + ema50[-1] * (1 - k50))

    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        start = max(0, i - 19)
        w = vols[start:i + 1]
        avg_vol[i] = sum(w) / len(w) if w else 1

    signals = []
    boxes = []
    i = confirm_bars + 50  # start after EMA warmup
    prev_box_top = 0  # track previous box for stacking requirement

    while i < len(candles) - 1:
        # Step 1: Find a new high
        is_new_high = highs[i] == max(highs[i - confirm_bars:i + 1])
        if not is_new_high:
            i += 1
            continue

        box_top = highs[i]
        top_idx = i

        # Step 2: Confirm top holds for confirm_bars
        confirmed_top = True
        for j in range(i + 1, min(i + 1 + confirm_bars, len(candles))):
            if highs[j] > box_top:
                confirmed_top = False
                i = j
                break
        if not confirmed_top:
            continue

        # Step 3: Find box bottom
        bottom_end = min(i + 1 + confirm_bars, len(candles))
        box_bottom = min(lows[top_idx:bottom_end])

        if box_top <= box_bottom:
            i = bottom_end
            continue

        # Min box size: at least 0.3% range
        box_pct = (box_top - box_bottom) / box_top * 100
        if box_pct < 0.3:
            i = bottom_end
            continue

        boxes.append({
            'top': round(box_top, 2), 'bottom': round(box_bottom, 2),
            'start_time': candles[top_idx]['t'],
            'end_time': candles[min(bottom_end, len(candles) - 1)]['t'],
        })

        # Step 4: Look for breakout — only if box is stacking higher (uptrend)
        if prev_box_top > 0 and box_top <= prev_box_top:
            prev_box_top = box_top
            i = search_end if 'search_end' in dir() else bottom_end
            continue
        search_end = min(bottom_end + confirm_bars * 5, len(candles))
        for ki in range(bottom_end, search_end):
            if lows[ki] < box_bottom:
                break  # breakdown
            if closes[ki] > box_top:
                # Volume > 1.5x avg
                if avg_vol[ki] > 0 and vols[ki] < avg_vol[ki] * 1.5:
                    continue
                # Trend filter: price must be above 50 EMA
                if closes[ki] < ema50[ki]:
                    continue
                sl = max(box_bottom, candles[ki]['l'])  # tighter SL: candle low or box bottom
                risk = closes[ki] - sl
                if risk <= 0:
                    break
                tp = closes[ki] + risk * min_rr
                signals.append({
                    'time': candles[ki]['t'], 'type': 'buy',
                    'price': round(closes[ki], 2),
                    'sl': round(sl, 2), 'tp1': round(tp, 2),
                    'box_top': round(box_top, 2), 'box_bottom': round(box_bottom, 2),
                })
                break

        prev_box_top = box_top
        i = search_end

    return {'type': 'signals', 'signals': signals, 'boxes': boxes}


def calc_fib_retracement(candles, lb=20, min_rr=2):
    """Fibonacci Retracement: detect swing H/L, signal on pullback to 0.382/0.618 levels."""
    if len(candles) < lb * 3:
        return {'type': 'signals', 'signals': []}
    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    vols = [c.get('v', 0) for c in candles]
    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        s = max(0, i - 19); w = vols[s:i + 1]; avg_vol[i] = sum(w) / len(w) if w else 1

    # Find major swing highs and lows
    swing_h, swing_l = [], []
    for i in range(lb, len(candles) - lb):
        if highs[i] == max(highs[i - lb:i + lb + 1]): swing_h.append(i)
        if lows[i] == min(lows[i - lb:i + lb + 1]): swing_l.append(i)

    signals = []
    # Bullish fib: swing low → swing high, buy on pullback to 0.382/0.618
    for li in range(len(swing_l)):
        for hi in range(len(swing_h)):
            sl_idx, sh_idx = swing_l[li], swing_h[hi]
            if sh_idx <= sl_idx or sh_idx - sl_idx < lb: continue
            sw_low, sw_high = lows[sl_idx], highs[sh_idx]
            rng = sw_high - sw_low
            if rng <= 0: continue
            fib_382 = sw_high - rng * 0.382
            fib_618 = sw_high - rng * 0.618
            # Look for pullback after swing high
            for k in range(sh_idx + 1, min(sh_idx + lb * 2, len(candles) - 1)):
                if candles[k]['l'] <= fib_382 and candles[k]['c'] > fib_618 and candles[k]['c'] > candles[k]['o']:
                    if avg_vol[k] > 0 and vols[k] < avg_vol[k] * 0.8: continue
                    sl = candles[k]['l']
                    risk = candles[k]['c'] - sl
                    if risk <= 0: continue
                    signals.append({'time': candles[k]['t'], 'type': 'buy', 'price': round(candles[k]['c'], 2),
                                    'sl': round(sl, 2), 'tp1': round(candles[k]['c'] + risk * min_rr, 2),
                                    'fib_level': 0.382})
                    break
            if len(signals) > 200: break
        if len(signals) > 200: break

    # Bearish fib: swing high → swing low, sell on pullback to 0.382/0.618
    for hi in range(len(swing_h)):
        for li in range(len(swing_l)):
            sh_idx, sl_idx = swing_h[hi], swing_l[li]
            if sl_idx <= sh_idx or sl_idx - sh_idx < lb: continue
            sw_high, sw_low = highs[sh_idx], lows[sl_idx]
            rng = sw_high - sw_low
            if rng <= 0: continue
            fib_382 = sw_low + rng * 0.382
            fib_618 = sw_low + rng * 0.618
            for k in range(sl_idx + 1, min(sl_idx + lb * 2, len(candles) - 1)):
                if candles[k]['h'] >= fib_382 and candles[k]['c'] < fib_618 and candles[k]['c'] < candles[k]['o']:
                    if avg_vol[k] > 0 and vols[k] < avg_vol[k] * 0.8: continue
                    sl = candles[k]['h']
                    risk = sl - candles[k]['c']
                    if risk <= 0: continue
                    signals.append({'time': candles[k]['t'], 'type': 'sell', 'price': round(candles[k]['c'], 2),
                                    'sl': round(sl, 2), 'tp1': round(candles[k]['c'] - risk * min_rr, 2),
                                    'fib_level': 0.382})
                    break
            if len(signals) > 400: break
        if len(signals) > 400: break

    signals.sort(key=lambda s: s['time'])
    return {'type': 'signals', 'signals': signals[-100:]}


def calc_fvg(candles, min_gap_pct=0.1):
    """Fair Value Gap: detect imbalance candles, signal when price revisits the gap."""
    if len(candles) < 5:
        return {'type': 'signals', 'signals': []}
    signals = []
    vols = [c.get('v', 0) for c in candles]
    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        s = max(0, i - 19); w = vols[s:i + 1]; avg_vol[i] = sum(w) / len(w) if w else 1

    for i in range(2, len(candles) - 1):
        prev, curr, nxt = candles[i - 2], candles[i - 1], candles[i]
        # Bullish FVG: gap between candle[i-2] high and candle[i] low (curr is the big candle)
        if curr['c'] > curr['o']:  # bullish big candle
            gap_top = nxt['l']
            gap_bottom = prev['h']
            if gap_top > gap_bottom:
                gap_size = gap_top - gap_bottom
                if gap_size / curr['c'] * 100 >= min_gap_pct:
                    # Look for price to revisit the gap
                    for k in range(i + 1, min(i + 30, len(candles))):
                        if candles[k]['l'] <= gap_top and candles[k]['c'] > gap_bottom and candles[k]['c'] > candles[k]['o']:
                            if avg_vol[k] > 0 and vols[k] < avg_vol[k]: continue
                            sl = candles[k]['l']
                            risk = candles[k]['c'] - sl
                            if risk > 0:
                                signals.append({'time': candles[k]['t'], 'type': 'buy', 'price': round(candles[k]['c'], 2),
                                                'sl': round(sl, 2), 'tp1': round(candles[k]['c'] + risk * 2, 2)})
                            break
        # Bearish FVG
        if curr['c'] < curr['o']:  # bearish big candle
            gap_top = prev['l']
            gap_bottom = nxt['h']
            if gap_top > gap_bottom:
                gap_size = gap_top - gap_bottom
                if gap_size / curr['c'] * 100 >= min_gap_pct:
                    for k in range(i + 1, min(i + 30, len(candles))):
                        if candles[k]['h'] >= gap_bottom and candles[k]['c'] < gap_top and candles[k]['c'] < candles[k]['o']:
                            if avg_vol[k] > 0 and vols[k] < avg_vol[k]: continue
                            sl = candles[k]['h']
                            risk = sl - candles[k]['c']
                            if risk > 0:
                                signals.append({'time': candles[k]['t'], 'type': 'sell', 'price': round(candles[k]['c'], 2),
                                                'sl': round(sl, 2), 'tp1': round(candles[k]['c'] - risk * 2, 2)})
                            break
    signals.sort(key=lambda s: s['time'])
    return {'type': 'signals', 'signals': signals[-100:]}


def calc_supply_demand(candles, min_move_pct=0.5, lb=3):
    """Supply/Demand Zones (Order Blocks): detect zones of strong moves, signal on revisit."""
    if len(candles) < lb + 5:
        return {'type': 'signals', 'signals': []}
    signals = []
    vols = [c.get('v', 0) for c in candles]
    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        s = max(0, i - 19); w = vols[s:i + 1]; avg_vol[i] = sum(w) / len(w) if w else 1

    zones = []  # (type, zone_high, zone_low, idx)
    for i in range(lb, len(candles) - lb):
        # Demand zone: price moves significantly UP from this candle
        future_high = max(c['h'] for c in candles[i + 1:i + 1 + lb])
        move_up = (future_high - candles[i]['l']) / candles[i]['l'] * 100
        if move_up >= min_move_pct:
            zones.append(('demand', candles[i]['h'], candles[i]['l'], i))
        # Supply zone: price moves significantly DOWN from this candle
        future_low = min(c['l'] for c in candles[i + 1:i + 1 + lb])
        move_down = (candles[i]['h'] - future_low) / candles[i]['h'] * 100
        if move_down >= min_move_pct:
            zones.append(('supply', candles[i]['h'], candles[i]['l'], i))

    # Signal on revisit
    for ztype, zh, zl, zi in zones:
        for k in range(zi + lb + 1, min(zi + 50, len(candles))):
            if ztype == 'demand' and candles[k]['l'] <= zh and candles[k]['c'] > zl and candles[k]['c'] > candles[k]['o']:
                if avg_vol[k] > 0 and vols[k] < avg_vol[k]: continue
                sl = candles[k]['l']
                risk = candles[k]['c'] - sl
                if risk > 0:
                    signals.append({'time': candles[k]['t'], 'type': 'buy', 'price': round(candles[k]['c'], 2),
                                    'sl': round(sl, 2), 'tp1': round(candles[k]['c'] + risk * 2, 2)})
                break
            elif ztype == 'supply' and candles[k]['h'] >= zl and candles[k]['c'] < zh and candles[k]['c'] < candles[k]['o']:
                if avg_vol[k] > 0 and vols[k] < avg_vol[k]: continue
                sl = candles[k]['h']
                risk = sl - candles[k]['c']
                if risk > 0:
                    signals.append({'time': candles[k]['t'], 'type': 'sell', 'price': round(candles[k]['c'], 2),
                                    'sl': round(sl, 2), 'tp1': round(candles[k]['c'] - risk * 2, 2)})
                break
    signals.sort(key=lambda s: s['time'])
    return {'type': 'signals', 'signals': signals[-100:]}


def calc_candlestick_patterns(candles):
    """Candlestick Patterns: engulfing, hammer, shooting star with volume confirmation."""
    if len(candles) < 3:
        return {'type': 'signals', 'signals': []}
    signals = []
    vols = [c.get('v', 0) for c in candles]
    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        s = max(0, i - 19); w = vols[s:i + 1]; avg_vol[i] = sum(w) / len(w) if w else 1

    for i in range(1, len(candles) - 1):
        prev, curr = candles[i - 1], candles[i]
        body = abs(curr['c'] - curr['o'])
        rng = curr['h'] - curr['l']
        if rng <= 0: continue
        prev_body = abs(prev['c'] - prev['o'])

        # Volume filter
        if avg_vol[i] > 0 and vols[i] < avg_vol[i]:
            continue

        sl = tp = None
        sig_type = None

        # Bullish Engulfing
        if prev['c'] < prev['o'] and curr['c'] > curr['o'] and curr['c'] > prev['o'] and curr['o'] < prev['c'] and body > prev_body:
            sig_type = 'buy'; sl = curr['l']; risk = curr['c'] - sl
            if risk > 0: tp = curr['c'] + risk * 2

        # Bearish Engulfing
        elif prev['c'] > prev['o'] and curr['c'] < curr['o'] and curr['c'] < prev['o'] and curr['o'] > prev['c'] and body > prev_body:
            sig_type = 'sell'; sl = curr['h']; risk = sl - curr['c']
            if risk > 0: tp = curr['c'] - risk * 2

        # Hammer (bullish): small body at top, long lower wick
        elif curr['c'] > curr['o'] and (min(curr['o'], curr['c']) - curr['l']) > body * 2 and (curr['h'] - max(curr['o'], curr['c'])) < body * 0.5:
            sig_type = 'buy'; sl = curr['l']; risk = curr['c'] - sl
            if risk > 0: tp = curr['c'] + risk * 2

        # Shooting Star (bearish): small body at bottom, long upper wick
        elif curr['c'] < curr['o'] and (curr['h'] - max(curr['o'], curr['c'])) > body * 2 and (min(curr['o'], curr['c']) - curr['l']) < body * 0.5:
            sig_type = 'sell'; sl = curr['h']; risk = sl - curr['c']
            if risk > 0: tp = curr['c'] - risk * 2

        if sig_type and sl and tp:
            signals.append({'time': curr['t'], 'type': sig_type, 'price': round(curr['c'], 2),
                            'sl': round(sl, 2), 'tp1': round(tp, 2)})

    return {'type': 'signals', 'signals': signals[-100:]}


def calc_volume_imbalance(candles, vol_spike_mult=5, consolidation_bars=3, min_rr=4):
    """Institutional Volume Imbalance strategy (Ravindra Rokade style).
    1. Detect volume spike (10x+ normal volume) = institutional entry
    2. Wait for consolidation (volume dries up to normal)
    3. Enter on next volume spike in same direction as original move
    4. SL: consolidation range low/high | TP: 1:4 R:R minimum
    """
    if len(candles) < 30:
        return {'type': 'signals', 'signals': []}

    closes = [c['c'] for c in candles]
    vols = [c.get('v', 0) for c in candles]

    # 20-bar average volume
    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        s = max(0, i - 19)
        w = vols[s:i + 1]
        avg_vol[i] = sum(w) / len(w) if w else 1

    signals = []
    i = 20

    while i < len(candles) - consolidation_bars - 2:
        # Step 1: Detect initial volume spike (10x+ average)
        if avg_vol[i] <= 0 or vols[i] < avg_vol[i] * vol_spike_mult:
            i += 1
            continue

        spike_candle = candles[i]
        spike_dir = 'bull' if spike_candle['c'] > spike_candle['o'] else 'bear'

        # Step 2: Wait for consolidation — volume must drop back to normal
        consol_start = i + 1
        consol_end = None
        for j in range(consol_start, min(i + 30, len(candles))):
            # Check if volume dried up (below 2x average for consolidation_bars consecutive)
            dry_count = 0
            for k in range(max(consol_start, j - consolidation_bars + 1), j + 1):
                if k < len(candles) and avg_vol[k] > 0 and vols[k] < avg_vol[k] * 2:
                    dry_count += 1
            if dry_count >= consolidation_bars:
                consol_end = j
                break

        if consol_end is None:
            i = consol_start
            continue

        # Consolidation range
        consol_high = max(c['h'] for c in candles[consol_start:consol_end + 1])
        consol_low = min(c['l'] for c in candles[consol_start:consol_end + 1])

        # Step 3: Look for second volume spike (breakout from consolidation)
        for k in range(consol_end + 1, min(consol_end + 20, len(candles) - 1)):
            if avg_vol[k] <= 0 or vols[k] < avg_vol[k] * vol_spike_mult * 0.3:
                continue  # need at least 5x for second spike

            c = candles[k]
            if spike_dir == 'bull' and c['c'] > consol_high and c['c'] > c['o']:
                sl = consol_low
                risk = c['c'] - sl
                if risk > 0:
                    signals.append({
                        'time': c['t'], 'type': 'buy', 'price': round(c['c'], 2),
                        'sl': round(sl, 2), 'tp1': round(c['c'] + risk * min_rr, 2),
                    })
                break

            elif spike_dir == 'bear' and c['c'] < consol_low and c['c'] < c['o']:
                sl = consol_high
                risk = sl - c['c']
                if risk > 0:
                    signals.append({
                        'time': c['t'], 'type': 'sell', 'price': round(c['c'], 2),
                        'sl': round(sl, 2), 'tp1': round(c['c'] - risk * min_rr, 2),
                    })
                break

        i = (consol_end or i) + 1

    return {'type': 'signals', 'signals': signals}


def calc_confluence_scalp(candles, ema_period=20, lb=5):
    """Confluence Scalp: trendline break + support bounce + EMA reclaim.
    Buy when: price bounces from support, breaks descending trendline, reclaims EMA.
    Sell when: price rejects from resistance, breaks ascending trendline, loses EMA.
    Target: EMA / previous structure. SL: below/above the bounce candle."""
    if len(candles) < max(ema_period + 10, lb * 4):
        return {'type': 'signals', 'signals': []}

    closes = [c['c'] for c in candles]
    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    vols = [c.get('v', 0) for c in candles]

    # EMA
    k = 2 / (ema_period + 1)
    ema = [closes[0]]
    for v in closes[1:]:
        ema.append(v * k + ema[-1] * (1 - k))

    # Avg volume
    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        s = max(0, i - 19); w = vols[s:i + 1]; avg_vol[i] = sum(w) / len(w) if w else 1

    # Swing highs/lows for support/resistance
    swing_h, swing_l = [], []
    for i in range(lb, len(candles) - lb):
        if highs[i] == max(highs[i - lb:i + lb + 1]):
            swing_h.append(i)
        if lows[i] == min(lows[i - lb:i + lb + 1]):
            swing_l.append(i)

    signals = []

    for i in range(ema_period + lb * 2, len(candles) - 1):
        c = candles[i]
        prev = candles[i - 1]

        # Volume filter
        if avg_vol[i] > 0 and vols[i] < avg_vol[i]:
            continue

        # === BULLISH CONFLUENCE: support bounce + trendline break + EMA reclaim ===
        # 1. Price was below EMA, now closing above or near it (reclaim)
        below_ema_recently = sum(1 for j in range(i - 4, i) if closes[j] < ema[j]) >= 3
        reclaiming_ema = c['c'] > ema[i] * 0.998  # within 0.2% of EMA or above

        # 2. Near a support level (recent swing low within 1%)
        near_support = False
        for sl_idx in swing_l:
            if sl_idx >= i - 20 and sl_idx < i:
                support_lvl = lows[sl_idx]
                if abs(c['l'] - support_lvl) / support_lvl < 0.01:
                    near_support = True
                    break

        # 3. Descending trendline break: recent swing highs forming lower highs, price breaks above
        tl_break_bull = False
        recent_sh = [j for j in swing_h if i - 30 < j < i - 2]
        if len(recent_sh) >= 2:
            h1, h2 = recent_sh[-2], recent_sh[-1]
            if highs[h2] < highs[h1]:  # descending highs
                slope = (highs[h2] - highs[h1]) / (h2 - h1)
                tl_val = highs[h2] + slope * (i - h2)
                if c['c'] > tl_val and prev['c'] <= tl_val + abs(slope):
                    tl_break_bull = True

        # 4. Bullish candle confirmation
        bullish_candle = c['c'] > c['o']

        # Need at least 3 of 4 conditions (confluence)
        bull_score = sum([below_ema_recently and reclaiming_ema, near_support, tl_break_bull, bullish_candle])
        if bull_score >= 3:
            sl = c['l']
            # Target: EMA or previous swing high
            target_candidates = [ema[i] * 1.005]  # slightly above EMA
            for sh_idx in swing_h:
                if sh_idx >= i - 15 and sh_idx < i and highs[sh_idx] > c['c']:
                    target_candidates.append(highs[sh_idx])
            tp = min(target_candidates) if target_candidates else ema[i] * 1.01
            risk = c['c'] - sl
            reward = tp - c['c']
            if risk > 0 and reward > risk * 0.8:  # at least 0.8:1 R:R for scalp
                signals.append({
                    'time': c['t'], 'type': 'buy', 'price': round(c['c'], 2),
                    'sl': round(sl, 2), 'tp1': round(tp, 2),
                })

        # === BEARISH CONFLUENCE: resistance rejection + trendline break + EMA loss ===
        above_ema_recently = sum(1 for j in range(i - 4, i) if closes[j] > ema[j]) >= 3
        losing_ema = c['c'] < ema[i] * 1.002

        near_resistance = False
        for sh_idx in swing_h:
            if sh_idx >= i - 20 and sh_idx < i:
                res_lvl = highs[sh_idx]
                if abs(c['h'] - res_lvl) / res_lvl < 0.01:
                    near_resistance = True
                    break

        tl_break_bear = False
        recent_sl = [j for j in swing_l if i - 30 < j < i - 2]
        if len(recent_sl) >= 2:
            l1, l2 = recent_sl[-2], recent_sl[-1]
            if lows[l2] > lows[l1]:  # ascending lows
                slope = (lows[l2] - lows[l1]) / (l2 - l1)
                tl_val = lows[l2] + slope * (i - l2)
                if c['c'] < tl_val and prev['c'] >= tl_val - abs(slope):
                    tl_break_bear = True

        bearish_candle = c['c'] < c['o']

        bear_score = sum([above_ema_recently and losing_ema, near_resistance, tl_break_bear, bearish_candle])
        if bear_score >= 3:
            sl = c['h']
            target_candidates = [ema[i] * 0.995]
            for sl_idx in swing_l:
                if sl_idx >= i - 15 and sl_idx < i and lows[sl_idx] < c['c']:
                    target_candidates.append(lows[sl_idx])
            tp = max(target_candidates) if target_candidates else ema[i] * 0.99
            risk = sl - c['c']
            reward = c['c'] - tp
            if risk > 0 and reward > risk * 0.8:
                signals.append({
                    'time': c['t'], 'type': 'sell', 'price': round(c['c'], 2),
                    'sl': round(sl, 2), 'tp1': round(tp, 2),
                })

    return {'type': 'signals', 'signals': signals[-100:]}


def calc_renko_redbar(candles, ema_fast=10, ema_slow=30, sma_long=150):
    """Renko + Red Bar Theory: ATR-based trend + first reversal candle entry.
    - Renko brick size auto-calculated from ATR (volatility-based range)
    - EMA 10/30 crossover for trend confirmation
    - SMA 150 for long-term trend
    - Entry on first opposite-color candle after opening candle (Red Bar)
    - SL: candle high/low, TP: 1 brick size"""
    if len(candles) < sma_long + 5:
        return {'type': 'signals', 'signals': [], 'renko_line': [], 'ema10': [], 'ema30': []}

    closes = [c['c'] for c in candles]
    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    vols = [c.get('v', 0) for c in candles]

    # ATR-14 for brick size
    trs = [highs[0] - lows[0]]
    for i in range(1, len(candles)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    atr = [0] * len(candles)
    if len(trs) >= 14:
        atr[13] = sum(trs[:14]) / 14
        for i in range(14, len(candles)):
            atr[i] = (atr[i-1] * 13 + trs[i]) / 14
    brick_size = atr[-1] if atr[-1] > 0 else (max(closes) - min(closes)) * 0.02

    # EMAs
    def _ema(data, p):
        k = 2 / (p + 1); e = [data[0]]
        for v in data[1:]: e.append(v * k + e[-1] * (1 - k))
        return e
    ema10 = _ema(closes, ema_fast)
    ema30 = _ema(closes, ema_slow)

    # SMA 150
    sma = [None] * len(closes)
    for i in range(sma_long - 1, len(closes)):
        sma[i] = sum(closes[i - sma_long + 1:i + 1]) / sma_long

    # Renko line: tracks the current brick level
    renko_level = closes[sma_long]
    renko_trend = 0  # 1=bullish, -1=bearish
    renko_line = []
    for i in range(sma_long, len(candles)):
        if closes[i] > renko_level + brick_size:
            renko_level = renko_level + brick_size
            renko_trend = 1
        elif closes[i] < renko_level - brick_size:
            renko_level = renko_level - brick_size
            renko_trend = -1
        renko_line.append({'time': candles[i]['t'], 'value': round(renko_level, 2)})

    # Volume filter
    avg_vol = [0] * len(vols)
    for i in range(len(vols)):
        s = max(0, i - 19); w = vols[s:i + 1]; avg_vol[i] = sum(w) / len(w) if w else 1

    # Generate signals: Red Bar entry
    signals = []
    ema10_data = [{'time': candles[i]['t'], 'value': round(ema10[i], 2)} for i in range(ema_fast - 1, len(candles))]
    ema30_data = [{'time': candles[i]['t'], 'value': round(ema30[i], 2)} for i in range(ema_slow - 1, len(candles))]

    # Group candles by trading day for "skip first candle" logic
    import datetime as _dt
    days = {}
    for i, c in enumerate(candles):
        d = _dt.datetime.fromtimestamp(c['t'], _dt.timezone.utc).date()
        days.setdefault(d, []).append(i)

    for day_indices in days.values():
        if len(day_indices) < 3:
            continue
        # Skip first candle of the day (the "X" trap)
        for j in range(1, len(day_indices)):
            i = day_indices[j]
            if i < sma_long or i >= len(candles) - 1:
                continue
            ri = i - sma_long  # renko_line index
            if ri < 0 or ri >= len(renko_line):
                continue

            c = candles[i]
            prev = candles[day_indices[j - 1]] if j > 0 else candles[i - 1]
            rl = renko_line[ri]['value']

            # Bearish setup: price below renko line, EMA10 < EMA30, below SMA150
            bearish = (closes[i] < rl and ema10[i] < ema30[i] and
                      sma[i] is not None and closes[i] < sma[i])
            # Bullish setup: price above renko line, EMA10 > EMA30, above SMA150
            bullish = (closes[i] > rl and ema10[i] > ema30[i] and
                      sma[i] is not None and closes[i] > sma[i])

            # Red Bar: first bearish candle after bullish candle (sell signal)
            if bearish and prev['c'] > prev['o'] and c['c'] < c['o']:
                if avg_vol[i] > 0 and vols[i] < avg_vol[i] * 0.5:
                    continue
                sl = c['h']
                tp = c['c'] - brick_size
                risk = sl - c['c']
                if risk > 0 and (c['c'] - tp) > risk * 0.8:
                    signals.append({'time': c['t'], 'type': 'sell', 'price': round(c['c'], 2),
                                    'sl': round(sl, 2), 'tp1': round(tp, 2)})
                    break  # one signal per day

            # Green Bar: first bullish candle after bearish candle (buy signal)
            if bullish and prev['c'] < prev['o'] and c['c'] > c['o']:
                if avg_vol[i] > 0 and vols[i] < avg_vol[i] * 0.5:
                    continue
                sl = c['l']
                tp = c['c'] + brick_size
                risk = c['c'] - sl
                if risk > 0 and (tp - c['c']) > risk * 0.8:
                    signals.append({'time': c['t'], 'type': 'buy', 'price': round(c['c'], 2),
                                    'sl': round(sl, 2), 'tp1': round(tp, 2)})
                    break  # one signal per day

    return {'type': 'signals', 'signals': signals[-100:], 'renko_line': renko_line, 'ema10': ema10_data, 'ema30': ema30_data}


def calc_next_move(candles):
    """Next Move Prediction Engine: analyzes current state and predicts probable price targets.
    Combines: trend, momentum, S/R levels, volatility range, EMA position, candle patterns."""
    if len(candles) < 50:
        return {'type': 'prediction', 'prediction': {}}

    closes = [c['c'] for c in candles]
    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    last = candles[-1]
    price = last['c']

    # EMAs
    def _ema(data, p):
        k = 2 / (p + 1); e = [data[0]]
        for v in data[1:]: e.append(v * k + e[-1] * (1 - k))
        return e
    ema14 = _ema(closes, 14)
    ema50 = _ema(closes, 50)

    # ATR for volatility
    trs = [highs[0] - lows[0]]
    for i in range(1, len(candles)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    atr14 = sum(trs[-14:]) / 14

    # RSI
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[-14:]]
    losses_r = [max(-d, 0) for d in deltas[-14:]]
    avg_gain = sum(gains) / 14
    avg_loss = sum(losses_r) / 14
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    rsi = round(100 - 100 / (1 + rs), 1)

    # Swing highs/lows for S/R
    lb = 5
    supports, resistances = [], []
    for i in range(lb, len(candles) - lb):
        if lows[i] == min(lows[i-lb:i+lb+1]) and lows[i] < price:
            supports.append(lows[i])
        if highs[i] == max(highs[i-lb:i+lb+1]) and highs[i] > price:
            resistances.append(highs[i])

    nearest_support = max(supports[-5:]) if supports else price - atr14
    nearest_resistance = min(resistances[-5:]) if resistances else price + atr14

    # Trend scoring (-100 to +100)
    score = 0
    # EMA position
    if price > ema14[-1]: score += 20
    else: score -= 20
    if price > ema50[-1]: score += 15
    else: score -= 15
    # EMA slope
    if ema14[-1] > ema14[-3]: score += 10
    else: score -= 10
    # RSI
    if rsi > 60: score += 10
    elif rsi < 40: score -= 10
    # Recent momentum (last 5 candles)
    recent_change = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) > 6 else 0
    if recent_change > 0.5: score += 15
    elif recent_change < -0.5: score -= 15
    # Higher highs / lower lows
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]: score += 10
    elif highs[-1] < highs[-2] and lows[-1] < lows[-2]: score -= 10
    # Last candle
    if last['c'] > last['o']: score += 5
    else: score -= 5

    score = max(-100, min(100, score))

    # Determine bias
    if score > 25: bias = 'BULLISH'
    elif score < -25: bias = 'BEARISH'
    else: bias = 'NEUTRAL'

    # Price targets
    upside_1 = round(price + atr14 * 0.5, 2)
    upside_2 = round(price + atr14, 2)
    upside_3 = round(nearest_resistance, 2)
    downside_1 = round(price - atr14 * 0.5, 2)
    downside_2 = round(price - atr14, 2)
    downside_3 = round(nearest_support, 2)

    # Probability estimate based on score
    if bias == 'BULLISH':
        up_prob = min(75, 50 + abs(score) // 4)
        down_prob = 100 - up_prob
    elif bias == 'BEARISH':
        down_prob = min(75, 50 + abs(score) // 4)
        up_prob = 100 - down_prob
    else:
        up_prob = down_prob = 50

    return {
        'type': 'prediction',
        'prediction': {
            'price': round(price, 2),
            'bias': bias,
            'score': score,
            'up_probability': up_prob,
            'down_probability': down_prob,
            'ema14': round(ema14[-1], 2),
            'ema50': round(ema50[-1], 2),
            'rsi': rsi,
            'atr': round(atr14, 2),
            'nearest_support': round(nearest_support, 2),
            'nearest_resistance': round(nearest_resistance, 2),
            'upside_targets': [upside_1, upside_2, upside_3],
            'downside_targets': [downside_1, downside_2, downside_3],
            'expected_range': [round(price - atr14, 2), round(price + atr14, 2)],
            'volatility_pct': round(atr14 / price * 100, 3),
        }
    }


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
    'box_theory': lambda c: calc_box_theory(c),
    'ema_trendline': lambda c: calc_ema_trendline_breakout(c),
    'ema920_pullback': lambda c: calc_ema920_pullback(c),
    'darvas_box': lambda c: calc_darvas_box(c),
    'fib_retracement': lambda c: calc_fib_retracement(c),
    'fvg': lambda c: calc_fvg(c),
    'supply_demand': lambda c: calc_supply_demand(c),
    'candle_patterns': lambda c: calc_candlestick_patterns(c),
    'vol_imbalance': lambda c: calc_volume_imbalance(c),
    'confluence_scalp': lambda c: calc_confluence_scalp(c),
    'renko_redbar': lambda c: calc_renko_redbar(c),
    'next_move': lambda c: calc_next_move(c),
}


def calc_indicators(candles, indicator_list):
    results = {}
    for name in indicator_list:
        fn = INDICATOR_FNS.get(name)
        if fn:
            results[name] = fn(candles)
    return results
