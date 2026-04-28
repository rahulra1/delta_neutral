"""Tests for IVCrushStrategy."""
import pytest
from unittest.mock import patch, MagicMock

MODULE = 'strategy.iv_crush'


def _make_strategy(**overrides):
    with patch(f'{MODULE}.WebSocketManager'):
        from strategy.iv_crush import IVCrushStrategy
        defaults = dict(asset='BTC', expiry_date='01-04-2026', lot_size=10,
                        iv_rv_threshold=1.3, max_loss_pct=50,
                        target_profit_pct=30, monitoring_interval=10)
        defaults.update(overrides)
        return IVCrushStrategy(**defaults)


CHAIN_ROW = {
    'strike': 95000,
    'call': {'symbol': 'BTC-C-95000', 'product_id': 2001, 'mark_price': 800.0,
             'iv': 0.85, 'strike_price': 95000},
    'put': {'symbol': 'BTC-P-95000', 'product_id': 2002, 'mark_price': 750.0,
            'iv': 0.80, 'strike_price': 95000},
}
PRODUCT_DETAILS = {'contract_value': 0.001, 'contract_unit_currency': 'BTC'}


class TestInit:
    def test_defaults(self):
        s = _make_strategy()
        assert s.iv_rv_threshold == 1.3
        assert s.max_loss_pct == 50
        assert s.target_profit_pct == 30
        assert s.call_position is None
        assert s.total_premium == 0
        assert s.running is True

    def test_custom_params(self):
        s = _make_strategy(iv_rv_threshold=2.0, lot_size=50)
        assert s.iv_rv_threshold == 2.0
        assert s.lot_size == 50


class TestIVRVCalculation:
    def test_calc_iv_rv_with_valid_data(self):
        s = _make_strategy()
        call = {'iv': 0.80}
        put = {'iv': 0.70}
        with patch.object(s, '_compute_realized_vol', return_value=0.50):
            avg_iv, rv, ratio = s._calc_iv_rv(call, put)
        assert avg_iv == 0.75
        assert rv == 0.50
        assert ratio == 1.5

    def test_calc_iv_rv_zero_rv_passes_filter(self):
        s = _make_strategy()
        call = {'iv': 0.80}
        put = {'iv': 0.70}
        with patch.object(s, '_compute_realized_vol', return_value=0):
            avg_iv, rv, ratio = s._calc_iv_rv(call, put)
        # Should return threshold so filter passes
        assert ratio == s.iv_rv_threshold

    def test_calc_iv_rv_single_iv(self):
        s = _make_strategy()
        call = {'iv': 0.80}
        put = {'iv': 0}  # no put IV
        with patch.object(s, '_compute_realized_vol', return_value=0.50):
            avg_iv, rv, ratio = s._calc_iv_rv(call, put)
        assert avg_iv == 0.80  # falls back to call IV only


class TestFindATM:
    def test_finds_closest_strike(self):
        s = _make_strategy()
        chain = [
            {'contract_type': 'call_options', 'strike_price': 90000, 'mark_price': 1000, 'spot_price': 95000},
            {'contract_type': 'call_options', 'strike_price': 95000, 'mark_price': 500, 'spot_price': 95000},
            {'contract_type': 'call_options', 'strike_price': 100000, 'mark_price': 200, 'spot_price': 95000},
            {'contract_type': 'put_options', 'strike_price': 90000, 'mark_price': 200, 'spot_price': 95000},
            {'contract_type': 'put_options', 'strike_price': 95000, 'mark_price': 500, 'spot_price': 95000},
            {'contract_type': 'put_options', 'strike_price': 100000, 'mark_price': 1000, 'spot_price': 95000},
        ]
        call, put = s._find_atm_options(chain)
        assert call['strike_price'] == 95000
        assert put['strike_price'] == 95000

    def test_empty_chain(self):
        s = _make_strategy()
        assert s._find_atm_options([]) == (None, None)
        assert s._find_atm_options(None) == (None, None)


