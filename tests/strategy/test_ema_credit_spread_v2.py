"""Tests for EMACreditSpreadV2 — two-tier (TP1/TP2/SL1/SL2) partial-exit logic
and close safety.

Two independent concerns are covered:

1. Close safety (mirrors test_ema_credit_spread_close.py): a leg must NOT be
   removed from monitoring until its close order actually SUCCEEDS.

2. Two-tier ladder math: with net premium P split 50/50, closing a tranche at
   X% of P books 0.5 * X% * P. For P=$20 that means TP1→+$5, TP2→+$9 (best
   +$14) and SL1→-$9, SL2→-$15 (worst -$24).
"""
from unittest.mock import patch

import pytest

from strategy.ema_credit_spread_v2 import EMACreditSpreadV2


def _leg(symbol, product_id, side='sell', size=100):
    return {
        'symbol': symbol, 'product_id': product_id, 'side': side,
        'type': 'put', 'delta': 0.20, 'strike': 90000,
        'entry_price': 450.0, 'size': size, 'day_num': 1,
        'opened_at': '2026-10-07',
    }


def _make_strategy(**kwargs):
    s = EMACreditSpreadV2(asset='BTC', lot_size=100, **kwargs)
    # Avoid DB/app persistence side effects during unit tests
    s._persist_state = lambda: None
    return s


# ─────────────────────────── _split_sizes ───────────────────────────

class TestSplitSizes:

    def test_even_split(self):
        assert EMACreditSpreadV2._split_sizes(100) == (50, 50)

    def test_odd_split_sums_to_total(self):
        t1, t2 = EMACreditSpreadV2._split_sizes(101)
        assert (t1, t2) == (50, 51)
        assert t1 + t2 == 101

    def test_tiny_sizes(self):
        assert EMACreditSpreadV2._split_sizes(1) == (0, 1)
        assert EMACreditSpreadV2._split_sizes(2) == (1, 1)


# ─────────────────────── default ladder params ──────────────────────

class TestDefaults:

    def test_default_tier_percentages(self):
        s = _make_strategy()
        assert s.tp1_pct == 0.50
        assert s.tp2_pct == 0.90
        assert s.sl1_pct == 0.90
        assert s.sl2_pct == 1.50

    def test_base_params_serialized_as_ints(self):
        s = _make_strategy()
        assert s._base_params['tp1_pct'] == 50
        assert s._base_params['tp2_pct'] == 90
        assert s._base_params['sl1_pct'] == 90
        assert s._base_params['sl2_pct'] == 150


# ─────────────────────── _close_partial behavior ────────────────────

class TestClosePartial:

    def test_partial_close_reduces_size_not_removes(self):
        """Closing tranche 1 reduces a leg's size but keeps it tracked while a
        remaining size is open."""
        s = _make_strategy()
        legs = [_leg('BTC-P-90000', 1002, 'sell', size=100)]
        s.legs = list(legs)
        qty = {id(legs[0]): 50}

        with patch('strategy.ema_credit_spread_v2.place_order', return_value={'id': 'ok'}):
            failed = s._close_partial(legs, qty, 'TP1')

        assert failed == []
        assert legs[0]['size'] == 50          # halved
        assert legs[0] in s.legs              # still tracked

    def test_partial_close_full_removes_leg(self):
        s = _make_strategy()
        legs = [_leg('BTC-P-90000', 1002, 'sell', size=100)]
        s.legs = list(legs)
        qty = {id(legs[0]): 100}              # close the whole thing

        with patch('strategy.ema_credit_spread_v2.place_order', return_value={'id': 'ok'}):
            failed = s._close_partial(legs, qty, 'TP2')

        assert failed == []
        assert s.legs == []
        assert legs == []

    def test_failed_partial_keeps_size(self):
        s = _make_strategy()
        legs = [_leg('BTC-P-90000', 1002, 'sell', size=100)]
        s.legs = list(legs)
        qty = {id(legs[0]): 50}

        with patch('strategy.ema_credit_spread_v2.place_order', return_value=None):
            failed = s._close_partial(legs, qty, 'TP1')

        assert failed == [legs[0]]
        assert legs[0]['size'] == 100         # unchanged — close did not succeed
        assert legs[0] in s.legs


# ───────────────────── _close_day_legs close safety ─────────────────

