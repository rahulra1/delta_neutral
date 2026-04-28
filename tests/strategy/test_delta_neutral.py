"""Tests for DeltaNeutralStrategy."""
import time
import threading
import pytest
from unittest.mock import patch, MagicMock
from tests.strategy.conftest import CALL_OPT, PUT_OPT, PRODUCT_DETAILS


MODULE = 'strategy'


def _make_strategy(**overrides):
    """Create a DeltaNeutralStrategy with mocked WebSocket."""
    with patch(f'{MODULE}.WebSocketManager'):
        from strategy import DeltaNeutralStrategy
        defaults = dict(target_delta=0.20, lot_size=100, premium_threshold=0.4,
                        target_pnl=25, max_adjustments=5, monitoring_interval=5)
        defaults.update(overrides)
        s = DeltaNeutralStrategy(**defaults)
    return s


class TestInit:
    def test_defaults(self):
        s = _make_strategy()
        assert s.target_delta == 0.20
        assert s.lot_size == 100
        assert s.premium_threshold == 0.4
        assert s.target_pnl == 25
        assert s.max_adjustments == 5
        assert s.call_position is None
        assert s.put_position is None
        assert s.cumulative_realized_pnl == 0
        assert s.adjustment_count == 0
        assert s.running is True

    def test_custom_params(self):
        s = _make_strategy(target_delta=0.30, lot_size=50, target_pnl=100)
        assert s.target_delta == 0.30
        assert s.lot_size == 50
        assert s.target_pnl == 100

    @patch(f'{MODULE}.get_option_chain', return_value=None)
    def test_initialize_fails_no_chain(self, mock_chain):
        s = _make_strategy()
        assert s.initialize() is False

    @patch(f'{MODULE}.get_position_entry_price', return_value=(500.0, -100))
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    @patch(f'{MODULE}.get_product_details', return_value=PRODUCT_DETAILS)
    @patch(f'{MODULE}.find_target_delta_options', return_value=(dict(CALL_OPT), dict(PUT_OPT)))
    @patch(f'{MODULE}.get_option_chain', return_value=[CALL_OPT, PUT_OPT])
    @patch(f'{MODULE}.time.sleep')
    def test_initialize_success(self, mock_sleep, mock_chain, mock_find, mock_details,
                                mock_order, mock_entry):
        s = _make_strategy()
        assert s.initialize() is True
        assert s.call_position is not None
        assert s.put_position is not None
        assert s.call_entry_price == CALL_OPT['mark_price']
        assert s.put_entry_price == PUT_OPT['mark_price']
        assert mock_order.call_count == 2  # call + put

    @patch(f'{MODULE}.get_position_entry_price', return_value=(500.0, -100))
    @patch(f'{MODULE}.place_order', side_effect=[{'id': 1}, None])  # put order fails
    @patch(f'{MODULE}.get_product_details', return_value=PRODUCT_DETAILS)
    @patch(f'{MODULE}.find_target_delta_options', return_value=(dict(CALL_OPT), dict(PUT_OPT)))
    @patch(f'{MODULE}.get_option_chain', return_value=[CALL_OPT, PUT_OPT])
    @patch(f'{MODULE}.time.sleep')
    def test_initialize_fails_order(self, mock_sleep, mock_chain, mock_find,
                                    mock_details, mock_order, mock_entry):
        s = _make_strategy()
        assert s.initialize() is False

    @patch(f'{MODULE}.find_target_delta_options', return_value=(None, None))
    @patch(f'{MODULE}.get_option_chain', return_value=[CALL_OPT])
    def test_initialize_fails_no_delta_match(self, mock_chain, mock_find):
        s = _make_strategy()
        assert s.initialize() is False


class TestOnPriceUpdate:
    def test_throttles_calls(self):
        s = _make_strategy()
        s.call_position = dict(CALL_OPT)
        s.put_position = dict(PUT_OPT)
        s.call_entry_price = 500.0
        s.put_entry_price = 450.0
        s.last_check_time_call = time.time()  # just checked

        with patch.object(s, 'check_adjustment') as mock_adj:
            s.on_price_update(CALL_OPT['symbol'], 600.0, 0.25)
            mock_adj.assert_not_called()  # throttled

    def test_calls_check_after_interval(self):
        s = _make_strategy(monitoring_interval=0)
        s.call_position = dict(CALL_OPT)
        s.put_position = dict(PUT_OPT)
        s.call_entry_price = 500.0
        s.last_check_time_call = 0  # long ago

        with patch.object(s, 'check_adjustment') as mock_adj:
            s.on_price_update(CALL_OPT['symbol'], 600.0, 0.25)
            mock_adj.assert_called_once_with('call', 600.0, 0.25)

    def test_put_update(self):
        s = _make_strategy(monitoring_interval=0)
        s.call_position = dict(CALL_OPT)
        s.put_position = dict(PUT_OPT)
        s.put_entry_price = 450.0
        s.last_check_time_put = 0

        with patch.object(s, 'check_adjustment') as mock_adj:
            s.on_price_update(PUT_OPT['symbol'], 700.0, -0.30)
            mock_adj.assert_called_once_with('put', 700.0, -0.30)

    def test_ignores_unknown_symbol(self):
        s = _make_strategy()
        s.call_position = dict(CALL_OPT)
        s.put_position = dict(PUT_OPT)
        with patch.object(s, 'check_adjustment') as mock_adj:
            s.on_price_update('UNKNOWN-SYM', 100.0, 0.1)
            mock_adj.assert_not_called()


