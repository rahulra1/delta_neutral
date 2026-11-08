"""Crypto scanner for Delta Exchange perpetual FUTURES.

Same idea as the NSE Sector Scanner (api/sector_scanner.py): which crypto sectors are green / red,
which coins inside the sector show signs of big-player buying or selling, and the news behind them.

Sources (all public, no login):
  Delta /v2/tickers                  one call: price, 24h change, 24h turnover, OI, 6h OI change, funding,
                                     and Delta's own sector tags (layer_1, defi, ai, meme, ...)
  Delta /v2/products                 contract value (coins per lot) for position sizing
  Delta /v2/history/candles  1h      8 days per coin: 1h / 4h / 6h change, 24h VWAP, relative volume
  News  api.catalyst_feeds           RSS headlines (CoinDesk, Cointelegraph, The Block, Decrypt, CryptoSlate)
                                     graded A strong / B moderate / C rumour / R unlock
        api.listing_feeds            Upbit (Korea) listing / delisting notices - historically the biggest movers
        api.coinmarketcal            dated upcoming events per coin (impact score), cached pages only

"Big player" activity is inferred, not observed: relative volume, price vs VWAP, and OI build-up
(price up + OI up = new longs; price down + OI up = new shorts).  Crypto trades 24/7, so the
"day" is a rolling 24 hours rather than a session.
"""

import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

UTC = timezone.utc
DELTA = 'https://api.india.delta.exchange'      # production market data (not the demo testnet)
HEADERS = {'User-Agent': 'Mozilla/5.0'}

SECTORS = {                                       # Delta tag -> display name
    'layer_1': 'Layer 1', 'layer_2': 'Layer 2', 'smart_contracts': 'Smart Contracts', 'defi': 'DeFi',
    'ai': 'AI', 'meme': 'Meme', 'gaming': 'Gaming', 'nft': 'NFT', 'new': 'New Listings', 'sol_ecosystem': 'Solana Ecosystem',
}
OTHER = 'Other (untagged)'
EXCLUDE_TAGS = {'xStock', 'metal'}                 # tokenized stocks / metals are not crypto

BIAS_PCT = 1.5          # |sector median 24h %| needed for BULLISH / BEARISH (crypto moves more than stocks)
BIAS_BREADTH = 0.3      # (up - down) / (up + down)
RVOL_HOT = 1.5          # last-24h volume vs the average of the previous 7 days
OI_BUILD_PCT = 2.0      # 6h OI change needed to call it a build-up / unwinding
FUNDING_HOT = 0.05      # % per funding interval: longs above this (or shorts below -this) are crowded
STOP_MIN_PCT, STOP_MAX_PCT = 0.75, 3.0
MIN_TURNOVER = 250_000  # 24h USD turnover needed for a LONG/SHORT setup (thinner books = noisy signals, bad fills)
RSI_LEN = 14            # 1h RSI
EMA_FAST, EMA_SLOW = 7, 20   # 1h EMA crossover (user's choice: 7 / 20)
FRESH_CROSS_BARS = 3    # a cross within this many 1h bars counts as "fresh"
# Weighted rating (0-100, 50 = neutral). Each component is scored -1..+1 IN THE TRADE DIRECTION; a component with
# no data is left out and the remaining weights are re-normalised. The UI lets the user change these weights.
# User's weights (Nov 2026): Twitter 25%, News 35%, Technical 40% = RSI 20% + EMA 7/20 crossover 20%.
# 'setup' (the scanner's RS / VWAP / RVOL / OI / funding checks) is available but weighted 0 by default.
WEIGHTS = {'news': 35, 'twitter': 25, 'rsi': 20, 'ema': 20, 'setup': 0}
TAKER_FEE = 0.0005 * 1.18                          # Delta taker 0.05% + 18% GST, per side
MAJOR_3 = {'BTC', 'ETH', 'XRP', 'SOL', 'BNB', 'ADA', 'TRX', 'LTC', 'DOT', 'UNI', 'TON', 'SUI', 'APT', 'ZEC', 'XMR'}

_cache = {}
_lock = threading.Lock()


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:
        logger.warning('crypto_scanner %s failed: %s', key, e)
        if hit:
            return hit[1]
        raise
    _cache[key] = (time.time(), val)
    return val


