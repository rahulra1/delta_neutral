"""EMA Credit Spread V2 — Daily recurring strategy with two-tier partial exits.

Identical entry logic to EMACreditSpread, but with a two-tranche TP/SL ladder.

Every day at 6:30 PM IST:
1. Check 1D candles, compute EMA14
2. If price < EMA14 → bearish → sell 20Δ call, buy 10Δ call (bear call spread)
3. If price > EMA14 → bullish → sell 20Δ put, buy 10Δ put (bull put spread)
4. Expiry: nearest available ≥8 days out
5. Position is split into TWO equal tranches (50/50), each with its own premium
   basis of P/2. Two-stage exit management:

   Tier 1 (both halves open) — thresholds measured against the FULL premium P:
     • TP1 (default 50%)  → position PnL at +0.50·P → close tranche 1 (half)
     • SL1 (default 90%)  → position PnL at -0.90·P → close tranche 1 (half)

   Tier 2 (remaining half) — after EITHER tier-1 event the remaining half is
   managed identically, with thresholds measured against the HALF's own basis
   (P/2), because a half-position's PnL can never reach the full-P levels:
     • TP2 (default 90%)  → remaining half at +0.90·(P/2) = +0.45·P → full exit
     • SL2 (default 150%) → remaining half at -1.50·(P/2) = -0.75·P → full exit
     (After SL1, TP2 serves as a recovery exit.)

   Worked example, P=$20, 50/50 split (each half basis = $10):
     • TP1→TP2:  +$5 + $9  = +$14   (best)
     • TP1→SL2:  +$5 - $15 = -$10
     • SL1→TP2:  -$9 + $9  =  $0    (recovery)
     • SL1→SL2:  -$9 - $15 = -$24   (worst)
6. After exit, waits for next day 6:30 PM.
"""

import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from api.chart import get_candles, calc_ema
from api.chain import get_expiries
from api.option_chain import get_option_chain
from api.delta_finder import find_target_delta_options
from api.orders import place_order
from api.pricing import get_current_price
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

ENTRY_HOUR = 18
ENTRY_MINUTE = 30
SELL_DELTA = 0.20
BUY_DELTA = 0.10
EMA_PERIOD = 14
TP1_PCT = 0.50
TP2_PCT = 0.90
SL1_PCT = 0.90
SL2_PCT = 1.50
LOT_SIZE = 100
MONITOR_INTERVAL = 5
MIN_EXPIRY_DAYS = 8


