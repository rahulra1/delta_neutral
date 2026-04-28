"""Tests for TrackedStrategy and StrategyRegistry."""
import pytest
from unittest.mock import patch, MagicMock

MODULE = 'strategy.tracker'


def _make_leg(symbol='BTC-C-95000', product_id=1001, side='sell', size=10, entry_price=500.0):
    return {
        'symbol': symbol, 'product_id': product_id,
        'type': 'call', 'strike': 95000,
        'side': side, 'size': size, 'entry_price': entry_price,
    }


def _make_tracked(**overrides):
    from strategy.tracker import TrackedStrategy
    defaults = dict(
        sid='test-001', source='AlgoX DN', name='Test Strategy',
        user_id='user1', legs=[_make_leg(), _make_leg('BTC-P-90000', 1002, 'sell', 10, 450.0)],
        asset='BTC', lot_size=0.001, max_profit=10, max_loss=10, interval=1,
    )
    defaults.update(overrides)
    return TrackedStrategy(**defaults)


class TestTrackedStrategyInit:
    def test_defaults(self):
        s = _make_tracked()
        assert s.sid == 'test-001'
        assert s.source == 'AlgoX DN'
        assert s.user_id == 'user1'
        assert s.max_profit == 10
        assert s.max_loss == 10
        assert s.status == 'running'
        assert s.current_pnl == 0
        assert s.running is False
        assert len(s.legs) == 2

    def test_auto_generates_sid(self):
        from strategy.tracker import TrackedStrategy
        s = TrackedStrategy()
        assert len(s.sid) == 8

    def test_max_values_absolute(self):
        s = _make_tracked(max_profit=-5, max_loss=-8)
        assert s.max_profit == 5
        assert s.max_loss == 8

    def test_empty_legs(self):
        s = _make_tracked(legs=None)
        assert s.legs == []


class TestTrackedStrategyLog:
    def test_log_appends(self):
        s = _make_tracked()
        s.log("hello")
        logs = s.get_logs()
        assert len(logs) == 1
        assert "hello" in logs[0]

    def test_log_truncates(self):
        s = _make_tracked()
        for i in range(600):
            s.log(f"msg {i}")
        assert len(s.get_logs(last_n=600)) == 500  # capped at 500

    def test_get_logs_last_n(self):
        s = _make_tracked()
        for i in range(10):
            s.log(f"msg {i}")
        assert len(s.get_logs(last_n=3)) == 3


class TestTrackedStrategyStartMonitoring:
    @patch(f'{MODULE}.place_order')
    @patch(f'{MODULE}.compute_leg_pnl', return_value=0.5)
    @patch(f'{MODULE}.get_current_price', return_value={'mark_price': 480.0})
    def test_start_sets_running(self, mock_price, mock_pnl, mock_order):
        s = _make_tracked()
        with patch.object(s, '_save_to_db'):
            with patch('threading.Thread') as mock_thread:
                mock_thread.return_value = MagicMock()
                s.start_monitoring()
        assert s.running is True
        assert s.status == 'running'

    def test_no_legs_skips(self):
        s = _make_tracked(legs=None)
        with patch.object(s, '_save_to_db'):
            s.start_monitoring()
        assert s.running is False  # never started


class TestTrackedStrategyClose:
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    def test_close_running(self, mock_order):
        s = _make_tracked()
        s.running = True
        s.current_pnl = 5.0
        with patch.object(s, '_save_to_db'):
            result = s.close()
        assert result is True
        assert s.running is False
        assert s.status == 'completed'
        assert s.exit_reason == 'manual'
        assert mock_order.call_count == 2

    @patch(f'{MODULE}.place_order', return_value=None)
    def test_close_fails_when_orders_fail(self, mock_order):
        s = _make_tracked()
        s.running = True
        with patch.object(s, '_save_to_db'):
            result = s.close()
        assert result is False  # some legs failed

    def test_close_not_running(self):
        s = _make_tracked()
        s.running = False
        result = s.close()
        assert result is True
        assert s.status == 'closed'

    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    def test_on_complete_callback(self, mock_order):
        cb = MagicMock()
        s = _make_tracked()
        s.on_complete = cb
        s.running = True
        s.current_pnl = 7.0
        with patch.object(s, '_save_to_db'):
            s.close()
        cb.assert_called_once_with(7.0, 'manual')


class TestTrackedStrategyExit:
    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    def test_exit_target_hit(self, mock_order):
        s = _make_tracked()
        s.running = True
        s.current_pnl = 12.0
        with patch.object(s, '_save_to_db'):
            s._exit('target_hit')
        assert s.exit_reason == 'target_hit'
        assert s.running is False
        assert s.status == 'completed'
        assert mock_order.call_count == 2

    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    def test_exit_sl_hit(self, mock_order):
        s = _make_tracked()
        s.running = True
        with patch.object(s, '_save_to_db'):
            s._exit('sl_hit')
        assert s.exit_reason == 'sl_hit'