def _get(path, params=None, timeout=20):
    for attempt in range(3):
        r = requests.get(DELTA + path, params=params, headers=HEADERS, timeout=timeout)
        if r.status_code == 429:
            time.sleep(1 + attempt)
            continue
        r.raise_for_status()
        return r.json()['result']
    r.raise_for_status()


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _breadth(up, down):
    return round((up - down) / (up + down), 2) if up + down else 0.0


def _bias(pchg, breadth):
    if pchg >= BIAS_PCT and breadth >= BIAS_BREADTH:
        return 'BULLISH'
    if pchg <= -BIAS_PCT and breadth <= -BIAS_BREADTH:
        return 'BEARISH'
    return 'MIXED'


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2) if n else 0.0


# --------------------------------------------------------------------------- market data

def _products():
    """{symbol: {'name', 'contract_value', 'ticker'}} for crypto perps (cached 6h)."""
    out = {}
    for p in _get('/v2/products', {'contract_types': 'perpetual_futures', 'page_size': 500}, timeout=30):
        tags = set((p.get('product_specs') or {}).get('tags') or [])
        if tags & EXCLUDE_TAGS or p.get('state') not in (None, 'live'):
            continue
        base = p['underlying_asset']['symbol']
        out[p['symbol']] = {'name': (p['underlying_asset'].get('name') or base).strip(),
                            'contract_value': _f(p.get('contract_value'), 1.0),
                            'product_id': p.get('id'),
                            'ticker': re.sub(r'^(1000|1M)', '', base)}
    return out


def _tickers():
    prods = _cached('products', 6 * 3600, _products)
    rows = []
    for t in _get('/v2/tickers', {'contract_types': 'perpetual_futures'}):
        sym = t.get('symbol')
        tags = set(t.get('tags') or [])
        if sym not in prods or tags & EXCLUDE_TAGS or t.get('product_trading_status') not in (None, 'operational'):
            continue
        price = _f(t.get('mark_price')) or _f(t.get('close'))
        oi_usd = _f(t.get('oi_value_usd'))
        oi6 = _f(t.get('oi_change_usd_6h'))
        prev = oi_usd - oi6
        rows.append({
            'symbol': sym, 'ticker': prods[sym]['ticker'], 'name': prods[sym]['name'],
            'product_id': prods[sym].get('product_id'),
            'contract_value': prods[sym]['contract_value'], 'price': price,
            'pchange': round(_f(t.get('mark_change_24h')), 2),          # already a % (not a fraction)
            'high': _f(t.get('high')), 'low': _f(t.get('low')),
            'turnover_usd': _f(t.get('turnover_usd')), 'oi_usd': oi_usd,
            'oi_chg_6h_pct': round(oi6 / prev * 100, 2) if prev > 0 else None,
            'funding': _f(t.get('funding_rate')),                     # % per funding interval
            'tags': sorted(tags & set(SECTORS)),
        })
    return rows


def _sector_rows(tickers):
    groups = {}
    for r in tickers:
        for tag in (r['tags'] or [None]):
            groups.setdefault(SECTORS.get(tag, OTHER), []).append(r)
    out = []
    for name, rs in groups.items():
        rs = [r for r in rs if r['ticker'] not in ('USDT', 'USDC')]
        if len(rs) < 3:
            continue
        up = sum(r['pchange'] > 0 for r in rs)
        dn = sum(r['pchange'] < 0 for r in rs)
        med = round(_median([r['pchange'] for r in rs]), 2)
        tv = sum(r['turnover_usd'] for r in rs)
        breadth = _breadth(up, dn)
        leaders = sorted(rs, key=lambda r: -r['turnover_usd'])[:3]
        out.append({'sector': name, 'pchange': med,
                    'tv_weighted': round(sum(r['pchange'] * r['turnover_usd'] for r in rs) / tv, 2) if tv else med,
                    'advances': up, 'declines': dn, 'coins': len(rs), 'breadth': breadth,
                    'bias': _bias(med, breadth), 'turnover_usd': tv,
                    'oi_chg_6h_pct': round(_median([r['oi_chg_6h_pct'] for r in rs if r['oi_chg_6h_pct'] is not None]), 2),
                    'leaders': [r['ticker'] for r in leaders]})
    out.sort(key=lambda s: s['pchange'], reverse=True)
    return out