class TestCheckAdjustment:
    def test_no_adjustment_below_threshold(self):
        s = _make_strategy()
        s.call_position = dict(CALL_OPT)
        s.put_position = dict(PUT_OPT)
        s.call_entry_price = 500.0

        with patch.object(s, 'adjust_position') as mock_adj:
            s.check_adjustment('call', 600.0, 0.25)  # 20% < 40%
            mock_adj.assert_not_called()

    def test_triggers_adjustment_above_threshold(self):
        s = _make_strategy()
        s.call_position = dict(CALL_OPT)
        s.put_position = dict(PUT_OPT)
        s.call_entry_price = 500.0
        s.put_entry_price = 450.0
        s.ws_manager = MagicMock()
        s.ws_manager.get_latest_price.return_value = {'mark_price': 450.0}

        with patch.object(s, 'adjust_position') as mock_adj:
            s.check_adjustment('call', 750.0, 0.30)  # 50% > 40%
            mock_adj.assert_called_once()

    def test_skips_when_max_adjustments_reached(self):
        s = _make_strategy(max_adjustments=3)
        s.call_entry_price = 500.0
        s.adjustment_count = 3

        with patch.object(s, 'adjust_position') as mock_adj:
            s.check_adjustment('call', 900.0, 0.30)
            mock_adj.assert_not_called()

    def test_skips_zero_entry(self):
        s = _make_strategy()
        s.call_entry_price = 0

        with patch.object(s, 'adjust_position') as mock_adj:
            s.check_adjustment('call', 500.0, 0.20)
            mock_adj.assert_not_called()

    def test_concurrent_adjustment_blocked(self):
        s = _make_strategy()
        s.call_entry_price = 500.0
        s.call_position = dict(CALL_OPT)
        s.put_position = dict(PUT_OPT)
        s._adjusting.acquire()  # simulate in-progress adjustment

        with patch.object(s, '_check_adjustment_inner') as mock_inner:
            s.check_adjustment('call', 900.0, 0.30)
            mock_inner.assert_not_called()

        s._adjusting.release()


