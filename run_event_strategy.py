"""Scheduled-Event Strategy for Delta perps  -  calendar + "sell-the-news" paper trades, delivered on Telegram/WhatsApp.

WHAT IT DOES (PAPER ONLY - no orders are ever placed)
  1. Calendar: pulls every dated event (upgrades, listings, tokenomics, partnerships, votes ...) for the ~196 coins with a
     Delta perp from CoinMarketCal's public coin pages (api/coinmarketcal.py), each with an impact score 0-10.
  2. Daily digest (default 02:30 UTC = 08:00 IST): coin, exact date, event, category, impact, link - next 14 days.
     Events that qualify for the trade rule are starred with their planned entry/exit dates.
  3. New-event alert: a qualifying event that appears on the calendar after the last digest is pushed immediately.
  4. Trade rule (from backtest/coin_event_backtest.py):
        event with impact >= 7 on day T  ->  at 00:00 UTC (05:30 IST) of T+1: SHORT the coin perp + LONG BTCUSD (equal notional)
                                         ->  at 00:00 UTC (05:30 IST) of T+4: close both legs.  All Telegram times are IST.
     Backtest, Jun-Oct 2026, 66 events, after 20 bp costs: median +2.0% per trade, mean +2.5%, 70% winners.
     Weak points: mean is only +0.55% without the 5 best trades, single trades lost up to -15%, t-stat ~2 with 10 rules
     tried, only ~4 months of events.  Treat as a hypothesis to paper-trade, not a proven edge.
     Entering ON the event day (T+0) was much worse (+0.2%) - the drop usually starts the day after.
  5. Entry/exit alerts with paper PnL; paper book in data/event_strategy/.

Usage
    python run_event_strategy.py --once                 # build calendar, print digest + today's actions (nothing sent)
    python run_event_strategy.py --once --notify        # same, but send the digest to Telegram now
    python run_event_strategy.py --notify               # run forever (checks every 10 min)
Options: --min-impact 7 --hold-days 3 --notional 100 --digest-hour 2.5 --horizon-days 14 --list-min-impact 4
Credentials: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (or Twilio vars) in .env - see api/notifier.py.
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

from api import catalyst_feeds as cf
from api import coinmarketcal as cm
from api import notifier

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "event_strategy")
BACKTEST_LINE = ("Backtest Jun-Oct 2026 (66 events): median +2.0%/trade, 70% win, worst -15%. "
                 "Unproven - paper only, no orders placed.")


# ----------------------------------------------------------------------------------------------------- state
def load_state(folder):
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, "state.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {"open": [], "closed": [], "known_q": [], "entered": [], "last_digest": "", "seeded": False}


def save_state(folder, st):
    st["known_q"] = st["known_q"][-2000:]
    st["entered"] = st["entered"][-2000:]
    p = os.path.join(folder, "state.json")
    with open(p + ".tmp", "w") as f:
        json.dump(st, f, indent=1)
    os.replace(p + ".tmp", p)


def log_trade(folder, rec):
    with open(os.path.join(folder, "trades.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


# ----------------------------------------------------------------------------------------------------- helpers
def d_(s):
    return date.fromisoformat(s)


def fmt_d(d):
    return d.strftime("%a %d %b")


def ist_at_utc_midnight(d):
    """'Wed 28 Oct 05:30 IST' for 00:00 UTC of date d (the strategy's entry/exit time)."""
    return datetime(d.year, d.month, d.day, tzinfo=UTC).astimezone(IST).strftime("%a %d %b %H:%M IST")


def now_ist(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), IST).strftime("%d %b %H:%M IST")


def qualifies(e, args):
    return e["precision"] == "day" and (e.get("impact") or 0) >= args.min_impact


def plan_dates(e, args):
    t = d_(e["date"])
    return t + timedelta(days=1), t + timedelta(days=1 + args.hold_days)


def prices():
    try:
        return cf.delta_tickers()
    except Exception as ex:                                                    # noqa: BLE001
        print("!! delta tickers failed: %s" % ex, file=sys.stderr)
        return {}


