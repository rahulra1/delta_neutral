"""NSE sector scanner: which sectors are green/red today, and which stocks inside
a sector show signs of big-player buying or selling.

Sources (all public, no broker login needed):
  NSE  /api/allIndices                       live sectoral index % change + advances/declines
  NSE  /api/NextApi/apiClient/marketWatchApi?functionName=getIndicesData&symbol=<index>
                                             live constituents (price, volume, traded value)
       (the old /api/equity-stockIndices endpoint now returns 404)
  NSE  /api/live-analysis-oi-spurts-underlyings   F&O open-interest change per underlying
  NSE  /api/snapshot-capital-market-largedeal     last session's bulk / block deals
  NSE  /api/fiidiiTradeReact                      last session's FII / DII net cash flow
  Yahoo 5m candles (5 days)                        VWAP, opening range (09:15-09:50), ORB / VWAP
                                                   LONG/SHORT on the last completed 5m close, relative volume

"Big player" activity can't be observed directly, so it is inferred from:
  relative volume vs the same time of day on prior sessions, price vs VWAP,
  opening-range breakout, OI build-up (F&O stocks) and last session's bulk/block deals.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from api.chart import _get_session as _yahoo_session

logger = logging.getLogger(__name__)

IST = ZoneInfo('Asia/Kolkata')
NSE_BASE = 'https://www.nseindia.com'
_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# Sector bias thresholds
BIAS_PCT = 0.5          # |sector % change| needed to call it green/red
BIAS_BREADTH = 0.3      # (adv - dec) / (adv + dec) needed to confirm it is broad-based
RVOL_HOT = 1.5          # relative volume that counts as "unusual"
OR_END = dtime(9, 50)   # opening range: 09:15-09:50 incl. the 09:45 candle (same as the NSE Sector Options strategy)
LATE_ENTRY = dtime(14, 30)
STOP_MIN_PCT, STOP_MAX_PCT = 0.4, 1.5   # clamp VWAP-based stop distance (% of entry)

_lock = threading.Lock()
_nse = {'s': None, 'ts': 0}
_cache = {}             # key -> (ts, value)


# --------------------------------------------------------------------------- helpers

def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:
        logger.warning(f"sector_scanner {key} failed: {e}")
        if hit:
            return hit[1]   # serve stale data rather than nothing
        raise
    _cache[key] = (time.time(), val)
    return val


def _session(fresh=False):
    with _lock:
        if fresh or not _nse['s'] or time.time() - _nse['ts'] > 300:
            s = requests.Session()
            s.headers.update({'User-Agent': _UA, 'Accept': 'application/json, text/plain, */*',
                              'Accept-Language': 'en-US,en;q=0.9',
                              'Referer': f'{NSE_BASE}/market-data/live-equity-market'})
            try:
                s.get(NSE_BASE, timeout=10)
            except Exception as e:
                logger.warning(f"NSE session init failed: {e}")
            _nse.update(s=s, ts=time.time())
        return _nse['s']


def _nse_get(path, params=None):
    """GET an NSE JSON endpoint, re-creating the cookie session once on failure."""
    for attempt in (0, 1):
        r = _session(fresh=attempt == 1).get(NSE_BASE + path, params=params, timeout=15)
        if r.ok:
            try:
                return r.json()
            except ValueError:
                pass
    raise RuntimeError(f'NSE {path} -> HTTP {r.status_code}')


def _num(v, default=0.0):
    try:
        return float(str(v).replace(',', ''))
    except (TypeError, ValueError):
        return default


def _breadth(adv, dec):
    adv, dec = _num(adv), _num(dec)
    return round((adv - dec) / (adv + dec), 2) if adv + dec else 0.0


def _bias(pchg, breadth):
    if pchg >= BIAS_PCT and breadth >= BIAS_BREADTH:
        return 'BULLISH'
    if pchg <= -BIAS_PCT and breadth <= -BIAS_BREADTH:
        return 'BEARISH'
    return 'MIXED'


# --------------------------------------------------------------------------- NSE data

def _market_status():
    d = _nse_get('/api/marketStatus')
    cm = next((m for m in d.get('marketState', []) if m.get('market') == 'Capital Market'), {})
    return {'status': cm.get('marketStatus'), 'trade_date': cm.get('tradeDate')}


def _fii_dii():
    rows = _nse_get('/api/fiidiiTradeReact')
    return [{'category': r.get('category'), 'date': r.get('date'),
             'buy': _num(r.get('buyValue')), 'sell': _num(r.get('sellValue')),
             'net': _num(r.get('netValue'))} for r in rows]


def _oi_spurts():
    """symbol -> {oi_pct, oi_chg}. Covers every F&O underlying."""
    d = _nse_get('/api/live-analysis-oi-spurts-underlyings')
    out = {}
    for r in d.get('data', []):
        prev = _num(r.get('prevOI'))
        chg = _num(r.get('changeInOI'))
        out[r['symbol']] = {'oi_chg': chg, 'oi_pct': round(chg / prev * 100, 2) if prev else 0.0}
    return out


def _large_deals():
    """symbol -> list of last session's bulk/block deals."""
    d = _nse_get('/api/snapshot-capital-market-largedeal')
    out = {}
    for kind, key in (('BULK', 'BULK_DEALS_DATA'), ('BLOCK', 'BLOCK_DEALS_DATA')):
        for r in d.get(key) or []:
            out.setdefault(r.get('symbol'), []).append({
                'kind': kind, 'side': r.get('buySell'), 'client': r.get('clientName'),
                'qty': _num(r.get('qty')), 'price': _num(r.get('watp')), 'date': r.get('date')})
    return {'as_on': d.get('as_on_date'), 'by_symbol': out}


