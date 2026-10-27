"""News / catalyst sources and grading for Delta-listed coins (all public, no API keys needed).

Sources
  * RSS  : CoinDesk, Cointelegraph, The Block, Decrypt, CryptoSlate, Kraken blog        (fresh news + dated upcoming events)
  * Snapshot GraphQL (hub.snapshot.org): pending/active DAO votes for a few Delta coins  (scheduled governance catalysts)
  * CoinMarketCal (OPTIONAL, set COINMARKETCAL_API_KEY): curated future events           (UNTESTED - no key available here)

Grading (user's framework)
  A strong   : ETF approval/launch/inflows, major exchange listing, institutional purchase, regulatory approval, major upgrade going live
  B moderate : partnership, ecosystem funding, staking / buyback-burn changes, adoption announcement, governance vote
  C weak     : rumors, influencer posts, generic roadmap, unverified claims   (never alerted unless asked)
  R risk     : token unlock (supply event) - listed separately, not a positive catalyst

Headline matching is keyword based and WILL produce some false matches and misses; the alert always shows the headline.
"""
import html
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from api.listing_feeds import DELTA_URL, HEADERS, http_json

UTC = timezone.utc

RSS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "The Block": "https://www.theblock.co/rss.xml",
    "Decrypt": "https://decrypt.co/feed",
    "CryptoSlate": "https://cryptoslate.com/feed/",
    "Kraken blog": "https://blog.kraken.com/feed",
}

# Snapshot space id -> Delta ticker (spaces verified to exist on 2026-10-27)
SNAPSHOT_SPACES = {
    "uniswapgovernance.eth": "UNI", "lido-snapshot.eth": "LDO", "arbitrumfoundation.eth": "ARB",
    "opcollective.eth": "OP", "dydxgov.eth": "DYDX", "sushigov.eth": "SUSHI", "cakevote.eth": "CAKE",
    "gitcoindao.eth": "GTC",
}

MAJORS = {"BTC", "ETH"}                       # headlines about these are constant noise
AMBIGUOUS_NAMES = {"Light", "Cross", "Based", "Layer", "Bless", "Space", "Story", "Move", "Time", "Sign", "Town",
                   "Pump", "Bank", "Hive", "Red", "Sonic", "Bill", "Cap", "Born", "Main", "Zen", "Ark"}


# ------------------------------------------------------------------------------ universe
def delta_universe():
    """{ticker: {'symbol','name'}} for crypto perps on Delta (tokenized stocks / metals excluded)."""
    prods = http_json(DELTA_URL + "/v2/products", {"contract_types": "perpetual_futures", "page_size": 500})["result"]
    uni = {}
    for p in prods:
        tags = (p.get("product_specs") or {}).get("tags") or []
        if "xStock" in tags or "metal" in tags:
            continue
        base = p["underlying_asset"]["symbol"]
        tk = re.sub(r"^(1000|1M)", "", base)
        if tk in MAJORS:
            continue
        name = (p["underlying_asset"].get("name") or "").strip()
        uni[tk] = {"symbol": p["symbol"], "name": name}
    return uni


def delta_tickers():
    """{symbol: {'chg24': % , 'close': price, 'turnover': usd}} for all perps in one call."""
    res = http_json(DELTA_URL + "/v2/tickers", {"contract_types": "perpetual_futures"})["result"]
    out = {}
    for t in res:
        try:
            out[t["symbol"]] = {"chg24": float(t.get("mark_change_24h") or 0), "close": float(t.get("close") or 0),
                                "turnover": float(t.get("turnover_usd") or 0)}
        except (TypeError, ValueError):
            continue
    return out


def match_coins(text, universe, max_coins=3):
    """Tickers of Delta coins mentioned in `text` (empty if it looks like a round-up naming many coins)."""
    found = []
    for tk, info in universe.items():
        hit = False
        if len(tk) <= 3:
            hit = re.search(r"(\$%s\b|\(%s\))" % (re.escape(tk), re.escape(tk)), text) is not None
        else:
            hit = re.search(r"(?<![A-Za-z0-9])\$?%s(?![A-Za-z0-9])" % re.escape(tk), text) is not None
        name = info["name"]
        if not hit and len(name) >= 5 and name not in AMBIGUOUS_NAMES and not name[0].isdigit():
            hit = re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(name), text) is not None
        if hit and tk not in found:
            found.append(tk)
    return found if len(found) <= max_coins else []


# ------------------------------------------------------------------------------ RSS
def _clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def fetch_rss(name, url, timeout=15):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        try:
            ts = parsedate_to_datetime(pub).astimezone(UTC).timestamp() if pub else None
        except (TypeError, ValueError):
            ts = None
        if title and ts:
            items.append({"source": name, "title": title, "summary": _clean(it.findtext("description"))[:400],
                          "link": link, "ts": ts})
    return items


