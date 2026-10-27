#!/usr/bin/env python3
"""News-event backtest on Delta Exchange perps (stdlib + requests only).

For each curated positive-news event (buyback / burn, AI-agent narrative, exchange
listing, protocol upgrade ...) on a coin that has a Delta perp, enter at the first
15m candle OPEN at/after (news time + delay) and exit on whichever comes first:

    * take profit  : price moves +TP% in the trade's favour   (default 50%)
    * stop loss    : price moves -SL% against the trade       (default 50%)
    * time stop    : --hold-days after entry (close of that candle)

Intrabar rule: if one 15m candle touches both TP and SL, SL is assumed to hit first.
Date-only events (time unknown) are entered at the NEXT UTC day's 00:00 open, so there
is no look-ahead.  Event times for timed events are the FIRST public report found, so
entries are already lagged behind the real announcement.

TP/SL are measured on PRICE (unleveraged).  Funding is not modelled.  Costs: --fee-bp
and --slip-bp per side.  Only coins currently listed on Delta (survivorship bias).

Usage:
    python backtest/crypto_news_event_backtest.py                # base run + grids
    python backtest/crypto_news_event_backtest.py --detail       # per-trade table
    python backtest/crypto_news_event_backtest.py --tp-pct 50 --sl-pct 50 --hold-days 7
"""
import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE_URL = "https://api.india.delta.exchange"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".crypto_cache")
BAR = 15 * 60  # seconds
MAX_BARS_PER_REQ = 3500

UTC = timezone.utc


def ms(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000, UTC)


def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


# (symbol, when, time_known, category, headline)
# `when`: datetime (timed) or date-only datetime at 00:00 UTC (time_known=False)
EVENTS = [
    # --- buyback / burn / treasury ---------------------------------------------------
    ("AINUSD", iso("2026-09-07T00:00:00Z"), False, "rebrand", "Infinity Ground: 'new phase of brand development', AIN ticker to be replaced"),
    ("AINUSD", iso("2026-10-05T00:00:00Z"), False, "buyback", "Infinity Ground: new funding closed, token remediation buyback, deposits to reopen"),
    ("AINUSD", ms(1793077765000), True, "buyback", "AIN +124% day report; ~$3M of AIN bought by buyback address"),
    ("HYPEUSD", ms(1791200140000), True, "buyback", "Hyperliquid announces $15M HYPE buyback tranche"),
    ("BEATUSD", ms(1781193860000), True, "burn", "Audiera BEAT +1,454%: weekly revenue ~100% burned"),
    ("BEATUSD", ms(1789190811000), True, "burn", "Audiera BEAT rebounds 29%: weekly buyback/burn update"),
    ("LABUSD", ms(1780398060000), True, "buyback", "LAB ATH: LABtrade multi-exchange buyback program"),
    ("ORDERUSD", ms(1792821746000), True, "buyback", "Orderly ORDER +12%: fee buyback proposal debate"),
    # --- AI-agent narrative ----------------------------------------------------------
    ("TIAUSD", ms(1792174445000), True, "ai_agent", "Celestia launches Eden, EVM chain for AI agents"),
    ("NEARUSD", ms(1791026658000), True, "ai_agent", "Grayscale AI report; NEAR 'currency of agents' rally"),
    ("WLDUSD", ms(1792581300000), True, "ai_product", "Worldcoin announces new financial-services product"),
    # --- listing / protocol upgrade / product ----------------------------------------
    ("BMTUSD", iso("2026-10-24T03:00:00Z"), True, "listing", "Bubblemaps BMT: Upbit KRW listing (12:00 KST)"),
    ("STRKUSD", ms(1791506596000), True, "upgrade", "Starknet considering L1 / quantum-resistant (first KuCoin report)"),
    ("ZECUSD", ms(1792591355000), True, "upgrade", "Zcash NU7 upgrade activation proposal confirmed"),
    ("AAVEUSD", ms(1790949763000), True, "product", "Aave V4 tokenized-stock collateral launch"),

    # ===== batch 2 (more sources: KuCoin, Invezz, Unchained, CCN, Cointelegraph, Decrypt,
    #       BeInCrypto, 99bitcoins, Phemex, CoinDCX, tronweekly, Blockonomi ...) ==========
    # --- buyback / burn / fee switch ---
    ("PUMPUSD", ms(1777447705000), True, "buyback", "Pump.fun burns $370M PUMP + 50% revenue buyback plan"),
    ("UNIUSD", ms(1785492962000), True, "buyback", "Uniswap activates fee switch, daily UNI burns start"),
    ("ENAUSD", ms(1787871439000), True, "buyback", "Ethena Foundation buys out seed investors + 95% revenue ENA buyback proposal"),
    ("AAVEUSD", ms(1782472238000), True, "buyback", "Aave founder confirms automated buyback mechanism in development"),
    ("AAVEUSD", ms(1790735626000), True, "buyback", "Aave founder announces token burn for Aavenomics 3.0 (+11%)"),
    # --- exchange listings ---
    ("LDOUSD", ms(1781839942000), True, "listing", "Upbit lists LDO (and 8 other assets) on 19 Jun"),
    ("RSRUSD", iso("2026-10-12T00:00:00Z"), False, "listing", "Upbit lists Reserve Rights (RSR)"),
    ("ZECUSD", ms(1777019537000), True, "listing", "Robinhood listing for Zcash"),
    # --- ETF / regulatory ---
    ("ZECUSD", ms(1787740336000), True, "etf_reg", "Grayscale spot Zcash ETF goes live"),
    ("NEARUSD", ms(1790714164000), True, "etf_reg", "Bitwise launches first US spot NEAR ETF (NRR)"),
    ("SUIUSD", ms(1771503356000), True, "etf_reg", "Canary + Grayscale launch first spot SUI ETFs"),
    ("SOLUSD", ms(1790262941000), True, "etf_reg", "SEC cuts altcoin ETF approvals to 75 days (SOL/XRP/DOGE)"),
    ("XRPUSD", ms(1790262941000), True, "etf_reg", "SEC cuts altcoin ETF approvals to 75 days (SOL/XRP/DOGE)"),
    ("DOGEUSD", ms(1790262941000), True, "etf_reg", "SEC cuts altcoin ETF approvals to 75 days (SOL/XRP/DOGE)"),
    # --- partnerships / institutions ---
    ("HBARUSD", iso("2026-09-23T00:00:00Z"), False, "partner", "Hedera IDTrust listed on IBM Cloud Catalog"),
    ("XLMUSD", ms(1779924260000), True, "partner", "DTCC to bring tokenized securities to Stellar"),
    # --- rebrand / governance ---
    ("GRAMUSD", ms(1777975663000), True, "rebrand", "Telegram replaces TON Foundation as network driver"),
    ("GRAMUSD", ms(1780359805000), True, "rebrand", "Telegram renames TON token to Gram"),
]