def _candles(symbol, hours=8 * 24):
    end = int(time.time()) // 3600 * 3600
    rows = _get('/v2/history/candles', {'resolution': '1h', 'symbol': symbol, 'start': end - hours * 3600,
                                        'end': end + 3600})
    return sorted(({'t': int(c['time']), 'o': _f(c['open']), 'h': _f(c['high']), 'l': _f(c['low']),
                    'c': _f(c['close']), 'v': _f(c.get('volume'))} for c in rows), key=lambda c: c['t'])


def _rsi(closes, n=14):
    """Wilder RSI of the last close (None if not enough bars)."""
    if len(closes) < n + 1:
        return None
    gains = [max(0.0, b - a) for a, b in zip(closes, closes[1:])]
    losses = [max(0.0, a - b) for a, b in zip(closes, closes[1:])]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 1)


def _ema_series(closes, n):
    k, out = 2 / (n + 1), []
    for c in closes:
        out.append(c if not out else c * k + out[-1] * (1 - k))
    return out


def _ema_cross(closes, fast=EMA_FAST, slow=EMA_SLOW):
    """{'state': 'BULL'|'BEAR', 'bars_since_cross': int|None, 'fast', 'slow'} on closed 1h bars."""
    if len(closes) < slow * 3:
        return None
    ef, es = _ema_series(closes, fast), _ema_series(closes, slow)
    diff = [a - b for a, b in zip(ef, es)][slow * 2:]               # skip the warm-up
    state = 'BULL' if diff[-1] > 0 else 'BEAR'
    since = next((i for i in range(1, len(diff)) if (diff[-i] > 0) != (diff[-i - 1] > 0)), None)
    return {'state': state, 'bars_since_cross': since, 'fast': round(ef[-1], 8), 'slow': round(es[-1], 8)}


def _intraday(symbol):
    """1h / 4h / 6h change, rolling-24h VWAP, 24h and 1h relative volume, RSI and EMA cross from 1h candles."""
    now = time.time()
    cs = [c for c in _candles(symbol) if c['t'] <= now]          # Delta also returns the next (empty) hour
    closed = [c for c in cs if c['t'] + 3600 <= now]
    if len(closed) < 30:
        return None
    last = cs[-1]['c']
    day = closed[-24:]
    vol24 = sum(c['v'] for c in day)
    pv = sum((c['h'] + c['l'] + c['c']) / 3 * c['v'] for c in day)
    prior = closed[:-24]
    days = [sum(c['v'] for c in prior[i:i + 24]) for i in range(0, len(prior) - 23, 24)]
    days = [d for d in days if d > 0]
    hourly = [c['v'] for c in prior if c['v'] > 0]
    ch = lambda k: round((last / closed[-k]['c'] - 1) * 100, 2) if len(closed) >= k and closed[-k]['c'] else None  # noqa: E731
    closes = [c['c'] for c in closed]
    return {
        'chg_1h': ch(1), 'chg_4h': ch(4), 'chg_6h': ch(6),
        'vwap': round(pv / vol24, 8) if vol24 else None,
        'rvol': round(vol24 / (sum(days) / len(days)), 2) if days and vol24 else None,
        'rvol_1h': round(closed[-1]['v'] / (sum(hourly) / len(hourly)), 2) if hourly and closed[-1]['v'] else None,
        'high_24h': max(c['h'] for c in day), 'low_24h': min(c['l'] for c in day),
        'spark': [round(c['c'], 8) for c in closed[-48:]],
        'rsi': _rsi(closes, RSI_LEN), 'ema': _ema_cross(closes),
    }


def _buildup(p6, oi6):
    if p6 is None or oi6 is None or abs(oi6) < OI_BUILD_PCT or p6 == 0:
        return None
    if oi6 > 0:
        return 'Long build-up' if p6 > 0 else 'Short build-up'
    return 'Short covering' if p6 > 0 else 'Long unwinding'