def _index_codes():
    """Display name ('NIFTY OIL & GAS') -> market-watch code ('NIFTY OIL AND GAS')."""
    d = _nse_get('/api/NextApi/apiClient/marketWatchApi', params={'functionName': 'getIndexList'})
    codes = {}
    for group in (d.get('data') or {}).values():
        if isinstance(group, dict):
            codes.update(group)
    return codes


GROUPS = {'SECTORAL INDICES': 'Sectoral', 'THEMATIC INDICES': 'Thematic'}


def _sectors_raw():
    d = _nse_get('/api/allIndices')
    sectors, broad = [], []
    for x in d.get('data', []):
        last, opn = _num(x.get('last')), _num(x.get('open'))
        row = {
            'index': x.get('index'),
            'last': last,
            'pchange': _num(x.get('percentChange')),
            'from_open': round((last - opn) / opn * 100, 2) if opn else 0.0,
            'advances': int(_num(x.get('advances'))),
            'declines': int(_num(x.get('declines'))),
            'breadth': _breadth(x.get('advances'), x.get('declines')),
            'pchange_30d': _num(x.get('perChange30d')),
        }
        row['bias'] = _bias(row['pchange'], row['breadth'])
        if x.get('key') in GROUPS:
            row['group'] = GROUPS[x['key']]
            sectors.append(row)
        elif x.get('index') in ('NIFTY 50', 'NIFTY BANK', 'NIFTY MIDCAP 100', 'INDIA VIX'):
            broad.append(row)
    sectors.sort(key=lambda r: r['pchange'], reverse=True)
    return {'sectors': sectors, 'broad': broad, 'timestamp': d.get('timestamp')}


def _constituents(index):
    try:
        code = _cached('index_codes', 86400, _index_codes).get(index, index)
    except Exception:
        code = index
    d = _nse_get('/api/NextApi/apiClient/marketWatchApi',
                 params={'functionName': 'getIndicesData', 'symbol': code})
    body = d.get('data') or {}
    rows = body.get('data') or []
    idx = next((r for r in rows if r.get('priority') == 1), None)
    stocks = [r for r in rows if r.get('priority') != 1 and r.get('symbol')]
    if not stocks:
        raise RuntimeError(f'No constituents returned for {index}')
    return {'index_row': idx, 'stocks': stocks, 'timestamp': body.get('timestamp'),
            'adu': body.get('aduCount') or {}}


# --------------------------------------------------------------------------- Yahoo intraday

