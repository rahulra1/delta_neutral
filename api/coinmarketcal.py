"""CoinMarketCal public-site client (no API key): dated crypto events for Delta-listed coins.

Data comes from the public pages that robots.txt allows (checked 2026-10-27):
  * /sitemaps/base        -> every coin page slug  (/coin/<slug>, ~5.3k coins)
  * /sitemaps/events/1    -> every event url + <lastmod> (used as a "known-by" upper bound in backtests)
  * /coin/<slug>          -> that coin's upcoming AND past events: date, category, impact score (0-10), title
                             plus a JSON-LD ItemList with exact ISO dates for upcoming events
  * /event/<slug>         -> one event's description (JSON-LD), fetched only when needed

Requests are throttled (default 1.2 s apart) and cached on disk.  This is HTML scraping: if CoinMarketCal changes
its markup the parser returns nothing (it does not crash) and the caller should say so.
"""
import html
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

UTC = timezone.utc
BASE = "https://coinmarketcal.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".crypto_cache", "coinmarketcal")
MIN_GAP_S = 1.2
OVERRIDES = {"POL": "polygon-ecosystem-token"}                 # verified by hand where auto-matching picks a namesake

_MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_last_req = [0.0]


def _get(url, timeout=25, retries=3):
    for attempt in range(retries):
        wait = MIN_GAP_S - (time.time() - _last_req[0])
        if wait > 0:
            time.sleep(wait)
        _last_req[0] = time.time()
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(10 * (attempt + 1))
            continue
        return r
    r.raise_for_status()
    return r