def _analyse(r, sec_pchg, bias, intra, news):
    direction = {'BULLISH': 'LONG', 'BEARISH': 'SHORT'}.get(bias) or ('LONG' if r['pchange'] >= 0 else 'SHORT')
    sign = 1 if direction == 'LONG' else -1
    vwap = intra.get('vwap') if intra else None
    rvol = intra.get('rvol') if intra else None
    p6 = intra.get('chg_6h') if intra else None
    buildup = _buildup(p6, r['oi_chg_6h_pct'])
    good_oi = {'LONG': ('Long build-up', 'Short covering'), 'SHORT': ('Short build-up', 'Long unwinding')}
    funding = r['funding']
    checks = {
        'rel_strength': sign * (r['pchange'] - sec_pchg) > 0,
        'vwap': None if vwap is None else sign * (r['price'] - vwap) > 0,
        'rvol': None if rvol is None else rvol >= RVOL_HOT,
        'oi': None if r['oi_chg_6h_pct'] is None else buildup in good_oi[direction],
        'funding': not (sign * funding > FUNDING_HOT),               # not crowded on our side
    }
    passed = sum(1 for v in checks.values() if v)
    thin = r['turnover_usd'] < MIN_TURNOVER
    core = checks['rel_strength'] and checks['vwap'] and checks['rvol'] and not thin
    plan = None
    if checks['vwap']:
        dist = min(max(abs(r['price'] - vwap) / r['price'] * 100, STOP_MIN_PCT), STOP_MAX_PCT)
        plan = {'side': direction, 'entry': r['price'], 'stop': r['price'] * (1 - sign * dist / 100),
                'stop_pct': round(dist, 2), 'stop_basis': f'24h VWAP (clamped {STOP_MIN_PCT}-{STOP_MAX_PCT}%)'}
    n = news or {}
    avail = [v for v in checks.values() if v is not None]
    ema = intra.get('ema') if intra else None
    rsi = intra.get('rsi') if intra else None
    components = {
        # no graded headline / listing = neutral 0 (news WAS checked), not "unknown": otherwise a coin with no news
        # would get the full 100% weight on technicals and look better than a coin with mixed news
        'news': news_signal(n) or 0.0,
        'twitter': None,                                  # filled in by get_sector_coins when X is configured
        'rsi': rsi_signal(rsi, sign),
        'ema': ema_signal(ema, sign),
        'setup': round(2 * sum(1 for v in avail if v) / len(avail) - 1, 3) if avail else None,
    }
    if components['news']:
        components['news'] = round(sign * components['news'], 3)       # bullish news helps a LONG, hurts a SHORT
    return dict(r, rel_to_sector=round(r['pchange'] - sec_pchg, 2), direction=direction, buildup=buildup, thin=thin,
                checks=checks, score=passed, setup=direction if core else ('WATCH' if passed >= 3 else None),
                plan=plan, **({k: intra[k] for k in ('chg_1h', 'chg_4h', 'chg_6h', 'vwap', 'rvol', 'rvol_1h', 'spark')}
                              if intra else {'chg_1h': None, 'chg_4h': None, 'chg_6h': None, 'vwap': None,
                                             'rvol': None, 'rvol_1h': None, 'spark': []}),
                rsi=rsi, ema_state=ema['state'] if ema else None,
                ema_bars_since_cross=ema['bars_since_cross'] if ema else None,
                ema_fresh=bool(ema and ema['bars_since_cross'] is not None and ema['bars_since_cross'] <= FRESH_CROSS_BARS),
                components=components, rating=rating(components), x=None,
                news_count=len(n.get('headlines', [])), news_tier=n.get('top_tier'),
                events_count=len(n.get('events', [])), listing=n.get('listing'))


# --------------------------------------------------------------------------- news

def _universe_for_news():
    """{ticker: {'symbol','name'}} incl. BTC / ETH (catalyst_feeds' own universe skips the majors)."""
    return {p['ticker']: {'symbol': s, 'name': p['name']} for s, p in _cached('products', 6 * 3600, _products).items()}


def _match(text, uni):
    from api.catalyst_feeds import match_coins
    found = match_coins(text, uni, max_coins=4)
    for tk in MAJOR_3:                                   # catalyst_feeds needs "$XRP" / "(XRP)" for 3-letter tickers
        if tk in uni and tk not in found and re.search(r'(?<![A-Za-z0-9])%s(?![A-Za-z0-9])' % tk, text):
            found.append(tk)
    return found[:4]


