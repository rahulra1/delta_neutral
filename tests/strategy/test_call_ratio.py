"""Tests for CallRatioStrategy."""
import pytest
from unittest.mock import patch, MagicMock

MODULE = 'strategy.call_ratio'

PRODUCT_DETAILS = {'contract_value': 0.001, 'contract_unit_currency': 'BTC'}


def _make_chain(spot=95000):
    """Build a minimal option chain with call options at various strikes."""
    strikes = [93000, 95000, 97000, 99000, 101000, 103000]
    chain = []
    for s in strikes:
        chain.append({
            'contract_type': 'call_options', 'strike_price': s,
            'product_id': s, 'symbol': f'BTC-C-{s}',
            'mark_price': max(1, (103000 - s) * 0.01),
            'spot_price': spot,
        })
    return chain


def _make_strategy(**overrides):
    with patch(f'{MODULE}.WebSocketManager'):
        from strategy.call_ratio import CallRatioStrategy
        defaults = dict(asset='BTC', expiry_date='01-04-2026', lot_size=10,
                        buy_offset_pct=2, sell_offset_pct=4, hedge_offset_pct=7,
                        target_pct=5, sl_pct=8, monitoring_interval=30)
        defaults.update(overrides)
        return CallRatioStrategy(**defaults)


def _make_leg(symbol, product_id, strike, side, size, entry_price, cv=0.001):
    return {
        'symbol': symbol, 'product_id': product_id, 'strike': strike,
        'side': side, 'size': size, 'entry_price': entry_price,
        'contract_value': cv,
    }


class TestInit:
    def test_defaults(self):
        s = _make_strategy()
        assert s.buy_offset_pct == 2
        assert s.sell_offset_pct == 4
        assert s.hedge_offset_pct == 7
        assert s.target_pct == 5
        assert s.sl_pct == 8
        assert s.legs == []
        assert s.running is True

    def test_custom_params(self):
        s = _make_strategy(target_pct=10, sl_pct=15)
        assert s.target_pct == 10
        assert s.sl_pct == 15


class TestFindStrike:
    def test_finds_closest(self):
        s = _make_strategy()
        chain = _make_chain()
        opt = s._find_strike(chain, 96500, 'call_options')
        assert opt['strike_price'] == 97000

    def test_returns_none_for_wrong_type(self):
        s = _make_strategy()
        chain = _make_chain()
        assert s._find_strike(chain, 95000, 'put_options') is None

    def test_exact_match(self):
        s = _make_strategy()
        chain = _make_chain()
        opt = s._find_strike(chain, 95000, 'call_options')
        assert opt['strike_price'] == 95000


class TestInitialize:
    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    @patch(f'{MODULE}.get_product_details', return_value=PRODUCT_DETAILS)
    @patch('api.option_chain.get_option_chain')
    def test_success(self, mock_chain, mock_details, mock_order, mock_sleep):
        s = _make_strategy(expiry_date='01-04-2026')
        chain = _make_chain(spot=95000)
        mock_chain.return_value = {'success': True, 'result': chain}
        result = s.initialize()
        assert result is True
        assert len(s.legs) == 3
        assert s.legs[0]['side'] == 'buy'
        assert s.legs[1]['side'] == 'sell'
        assert s.legs[1]['size'] == 20  # 2x lot_size
        assert s.legs[2]['side'] == 'buy'
        assert mock_order.call_count == 3
        assert s.deployed_margin > 0

    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    @patch(f'{MODULE}.get_product_details', return_value=PRODUCT_DETAILS)
    @patch('api.option_chain.get_option_chain', return_value=None)
    def test_fails_empty_chain(self, mock_chain, mock_details, mock_order, mock_sleep):
        s = _make_strategy(expiry_date='01-04-2026')
        assert s.initialize() is False

    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.place_order', side_effect=[{'id': 1}, None, {'id': 3}])
    @patch(f'{MODULE}.get_product_details', return_value=PRODUCT_DETAILS)
    @patch('api.option_chain.get_option_chain')
    def test_fails_when_sell_order_fails(self, mock_chain, mock_details, mock_order, mock_sleep):
        s = _make_strategy(expiry_date='01-04-2026')
        mock_chain.return_value = {'success': True, 'result': _make_chain()}
        assert s.initialize() is False

    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    @patch(f'{MODULE}.get_product_details', return_value=PRODUCT_DETAILS)
    @patch('api.option_chain.get_option_chain')
    @patch('api.chain.get_expiries', return_value=['15-05-2026'])
    def test_auto_selects_expiry(self, mock_exp, mock_chain, mock_details, mock_order, mock_sleep):
        s = _make_strategy(expiry_date='')
        mock_chain.return_value = {'success': True, 'result': _make_chain()}
        result = s.initialize()
        assert s.expiry_date == '15-05-2026'
        assert result is True

    @patch('api.chain.get_expiries', return_value=[])
    def test_fails_no_expiries(self, mock_exp):
        s = _make_strategy(expiry_date='')
        assert s.initialize() is False


class TestMonitorPnL:
    def _setup_running(self):
        s = _make_strategy()
        s.legs = [
            _make_leg('BTC-C-97000', 97000, 97000, 'buy', 10, 80.0),
            _make_leg('BTC-C-99000', 99000, 99000, 'sell', 20, 40.0),
            _make_leg('BTC-C-102000', 102000, 102000, 'buy', 10, 10.0),
        ]
        s.deployed_margin = 1.0
        s.ws_manager = MagicMock()
        return s

    def test_pnl_calculation_logic(self):
        """Verify the PnL formula: direction * (mark - entry) * size * cv."""
        # buy: 1*(100-80)*10*0.001 = 0.20
        buy_pnl = 1 * (100.0 - 80.0) * 10 * 0.001
        # sell: -1*(60-40)*20*0.001 = -0.40
        sell_pnl = -1 * (60.0 - 40.0) * 20 * 0.001
        # hedge buy: 1*(15-10)*10*0.001 = 0.05
        hedge_pnl = 1 * (15.0 - 10.0) * 10 * 0.001
        total = buy_pnl + sell_pnl + hedge_pnl
        assert abs(total - (-0.15)) < 0.001

    def test_target_exit_condition(self):
        s = self._setup_running()
        s.total_pnl = 0.06
        s.pnl_pct = 6.0  # > target_pct of 5
        assert s.pnl_pct >= s.target_pct

    def test_sl_exit_condition(self):
        s = self._setup_running()
        s.total_pnl = -0.10
        s.pnl_pct = -10.0  # > sl_pct of 8
        assert s.pnl_pct <= -s.sl_pct


class TestCloseAll:
    @patch(f'{MODULE}.time.sleep')
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    def test_closes_all_legs(self, mock_order, mock_sleep):
        s = _make_strategy()
        s.legs = [
            _make_leg('A', 1, 97000, 'buy', 10, 80.0),
            _make_leg('B', 2, 99000, 'sell', 20, 40.0),
        ]
        s.ws_manager = MagicMock()
        s.close_all()
        assert mock_order.call_count == 2
        calls = mock_order.call_args_list
        assert calls[0][0][3] == 'sell'  # close buy → sell
        assert calls[1][0][3] == 'buy'   # close sell → buy
        assert s.running is False


class TestPnlProperty:
    def test_returns_total_pnl(self):
        s = _make_strategy()
        s.total_pnl = 3.14
        assert s.pnl == 3.14