def _intraday(symbol):
    """VWAP, opening range, ORB / VWAP signals and time-of-day relative volume from Yahoo 5m candles.

    Same rules as the NSE Sector Options strategy:
      opening range = high / low of 09:15-09:50 (seven 5m candles, incl. 09:45-09:50)
      ORB signal    = last COMPLETED 5m close above the range high (LONG) / below the low (SHORT), else INSIDE
      VWAP          = session VWAP from the completed 5m bars; signal = last 5m close above (LONG) / below (SHORT)
    Yahoo's newest bar is the still-forming one (its timestamp is the last trade, not a 5m boundary) -> excluded."""
    s, crumb = _yahoo_session()
    r = s.get(f'https://query2.finance.yahoo.com/v8/finance/chart/{quote(symbol)}.NS',
              params={'interval': '5m', 'range': '5d', 'crumb': crumb or ''}, timeout=10)
    r.raise_for_status()
    res = (r.json().get('chart', {}).get('result') or [None])[0]
    if not res or not res.get('timestamp'):
        return None
    q = res['indicators']['quote'][0]
    now = datetime.now(IST)
    days = {}
    for i, ts in enumerate(res['timestamp']):
        c, v = q['close'][i], q['volume'][i]
        if c is None or q['high'][i] is None or q['low'][i] is None:
            continue
        t = datetime.fromtimestamp(ts, IST)
        if t.second or t.minute % 5 or t + timedelta(minutes=5) > now:
            continue                                         # still-forming 5m bar
        days.setdefault(t.date(), []).append((t.time(), q['high'][i], q['low'][i], c, v or 0))
    if not days:
        return None
    dates = sorted(days)
    today = days[dates[-1]]
    pv = sum((h + l + c) / 3 * v for _, h, l, c, v in today)
    vol = sum(v for *_, v in today)
    vwap = pv / vol if vol else None
    orb = [b for b in today if b[0] < OR_END]
    now_t, close5 = today[-1][0], today[-1][3]
    or_complete = now_t >= OR_END                            # at least one completed bar after the range
    or_high = round(max(b[1] for b in orb), 2) if orb else None
    or_low = round(min(b[2] for b in orb), 2) if orb else None
    orb_signal = None
    if orb and or_complete:
        orb_signal = 'LONG' if close5 > or_high else 'SHORT' if close5 < or_low else 'INSIDE'
    vwap_signal = None if not vwap else ('LONG' if close5 > vwap else 'SHORT' if close5 < vwap else None)
    # relative volume: today's volume so far vs prior sessions' volume up to the same clock time
    prior = [sum(b[4] for b in days[d] if b[0] <= now_t) for d in dates[:-1]]
    prior = [p for p in prior if p > 0]
    rvol = round(vol / (sum(prior) / len(prior)), 2) if prior and vol else None
    return {
        'session_date': dates[-1].isoformat(),
        'vwap': round(vwap, 2) if vwap else None,
        'or_high': or_high,
        'or_low': or_low,
        'or_complete': or_complete,
        'close_5m': round(close5, 2),
        'orb_signal': orb_signal,
        'vwap_signal': vwap_signal,
        'rvol': rvol,
        # end time of the last completed 5m bar
        'bar_time': (datetime.combine(dates[-1], now_t) + timedelta(minutes=5)).strftime('%H:%M'),
    }


def _intraday_cached(symbol):
    try:
        return _cached(('yahoo', symbol), 60, lambda: _intraday(symbol))
    except Exception:
        return None


# --------------------------------------------------------------------------- analysis

def _buildup(pchg, oi_pct):
    if oi_pct is None or abs(oi_pct) < 0.5 or pchg == 0:
        return None
    if oi_pct > 0:
        return 'Long build-up' if pchg > 0 else 'Short build-up'
    return 'Short covering' if pchg > 0 else 'Long unwinding'


def _analyse(stock, sector_pchg, sector_bias, oi, deals, intra, late):
    ltp = _num(stock.get('lastPrice'))
    pchg = _num(stock.get('pChange'))
    direction = {'BULLISH': 'LONG', 'BEARISH': 'SHORT'}.get(sector_bias) or ('LONG' if pchg >= 0 else 'SHORT')
    sign = 1 if direction == 'LONG' else -1

    vwap = intra.get('vwap') if intra else None
    orh = intra.get('or_high') if intra else None
    orl = intra.get('or_low') if intra else None
    rvol = intra.get('rvol') if intra else None
    orb_sig = intra.get('orb_signal') if intra else None       # LONG / SHORT / INSIDE / None (range forming)
    vwap_sig = intra.get('vwap_signal') if intra else None     # LONG / SHORT on the last completed 5m close
    oi_pct = oi.get('oi_pct') if oi else None
    buildup = _buildup(pchg, oi_pct)

    good_oi = {'LONG': ('Long build-up', 'Short covering'), 'SHORT': ('Short build-up', 'Long unwinding')}
    checks = {
        'rel_strength': sign * (pchg - sector_pchg) > 0,
        'vwap': None if vwap is None else (vwap_sig == direction if vwap_sig else sign * (ltp - vwap) > 0),
        'orb': None if orb_sig is None else orb_sig == direction,
        'rvol': None if rvol is None else rvol >= RVOL_HOT,
        'oi': None if oi is None else buildup in good_oi[direction],
    }
    passed = sum(1 for v in checks.values() if v)
    core = checks['rel_strength'] and checks['vwap'] and checks['rvol']

    plan = None
    if checks['vwap']:
        dist = abs(ltp - vwap) / ltp * 100
        dist = min(max(dist, STOP_MIN_PCT), STOP_MAX_PCT)
        stop = ltp * (1 - sign * dist / 100)
        plan = {'side': direction, 'entry': round(ltp, 2), 'stop': round(stop, 2),
                'stop_pct': round(dist, 2), 'stop_basis': 'VWAP (clamped 0.4-1.5%)'}

    sym_deals = deals.get(stock['symbol'], [])
    deal_net = sum(d['qty'] * (1 if d['side'] == 'BUY' else -1) for d in sym_deals)
    return {
        'symbol': stock['symbol'],
        'name': stock.get('companyName'),
        'ltp': ltp,
        'pchange': pchg,
        'rel_to_sector': round(pchg - sector_pchg, 2),
        'open': _num(stock.get('open')),
        'high': _num(stock.get('dayHigh')),
        'low': _num(stock.get('dayLow')),
        'prev_close': _num(stock.get('previousClose')),
        'volume': _num(stock.get('totalTradedVolume')),
        'value_cr': round(_num(stock.get('totalTradedValue')) / 1e7, 2),
        'vwap': vwap, 'or_high': orh, 'or_low': orl, 'rvol': rvol,
        'close_5m': intra.get('close_5m') if intra else None,
        'orb_signal': orb_sig, 'vwap_signal': vwap_sig,
        'fno': oi is not None,
        'oi_pct': oi_pct,
        'buildup': buildup,
        'deals': sym_deals,
        'deal_bias': 'BUY' if deal_net > 0 else 'SELL' if deal_net < 0 else None,
        'direction': direction,
        'checks': checks,
        'score': passed,
        'setup': ('LATE' if late else direction) if core else ('WATCH' if passed >= 2 else None),
        'plan': plan,
        'bar_time': intra.get('bar_time') if intra else None,
    }


