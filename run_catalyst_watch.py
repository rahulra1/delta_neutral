"""Catalyst watch: tells you (Telegram / WhatsApp) which Delta coins have news coming or just out.  NO ORDERS.

Every cycle it
  1. reads crypto-news RSS feeds (CoinDesk, Cointelegraph, The Block, Decrypt, CryptoSlate, Kraken blog),
  2. keeps headlines that name a coin with a Delta perp, grades them A strong / B moderate (C = weak/rumor is dropped),
  3. sends NEW A/B headlines as an instant alert,
  4. every --digest-hours sends an "UPCOMING CATALYSTS" list for the next 14 days built from
       - dated items in the news ("upgrade on November 5", "unlock on Nov 2"...),
       - pending/active DAO votes on Snapshot (UNI, LDO, ARB, OP, DYDX, SUSHI, CAKE, GTC),
       - CoinMarketCal events if COINMARKETCAL_API_KEY is set (untested).
Token unlocks are shown as a separate RISK line (supply), not as a positive catalyst.

Important limits: it cannot know about news that has not been announced; it reacts to published headlines and
dated events.  Backtests in this repo (backtest/crypto_news_event_backtest.py, upbit_listing_backtest.py) found that
buying right after strong news usually lost money because the first pop fades - treat alerts as a watch-list.

Usage:
    python run_catalyst_watch.py --once                # preview: print what would be sent (nothing is sent)
    python run_catalyst_watch.py --notify-test         # send one test message to Telegram/WhatsApp
    python run_catalyst_watch.py --notify              # run forever and really send
    python run_catalyst_watch.py --notify --poll 120 --digest-hours 8 --include-c
Credentials: see api/notifier.py (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, or Twilio WhatsApp vars; .env is supported).
State: data/catalyst_watch/seen.json
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from api import catalyst_feeds as cf
from api import notifier

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "catalyst_watch")
TIER_ICON = {"A": "\U0001F7E5 A-STRONG", "B": "\U0001F7E8 B-MODERATE", "C": "\u2B1C C-WEAK", "R": "\u26A0\uFE0F RISK"}
FOOTER = ("\u2139\uFE0F Backtests here: buying right after strong news mostly lost money (the first pop fades). "
          "Use as a watch-list, not a buy signal.")


def load_state(folder):
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, "seen.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {"seen": [], "digest_ts": 0, "digest_sig": "", "first_run": True}


def save_state(folder, st):
    st["seen"] = st["seen"][-3000:]
    p = os.path.join(folder, "seen.json")
    with open(p + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(p + ".tmp", p)


def key_of(c):
    return hashlib.sha1((c.get("link") or c["title"]).encode()).hexdigest()[:16]


def ago(ts, now_ts):
    m = int((now_ts - ts) / 60)
    return "%dm ago" % m if m < 90 else "%dh ago" % (m // 60)


def coin_line(tickers, universe, tick):
    parts = []
    for tk in tickers:
        sym = universe[tk]["symbol"]
        t = tick.get(sym)
        parts.append("%s (Delta %s%s)" % (tk, sym, " %+.1f%% 24h" % t["chg24"] if t else ""))
    return ", ".join(parts)


def fmt_fresh(cands, universe, tick, now_ts):
    lines = ["\U0001F514 NEWS ALERT \u00B7 %s IST" % datetime.fromtimestamp(now_ts, IST).strftime("%d %b %H:%M")]
    for c in cands:
        lines.append("")
        lines.append("%s \u00B7 %s \u00B7 %s" % (TIER_ICON[c["tier"]], c["cat"], coin_line(c["tickers"], universe, tick)))
        lines.append(c["title"][:200])
        lines.append("%s \u00B7 %s \u00B7 %s" % (c["source"], ago(c["ts"], now_ts), c["link"]))
    lines += ["", FOOTER]
    return "\n".join(lines)


def fmt_digest(ups, universe, tick, now, horizon_days):
    lines = ["\U0001F4C5 UPCOMING CATALYSTS \u00B7 next %d days \u00B7 %s IST" % (horizon_days, now.astimezone(IST).strftime("%d %b %H:%M"))]
    pos = [u for u in ups if u["tier"] != "R"]
    risk = [u for u in ups if u["tier"] == "R"]
    by_day = {}
    for u in sorted(pos, key=lambda u: (u["when"], u["tier"])):
        by_day.setdefault(u["when"].date(), []).append(u)
    if not by_day:
        lines.append("\n(no dated catalysts found for Delta coins)")
    for day, lst in by_day.items():
        lines.append("")
        lines.append("\U0001F5D3 %s" % day.strftime("%a %d %b"))
        for u in lst:
            lines.append("  %s \u00B7 %s \u00B7 %s" % (TIER_ICON[u["tier"]].split(" ")[0] + u["tier"], ",".join(u["tickers"]), u["cat"]))
            lines.append("    %s (%s)" % (u["title"][:150], u["source"]))
            if u["link"]:
                lines.append("    " + u["link"])
    if risk:
        lines += ["", "\u26A0\uFE0F SUPPLY RISK (unlocks)"]
        for u in sorted(risk, key=lambda u: u["when"]):
            lines.append("  %s \u00B7 %s \u00B7 %s" % (u["when"].strftime("%d %b"), ",".join(u["tickers"]), u["title"][:130]))
    lines += ["", FOOTER]
    return "\n".join(lines)


def collect_upcoming(items, universe, now, horizon_days):
    ups = cf.upcoming_from_news(items, universe, now, horizon_days)
    for fn in (cf.snapshot_upcoming, cf.coinmarketcal_upcoming):
        try:
            ups += fn(universe, now, horizon_days)
        except Exception as e:                                   # noqa: BLE001
            print("!! %s failed: %s" % (fn.__name__, e), file=sys.stderr)
    seen, out = set(), []
    for u in ups:                                                # de-dupe same coin+date+category
        k = (tuple(u["tickers"]), u["when"].date(), u["cat"], u["title"][:40])
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out


def cycle(args, st, universe, first=False):
    now = datetime.now(UTC)
    now_ts = now.timestamp()
    items, errs = cf.fetch_all_rss()
    for e in errs:
        print("!! rss: %s" % e, file=sys.stderr)
    include = ("A", "B", "C") if args.include_c else ("A", "B")
    fresh = cf.fresh_catalysts(items, universe, now_ts, args.max_age_h, include)
    new = [c for c in fresh if key_of(c) not in st["seen"]]
    for c in fresh:
        st["seen"].append(key_of(c))
    if st["first_run"] and not args.backfill and not args.once:
        new = []                                                  # first live run: seed history silently
    st["first_run"] = False

    sent = 0
    tick = None
    if new:
        tick = tick or _safe_tickers()
        notifier.notify(fmt_fresh(new, universe, tick, now_ts), send=args.notify)
        sent += 1

    due = now_ts - st["digest_ts"] >= args.digest_hours * 3600
    if due or args.once:
        ups = collect_upcoming(items, universe, now, args.horizon_days)
        sig = hashlib.sha1(json.dumps([(u["tickers"], str(u["when"].date()), u["cat"]) for u in ups],
                                      sort_keys=True).encode()).hexdigest()
        if args.once or sig != st["digest_sig"] or now_ts - st["digest_ts"] >= 3 * args.digest_hours * 3600:
            tick = tick or _safe_tickers()
            notifier.notify(fmt_digest(ups, universe, tick, now, args.horizon_days), send=args.notify)
            st["digest_sig"] = sig
            sent += 1
        st["digest_ts"] = now_ts
    if args.once and not new and not fresh:
        print("(no fresh A/B headlines naming a Delta coin in the last %gh)" % args.max_age_h)
    return sent


def _safe_tickers():
    try:
        return cf.delta_tickers()
    except Exception as e:                                       # noqa: BLE001
        print("!! delta tickers failed: %s" % e, file=sys.stderr)
        return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notify", action="store_true", help="really send to Telegram/WhatsApp (default: console dry-run)")
    ap.add_argument("--notify-test", action="store_true", help="send one test message and exit")
    ap.add_argument("--once", action="store_true", help="single scan; show ALL fresh A/B items + the upcoming digest, then exit")
    ap.add_argument("--poll", type=float, default=180.0, help="seconds between RSS scans (default 180)")
    ap.add_argument("--digest-hours", type=float, default=12.0, help="how often to send the upcoming-catalysts list")
    ap.add_argument("--horizon-days", type=int, default=14)
    ap.add_argument("--max-age-h", type=float, default=6.0, help="only headlines newer than this count as 'fresh'")
    ap.add_argument("--include-c", action="store_true", help="also alert weak/rumor (tier C) headlines")
    ap.add_argument("--backfill", action="store_true", help="on first run, alert on headlines already in the window")
    ap.add_argument("--state-dir", default=STATE_DIR)
    args = ap.parse_args()

    if args.notify_test:
        used = notifier.notify("\u2705 Catalyst watch connected. You will get NEWS ALERTs and an UPCOMING CATALYSTS digest here.",
                               send=True)
        print("sent via: %s" % (", ".join(used) or "nothing (no credentials configured, or the send failed - see above)"))
        return

    chans = [n for n, ok in (("telegram", notifier.telegram_configured()), ("whatsapp", notifier.whatsapp_configured())) if ok]
    print("channels configured: %s | mode: %s" % (", ".join(chans) or "none",
                                                 "SENDING" if args.notify and chans else "DRY-RUN (console only)"))
    universe = cf.delta_universe()
    print("Delta coins watched: %d" % len(universe))
    st = load_state(args.state_dir)

    if args.once:
        cycle(args, st, universe)
        return
    try:
        while True:
            t0 = time.time()
            try:
                cycle(args, st, universe)
                save_state(args.state_dir, st)
            except Exception as e:                               # noqa: BLE001
                print("[%s] cycle failed: %s" % (datetime.now(UTC).strftime("%H:%M:%S"), e), file=sys.stderr)
            time.sleep(max(5.0, args.poll - (time.time() - t0)))
    except KeyboardInterrupt:
        save_state(args.state_dir, st)
        print("\nstopped.")


if __name__ == "__main__":
    main()
