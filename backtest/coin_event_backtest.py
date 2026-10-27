#!/usr/bin/env python3
"""Event-study backtest: how do Delta perps move around SCHEDULED CoinMarketCal events?  (stdlib + requests)

Events : every past, day-precise event on CoinMarketCal for coins with a Delta perp (api/coinmarketcal.py).
Prices : Delta India daily candles (00:00 UTC open).  T = event day.  All returns are open-to-open:
           pre5  : open(T-5) -> open(T)      "buy the rumour"   (long 5 days before, exit at event-day open)
           pre2  : open(T-2) -> open(T)
           day   : open(T)   -> open(T+1)    event day itself
           post3 : open(T+1) -> open(T+4)    "sell the news" window (a fade = negative here)
           post7 : open(T+1) -> open(T+8)
         ADJ = coin return minus BTCUSD return over the same window (strips the market move).
Known-before filter (--known-days K): CoinMarketCal's sitemap <lastmod> is the LAST edit time, so lastmod <= T-K
         proves the event was public at least K days before.  Events edited later are dropped (conservative; it removes
         many genuine events too, but never keeps one that was not yet known).  Without it the pre-event windows have
         look-ahead (the event may have been added after the run-up).
Costs  : --cost-bp round trip (default 20 bp = 2 x (5 bp fee + 5 bp slippage)) subtracted in the strategy section.
Caveats: ~5 months of events (Jun-Oct 2026, what CoinMarketCal currently shows), only coins listed on Delta today
         (survivorship), funding not modelled, one sample per coin/day per group.

Usage:
    python backtest/coin_event_backtest.py
    python backtest/coin_event_backtest.py --known-days 5 --min-impact 6 --detail
"""
import argparse
import json
import math
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api import catalyst_feeds as cf          # noqa: E402
from api import coinmarketcal as cm           # noqa: E402

UTC = timezone.utc
BASE_URL = "https://api.india.delta.exchange"
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".crypto_cache", "daily")
WINDOWS = {"pre5": (-5, 0), "pre2": (-2, 0), "day": (0, 1), "post3": (1, 4), "post7": (1, 8)}
CAT_GROUPS = {
    "Exchange": ("Exchange",), "Release/Upgrade": ("Release", "Fork/Swap"), "Tokenomics": ("Tokenomics",),
    "Partnership/Integration": ("Partnership", "Integration"), "Staking/Airdrop": ("Staking/Farming", "Airdrop/Snapshot"),
    "Regulation": ("Regulation",), "AMA/Other": ("AMA/Team Update", "Other"),
}


def cat_group(cat):
    first = (cat or "").split(",")[0].strip()
    for g, members in CAT_GROUPS.items():
        if first in members:
            return g
    return "AMA/Other"


def daily_opens(symbol, start, end):
    """{date: open} for a Delta perp (cached per symbol for 6h)."""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, symbol + ".json")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 6 * 3600:
        with open(p) as f:
            raw = json.load(f)
    else:
        for attempt in range(4):
            r = requests.get(BASE_URL + "/v2/history/candles", timeout=30,
                             params={"resolution": "1d", "symbol": symbol, "start": int(start), "end": int(end)})
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            break
        raw = [[c["time"], c["open"], c["close"]] for c in r.json().get("result", [])]
        with open(p, "w") as f:
            json.dump(raw, f)
    return {datetime.fromtimestamp(t, UTC).date(): float(o) for t, o, _c in raw if o}


def window_ret(opens, d0, a, b):
    x, y = opens.get(d0 + timedelta(days=a)), opens.get(d0 + timedelta(days=b))
    if not x or not y:
        return None
    return y / x - 1.0


