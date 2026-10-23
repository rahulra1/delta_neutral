"""NIFTY Supertrend weekly credit-spread backtest on REAL NSE/BSE bhavcopy data.

Strategy rules
  - Entry : 9:31 IST on the trading day BEFORE each SENSEX weekly expiry
            (normally Wednesday; holiday weeks shift it automatically).
  - Signal: Supertrend(20, 3.5, EMA-smoothed ATR) on NIFTY 15-minute candles,
            read at 9:31 from the 9:15-9:30 bar (the last COMPLETED bar, no
            look-ahead).  Green (uptrend) -> BULL PUT spread,
            Red (downtrend) -> BEAR CALL spread.  15m history comes from the
            Groww API (Yahoo keeps only ~60 days of 15m); --st-interval 1h/1d
            use Yahoo instead.
  - Legs  : sell the ~SELL_DELTA option, buy the ~BUY_DELTA option (same side),
            on the next NIFTY weekly expiry.
  - Exit  : stop-loss when the spread costs (1 + SL_MULT) x credit to close
            (SL_MULT=1.0 -> loss equals the premium collected), else 9:30 IST on
            the trading day BEFORE that NIFTY expiry (normally Monday).

Data and pricing model (be aware of these when reading the results)
  - Bhavcopy is END-OF-DAY.  There is no real 9:31 / 9:30 option price, so entry
    and the time-exit are MODELLED: each strike's IV is back-solved (Black-Scholes)
    from the previous trading day's settle, then repriced at the NIFTY level at
    ~9:30 (close of the 9:15 15m bar; the day's open when 15m bars are absent)
    with the correct time-to-expiry (sticky-strike).
  - Stop-loss is checked bar-by-bar on the NIFTY intraday bars: the spread is
    repriced (IV from the prior session's settle) at each bar's adverse extreme
    and fills at the SL level, or at the bar open on a gap.  The real daily
    settle is a backstop that catches IV spikes the constant-IV model can't see
    (booked at that settle; a "filled at SL level" bound is also reported).
    With --st-interval 1d there are no intraday bars, so only settles are used.
  - Time-exit P&L is also reported at two REAL-price bounds: the settle of the
    day before exit (earlier than 9:30) and the settle of the exit day (later).
  - P&L is gross: no brokerage, STT, exchange charges or slippage.

Usage:
    python backtest/nifty_supertrend_weekly_spread_backtest.py            # 15m via Groww
    python backtest/nifty_supertrend_weekly_spread_backtest.py --st-interval 1h --quiet
    python backtest/nifty_supertrend_weekly_spread_backtest.py --trade-symbol SENSEX
"""

import os
import sys
import json
import argparse
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.nse_bhavcopy import get_option_chain  # noqa: E402
from backtest.nse_ema_spread_backtest import (  # noqa: E402
    _bs_price, _bs_delta, _implied_vol, NSE_LOT_SIZES,
)

YF_TICKERS = {'NIFTY': '^NSEI', 'SENSEX': '^BSESN'}
GROWW_SYMBOLS = {'NIFTY': ('NSE', 'NSE-NIFTY'), 'SENSEX': ('BSE', 'BSE-SENSEX')}
INTERVAL_MIN = {'15m': 15, '1h': 60, '1d': 375}   # 1d = full 9:15-15:30 session
EXCHANGE = {'NIFTY': 'NSE', 'SENSEX': 'BSE'}
CANDLE_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.nse_cache')

ENTRY_HM = (9, 31)
EXIT_HM = (9, 30)
SETTLE_HM = (15, 30)   # bhavcopy settle time and option expiry time
SESSION_MIN = 375      # 9:15-15:30
_TRADING_DAYS = None   # set by run() for the trading-time clock


# ─── helpers ───────────────────────────────────────────────────────────────

def _d(s):
    return datetime.strptime(s, '%Y-%m-%d').date()


def _at(day, hm):
    """Naive IST datetime for a 'YYYY-MM-DD' date at (hour, minute)."""
    return datetime.combine(_d(day) if isinstance(day, str) else day,
                            datetime.min.time()).replace(hour=hm[0], minute=hm[1])


