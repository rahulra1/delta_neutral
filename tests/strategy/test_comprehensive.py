"""Comprehensive tests: order execution, persistence across restarts,
live P&L monitoring, and OOP/design review for ALL strategy modules."""
import json
import time
import threading
import sqlite3
import tempfile
import os
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════
# MOCK INFRASTRUCTURE
# ═══════════════════════════════════════════════════════════════════════

MOCK_CALL = {
    'symbol': 'C-BTC-100000-010426', 'product_id': 1001,
    'strike_price': 100000, 'mark_price': 500.0,
    'delta': 0.20, 'contract_type': 'call_options',
    'iv': 0.8, 'spot_price': 95000,
}
MOCK_PUT = {
    'symbol': 'P-BTC-90000-010426', 'product_id': 1002,
    'strike_price': 90000, 'mark_price': 450.0,
    'delta': -0.20, 'contract_type': 'put_options',
    'iv': 0.8, 'spot_price': 95000,
}
MOCK_PRODUCT_DETAILS = {'contract_value': 0.001, 'contract_unit_currency': 'BTC'}


def mock_place_order(product_id, symbol, size, side, **kw):
    """Simulates a successful order fill."""
    return {'id': 99999, 'product_id': product_id, 'symbol': symbol,
            'size': size, 'side': side, 'state': 'closed'}


def mock_place_order_fail(*args, **kw):
    return None


def mock_get_current_price(pid, asset='BTC'):
    prices = {1001: 480.0, 1002: 430.0}
    return {'mark_price': prices.get(pid, 500.0), 'delta': 0.18}


def mock_get_positions():
    return [
        {'product_id': 1001, 'size': -10, 'entry_price': 500.0},
        {'product_id': 1002, 'size': -10, 'entry_price': 450.0},
    ]


def mock_get_position_entry_price(pid):
    entries = {1001: (500.0, -10), 1002: (450.0, -10)}
    return entries.get(pid, (None, 0))


def mock_option_chain(*args, **kw):
    return [MOCK_CALL, MOCK_PUT]


def mock_find_delta_options(chain, target_delta, tol):
    return MOCK_CALL, MOCK_PUT


def mock_candles(asset, tf):
    """Return fake candles with a buy signal embedded."""
    now = time.time()
    return [{'t': now - i * 900, 'o': 95000, 'h': 95500,
             'l': 94500, 'c': 95000 + (i % 2) * 100, 'v': 100}
            for i in range(100, 0, -1)]


def mock_indicator_fn(candles):
    """Fake indicator that always returns a recent buy signal."""
    return {'signals': [{'time': time.time() - 60, 'type': 'buy',
                         'price': 95000, 'sl': 94000, 'tp1': 97000}]}


# ═══════════════════════════════════════════════════════════════════════
# TEST: FuturesSignalTrader — Order Execution & Legs Tracking
# ═══════════════════════════════════════════════════════════════════════