class EMACreditSpreadV2(BaseStrategy):
    """Daily EMA-based credit spread with two-tier (TP1/TP2/SL1/SL2) partial
    exits. Start once, trades every day at 6:30 PM IST."""

    def __init__(self, asset='BTC', lot_size=LOT_SIZE, sell_delta=SELL_DELTA,
                 buy_delta=BUY_DELTA, ema_period=EMA_PERIOD,
                 tp1_pct=TP1_PCT, tp2_pct=TP2_PCT, sl1_pct=SL1_PCT, sl2_pct=SL2_PCT,
                 monitor_interval=MONITOR_INTERVAL,
                 entry_hour=ENTRY_HOUR, entry_minute=ENTRY_MINUTE,
                 min_expiry_days=MIN_EXPIRY_DAYS):
        self.asset = asset
        self.lot_size = lot_size
        self.sell_delta = sell_delta
        self.buy_delta = buy_delta
        self.ema_period = ema_period
        self.tp1_pct = tp1_pct
        self.tp2_pct = tp2_pct
        self.sl1_pct = sl1_pct
        self.sl2_pct = sl2_pct
        self.monitor_interval = monitor_interval
        self.entry_hour = entry_hour
        self.entry_minute = entry_minute
        self.min_expiry_days = min_expiry_days

        self._running = False
        self.legs = []  # [{symbol, product_id, side, type, delta, strike, entry_price, size}]
        self.net_premium = 0.0
        self._pnl = 0.0
        self.total_days_traded = 0
        self.cumulative_pnl = 0.0
        self.trade_log = []
        self._sid = None  # Set externally after creation for DB persistence
        self._pnl_history = []            # [(iso_ts, pnl), ...] for UI chart
        self._legs_lock = threading.Lock()  # protects self.legs mutations
        self._snap_counter = 0
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10
        self._base_params = {
            'asset': asset, 'lot_size': lot_size, 'sell_delta': sell_delta,
            'buy_delta': buy_delta, 'ema_period': ema_period,
            'tp1_pct': int(tp1_pct * 100), 'tp2_pct': int(tp2_pct * 100),
            'sl1_pct': int(sl1_pct * 100), 'sl2_pct': int(sl2_pct * 100),
            'monitoring_interval': monitor_interval,
            'entry_hour': entry_hour, 'entry_minute': entry_minute,
            'min_expiry_days': min_expiry_days,
        }

    def initialize(self):
        self._running = True
        print(f"[EMA Spread V2] Started | {self.entry_hour}:{self.entry_minute:02d} IST daily")
        print(f"[EMA Spread V2] EMA{self.ema_period} | Sell {self.sell_delta}Δ / Buy {self.buy_delta}Δ | "
              f"TP1: {self.tp1_pct*100:.0f}% TP2: {self.tp2_pct*100:.0f}% | "
              f"SL1: {self.sl1_pct*100:.0f}% SL2: {self.sl2_pct*100:.0f}%")
        return True

    def monitor(self):
        """Main daily loop — spawns a new monitored trade each day."""
        import threading
        while self._running:
            self._wait_for_next_entry()
            if not self._running:
                break

            self.total_days_traded += 1
            day_num = self.total_days_traded
            tag = f"[EMA V2 Day{day_num}]"

            print(f"\n{tag} ═══ {datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST ═══")
            day_legs, day_premium, direction = self._open_daily_trade(tag, day_num)
            if not day_legs:
                print(f"{tag} No trade today")
                continue

            t = threading.Thread(target=self._monitor_day_trade,
                                 args=(day_legs, day_premium, day_num, direction), daemon=True)
            t.start()

    def close_all(self):
        self._running = False
        with self._legs_lock:
            legs_copy = list(self.legs)

        # Only remove a leg once its close order SUCCEEDS. Retry a few times for
        # transient API errors; any leg that still won't close is KEPT in
        # self.legs (and persisted) so it is never silently dropped.
        max_close_attempts = 3
        for attempt in range(1, max_close_attempts + 1):
            if not legs_copy:
                break
            remaining = []
            for leg in legs_copy:
                close_side = 'buy' if leg['side'] == 'sell' else 'sell'
                try:
                    result = place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
                except Exception as e:
                    result = None
                    logger.warning(f"[EMA Spread V2] Failed to close leg {leg.get('symbol')}: {e}")
                if result:
                    with self._legs_lock:
                        if leg in self.legs:
                            self.legs.remove(leg)
                else:
                    remaining.append(leg)
            legs_copy = remaining
            if legs_copy and attempt < max_close_attempts:
                logger.warning(f"[EMA Spread V2] {len(legs_copy)} leg(s) failed to close "
                               f"(attempt {attempt}/{max_close_attempts}) — retrying")
                time.sleep(2 ** attempt)

        if legs_copy:
            logger.error(f"[EMA Spread V2] ✗ {len(legs_copy)} leg(s) could NOT be closed after "
                         f"{max_close_attempts} attempts — leaving them under tracking: "
                         f"{[l.get('symbol') for l in legs_copy]}")
        try:
            self._persist_state()
        except Exception:
            pass

    @property
    def pnl(self):
        from config import get_contract_value
        cv = get_contract_value(self.asset)
        open_pnl = 0.0
        for leg in self.legs:
            data = get_current_price(leg['product_id'], self.asset)
            if data:
                if leg['side'] == 'sell':
                    open_pnl += (leg['entry_price'] - data['mark_price']) * leg['size'] * cv
                else:
                    open_pnl += (data['mark_price'] - leg['entry_price']) * leg['size'] * cv
        return self.cumulative_pnl + open_pnl

    # --- Daily trade ---

    def _open_daily_trade(self, tag, day_num):
        """Check EMA, place spread. Returns (legs, premium, direction) or ([], 0, '')."""
        candles = get_candles(self.asset, '1d')
        if not candles or len(candles) < self.ema_period + 1:
            print(f"{tag} ✗ Not enough candle data")
            return [], 0, ''

        ema_values = calc_ema(candles, self.ema_period)
        if not ema_values:
            return [], 0, ''

        current_price = candles[-1]['c']
        ema_current = ema_values[-1]['value']
        bearish = current_price < ema_current
        direction = 'bear_call' if bearish else 'bull_put'
        print(f"{tag} Price: {current_price} | EMA{self.ema_period}: {ema_current} | {'BEARISH' if bearish else 'BULLISH'}")

        expiries = get_expiries(self.asset, min_days=self.min_expiry_days)
        if not expiries:
            return [], 0, ''
        expiry = expiries[0]
        print(f"{tag} Expiry: {expiry}")

        chain = get_option_chain(expiry, self.asset)
        if not chain:
            return [], 0, ''

        sell_call, sell_put = find_target_delta_options(chain, self.sell_delta, 0.05)
        buy_call, buy_put = find_target_delta_options(chain, self.buy_delta, 0.05)

        if bearish:
            sell_leg, buy_leg, opt_type = sell_call, buy_call, 'call'
        else:
            sell_leg, buy_leg, opt_type = sell_put, buy_put, 'put'

        if not sell_leg or not buy_leg or sell_leg['product_id'] == buy_leg['product_id']:
            return [], 0, ''

        sell_result = place_order(sell_leg['product_id'], sell_leg['symbol'], self.lot_size, 'sell')
        if not sell_result:
            return [], 0, ''

        buy_result = place_order(buy_leg['product_id'], buy_leg['symbol'], self.lot_size, 'buy')
        if not buy_result:
            place_order(sell_leg['product_id'], sell_leg['symbol'], self.lot_size, 'buy')
            return [], 0, ''

        day_legs = [
            {'symbol': sell_leg['symbol'], 'product_id': sell_leg['product_id'],
             'side': 'sell', 'type': opt_type, 'delta': sell_leg['delta'],
             'strike': sell_leg['strike_price'], 'entry_price': sell_leg['mark_price'],
             'size': self.lot_size, 'day_num': day_num,
             'opened_at': datetime.now(IST).strftime('%Y-%m-%d')},
            {'symbol': buy_leg['symbol'], 'product_id': buy_leg['product_id'],
             'side': 'buy', 'type': opt_type, 'delta': buy_leg['delta'],
             'strike': buy_leg['strike_price'], 'entry_price': buy_leg['mark_price'],
             'size': self.lot_size, 'day_num': day_num,
             'opened_at': datetime.now(IST).strftime('%Y-%m-%d')},
        ]

        from config import get_contract_value
        cv = get_contract_value(self.asset)
        premium = (sell_leg['mark_price'] - buy_leg['mark_price']) * self.lot_size * cv

        print(f"{tag} ✓ SELL {opt_type.upper()} {sell_leg['strike_price']} (Δ{sell_leg['delta']:.2f}) @ {sell_leg['mark_price']}")
        print(f"{tag} ✓ BUY  {opt_type.upper()} {buy_leg['strike_price']} (Δ{buy_leg['delta']:.2f}) @ {buy_leg['mark_price']}")
        print(f"{tag} Net premium: ${premium:.4f} | "
              f"TP1: ${premium*self.tp1_pct:.4f} TP2: ${premium*self.tp2_pct:.4f} | "
              f"SL1: -${premium*self.sl1_pct:.4f} SL2: -${premium*self.sl2_pct:.4f}")

        with self._legs_lock:
            self.legs.extend(day_legs)
        self._persist_state()
        return day_legs, premium, direction

    @staticmethod
    def _split_sizes(total):
        """Split a position size into two tranches as evenly as possible.
        Tranche 1 gets the floor-half, tranche 2 the remainder so the two always
        sum back to `total` even for odd sizes."""
        t1 = total // 2
        t2 = total - t1
        return t1, t2

    def _close_partial(self, day_legs, fraction_sizes, label):
        """Close `fraction_sizes[leg_id]` contracts of each leg (a partial exit).
        Reduces each leg's tracked size by the amount actually closed. A leg's
        size is only reduced once its (partial) close order SUCCEEDS. Returns the
        list of leg symbols that failed to close so the caller can retry.
        `fraction_sizes` maps id(leg) → number of contracts to close."""
        failed = []
        for leg in list(day_legs):
            qty = fraction_sizes.get(id(leg), 0)
            if qty <= 0:
                continue
            close_side = 'buy' if leg['side'] == 'sell' else 'sell'
            try:
                result = place_order(leg['product_id'], leg['symbol'], qty, close_side)
            except Exception as e:
                result = None
                logger.warning(f"[EMA Spread V2] Partial close raised for {leg.get('symbol')}: {e}")

            if result:
                with self._legs_lock:
                    leg['size'] = max(0, leg['size'] - qty)
                    # Drop fully-closed legs from monitoring
                    if leg['size'] == 0 and leg in self.legs:
                        self.legs.remove(leg)
                if leg['size'] == 0 and leg in day_legs:
                    day_legs.remove(leg)
            else:
                logger.warning(f"[EMA Spread V2] ✗ Partial close FAILED for {leg.get('symbol')} ({label}) "
                               f"— keeping under monitoring")
                failed.append(leg)
        return failed

    def _monitor_day_trade(self, day_legs, premium, day_num, direction):
        """Monitor a single day's spread in its own thread with a two-tier ladder.

        State machine:
          • Both tranches open → watch TP1 (+50% of P) / SL1 (-90% of P).
          • After TP1 or SL1 → the remaining half is managed identically: it
            exits on TP2 (+90% of the half's own basis) or SL2 (-150% of the
            half's basis). After SL1, TP2 acts as a recovery exit.
        """
        from config import get_contract_value, set_thread_credentials
        # Set thread-local credentials for API calls
        if hasattr(self, '_api_key') and self._api_key:
            set_thread_credentials(self._api_key, self._api_secret, self._broker)
        # Route logs to the strategy's log queue
        if hasattr(self, '_log_queue') and self._log_queue:
            from app import LogCapture
            LogCapture._local.log_queue = self._log_queue
            LogCapture._local.log_history = self._log_history
        cv = get_contract_value(self.asset)
        tp1 = premium * self.tp1_pct
        tp2 = premium * self.tp2_pct
        sl1 = premium * self.sl1_pct
        sl2 = premium * self.sl2_pct
        cycle = 0

        # Tranche sizing: record how many contracts tranche 1 represents per leg.
        tranche1_qty = {id(leg): self._split_sizes(leg['size'])[0] for leg in day_legs}
        tier1_done = False      # True once tranche 1 has been closed (TP1 or SL1)
        realized_tier1 = 0.0    # PnL booked from the tranche-1 partial close

        # Compute day label once (includes opened date if available)
        opened_date = ''
        for leg in day_legs:
            if leg.get('opened_at'):
                opened_date = leg['opened_at']
                break
        day_label = f"[EMA V2 Day{day_num} ({opened_date})]" if opened_date else f"[EMA V2 Day{day_num}]"

        while self._running:
            time.sleep(self.monitor_interval)
            cycle += 1

            pnl = 0.0
            leg_details = []
            all_legs_ok = True
            for leg in day_legs:
                data = get_current_price(leg['product_id'], self.asset)
                if not data:
                    all_legs_ok = False
                    leg_details.append(f"{leg.get('symbol', '?')}: no data")
                    continue
                mark = data['mark_price']
                if leg['side'] == 'sell':
                    leg_pnl = (leg['entry_price'] - mark) * leg['size'] * cv
                else:
                    leg_pnl = (mark - leg['entry_price']) * leg['size'] * cv
                pnl += leg_pnl
                # Enrich leg dict with live data for UI
                leg['current_mark'] = round(mark, 4)
                leg['current_pnl'] = round(leg_pnl, 4)
                leg_details.append(f"{leg['side'].upper()} {leg['strike']}: ${leg_pnl:+.4f}")

            # `pnl` above is the open PnL of whatever size is STILL open. Total
            # trade PnL also includes anything already booked from tranche 1.
            total_pnl = pnl + realized_tier1

            # Handle consecutive failures
            if not all_legs_ok:
                self._consecutive_failures += 1
                print(f"{day_label} ⚠ Price fetch failed ({self._consecutive_failures}/{self._max_consecutive_failures})")
                if self._consecutive_failures >= self._max_consecutive_failures:
                    print(f"{day_label} 🚨 EMERGENCY: {self._consecutive_failures} consecutive failures — closing legs")
                    still_open = self._close_day_legs(day_legs)
                    if still_open:
                        print(f"{day_label} ⚠ {len(still_open)} leg(s) still open after close attempt — "
                              f"continuing to monitor and retry")
                        continue
                    self._record_day(day_num, total_pnl, premium, 'api_failure', direction)
                    return
                continue
            self._consecutive_failures = 0

            # Track PnL history for UI chart
            now_iso = datetime.now(IST).isoformat()
            self._pnl_history.append((now_iso, round(self.cumulative_pnl + total_pnl, 4)))
            if len(self._pnl_history) > 500:
                self._pnl_history = self._pnl_history[-500:]

            # Save PnL snapshot to DB every 6 ticks
            self._snap_counter += 1
            if self._snap_counter % 6 == 0 and self._sid:
                try:
                    from models import save_pnl_snapshot
                    user_id = getattr(self, '_user_id', None)
                    if not user_id:
                        try:
                            from app import ema_spread_v2_strategies
                            for s_id, entry in ema_spread_v2_strategies.items():
                                if entry.get('strategy') is self:
                                    user_id = entry.get('user_id')
                                    self._user_id = user_id
                                    break
                        except Exception:
                            pass
                    if user_id:
                        save_pnl_snapshot(user_id, self._sid, round(self.cumulative_pnl + total_pnl, 4))
                except Exception:
                    pass

            legs_str = ' | '.join(leg_details) if leg_details else ''
            pct = (total_pnl / premium * 100) if premium else 0
            stage = 'T2' if tier1_done else 'T1'
            # Expose live values for the status route / UI tiles
            self._pnl = round(total_pnl, 4)
            self.net_premium = round(premium, 4)
            print(f"{day_label} [{stage}] PnL: ${total_pnl:+.4f} ({pct:+.1f}%) | "
                  f"Cum: ${self.cumulative_pnl:+.4f} | {legs_str}")

            # ── Tier 1: both tranches open, watch TP1 / SL1 ──
            if not tier1_done:
                if pnl >= tp1:
                    print(f"{day_label} 🎯 TP1 hit: ${pnl:.4f} — closing tranche 1 (half)")
                    failed = self._close_partial(day_legs, tranche1_qty, 'TP1')
                    if failed:
                        print(f"{day_label} ⚠ TP1 partial close incomplete — retrying next cycle")
                        continue
                    # Book the realized PnL on the half just closed (half of
                    # current open pnl, since the two tranches shared the mark).
                    realized_tier1 = pnl * 0.5
                    tier1_done = True
                    self._persist_state()
                    continue
                if pnl <= -sl1:
                    print(f"{day_label} 🛑 SL1 hit: ${pnl:.4f} — closing tranche 1 (half)")
                    failed = self._close_partial(day_legs, tranche1_qty, 'SL1')
                    if failed:
                        print(f"{day_label} ⚠ SL1 partial close incomplete — retrying next cycle")
                        continue
                    realized_tier1 = pnl * 0.5
                    tier1_done = True
                    self._persist_state()
                    continue
                continue

            # ── Tier 2: tranche 1 already booked, manage the remaining half ──
            # After EITHER tier-1 event (TP1 or SL1) the remaining half is managed
            # identically: it exits on TP2 (upside) or SL2 (downside). Thresholds
            # are measured against the HALF's own premium basis (premium*0.5):
            #   TP2 = 90% of the half  = tp2 * 0.5   (= premium * 0.45)
            #   SL2 = 150% of the half = sl2 * 0.5   (= premium * 0.75)
            # `pnl` is the remaining half's open PnL; `total_pnl` includes the
            # realized tier-1 amount.
            tp2_half = tp2 * 0.5
            sl2_half = sl2 * 0.5

            if pnl >= tp2_half:
                print(f"{day_label} 🎯 TP2 hit: ${total_pnl:.4f} total — closing remaining half")
                still_open = self._close_day_legs(day_legs)
                if still_open:
                    print(f"{day_label} ⚠ TP2 close incomplete — {len(still_open)} leg(s) still open; "
                          f"keeping under monitoring and retrying")
                    continue
                self._record_day(day_num, total_pnl, premium, 'target', direction)
                return

            if pnl <= -sl2_half:
                print(f"{day_label} 🛑 SL2 hit: ${total_pnl:.4f} total — closing remaining half")
                still_open = self._close_day_legs(day_legs)
                if still_open:
                    print(f"{day_label} ⚠ SL2 close incomplete — {len(still_open)} leg(s) still open; "
                          f"keeping under monitoring and retrying")
                    continue
                self._record_day(day_num, total_pnl, premium, 'stoploss', direction)
                return

    def _close_day_legs(self, day_legs):
        """Attempt to close each leg (full remaining size). A leg is only removed
        from monitoring (self.legs and the day_legs list) once its close order
        SUCCEEDS. Returns the list of legs that failed to close so the caller can
        keep monitoring/retrying them."""
        still_open = []
        for leg in list(day_legs):
            close_side = 'buy' if leg['side'] == 'sell' else 'sell'
            try:
                result = place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
            except Exception as e:
                result = None
                logger.warning(f"[EMA Spread V2] Close order raised for {leg.get('symbol')}: {e}")

            if result:
                with self._legs_lock:
                    if leg in self.legs:
                        self.legs.remove(leg)
                if leg in day_legs:
                    day_legs.remove(leg)
            else:
                logger.warning(f"[EMA Spread V2] ✗ Close FAILED for {leg.get('symbol')} — keeping under monitoring")
                still_open.append(leg)
        return still_open

    def _record_day(self, day_num, pnl, premium, exit_reason, direction):
        self.cumulative_pnl += pnl
        self.trade_log.append({
            'date': datetime.now(IST).strftime('%Y-%m-%d'),
            'day': day_num,
            'pnl': round(pnl, 4),
            'premium': round(premium, 4),
            'exit_reason': exit_reason,
            'direction': direction,
        })
        print(f"[EMA V2 D{day_num}] Closed | PnL: ${pnl:+.4f} | Cumulative: ${self.cumulative_pnl:+.4f}")
        # Persist state to DB so it survives server restarts
        self._persist_state()

    # --- Persistence ---

    def _persist_state(self):
        """Save trade_log, cumulative_pnl, total_days_traded, and legs to DB.
        This ensures data survives server restarts."""
        try:
            from models import update_strategy_db
            import json
            sid = getattr(self, '_sid', None)
            if not sid:
                try:
                    from app import ema_spread_v2_strategies
                    for s_id, entry in ema_spread_v2_strategies.items():
                        if entry.get('strategy') is self:
                            sid = s_id
                            self._sid = sid
                            break
                except Exception:
                    pass
            if not sid:
                return
            base_params = getattr(self, '_base_params', {
                'asset': self.asset, 'lot_size': self.lot_size,
                'sell_delta': self.sell_delta, 'buy_delta': self.buy_delta,
                'ema_period': self.ema_period,
                'tp1_pct': int(self.tp1_pct * 100), 'tp2_pct': int(self.tp2_pct * 100),
                'sl1_pct': int(self.sl1_pct * 100), 'sl2_pct': int(self.sl2_pct * 100),
                'monitoring_interval': self.monitor_interval,
                'entry_hour': self.entry_hour, 'entry_minute': self.entry_minute,
                'min_expiry_days': self.min_expiry_days,
            })
            details = {**base_params,
                       'trade_log': self.trade_log,
                       'cumulative_pnl': self.cumulative_pnl,
                       'total_days_traded': self.total_days_traded}
            legs_data = []
            for leg in self.legs:
                legs_data.append({
                    'symbol': leg.get('symbol', ''),
                    'product_id': leg.get('product_id'),
                    'side': leg.get('side', ''),
                    'type': leg.get('type', ''),
                    'delta': leg.get('delta', 0),
                    'strike': leg.get('strike', 0),
                    'entry_price': leg.get('entry_price', 0),
                    'size': leg.get('size', 0),
                    'day_num': leg.get('day_num', 0),
                    'opened_at': leg.get('opened_at', ''),
                })
            update_strategy_db(sid,
                               details=details,
                               legs=legs_data,
                               pnl=round(self.cumulative_pnl, 4))
            logger.debug(f"[EMA Spread V2] State persisted: {self.total_days_traded} days, ${self.cumulative_pnl:.4f}")
        except Exception as e:
            logger.warning(f"[EMA Spread V2] Failed to persist state: {e}")

    # --- Timing ---

    def _wait_for_next_entry(self):
        now = datetime.now(IST)
        entry_today = now.replace(hour=self.entry_hour, minute=self.entry_minute, second=0, microsecond=0)
        if now < entry_today:
            target = entry_today
        else:
            target = entry_today + timedelta(days=1)
        wait = (target - now).total_seconds()
        if wait > 60:
            print(f"[EMA Spread V2] Next trade at {target.strftime('%Y-%m-%d %H:%M')} IST ({wait/3600:.1f}h)")
        self._interruptible_sleep(wait)

    def _interruptible_sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(30, end - time.time()))