def _years(t0, t1):
    """Time from t0 to t1 in years.

    With _TRADING_DAYS set (the default clock) time is counted only during NSE
    sessions (9:15-15:30) on actual trading days, so weekends and holidays add
    no decay -- matching how short-dated index options are priced.  Without it,
    plain calendar time is used.
    """
    if t1 <= t0:
        return 0.0
    if not _TRADING_DAYS:
        return (t1 - t0).total_seconds() / (365.0 * 86400.0)
    mins = 0.0
    d = t0.date()
    while d <= t1.date():
        if d.isoformat() in _TRADING_DAYS:
            s = datetime.combine(d, datetime.min.time()).replace(hour=9, minute=15)
            e = datetime.combine(d, datetime.min.time()).replace(hour=15, minute=30)
            lo, hi = max(s, t0), min(e, t1)
            if hi > lo:
                mins += (hi - lo).total_seconds() / 60.0
        d += timedelta(days=1)
    return mins / (252.0 * SESSION_MIN)


def _px(row):
    """Real price of a bhavcopy row: settle, falling back to close."""
    return row['settle'] if row['settle'] > 0 else row['close']


# ─── data loading ──────────────────────────────────────────────────────────

def _cache_rows(path, rows):
    with open(path, 'w') as f:
        json.dump([{**r, 't': r['t'].isoformat()} for r in rows], f)


def _read_cache(path):
    with open(path) as f:
        return [{**r, 't': datetime.fromisoformat(r['t'])} for r in json.load(f)]


def load_candles_yahoo(symbol, interval, start, end):
    """Index OHLC candles from Yahoo Finance (1d: years, 1h: ~730d, 15m: ~60d)."""
    os.makedirs(CANDLE_CACHE, exist_ok=True)
    path = os.path.join(CANDLE_CACHE,
                        f"yf_{symbol}_{interval}_{start.isoformat()}_{end.isoformat()}.json")
    if os.path.exists(path):
        return _read_cache(path)

    import yfinance as yf
    import pandas as pd
    df = yf.download(YF_TICKERS[symbol], start=start.isoformat(),
                     end=(end + timedelta(days=1)).isoformat(),
                     interval=interval, progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"No {interval} candles from Yahoo for {symbol} "
                           f"({start}..{end}); Yahoo keeps 15m for ~60 days, 1h ~730 days")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    idx = df.index
    if getattr(idx, 'tz', None) is not None:
        idx = idx.tz_convert('Asia/Kolkata').tz_localize(None)
    rows = []
    for t, o, h, l, c in zip(idx, df['Open'], df['High'], df['Low'], df['Close']):
        if any(v != v for v in (o, h, l, c)):  # NaN guard
            continue
        rows.append({'t': t.to_pydatetime(), 'o': float(o), 'h': float(h),
                     'l': float(l), 'c': float(c)})
    _cache_rows(path, rows)
    return rows


def _groww_ts(v):
    """Groww candle timestamp -> naive IST datetime (epoch s / ms, or ISO string)."""
    from datetime import timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    if isinstance(v, (int, float)):
        secs = v / 1000.0 if v > 1e11 else v
        return datetime.fromtimestamp(secs, ist).replace(tzinfo=None)
    dt = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    return dt.astimezone(ist).replace(tzinfo=None) if dt.tzinfo else dt