def _headlines():
    from api.catalyst_feeds import fetch_all_rss, grade
    items, errors = fetch_all_rss()
    uni = _universe_for_news()
    now = time.time()
    out = []
    for it in items:
        if now - it['ts'] > 3 * 86400 or it['ts'] > now + 600:
            continue
        tickers = _match(it['title'], uni) or _match(it['title'] + '. ' + it['summary'], uni)
        tier, cat = grade(it['title'] + '. ' + it['summary'][:200])
        out.append({'title': it['title'], 'summary': it['summary'][:280], 'link': it['link'], 'source': it['source'],
                    'ts': int(it['ts']), 'tickers': tickers, 'tier': tier, 'category': cat})
    out.sort(key=lambda x: -x['ts'])
    return {'items': out, 'errors': errors}


def _listings():
    """Upbit listing / delisting notices of the last 7 days that involve a Delta perp."""
    from api.listing_feeds import upbit_notices, upbit_ts, parse_listing
    uni = _universe_for_news()
    notices, _ = upbit_notices()
    out = []
    for n in notices:
        ts = upbit_ts(n)
        if time.time() - ts > 7 * 86400:
            continue
        title = n.get('title', '')
        parsed = parse_listing(title)
        if parsed:
            kind, tks = parsed
        elif '종료' in title or '유의' in title:
            kind = 'delist' if '종료' in title else 'caution'
            tks = [t for t in re.findall(r'\(([A-Z0-9]{2,12})\)', title)]
        else:
            continue
        tks = [t for t in tks if t in uni]
        if tks:
            out.append({'exchange': 'Upbit', 'kind': kind, 'tickers': tks, 'title': title, 'ts': ts,
                        'link': f"https://upbit.com/service_center/notice?id={n.get('id')}"})
    return out


def _events_for(tickers):
    """CoinMarketCal events from the last 7 days to the next 30 days (coin pages cached 12h)."""
    from api import coinmarketcal as cm
    try:
        mapping = cm._cached_json('delta_map.json', 30 * 86400, lambda: {'map': {}}).get('map', {})
    except Exception:
        mapping = {}
    mapping = {'BTC': 'bitcoin', 'ETH': 'ethereum', **mapping}      # the alert universe skips the majors
    today = datetime.now(UTC).date()
    out = {}
    for tk in tickers:
        slug = mapping.get(tk)
        if not slug:
            continue
        try:
            page = cm.coin_page(slug, 12 * 3600)
        except Exception:
            page = None
        for ev in (page or {}).get('events', []):
            d, prec = cm.resolve_date(ev, today)
            if d and today - timedelta(days=7) <= d <= today + timedelta(days=30):
                out.setdefault(tk, []).append({'past': d < today, 'title': ev.get('title'), 'category': ev.get('category'),
                                               'impact': ev.get('impact'), 'date': d.isoformat(), 'precision': prec,
                                               'url': cm.BASE + '/event/' + ev['slug']})
    for v in out.values():
        v.sort(key=lambda e: e['date'])
    return out


def _news_by_ticker(tickers, with_events=True):
    heads = _cached('headlines', 300, _headlines)['items']
    try:
        lists = _cached('listings', 300, _listings)
    except Exception:
        lists = []
    events = _cached(('events', tuple(sorted(tickers))), 3600, lambda: _events_for(tickers)) if with_events else {}
    out = {}
    for tk in tickers:
        h = [x for x in heads if tk in x['tickers']]
        tiers = [x['tier'] for x in h if x['tier']]
        lst = next((x for x in lists if tk in x['tickers']), None)
        out[tk] = {'headlines': h[:10], 'top_tier': min(tiers) if tiers else None, 'events': events.get(tk, []),
                   'listing': lst}
    return out


# --------------------------------------------------------------------------- weighted rating
NEWS_POS = re.compile(r"\b(approv\w+|lists?|listing|listed|launch\w*|mainnet|upgrade\w*|partner\w*|integrat\w+|"
                      r"buy-?backs?|burns?|burned|adopt\w+|inflows?|surg\w+|rall\w+|soar\w*|jumps?|record|all-time high|"
                      r"bullish|raises?|raised|funding|acquir\w+|accumulat\w+|wins?|green-?light\w*)\b", re.I)
