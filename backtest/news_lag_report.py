#!/usr/bin/env python3
"""How late is the first public report?  Uses the EVENTS from crypto_news_event_backtest.py.

For every TIMED event with Delta 15m candles (read from the backtest's cache) it measures,
at the moment of the first report found:
    runup%   = price now vs the lowest low of the prior 24h   (how much was already priced in)
    next24h% = best high in the next 24h vs the entry open    (how much was still left)
Run the backtest first so candles are cached:  python backtest/crypto_news_event_backtest.py
"""
import os
import statistics
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crypto_news_event_backtest as bt  # noqa: E402

UTC = timezone.utc


def main():
    import time
    rows = []
    now = int(time.time())
    need = {}
    for sym, when, known, *_ in bt.EVENTS:
        a = int((when - bt.timedelta(days=2)).timestamp())
        b = min(now, int((when + bt.timedelta(days=33)).timestamp()))
        lo, hi = need.get(sym, (a, b))
        need[sym] = (min(lo, a), max(hi, b))
    data = {s: bt.fetch_15m(s, a, b) for s, (a, b) in need.items()}
    for sym, when, known, cat, head in bt.EVENTS:
        if not known:
            continue
        cs = data.get(sym)
        if not cs:
            continue
        t = int(when.timestamp())
        idx = bt.find_entry_idx(cs, t)
        if idx is None or idx < 96 or cs[0][0] > t - 86400:
            continue
        window = cs[idx - 96:idx]                       # prior 24h
        trough = min(window, key=lambda c: c[3])
        entry = cs[idx][1]
        runup = 100 * (entry / trough[3] - 1)
        nxt = cs[idx:idx + 96]
        best = 100 * (max(c[2] for c in nxt) / entry - 1)
        worst = 100 * (min(c[3] for c in nxt) / entry - 1)
        rows.append((sym.replace("USD", ""), cat, datetime.fromtimestamp(t, UTC).strftime("%m-%d %H:%M"),
                     runup, 0.0, best, worst, head))
    print("%-7s %-9s %-12s %9s %10s %10s  %s" % ("coin", "cat", "report(UTC)", "runup24h", "best+24h", "worst+24h", "headline"))
    for r in rows:
        print("%-7s %-9s %-12s %+8.0f%% %+9.0f%% %+9.0f%%  %s" % (r[0], r[1], r[2], r[3], r[5], r[6], r[7][:60]))
    if rows:
        print("\nn=%d timed events | median run-up already in at first report: %+.1f%% | "
              "median best move left in next 24h: %+.1f%% | median worst: %+.1f%%" % (
                  len(rows), statistics.median(r[3] for r in rows),
                  statistics.median(r[5] for r in rows), statistics.median(r[6] for r in rows)))
        big = [r for r in rows if r[3] >= 15]
        small = [r for r in rows if r[3] < 15]
        for name, g in (("run-up >=15% (late report)", big), ("run-up <15% (early report)", small)):
            if g:
                print("  %-28s n=%-2d median best+24h %+6.1f%%  median worst+24h %+6.1f%%" % (
                    name, len(g), statistics.median(r[5] for r in g), statistics.median(r[6] for r in g)))


if __name__ == "__main__":
    main()