def load_candles_groww(symbol, interval, start, end, profile_id=None):
    """Index OHLC candles from the Groww historical API, fetched in 30-day chunks
    and cached. Uses the stored Groww profile (or `profile_id`) credentials."""
    os.makedirs(CANDLE_CACHE, exist_ok=True)
    path = os.path.join(CANDLE_CACHE,
                        f"groww_{symbol}_{interval}_{start.isoformat()}_{end.isoformat()}.json")
    if os.path.exists(path):
        return _read_cache(path)

    import config
    from models import get_db
    from api.groww import _get_client
    db = get_db()
    if profile_id:
        p = db.execute("SELECT api_key, api_secret FROM profiles WHERE id=?", (profile_id,)).fetchone()
    else:
        p = db.execute("SELECT api_key, api_secret FROM profiles WHERE broker='groww' "
                       "ORDER BY id LIMIT 1").fetchone()
    db.close()
    if not p:
        raise RuntimeError("No Groww profile found for historical candles")
    config.set_thread_credentials(p['api_key'], p['api_secret'], 'groww')
    client = _get_client(force_refresh=True)

    exch, gsym = GROWW_SYMBOLS[symbol]
    groww_iv = {'15m': '15minute', '1h': '60minute', '1d': '1day'}[interval]
    rows, seen = [], set()
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=29), end)
        resp = client.get_historical_candles(
            exchange=exch, segment='CASH', groww_symbol=gsym,
            start_time=f"{cur.isoformat()} 09:00:00",
            end_time=f"{chunk_end.isoformat()} 15:30:00",
            candle_interval=groww_iv)
        for c in (resp or {}).get('candles', []) or []:
            if isinstance(c, dict):
                t, o, h, l, cl = (c.get('timestamp', c.get('time')), c.get('open'),
                                  c.get('high'), c.get('low'), c.get('close'))
            else:
                t, o, h, l, cl = c[0], c[1], c[2], c[3], c[4]
            ts = _groww_ts(t)
            if ts in seen or None in (o, h, l, cl):
                continue
            seen.add(ts)
            rows.append({'t': ts, 'o': float(o), 'h': float(h), 'l': float(l), 'c': float(cl)})
        cur = chunk_end + timedelta(days=1)
    rows.sort(key=lambda r: r['t'])
    if not rows:
        raise RuntimeError(f"Groww returned no {interval} candles for {symbol}")
    _cache_rows(path, rows)
    return rows


def load_candles(symbol, interval, start, end, source='yahoo', profile_id=None):
    if source == 'groww':
        return load_candles_groww(symbol, interval, start, end, profile_id)
    return load_candles_yahoo(symbol, interval, start, end)


def supertrend(candles, period=20, mult=3.5, atr_mode='ema'):
    """Supertrend with selectable ATR smoothing.

    atr_mode: 'ema' (alpha = 2/(period+1)), 'rma' (Wilder, alpha = 1/period),
              or 'sma'. Returns [{'t', 'dir', 'st'}] for each candle once the ATR
    is seeded; dir = +1 green (uptrend), -1 red (downtrend).
    """
    n = len(candles)
    if n <= period:
        return []
    tr = [candles[0]['h'] - candles[0]['l']]
    for i in range(1, n):
        h, l, pc = candles[i]['h'], candles[i]['l'], candles[i - 1]['c']
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))

    atr = [None] * n
    atr[period - 1] = sum(tr[:period]) / period
    alpha = 2.0 / (period + 1) if atr_mode == 'ema' else 1.0 / period
    for i in range(period, n):
        if atr_mode == 'sma':
            atr[i] = sum(tr[i - period + 1:i + 1]) / period
        else:
            atr[i] = atr[i - 1] + alpha * (tr[i] - atr[i - 1])

    out = []
    fu = fl = None   # final upper / lower bands
    d = 1
    for i in range(period - 1, n):
        c = candles[i]
        hl2 = (c['h'] + c['l']) / 2
        bu, bl = hl2 + mult * atr[i], hl2 - mult * atr[i]
        if fu is None:
            fu, fl = bu, bl
        else:
            pc = candles[i - 1]['c']
            pu, pl = fu, fl   # previous bar's final bands
            fu = bu if (bu < pu or pc > pu) else pu
            fl = bl if (bl > pl or pc < pl) else pl
            # TradingView rule: flip against the PREVIOUS bar's bands.
            if d == -1 and c['c'] > pu:
                d = 1
            elif d == 1 and c['c'] < pl:
                d = -1
        out.append({'t': c['t'], 'dir': d, 'st': fl if d == 1 else fu})
    return out


def load_chains(symbol, exchange, start, end, every_day=True):
    """{date: chain} for every trading day in range (bhavcopy, cached on disk).

    every_day=False keeps only the first trading day of each ISO week, which is
    enough to collect the expiry calendar (expiries are listed weeks ahead).
    """
    chains = {}
    weeks_done = set()
    d = start
    while d <= end:
        wk = d.isocalendar()[:2]
        if d.weekday() < 5 and (every_day or wk not in weeks_done):
            c = get_option_chain(d.strftime('%Y%m%d'), symbol=symbol,
                                 instrument='IDO', exchange=exchange)
            if c:
                chains[c['date']] = c
                weeks_done.add(wk)
        d += timedelta(days=1)
    return chains


