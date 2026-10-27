"""Exchange listing-announcement feeds + Delta perp matching (public endpoints, no auth).

Used by:
  * run_listing_monitor.py            - live poller / paper-trade logger
  * backtest/upbit_listing_backtest.py - historical event study

Upbit is the only feed that has been backtested against Delta prices.  Bybit parsing is provided
as an optional, UNTESTED second source.
"""
import re
import time
from datetime import datetime, timezone

import requests

UPBIT_URL = "https://api-manager.upbit.com/api/v1/announcements"
BYBIT_URL = "https://api.bybit.com/v5/announcements/index"
DELTA_URL = "https://api.india.delta.exchange"   # public market data (live production, not the demo/testnet)
HEADERS = {"User-Agent": "Mozilla/5.0"}
IGNORE_TICKERS = {"KRW", "BTC", "USDT", "ETH", "USD", "USDC"}

_TICKER_GROUP = re.compile(r"[A-Z0-9]{1,12}(\s*,\s*[A-Z0-9]{1,12})*")


def http_json(url, params=None, tries=3, timeout=15, backoff=1.0):
    """GET -> parsed JSON.  Retries on non-200 / non-JSON bodies (Upbit rate-limits with an empty body)."""
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 200 and r.text[:1] in "{[":
                return r.json()
            last = r.status_code
        except requests.RequestException as e:
            last = e
        time.sleep(backoff * (i + 1))
    raise RuntimeError("request failed (%s): %s %s" % (last, url, params))


# ---------------------------------------------------------------------------- Upbit
def upbit_notices(page=1, per_page=20, tries=3):
    """One page of Upbit 'trade' notices, newest first.  per_page must be <= 20 (API returns 400 above)."""
    d = http_json(UPBIT_URL, {"os": "web", "page": page, "per_page": per_page, "category": "trade"},
                  tries=tries)["data"]
    return d["notices"], d["total_pages"]


def upbit_ts(notice):
    """Epoch seconds of the FIRST posting of a notice (KST offset is in the string)."""
    return int(datetime.fromisoformat(notice["first_listed_at"]).astimezone(timezone.utc).timestamp())


def parse_listing(title):
    """Upbit title -> (kind, [tickers]) or None.   kind: 'new' (brand-new listing) | 'add' (added to a market)."""
    if "종료" in title or "유의" in title:          # delisting / caution notices
        return None
    if "신규 거래지원 안내" in title:
        kind = "new"
    elif "마켓 디지털 자산 추가" in title or "원화 마켓 오픈" in title:
        kind = "add"
    else:
        return None
    tickers = []
    for grp in re.findall(r"\(([^)]*)\)", title):
        grp = grp.strip()
        if _TICKER_GROUP.fullmatch(grp):
            for tk in re.split(r"\s*,\s*", grp):
                if tk not in IGNORE_TICKERS and tk not in tickers:
                    tickers.append(tk)
    return (kind, tickers) if tickers else None


# ---------------------------------------------------------------------------- Bybit (UNTESTED edge)
def bybit_listings(limit=20):
    """Recent Bybit 'new_crypto' announcements -> [{'id','ts','title','url','tickers'}]."""
    res = http_json(BYBIT_URL, {"locale": "en-US", "type": "new_crypto", "limit": limit})["result"]["list"]
    out = []
    for a in res:
        title = a.get("title", "")
        low = title.lower()
        if "tradfi" in low or "token splash" in low or "pre-market" in low:
            continue                                            # stocks / airdrop events / pre-market, not spot-style listings
        if "will list" not in low and "new listing" not in low and "listing of" not in low:
            continue
        tickers = []
        for tk in re.findall(r"\(([A-Z0-9]{2,12})\)", title) + re.findall(r"\b([A-Z0-9]{2,12})USDT\b", title):
            if tk not in IGNORE_TICKERS and tk not in tickers:
                tickers.append(tk)
        if tickers:
            out.append({"id": a.get("url", title), "ts": int(a["publishTime"]) // 1000, "title": title,
                        "url": a.get("url"), "tickers": tickers})
    return out


# ---------------------------------------------------------------------------- Delta
def delta_symbols():
    """{perp symbol: launch_time} for all live Delta perpetuals."""
    d = http_json(DELTA_URL + "/v2/products", {"contract_types": "perpetual_futures", "page_size": 500})["result"]
    return {p["symbol"]: p.get("launch_time") for p in d}


def to_perp(ticker, symbols):
    """'BONK' -> '1000BONKUSD' (Delta prefixes some tickers), or None when there is no perp."""
    for pre in ("", "1000", "1M"):
        s = pre + ticker + "USD"
        if s in symbols:
            return s
    return None


def delta_quote(symbol):
    """Live best bid/ask/mid from the L2 book (the /v2/tickers payload can be stale for thin coins)."""
    d = http_json("%s/v2/l2orderbook/%s" % (DELTA_URL, symbol), {"depth": 1}, tries=2, timeout=8)["result"]
    bid = float(d["buy"][0]["price"]) if d.get("buy") else None
    ask = float(d["sell"][0]["price"]) if d.get("sell") else None
    mid = (bid + ask) / 2 if bid and ask else (bid or ask)
    return {"bid": bid, "ask": ask, "mid": mid}