class TestFuturesSignalTrader:
    """Test that FST detects signals, places orders, and tracks legs."""

    def _make_trader(self):
        from strategy.futures_signal_trader import FuturesSignalTrader
        return FuturesSignalTrader(
            signal_key='rsi_div_mss', asset='BTC', timeframe='15m',
            lots=1, scan_interval=1, max_trades_per_day=3,
            api_key='test', api_secret='test', broker='demo'
        )

    @patch('strategy.futures_signal_trader.place_order', side_effect=mock_place_order)
    @patch('strategy.futures_signal_trader.get_candles', side_effect=mock_candles)
    @patch('strategy.futures_signal_trader.INDICATOR_FNS', {'rsi_div_mss': mock_indicator_fn})
    def test_signal_detected_and_order_placed(self, mock_gc, mock_po):
        trader = self._make_trader()
        trader._scan_and_trade()
        assert mock_po.called
        assert trader.trades_today == 1
        assert len(trader.trade_log) == 1
        assert trader.trade_log[0]['success'] is True

    @patch('strategy.futures_signal_trader.place_order', side_effect=mock_place_order)
    @patch('strategy.futures_signal_trader.get_candles', side_effect=mock_candles)
    @patch('strategy.futures_signal_trader.INDICATOR_FNS', {'rsi_div_mss': mock_indicator_fn})
    def test_leg_stored_after_fill(self, mock_gc, mock_po):
        trader = self._make_trader()
        trader._scan_and_trade()
        assert len(trader.legs) == 1
        leg = trader.legs[0]
        assert leg['symbol'] == 'BTCUSD'
        assert leg['side'] == 'buy'
        assert leg['size'] == 1
        assert leg['entry_price'] == 95000

    @patch('strategy.futures_signal_trader.place_order', side_effect=mock_place_order_fail)
    @patch('strategy.futures_signal_trader.get_candles', side_effect=mock_candles)
    @patch('strategy.futures_signal_trader.INDICATOR_FNS', {'rsi_div_mss': mock_indicator_fn})
    def test_failed_order_no_leg(self, mock_gc, mock_po):
        trader = self._make_trader()
        trader._scan_and_trade()
        assert len(trader.legs) == 0
        assert trader.trades_today == 0
        assert trader.trade_log[0]['success'] is False

    @patch('strategy.futures_signal_trader.place_order', side_effect=mock_place_order)
    @patch('strategy.futures_signal_trader.get_candles', side_effect=mock_candles)
    @patch('strategy.futures_signal_trader.INDICATOR_FNS', {'rsi_div_mss': mock_indicator_fn})
    def test_max_trades_per_day_respected(self, mock_gc, mock_po):
        trader = self._make_trader()
        trader.max_trades_per_day = 1
        trader._scan_and_trade()
        # Reset signal time to allow second trade attempt
        trader.last_signal_time = 0
        trader._scan_and_trade()
        assert trader.trades_today == 1  # capped at max

    @patch('strategy.futures_signal_trader.place_order', side_effect=mock_place_order)
    @patch('strategy.futures_signal_trader.get_candles', return_value=None)
    @patch('strategy.futures_signal_trader.INDICATOR_FNS', {'rsi_div_mss': mock_indicator_fn})
    def test_no_candles_no_crash(self, mock_gc, mock_po):
        trader = self._make_trader()
        trader._scan_and_trade()
        assert trader.trades_today == 0

    def test_symbol_only_no_product_id_in_payload(self):
        """Verify place_order sends only product_symbol for futures."""
        with patch('api.orders.requests.post') as mock_post:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.status_code = 200
            mock_resp.json.return_value = {'success': True, 'result': {'id': 1}}
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            with patch('api.orders.get_headers', return_value={}):
                with patch('api.orders.config') as mock_config:
                    mock_config.BASE_URL = 'https://test.example.com'
                    from api.orders import place_order
                    place_order(None, 'BTCUSD', 1, 'buy')

            payload = json.loads(mock_post.call_args[1].get('data', mock_post.call_args[0][0] if not mock_post.call_args[1] else '{}'))
            # Handle both positional and keyword
            if not payload:
                call_kw = mock_post.call_args
                payload = json.loads(call_kw.kwargs.get('data', '{}') if hasattr(call_kw, 'kwargs') else '{}')
            assert 'product_id' not in payload
            assert payload['product_symbol'] == 'BTCUSD'


# ═══════════════════════════════════════════════════════════════════════
# TEST: PrevDayBreakoutTrader
# ═══════════════════════════════════════════════════════════════════════

