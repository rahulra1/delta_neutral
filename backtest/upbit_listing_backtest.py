#!/usr/bin/env python3
"""Upbit listing-notice backtest on Delta perps (stdlib + requests only).

Source of truth for the news time is Upbit's own announcement API (second-level timestamps),
not media articles:
    https://api-manager.upbit.com/api/v1/announcements?os=web&page=N&per_page=20&category=trade

Events: every notice that adds a coin to an Upbit market ("신규 거래지원 안내" = brand-new listing,
"... 마켓 디지털 자산 추가" = coin added to another market, e.g. KRW) whose ticker has a Delta
perpetual that already existed at the notice time.

For each event we use 1-minute Delta candles and report
  1) EVENT STUDY  - return from the last pre-notice close to +1m/+5m/+30m/+1h/+4h/+1d/+3d
  2) TRADE GRID   - long (and mirrored short) with TP/SL on price (default +/-50%) and a time stop,
                    for entry delays of 0/1/2/5/15/60 minutes after the notice.

Entry delay semantics (1m bars):
    0 min  : open of the candle that CONTAINS the notice  (optimistic - may pre-date the notice by <60s)
    N>=1   : open of the first candle whose open time >= notice + N minutes
Intrabar: if one bar touches both TP and SL the SL is assumed first.  Costs: --fee-bp + --slip-bp
per side.  Funding is not modelled.  Only coins currently listed on Delta (survivorship bias).

Usage:
    python backtest/upbit_listing_backtest.py
    python backtest/upbit_listing_backtest.py --detail --refresh
    python backtest/upbit_listing_backtest.py --since 2026-01-01 --tp-pct 50 --sl-pct 50
"""
import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.listing_feeds import delta_symbols, parse_listing, to_perp  # noqa: E402

UPBIT_URL = "https://api-manager.upbit.com/api/v1/announcements"
DELTA = "https://api.india.delta.exchange"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".crypto_cache")
UTC = timezone.utc
BAR = 60
MAX_BARS_PER_REQ = 3500
HEADERS = {"User-Agent": "Mozilla/5.0"}


# ------------------------------------------------------------------------------ http
def _get(url, params, headers=None, tries=8):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 200 and r.text[:1] in "{[":
                return r.json()
            last = r.status_code
        except requests.RequestException as e:                    # noqa: PERF203
            last = e
        time.sleep(1.5 * (i + 1))
    raise RuntimeError("request failed (%s): %s %s" % (last, url, params))


# ---------------------------------------------------------------------------- upbit
def fetch_notices(refresh=False):
    path = os.path.join(CACHE_DIR, "upbit_trade_notices.json")
    os.makedirs(CACHE_DIR, exist_ok=True)
    if os.path.exists(path) and not refresh:
        with open(path) as f:
            return json.load(f)
    out, page = [], 1
    while True:
        d = _get(UPBIT_URL, {"os": "web", "page": page, "per_page": 20, "category": "trade"}, HEADERS)["data"]
        out += d["notices"]
        if page >= d["total_pages"]:
            break
        page += 1
        time.sleep(0.8)
    with open(path, "w") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