# ─── strategy pieces ───────────────────────────────────────────────────────

def _bar_end(t, interval):
    """When a bar starting at `t` is complete (capped at the 15:30 close)."""
    end = t + timedelta(minutes=INTERVAL_MIN[interval])
    close = t.replace(hour=SETTLE_HM[0], minute=SETTLE_HM[1], second=0, microsecond=0)
    if interval == '1d':
        return close
    return min(end, close)


def supertrend_direction_asof(st_points, when, interval):
    """Direction (+1 green / -1 red) of the last bar COMPLETED by `when`.

    At 9:31 with 15m bars this is the same day's 9:15-9:30 bar; with 1h / 1d
    bars it is the previous session's last bar (no look-ahead either way).
    """
    last = None
    for p in st_points:
        if _bar_end(p['t'], interval) <= when:
            last = p
        else:
            break
    return last['dir'] if last else None


def spot_at(day, hm, intraday, daily_open, interval='15m'):
    """Index level at (day, hm): close of the last intraday bar completed by hm
    (with 15m bars that is the 9:15-9:30 bar), else the day's open."""
    if intraday:
        target = _at(day, hm)
        best = None
        for c in intraday.get(day, []):
            if _bar_end(c['t'], interval) <= target:
                best = c
        if best:
            return best['c']
    return daily_open.get(day)


def model_leg(row, s_prev, t_prev, s_now, t_now, r, is_call):
    """Reprice a strike at (s_now, t_now) using IV back-solved from its prior settle."""
    iv = _implied_vol(_px(row), s_prev, row['strike'], t_prev, r, is_call)
    if iv is None:
        return None
    return {'price': _bs_price(s_now, row['strike'], t_now, r, iv, is_call),
            'delta': _bs_delta(s_now, row['strike'], t_now, r, iv, is_call),
            'iv': iv}


def pick_strikes(leg_rows, s_prev, t_prev, s_now, t_now, r, is_call,
                 sell_delta, buy_delta):
    """Model every liquid strike at entry time, pick SELL / BUY by |delta|."""
    cands = []
    for row in leg_rows.values():
        if _px(row) <= 0 or (row['oi'] <= 0 and row['volume'] <= 0):
            continue  # untradeable / stale strike
        m = model_leg(row, s_prev, t_prev, s_now, t_now, r, is_call)
        if m:
            cands.append((abs(m['delta']), row, m))
    if not cands:
        return None, None
    sell = min(cands, key=lambda x: abs(x[0] - sell_delta))
    buy = min(cands, key=lambda x: abs(x[0] - buy_delta))
    if sell[1]['strike'] == buy[1]['strike']:
        return None, None
    return sell, buy


def build_schedule(signal_chains_dates, sensex_expiries, nifty_expiries,
                   trade_expiries, trade_symbol):
    """Entry/exit/expiry tuples derived from the real expiry calendar."""
    cal = sorted(signal_chains_dates)
    sched = []
    for sx in sensex_expiries:
        prior = [d for d in cal if d < sx]
        if len(prior) < 2:
            continue
        entry = prior[-1]          # trading day before SENSEX expiry
        prev = prior[-2]           # IV source for the entry model
        nxt = [n for n in nifty_expiries if n > entry]
        if not nxt:
            continue
        nifty_exp = nxt[0]
        before = [d for d in cal if d < nifty_exp]
        exit_day = before[-1] if before else None   # trading day before NIFTY expiry
        if not exit_day or exit_day <= entry:
            continue
        if trade_symbol == 'NIFTY':
            trade_exp = nifty_exp
        else:  # trade must still be alive at the exit
            later = [e for e in trade_expiries if e > exit_day]
            if not later:
                continue
            trade_exp = later[0]
        sched.append({'entry': entry, 'prev': prev, 'exit': exit_day,
                      'nifty_exp': nifty_exp, 'trade_exp': trade_exp})
    return sched


# ─── backtest ──────────────────────────────────────────────────────────────

