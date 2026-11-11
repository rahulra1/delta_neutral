"""NSE Scanner Overnight Options — PAPER trades Sector Scanner picks overnight (15:15 -> next day 09:17).

Daily flow (IST):
  1. ENTRY at 15:15 (entry window 15:15-15:25) on trading days:
       read the live NSE sectors (api.sector_scanner); walk the BULLISH / BEARISH sectors (sector_group,
       default Sectoral) from the biggest |% change| down and take the TOP setup of EACH sector
       (scanner rank; LONG / SHORT, labelled LATE after 14:30) whose direction matches the sector bias.
       Only F&O stocks (listed stock options in Groww's instruments list). One pick per sector, max
       max_trades (5) picks, never the same stock twice.
  2. Paper-BUY 1 lot (configurable) of the ATM CALL for LONG / ATM PUT for SHORT.
       Expiry: the nearest expiry with at least min_dte (15) days left, otherwise the next month.
       Price = Groww option-chain LTP. Skipped if lots x lot size x premium > max_premium.
  3. EXIT every position at 09:17 on the NEXT trading day (Groww option LTP). No stop / target.
       If the app was down at 09:17 the positions are sold as soon as it is back during market hours.
  4. Estimated Groww F&O charges are deducted from the realized P&L.

No real orders are ever placed (paper only). Needs a Groww profile with a valid token.
Backtest of the same rules: backtest/sector_scanner_overnight_options.py.
"""

import logging
import time
from datetime import datetime, timedelta

from strategy.nse_sector_options import IST, NseSectorOptions, estimate_charges

logger = logging.getLogger(__name__)
TAG = '[Scanner Overnight]'
ENTRY_WINDOW_MIN = 10            # keep trying the entry scan for this long after 15:15
EXIT_RETRY_MIN = 13              # 09:17 .. 09:30: wait for a real option LTP, then use the last mark


