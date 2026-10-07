"""Tests for EMACreditSpread close behavior.

Core requirement under test: a leg must NOT be removed from monitoring
(self.legs / the day_legs list) until its close order actually SUCCEEDS.
place_order returns a truthy result dict on success and None on failure.
"""
from unittest.mock import patch

import pytest

from strategy.ema_credit_spread import EMACreditSpread


def _leg(symbol, product_id, side='sell'):
    return {
        'symbol': symbol, 'product_id': product_id, 'side': side,
        'type': 'put', 'delta': 0.20, 'strike': 90000,
        'entry_price': 450.0, 'size': 100, 'day_num': 1,
        'opened_at': '2026-10-07',
    }


def _make_strategy():
    s = EMACreditSpread(asset='BTC', lot_size=100)
    # Avoid DB/app persistence side effects during unit tests
    s._persist_state = lambda: None
    return s


class TestCloseDayLegsKeepsUntilClosed:

    def test_all_closes_succeed_removes_all_legs(self):
        s = _make_strategy()
        legs = [_leg('BTC-P-90000', 1002, 'sell'), _leg('BTC-P-80000', 1003, 'buy')]
        s.legs = list(legs)

        with patch('strategy.ema_credit_spread.place_order', return_value={'id': 'ok'}):
            still_open = s._close_day_legs(legs)

        assert still_open == []          # nothing left open
        assert s.legs == []              # removed from monitoring
        assert legs == []                # day_legs list drained

    def test_failed_close_keeps_leg_under_monitoring(self):
        s = _make_strategy()
        good = _leg('BTC-P-90000', 1002, 'sell')
        bad = _leg('BTC-P-80000', 1003, 'buy')
        legs = [good, bad]
        s.legs = list(legs)

        # First leg closes (truthy), second fails (None)
        def _side_effect(product_id, symbol, size, side):
            return {'id': 'ok'} if product_id == 1002 else None

        with patch('strategy.ema_credit_spread.place_order', side_effect=_side_effect):
            still_open = s._close_day_legs(legs)

        # The leg that failed to close must still be tracked
        assert still_open == [bad]
        assert good not in s.legs        # closed leg removed
        assert bad in s.legs             # failed leg KEPT under monitoring
        assert legs == [bad]             # day_legs still contains the open leg

    def test_exception_during_close_keeps_leg(self):
        s = _make_strategy()
        leg = _leg('BTC-P-90000', 1002, 'sell')
        legs = [leg]
        s.legs = list(legs)

        with patch('strategy.ema_credit_spread.place_order', side_effect=RuntimeError('api down')):
            still_open = s._close_day_legs(legs)

        assert still_open == [leg]       # treated as not closed
        assert leg in s.legs             # kept under monitoring


class TestCloseAllKeepsUntilClosed:

    def test_close_all_success_clears_everything(self):
        s = _make_strategy()
        s.legs = [_leg('BTC-P-90000', 1002, 'sell'), _leg('BTC-P-80000', 1003, 'buy')]

        with patch('strategy.ema_credit_spread.place_order', return_value={'id': 'ok'}):
            s.close_all()

        assert s.legs == []
        assert s._running is False

    def test_close_all_retries_then_succeeds(self):
        """A leg that fails the first attempt but succeeds on retry must end up
        closed and removed (bounded retry with backoff)."""
        s = _make_strategy()
        leg = _leg('BTC-P-90000', 1002, 'sell')
        s.legs = [leg]

        calls = {'n': 0}

        def _flaky(product_id, symbol, size, side):
            calls['n'] += 1
            return None if calls['n'] == 1 else {'id': 'ok'}  # fail once, then succeed

        with patch('strategy.ema_credit_spread.place_order', side_effect=_flaky), \
             patch('strategy.ema_credit_spread.time.sleep'):  # skip backoff delay
            s.close_all()

        assert s.legs == []          # eventually closed
        assert calls['n'] >= 2       # retried

    def test_close_all_permanent_failure_keeps_legs(self):
        """If a leg can never be closed, it must remain tracked (not silently
        dropped) after all retry attempts are exhausted."""
        s = _make_strategy()
        leg = _leg('BTC-P-90000', 1002, 'sell')
        s.legs = [leg]

        with patch('strategy.ema_credit_spread.place_order', return_value=None), \
             patch('strategy.ema_credit_spread.time.sleep'):
            s.close_all()

        assert leg in s.legs         # kept under tracking despite shutdown