class TestTrackedStrategyGetStatus:
    def test_returns_full_status(self):
        s = _make_tracked()
        s.current_pnl = 3.5
        status = s.get_status()
        assert status['sid'] == 'test-001'
        assert status['source'] == 'AlgoX DN'
        assert status['pnl'] == 3.5
        assert status['max_profit'] == 10
        assert status['max_loss'] == 10
        assert len(status['legs']) == 2
        assert 'logs' in status
        assert 'pnl_history' in status

    def test_leg_details_in_status(self):
        s = _make_tracked()
        status = s.get_status()
        leg = status['legs'][0]
        assert leg['symbol'] == 'BTC-C-95000'
        assert leg['side'] == 'sell'
        assert leg['entry_price'] == 500.0


# --- StrategyRegistry Tests ---

class TestStrategyRegistry:
    def _make_registry(self):
        from strategy.tracker import StrategyRegistry
        return StrategyRegistry()

    def test_register_and_get(self):
        reg = self._make_registry()
        s = _make_tracked(sid='abc')
        reg.register(s)
        assert reg.get('abc') is s

    def test_get_nonexistent(self):
        reg = self._make_registry()
        assert reg.get('nope') is None

    def test_get_user_strategies(self):
        reg = self._make_registry()
        s1 = _make_tracked(sid='s1', user_id='u1')
        s2 = _make_tracked(sid='s2', user_id='u2')
        s3 = _make_tracked(sid='s3', user_id='u1')
        reg.register(s1)
        reg.register(s2)
        reg.register(s3)
        result = reg.get_user_strategies('u1')
        assert len(result) == 2
        assert all(s.user_id == 'u1' for s in result)

    def test_get_running(self):
        reg = self._make_registry()
        s1 = _make_tracked(sid='r1', user_id='u1')
        s1.running = True
        s2 = _make_tracked(sid='r2', user_id='u1')
        s2.running = False
        reg.register(s1)
        reg.register(s2)
        assert len(reg.get_running('u1')) == 1
        assert reg.get_running('u1')[0].sid == 'r1'

    def test_get_running_all_users(self):
        reg = self._make_registry()
        s1 = _make_tracked(sid='a1', user_id='u1')
        s1.running = True
        s2 = _make_tracked(sid='a2', user_id='u2')
        s2.running = True
        reg.register(s1)
        reg.register(s2)
        assert len(reg.get_running()) == 2

    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    def test_close_by_sid(self, mock_order):
        reg = self._make_registry()
        s = _make_tracked(sid='c1')
        s.running = True
        reg.register(s)
        with patch.object(s, '_save_to_db'):
            assert reg.close('c1') is True
        assert s.running is False

    def test_close_nonexistent(self):
        reg = self._make_registry()
        assert reg.close('nope') is False

    @patch(f'{MODULE}.place_order', return_value={'id': 1})
    def test_close_all_user(self, mock_order):
        reg = self._make_registry()
        s1 = _make_tracked(sid='ca1', user_id='u1')
        s1.running = True
        s2 = _make_tracked(sid='ca2', user_id='u1')
        s2.running = True
        s3 = _make_tracked(sid='ca3', user_id='u2')
        s3.running = True
        reg.register(s1)
        reg.register(s2)
        reg.register(s3)
        with patch.object(s1, '_save_to_db'), patch.object(s2, '_save_to_db'):
            count = reg.close_all('u1')
        assert count == 2
        assert not s1.running
        assert not s2.running
        assert s3.running  # different user, untouched

    def test_all_statuses(self):
        reg = self._make_registry()
        s1 = _make_tracked(sid='st1', user_id='u1')
        s2 = _make_tracked(sid='st2', user_id='u1')
        reg.register(s1)
        reg.register(s2)
        statuses = reg.all_statuses('u1')
        assert len(statuses) == 2
        assert all(isinstance(s, dict) for s in statuses)

    def test_cleanup_completed(self):
        reg = self._make_registry()
        s = _make_tracked(sid='old')
        s.running = False
        s.status = 'completed'
        s.started_at = '2020-01-01T00:00:00'  # very old
        reg.register(s)
        removed = reg.cleanup_completed(max_age_seconds=1)
        assert removed == 1
        assert reg.get('old') is None

    def test_cleanup_keeps_recent(self):
        reg = self._make_registry()
        s = _make_tracked(sid='new')
        s.running = False
        s.status = 'completed'
        # started_at is set to now by default
        reg.register(s)
        removed = reg.cleanup_completed(max_age_seconds=3600)
        assert removed == 0
        assert reg.get('new') is not None


class TestGlobalRegistry:
    def test_singleton_exists(self):
        from strategy.tracker import registry
        assert registry is not None
        from strategy.tracker import StrategyRegistry
        assert isinstance(registry, StrategyRegistry)