class TestPrevDayBreakoutTrader:

    @patch('strategy.prev_day_breakout.place_order', side_effect=mock_place_order)
    @patch('strategy.prev_day_breakout.get_candles', side_effect=mock_candles)
    @patch('strategy.prev_day_breakout.calc_prev_day_breakout_retest')
    def test_order_placed_on_signal(self, mock_calc, mock_gc, mock_po):
        mock_calc.return_value = {'signals': [
            {'time': time.time() - 30, 'type': 'buy', 'price': 95000, 'sl': 94000, 'tp1': 97000}
        ]}
        from strategy.prev_day_breakout import PrevDayBreakoutTrader
        trader = PrevDayBreakoutTrader(
            asset='BTC', timeframe='15m', lots=1,
            api_key='t', api_secret='t', broker='demo'
        )
        trader._scan_and_trade()
        assert mock_po.called
        assert trader.trades_today == 1

    @patch('strategy.prev_day_breakout.place_order', side_effect=mock_place_order)
    @patch('strategy.prev_day_breakout.get_candles', side_effect=mock_candles)
    @patch('strategy.prev_day_breakout.calc_prev_day_breakout_retest')
    def test_old_signal_ignored(self, mock_calc, mock_gc, mock_po):
        mock_calc.return_value = {'signals': [
            {'time': time.time() - 9999, 'type': 'buy', 'price': 95000, 'sl': 94000, 'tp1': 97000}
        ]}
        from strategy.prev_day_breakout import PrevDayBreakoutTrader
        trader = PrevDayBreakoutTrader(asset='BTC', timeframe='15m', lots=1,
                                       api_key='t', api_secret='t', broker='demo')
        trader._scan_and_trade()
        assert not mock_po.called


# ═══════════════════════════════════════════════════════════════════════
# TEST: DeltaNeutralStrategy — Order Execution
# ═══════════════════════════════════════════════════════════════════════

class TestDeltaNeutralStrategy:

    def _make(self):
        from strategy import DeltaNeutralStrategy
        return DeltaNeutralStrategy(
            asset='BTC', expiry_date='01-04-2026', target_delta=0.20,
            lot_size=10, monitoring_interval=1
        )

    @patch('strategy.get_position_entry_price', side_effect=mock_get_position_entry_price)
    @patch('strategy.place_order', side_effect=mock_place_order)
    @patch('strategy.get_product_details', return_value=MOCK_PRODUCT_DETAILS)
    @patch('strategy.find_target_delta_options', side_effect=mock_find_delta_options)
    @patch('strategy.get_option_chain', side_effect=mock_option_chain)
    def test_initialize_places_both_orders(self, mock_chain, mock_find,
                                           mock_details, mock_order, mock_entry):
        strat = self._make()
        with patch.object(strat.ws_manager, 'start'), \
             patch.object(strat.ws_manager, 'subscribe'):
            result = strat.initialize()
        assert result is True
        assert mock_order.call_count == 2  # call + put
        assert strat.call_position == MOCK_CALL
        assert strat.put_position == MOCK_PUT

    @patch('strategy.get_position_entry_price', side_effect=mock_get_position_entry_price)
    @patch('strategy.place_order', return_value=None)
    @patch('strategy.get_product_details', return_value=MOCK_PRODUCT_DETAILS)
    @patch('strategy.find_target_delta_options', side_effect=mock_find_delta_options)
    @patch('strategy.get_option_chain', side_effect=mock_option_chain)
    def test_initialize_fails_on_order_fail(self, *mocks):
        strat = self._make()
        result = strat.initialize()
        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# TEST: Persistence Across Server Restart
# ═══════════════════════════════════════════════════════════════════════