class TestCloseDayLegsKeepsUntilClosed:

    def test_all_closes_succeed_removes_all_legs(self):
        s = _make_strategy()
        legs = [_leg('BTC-P-90000', 1002, 'sell'), _leg('BTC-P-80000', 1003, 'buy')]
        s.legs = list(legs)

        with patch('strategy.ema_credit_spread_v2.place_order', return_value={'id': 'ok'}):
            still_open = s._close_day_legs(legs)

        assert still_open == []
        assert s.legs == []
        assert legs == []

    def test_failed_close_keeps_leg_under_monitoring(self):
        s = _make_strategy()
        good = _leg('BTC-P-90000', 1002, 'sell')
        bad = _leg('BTC-P-80000', 1003, 'buy')
        legs = [good, bad]
        s.legs = list(legs)

        def _side_effect(product_id, symbol, size, side):
            return {'id': 'ok'} if product_id == 1002 else None

        with patch('strategy.ema_credit_spread_v2.place_order', side_effect=_side_effect):
            still_open = s._close_day_legs(legs)

        assert still_open == [bad]
        assert good not in s.legs
        assert bad in s.legs
        assert legs == [bad]

    def test_exception_during_close_keeps_leg(self):
        s = _make_strategy()
        leg = _leg('BTC-P-90000', 1002, 'sell')
        legs = [leg]
        s.legs = list(legs)

        with patch('strategy.ema_credit_spread_v2.place_order', side_effect=RuntimeError('api down')):
            still_open = s._close_day_legs(legs)

        assert still_open == [leg]
        assert leg in s.legs


# ────────────────────── close_all close safety ──────────────────────

class TestCloseAllKeepsUntilClosed:

    def test_close_all_success_clears_everything(self):
        s = _make_strategy()
        s.legs = [_leg('BTC-P-90000', 1002, 'sell'), _leg('BTC-P-80000', 1003, 'buy')]

        with patch('strategy.ema_credit_spread_v2.place_order', return_value={'id': 'ok'}):
            s.close_all()

        assert s.legs == []
        assert s._running is False

    def test_close_all_retries_then_succeeds(self):
        s = _make_strategy()
        leg = _leg('BTC-P-90000', 1002, 'sell')
        s.legs = [leg]
        calls = {'n': 0}

        def _flaky(product_id, symbol, size, side):
            calls['n'] += 1
            return None if calls['n'] == 1 else {'id': 'ok'}

        with patch('strategy.ema_credit_spread_v2.place_order', side_effect=_flaky), \
             patch('strategy.ema_credit_spread_v2.time.sleep'):
            s.close_all()

        assert s.legs == []
        assert calls['n'] >= 2

    def test_close_all_permanent_failure_keeps_legs(self):
        s = _make_strategy()
        leg = _leg('BTC-P-90000', 1002, 'sell')
        s.legs = [leg]

        with patch('strategy.ema_credit_spread_v2.place_order', return_value=None), \
             patch('strategy.ema_credit_spread_v2.time.sleep'):
            s.close_all()

        assert leg in s.legs


# ───────────────────── two-tier ladder trigger math ─────────────────

class _MarkFeed:
    """Returns a scripted sequence of mark prices for get_current_price, one per
    monitor tick, so we can steer the open PnL through the TP/SL ladder. After
    the script is exhausted it keeps returning the last mark.

    A `strategy` + `max_ticks` safety stop forces the monitor loop to end even
    if the ladder never triggers, so a buggy run can't hang the test suite."""

    def __init__(self, marks, strategy=None, max_ticks=50):
        self._marks = marks
        self._i = 0
        self._strategy = strategy
        self._max_ticks = max_ticks

    def __call__(self, product_id, asset):
        if self._strategy is not None and self._i >= self._max_ticks:
            self._strategy._running = False
        m = self._marks[min(self._i, len(self._marks) - 1)]
        self._i += 1
        return {'mark_price': m}


def _run_ladder(strategy, premium, direction, marks, cv=1.0):
    """Drive a single bull-put spread (one sell leg) through _monitor_day_trade.

    Using a single sell leg with size=lot and contract_value=cv keeps the PnL
    arithmetic transparent: pnl = (entry - mark) * size * cv.
    The monitor is run with time.sleep patched out; it returns when the ladder
    reaches TP2/SL2 (a terminal exit).
    """
    leg = {'symbol': 'BTC-P-90000', 'product_id': 1002, 'side': 'sell',
           'type': 'put', 'delta': 0.20, 'strike': 90000,
           'entry_price': 100.0, 'size': strategy.lot_size, 'day_num': 1,
           'opened_at': '2026-10-07'}
    strategy.legs = [leg]
    day_legs = [leg]
    strategy._running = True   # monitor loops `while self._running`

    feed = _MarkFeed(marks, strategy=strategy)
    with patch('strategy.ema_credit_spread_v2.get_current_price', side_effect=feed), \
         patch('strategy.ema_credit_spread_v2.place_order', return_value={'id': 'ok'}), \
         patch('strategy.ema_credit_spread_v2.time.sleep'), \
         patch('config.get_contract_value', return_value=cv):
        strategy._monitor_day_trade(day_legs, premium, 1, direction)