def run(args):
    end = _d(args.end)
    start = _d(args.start) if args.start else end - timedelta(days=365)
    trade = args.trade_symbol.upper()
    lot = NSE_LOT_SIZES.get(trade, 50)
    qty = args.lots * lot

    print(f"Loading data {start} .. {end} (bhavcopy is cached on disk) ...", flush=True)
    nifty = load_chains('NIFTY', 'NSE', start - timedelta(days=10), end)
    sensex = load_chains('SENSEX', 'BSE', start - timedelta(days=10), end,
                         every_day=(trade == 'SENSEX'))
    trade_chains = nifty if trade == 'NIFTY' else sensex
    last_data_day = max(trade_chains)

    nifty_exp = sorted({e for c in nifty.values() for e in c['expiries']})
    sensex_exp = sorted({e for c in sensex.values() for e in c['expiries']
                         if start.isoformat() <= e <= end.isoformat()})
    trade_exp = nifty_exp if trade == 'NIFTY' else \
        sorted({e for c in sensex.values() for e in c['expiries']})

    warm = {'1d': 200, '1h': 60, '15m': 20}[args.st_interval]
    source = args.candle_source or ('groww' if args.st_interval == '15m' else 'yahoo')
    try:
        sig_candles = load_candles('NIFTY', args.st_interval, start - timedelta(days=warm), end,
                                   source=source, profile_id=args.groww_profile)
    except Exception as e:
        print(f"\n✗ Could not load {args.st_interval} NIFTY candles from {source}: "
              f"{type(e).__name__}: {e}")
        if source == 'groww':
            print("  The Groww API token in the Groww profile is expired/invalid. Regenerate it\n"
                  "  in Groww (API keys page), update the profile, and re-run. Meanwhile:\n"
                  "    --st-interval 1h                 full-year proxy on Yahoo hourly bars\n"
                  "    --candle-source yahoo            15m on Yahoo (last ~60 days only)")
        sys.exit(1)
    st = supertrend(sig_candles, args.st_period, args.st_mult, args.st_atr)
    print(f"Signal candles: {len(sig_candles)} x {args.st_interval} from {source} "
          f"({sig_candles[0]['t']} .. {sig_candles[-1]['t']})")
    trade_daily = load_candles_yahoo(trade, '1d', start - timedelta(days=10), end)
    opens = {c['t'].date().isoformat(): c['o'] for c in trade_daily}
    # Intraday bars of the traded index: ~9:30 spot + intraday stop-loss path.
    intraday = {}
    if args.st_interval != '1d' and trade == 'NIFTY':
        for c in sig_candles:
            intraday.setdefault(c['t'].date().isoformat(), []).append(c)
    iv_ = args.st_interval
    cal_all = sorted(trade_chains)
    prev_day = {d: cal_all[i - 1] for i, d in enumerate(cal_all) if i > 0}
    global _TRADING_DAYS
    _TRADING_DAYS = set(cal_all) if args.clock == 'trading' else None

    sched = build_schedule(trade_chains.keys(), sensex_exp, nifty_exp, trade_exp, trade)
    sched = [s for s in sched if s['entry'] >= start.isoformat()]
    print(f"{len(sched)} weekly cycles in window; last bhavcopy day {last_data_day}.\n")

    trades, skipped = [], []
    for s in sched:
        if s['exit'] > last_data_day:
            skipped.append((s['entry'], 'exit day not reached yet'))
            continue
        direction = supertrend_direction_asof(st, _at(s['entry'], ENTRY_HM), args.st_interval)
        if direction is None:
            skipped.append((s['entry'], 'no supertrend'))
            continue
        is_call = direction < 0          # red -> bear call ; green -> bull put
        side = 'CE' if is_call else 'PE'
        prev_c = trade_chains.get(s['prev'])
        s_open = spot_at(s['entry'], ENTRY_HM, intraday, opens, iv_)
        legs = prev_c and prev_c['options'].get(s['trade_exp'])
        if not legs or not s_open:
            skipped.append((s['entry'], 'missing chain/open'))
            continue

        exp_t = _at(s['trade_exp'], SETTLE_HM)
        t_prev = _years(_at(s['prev'], SETTLE_HM), exp_t)
        t_entry = _years(_at(s['entry'], ENTRY_HM), exp_t)
        sell, buy = pick_strikes(legs[side], prev_c['spot'], t_prev, s_open, t_entry,
                                 args.rate, is_call, args.sell_delta, args.buy_delta)
        if not sell:
            skipped.append((s['entry'], 'no strikes'))
            continue
        k_sell, k_buy = sell[1]['strike'], buy[1]['strike']
        credit_u = sell[2]['price'] - buy[2]['price']
        if credit_u <= 0:
            skipped.append((s['entry'], 'no net credit'))
            continue
        credit = credit_u * qty
        sl_level = credit * (1 + args.sl_mult)

        def spread_settle(day):
            c = trade_chains.get(day)
            lg = c and c['options'].get(s['trade_exp'])
            if not lg:
                return None
            sr, br = lg[side].get(k_sell), lg[side].get(k_buy)
            if not sr or not br:
                return None
            return (_px(sr) - _px(br)) * qty

        # ── Stop-loss ──────────────────────────────────────────────────────
        # 1) Intraday path: reprice the spread on every NIFTY bar using each
        #    strike's IV from the prior session's settle (sticky-strike). The
        #    stop triggers on the bar's ADVERSE extreme (low for a bull put,
        #    high for a bear call) and fills at the SL level -- or at the bar's
        #    open value if the market gapped through it.
        # 2) Backstop: real daily settle >= SL (catches IV jumps the
        #    constant-IV intraday model can't see), booked at that settle.
        cal = sorted(d for d in trade_chains if s['entry'] <= d < s['exit'])
        entry_dt = _at(s['entry'], ENTRY_HM)

        def day_ivs(day):
            src = s['prev'] if day == s['entry'] else prev_day.get(day)
            c = trade_chains.get(src)
            lg = c and c['options'].get(s['trade_exp'])
            if not lg or k_sell not in lg[side] or k_buy not in lg[side]:
                return None
            t_src = _years(_at(src, SETTLE_HM), exp_t)
            ivs = _implied_vol(_px(lg[side][k_sell]), c['spot'], k_sell, t_src, args.rate, is_call)
            ivb = _implied_vol(_px(lg[side][k_buy]), c['spot'], k_buy, t_src, args.rate, is_call)
            return (ivs, ivb) if ivs is not None and ivb is not None else None

        def model_spread(S, when, ivs):
            t = _years(when, exp_t)
            return (_bs_price(S, k_sell, t, args.rate, ivs[0], is_call)
                    - _bs_price(S, k_buy, t, args.rate, ivs[1], is_call)) * qty

        exit_val, reason, exit_when = None, None, None
        for day in cal:
            ivs = day_ivs(day) if intraday else None
            if ivs:
                for bar in intraday.get(day, []):
                    b_end = _bar_end(bar['t'], iv_)
                    if b_end <= entry_dt:
                        continue  # bar completed before we entered
                    adverse = bar['h'] if is_call else bar['l']
                    if model_spread(adverse, b_end, ivs) >= sl_level:
                        v_open = model_spread(bar['o'], max(bar['t'], entry_dt), ivs)
                        exit_val = max(sl_level, v_open)
                        reason = 'stoploss_gap' if v_open > sl_level else 'stoploss'
                        exit_when = f"{day} {b_end.strftime('%H:%M')} intraday"
                        break
                if exit_val is not None:
                    break
            v = spread_settle(day)
            if v is not None and v >= sl_level:
                exit_val, reason, exit_when = v, 'stoploss_settle', f"{day} settle"
                break

        bound_prev = spread_settle(cal[-1]) if cal else None
        bound_exit = spread_settle(s['exit'])

        if exit_val is None:
            # Time exit at 9:30 on the exit day, modelled from the prior settle.
            pc = trade_chains.get(cal[-1]) if cal else None
            pl = pc and pc['options'].get(s['trade_exp'])
            x_open = spot_at(s['exit'], EXIT_HM, intraday, opens, iv_)
            ms = mb = None
            if pl and x_open and pl[side].get(k_sell) and pl[side].get(k_buy):
                t_p = _years(_at(cal[-1], SETTLE_HM), exp_t)
                t_x = _years(_at(s['exit'], EXIT_HM), exp_t)
                ms = model_leg(pl[side][k_sell], pc['spot'], t_p, x_open, t_x, args.rate, is_call)
                mb = model_leg(pl[side][k_buy], pc['spot'], t_p, x_open, t_x, args.rate, is_call)
            if ms and mb:
                exit_val = (ms['price'] - mb['price']) * qty
            elif bound_exit is not None:
                exit_val = bound_exit  # model unavailable -> real exit-day settle
            else:
                skipped.append((s['entry'], 'no exit price'))
                continue
            reason = 'stoploss_gap' if exit_val >= sl_level else 'time_exit'
            exit_when = f"{s['exit']} 09:30"

        pnl = credit - exit_val
        trades.append({
            'entry': s['entry'], 'weekday': _d(s['entry']).strftime('%a'),
            'exit': s['exit'], 'expiry': s['trade_exp'],
            'st': 'green' if direction > 0 else 'red',
            'type': 'bear_call' if is_call else 'bull_put',
            'spot_open': s_open, 'sell_k': k_sell, 'buy_k': k_buy,
            'sell_d': sell[2]['delta'], 'buy_d': buy[2]['delta'],
            'credit_pts': credit_u, 'credit': credit, 'exit_val': exit_val,
            'reason': reason, 'exit_when': exit_when, 'pnl': pnl,
            'pnl_prev_settle': (credit - bound_prev) if reason == 'time_exit' and bound_prev is not None else pnl,
            'pnl_exit_settle': (credit - bound_exit) if reason == 'time_exit' and bound_exit is not None else pnl,
            'pnl_sl_capped': (credit - sl_level) if reason == 'stoploss_settle' else pnl,
            'max_risk': abs(k_sell - k_buy) * qty - credit,
        })

    report(trades, skipped, args, trade, qty, start, end)
    if args.json_out:
        with open(args.json_out, 'w') as f:
            json.dump({'trades': trades, 'skipped': skipped}, f, indent=2, default=str)
        print(f"Trades written to {args.json_out}")
    return trades