class TestPersistence:
    """Verify strategies survive a simulated restart by saving/restoring from DB."""

    def _setup_db(self, tmp_path):
        db_path = str(tmp_path / 'test.db')
        os.environ['DB_PATH'] = db_path
        return db_path

    @patch('models.DB_PATH', new_callable=lambda: PropertyMock(return_value=':memory:'))
    def test_save_and_restore_strategy(self, _):
        """Simulate: save strategy → restart → restore → verify state."""
        import importlib
        import models
        # Use temp DB
        tf = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tf.close()
        try:
            models.DB_PATH = tf.name
            models.init_db()

            # Save a strategy
            legs = [
                {'symbol': 'C-BTC-100000', 'product_id': 1001, 'side': 'sell',
                 'size': 10, 'entry_price': 500.0, 'type': 'call'},
                {'symbol': 'P-BTC-90000', 'product_id': 1002, 'side': 'sell',
                 'size': 10, 'entry_price': 450.0, 'type': 'put'},
            ]
            models.save_strategy(
                sid='test123', user_id=1, source='Option Chain',
                name='BTC Strangle', status='running',
                started_at=datetime.now().isoformat(),
                legs=legs, max_profit=10, max_loss=10,
                profile_id=1, asset='BTC'
            )

            # Simulate restart: fetch from DB
            restored = models.get_live_strategies(1)
            assert len(restored) >= 1
            s = next(r for r in restored if r['sid'] == 'test123')
            assert s['status'] == 'running'
            assert s['source'] == 'Option Chain'
            restored_legs = json.loads(s['legs']) if isinstance(s['legs'], str) else s['legs']
            assert len(restored_legs) == 2
            assert restored_legs[0]['symbol'] == 'C-BTC-100000'
            assert restored_legs[0]['entry_price'] == 500.0
        finally:
            os.unlink(tf.name)

    def test_futures_legs_persist(self):
        """Verify FuturesSignalTrader legs can be serialized for DB storage."""
        from strategy.futures_signal_trader import FuturesSignalTrader
        trader = FuturesSignalTrader(
            signal_key='rsi_div_mss', asset='BTC', timeframe='15m',
            lots=1, api_key='t', api_secret='t', broker='demo'
        )
        # Simulate a filled trade
        trader.legs.append({
            'symbol': 'BTCUSD', 'side': 'buy', 'size': 1,
            'entry_price': 95000, 'sl': 94000, 'tp': 97000,
            'time': '10:30:00',
        })
        # Serialize legs (what DB save would do)
        serialized = json.dumps(trader.legs)
        restored = json.loads(serialized)
        assert restored[0]['symbol'] == 'BTCUSD'
        assert restored[0]['entry_price'] == 95000


# ═══════════════════════════════════════════════════════════════════════
# TEST: Live P&L Computation
# ═══════════════════════════════════════════════════════════════════════

class TestLivePnL:
    """Test compute_live_legs enrichment with mock prices."""

    @patch('api.live_pnl.get_current_price')
    def test_compute_live_legs_short_strangle(self, mock_price):
        mock_price.side_effect = lambda pid, asset: {
            'mark_price': {1001: 480.0, 1002: 430.0}.get(pid, 500)
        }
        from api.live_pnl import compute_live_legs
        legs = [
            {'product_id': 1001, 'symbol': 'C-BTC-100000', 'side': 'sell',
             'size': 10, 'entry_price': 500.0, 'type': 'call'},
            {'product_id': 1002, 'symbol': 'P-BTC-90000', 'side': 'sell',
             'size': 10, 'entry_price': 450.0, 'type': 'put'},
        ]
        enriched, total_pnl = compute_live_legs(legs, 'BTC')
        # Short sell: profit when price drops
        # Call: (500-480)*10*0.001 = 0.20
        # Put:  (450-430)*10*0.001 = 0.20
        assert total_pnl == pytest.approx(0.40, abs=0.01)
        assert enriched[0]['current_mark'] == 480.0
        assert enriched[1]['current_mark'] == 430.0

    @patch('api.live_pnl.get_current_price')
    def test_live_legs_loss_scenario(self, mock_price):
        mock_price.side_effect = lambda pid, asset: {'mark_price': 600.0}
        from api.live_pnl import compute_live_legs
        legs = [{'product_id': 1001, 'symbol': 'C', 'side': 'sell',
                 'size': 10, 'entry_price': 500.0}]
        _, total_pnl = compute_live_legs(legs, 'BTC')
        # (500-600)*10*0.001 = -1.0
        assert total_pnl == pytest.approx(-1.0, abs=0.01)

    @patch('api.live_pnl.get_current_price', side_effect=Exception("API down"))
    def test_api_failure_uses_entry_as_fallback(self, mock_price):
        from api.live_pnl import compute_live_legs
        legs = [{'product_id': 1001, 'symbol': 'C', 'side': 'sell',
                 'size': 10, 'entry_price': 500.0}]
        enriched, total_pnl = compute_live_legs(legs, 'BTC')
        # Fallback to entry = no P&L change
        assert total_pnl == 0
        assert enriched[0]['current_mark'] == 500.0


# ═══════════════════════════════════════════════════════════════════════
# TEST: StrategyMonitor — Monitoring Loop & Exit
# ═══════════════════════════════════════════════════════════════════════