class TestTwoTierLadderMath:
    """P = $20, 50/50 split, cv=1, size=100, entry=100 → pnl = (100-mark)*size.

    These tests verify the *behavior* of the two-tier ladder:
      • TP1/SL1 close half (partial), the trade stays open.
      • TP2/SL2 close the rest (terminal exit) and record the trade.
      • Recorded PnL = realized_tier1 + remaining-half PnL, near the agreed
        nominal (+$14 best / -$24 worst at the exact threshold levels).
    Marks clearly clear each threshold (not sit on the FP edge).
    """

    def _mark_for_full(self, dollars):
        # full-size (100) pnl = (100 - mark) * 100  →  mark = 100 - dollars/100
        return 100 - dollars / 100.0

    def _mark_for_half(self, dollars):
        # half-size (50) pnl = (100 - mark) * 50  →  mark = 100 - dollars/50
        return 100 - dollars / 50.0

    def test_best_case_tp1_then_tp2(self):
        s = _make_strategy()
        premium = 20.0
        # Tick 1: full pnl +$12 (clears TP1 +$10) → book half = +$6.
        # Tick 2: half pnl +$10 (clears TP2 compare level +$9) → full exit.
        marks = [self._mark_for_full(12), self._mark_for_half(10), self._mark_for_half(10)]
        _run_ladder(s, premium, 'bull_put', marks)

        assert len(s.trade_log) == 1
        t = s.trade_log[0]
        assert t['exit_reason'] == 'target'
        # realized tier1 (+$6) + remaining half (+$10) = +$16
        assert t['pnl'] == pytest.approx(16.0, abs=0.01)
        # Scaling out always books LESS than the single-exit 90% max (+$18).
        assert t['pnl'] < premium * 0.90 + 1e-9

    def test_worst_case_sl1_then_sl2(self):
        s = _make_strategy()
        premium = 20.0
        # Tick 1: full pnl -$20 (clears SL1 -$18) → book half = -$10.
        # Tick 2: half pnl -$16 (clears SL2 compare level -$15) → full exit.
        marks = [self._mark_for_full(-20), self._mark_for_half(-16), self._mark_for_half(-16)]
        _run_ladder(s, premium, 'bull_put', marks)

        assert len(s.trade_log) == 1
        t = s.trade_log[0]
        assert t['exit_reason'] == 'stoploss'
        # realized tier1 (-$10) + remaining half (-$16) = -$26
        assert t['pnl'] == pytest.approx(-26.0, abs=0.01)

    def test_tp1_partial_does_not_record_trade(self):
        """After only TP1 (tranche 1), the trade must still be open — no
        trade_log entry yet, and the second tranche still tracked."""
        s = _make_strategy()
        premium = 20.0
        # TP1 at +$12 full → book +$6. Then hold the half at +$2 — between the
        # SL2 downside (-$15) and TP2 (+$9) → stays open.
        marks = [self._mark_for_full(12)] + [self._mark_for_half(2)] * 60
        _run_ladder(s, premium, 'bull_put', marks)

        assert s.trade_log == []            # no terminal exit recorded
        assert len(s.legs) == 1             # remaining half still tracked
        assert s.legs[0]['size'] == 50      # halved by the TP1 partial close

    def test_tp1_then_sl2(self):
        """After TP1 banks a profit, the remaining half can still ride down to
        the SL2 floor (there is NO breakeven-lock)."""
        s = _make_strategy()
        premium = 20.0
        # Tick 1: TP1 at +$12 full → book +$6.
        # Tick 2: remaining half drops to -$16 (clears SL2 -$15) → exit.
        marks = [self._mark_for_full(12), self._mark_for_half(-16), self._mark_for_half(-16)]
        _run_ladder(s, premium, 'bull_put', marks)

        assert len(s.trade_log) == 1
        t = s.trade_log[0]
        assert t['exit_reason'] == 'stoploss'
        # realized tier1 (+$6) + remaining half (-$16) = -$10
        assert t['pnl'] == pytest.approx(-10.0, abs=0.01)

    def test_sl1_then_tp2_recovery(self):
        """After SL1 books a loss, the remaining half may still RECOVER and
        exit via TP2."""
        s = _make_strategy()
        premium = 20.0
        # Tick 1: SL1 at -$20 full → book -$10 (realized_tier1).
        # Tick 2: remaining half recovers to +$10 (clears TP2 +$9) → exit.
        marks = [self._mark_for_full(-20), self._mark_for_half(10), self._mark_for_half(10)]
        _run_ladder(s, premium, 'bull_put', marks)

        assert len(s.trade_log) == 1
        t = s.trade_log[0]
        assert t['exit_reason'] == 'target'
        # realized (-$10) + remaining half (+$10) = $0 (recovered to flat)
        assert t['pnl'] == pytest.approx(0.0, abs=0.01)