# Catalyst-strength grading (user's framework):
#   A strong  : ETF approval/inflows, major listing, institutional purchase, regulatory approval, major protocol upgrade
#   B moderate: partnership, ecosystem funding, staking/tokenomics changes (buybacks/burns), adoption announcement
#   C weak    : influencer posts, generic roadmap/rebrand updates, exchange rumors, unverified claims, proposals still being debated
# Judgment calls: buybacks/burns/fee-fundamentals -> B (tokenomics), except Uniswap fee-switch ACTIVATION -> A (live protocol change);
# Starknet 'considering L1', TON rename, AIN ticker rebrand -> C (roadmap/brand); ORDER buyback 'proposal debate' -> C.
TIER_RULES = [
    ("AINUSD", "brand development", "C"), ("AINUSD", "new funding closed", "B"), ("AINUSD", "AIN +124%", "B"),
    ("HYPEUSD", "$15M HYPE buyback", "B"), ("BEATUSD", "BEAT", "B"), ("LABUSD", "LAB ATH", "B"),
    ("ORDERUSD", "proposal debate", "C"), ("TIAUSD", "Eden", "B"), ("NEARUSD", "Grayscale AI report", "C"),
    ("WLDUSD", "financial-services", "B"), ("BMTUSD", "Upbit", "A"), ("STRKUSD", "considering L1", "C"),
    ("ZECUSD", "NU7", "A"), ("AAVEUSD", "V4", "A"), ("PUMPUSD", "$370M", "B"), ("UNIUSD", "fee switch", "A"),
    ("ENAUSD", "buys out seed", "B"), ("AAVEUSD", "automated buyback", "B"), ("AAVEUSD", "Aavenomics", "B"),
    ("LDOUSD", "Upbit", "A"), ("RSRUSD", "Upbit", "A"), ("ZECUSD", "Robinhood", "A"),
    ("ZECUSD", "ETF", "A"), ("NEARUSD", "ETF", "A"), ("SUIUSD", "ETF", "A"),
    ("SOLUSD", "SEC cuts", "A"), ("XRPUSD", "SEC cuts", "A"), ("DOGEUSD", "SEC cuts", "A"),
    ("HBARUSD", "IBM", "B"), ("XLMUSD", "DTCC", "B"),
    ("GRAMUSD", "replaces TON Foundation", "B"), ("GRAMUSD", "renames TON", "C"),
]