NEWS_NEG = re.compile(r"\b(hack\w*|exploit\w*|drain\w*|stolen|breach\w*|delist\w*|lawsuits?|sues?|sued|charg(?:es|ed)|"
                      r"fraud|scam|rug|bans?|banned|halt\w*|outages?|liquidat\w+|sell-?off|dump\w*|plung\w+|crash\w*|"
                      r"investigat\w+|probe|fines?|fined|penalt\w+|unlocks?|vesting|bearish|downgrad\w+|suspend\w*|"
                      r"outflows?|tumbl\w+|slump\w*)\b", re.I)
TIER_W = {'A': 1.0, 'B': 0.6, 'C': 0.3, 'R': 0.8}


def news_direction(text, tier=None):
    """+1 bullish / -1 bearish / 0 neutral headline. A token unlock (tier R) is always bearish."""
    if tier == 'R':
        return -1
    p, n = len(NEWS_POS.findall(text)), len(NEWS_NEG.findall(text))
    return 1 if p > n else -1 if n > p else 0


def news_signal(news, now=None):
    """Net news direction -1..+1 from graded headlines (half-life 24h) + exchange listing notices. None = no news."""
    now = now or time.time()
    total, seen = 0.0, 0
    for h in (news or {}).get('headlines', []):
        d = news_direction(h['title'] + '. ' + h.get('summary', ''), h.get('tier'))
        if not d:
            continue
        w = TIER_W.get(h.get('tier'), 0.3)
        age_h = max(0.0, (now - h['ts']) / 3600)
        total += d * w * 0.5 ** (age_h / 24)
        seen += 1
    lst = (news or {}).get('listing')
    if lst:
        total += 1.0 if lst['kind'] in ('new', 'add') else -1.0
        seen += 1
    if not seen:
        return None
    return round(math.tanh(total), 3)


def rsi_signal(rsi, sign):
    """RSI in the trade direction: momentum zone 50-70 (long) / 30-50 (short) = +1, stretched = small, wrong side = -1."""
    if rsi is None:
        return None
    r = rsi if sign > 0 else 100 - rsi
    if 50 <= r <= 70:
        return 1.0
    if 70 < r <= 80:
        return 0.3
    if r > 80:
        return -0.5                                  # overbought (long) / oversold (short): late, risk of snap-back
    if 45 <= r < 50:
        return -0.3
    return -1.0


def ema_signal(ema, sign):
    """EMA 7/20 on 1h in the trade direction: aligned = +1 (fresh cross in our favour too), against = -1."""
    if not ema:
        return None
    aligned = (ema['state'] == 'BULL') == (sign > 0)
    return 1.0 if aligned else -1.0


def rating(components, weights=None):
    """Weighted 0-100 (50 neutral) from -1..+1 components; missing components drop out and weights re-normalise."""
    w = weights or WEIGHTS
    num = den = 0.0
    for k, v in components.items():
        if v is None or not w.get(k):
            continue
        num += w[k] * v
        den += w[k]
    return round(50 + 50 * num / den, 1) if den else None


# --------------------------------------------------------------------------- public API

def get_overview():
    tick = _cached('tickers', 20, _tickers)
    sectors = _sector_rows(tick)
    majors = {r['ticker']: r for r in tick if r['ticker'] in ('BTC', 'ETH', 'SOL', 'XRP')}
    up = sum(r['pchange'] > 0 for r in tick)
    dn = sum(r['pchange'] < 0 for r in tick)
    return {
        'sectors': sectors,
        'majors': [majors[k] for k in ('BTC', 'ETH', 'SOL', 'XRP') if k in majors],
        'market': {'coins': len(tick), 'advances': up, 'declines': dn, 'breadth': _breadth(up, dn),
                   'median_pchange': round(_median([r['pchange'] for r in tick]), 2),
                   'turnover_usd': sum(r['turnover_usd'] for r in tick),
                   'oi_usd': sum(r['oi_usd'] for r in tick)},
        'gainers': sorted(tick, key=lambda r: -r['pchange'])[:5],
        'losers': sorted(tick, key=lambda r: r['pchange'])[:5],
        'timestamp': datetime.now(UTC).isoformat(),
        'thresholds': {'bias_pct': BIAS_PCT, 'bias_breadth': BIAS_BREADTH},
    }


