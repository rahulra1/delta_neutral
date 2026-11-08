"""Twitter / X sentiment for a crypto ticker, from the OFFICIAL X API v2 recent search.

Free scrapers / Nitter mirrors are dead or blocked (checked Nov 2026: nitter.net 410, xcancel 451, X API without
a token 401), so this needs an X API bearer token with search access (the free X tier has no search):
    X_BEARER_TOKEN=...   in the environment or the project-root .env  (never committed)
Without it every call returns None and the scanner leaves Twitter out of the weighted rating.

GET https://api.x.com/2/tweets/search/recent
    query = ($SOL OR #SOL) lang:en -is:retweet      tweet.fields = created_at,public_metrics   max_results = 50
Each tweet's direction comes from crypto keywords (bullish / moon / breakout ... vs bearish / dump / rug / hack ...),
weighted by engagement log(1 + likes + 2 x retweets + replies). Score = engagement-weighted net direction (-1..+1),
0 when fewer than MIN_DIRECTIONAL tweets carry a direction. Results are cached CACHE_TTL seconds per ticker
because X rate-limits recent search per 15-minute window.
"""

import logging
import math
import os
import re
import threading
import time

import requests

logger = logging.getLogger(__name__)

URL = 'https://api.x.com/2/tweets/search/recent'
CACHE_TTL = 30 * 60
MIN_DIRECTIONAL = 3
_cache, _lock = {}, threading.Lock()
_blocked_until = {'t': 0.0}

POS = re.compile(r"\b(bull(ish)?|moon(ing)?|pump(ing)?|breakout|breaking out|ath|all[- ]time high|rally|rallying|surg\w+|"
                 r"soar\w*|rip(ping)?|send(ing)? it|long(ing)?|buy(ing)?|accumulat\w+|undervalued|listing|listed|"
                 r"partnership|upgrade|mainnet|etf approv\w*|bought|green)\b", re.I)
NEG = re.compile(r"\b(bear(ish)?|dump(ing|ed)?|rug(ged| pull)?|scam|hack(ed)?|exploit(ed)?|drain(ed)?|crash(ing|ed)?|"
                 r"plung\w+|tank(ing|ed)?|short(ing)?|sell(ing)?|sold|delist\w*|unlock\w*|overvalued|rekt|liquidat\w+|"
                 r"red|bleed(ing)?|lawsuit|sec charges|fraud)\b", re.I)


def _token():
    tok = os.environ.get('X_BEARER_TOKEN')
    if tok:
        return tok.strip()
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
        with open(path) as f:
            for line in f:
                if line.strip().startswith('X_BEARER_TOKEN='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def configured():
    return bool(_token())


def direction(text):
    p, n = len(POS.findall(text)), len(NEG.findall(text))
    return 1 if p > n else -1 if n > p else 0


def score_tweets(tweets):
    """[{'text', 'public_metrics': {...}}] -> (score -1..+1 or 0, stats)."""
    num = den = 0.0
    pos = neg = 0
    for t in tweets:
        d = direction(t.get('text', ''))
        if not d:
            continue
        m = t.get('public_metrics') or {}
        w = math.log1p(m.get('like_count', 0) + 2 * m.get('retweet_count', 0) + m.get('reply_count', 0)) + 1
        num += d * w
        den += w
        pos += d > 0
        neg += d < 0
    s = num / den if den and pos + neg >= MIN_DIRECTIONAL else 0.0
    return round(s, 3), {'tweets': len(tweets), 'bullish': pos, 'bearish': neg}


def sentiment(ticker):
    """{'score', 'tweets', 'bullish', 'bearish', 'top': [...]} or None (not configured / failed / rate-limited)."""
    tok = _token()
    if not tok or time.time() < _blocked_until['t']:
        return None
    key = ticker.upper()
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    q = f'(${key} OR #{key}) lang:en -is:retweet'
    try:
        r = requests.get(URL, headers={'Authorization': f'Bearer {tok}'}, timeout=10,
                         params={'query': q, 'max_results': 50, 'tweet.fields': 'created_at,public_metrics'})
        if r.status_code == 429:
            reset = float(r.headers.get('x-rate-limit-reset') or time.time() + 900)
            _blocked_until['t'] = reset
            logger.warning("x_social: rate-limited until %s", time.strftime('%H:%M', time.localtime(reset)))
            return None
        if r.status_code in (401, 403):
            _blocked_until['t'] = time.time() + 3600
            logger.warning("x_social: X API %s — token invalid or no search access: %s", r.status_code, r.text[:200])
            return None
        r.raise_for_status()
        tweets = r.json().get('data') or []
    except Exception as e:                          # noqa: BLE001
        logger.warning("x_social %s failed: %s", key, e)
        return None
    s, stats = score_tweets(tweets)
    top = sorted(tweets, key=lambda t: -sum((t.get('public_metrics') or {}).get(k, 0)
                                             for k in ('like_count', 'retweet_count')))[:5]
    out = {'score': s, **stats, 'top': [{'text': t.get('text', '')[:280], 'created_at': t.get('created_at'),
                                         'likes': (t.get('public_metrics') or {}).get('like_count', 0),
                                         'id': t.get('id')} for t in top]}
    with _lock:
        _cache[key] = (time.time(), out)
    return out
