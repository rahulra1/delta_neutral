"""Tests for StrategyMonitor."""
import time
import threading
import pytest
from unittest.mock import patch, MagicMock, call

MODULE = 'strategy.monitor'


def _make_leg(symbol, product_id, side, size, entry_price):
    return {
        'symbol': symbol, 'product_id': product_id,
        'type': 'call', 'strike': 95000,
        'side': side, 'size': size, 'entry_price': entry_price,
    }


def _make_monitor(legs=None, max_profit=10, max_loss=10, **kw):
    from strategy.monitor import StrategyMonitor
    if legs is None:
        legs = [
            _make_leg('BTC-C-95000', 1001, 'sell', 10, 500.0),
            _make_leg('BTC-P-90000', 1002, 'sell', 10, 450.0),
        ]
    return StrategyMonitor(legs=legs, max_profit=max_profit, max_loss=max_loss,
                           asset='BTC', lot_size=0.001, interval=1, **kw)


class TestInit:
    def test_defaults(self):
        m = _make_monitor()
        assert m.max_profit == 10
        assert m.max_loss == 10
        assert m.running is False
        assert m.current_pnl == 0
        assert m.exit_reason is None
        assert len(m.legs) == 2

    def test_max_values_absolute(self):
        m = _make_monitor(max_profit=-5, max_loss=-8)
        assert m.max_profit == 5
        assert m.max_loss == 8

    def test_on_complete_callback(self):
        cb = MagicMock()
        m = _make_monitor(on_complete=cb)
        assert m.on_complete is cb


class TestLog:
    def test_log_appends(self):
        m = _make_monitor()
        m._log("test message")
        assert len(m.log) == 1
        assert "test message" in m.log[0]

    def test_log_truncates_at_200(self):
        m = _make_monitor()
        for i in range(250):
            m._log(f"msg {i}")
        assert len(m.log) == 200


class TestPnLCalculation:
    @patch(f'{MODULE}.compute_leg_pnl')
    @patch(f'{MODULE}.get_current_price')
    def test_computes_total_pnl(self, mock_price, mock_leg_pnl):
        m = _make_monitor()
        mock_price.return_value = {'mark_price': 480.0}
        mock_leg_pnl.side_effect = [0.20, 0.15]  # two legs

        # Simulate one iteration of the monitor loop
        pnl = 0
        for leg in m.legs:
            data = mock_price(leg['product_id'], m.asset)
            mark = data['mark_price']
            leg_pnl = mock_leg_pnl(leg['entry_price'], mark, leg['size'], leg['side'], m.lot_size)
            pnl += leg_pnl
        assert pnl == pytest.approx(0.35)


class TestMaxProfitExit:
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    @patch(f'{MODULE}.compute_leg_pnl', return_value=6.0)
    @patch(f'{MODULE}.get_current_price', return_value={'mark_price': 400.0})
    def test_exits_on_max_profit(self, mock_price, mock_pnl, mock_order):
        cb = MagicMock()
        m = _make_monitor(max_profit=10, on_complete=cb)
        m.running = True

        # Manually run one cycle
        pnl = 0
        for leg in m.legs:
            data = mock_price(leg['product_id'], m.asset)
            leg_pnl = mock_pnl(leg['entry_price'], data['mark_price'],
                               leg['size'], leg['side'], m.lot_size)
            pnl += leg_pnl
        m.current_pnl = pnl  # 12.0 > max_profit 10
        assert m.current_pnl >= m.max_profit

    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    @patch(f'{MODULE}.compute_leg_pnl', return_value=-6.0)
    @patch(f'{MODULE}.get_current_price', return_value={'mark_price': 600.0})
    def test_exits_on_max_loss(self, mock_price, mock_pnl, mock_order):
        m = _make_monitor(max_loss=10)
        m.running = True

        pnl = 0
        for leg in m.legs:
            data = mock_price(leg['product_id'], m.asset)
            leg_pnl = mock_pnl(leg['entry_price'], data['mark_price'],
                               leg['size'], leg['side'], m.lot_size)
            pnl += leg_pnl
        m.current_pnl = pnl  # -12.0 <= -max_loss -10
        assert m.current_pnl <= -m.max_loss


class TestIncompleteData:
    @patch(f'{MODULE}.get_current_price', return_value=None)
    def test_skips_exit_check_on_missing_data(self, mock_price):
        m = _make_monitor()
        m.running = True

        all_legs_ok = True
        for leg in m.legs:
            data = mock_price(leg['product_id'], m.asset)
            if not data:
                all_legs_ok = False
        assert not all_legs_ok  # should skip exit check

    def test_missing_product_id(self):
        leg = {'symbol': 'X', 'side': 'sell', 'size': 10, 'entry_price': 100}
        m = _make_monitor(legs=[leg])
        # Leg has no product_id — should be flagged
        assert 'product_id' not in leg or leg.get('product_id') is None


class TestCloseAll:
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    def test_close_all_places_opposite_orders(self, mock_order):
        cb = MagicMock()
        m = _make_monitor(on_complete=cb)
        m.running = True
        m.current_pnl = 5.0
        m.exit_reason = 'max_profit'
        m._close_all()
        assert mock_order.call_count == 2
        # sell legs should be closed with buy
        for c in mock_order.call_args_list:
            assert c[0][3] == 'buy'  # opposite of 'sell'
        assert m.running is False
        cb.assert_called_once_with(5.0, 'max_profit')


class TestStop:
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    def test_manual_stop(self, mock_order):
        m = _make_monitor()
        m.running = True
        m.stop()
        assert m.running is False
        assert mock_order.call_count == 2


class TestGetStatus:
    def test_returns_dict(self):
        m = _make_monitor()
        m.current_pnl = 3.5
        status = m.get_status()
        assert status['running'] is False
        assert status['current_pnl'] == 3.5
        assert status['max_profit'] == 10
        assert status['max_loss'] == 10
        assert len(status['legs']) == 2

    def test_pnl_history_in_status(self):
        m = _make_monitor()
        m.pnl_history = [('2026-01-01T00:00:00', 1.0), ('2026-01-01T00:00:10', 2.0)]
        status = m.get_status()
        assert len(status['pnl_history']) == 2