class TestStrategyMonitorExecution:
    """Test monitor detects target/SL and closes legs."""

    def _make_monitor(self, legs=None):
        from strategy.monitor import StrategyMonitor
        if legs is None:
            legs = [
                {'symbol': 'C-BTC-100000', 'product_id': 1001, 'type': 'call',
                 'strike': 100000, 'side': 'sell', 'size': 10, 'entry_price': 500.0},
                {'symbol': 'P-BTC-90000', 'product_id': 1002, 'type': 'put',
                 'strike': 90000, 'side': 'sell', 'size': 10, 'entry_price': 450.0},
            ]
        return StrategyMonitor(legs=legs, max_profit=0.5, max_loss=0.5,
                               asset='BTC', lot_size=0.001, interval=0.1)

    @patch('strategy.monitor.place_order', side_effect=mock_place_order)
    @patch('strategy.monitor.get_current_price')
    def test_exits_on_max_profit(self, mock_price, mock_order):
        """Monitor should close when P&L exceeds max_profit."""
        # Prices dropped a lot → profit for short
        mock_price.side_effect = lambda pid, asset: {
            'mark_price': {1001: 200.0, 1002: 150.0}.get(pid, 500)
        }
        mon = self._make_monitor()
        mon.start()
        time.sleep(0.5)
        # Should have exited
        assert mon.running is False
        assert mon.exit_reason is not None
        assert mock_order.called  # closed the legs

    @patch('strategy.monitor.place_order', side_effect=mock_place_order)
    @patch('strategy.monitor.get_current_price')
    def test_exits_on_max_loss(self, mock_price, mock_order):
        """Monitor should close when loss exceeds max_loss."""
        # Prices went up a lot → loss for short
        mock_price.side_effect = lambda pid, asset: {
            'mark_price': {1001: 900.0, 1002: 850.0}.get(pid, 500)
        }
        mon = self._make_monitor()
        mon.start()
        time.sleep(0.5)
        assert mon.running is False
        assert mock_order.called


# ═══════════════════════════════════════════════════════════════════════
# TEST: TrackedStrategy Registry
# ═══════════════════════════════════════════════════════════════════════

class TestTrackedStrategy:

    @patch('strategy.tracker.get_current_price', side_effect=mock_get_current_price)
    @patch('strategy.tracker.place_order', side_effect=mock_place_order)
    def test_registry_tracks_strategy(self, mock_order, mock_price):
        from strategy.tracker import TrackedStrategy, registry
        legs = [
            {'symbol': 'C-BTC', 'product_id': 1001, 'side': 'sell',
             'size': 10, 'entry_price': 500.0},
        ]
        ts = TrackedStrategy(sid='tr001', source='test', name='Test',
                             user_id=1, legs=legs, max_profit=10, max_loss=10,
                             interval=0.1)
        registry.register(ts)
        assert registry.get('tr001') is ts
        assert ts in registry.get_user_strategies(1)
        # Cleanup
        ts.running = False
        registry.close('tr001')

    def test_status_dict_complete(self):
        from strategy.tracker import TrackedStrategy
        ts = TrackedStrategy(sid='tr002', source='test', name='Test', user_id=1)
        status = ts.get_status()
        assert 'sid' in status
        assert 'status' in status
        assert 'legs' in status
        assert 'started_at' in status


# ═══════════════════════════════════════════════════════════════════════
# TEST: Order Execution — place_order API payload
# ═══════════════════════════════════════════════════════════════════════