class TestAdjustPosition:
    def _setup_strategy(self):
        s = _make_strategy()
        s.call_position = dict(CALL_OPT)
        s.put_position = dict(PUT_OPT)
        s.call_entry_price = 500.0
        s.put_entry_price = 450.0
        s.call_contract_value = 0.001
        s.put_contract_value = 0.001
        s.ws_manager = MagicMock()
        return s

    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.get_product_details', return_value=PRODUCT_DETAILS)
    @patch(f'{MODULE}.get_position_entry_price', return_value=(500.0, -100))
    @patch(f'{MODULE}.find_target_delta_options', return_value=(dict(CALL_OPT), dict(PUT_OPT)))
    @patch(f'{MODULE}.get_option_chain', return_value=[CALL_OPT, PUT_OPT])
    @patch(f'{MODULE}.get_current_price', return_value={'mark_price': 750.0, 'delta': 0.30})
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    def test_call_triggered_closes_put(self, mock_order, mock_price, mock_chain,
                                       mock_find, mock_entry, mock_details, mock_sleep):
        s = self._setup_strategy()
        s.adjust_position('call', 0.30, 750.0, 450.0)
        # Should close put (buy) then sell new put
        assert mock_order.call_count == 2
        assert s.adjustment_count == 1
        assert s.call_entry_price == 750.0  # reset to current

    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.get_product_details', return_value=PRODUCT_DETAILS)
    @patch(f'{MODULE}.get_position_entry_price', return_value=(450.0, -100))
    @patch(f'{MODULE}.find_target_delta_options', return_value=(dict(CALL_OPT), dict(PUT_OPT)))
    @patch(f'{MODULE}.get_option_chain', return_value=[CALL_OPT, PUT_OPT])
    @patch(f'{MODULE}.get_current_price', return_value={'mark_price': 650.0, 'delta': -0.30})
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    def test_put_triggered_closes_call(self, mock_order, mock_price, mock_chain,
                                       mock_find, mock_entry, mock_details, mock_sleep):
        s = self._setup_strategy()
        s.adjust_position('put', -0.30, 500.0, 650.0)
        assert mock_order.call_count == 2
        assert s.adjustment_count == 1
        assert s.put_entry_price == 650.0

    @patch(f'{MODULE}.get_position_entry_price', return_value=(None, None))
    @patch(f'{MODULE}.place_order')
    def test_abort_when_entry_price_unavailable(self, mock_order, mock_entry):
        s = self._setup_strategy()
        s.adjust_position('call', 0.30, 750.0, 450.0)
        mock_order.assert_not_called()  # aborted before placing any order

    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.get_position_entry_price', return_value=(500.0, -100))
    @patch(f'{MODULE}.find_target_delta_options', return_value=(None, None))
    @patch(f'{MODULE}.get_option_chain', return_value=[])
    @patch(f'{MODULE}.get_current_price', return_value={'mark_price': 750.0, 'delta': 0.30})
    @patch(f'{MODULE}.place_order')
    def test_rollback_when_no_replacement(self, mock_order, mock_price, mock_chain,
                                          mock_find, mock_entry, mock_sleep):
        s = self._setup_strategy()
        # close succeeds, but no replacement found → rollback
        mock_order.side_effect = [{'id': 1}, {'id': 2}]  # close + rollback
        s.adjust_position('call', 0.30, 750.0, 450.0)
        assert s.adjustment_count == 0  # no adjustment counted
        assert mock_order.call_count == 2  # close + rollback

    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.get_position_entry_price', return_value=(500.0, -100))
    @patch(f'{MODULE}.find_target_delta_options', return_value=(dict(CALL_OPT), dict(PUT_OPT)))
    @patch(f'{MODULE}.get_option_chain', return_value=[CALL_OPT, PUT_OPT])
    @patch(f'{MODULE}.get_current_price', return_value={'mark_price': 750.0, 'delta': 0.30})
    @patch(f'{MODULE}.place_order')
    def test_rollback_when_new_order_fails(self, mock_order, mock_price, mock_chain,
                                           mock_find, mock_entry, mock_sleep):
        s = self._setup_strategy()
        mock_order.side_effect = [{'id': 1}, None, {'id': 3}]  # close ok, new fails, rollback ok
        s.adjust_position('call', 0.30, 750.0, 450.0)
        assert s.adjustment_count == 0

    def test_cumulative_pnl_tracks(self):
        s = self._setup_strategy()
        s.cumulative_realized_pnl = 5.0
        with patch(f'{MODULE}.get_position_entry_price', return_value=(500.0, -100)), \
             patch(f'{MODULE}.place_order', return_value={'id': 1}), \
             patch(f'{MODULE}.get_current_price', return_value={'mark_price': 750.0, 'delta': 0.30}), \
             patch(f'{MODULE}.get_option_chain', return_value=[CALL_OPT, PUT_OPT]), \
             patch(f'{MODULE}.find_target_delta_options', return_value=(dict(CALL_OPT), dict(PUT_OPT))), \
             patch(f'{MODULE}.get_product_details', return_value=PRODUCT_DETAILS), \
             patch(f'{MODULE}.time.sleep'):
            s.adjust_position('call', 0.30, 750.0, 450.0)
        # cumulative should have changed from the realized pnl of closing the put
        assert s.cumulative_realized_pnl != 5.0


class TestCloseAllPositions:
    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    @patch(f'{MODULE}.get_position_entry_price', return_value=(500.0, -100))
    @patch(f'{MODULE}.get_current_price', return_value={'mark_price': 480.0, 'delta': 0.20})
    def test_closes_both_legs(self, mock_price, mock_entry, mock_order, mock_sleep):
        s = _make_strategy()
        s.call_position = dict(CALL_OPT)
        s.put_position = dict(PUT_OPT)
        s.call_contract_value = 0.001
        s.put_contract_value = 0.001
        s.ws_manager = MagicMock()
        s.close_all_positions()
        assert mock_order.call_count == 2
        s.ws_manager.stop.assert_called_once()

    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.place_order')
    @patch(f'{MODULE}.get_position_entry_price', return_value=(None, 0))
    @patch(f'{MODULE}.get_current_price', return_value=None)
    def test_handles_missing_data(self, mock_price, mock_entry, mock_order, mock_sleep):
        s = _make_strategy()
        s.call_position = dict(CALL_OPT)
        s.put_position = dict(PUT_OPT)
        s.ws_manager = MagicMock()
        s.close_all_positions()  # should not raise


class TestPnlProperty:
    def test_returns_total_pnl(self):
        s = _make_strategy()
        s.total_pnl = 42.5
        assert s.pnl == 42.5
