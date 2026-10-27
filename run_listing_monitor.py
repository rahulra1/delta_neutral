"""Exchange-listing monitor: alert + paper-trade log for Delta perps.  NO ORDERS ARE PLACED.

Polls Upbit's own announcement feed (the first public source for Upbit listings) every few seconds.
When a listing notice names a ticker that has a Delta perpetual it
  1. prints an alert with the detection latency and the live Delta bid/ask,
  2. appends the event to  data/listing_monitor/events.jsonl,
  3. paper-"shorts" it at the bid and records the Delta book again at +1/+5/+15/+60/+240/+1440 min
     (type=checkpoint lines in the same file), so the backtest result can be verified forward.

Why short: backtest/upbit_listing_backtest.py found Delta perps spike inside the notice minute and then
fade - a short entered ~1 min after the notice won in 27 of 30 events (see that script for caveats).
Bybit (--bybit) is an extra, UNTESTED source.

Usage:
    python run_listing_monitor.py                      # run forever (Ctrl-C to stop)
    python run_listing_monitor.py --replay 3           # dry check: treat the 3 latest listing notices as new, then exit
    python run_listing_monitor.py --interval 6 --bybit   # sustained polling every 4s hit Upbit 429s; the loop also backs off by itself
Files (created on first run): data/listing_monitor/{seen.json, pending.json, events.jsonl}
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from api import listing_feeds as lf
from api import notifier

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "listing_monitor")
DEFAULT_CHECKPOINTS = (1, 5, 15, 60, 240, 1440)


def now_s():
    return time.time()


def fmt(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.seen_path = os.path.join(folder, "seen.json")
        self.pending_path = os.path.join(folder, "pending.json")
        self.events_path = os.path.join(folder, "events.jsonl")

    def load(self, name, default):
        p = os.path.join(self.folder, name)
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
        return default

    def save(self, name, obj):
        p = os.path.join(self.folder, name)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, p)

    def log(self, rec):
        with open(self.events_path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def handle_listing(source, nid, title, notice_ts, kind, tickers, symbols, store, pending, args, replay=False):
    """Match tickers to Delta perps, alert, log and (unless replay) queue paper checkpoints."""
    for tk in tickers:
        sym = lf.to_perp(tk, symbols)
        if not sym:
            print("[%s] %s %-8s %-5s no Delta perp | %s" % (fmt(now_s()), source, tk, kind, title[:70]))
            continue
        try:
            q = lf.delta_quote(sym)
        except Exception as e:                                   # noqa: BLE001
            q = {"bid": None, "ask": None, "mid": None}
            print("   !! quote failed for %s: %s" % (sym, e), file=sys.stderr)
        det = now_s()
        rec = {"type": "event", "source": source, "id": str(nid), "kind": kind, "ticker": tk, "symbol": sym,
               "title": title, "notice_ts": notice_ts, "detected_ts": det, "latency_s": round(det - notice_ts, 1),
               "bid": q["bid"], "ask": q["ask"], "mid": q["mid"], "replay": replay,
               "tested_edge": source == "upbit"}
        store.log(rec)
        print("\n*** LISTING %s %-8s -> %s | kind=%s | notice %s UTC | %s | Delta bid %s ask %s%s"
              % (source.upper(), tk, sym, kind, fmt(notice_ts),
                 "REPLAY (no latency)" if replay else "detected +%.1fs" % (det - notice_ts), q["bid"], q["ask"],
                 "" if source == "upbit" else "  [UNTESTED SOURCE]"))
        print("    %s" % title)
        if getattr(args, "notify", False) and not replay:
            notifier.notify("\U0001F680 LISTING %s: %s -> Delta %s\nkind=%s | notice %s IST | detected +%.0fs | bid %s ask %s%s\n%s\n"
                            "Backtest: post-listing pop usually fades; alert only, no order placed."
                            % (source.upper(), tk, sym, kind,
                               datetime.fromtimestamp(notice_ts, IST).strftime("%d %b %H:%M:%S"), det - notice_ts, q["bid"], q["ask"],
                               "" if source == "upbit" else " [UNTESTED SOURCE]", title[:200]), send=True)
        if replay or not q["bid"] or args.no_paper:
            continue
        for m in args.checkpoints:
            pending.append({"id": str(nid), "symbol": sym, "ticker": tk, "source": source, "due": det + m * 60,
                            "min": m, "entry_bid": q["bid"], "entry_ask": q["ask"], "entry_ts": det})
        store.save("pending.json", pending)


def process_checkpoints(pending, store):
    t = now_s()
    due = [p for p in pending if p["due"] <= t]
    if not due:
        return
    for p in due:
        try:
            q = lf.delta_quote(p["symbol"])
        except Exception as e:                                   # noqa: BLE001
            print("   !! checkpoint quote failed %s: %s" % (p["symbol"], e), file=sys.stderr)
            if t - p["due"] < 600:                               # retry for up to 10 min
                continue
            q = {"bid": None, "ask": None}
        short_ret = None
        if q["ask"] and p["entry_bid"]:
            short_ret = round((p["entry_bid"] - q["ask"]) / p["entry_bid"], 5)   # sell at bid, cover at ask
        rec = {"type": "checkpoint", "source": p["source"], "id": p["id"], "ticker": p["ticker"], "symbol": p["symbol"],
               "min": p["min"], "ts": t, "bid": q["bid"], "ask": q["ask"], "entry_bid": p["entry_bid"],
               "paper_short_ret": short_ret}
        store.log(rec)
        print("    [paper] %-8s +%4dm  short from %s -> ask %s : %s" % (
            p["ticker"], p["min"], p["entry_bid"], q["ask"],
            "n/a" if short_ret is None else "%+.1f%%" % (100 * short_ret)))
        pending.remove(p)
    store.save("pending.json", pending)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=6.0, help="base seconds between Upbit polls (default 6: sustained 4s polling hit HTTP 429, 6s did not; auto-backs off on 429)")
    ap.add_argument("--bybit", action="store_true", help="also watch Bybit listings (UNTESTED edge)")
    ap.add_argument("--bybit-interval", type=float, default=15.0)
    ap.add_argument("--state-dir", default=DEFAULT_DIR)
    ap.add_argument("--replay", type=int, default=0, metavar="N",
                    help="dry check: process the N most recent Upbit listing notices that have a Delta perp, then exit")
    ap.add_argument("--notify", action="store_true", help="also send live listing alerts to Telegram/WhatsApp (see api/notifier.py)")
    ap.add_argument("--no-paper", action="store_true", help="alert/log only, no +N-minute paper checkpoints")
    ap.add_argument("--checkpoints", type=lambda s: tuple(int(x) for x in s.split(",")), default=DEFAULT_CHECKPOINTS,
                    help="minutes after detection to record the book (default 1,5,15,60,240,1440)")
    args = ap.parse_args()

    store = Store(args.state_dir)
    symbols = lf.delta_symbols()
    print("Delta perps loaded: %d | state dir: %s" % (len(symbols), store.folder))

    # ---------------- replay / dry check
    if args.replay:
        notices, _ = lf.upbit_notices(1)
        n_done = 0
        page = 1
        while n_done < args.replay and notices:
            for n in notices:
                p = lf.parse_listing(n["title"])
                if not p or not any(lf.to_perp(tk, symbols) for tk in p[1]):
                    continue
                handle_listing("upbit", n["id"], n["title"], lf.upbit_ts(n), p[0], p[1], symbols, store, [], args, replay=True)
                n_done += 1
                if n_done >= args.replay:
                    break
            page += 1
            if n_done < args.replay and page <= 5:
                notices, _ = lf.upbit_notices(page)
                time.sleep(1)
            else:
                break
        print("\nreplay done (%d events). Nothing was traded." % n_done)
        return

    # ---------------- live loop
    seen = set(store.load("seen.json", []))
    pending = store.load("pending.json", [])
    first_run = not seen
    last_bybit = 0.0
    fail = 0
    streak = 0
    cur_interval = args.interval
    print("Watching Upbit every %gs%s. Ctrl-C to stop. (alerts + paper log only - no orders)"
          % (args.interval, " + Bybit every %gs" % args.bybit_interval if args.bybit else ""))
    try:
        while True:
            t0 = now_s()
            try:
                notices, _ = lf.upbit_notices(1, tries=1)
                fail = 0
                streak += 1
                if streak >= 50 and cur_interval > args.interval:      # recovered: creep back toward the base interval
                    cur_interval = max(args.interval, cur_interval - 1)
                    streak = 0
                if first_run:
                    seen.update(str(n["id"]) for n in notices)
                    store.save("seen.json", sorted(seen))
                    first_run = False
                    print("seeded %d existing notices (no alerts for history)" % len(seen))
                for n in reversed(notices):                       # oldest -> newest
                    nid = str(n["id"])
                    if nid in seen:
                        continue
                    seen.add(nid)
                    p = lf.parse_listing(n["title"])
                    if p:
                        handle_listing("upbit", nid, n["title"], lf.upbit_ts(n), p[0], p[1], symbols, store, pending, args)
                    store.save("seen.json", sorted(seen))
            except Exception as e:                                # noqa: BLE001
                fail += 1
                streak = 0
                cur_interval = min(20.0, cur_interval + 2.0)           # slow down while rate-limited
                print("[%s] upbit poll failed (%d): %s -> poll interval now %gs" % (fmt(now_s()), fail, e, cur_interval), file=sys.stderr)
                time.sleep(min(60, 2 ** min(fail, 6)))

            if args.bybit and now_s() - last_bybit >= args.bybit_interval:
                last_bybit = now_s()
                try:
                    for a in reversed(lf.bybit_listings(20)):
                        key = "bybit:" + str(a["id"])
                        if key in seen:
                            continue
                        seen.add(key)
                        if a["ts"] < now_s() - 3600:
                            continue                              # stale item seen for the first time: mark seen, no alert
                        handle_listing("bybit", key, a["title"], a["ts"], "bybit", a["tickers"], symbols, store, pending, args)
                    store.save("seen.json", sorted(seen))
                except Exception as e:                            # noqa: BLE001
                    print("[%s] bybit poll failed: %s" % (fmt(now_s()), e), file=sys.stderr)

            process_checkpoints(pending, store)
            time.sleep(max(0.0, cur_interval - (now_s() - t0)))
    except KeyboardInterrupt:
        print("\nstopped. %d paper checkpoints still pending (saved to pending.json)." % len(pending))
        store.save("pending.json", pending)


if __name__ == "__main__":
    main()