# --------------------------------------------------------------------------- public API

def get_sectors():
    data = dict(_cached('sectors', 20, _sectors_raw))
    try:
        data['market'] = _cached('mstatus', 60, _market_status)
    except Exception:
        data['market'] = None
    try:
        data['fii_dii'] = _cached('fiidii', 1800, _fii_dii)
    except Exception:
        data['fii_dii'] = []
    data['thresholds'] = {'bias_pct': BIAS_PCT, 'bias_breadth': BIAS_BREADTH}
    return data


def get_sector_stocks(index):
    sectors = _cached('sectors', 20, _sectors_raw)['sectors']
    sec = next((s for s in sectors if s['index'] == index), None)
    if sec is None:
        raise ValueError(f'Unknown sector index: {index}')
    cons = _cached(('cons', index), 20, lambda: _constituents(index))
    try:
        oi_map = _cached('oi', 60, _oi_spurts)
    except Exception:
        oi_map = {}
    try:
        deals = _cached('deals', 1800, _large_deals)
    except Exception:
        deals = {'as_on': None, 'by_symbol': {}}

    # Use the live constituent index row for the sector change (fresher than allIndices)
    idx = cons.get('index_row') or {}
    sec_pchg = _num(idx.get('pChange'), sec['pchange'])
    adu = cons.get('adu') or {}
    breadth = _breadth(adu.get('advances', sec['advances']), adu.get('declines', sec['declines']))
    bias = _bias(sec_pchg, breadth)

    symbols = [s['symbol'] for s in cons['stocks']]
    with ThreadPoolExecutor(max_workers=8) as ex:
        intra = dict(zip(symbols, ex.map(_intraday_cached, symbols)))

    now = datetime.now(IST)
    late = now.time() >= LATE_ENTRY
    rows = [_analyse(s, sec_pchg, bias, oi_map.get(s['symbol']), deals['by_symbol'],
                     intra.get(s['symbol']), late) for s in cons['stocks']]
    try:
        from api.stock_news import by_symbol
        news = by_symbol(symbols, extra={s['symbol']: s.get('companyName') for s in cons['stocks']})
    except Exception as e:
        logger.warning(f"sector_scanner news failed: {e}")
        news = {}
    for r in rows:
        n = news.get(r['symbol']) or {}
        r.update({k: n.get(k) for k in ('news_count', 'filings_count', 'news_tier', 'news_direction', 'news_pos', 'news_neg')})
    rows.sort(key=lambda r: (r['setup'] in ('LONG', 'SHORT', 'LATE'), r['score'],
                             r['rvol'] or 0, abs(r['rel_to_sector'])), reverse=True)
    return {
        'index': index,
        'pchange': sec_pchg,
        'breadth': breadth,
        'advances': int(_num(adu.get('advances', sec['advances']))),
        'declines': int(_num(adu.get('declines', sec['declines']))),
        'bias': bias,
        'stocks': rows,
        'timestamp': cons.get('timestamp'),
        'deals_as_on': deals.get('as_on'),
        'late': late,
        'intraday_missing': sum(1 for v in intra.values() if not v),
        'params': {'rvol_hot': RVOL_HOT, 'stop_min_pct': STOP_MIN_PCT, 'stop_max_pct': STOP_MAX_PCT,
                   'late_entry': LATE_ENTRY.strftime('%H:%M')},
    }