def short_title(e):
    return (e.get("title") or "")[:70]


# ----------------------------------------------------------------------------------------------------- messages
def fmt_digest(events, universe, today, args, tick):
    horizon = today + timedelta(days=args.horizon_days)
    day_ev = [e for e in events if e["precision"] == "day" and today <= d_(e["date"]) <= horizon
              and (e.get("impact") or 0) >= args.list_min_impact]
    month_ev = [e for e in events if e["precision"] == "month" and today.replace(day=1) <= d_(e["date"]) <= horizon
                and (e.get("impact") or 0) >= max(args.list_min_impact, 5)]
    n_q = sum(qualifies(e, args) for e in day_ev)
    lines = ["\U0001F4C5 COIN EVENT CALENDAR \u00B7 %s \u2192 %s \u00B7 sent %s" % (
                 today.strftime("%d %b"), horizon.strftime("%d %b %Y"), now_ist()),
             "%d dated events on Delta coins (impact \u2265 %g) \u00B7 \u2B50 %d trade setups (impact \u2265 %g)"
             % (len(day_ev), args.list_min_impact, n_q, args.min_impact)]
    by_day = {}
    for e in sorted(day_ev, key=lambda e: (e["date"], -(e.get("impact") or 0), e["ticker"])):
        by_day.setdefault(e["date"], []).append(e)
    for ds, lst in by_day.items():
        lines += ["", "\U0001F5D3 %s" % fmt_d(d_(ds))]
        for e in lst:
            sym = universe[e["ticker"]]["symbol"]
            t = tick.get(sym)
            star = "\u2B50 " if qualifies(e, args) else "\u2022 "
            lines.append("%s%s \u00B7 %s \u00B7 impact %.1f \u00B7 %s%s" % (
                star, e["ticker"], short_title(e), e.get("impact") or 0, e.get("category") or "?",
                " \u00B7 %+.1f%% 24h" % t["chg24"] if t else ""))
            if qualifies(e, args):
                en, ex = plan_dates(e, args)
                lines.append("   \u25B6 plan: SHORT %s (+ long BTC) %s \u2192 close %s"
                             % (sym, ist_at_utc_midnight(en), ist_at_utc_midnight(ex)))
            lines.append("   " + e["url"])
    if not by_day:
        lines += ["", "(no dated events in the window)"]
    if month_ev:
        lines += ["", "\U0001F4CC Month only (exact day not announced)"]
        for e in sorted(month_ev, key=lambda e: (e["date"], -(e.get("impact") or 0))):
            lines.append("\u2022 %s \u00B7 %s \u00B7 %s \u00B7 impact %.1f" % (
                d_(e["date"]).strftime("%b %Y"), e["ticker"], short_title(e), e.get("impact") or 0))
    lines += ["", "\u2139\uFE0F " + BACKTEST_LINE,
              "Times in IST. Event dates are the calendar day CoinMarketCal lists (exact hour usually not given).",
              "Source: coinmarketcal.com (dates can move - check the link)."]
    return "\n".join(lines)