def summarize(vals):
    n = len(vals)
    if n < 2:
        return n, (vals[0] if vals else 0.0), 0.0, 0.0, 0.0
    m = statistics.mean(vals)
    sd = statistics.stdev(vals)
    t = m / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return n, m, statistics.median(vals), sum(v > 0 for v in vals) / n, t


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--known-days", type=int, default=0, help="require sitemap lastmod <= T-K (0 = no filter)")
    ap.add_argument("--min-impact", type=float, default=0.0)
    ap.add_argument("--cost-bp", type=float, default=20.0)
    ap.add_argument("--min-turnover", type=float, default=0.0, help="drop coins with 24h Delta turnover below this USD")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--json-out", default="", help="write per-group stats JSON (used by the live strategy)")
    args = ap.parse_args()

    today = datetime.now(UTC).date()
    universe = cf.delta_universe()
    events = [e for e in cm.delta_events(universe, log=lambda *_: None)
              if e["precision"] == "day" and date.fromisoformat(e["date"]) <= today - timedelta(days=9)]
    if args.min_impact:
        events = [e for e in events if (e["impact"] or 0) >= args.min_impact]
    if args.min_turnover:
        tick = cf.delta_tickers()
        events = [e for e in events if tick.get(universe[e["ticker"]]["symbol"], {}).get("turnover", 0) >= args.min_turnover]
    if args.known_days:
        lm = cm.event_lastmod()
        keep = []
        for e in events:
            ts = lm.get(e["slug"])
            if ts and datetime.fromtimestamp(ts, UTC).date() <= date.fromisoformat(e["date"]) - timedelta(days=args.known_days):
                keep.append(e)
        print("known-before filter (lastmod <= T-%d): %d of %d events kept" % (args.known_days, len(keep), len(events)))
        events = keep
    if not events:
        print("no events")
        return

    first = min(date.fromisoformat(e["date"]) for e in events) - timedelta(days=10)
    start = datetime(first.year, first.month, first.day, tzinfo=UTC).timestamp()
    end = time.time()
    btc = daily_opens("BTCUSD", start, end)
    prices = {}
    for tk in sorted({e["ticker"] for e in events}):
        try:
            prices[tk] = daily_opens(universe[tk]["symbol"], start, end)
        except Exception as ex:                                            # noqa: BLE001
            print("!! candles %s: %s" % (tk, ex), file=sys.stderr)

    rows, seen = [], set()
    for e in events:
        d0 = date.fromisoformat(e["date"])
        op = prices.get(e["ticker"])
        if not op:
            continue
        g = cat_group(e["category"])
        k = (e["ticker"], d0, g)
        if k in seen:
            continue
        seen.add(k)
        r = {"ticker": e["ticker"], "date": d0, "group": g, "impact": e["impact"] or 0, "title": e["title"]}
        for w, (a, b) in WINDOWS.items():
            cr, br = window_ret(op, d0, a, b), window_ret(btc, d0, a, b)
            r[w] = None if cr is None or br is None else cr - br
        rows.append(r)
    print("samples: %d (coin/day/category, %s .. %s), coins: %d"
          % (len(rows), min(r["date"] for r in rows), max(r["date"] for r in rows), len({r["ticker"] for r in rows})))

    def table(title, filt):
        print("\n== %s  (BTC-adjusted returns, mean / median / win%% / t)" % title)
        print("%-26s" % "group" + "".join("%26s" % w for w in WINDOWS))
        groups = [("ALL", lambda r: True)] + [(g, (lambda g: lambda r: r["group"] == g)(g)) for g in CAT_GROUPS] + [
            ("impact >= 7", lambda r: r["impact"] >= 7), ("impact 5-6.9", lambda r: 5 <= r["impact"] < 7),
            ("impact < 5", lambda r: r["impact"] < 5)]
        out = {}
        for name, f in groups:
            sel = [r for r in rows if filt(r) and f(r)]
            line, out[name] = "%-26s" % ("%s (n=%d)" % (name, len(sel))), {}
            for w in WINDOWS:
                n, m, md, win, t = summarize([r[w] for r in sel if r[w] is not None])
                out[name][w] = {"n": n, "mean": m, "median": md, "win": win, "t": t}
                line += "%26s" % ("%+.2f%% %+.2f%% %3.0f%% %+.1f" % (100 * m, 100 * md, 100 * win, t) if n >= 5 else "n<5")
            print(line)
        return out

    stats = table("ALL EVENTS", lambda r: True)
    mid = sorted(r["date"] for r in rows)[len(rows) // 2]
    table("FIRST HALF (< %s)" % mid, lambda r: r["date"] < mid)
    table("SECOND HALF (>= %s)" % mid, lambda r: r["date"] >= mid)

    cost = args.cost_bp / 1e4
    print("\n== candidate rules, NET of %.0f bp round trip (each trade = one BTC-hedged leg; mean per trade)" % args.cost_bp)
    rules = [
        ("LONG pre5, impact>=7", lambda r: r["impact"] >= 7, "pre5", 1),
        ("LONG pre5, impact>=5", lambda r: r["impact"] >= 5, "pre5", 1),
        ("LONG pre2, impact>=7", lambda r: r["impact"] >= 7, "pre2", 1),
        ("LONG pre5, Release/Upgrade imp>=5", lambda r: r["group"] == "Release/Upgrade" and r["impact"] >= 5, "pre5", 1),
        ("LONG pre5, Exchange", lambda r: r["group"] == "Exchange", "pre5", 1),
        ("SHORT post3, impact>=7", lambda r: r["impact"] >= 7, "post3", -1),
        ("SHORT post3, Exchange", lambda r: r["group"] == "Exchange", "post3", -1),
        ("SHORT post7, impact>=5", lambda r: r["impact"] >= 5, "post7", -1),
        ("SHORT post3, Release/Upgrade imp>=5", lambda r: r["group"] == "Release/Upgrade" and r["impact"] >= 5, "post3", -1),
        ("SHORT post3, Tokenomics", lambda r: r["group"] == "Tokenomics", "post3", -1),
    ]
    rule_out = {}
    for name, f, w, side in rules:
        v = [side * r[w] - cost for r in rows if f(r) and r[w] is not None]
        n, m, md, win, t = summarize(v)
        h1 = [side * r[w] - cost for r in rows if f(r) and r[w] is not None and r["date"] < mid]
        h2 = [side * r[w] - cost for r in rows if f(r) and r[w] is not None and r["date"] >= mid]
        rule_out[name] = {"n": n, "mean": m, "median": md, "win": win, "t": t,
                          "h1": statistics.mean(h1) if h1 else 0, "h2": statistics.mean(h2) if h2 else 0}
        print("%-38s n=%4d  mean %+.2f%%  median %+.2f%%  win %3.0f%%  t %+.1f  | halves %+.2f%% / %+.2f%%"
              % (name, n, 100 * m, 100 * md, 100 * win, t, 100 * rule_out[name]["h1"], 100 * rule_out[name]["h2"]))

    if args.detail:
        print("\n== detail (impact >= 7)")
        for r in sorted(rows, key=lambda r: r["date"]):
            if r["impact"] >= 7:
                print("%s %-8s %-24s imp %.1f  pre5 %s  day %s  post3 %s  %s" % (
                    r["date"], r["ticker"], r["group"], r["impact"],
                    *("%+6.1f%%" % (100 * r[w]) if r[w] is not None else "   n/a" for w in ("pre5", "day", "post3")),
                    r["title"][:50]))
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"generated": datetime.now(UTC).isoformat(), "known_days": args.known_days, "n": len(rows),
                       "groups": stats, "rules": rule_out}, f, indent=1)
        print("wrote", args.json_out)


if __name__ == "__main__":
    main()