def tier_of(sym, head):
    for s, frag, t in TIER_RULES:
        if s == sym and frag in head:
            return t
    return "?"


# ----------------------------------------------------------------------------- data
def _get(url, params, tries=6):
    for i in range(tries):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(1.5 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("rate-limited: %s %s" % (url, params))


def fetch_15m(symbol, start_ts, end_ts):
    """Oldest-first 15m candles [start_ts, end_ts] (epoch seconds), paged + cached."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    # round the window to whole days so cache keys are stable
    start_ts = int(start_ts // 86400 * 86400)
    end_ts = int(end_ts // 86400 * 86400 + 86400)
    path = os.path.join(CACHE_DIR, "%s_15m_%d_%d.json" % (symbol, start_ts, end_ts))
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    by_time = {}
    cur_end = end_ts
    while cur_end > start_ts:
        cur_start = max(start_ts, cur_end - MAX_BARS_PER_REQ * BAR)
        data = _get(BASE_URL + "/v2/history/candles",
                    {"resolution": "15m", "symbol": symbol, "start": cur_start, "end": cur_end})
        for c in data.get("result", []):
            by_time[int(c["time"])] = (int(c["time"]), float(c["open"]), float(c["high"]),
                                       float(c["low"]), float(c["close"]))
        cur_end = cur_start
        time.sleep(0.25)
    candles = [by_time[k] for k in sorted(by_time)]
    # drop the still-forming candle so nothing is peeked from the future
    now = int(time.time())
    candles = [c for c in candles if c[0] + BAR <= now]
    with open(path, "w") as f:
        json.dump(candles, f)
    return candles


# ------------------------------------------------------------------------ simulation
def find_entry_idx(candles, entry_ts):
    """Index of first candle whose open time >= entry_ts (None if beyond data)."""
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid][0] < entry_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(candles) else None


def simulate(candles, entry_idx, side, tp_pct, sl_pct, hold_days, fee_bp, slip_bp):
    """Return dict with exit info.  side: 'long' | 'short'."""
    slip = slip_bp / 1e4
    fee = fee_bp / 1e4
    o = candles[entry_idx][1]
    entry = o * (1 + slip) if side == "long" else o * (1 - slip)
    if side == "long":
        tp_px, sl_px = entry * (1 + tp_pct / 100), entry * (1 - sl_pct / 100)
    else:
        tp_px, sl_px = entry * (1 - tp_pct / 100), entry * (1 + sl_pct / 100)
    end_ts = candles[entry_idx][0] + int(hold_days * 86400)
    reason, exit_px, exit_ts = None, None, None
    last = entry_idx
    for i in range(entry_idx, len(candles)):
        t, _, h, l, c = candles[i]
        last = i
        if side == "long":
            hit_sl, hit_tp = l <= sl_px, h >= tp_px
        else:
            hit_sl, hit_tp = h >= sl_px, l <= tp_px
        if hit_sl:                      # SL wins ties (conservative)
            reason, exit_px, exit_ts = "SL", sl_px, t
            break
        if hit_tp:
            reason, exit_px, exit_ts = "TP", tp_px, t
            break
        if t + BAR >= end_ts:
            reason, exit_px, exit_ts = "TIME", c, t + BAR
            break
    if reason is None:                  # ran out of data before the time stop
        reason, exit_px, exit_ts = "OPEN", candles[last][4], candles[last][0] + BAR
    # costs: slippage on exit + fee both sides
    if side == "long":
        exit_fill = exit_px * (1 - slip) if reason in ("TIME", "OPEN") else exit_px
        gross = exit_fill / entry - 1
    else:
        exit_fill = exit_px * (1 + slip) if reason in ("TIME", "OPEN") else exit_px
        gross = (entry - exit_fill) / entry
    ret = gross - 2 * fee
    return {"entry_ts": candles[entry_idx][0], "entry_px": entry, "exit_ts": exit_ts,
            "exit_px": exit_px, "reason": reason, "ret": ret}


def pre_move(candles, idx, hours=24):
    j = idx - int(hours * 3600 / BAR)
    if j < 0:
        return None
    return candles[idx][1] / candles[j][4] - 1


# ---------------------------------------------------------------------------- driver
def run_all(data, delay_h, hold_days, side, args):
    trades = []
    for ev in EVENTS:
        sym, when, known, cat, head = ev
        cs = data.get(sym)
        if not cs:
            continue
        base = when if known else when + timedelta(days=1)      # date-only -> next UTC day 00:00
        entry_ts = int((base + timedelta(hours=delay_h)).timestamp())
        if entry_ts < cs[0][0]:
            continue                                            # event pre-dates Delta history
        idx = find_entry_idx(cs, entry_ts)
        if idx is None:
            continue
        r = simulate(cs, idx, side, args.tp_pct, args.sl_pct, hold_days, args.fee_bp, args.slip_bp)
        r.update(sym=sym, cat=cat, head=head, pre24=pre_move(cs, idx), tier=tier_of(sym, head))
        if args.tiers and r["tier"] not in args.tiers:
            continue
        if args.max_pre24_pct is not None and r["pre24"] is not None and 100 * r["pre24"] > args.max_pre24_pct:
            continue                                            # already pumped before entry -> skip (anti-chase)
        trades.append(r)
    return trades


def stats(trades, notional):
    n = len(trades)
    if n == 0:
        return None
    rets = [t["ret"] for t in trades]
    wins = [x for x in rets if x > 0]
    loss = [x for x in rets if x <= 0]
    gp, gl = sum(wins), -sum(loss)
    return {
        "n": n,
        "tp": sum(1 for t in trades if t["reason"] == "TP"),
        "sl": sum(1 for t in trades if t["reason"] == "SL"),
        "time": sum(1 for t in trades if t["reason"] == "TIME"),
        "open": sum(1 for t in trades if t["reason"] == "OPEN"),
        "win": 100 * len(wins) / n,
        "avg": 100 * sum(rets) / n,
        "med": 100 * statistics.median(rets),
        "pnl": notional * sum(rets),
        "pf": (gp / gl) if gl > 0 else float("inf"),
    }


def fmt_stats(label, s):
    if not s:
        return "%-22s no trades" % label
    pf = "inf" if s["pf"] == float("inf") else "%.2f" % s["pf"]
    return ("%-22s n=%-2d TP=%-2d SL=%-2d TIME=%-2d OPEN=%-2d win=%3.0f%% avg=%+6.1f%% med=%+6.1f%% PF=%-5s pnl=$%+8.0f"
            % (label, s["n"], s["tp"], s["sl"], s["time"], s["open"], s["win"], s["avg"], s["med"], pf, s["pnl"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tp-pct", type=float, default=50.0)
    ap.add_argument("--sl-pct", type=float, default=50.0)
    ap.add_argument("--hold-days", type=float, default=7.0, help="time stop for the base run")
    ap.add_argument("--delay-h", type=float, default=0.0, help="entry delay after news (base run)")
    ap.add_argument("--fee-bp", type=float, default=5.0)
    ap.add_argument("--slip-bp", type=float, default=3.0)
    ap.add_argument("--notional", type=float, default=1000.0)
    ap.add_argument("--max-pre24-pct", type=float, default=None,
                    help="anti-chase filter: skip events where price already rose more than this %% in the 24h before entry")
    ap.add_argument("--tiers", default="", help="only trade these catalyst tiers, e.g. 'A' or 'A,B' (default: all)")
    ap.add_argument("--detail", action="store_true", help="print per-trade table for the base run")
    args = ap.parse_args()
    args.tiers = {x.strip().upper() for x in args.tiers.split(",") if x.strip()}

    # ---- fetch candles (event -2d .. event +31d, capped at now)
    now = int(time.time())
    need = {}
    for sym, when, known, *_ in EVENTS:
        a = int((when - timedelta(days=2)).timestamp())
        b = min(now, int((when + timedelta(days=33)).timestamp()))
        lo, hi = need.get(sym, (a, b))
        need[sym] = (min(lo, a), max(hi, b))
    data = {}
    for sym, (a, b) in sorted(need.items()):
        try:
            data[sym] = fetch_15m(sym, a, b)
        except Exception as e:                                   # noqa: BLE001
            print("!! %s fetch failed: %s" % (sym, e), file=sys.stderr)
            continue
        cs = data[sym]
        print("  %-9s %5d bars  %s -> %s" % (sym, len(cs),
              datetime.fromtimestamp(cs[0][0], UTC).strftime("%Y-%m-%d") if cs else "-",
              datetime.fromtimestamp(cs[-1][0], UTC).strftime("%Y-%m-%d %H:%M") if cs else "-"),
              file=sys.stderr)

    skipped = [e for e in EVENTS if e[0] not in data or not data[e[0]]]
    print("\nNews-event backtest  TP=+%.0f%%  SL=-%.0f%%  fee=%gbp/side slip=%gbp/side  notional=$%g"
          % (args.tp_pct, args.sl_pct, args.fee_bp, args.slip_bp, args.notional))
    print("Events defined: %d   (entry = open of first 15m candle after news; date-only -> next UTC day 00:00)\n" % len(EVENTS))

    for side in ("long", "short"):
        base = run_all(data, args.delay_h, args.hold_days, side, args)
        print("=" * 150)
        print("%s  |  delay=%gh  hold=%gd" % (side.upper(), args.delay_h, args.hold_days))
        print("=" * 150)
        if args.detail or side == "long":
            print("%-9s %-10s %-16s %-16s %8s %6s %-5s %8s  %s" %
                  ("coin", "cat", "entry(UTC)", "exit(UTC)", "pre24h", "", "exit", "ret", "headline"))
            for t in base:
                pre = "%+.0f%%" % (100 * t["pre24"]) if t["pre24"] is not None else "n/a"
                print("%-9s %-10s %-16s %-16s %8s %6s %-5s %+7.1f%%  %s" % (
                    t["sym"].replace("USD", ""), t["cat"],
                    datetime.fromtimestamp(t["entry_ts"], UTC).strftime("%m-%d %H:%M"),
                    datetime.fromtimestamp(t["exit_ts"], UTC).strftime("%m-%d %H:%M"),
                    pre, "", t["reason"], 100 * t["ret"], t["head"][:70]))
        print()
        print(fmt_stats("BASE all events", stats(base, args.notional)))
        if len(base) >= 4:
            rets = [t["ret"] for t in base]
            sd = statistics.pstdev(rets) or 1e-9
            tstat = (sum(rets) / len(rets)) / (sd / len(rets) ** 0.5)
            ex3 = sorted(rets)[:-3]
            print("  robustness: t-stat of mean return = %+.2f | PnL ex-top-3 winners = $%+.0f | worst trade = %+.1f%% | best = %+.1f%%"
                  % (tstat, args.notional * sum(ex3), 100 * min(rets), 100 * max(rets)))
        print("  --- by catalyst tier (A strong / B moderate / C weak) ---")
        for tr in ("A", "B", "C"):
            print(fmt_stats("  tier " + tr, stats([t for t in base if t["tier"] == tr], args.notional)))
        print("  --- by category ---")
        cats = sorted(set(t["cat"] for t in base))
        for c in cats:
            print(fmt_stats("  cat=" + c, stats([t for t in base if t["cat"] == c], args.notional)))
        print()
        print("Sensitivity - entry delay x time stop (n / avg ret / PF / win%)")
        holds = [1, 3, 7, 14, 30]
        delays = [0, 1, 6, 24]
        print("%-10s" % "delay\\hold" + "".join("%-34s" % ("%gd" % h) for h in holds))
        for d in delays:
            row = "%-10s" % ("%gh" % d)
            for h in holds:
                s = stats(run_all(data, d, h, side, args), args.notional)
                if not s:
                    row += "%-34s" % "-"
                else:
                    pf = "inf" if s["pf"] == float("inf") else "%.2f" % s["pf"]
                    row += "%-34s" % ("%d / %+.1f%% / %s / %.0f%%" % (s["n"], s["avg"], pf, s["win"]))
            print(row)
        print()

    if skipped:
        print("Skipped (no Delta data): %s" % ", ".join(sorted(set(e[0] for e in skipped))))
    pre = []
    for sym, when, known, cat, head in EVENTS:
        cs = data.get(sym)
        if cs:
            base_t = when if known else when + timedelta(days=1)
            if int(base_t.timestamp()) < cs[0][0]:
                pre.append("%s (%s)" % (sym, head[:40]))
    if pre:
        print("Skipped (event pre-dates Delta candle history): %s" % "; ".join(pre))


if __name__ == "__main__":
    main()