def get_sector_coins(sector):
    tick = _cached('tickers', 20, _tickers)
    sec = next((s for s in _sector_rows(tick) if s['sector'] == sector), None)
    if sec is None:
        raise ValueError(f'Unknown sector: {sector}')
    tag = next((k for k, v in SECTORS.items() if v == sector), None)
    coins = [r for r in tick if (tag in r['tags'] if tag else not r['tags'])]
    with ThreadPoolExecutor(max_workers=8) as ex:
        intra = dict(zip([c['symbol'] for c in coins],
                         ex.map(lambda c: _safe_intraday(c['symbol']), coins)))
    news = _news_by_ticker([c['ticker'] for c in coins], with_events=False)
    rows = [_analyse(c, sec['pchange'], sec['bias'], intra.get(c['symbol']), news.get(c['ticker'])) for c in coins]
    _add_twitter(rows)
    rows.sort(key=lambda r: (r['setup'] in ('LONG', 'SHORT'), r['score'], r['rvol'] or 0, abs(r['rel_to_sector'])),
              reverse=True)
    from api import x_social
    return dict(sec, stocks=rows, intraday_missing=sum(1 for v in intra.values() if not v),
                timestamp=datetime.now(UTC).isoformat(),
                params={'rvol_hot': RVOL_HOT, 'min_turnover': MIN_TURNOVER, 'oi_build_pct': OI_BUILD_PCT, 'funding_hot': FUNDING_HOT,
                        'stop_min_pct': STOP_MIN_PCT, 'stop_max_pct': STOP_MAX_PCT, 'taker_fee': TAKER_FEE,
                        'weights': WEIGHTS, 'rsi_len': RSI_LEN, 'ema_fast': EMA_FAST, 'ema_slow': EMA_SLOW,
                        'fresh_cross_bars': FRESH_CROSS_BARS, 'x_configured': x_social.configured(),
                        'x_max_coins': X_MAX_COINS})


X_MAX_COINS = 10        # X recent search is rate-limited: only the most promising coins of a sector get Twitter data


def _add_twitter(rows):
    """Twitter/X sentiment (official API, cached 30 min) for the sector's setups / WATCH coins, best first."""
    from api import x_social
    if not x_social.configured():
        return
    cands = sorted([r for r in rows if r['setup'] in ('LONG', 'SHORT', 'WATCH')],
                   key=lambda r: (r['setup'] != 'WATCH', r['score'], r['turnover_usd']), reverse=True)[:X_MAX_COINS]
    with ThreadPoolExecutor(max_workers=4) as ex:
        res = list(ex.map(lambda r: x_social.sentiment(r['ticker']), cands))
    sign_of = {'LONG': 1, 'SHORT': -1}
    for r, x in zip(cands, res):
        if not x:
            continue
        r['x'] = {k: x[k] for k in ('score', 'tweets', 'bullish', 'bearish')}
        r['components']['twitter'] = round(sign_of[r['direction']] * x['score'], 3)
        r['rating'] = rating(r['components'])


def _safe_intraday(sym):
    try:
        return _cached(('intra', sym), 120, lambda: _intraday(sym))
    except Exception:
        return None


def get_news(limit=60):
    """Market-wide news board: graded headlines (newest first) + exchange listing notices."""
    h = _cached('headlines', 300, _headlines)
    try:
        lists = _cached('listings', 300, _listings)
    except Exception as e:
        lists, h = [], dict(h, errors=h['errors'] + [f'Upbit: {e}'])
    return {'headlines': h['items'][:limit], 'listings': lists, 'errors': h['errors'],
            'timestamp': datetime.now(UTC).isoformat()}


def get_coin_news(ticker):
    ticker = ticker.upper()
    if ticker not in _universe_for_news():
        raise ValueError(f'Unknown coin: {ticker}')
    from api import x_social
    n = _news_by_ticker([ticker])[ticker]
    for h in n['headlines']:
        h['direction'] = news_direction(h['title'] + '. ' + h.get('summary', ''), h.get('tier'))
    return dict(n, ticker=ticker, news_score=news_signal(n), x_configured=x_social.configured(),
                x=x_social.sentiment(ticker) if x_social.configured() else None)