def fmt_new_events(new, universe, args):
    lines = ["\U0001F195 NEW HIGH-IMPACT EVENT%s ON CALENDAR" % ("S" if len(new) > 1 else "")]
    for e in new:
        en, ex = plan_dates(e, args)
        lines += ["", "\u2B50 %s \u00B7 %s \u00B7 %s \u00B7 impact %.1f \u00B7 %s" % (
            e["ticker"], fmt_d(d_(e["date"])), short_title(e), e.get("impact") or 0, e.get("category") or "?"),
            "   \u25B6 plan: SHORT %s (+ long BTC) %s \u2192 close %s" % (universe[e["ticker"]]["symbol"],
                                                                     ist_at_utc_midnight(en), ist_at_utc_midnight(ex)),
            "   " + e["url"]]
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------------- trading
def do_entries(st, events, universe, now, args, send):
    """Open paper positions for qualifying events whose T+1 is today (within the entry grace window)."""
    today = now.date()
    if now.hour + now.minute / 60.0 > args.entry_grace_h:
        return
    due = [e for e in events if qualifies(e, args) and d_(e["date"]) + timedelta(days=1) == today
           and e["slug"] not in st["entered"]]
    if not due:
        return
    tick = prices()
    btc = (tick.get("BTCUSD") or {}).get("close")
    msgs = []
    for e in sorted(due, key=lambda e: -(e.get("impact") or 0)):
        st["entered"].append(e["slug"])
        sym = universe[e["ticker"]]["symbol"]
        px = (tick.get(sym) or {}).get("close")
        if any(p["symbol"] == sym for p in st["open"]):
            msgs.append("\u23ED %s already short from an earlier event - skipped (%s)" % (sym, short_title(e)))
            continue
        if not px or not btc:
            msgs.append("\u26A0\uFE0F %s: no live price - entry skipped" % sym)
            continue
        _, exit_d = plan_dates(e, args)
        pos = {"symbol": sym, "ticker": e["ticker"], "event": short_title(e), "event_date": e["date"], "slug": e["slug"],
               "impact": e.get("impact"), "entry_ts": now.timestamp(), "entry_px": px, "btc_entry": btc,
               "exit_date": exit_d.isoformat(), "notional": args.notional, "url": e["url"]}
        st["open"].append(pos)
        log_trade(args.state_dir, dict(pos, action="open"))
        msgs.append("\U0001F53B PAPER SHORT %s @ %g  (+ long BTC @ %g, $%g each leg) \u00B7 %s\n"
                    "   event %s %s \u00B7 impact %.1f\n"
                    "   exit planned %s\n   %s" % (sym, px, btc, args.notional, now_ist(now.timestamp()),
                                                 fmt_d(d_(e["date"])), short_title(e), e.get("impact") or 0,
                                                 ist_at_utc_midnight(exit_d), e["url"]))
    notifier.notify("\U0001F4E5 EVENT STRATEGY \u00B7 ENTRIES %s\n\n" % now_ist(now.timestamp()) + "\n\n".join(msgs) +
                    "\n\n" + BACKTEST_LINE, send=send)


def do_exits(st, now, args, send):
    today = now.date()
    due = [p for p in st["open"] if d_(p["exit_date"]) <= today]
    if not due:
        return
    tick = prices()
    btc = (tick.get("BTCUSD") or {}).get("close")
    msgs, cost = [], args.cost_bp / 1e4
    for p in due:
        px = (tick.get(p["symbol"]) or {}).get("close")
        if not px or not btc:
            msgs.append("\u26A0\uFE0F %s: no live price - exit retried next cycle" % p["symbol"])
            continue
        coin_r = px / p["entry_px"] - 1
        btc_r = btc / p["btc_entry"] - 1
        pnl_pct = -coin_r + btc_r - cost                         # short coin + long BTC, both legs' costs
        pnl_usd = pnl_pct * p["notional"]
        st["open"].remove(p)
        rec = dict(p, action="close", exit_ts=now.timestamp(), exit_px=px, btc_exit=btc, coin_ret=coin_r,
                   btc_ret=btc_r, pnl_pct=pnl_pct, pnl_usd=pnl_usd)
        st["closed"].append(rec)
        log_trade(args.state_dir, rec)
        msgs.append("%s CLOSE %s @ %g  coin %+.2f%% \u00B7 BTC %+.2f%%  \u2192 PnL %+.2f%% ($%+.2f)\n   %s"
                    % ("\u2705" if pnl_pct > 0 else "\u274C", p["symbol"], px, 100 * coin_r, 100 * btc_r,
                       100 * pnl_pct, pnl_usd, p["event"]))
    closed = st["closed"]
    tot = sum(c["pnl_usd"] for c in closed)
    wins = sum(c["pnl_pct"] > 0 for c in closed)
    notifier.notify("\U0001F4E4 EVENT STRATEGY \u00B7 EXITS %s\n\n" % now_ist(now.timestamp()) + "\n\n".join(msgs) +
                    "\n\nPaper book: %d closed, %d wins, total $%+.2f \u00B7 %d open" % (len(closed), wins, tot,
                                                                                      len(st["open"])), send=send)