def fetch_all_rss(feeds=None):
    items, errors = [], []
    for name, url in (feeds or RSS_FEEDS).items():
        try:
            items += fetch_rss(name, url)
        except Exception as e:                                   # noqa: BLE001
            errors.append("%s: %s" % (name, e))
    return items, errors


# ------------------------------------------------------------------------------ grading
_RUMOR = re.compile(r"\b(rumou?rs?|reportedly|allegedly|unconfirmed|speculat\w+|sources? say|insiders?|influencers?|"
                    r"tweets?|claims?|roadmap|could list|may list|might list|hints?|teases?)\b", re.I)
_A = [
    ("etf", re.compile(r"\bETFs?\b", re.I), re.compile(r"\b(approv\w+|launch\w*|debut\w*|inflows?|go(?:es)? live|begins? trading|green-?light\w*|lists?|listed)\b", re.I)),
    ("regulation", re.compile(r"\b(SEC|CFTC|FCA|MiCA|regulator\w*|licen[sc]e|charter|DTCC|OCC)\b"), re.compile(r"\b(approv\w+|grants?|green-?light\w*|clears?|authoriz\w+|wins?)\b", re.I)),
    ("listing", re.compile(r"\b(Binance|Coinbase|Upbit|Bithumb|Robinhood|OKX|Bybit|Kraken)\b", re.I), re.compile(r"\b(will list|to list|lists?|listing|listed|adds?|added)\b", re.I)),
    ("upgrade", re.compile(r"\b(hard ?fork|mainnet|network upgrade|protocol upgrade|upgrade)\b", re.I), re.compile(r"\b(activat\w+|go(?:es)? live|launch\w*|ships?|complet\w+|goes? into effect)\b", re.I)),
    ("institution", re.compile(r"\b(BlackRock|Fidelity|Grayscale|Bitwise|VanEck|Franklin Templeton|Strategy|Nasdaq-listed|treasury (?:company|firm)|JPMorgan|Goldman|Visa|Mastercard|Stripe)\b"), re.compile(r"\b(buys?|bought|acquires?|purchas\w+|adds?|accumulat\w+|invests?)\b", re.I)),
]
_B = [
    ("partnership", re.compile(r"\b(partner\w*|collaborat\w+|integrat\w+|teams? up)\b", re.I)),
    ("funding", re.compile(r"\b(raises?|raised|funding|backed|grants?|investment)\b", re.I)),
    ("staking", re.compile(r"\b(staking|restak\w+|unstak\w+)\b", re.I)),
    ("tokenomics", re.compile(r"\b(buy-?backs?|burns?|burned|fee switch|revenue share)\b", re.I)),
    ("adoption", re.compile(r"\b(adopt\w+|rolls? out|launch(?:es|ed)?|goes live|unveils?)\b", re.I)),
    ("governance", re.compile(r"\b(vote|voting|proposal)\b", re.I)),
]
_UNLOCK = re.compile(r"\b(unlocks?|vesting)\b", re.I)


def grade(text):
    """-> (tier, category) ; tier in 'A','B','C','R' or (None, None) when it is not a catalyst."""
    if _UNLOCK.search(text):
        return "R", "unlock"
    if _RUMOR.search(text):
        return "C", "rumor/roadmap"
    for cat, subj, act in _A:
        if subj.search(text) and act.search(text):
            return "A", cat
    for cat, rx in _B:
        if rx.search(text):
            return "B", cat
    return None, None


# ------------------------------------------------------------------------------ dates
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DATE1 = re.compile(r"\b%s\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*,?\s*\d{4})" % _MON)
_DATE2 = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+%s\b" % _MON)
_FUTURE_WORDS = re.compile(r"\b(unlock\w*|upgrade|fork|mainnet|launch\w*|vote|voting|deadline|decision|approv\w+|listing|"
                           r"airdrop|TGE|token generation|burn|buy-?back|ETF|migration|go(?:es)? live|activat\w+|"
                           r"expected|scheduled|set to|will|to begin|begins?)\b", re.I)