def _cache_path(name):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def _cached_json(name, max_age_s, builder):
    p = _cache_path(name)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_s:
        with open(p) as f:
            return json.load(f)
    data = builder()
    with open(p + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(p + ".tmp", p)
    return data


# ---------------------------------------------------------------------------------------------- sitemaps
def coin_slugs(max_age_s=7 * 86400):
    def build():
        txt = _get(BASE + "/sitemaps/base", timeout=40).text
        return sorted({u.rsplit("/", 1)[1] for u in re.findall(r"<loc>(https://coinmarketcal\.com/coin/[^<]+)</loc>", txt)})
    return _cached_json("coin_slugs.json", max_age_s, build)


def event_lastmod(max_age_s=12 * 3600):
    """{event_slug: lastmod_epoch} for every event in the public sitemap."""
    def build():
        txt = _get(BASE + "/sitemaps/events/1", timeout=60).text
        out = {}
        for loc, lm in re.findall(r"<loc>https://coinmarketcal\.com/event/([^<]+)</loc><lastmod>([^<]+)</lastmod>", txt):
            try:
                out[loc] = datetime.fromisoformat(lm.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        return out
    return _cached_json("event_lastmod.json", max_age_s, build)


# ---------------------------------------------------------------------------------------------- coin mapping
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def coingecko_ids_by_symbol(max_age_s=7 * 86400):
    """{symbol_lower: [(id, name), ...]} from CoinGecko's free /coins/list (CoinMarketCal slugs follow CG ids)."""
    def build():
        r = requests.get("https://api.coingecko.com/api/v3/coins/list", headers=HEADERS, timeout=40)
        r.raise_for_status()
        out = {}
        for c in r.json():
            out.setdefault((c.get("symbol") or "").lower(), []).append([c["id"], c.get("name") or ""])
        return out
    try:
        return _cached_json("cg_by_symbol.json", max_age_s, build)
    except Exception:                                                   # noqa: BLE001 - optional helper
        return {}


def _candidates(ticker, name, slugs, cg=None):
    n = _norm(name)
    t = ticker.lower()
    first = _norm((name or "").split(" ")[0])
    hy = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    cands = [n, hy, n + "coin", n + "token", n + "network", n + "protocol", n + "finance", n + "ai",
             hy + "-network", hy + "-protocol", hy + "-token", hy + "-2",
             re.sub(r"(coin|token|network|protocol)$", "", n), t, t + "coin", t + "token", first, first + "network",
             first + "protocol", first + "coin", first + "token"]
    # CoinGecko ids that carry this ticker, most name-similar first
    by_sym = (cg or {}).get(t, [])
    by_sym = sorted(by_sym, key=lambda x: (0 if _norm(x[1]) == n else 1 if n and (n in _norm(x[1]) or _norm(x[1]) in n) else 2,
                                           len(x[0])))
    cands += [cid for cid, _ in by_sym[:6]]
    seen, out = set(), []
    for c in cands:
        if c and c not in seen and c in slugs:
            seen.add(c)
            out.append(c)
    return out[:6]


def parse_coin_page(text):
    """-> {'name','ticker','events':[...]} or None.  Events: dict(slug,title,category,impact,label,date,precision)."""
    body = text[:text.find("self.__next_f")] if "self.__next_f" in text else text
    m = re.search(r'"@type":\s*"WebPage",\s*"name":\s*"([^"]+?) \(([A-Za-z0-9]+)\) events"', body)
    if not m:
        return None
    exact = {}                                                        # upcoming events have exact ISO dates in JSON-LD
    for blk in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', body, re.S):
        try:
            d = json.loads(blk)
        except ValueError:
            continue
        if d.get("@type") == "ItemList":
            for it in d.get("itemListElement", []):
                item = it.get("item") or {}
                url, tc = item.get("url", ""), item.get("temporalCoverage", "")
                if url and tc:
                    exact[url.rsplit("/", 1)[1]] = tc
    events, seen = [], set()
    for attrs, inner in re.findall(r'<a\b([^>]*href="/event/[^"]+"[^>]*)>(.*?)</a>', body, re.S):
        slug = re.search(r'href="/event/([^"]+)"', attrs).group(1)
        parts = [html.unescape(p.strip()) for p in re.split(r"<[^>]+>", inner) if p.strip()]
        if slug in seen or not parts or parts[0].startswith("Top catalyst"):
            continue
        seen.add(slug)
        try:
            impact = float(parts[-1])
        except ValueError:
            impact = None
        # card layout: [label, date, TICKER(s)..., category, title, (High), score]
        ev = {"slug": slug, "label": parts[0], "date_txt": parts[1] if len(parts) > 1 else "", "impact": impact,
              "title": "", "category": "", "iso": exact.get(slug)}
        core = [p for p in parts[2:-1] if p not in ("High", "Medium", "Low", "\u2197")]
        if len(core) >= 2:
            ev["title"], ev["category"] = core[-1], core[-2]
            ev["coins"] = core[:-2]
        events.append(ev)
    return {"name": m.group(1), "ticker": m.group(2).upper(), "events": events}


def resolve_date(ev, today):
    """Turn a card into (date, precision) with precision 'day' | 'month' | None.  `today` is a date (UTC)."""
    iso = ev.get("iso")
    if iso:
        try:
            if len(iso) == 10:
                return date.fromisoformat(iso), "day"
            if len(iso) == 7:
                return date.fromisoformat(iso + "-01"), "month"
        except ValueError:
            pass
    txt = ev.get("date_txt", "").replace("\u2192", "").strip()
    label = ev.get("label", "").upper()
    m = re.match(r"^(\d{1,2}) ([A-Za-z]{3})$", txt)                    # "20 Oct" (no year)
    if m:
        d, mon = int(m.group(1)), _MON.get(m.group(2).lower())
        if not mon:
            return None, None
        past = "AGO" in label or label in ("PAST", "ONGOING")
        future = label.startswith("IN ") or label == "TODAY"
        best = None
        for yr in (today.year - 1, today.year, today.year + 1):
            try:
                c = date(yr, mon, d)
            except ValueError:
                continue
            if past and c > today:
                continue
            if future and c < today:
                continue
            if best is None or abs((c - today).days) < abs((best - today).days):
                best = c
        return best, ("day" if best else None)
    m = re.match(r"^([A-Za-z]{3}) (\d{4})$", txt)                      # "Aug 2026"
    if m and _MON.get(m.group(1).lower()):
        return date(int(m.group(2)), _MON[m.group(1).lower()], 1), "month"
    return None, None


def coin_page(slug, max_age_s=6 * 3600):
    """Parsed coin page (cached).  None if the page is missing or unparseable."""
    p = _cache_path("coin_" + slug + ".html")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_s:
        with open(p, encoding="utf-8") as f:
            return parse_coin_page(f.read())
    r = _get(BASE + "/coin/" + slug)
    if r.status_code != 200:
        return None
    with open(p, "w", encoding="utf-8") as f:
        f.write(r.text)
    return parse_coin_page(r.text)


def map_delta_coins(universe, log=print):
    """{ticker: slug} for Delta coins, verified by the ticker shown on the CoinMarketCal page.  Cached 7 days."""
    p = _cache_path("delta_map.json")
    cache = {}
    if os.path.exists(p):
        with open(p) as f:
            cache = json.load(f)
    fresh = time.time() - cache.get("_ts", 0) < 7 * 86400
    slugs = set(coin_slugs())
    cg = coingecko_ids_by_symbol()
    out = {}
    for tk, info in sorted(universe.items()):
        if fresh and tk in cache.get("map", {}):
            out[tk] = cache["map"][tk]
            continue
        if fresh and tk in cache.get("miss", []):
            continue
        best = None
        for cand in ([OVERRIDES[tk]] if tk in OVERRIDES else _candidates(tk, info.get("name", ""), slugs, cg)):
            page = coin_page(cand)
            if not page or page["ticker"] != tk:
                continue
            a, b = _norm(info.get("name", "")), _norm(page["name"])
            if a and b and (a in b or b in a or a[:4] == b[:4]):
                best = cand                                            # ticker AND name agree -> take it
                break
            best = best or cand                                         # ticker-only match: keep looking
        if best:
            out[tk] = best
    miss = sorted(set(universe) - set(out))
    with open(p, "w") as f:
        json.dump({"_ts": time.time() if not fresh else cache["_ts"], "map": out, "miss": miss}, f)
    log("CoinMarketCal: mapped %d/%d Delta coins" % (len(out), len(universe)))
    return out


def delta_events(universe, today=None, log=print, page_max_age_s=6 * 3600):
    """All CoinMarketCal events (past + upcoming) for Delta coins as flat dicts with resolved dates."""
    today = today or datetime.now(UTC).date()
    out = []
    for tk, slug in map_delta_coins(universe, log).items():
        page = coin_page(slug, page_max_age_s)
        if not page:
            continue
        for ev in page["events"]:
            d, prec = resolve_date(ev, today)
            if not d:
                continue
            out.append(dict(ev, ticker=tk, coin_slug=slug, date=d.isoformat(), precision=prec,
                            url=BASE + "/event/" + ev["slug"]))
    return out


def event_description(slug, max_age_s=7 * 86400):
    """Short description text from an event page (cached)."""
    def build():
        r = _get(BASE + "/event/" + slug)
        m = re.search(r'"@type":\s*"WebPage".*?"description":\s*"((?:[^"\\]|\\.)*)"', r.text, re.S)
        return {"d": json.loads('"%s"' % m.group(1)) if m else ""}
    try:
        return _cached_json("ev_" + slug + ".json", max_age_s, build)["d"]
    except Exception:                                                   # noqa: BLE001
        return ""