class NseScannerOvernight(NseSectorOptions):
    DTE_STRICT = False         # unchanged: expiry at least min_dte (15) days away

    def __init__(self, max_trades=5, min_dte=15, lots=1, max_premium=100000.0, sector_group='Sectoral',
                 entry_hour=15, entry_minute=15, exit_hour=9, exit_minute=17, trading_days=None,
                 monitor_interval=15, run_immediately=False):
        super().__init__(entry_hour=entry_hour, entry_minute=entry_minute, exit_hour=exit_hour,
                         exit_minute=exit_minute, lots=lots, min_dte=min_dte, max_premium=max_premium,
                         sector_group=sector_group, trading_days=trading_days, monitor_interval=monitor_interval,
                         run_immediately=run_immediately)
        self.max_trades = max(1, int(max_trades))
        self._base_params = {
            'mode': 'overnight', 'max_trades': self.max_trades, 'min_dte': self.min_dte, 'lots': self.lots,
            'max_premium': self.max_premium, 'sector_group': self.sector_group,
            'entry_hour': self.entry_hour, 'entry_minute': self.entry_minute,
            'exit_hour': self.exit_hour, 'exit_minute': self.exit_minute,
            'trading_days': self.trading_days, 'monitoring_interval': self.monitor_interval,
            'run_immediately': self.run_immediately, 'dry_run': True,
        }

    # ------------------------------------------------------------------ lifecycle
    def initialize(self):
        self._running = True
        from config import get_api_key, get_api_secret, _thread_local
        self._api_key, self._api_secret = get_api_key(), get_api_secret()
        self._broker = getattr(_thread_local, 'broker', '')
        days = ','.join(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][d] for d in self.trading_days)
        print(f"{TAG} Started [PAPER] | entry {self.entry_hour:02d}:{self.entry_minute:02d} IST -> exit next trading day "
              f"{self.exit_hour:02d}:{self.exit_minute:02d} | days {days}")
        print(f"{TAG} Top setup of each BULLISH/BEARISH {self.sector_group.lower()} sector, max {self.max_trades} "
              f"(one per sector), F&O stocks only | {self.lots} lot ATM CE (LONG) / PE (SHORT) | expiry >= "
              f"{self.min_dte} days else next month | max premium Rs {self.max_premium:,.0f} | no SL / TP"
              + (" | RUN-NOW (test)" if self.run_immediately else ""))
        if self._broker != 'groww':
            print(f"{TAG} ⚠ Profile broker is '{self._broker}', not 'groww' — option prices need a Groww profile")
        open_legs = self._open_legs()
        if open_legs:
            print(f"{TAG} Resumed with {len(open_legs)} overnight position(s): "
                  + ', '.join(l['symbol'] for l in open_legs) + f" — selling at the next {self.exit_hour:02d}:"
                  f"{self.exit_minute:02d}")
        return True

    def monitor(self):
        self._apply_thread_context()
        first = True
        while self._running:
            try:
                now = datetime.now(IST)
                force = first and self.run_immediately
                if self._exit_due(now):
                    self._exit_overnight(now)
                if force or self._entry_due(now):
                    self._scan_and_enter(now, force=force)
                if self._open_legs() and self._market_hours(now):
                    self._refresh_marks(now)
                first = False
            except Exception as e:
                logger.error("%s cycle failed: %s", TAG, e)
                print(f"{TAG} ⚠ cycle error: {e}")
            self._interruptible_sleep(self._sleep_for(datetime.now(IST)))

    # ------------------------------------------------------------------ timing
    def _market_hours(self, now):
        return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) < (15, 30)

    def _market_open(self):
        try:
            import api.sector_scanner as ss
            return (ss._cached('mstatus', 60, ss._market_status) or {}).get('status') == 'Open'
        except Exception as e:                      # noqa: BLE001
            logger.warning("%s market status failed: %s", TAG, e)
            return None                             # unknown -> rely on prices being available

    def _entry_due(self, now):
        today = now.strftime('%Y-%m-%d')
        if now.weekday() not in self.trading_days or self._entered_day == today:
            return False
        start = self._entry_time(now)
        end = start + timedelta(minutes=ENTRY_WINDOW_MIN)
        if now >= end:                              # started after the window: no entry today
            self._entered_day = today
            print(f"{TAG} {today} entry window {start:%H:%M}-{end:%H:%M} already passed — next entry on the next "
                  f"trading day")
            self._persist_state()
            return False
        return now >= start

    def _in_entry_window(self, now):
        start = self._entry_time(now)
        return (now.weekday() in self.trading_days and self._entered_day != now.strftime('%Y-%m-%d')
                and start <= now < start + timedelta(minutes=ENTRY_WINDOW_MIN))

    def _exit_due(self, now):
        """True when there are positions from an EARLIER day and it is 09:17+ on a market day."""
        today = now.date().isoformat()
        old = [l for l in self._open_legs() if l['opened_at'][:10] < today]
        if not old or not self._market_hours(now):
            return False
        return (now.hour, now.minute) >= (self.exit_hour, self.exit_minute)

    def _sleep_for(self, now):
        if self._in_entry_window(now) or self._exit_due(now):
            return 20
        if self._open_legs() and self._market_hours(now):
            return self.monitor_interval
        targets = []
        if self._open_legs():
            ex = now.replace(hour=self.exit_hour, minute=self.exit_minute, second=5, microsecond=0)
            targets.append(ex if ex > now else ex + timedelta(days=1))
        en = self._entry_time(now)
        targets.append(en if en > now else en + timedelta(days=1))
        return max(5, min(min((t - now).total_seconds() for t in targets), 300))

    # ------------------------------------------------------------------ entry (15:15)
    def _scan_and_enter(self, now, force=False):
        today = now.strftime('%Y-%m-%d')
        from api.sector_scanner import get_sectors, get_sector_stocks
        overview = get_sectors()
        mkt = (overview.get('market') or {}).get('status')
        if mkt != 'Open' and not force:
            self._entered_day = today
            print(f"{TAG} {today} market status '{mkt}' — no entry today")
            self._persist_state()
            return
        held = {l['underlying'] for l in self._open_legs()}
        slots = self.max_trades - len(self._open_legs())
        cands = [s for s in overview['sectors'] if s.get('group') == self.sector_group and s['bias'] != 'MIXED']
        cands.sort(key=lambda s: abs(s['pchange']), reverse=True)
        print(f"\n{TAG} {now:%Y-%m-%d %H:%M} scan | {len(cands)} BULLISH/BEARISH {self.sector_group.lower()} sectors: "
              + ', '.join(f"{s['index'].replace('NIFTY ', '')} {s['pchange']:+.2f}%" for s in cands[:8])
              + f" | slots {slots}")
        picks, skipped = [], []
        for sec in cands:
            if len(picks) >= slots:
                break
            try:
                d = get_sector_stocks(sec['index'])
            except Exception as e:                  # noqa: BLE001
                skipped.append(f"{sec['index']} unavailable ({e})")
                continue
            if d['bias'] == 'MIXED':
                skipped.append(f"{sec['index']} now MIXED")
                continue
            want = 'LONG' if d['bias'] == 'BULLISH' else 'SHORT'
            for r in d['stocks']:                   # scanner order: setups first, then score / RVOL
                if r['setup'] not in ('LONG', 'SHORT', 'LATE') or r['direction'] != want:
                    continue
                if r['symbol'] in held or r['symbol'] in {p[1]['symbol'] for p in picks}:
                    continue
                contract, why = self._atm_contract(r['symbol'], 'CE' if want == 'LONG' else 'PE')
                if not contract:
                    skipped.append(f"{r['symbol']} ({why})")
                    continue                        # next stock of the SAME sector
                picks.append((d, r, contract))
                break                               # one pick per sector
        for d, r, c in picks:
            self._enter(d, r, c, rank=len([l for l in self._open_legs() if l['opened_at'][:10] == today]) + 1)
        if skipped:
            print(f"{TAG}   passed over: {', '.join(skipped[-10:])}")
        if picks or force or now >= self._entry_time(now) + timedelta(minutes=ENTRY_WINDOW_MIN - 1):
            self._entered_day = today
            if not picks:
                print(f"{TAG} No F&O setup in a bullish/bearish sector — no trade today")
        else:
            print(f"{TAG} No tradeable setup yet — rescanning until "
                  f"{self._entry_time(now) + timedelta(minutes=ENTRY_WINDOW_MIN):%H:%M}")
        self._last_scan = {'time': now.isoformat(), 'day': today, 'picks': [p[1]['symbol'] for p in picks],
                           'sectors': [s['index'] for s in cands[:8]], 'skipped': skipped[-10:]}
        self._persist_state()

    def _enter(self, sector, row, c, rank=1):
        now = datetime.now(IST)
        leg = {
            'symbol': c['trading_symbol'], 'underlying': row['symbol'], 'type': 'call' if c['kind'] == 'CE' else 'put',
            'strike': c['strike'], 'expiry': c['expiry'], 'side': 'buy', 'direction': row['direction'],
            'qty': c['qty'], 'size': self.lots, 'lot_size': c['lot_size'],
            'entry_price': c['ltp'], 'current_mark': c['ltp'], 'current_pnl': 0.0,
            'stock_entry': round(c['spot'], 2), 'stock_mark': round(c['spot'], 2),
            'sector': sector['index'], 'sector_pchg': sector['pchange'], 'setup': row['setup'],
            'rank': rank, 'opened_at': now.isoformat(), 'stopped': False,
        }
        with self._legs_lock:
            self.legs = [l for l in self.legs if not l.get('stopped')] + [leg]
        if not any(t.get('date') == now.strftime('%Y-%m-%d') and t['action'] == 'BUY' for t in self.trade_log):
            self.total_days_traded += 1
        self.total_trades += 1
        self.trade_log.append({
            'ts': now.isoformat(), 'date': now.strftime('%Y-%m-%d'), 'action': 'BUY', 'symbol': c['trading_symbol'],
            'underlying': row['symbol'], 'qty': c['qty'], 'price': round(c['ltp'], 2),
            'stock_price': round(c['spot'], 2), 'sector': sector['index'],
            'reason': f"#{rank} {row['direction']} setup, {sector['index']} {sector['pchange']:+.2f}%",
        })
        print(f"{TAG} [PAPER] BUY #{rank} {c['trading_symbol']} x{c['qty']} ({self.lots} lot) @ Rs {c['ltp']:.2f} "
              f"= Rs {c['ltp'] * c['qty']:,.0f} | {row['direction']} {row['symbol']} @ {c['spot']:.2f} | "
              f"{sector['index']} {sector['pchange']:+.2f}% | expiry {c['expiry']} | IV {c['iv']}")

    # ------------------------------------------------------------------ marks / exit (09:17)
    def _refresh_marks(self, now):
        for leg in self._open_legs():
            try:
                spot, opt = self._marks(leg)
            except Exception:                       # noqa: BLE001
                continue
            if opt:
                leg['current_mark'] = round(opt, 2)
                leg['current_pnl'] = round((opt - leg['entry_price']) * leg['qty'], 2)
            if spot:
                leg['stock_mark'] = round(spot, 2)
        self._pnl_history.append((now.isoformat(), round(self.pnl, 2)))
        self._pnl_history = self._pnl_history[-500:]

    def _exit_overnight(self, now):
        if self._market_open() is False:
            return                                  # holiday / closed: try again later
        today = now.date().isoformat()
        late = now >= now.replace(hour=self.exit_hour, minute=self.exit_minute, second=0) + timedelta(minutes=EXIT_RETRY_MIN)
        for leg in [l for l in self._open_legs() if l['opened_at'][:10] < today]:
            try:
                spot, opt = self._marks(leg)
            except Exception as e:                  # noqa: BLE001
                spot, opt = None, None
                logger.warning("%s exit marks %s failed: %s", TAG, leg['symbol'], e)
            if spot:
                leg['stock_mark'] = round(spot, 2)
            if opt:
                self._exit(leg, reason='next_day_0917', opt=opt)
            elif late:
                self._exit(leg, reason='next_day_no_ltp', opt=leg.get('current_mark') or leg['entry_price'])

    def close_all(self):
        """Manual stop: paper-sell every open option at the current LTP."""
        self._running = False
        for leg in self._open_legs():
            self._exit(leg, reason='manual_stop')
        self._persist_state()

    def _exit(self, leg, reason, opt=None):
        if opt is None:
            try:
                _, opt = self._marks(leg)
            except Exception:                       # noqa: BLE001
                opt = None
        px = opt or leg.get('current_mark') or leg['entry_price']
        gross = (px - leg['entry_price']) * leg['qty']
        fees = estimate_charges(leg['entry_price'] * leg['qty'], px * leg['qty'])
        net = gross - fees
        now = datetime.now(IST)
        with self._legs_lock:
            leg.update(stopped=True, exit_price=round(px, 2), current_mark=round(px, 2), current_pnl=0.0,
                       exit_reason=reason)
            self.cumulative_pnl += net
            self.total_charges += fees
        side = 1 if leg['type'] == 'call' else -1
        move = side * ((leg.get('stock_mark') or leg['stock_entry']) / leg['stock_entry'] - 1) * 100
        self.trade_log.append({
            'ts': now.isoformat(), 'date': now.strftime('%Y-%m-%d'), 'action': 'SELL', 'symbol': leg['symbol'],
            'underlying': leg['underlying'], 'qty': leg['qty'], 'price': round(px, 2),
            'stock_price': leg.get('stock_mark'), 'gross': round(gross, 2), 'charges': fees, 'pnl': round(net, 2),
            'reason': reason,
        })
        print(f"{TAG} [PAPER] SELL {leg['symbol']} x{leg['qty']} @ Rs {px:.2f} | {reason} | stock {leg['underlying']} "
              f"{leg.get('stock_mark')} ({move:+.2f}% our way) | gross Rs {gross:+,.0f} - charges Rs {fees:,.0f} = "
              f"Rs {net:+,.0f} | cumulative Rs {self.cumulative_pnl:+,.0f}")
        self._persist_state()

    # ------------------------------------------------------------------ persistence / status
    def _persist_state(self):
        if not self._sid:
            return
        try:
            from models import update_strategy_db
            legs = [dict(l) for l in self._open_legs()]
            details = {**self._base_params, 'trade_log': self.trade_log[-300:],
                       'cumulative_pnl': self.cumulative_pnl, 'total_charges': self.total_charges,
                       'total_trades': self.total_trades, 'total_days_traded': self.total_days_traded,
                       'entered_day': self._entered_day, 'last_scan': self._last_scan}
            update_strategy_db(self._sid, details=details, legs=legs, pnl=round(self.pnl, 2))
        except Exception as e:                      # noqa: BLE001
            logger.warning("%s persist failed: %s", TAG, e)

    def restore_state(self, details=None, legs=None):
        details = details or {}
        self.cumulative_pnl = float(details.get('cumulative_pnl') or 0)
        self.total_charges = float(details.get('total_charges') or 0)
        self.total_trades = int(details.get('total_trades') or 0)
        self.total_days_traded = int(details.get('total_days_traded') or 0)
        self.trade_log = details.get('trade_log') or []
        self._entered_day = details.get('entered_day')
        self._last_scan = details.get('last_scan')
        with self._legs_lock:
            self.legs = [dict(l) for l in (legs or []) if isinstance(l, dict) and not l.get('stopped')]
        return self

    def status(self):
        legs = [dict(l) for l in self._open_legs()]
        now = datetime.now(IST)
        if legs:
            nxt = f"selling {len(legs)} position(s) at the next {self.exit_hour:02d}:{self.exit_minute:02d}"
        elif self._entered_day == now.strftime('%Y-%m-%d') or now.weekday() not in self.trading_days:
            nxt = f"next entry {self.entry_hour:02d}:{self.entry_minute:02d} on the next trading day"
        else:
            nxt = f"next entry today {self.entry_hour:02d}:{self.entry_minute:02d}"
        return {
            'cumulative_pnl': round(self.cumulative_pnl, 2), 'total_pnl': round(self.pnl, 2),
            'total_charges': round(self.total_charges, 2), 'total_trades': self.total_trades,
            'days_traded': self.total_days_traded, 'trade_log': self.trade_log[-30:], 'legs': legs,
            'last_scan': self._last_scan, 'entered_day': self._entered_day, 'next_action': nxt,
            'params': self._base_params,
        }
