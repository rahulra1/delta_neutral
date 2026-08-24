"""Tests for NSE EMA Spread order-fill confirmation.

Regression: on weekends/holidays the market is closed, so a submitted market
order never fills — but the strategy used to record the position and start
monitoring anyway. Now _place_spread_orders must only report success when the
broker confirms BOTH legs EXECUTED, and must unwind a one-sided fill.
"""
from unittest.mock import patch

from strategy.nse_ema_spread import NseEmaCreditSpread


def _strategy():
    s = NseEmaCreditSpread.__new__(NseEmaCreditSpread)  # bypass __init__
    s.quantity = 75
    return s


SELL = {'trading_symbol': 'NIFTY_C_24500', 'strike': 24500}
BUY = {'trading_symbol': 'NIFTY_C_24700', 'strike': 24700}


class TestOrderFillConfirmation:

    def test_weekend_orders_not_filled_no_position(self):
        """Orders submitted but never filled -> success False, buy leg not even
        attempted (abort after sell fails to fill)."""
        s = _strategy()
        with patch('api.groww.place_order', return_value={'groww_order_id': 'OID1', 'order_status': 'NEW'}) as po, \
             patch('api.groww.confirm_order_filled', return_value=(False, 'timeout:NEW')), \
             patch('api.groww.cancel_order', return_value={}):
            ok = s._place_spread_orders(SELL, BUY, '[T]')
        assert ok is False
        assert po.call_count == 1  # only the sell leg attempted, then aborted

    def test_both_legs_executed_success(self):
        s = _strategy()
        with patch('api.groww.place_order', return_value={'groww_order_id': 'OID', 'order_status': 'NEW'}), \
             patch('api.groww.confirm_order_filled', return_value=(True, 'EXECUTED')):
            ok = s._place_spread_orders(SELL, BUY, '[T]')
        assert ok is True

    def test_sell_filled_buy_unfilled_unwinds(self):
        """If only the sell fills, we must buy it back and report failure."""
        s = _strategy()
        fills = iter([(True, 'EXECUTED'), (False, 'timeout:NEW')])
        calls = {'n': 0}

        def fake_place(**kw):
            calls['n'] += 1
            return {'groww_order_id': 'OID', 'order_status': 'NEW'}

        with patch('api.groww.place_order', side_effect=fake_place), \
             patch('api.groww.confirm_order_filled', side_effect=lambda oid, *a, **k: next(fills)), \
             patch('api.groww.cancel_order', return_value={}):
            ok = s._place_spread_orders(SELL, BUY, '[T]')
        assert ok is False
        # sell + buy + unwind buy-back = 3 place_order calls
        assert calls['n'] == 3

    def test_rejected_status_no_position(self):
        s = _strategy()
        with patch('api.groww.place_order', return_value={'groww_order_id': 'OID', 'order_status': 'NEW'}), \
             patch('api.groww.confirm_order_filled', return_value=(False, 'REJECTED')), \
             patch('api.groww.cancel_order', return_value={}):
            ok = s._place_spread_orders(SELL, BUY, '[T]')
        assert ok is False


class TestConfirmOrderFilled:
    """Unit tests for the api.groww.confirm_order_filled polling helper."""

    def test_executed_returns_true(self):
        from api import groww
        with patch.object(groww, 'get_order_status', return_value={'order_status': 'EXECUTED'}):
            filled, status = groww.confirm_order_filled('OID', timeout=5, poll_interval=0)
        assert filled is True and status == 'EXECUTED'

    def test_rejected_returns_false(self):
        from api import groww
        with patch.object(groww, 'get_order_status', return_value={'order_status': 'REJECTED'}):
            filled, status = groww.confirm_order_filled('OID', timeout=5, poll_interval=0)
        assert filled is False and status == 'REJECTED'

    def test_pending_then_executed(self):
        from api import groww
        seq = iter([{'order_status': 'NEW'}, {'order_status': 'ACKED'}, {'order_status': 'EXECUTED'}])
        with patch.object(groww, 'get_order_status', side_effect=lambda *a, **k: next(seq)):
            filled, status = groww.confirm_order_filled('OID', timeout=5, poll_interval=0)
        assert filled is True and status == 'EXECUTED'

    def test_timeout_when_never_fills(self):
        from api import groww
        with patch.object(groww, 'get_order_status', return_value={'order_status': 'NEW'}):
            filled, status = groww.confirm_order_filled('OID', timeout=0.01, poll_interval=0)
        assert filled is False
        assert status.startswith('timeout')

    def test_no_order_id_returns_false(self):
        from api import groww
        filled, status = groww.confirm_order_filled('', timeout=1)
        assert filled is False and status == 'no_order_id'