def future_dates(text, now, horizon_days=21):
    """Calendar dates (UTC midnight) mentioned in `text` that fall in [today, today+horizon]."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for m in list(_DATE1.finditer(text)):
        mon, day = m.group(1), m.group(2)
        out.append((mon, int(day)))
    for m in list(_DATE2.finditer(text)):
        out.append((m.group(2), int(m.group(1))))
    res = []
    for mon, day in out:
        mnum = _MONTHS.get(mon[:3].lower())
        if not mnum or not (1 <= day <= 31):
            continue
        for yr in (today.year, today.year + 1):
            try:
                dt = datetime(yr, mnum, day, tzinfo=UTC)
            except ValueError:
                continue
            if today <= dt <= today + timedelta(days=horizon_days):
                res.append(dt)
                break
    return sorted(set(res))


# ------------------------------------------------------------------------------ catalysts
def fresh_catalysts(items, universe, now_ts, max_age_h=6, include=("A", "B")):
    """RSS items from the last `max_age_h` hours that name a Delta coin and grade into `include`."""
    out = []
    for it in items:
        if now_ts - it["ts"] > max_age_h * 3600 or it["ts"] > now_ts + 600:
            continue
        text = it["title"] + ". " + it["summary"]
        tickers = match_coins(it["title"], universe) or match_coins(text, universe, max_coins=2)
        if not tickers:
            continue
        tier, cat = grade(it["title"] + ". " + it["summary"][:200])
        if tier not in include:
            continue
        out.append({"kind": "fresh", "tier": tier, "cat": cat, "tickers": tickers, "title": it["title"],
                    "link": it["link"], "source": it["source"], "ts": it["ts"]})
    out.sort(key=lambda c: (c["tier"], -c["ts"]))
    return out


def upcoming_from_news(items, universe, now, horizon_days=14, max_age_days=5):
    """News items (last few days) naming a Delta coin AND a future calendar date within the horizon."""
    out = []
    for it in items:
        if now.timestamp() - it["ts"] > max_age_days * 86400:
            continue
        text = it["title"] + ". " + it["summary"]
        tickers = match_coins(it["title"], universe) or match_coins(text, universe, max_coins=2)
        if not tickers:
            continue
        dates = future_dates(text, now, horizon_days)
        if not dates or not _FUTURE_WORDS.search(text):
            continue
        tier, cat = grade(it["title"] + ". " + it["summary"][:200])
        out.append({"kind": "upcoming", "tier": tier or "B", "cat": cat or "event", "tickers": tickers,
                    "title": it["title"], "link": it["link"], "source": it["source"], "ts": it["ts"], "when": dates[0]})
    return out


def snapshot_upcoming(universe, now, horizon_days=14):
    """Pending/active Snapshot votes for mapped DAOs that end (or start) within the horizon."""
    spaces = [s for s, tk in SNAPSHOT_SPACES.items() if tk in universe]
    if not spaces:
        return []
    now_ts = int(now.timestamp())
    q = ('{ proposals(first: 60, where: {space_in: %s, end_gt: %d, start_lt: %d}, orderBy: "end", orderDirection: asc) '
         '{ id title start end state space { id } } }' % (json.dumps(spaces), now_ts, now_ts + horizon_days * 86400))
    r = requests.post("https://hub.snapshot.org/graphql", json={"query": q}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    out = []
    for p in (r.json().get("data") or {}).get("proposals", []):
        tk = SNAPSHOT_SPACES.get(p["space"]["id"])
        if tk not in universe:
            continue
        end = datetime.fromtimestamp(p["end"], UTC)
        out.append({"kind": "upcoming", "tier": "B", "cat": "governance vote", "tickers": [tk],
                    "title": "DAO vote (%s): %s" % (p["state"], _clean(p["title"])[:110]),
                    "link": "https://snapshot.org/#/%s/proposal/%s" % (p["space"]["id"], p["id"]),
                    "source": "Snapshot", "ts": p["start"], "when": end.replace(hour=0, minute=0, second=0)})
    return out


def coinmarketcal_upcoming(universe, now, horizon_days=14):
    """Optional curated calendar (needs COINMARKETCAL_API_KEY).  UNTESTED against the live API."""
    key = os.environ.get("COINMARKETCAL_API_KEY")
    if not key:
        return []
    start = now.strftime("%Y-%m-%d")
    end = (now + timedelta(days=horizon_days)).strftime("%Y-%m-%d")
    r = requests.get("https://developers.coinmarketcal.com/v1/events",
                     params={"max": 150, "dateRangeStart": start, "dateRangeEnd": end},
                     headers={"x-api-key": key, "Accept": "application/json"}, timeout=20)
    r.raise_for_status()
    out = []
    for ev in r.json().get("body", []):
        syms = [c.get("symbol") for c in ev.get("coins", []) if c.get("symbol") in universe]
        if not syms:
            continue
        title = (ev.get("title") or {}).get("en") or ""
        cats = " ".join(c.get("name", "") for c in ev.get("categories", []))
        tier, cat = grade(title + " " + cats)
        try:
            when = datetime.fromisoformat(ev["date_event"].replace("Z", "+00:00")).astimezone(UTC)
        except (KeyError, ValueError):
            continue
        out.append({"kind": "upcoming", "tier": tier or "B", "cat": cat or cats[:20] or "event", "tickers": syms[:3],
                    "title": title[:140], "link": ev.get("source") or ev.get("proof") or "", "source": "CoinMarketCal",
                    "ts": now.timestamp(), "when": when})
    return out