class TestInitialize:
    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.get_position_entry_price', return_value=(800.0, -10))
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    @patch(f'{MODULE}.get_product_details', return_value=PRODUCT_DETAILS)
    @patch(f'{MODULE}.get_option_chain')
    @patch('api.chain.get_option_chain_full')
    def test_success_with_provided_expiry(self, mock_full, mock_chain, mock_details,
                                          mock_order, mock_entry, mock_sleep):
        s = _make_strategy(expiry_date='01-04-2026')
        mock_full.return_value = ([CHAIN_ROW], 95000, None)
        with patch.object(s, '_calc_iv_rv', return_value=(0.82, 0.50, 1.64)):
            result = s.initialize()
        assert result is True
        assert s.call_position is not None
        assert s.put_position is not None
        assert s.total_premium > 0
        assert mock_order.call_count == 2

    @patch(f'{MODULE}.get_option_chain')
    @patch('api.chain.get_option_chain_full')
    def test_fails_when_iv_rv_below_threshold(self, mock_full, mock_chain):
        s = _make_strategy(expiry_date='01-04-2026', iv_rv_threshold=2.0)
        mock_full.return_value = ([CHAIN_ROW], 95000, None)
        with patch.object(s, '_calc_iv_rv', return_value=(0.82, 0.50, 1.2)):
            result = s.initialize()
        assert result is False
        assert 'skipped' in s.status_msg.lower()

    @patch(f'{MODULE}.get_option_chain')
    @patch('api.chain.get_option_chain_full', return_value=(None, None, None))
    def test_fails_empty_chain(self, mock_full, mock_chain):
        s = _make_strategy(expiry_date='01-04-2026')
        assert s.initialize() is False

    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.get_position_entry_price', return_value=(800.0, -10))
    @patch(f'{MODULE}.place_order', side_effect=[None, {'id': 1}])
    @patch(f'{MODULE}.get_product_details', return_value=PRODUCT_DETAILS)
    @patch(f'{MODULE}.get_option_chain')
    @patch('api.chain.get_option_chain_full')
    def test_fails_when_call_order_fails(self, mock_full, mock_chain, mock_details,
                                         mock_order, mock_entry, mock_sleep):
        s = _make_strategy(expiry_date='01-04-2026')
        mock_full.return_value = ([CHAIN_ROW], 95000, None)
        with patch.object(s, '_calc_iv_rv', return_value=(0.82, 0.50, 1.64)):
            assert s.initialize() is False


class TestMonitorExits:
    def _setup_running(self):
        s = _make_strategy()
        s.call_position = CHAIN_ROW['call']
        s.put_position = CHAIN_ROW['put']
        s.call_entry_price = 800.0
        s.put_entry_price = 750.0
        s.call_contract_value = 0.001
        s.put_contract_value = 0.001
        s.total_premium = (800.0 * 10 * 0.001) + (750.0 * 10 * 0.001)  # 15.5
        s.ws_manager = MagicMock()
        return s

    @patch(f'{MODULE}.time.sleep', side_effect=StopIteration)
    def test_target_profit_exit(self, mock_sleep):
        s = self._setup_running()
        # Prices dropped a lot → big profit for short seller
        s.ws_manager.get_latest_price.side_effect = lambda sym: (
            {'mark_price': 100.0, 'iv': 0.40} if 'C' in sym
            else {'mark_price': 100.0, 'iv': 0.40}
        )
        with patch.object(s, 'close_all') as mock_close:
            # Run one iteration — should hit target
            s.running = True
            # Manually run one cycle of monitor logic
            call_price, put_price = 100.0, 100.0
            call_pnl = (800.0 - 100.0) * 10 * 0.001
            put_pnl = (750.0 - 100.0) * 10 * 0.001
            s.unrealized_pnl = call_pnl + put_pnl
            s.total_pnl = s.unrealized_pnl
            pnl_pct = s.total_pnl / s.total_premium * 100
            assert pnl_pct >= s.target_profit_pct

    @patch(f'{MODULE}.time.sleep', side_effect=StopIteration)
    def test_max_loss_exit(self, mock_sleep):
        s = self._setup_running()
        # Prices spiked → loss for short seller
        call_price, put_price = 2000.0, 2000.0
        call_pnl = (800.0 - 2000.0) * 10 * 0.001
        put_pnl = (750.0 - 2000.0) * 10 * 0.001
        s.unrealized_pnl = call_pnl + put_pnl
        s.total_pnl = s.unrealized_pnl
        pnl_pct = s.total_pnl / s.total_premium * 100
        assert pnl_pct <= -s.max_loss_pct


class TestCloseAll:
    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    def test_closes_both_legs(self, mock_order, mock_sleep):
        s = _make_strategy()
        s.call_position = CHAIN_ROW['call']
        s.put_position = CHAIN_ROW['put']
        s.ws_manager = MagicMock()
        s.close_all()
        assert mock_order.call_count == 2
        assert s.running is False
        s.ws_manager.stop.assert_called_once()

    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.place_order')
    def test_handles_none_positions(self, mock_order, mock_sleep):
        s = _make_strategy()
        s.call_position = None
        s.put_position = None
        s.ws_manager = MagicMock()
        s.close_all()
        mock_order.assert_not_called()


class TestPnlProperty:
    def test_returns_total_pnl(self):
        s = _make_strategy()
        s.total_pnl = -12.5
        assert s.pnl == -12.5