# ----------------------------------------------------------------------------------------------------- main loop
def cycle(st, universe, args, cache, force_digest=False):
    now = datetime.now(UTC)
    today = now.date()
    if time.time() - cache.get("ts", 0) > args.refresh_h * 3600 or not cache.get("events"):
        t0 = time.time()
        cache["events"] = cm.delta_events(universe, today=today, log=lambda *a: None,
                                          page_max_age_s=int(args.refresh_h * 3600))
        cache["ts"] = time.time()
        print("[%s] calendar refreshed: %d events (%.0fs)" % (now.strftime("%H:%M"), len(cache["events"]), time.time() - t0))
        if not cache["events"]:
            notifier.notify("\u26A0\uFE0F Event strategy: CoinMarketCal returned no events (site changed or blocked?).",
                            send=args.notify)
    events = cache["events"]

    do_exits(st, now, args, args.notify)
    do_entries(st, events, universe, now, args, args.notify)

    upcoming_q = [e for e in events if qualifies(e, args) and d_(e["date"]) >= today]
    new = [e for e in upcoming_q if e["slug"] not in st["known_q"]]
    st["known_q"] += [e["slug"] for e in new]

    hour = now.hour + now.minute / 60.0
    digest_due = st["last_digest"] != today.isoformat() and hour >= args.digest_hour
    if force_digest or digest_due or not st["seeded"]:
        notifier.notify(fmt_digest(events, universe, today, args, prices()), send=args.notify)
        st["last_digest"] = today.isoformat()
    elif new:
        notifier.notify(fmt_new_events(new, universe, args), send=args.notify)
    st["seeded"] = True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notify", action="store_true", help="really send to Telegram/WhatsApp (default: console only)")
    ap.add_argument("--once", action="store_true", help="one cycle, always prints/sends the digest, then exit")
    ap.add_argument("--min-impact", type=float, default=7.0, help="impact score that triggers a trade setup")
    ap.add_argument("--hold-days", type=int, default=3)
    ap.add_argument("--notional", type=float, default=100.0, help="paper USD per leg")
    ap.add_argument("--cost-bp", type=float, default=40.0, help="round-trip cost for BOTH legs (default 40 bp)")
    ap.add_argument("--entry-grace-h", type=float, default=6.0, help="open entries only until this UTC hour on T+1")
    ap.add_argument("--digest-hour", type=float, default=2.5, help="UTC hour for the daily calendar (2.5 = 08:00 IST)")
    ap.add_argument("--horizon-days", type=int, default=14)
    ap.add_argument("--list-min-impact", type=float, default=4.0, help="hide calendar items below this impact")
    ap.add_argument("--refresh-h", type=float, default=6.0, help="hours between full calendar refreshes")
    ap.add_argument("--poll-min", type=float, default=10.0)
    ap.add_argument("--state-dir", default=STATE_DIR)
    args = ap.parse_args()

    chans = [n for n, ok in (("telegram", notifier.telegram_configured()),
                             ("whatsapp", notifier.whatsapp_configured())) if ok]
    print("channels: %s | mode: %s | PAPER ONLY" % (", ".join(chans) or "none",
                                                   "SENDING" if args.notify and chans else "console"))
    universe = cf.delta_universe()
    st = load_state(args.state_dir)
    cache = {}
    if args.once:
        cycle(st, universe, args, cache, force_digest=True)
        save_state(args.state_dir, st)
        return
    try:
        while True:
            t0 = time.time()
            try:
                cycle(st, universe, args, cache)
                save_state(args.state_dir, st)
            except Exception as ex:                                            # noqa: BLE001
                print("[%s] cycle failed: %s" % (datetime.now(UTC).strftime("%H:%M:%S"), ex), file=sys.stderr)
            time.sleep(max(30.0, args.poll_min * 60 - (time.time() - t0)))
    except KeyboardInterrupt:
        save_state(args.state_dir, st)
        print("\nstopped.")


if __name__ == "__main__":
    main()