# ----------------------------------------------------------------------------- delta
def fetch_1m(symbol, start_ts, end_ts):
    os.makedirs(CACHE_DIR, exist_ok=True)
    start_ts = int(start_ts // 3600 * 3600)
    end_ts = int(end_ts // 3600 * 3600 + 3600)
    path = os.path.join(CACHE_DIR, "%s_1m_%d_%d.json" % (symbol, start_ts, end_ts))
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    by_time, cur_end = {}, end_ts
    while cur_end > start_ts:
        cur_start = max(start_ts, cur_end - MAX_BARS_PER_REQ * BAR)
        res = _get(DELTA + "/v2/history/candles",
                   {"resolution": "1m", "symbol": symbol, "start": cur_start, "end": cur_end}).get("result", [])
        for c in res:
            by_time[int(c["time"])] = (int(c["time"]), float(c["open"]), float(c["high"]),
                                       float(c["low"]), float(c["close"]))
        cur_end = cur_start
        time.sleep(0.2)
    candles = [by_time[k] for k in sorted(by_time)]
    now = int(time.time())
    candles = [c for c in candles if c[0] + BAR <= now]           # drop still-forming bar
    with open(path, "w") as f:
        json.dump(candles, f)
    return candles


def idx_at_or_after(candles, ts):
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid][0] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(candles) else None


def idx_containing(candles, ts):
    i = idx_at_or_after(candles, ts + 1)
    i = (len(candles) if i is None else i) - 1
    return i if i >= 0 and candles[i][0] <= ts < candles[i][0] + BAR else None


# -------------------------------------------------------------------------- simulate
def simulate(candles, ei, side, tp_pct, sl_pct, hold_min, fee_bp, slip_bp):
    slip, fee = slip_bp / 1e4, fee_bp / 1e4
    o = candles[ei][1]
    entry = o * (1 + slip) if side == "long" else o * (1 - slip)
    if side == "long":
        tp_px, sl_px = entry * (1 + tp_pct / 100), entry * (1 - sl_pct / 100)
    else:
        tp_px, sl_px = entry * (1 - tp_pct / 100), entry * (1 + sl_pct / 100)
    end_ts = candles[ei][0] + hold_min * 60
    reason = None
    last = ei
    for i in range(ei, len(candles)):
        t, _, h, l, c = candles[i]
        last = i
        hit_sl = (l <= sl_px) if side == "long" else (h >= sl_px)
        hit_tp = (h >= tp_px) if side == "long" else (l <= tp_px)
        if hit_sl:
            reason, px = "SL", sl_px
            break
        if hit_tp:
            reason, px = "TP", tp_px
            break
        if t + BAR >= end_ts:
            reason, px = "TIME", c
            break
    if reason is None:
        reason, px = "OPEN", candles[last][4]
    if reason in ("TIME", "OPEN"):
        px = px * (1 - slip) if side == "long" else px * (1 + slip)
    gross = (px / entry - 1) if side == "long" else (entry - px) / entry
    return {"reason": reason, "ret": gross - 2 * fee}


def stats(rets, reasons, notional):
    n = len(rets)
    if not n:
        return None
    w = [r for r in rets if r > 0]
    l = [r for r in rets if r <= 0]
    gp, gl = sum(w), -sum(l)
    return {"n": n, "tp": reasons.count("TP"), "sl": reasons.count("SL"), "open": reasons.count("OPEN"),
            "win": 100 * len(w) / n, "avg": 100 * sum(rets) / n, "med": 100 * statistics.median(rets),
            "pf": gp / gl if gl > 0 else float("inf"), "pnl": notional * sum(rets)}


def cell(s):
    if not s:
        return "-"
    pf = "inf" if s["pf"] == float("inf") else "%.2f" % s["pf"]
    return "%d/%+.1f%%/%s/%.0f%%" % (s["n"], s["avg"], pf, s["win"])


# ------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2025-06-01", help="earliest notice date (UTC, YYYY-MM-DD)")
    ap.add_argument("--tp-pct", type=float, default=50.0)
    ap.add_argument("--sl-pct", type=float, default=50.0)
    ap.add_argument("--fee-bp", type=float, default=5.0)
    ap.add_argument("--slip-bp", type=float, default=3.0)
    ap.add_argument("--notional", type=float, default=1000.0)
    ap.add_argument("--kind", choices=["all", "new", "add"], default="all")
    ap.add_argument("--refresh", action="store_true", help="re-download the Upbit notice list")
    ap.add_argument("--detail", action="store_true", help="per-event table")
    args = ap.parse_args()

    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    notices = fetch_notices(args.refresh)
    symbols = delta_symbols()

    events, seen = [], set()
    for n in notices:
        p = parse_listing(n["title"])
        if not p:
            continue
        t = datetime.fromisoformat(n["first_listed_at"]).astimezone(UTC)
        if t < since:
            continue
        kind, tickers = p
        if args.kind != "all" and kind != args.kind:
            continue
        for tk in tickers:
            sym = to_perp(tk, symbols)
            if not sym or (sym, int(t.timestamp()) // 3600) in seen:
                continue
            seen.add((sym, int(t.timestamp()) // 3600))
            events.append({"sym": sym, "tk": tk, "t": int(t.timestamp()), "kind": kind, "title": n["title"]})
    events.sort(key=lambda e: e["t"])
    print("Upbit notices: %d | listing notices with a Delta perp since %s: %d" % (len(notices), args.since, len(events)),
          file=sys.stderr)

    # ---- candles
    now = int(time.time())
    usable = []
    for e in events:
        lo = e["t"] - 3600
        hi = min(now, e["t"] + 8 * 86400)
        try:
            cs = fetch_1m(e["sym"], lo, hi)
        except Exception as ex:                                  # noqa: BLE001
            print("  !! %s fetch failed: %s" % (e["sym"], ex), file=sys.stderr)
            continue
        if not cs or cs[0][0] > e["t"] - 600:                    # perp did not exist yet / no pre-notice bar
            continue
        i0 = idx_containing(cs, e["t"])
        if i0 is None or i0 < 1:
            continue
        e["cs"] = cs
        e["i0"] = i0
        e["p0"] = cs[i0 - 1][4]                                  # last close BEFORE the notice minute
        usable.append(e)
    print("usable events (perp existed, candles present): %d" % len(usable), file=sys.stderr)
    if not usable:
        print("no usable events")
        return

    print("\nUpbit-listing backtest | TP=+%.0f%% SL=-%.0f%% | fee %gbp slip %gbp per side | $%g/trade | since %s | kind=%s"
          % (args.tp_pct, args.sl_pct, args.fee_bp, args.slip_bp, args.notional, args.since, args.kind))
    print("events: %d (%s)" % (len(usable), ", ".join("%s %s" % (e["tk"], datetime.fromtimestamp(e["t"], UTC).strftime("%y-%m-%d"))
                                                      for e in usable)))

    # ---- 1) event study
    horizons = [("+1m", 1), ("+5m", 5), ("+30m", 30), ("+1h", 60), ("+4h", 240), ("+1d", 1440), ("+3d", 4320)]
    print("\n1) EVENT STUDY - return vs last close BEFORE the notice (close of horizon bar, no costs)")
    print("   %-8s %6s %9s %9s %7s %9s %9s" % ("horizon", "n", "mean", "median", "%pos", "p25", "p75"))
    for name, m in horizons:
        vals = []
        for e in usable:
            j = e["i0"] + m
            if j < len(e["cs"]):
                vals.append(e["cs"][j][4] / e["p0"] - 1)
        if len(vals) >= 3:
            q = statistics.quantiles(vals, n=4) if len(vals) >= 4 else [min(vals), 0, max(vals)]
            print("   %-8s %6d %+8.1f%% %+8.1f%% %6.0f%% %+8.1f%% %+8.1f%%" % (
                name, len(vals), 100 * statistics.mean(vals), 100 * statistics.median(vals),
                100 * sum(1 for v in vals if v > 0) / len(vals), 100 * q[0], 100 * q[2]))
    # peak within first hour / day
    for name, m in (("peak<=1h", 60), ("peak<=1d", 1440)):
        vals = []
        for e in usable:
            seg = e["cs"][e["i0"]:e["i0"] + m]
            if len(seg) >= min(m, 30):
                vals.append(max(c[2] for c in seg) / e["p0"] - 1)
        if vals:
            print("   %-8s %6d %+8.1f%% %+8.1f%%   (best high vs pre-notice close)" % (
                name, len(vals), 100 * statistics.mean(vals), 100 * statistics.median(vals)))

    # ---- 2) trade grid
    delays = [0, 1, 2, 5, 15, 60]
    holds = [("15m", 15), ("1h", 60), ("4h", 240), ("1d", 1440), ("3d", 4320), ("7d", 10080)]
    for side in ("long", "short"):
        print("\n2) TRADE GRID %s - cell = n / avg ret / PF / win%%   (TP %.0f%% / SL %.0f%% on price)"
              % (side.upper(), args.tp_pct, args.sl_pct))
        print("   %-8s" % "delay" + "".join("%-26s" % h for h, _ in holds))
        for d in delays:
            row = "   %-8s" % ("%dm" % d)
            for _, hm in holds:
                rets, reasons = [], []
                for e in usable:
                    cs = e["cs"]
                    ei = e["i0"] if d == 0 else idx_at_or_after(cs, e["t"] + d * 60)
                    if ei is None or ei >= len(cs) - 1:
                        continue
                    r = simulate(cs, ei, side, args.tp_pct, args.sl_pct, hm, args.fee_bp, args.slip_bp)
                    rets.append(r["ret"])
                    reasons.append(r["reason"])
                row += "%-26s" % cell(stats(rets, reasons, args.notional))
            print(row)

    # ---- 3) split by kind for base config
    print("\n3) BASE CONFIG (delay 1m, 1d hold) by listing kind and side")
    for kind in ("new", "add"):
        for side in ("long", "short"):
            rets, reasons = [], []
            for e in usable:
                if e["kind"] != kind:
                    continue
                ei = idx_at_or_after(e["cs"], e["t"] + 60)
                if ei is None or ei >= len(e["cs"]) - 1:
                    continue
                r = simulate(e["cs"], ei, side, args.tp_pct, args.sl_pct, 1440, args.fee_bp, args.slip_bp)
                rets.append(r["ret"])
                reasons.append(r["reason"])
            s = stats(rets, reasons, args.notional)
            lab = "new listing" if kind == "new" else "market add (e.g. KRW)"
            if s:
                pf = "inf" if s["pf"] == float("inf") else "%.2f" % s["pf"]
                print("   %-22s %-5s n=%-3d TP=%-2d SL=%-2d OPEN=%-2d win=%3.0f%% avg=%+6.1f%% med=%+6.1f%% PF=%-5s pnl=$%+8.0f"
                      % (lab, side, s["n"], s["tp"], s["sl"], s["open"], s["win"], s["avg"], s["med"], pf, s["pnl"]))

    # ---- 3b) robustness of the base SHORT (delay 1m, 1d hold)
    def base_trades(side, hold=1440, dedupe=False):
        out, seen_t = [], set()
        for e in usable:
            if dedupe:
                if e["t"] in seen_t:
                    continue
                seen_t.add(e["t"])
            ei = idx_at_or_after(e["cs"], e["t"] + 60)
            if ei is None or ei >= len(e["cs"]) - 1:
                continue
            r = simulate(e["cs"], ei, side, args.tp_pct, args.sl_pct, hold, args.fee_bp, args.slip_bp)
            seg = e["cs"][ei:ei + hold]
            entry_px = e["cs"][ei][1]
            r.update(e=e, mae_short=100 * (max(c[2] for c in seg) / entry_px - 1),
                     mae_long=100 * (1 - min(c[3] for c in seg) / entry_px))
            out.append(r)
        return out

    print("\n3b) ROBUSTNESS (delay 1m, 1d hold; costs as set above)")
    for side in ("short", "long"):
        for dd in (False, True):
            tr = base_trades(side, 1440, dd)
            if len(tr) < 4:
                continue
            rets = [t["ret"] for t in tr]
            sd = statistics.pstdev(rets) or 1e-9
            tstat = statistics.mean(rets) / (sd / len(rets) ** 0.5)
            half = len(tr) // 2
            a, b = rets[:half], rets[half:]
            mae = sorted(t["mae_short" if side == "short" else "mae_long"] for t in tr)
            print("   %-5s %-26s n=%-2d avg=%+6.1f%% t=%+5.2f | 1st half %+6.1f%% 2nd half %+6.1f%% | "
                  "max adverse move in 1d: median %.0f%%, p90 %.0f%%, worst %.0f%%" % (
                      side, "one coin per notice" if dd else "all events", len(tr), 100 * statistics.mean(rets), tstat,
                      100 * statistics.mean(a), 100 * statistics.mean(b),
                      statistics.median(mae), mae[int(0.9 * (len(mae) - 1))], mae[-1]))

    # ---- 4) per-event detail
    if args.detail:
        print("\n4) PER EVENT (long, delay 1m, 1d hold)  pop = notice-minute high vs pre-close; +1m/+1h/+1d = close vs pre-close")
        print("   %-9s %-4s %-16s %7s %7s %7s %7s  %-5s %8s  %s" % (
            "coin", "kind", "notice(UTC)", "pop", "+1m", "+1h", "+1d", "exit", "ret", "title"))
        for e in usable:
            cs, i0, p0 = e["cs"], e["i0"], e["p0"]

            def at(m):
                return "%+6.0f%%" % (100 * (cs[i0 + m][4] / p0 - 1)) if i0 + m < len(cs) else "   n/a "
            pop = 100 * (cs[i0][2] / p0 - 1)
            ei = idx_at_or_after(cs, e["t"] + 60)
            if ei is None or ei >= len(cs) - 1:
                continue
            r = simulate(cs, ei, "long", args.tp_pct, args.sl_pct, 1440, args.fee_bp, args.slip_bp)
            print("   %-9s %-4s %-16s %+6.0f%% %s %s %s  %-5s %+7.1f%%  %s" % (
                e["tk"], e["kind"], datetime.fromtimestamp(e["t"], UTC).strftime("%y-%m-%d %H:%M"),
                pop, at(1), at(60), at(1440), r["reason"], 100 * r["ret"], e["title"][:50]))


if __name__ == "__main__":
    main()