class TestPlaceOrderAPI:
    """Verify the order payload sent to Delta Exchange."""

    @patch('api.orders.requests.post')
    @patch('api.orders.get_headers', return_value={'api-key': 'test'})
    @patch('api.orders.config')
    def test_options_order_includes_product_id(self, mock_cfg, mock_hdr, mock_post):
        mock_cfg.BASE_URL = 'https://test.com'
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {'success': True, 'result': {'id': 1}}
        mock_post.return_value = mock_resp

        from api.orders import place_order
        place_order(1001, 'C-BTC-100000', 10, 'sell')

        sent = json.loads(mock_post.call_args.kwargs.get('data', mock_post.call_args[1].get('data', '{}')))
        assert sent['product_id'] == 1001
        assert sent['product_symbol'] == 'C-BTC-100000'

    @patch('api.orders.requests.post')
    @patch('api.orders.get_headers', return_value={'api-key': 'test'})
    @patch('api.orders.config')
    def test_futures_order_no_product_id(self, mock_cfg, mock_hdr, mock_post):
        mock_cfg.BASE_URL = 'https://test.com'
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {'success': True, 'result': {'id': 2}}
        mock_post.return_value = mock_resp

        from api.orders import place_order
        place_order(None, 'BTCUSD', 1, 'buy')

        sent = json.loads(mock_post.call_args.kwargs.get('data', mock_post.call_args[1].get('data', '{}')))
        assert 'product_id' not in sent
        assert sent['product_symbol'] == 'BTCUSD'
        assert sent['size'] == 1
        assert sent['side'] == 'buy'


# ═══════════════════════════════════════════════════════════════════════
# TEST: OOP & Design Principles
# ═══════════════════════════════════════════════════════════════════════

class TestOOPDesign:
    """Verify adherence to OOP/SOLID principles."""

    def test_base_strategy_is_abstract(self):
        """BaseStrategy cannot be instantiated directly."""
        from strategy.base import BaseStrategy
        with pytest.raises(TypeError):
            BaseStrategy()

    def test_all_strategies_implement_interface(self):
        """All strategies implement BaseStrategy ABC methods."""
        from strategy.base import BaseStrategy
        from strategy import DeltaNeutralStrategy
        from strategy.iv_crush import IVCrushStrategy
        from strategy.call_ratio import CallRatioStrategy

        for cls in [DeltaNeutralStrategy, IVCrushStrategy, CallRatioStrategy]:
            assert issubclass(cls, BaseStrategy)
            # Check required methods exist
            assert hasattr(cls, 'initialize')
            assert hasattr(cls, 'monitor')
            assert hasattr(cls, 'close_all')
            assert hasattr(cls, 'pnl')

    def test_futures_traders_not_base_strategy(self):
        """FuturesSignalTrader and PrevDayBreakout are NOT BaseStrategy subclasses
        (design choice — they're signal scanners, not full-lifecycle strategies)."""
        from strategy.futures_signal_trader import FuturesSignalTrader
        from strategy.prev_day_breakout import PrevDayBreakoutTrader
        from strategy.base import BaseStrategy
        assert not issubclass(FuturesSignalTrader, BaseStrategy)
        assert not issubclass(PrevDayBreakoutTrader, BaseStrategy)

    def test_single_responsibility_compute_leg_pnl(self):
        """compute_leg_pnl is a pure function — no side effects."""
        from api.live_pnl import compute_leg_pnl
        # Same inputs → same output (deterministic)
        r1 = compute_leg_pnl(100, 120, 10, 'buy', 0.001)
        r2 = compute_leg_pnl(100, 120, 10, 'buy', 0.001)
        assert r1 == r2

    def test_strategy_monitor_dependency_injection(self):
        """StrategyMonitor accepts on_complete callback — dependency injection."""
        from strategy.monitor import StrategyMonitor
        cb = MagicMock()
        m = StrategyMonitor(legs=[], max_profit=10, max_loss=10, on_complete=cb)
        assert m.on_complete is cb

    def test_config_uses_thread_local_for_multi_tenancy(self):
        """Config supports thread-local credentials for multi-user isolation."""
        from config import set_thread_credentials, get_api_key, get_api_secret
        set_thread_credentials('key123', 'secret456', 'demo')
        assert get_api_key() == 'key123'
        assert get_api_secret() == 'secret456'

    def test_tracker_registry_encapsulation(self):
        """StrategyRegistry encapsulates strategy collection properly."""
        from strategy.tracker import StrategyRegistry
        reg = StrategyRegistry()
        assert hasattr(reg, '_strategies')
        assert hasattr(reg, '_lock')
        # Public interface
        assert callable(getattr(reg, 'register', None))
        assert callable(getattr(reg, 'get', None))
        assert callable(getattr(reg, 'close', None))