def report(trades, skipped, args, trade, qty, start, end):
    w = 96
    print('=' * w)
    print(f"  NIFTY SUPERTREND WEEKLY SPREAD — trading {trade} options  ({start} .. {end})")
    print(f"  Supertrend {args.st_interval} ({args.st_period},{args.st_mult}, ATR {args.st_atr}) on NIFTY | "
          f"Sell {args.sell_delta}Δ / Buy {args.buy_delta}Δ | SL = {args.sl_mult:.0%} of credit | "
          f"{args.lots} lot = {qty} qty | BS clock: {args.clock}")
    print('=' * w)
    if not trades:
        print(f"  No trades. Skipped: {skipped}")
        return
    if not args.quiet:
        print(f"  {'ENTRY':<15}{'ST':<6}{'TYPE':<10}{'SELL/BUY':<13}{'Δs/Δb':<12}"
              f"{'CREDIT':>8}{'PNL':>9}  EXIT")
        for t in trades:
            print(f"  {t['entry']+' '+t['weekday']:<15}{t['st']:<6}{t['type']:<10}"
                  f"{int(t['sell_k'])}/{int(t['buy_k']):<7}"
                  f"{abs(t['sell_d']):.2f}/{abs(t['buy_d']):.2f}{'':<3}"
                  f"{t['credit']:>8.0f}{t['pnl']:>9.0f}  {t['reason']} @ {t['exit_when']}")
        print('-' * w)

    n = len(trades)
    wins = [t for t in trades if t['pnl'] > 0]
    total = sum(t['pnl'] for t in trades)
    cum = peak = dd = 0.0
    for t in trades:
        cum += t['pnl']
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    reasons, dirs, wdays = {}, {}, {}
    for t in trades:
        reasons[t['reason']] = reasons.get(t['reason'], 0) + 1
        dirs[t['type']] = dirs.get(t['type'], 0) + 1
        wdays[t['weekday']] = wdays.get(t['weekday'], 0) + 1
    by_type = {k: sum(t['pnl'] for t in trades if t['type'] == k) for k in dirs}
    avg_w = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    losses = [t for t in trades if t['pnl'] <= 0]
    avg_l = sum(t['pnl'] for t in losses) / len(losses) if losses else 0
    worst = min(trades, key=lambda t: t['pnl'])

    print(f"  Trades:          {n}  (skipped {len(skipped)})   entry weekdays {wdays}")
    print(f"  Win rate:        {len(wins)/n*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Total P&L:       ₹{total:,.0f}   ({sum(t['pnl'] for t in trades)/qty:,.1f} index pts per unit)")
    print(f"    exit priced at prior-day settle (earlier bound): ₹{sum(t['pnl_prev_settle'] for t in trades):,.0f}")
    print(f"    exit priced at exit-day settle  (later bound):   ₹{sum(t['pnl_exit_settle'] for t in trades):,.0f}")
    print(f"    settle-detected stops filled at the SL level:    ₹{sum(t['pnl_sl_capped'] for t in trades):,.0f}")
    print(f"  Avg win / loss:  ₹{avg_w:,.0f} / ₹{avg_l:,.0f}")
    print(f"  Worst trade:     ₹{worst['pnl']:,.0f} on {worst['entry']} ({worst['type']}, {worst['reason']})")
    print(f"  Max drawdown:    ₹{dd:,.0f}")
    print(f"  Exit reasons:    {reasons}")
    print(f"  By spread type:  " + ', '.join(f"{k} {dirs[k]} trades ₹{by_type[k]:,.0f}" for k in dirs))
    print(f"  Avg credit:      ₹{sum(t['credit'] for t in trades)/n:,.0f}   "
          f"max defined risk ₹{max(t['max_risk'] for t in trades):,.0f} (one spread open at a time)")
    if skipped:
        print(f"  Skipped cycles:  {skipped}")

    months = {}
    for t in trades:
        m = months.setdefault(t['entry'][:7], [0.0, 0, 0])
        m[0] += t['pnl']
        m[1] += 1
        m[2] += t['pnl'] > 0
    print('-' * w)
    print(f"  {'MONTH':<10}{'TRADES':>7}{'WINS':>6}{'P&L':>12}{'CUM':>12}")
    c = 0.0
    for k in sorted(months):
        p, cnt, wn = months[k]
        c += p
        print(f"  {k:<10}{cnt:>7}{wn:>6}{p:>12,.0f}{c:>12,.0f}")
    print('-' * w)
    print("  Entry / 9:30 exit / intraday stops are Black-Scholes reprices on real NIFTY bars (IV from")
    print("  the prior settle); settle-checked stops use real prices. Gross P&L, no costs/slippage.")
    print('=' * w)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--trade-symbol', default='NIFTY', choices=['NIFTY', 'SENSEX'])
    ap.add_argument('--st-interval', default='15m', choices=['15m', '1h', '1d'])
    ap.add_argument('--st-period', type=int, default=20)
    ap.add_argument('--st-mult', type=float, default=3.5)
    ap.add_argument('--st-atr', default='ema', choices=['ema', 'rma', 'sma'],
                    help='ATR smoothing for the Supertrend (default ema)')
    ap.add_argument('--candle-source', default=None, choices=['groww', 'yahoo'],
                    help='default: groww for 15m (1yr history), yahoo for 1h/1d')
    ap.add_argument('--groww-profile', type=int, default=None,
                    help='profile id whose Groww keys fetch 15m history')
    ap.add_argument('--sell-delta', type=float, default=0.20)
    ap.add_argument('--buy-delta', type=float, default=0.10)
    ap.add_argument('--sl-mult', type=float, default=1.0,
                    help='stop when loss = sl_mult x credit (1.0 = premium collected)')
    ap.add_argument('--lots', type=int, default=1)
    ap.add_argument('--start', default=None, help='default: end - 365 days')
    ap.add_argument('--end', default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument('--rate', type=float, default=0.065)
    ap.add_argument('--clock', default='trading', choices=['trading', 'calendar'],
                    help='Black-Scholes time: trading-session minutes (default) or calendar time')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--json-out', default=None)
    run(ap.parse_args())


if __name__ == '__main__':
    main()
