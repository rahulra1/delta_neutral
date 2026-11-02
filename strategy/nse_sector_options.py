"""NSE Sector Options — PAPER trades ATM stock options picked by the Sector Scanner.

Daily flow (IST, trading days only):
  1. At the entry time (default 09:45) read the live NSE sectors (api.sector_scanner) and walk
     the BULLISH / BEARISH sectors from the biggest |% change| down.
  2. In each sector take the top-ranked LONG / SHORT setup that has listed stock options
     (Groww public instruments master gives the lot size, expiries and contract symbols).
  3. Paper-BUY 1 lot (configurable) of the ATM CALL for LONG or ATM PUT for SHORT on the
     nearest expiry with >= min_dte (default 10) days left, i.e. the current month until it is
     under 10 days from expiry, then the next month. Price = Groww option-chain LTP.
  4. Exit is decided ONLY by the STOCK price (Groww CASH LTP):
       stop   = stock moves stop_pct % against the entry stock price
       target = stock moves target_pct % in favour
     else square-off at the exit time (default 15:15). Paper-SELL at the Groww option LTP.
  5. One trade per day. Estimated Groww F&O charges are deducted from the realized P&L.

No real orders are ever placed (paper only). Needs a Groww profile with a valid token for
the option chain / LTPs; the sector scan itself uses public NSE data.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
TAG = '[Sector Options]'
MAX_PRICE_FAILURES = 20          # consecutive failed price polls before we stop trusting marks


def estimate_charges(buy_value, sell_value):
    """Groww F&O option-buy charges for one round trip (Rs)."""
    brokerage = 40.0                                  # Rs 20 per executed order x 2
    txn = 0.0003503 * (buy_value + sell_value)        # NSE options transaction charge
    sebi = 0.000001 * (buy_value + sell_value)
    stt = 0.001 * sell_value                          # 0.1% on sell-side premium
    stamp = 0.00003 * buy_value
    return round(brokerage + txn + sebi + stt + stamp + 0.18 * (brokerage + txn + sebi), 2)


class NseSectorOptions(BaseStrategy):

    def __init__(self, entry_hour=9, entry_minute=45, exit_hour=15, exit_minute=15,
                 stop_pct=0.5, target_pct=0.75, lots=1, min_dte=10, max_premium=100000.0,
                 entry_window_min=30, sector_group='Sectoral', trading_days=None,
                 monitor_interval=15, run_immediately=False):
        self.entry_hour, self.entry_minute = int(entry_hour), int(entry_minute)
        self.exit_hour, self.exit_minute = int(exit_hour), int(exit_minute)
        self.stop_pct, self.target_pct = float(stop_pct), float(target_pct)
        self.lots = max(1, int(lots))
        self.min_dte = int(min_dte)
        self.max_premium = float(max_premium)
        self.entry_window_min = int(entry_window_min)
        self.sector_group = sector_group
        self.trading_days = trading_days if trading_days is not None else [0, 1, 2, 3, 4]
        self.monitor_interval = max(5, int(monitor_interval))
        self.run_immediately = bool(run_immediately)

        self.legs = []
        self._legs_lock = threading.Lock()
        self.cumulative_pnl = 0.0
        self.total_charges = 0.0
        self.total_trades = 0
        self.total_days_traded = 0
        self.trade_log = []
        self._entered_day = None          # 'YYYY-MM-DD' of the last day an entry was made / given up
        self._last_scan = None            # summary of the last scan for the UI
        self._running = False
        self._pnl_history = []
        self._price_failures = 0

        # set by app.py
        self._sid = None
        self._user_id = None
        self._api_key = None
        self._api_secret = None
        self._broker = None
        self._log_queue = None
        self._log_history = None

        self._base_params = {
            'entry_hour': self.entry_hour, 'entry_minute': self.entry_minute,
            'exit_hour': self.exit_hour, 'exit_minute': self.exit_minute,
            'stop_pct': self.stop_pct, 'target_pct': self.target_pct, 'lots': self.lots,
            'min_dte': self.min_dte, 'max_premium': self.max_premium,
            'entry_window_min': self.entry_window_min, 'sector_group': self.sector_group,
            'trading_days': self.trading_days, 'monitoring_interval': self.monitor_interval,
            'run_immediately': self.run_immediately, 'dry_run': True,
        }

    # ------------------------------------------------------------------ lifecycle
    def initialize(self):
        self._running = True
        from config import get_api_key, get_api_secret, _thread_local
        self._api_key = get_api_key()
        self._api_secret = get_api_secret()
        self._broker = getattr(_thread_local, 'broker', '')
        days = ','.join(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][d] for d in self.trading_days)
        print(f"{TAG} Started [PAPER] | entry {self.entry_hour:02d}:{self.entry_minute:02d} IST, "
              f"square-off {self.exit_hour:02d}:{self.exit_minute:02d} | days {days}")
        print(f"{TAG} Buy {self.lots} lot ATM CE (LONG) / PE (SHORT) of the top {self.sector_group.lower()} "
              f"setup | exit on STOCK: SL -{self.stop_pct}% / TP +{self.target_pct}% | "
              f"min {self.min_dte}d to expiry | max premium Rs {self.max_premium:,.0f}"
              + (" | RUN-NOW (test)" if self.run_immediately else ""))
        if self._broker != 'groww':
            print(f"{TAG} ⚠ Profile broker is '{self._broker}', not 'groww' — option prices need a Groww profile")
        with self._legs_lock:
            open_legs = [l for l in self.legs if not l.get('stopped')]
        if open_legs:
            print(f"{TAG} Resumed with open position: {open_legs[0]['symbol']}")
        return True

    def monitor(self):
        self._apply_thread_context()
        first = True
        while self._running:
            try:
                now = datetime.now(IST)
                if self._open_leg():
                    self._manage_position(now)
                elif self._should_try_entry(now, force=first and self.run_immediately):
                    self._try_entry(now, force=first and self.run_immediately)
                first = False
            except Exception as e:
                logger.error("%s cycle failed: %s", TAG, e)
                print(f"{TAG} ⚠ cycle error: {e}")
            self._interruptible_sleep(self.monitor_interval if self._open_leg() else
                                      self._idle_sleep(datetime.now(IST)))

    def close_all(self):
        """Manual stop: paper-sell any open option at the current LTP."""
        self._running = False
        leg = self._open_leg()
        if leg:
            self._exit(leg, reason='manual_stop')
        self._persist_state()

    @property
    def pnl(self):
        """Realized net P&L + open (gross) P&L from the last marks. No network calls."""
        with self._legs_lock:
            open_pnl = sum(l.get('current_pnl', 0) for l in self.legs if not l.get('stopped'))
        return self.cumulative_pnl + open_pnl

    # ------------------------------------------------------------------ timing
    def _entry_time(self, now):
        return now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)

    def _exit_time(self, now):
        return now.replace(hour=self.exit_hour, minute=self.exit_minute, second=0, microsecond=0)

    def _should_try_entry(self, now, force=False):
        today = now.strftime('%Y-%m-%d')
        if force:
            return True
        if self._entered_day == today or now.weekday() not in self.trading_days:
            return False
        start = self._entry_time(now)
        return start <= now < min(start + timedelta(minutes=self.entry_window_min), self._exit_time(now))

    def _idle_sleep(self, now):
        """Sleep until the next entry check: short inside the entry window, else until it opens."""
        start = self._entry_time(now)
        if now.weekday() in self.trading_days and self._entered_day != now.strftime('%Y-%m-%d') \
                and now < start + timedelta(minutes=self.entry_window_min):
            return max(5, min((start - now).total_seconds(), 60)) if now < start else 60
        return 300

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(5, max(0.1, end - time.time())))

    # ------------------------------------------------------------------ entry
    def _try_entry(self, now, force=False):
        today = now.strftime('%Y-%m-%d')
        from api.sector_scanner import get_sectors, get_sector_stocks
        overview = get_sectors()
        mkt = (overview.get('market') or {}).get('status')
        if mkt != 'Open' and not force:
            print(f"{TAG} {today} market status '{mkt}' — no trade today")
            self._entered_day = today
            self._persist_state()
            return
        cands = [s for s in overview['sectors'] if s.get('group') == self.sector_group and s['bias'] != 'MIXED']
        cands.sort(key=lambda s: abs(s['pchange']), reverse=True)
        print(f"\n{TAG} {now:%Y-%m-%d %H:%M} scan | {len(cands)} BULLISH/BEARISH {self.sector_group.lower()} sectors: "
              + ', '.join(f"{s['index'].replace('NIFTY ', '')} {s['pchange']:+.2f}%" for s in cands[:6]))
        skipped = []
        for sec in cands:
            try:
                d = get_sector_stocks(sec['index'])
            except Exception as e:
                print(f"{TAG}   {sec['index']}: unavailable ({e})")
                continue
            if d['bias'] == 'MIXED':
                skipped.append(f"{sec['index']} now MIXED")
                continue
            for r in d['stocks']:
                # LATE (after 14:30) setups are only accepted for a forced "run immediately" test
                if r['setup'] not in (('LONG', 'SHORT', 'LATE') if force else ('LONG', 'SHORT')):
                    continue
                kind = 'CE' if r['direction'] == 'LONG' else 'PE'
                contract, why = self._atm_contract(r['symbol'], kind)
                if not contract:
                    skipped.append(f"{r['symbol']} ({why})")
                    continue
                self._last_scan = {'time': now.isoformat(), 'sector': d['index'], 'sector_pchg': d['pchange'],
                                   'bias': d['bias'], 'stock': r['symbol'], 'setup': r['direction'],
                                   'rvol': r['rvol'], 'skipped': skipped[-10:]}
                if skipped:
                    print(f"{TAG}   passed over: {', '.join(skipped[-8:])}")
                self._enter(d, r, contract)
                self._entered_day = today
                self._persist_state()
                return
        if skipped:
            print(f"{TAG}   passed over: {', '.join(skipped[-8:])}")
        deadline = self._entry_time(now) + timedelta(minutes=self.entry_window_min)
        if force or now >= deadline - timedelta(seconds=self.monitor_interval * 2):
            print(f"{TAG} No tradeable F&O setup — no trade today")
            self._entered_day = today
            self._last_scan = {'time': now.isoformat(), 'stock': None, 'skipped': skipped[-10:]}
            self._persist_state()
        else:
            print(f"{TAG} No tradeable F&O setup yet — rescanning until {deadline:%H:%M}")

    def _atm_contract(self, symbol, kind):
        """Pick the ATM contract on the nearest expiry with >= min_dte days. Returns (contract, reason)."""
        from api.groww import get_stock_option_meta, get_groww_chain
        meta = get_stock_option_meta(symbol)
        if not meta:
            return None, 'no stock options'
        today = datetime.now(IST).date()
        exps = [e for e in meta['expiries']
                if (datetime.strptime(e, '%Y-%m-%d').date() - today).days >= self.min_dte]
        if not exps:
            return None, 'no expiry'
        expiry = exps[0]
        chain, spot, _ = get_groww_chain(symbol, datetime.strptime(expiry, '%Y-%m-%d').strftime('%d-%m-%Y'))
        if not chain or not spot:
            return None, 'Groww chain unavailable'
        side_key = 'call' if kind == 'CE' else 'put'
        rows = [r for r in chain if r.get(side_key) and float(r[side_key].get('mark_price') or 0) > 0]
        if not rows:
            return None, 'no priced strikes'
        row = min(rows, key=lambda r: abs(float(r['strike']) - spot))
        leg = row[side_key]
        strike = float(row['strike'])
        ltp = float(leg['mark_price'])
        trading_symbol = leg.get('trading_symbol') or meta['contracts'].get((expiry, kind, strike))
        if not trading_symbol:
            return None, 'no trading symbol'
        qty = meta['lot_size'] * self.lots
        if ltp * qty > self.max_premium:
            return None, f'premium Rs {ltp * qty:,.0f} > max'
        return {'trading_symbol': trading_symbol, 'expiry': expiry, 'kind': kind, 'strike': strike,
                'ltp': ltp, 'spot': float(spot), 'lot_size': meta['lot_size'], 'qty': qty,
                'oi': leg.get('oi'), 'iv': leg.get('iv'), 'delta': leg.get('delta')}, None

    def _enter(self, sector, row, c):
        side = 1 if c['kind'] == 'CE' else -1
        stock_entry = c['spot']
        stop = stock_entry * (1 - side * self.stop_pct / 100)
        target = stock_entry * (1 + side * self.target_pct / 100)
        now = datetime.now(IST)
        leg = {
            'symbol': c['trading_symbol'], 'underlying': row['symbol'], 'type': 'call' if side == 1 else 'put',
            'strike': c['strike'], 'expiry': c['expiry'], 'side': 'buy', 'direction': row['direction'],
            'qty': c['qty'], 'size': self.lots, 'lot_size': c['lot_size'],
            'entry_price': c['ltp'], 'current_mark': c['ltp'], 'current_pnl': 0.0,
            'stock_entry': round(stock_entry, 2), 'stock_mark': round(stock_entry, 2),
            'stock_stop': round(stop, 2), 'stock_target': round(target, 2),
            'sector': sector['index'], 'sector_pchg': sector['pchange'],
            'opened_at': now.isoformat(), 'stopped': False,
        }
        with self._legs_lock:
            self.legs = [l for l in self.legs if not l.get('stopped')] + [leg]
        self.total_trades += 1
        self.total_days_traded += 1
        self.trade_log.append({
            'ts': now.isoformat(), 'date': now.strftime('%Y-%m-%d'), 'action': 'BUY',
            'symbol': c['trading_symbol'], 'underlying': row['symbol'], 'qty': c['qty'],
            'price': round(c['ltp'], 2), 'stock_price': round(stock_entry, 2), 'sector': sector['index'],
            'reason': f"{row['direction']} setup" + (' (LATE, test)' if row['setup'] == 'LATE' else ''),
        })
        print(f"{TAG} [PAPER] BUY {c['trading_symbol']} x{c['qty']} ({self.lots} lot) @ Rs {c['ltp']:.2f} "
              f"= Rs {c['ltp'] * c['qty']:,.0f} | {row['direction']} {row['symbol']} @ {stock_entry:.2f} "
              f"in {sector['index']} {sector['pchange']:+.2f}% | RVOL {row['rvol']}")
        print(f"{TAG}   stock SL {stop:.2f} ({-self.stop_pct:+.2f}%) / TP {target:.2f} (+{self.target_pct:.2f}%) "
              f"| expiry {c['expiry']} | IV {c['iv']} | delta {c['delta']}")

    # ------------------------------------------------------------------ position management
    def _open_leg(self):
        with self._legs_lock:
            return next((l for l in self.legs if not l.get('stopped')), None)

    def _marks(self, leg):
        from api.groww import get_spot_price, get_option_ltp
        spot = get_spot_price(leg['underlying'])
        opt = get_option_ltp(leg['symbol'])
        return (float(spot) if spot else None), (float(opt) if opt else None)

    def _manage_position(self, now):
        leg = self._open_leg()
        spot, opt = self._marks(leg)
        if opt:
            leg['current_mark'] = round(opt, 2)
            leg['current_pnl'] = round((opt - leg['entry_price']) * leg['qty'], 2)
        if spot:
            leg['stock_mark'] = round(spot, 2)
        if spot and opt:
            self._price_failures = 0
        else:
            self._price_failures += 1
            if self._price_failures in (3, MAX_PRICE_FAILURES):
                print(f"{TAG} ⚠ price poll failed {self._price_failures}x (stock={spot}, option={opt})")
        self._pnl_history.append((now.isoformat(), round(self.pnl, 2)))
        self._pnl_history = self._pnl_history[-500:]

        if now >= self._exit_time(now) or now.date().isoformat() > leg['opened_at'][:10]:
            self._exit(leg, reason='square_off', opt=opt)
            return
        if not spot:
            return
        long_ = leg['type'] == 'call'
        if (spot <= leg['stock_stop']) if long_ else (spot >= leg['stock_stop']):
            self._exit(leg, reason='stop_loss', opt=opt)
        elif (spot >= leg['stock_target']) if long_ else (spot <= leg['stock_target']):
            self._exit(leg, reason='take_profit', opt=opt)

    def _exit(self, leg, reason, opt=None):
        if opt is None:
            try:
                _, opt = self._marks(leg)
            except Exception:
                opt = None
        px = opt or leg.get('current_mark') or leg['entry_price']
        gross = (px - leg['entry_price']) * leg['qty']
        fees = estimate_charges(leg['entry_price'] * leg['qty'], px * leg['qty'])
        net = gross - fees
        now = datetime.now(IST)
        with self._legs_lock:
            leg.update(stopped=True, exit_price=round(px, 2), current_mark=round(px, 2),
                       current_pnl=0.0, exit_reason=reason)
            self.cumulative_pnl += net
            self.total_charges += fees
        move = ((leg.get('stock_mark') or leg['stock_entry']) / leg['stock_entry'] - 1) * 100
        self.trade_log.append({
            'ts': now.isoformat(), 'date': now.strftime('%Y-%m-%d'), 'action': 'SELL',
            'symbol': leg['symbol'], 'underlying': leg['underlying'], 'qty': leg['qty'],
            'price': round(px, 2), 'stock_price': leg.get('stock_mark'), 'gross': round(gross, 2),
            'charges': fees, 'pnl': round(net, 2), 'reason': reason,
        })
        print(f"{TAG} [PAPER] SELL {leg['symbol']} x{leg['qty']} @ Rs {px:.2f} | {reason} | "
              f"stock {leg['underlying']} {leg.get('stock_mark')} ({move:+.2f}%) | gross Rs {gross:+,.0f} "
              f"- charges Rs {fees:,.0f} = Rs {net:+,.0f} | cumulative Rs {self.cumulative_pnl:+,.0f}")
        self._persist_state()

    # ------------------------------------------------------------------ persistence / threading
    def _apply_thread_context(self):
        from config import set_thread_credentials
        if self._api_key:
            set_thread_credentials(self._api_key, self._api_secret, self._broker)
        try:
            if self._log_queue is not None:
                from app import LogCapture
                LogCapture._local.log_queue = self._log_queue
                LogCapture._local.log_history = self._log_history
        except Exception:
            pass

    def _persist_state(self):
        if not self._sid:
            return
        try:
            from models import update_strategy_db
            with self._legs_lock:
                legs = [dict(l) for l in self.legs if not l.get('stopped')]
            details = {**self._base_params, 'trade_log': self.trade_log[-200:],
                       'cumulative_pnl': self.cumulative_pnl, 'total_charges': self.total_charges,
                       'total_trades': self.total_trades, 'total_days_traded': self.total_days_traded,
                       'entered_day': self._entered_day, 'last_scan': self._last_scan}
            update_strategy_db(self._sid, details=details, legs=legs, pnl=round(self.pnl, 2))
        except Exception as e:
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
        """Snapshot for the status endpoint / UI."""
        with self._legs_lock:
            legs = [dict(l) for l in self.legs if not l.get('stopped')]
        return {
            'cumulative_pnl': round(self.cumulative_pnl, 2), 'total_pnl': round(self.pnl, 2),
            'total_charges': round(self.total_charges, 2), 'total_trades': self.total_trades,
            'days_traded': self.total_days_traded, 'trade_log': self.trade_log[-20:],
            'legs': legs, 'last_scan': self._last_scan, 'entered_day': self._entered_day,
            'params': self._base_params,
        }
