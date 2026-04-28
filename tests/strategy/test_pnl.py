"""Tests for shared P&L utilities: compute_leg_pnl and calculate_total_pnl."""
import pytest


class TestComputeLegPnl:
    def _compute(self, entry, mark, size, side, cv):
        from api.live_pnl import compute_leg_pnl
        return compute_leg_pnl(entry, mark, size, side, cv)

    def test_buy_profit(self):
        # Buy: price went up → profit
        assert self._compute(100, 120, 10, 'buy', 0.001) == pytest.approx(0.20)

    def test_buy_loss(self):
        # Buy: price went down → loss
        assert self._compute(100, 80, 10, 'buy', 0.001) == pytest.approx(-0.20)

    def test_sell_profit(self):
        # Sell: price went down → profit
        assert self._compute(100, 80, 10, 'sell', 0.001) == pytest.approx(0.20)

    def test_sell_loss(self):
        # Sell: price went up → loss
        assert self._compute(100, 120, 10, 'sell', 0.001) == pytest.approx(-0.20)

    def test_zero_size(self):
        assert self._compute(100, 120, 0, 'buy', 0.001) == 0

    def test_zero_price_change(self):
        assert self._compute(100, 100, 10, 'sell', 0.001) == 0

    def test_large_contract_value(self):
        # ETH-style: cv=0.01
        assert self._compute(50, 60, 5, 'buy', 0.01) == pytest.approx(0.50)


class TestCalculateTotalPnl:
    def _calc(self, positions, call_price, put_price, call_pid, put_pid,
              call_cv, put_cv, cum_realized):
        from api.pnl import calculate_total_pnl
        return calculate_total_pnl(positions, call_price, put_price,
                                   call_pid, put_pid, call_cv, put_cv, cum_realized)

    def test_basic_short_strangle_profit(self):
        """Both legs dropped in price → profit for short seller."""
        positions = [
            {'product_id': 1, 'size': -10, 'entry_price': 500.0},
            {'product_id': 2, 'size': -10, 'entry_price': 450.0},
        ]
        realized, unrealized, total, c_info, p_info = self._calc(
            positions, 480.0, 430.0, 1, 2, 0.001, 0.001, 0
        )
        # Call: (500-480)*10*0.001 = 0.20
        # Put:  (450-430)*10*0.001 = 0.20
        assert unrealized == pytest.approx(0.40)
        assert total == pytest.approx(0.40)
        assert c_info is not None
        assert p_info is not None

    def test_with_cumulative_realized(self):
        positions = [
            {'product_id': 1, 'size': -10, 'entry_price': 500.0},
        ]
        realized, unrealized, total, c_info, p_info = self._calc(
            positions, 480.0, 0, 1, 2, 0.001, 0.001, 5.0
        )
        assert realized == 5.0
        assert total == pytest.approx(5.0 + (500 - 480) * 10 * 0.001)

    def test_no_positions(self):
        realized, unrealized, total, c_info, p_info = self._calc(
            [], 500.0, 450.0, 1, 2, 0.001, 0.001, 0
        )
        assert unrealized == 0
        assert total == 0
        assert c_info is None
        assert p_info is None

    def test_none_positions(self):
        realized, unrealized, total, c_info, p_info = self._calc(
            None, 500.0, 450.0, 1, 2, 0.001, 0.001, 0
        )
        assert total == 0

    def test_zero_size_skipped(self):
        positions = [
            {'product_id': 1, 'size': 0, 'entry_price': 500.0},
        ]
        realized, unrealized, total, c_info, p_info = self._calc(
            positions, 480.0, 430.0, 1, 2, 0.001, 0.001, 0
        )
        assert c_info is None  # skipped

    def test_unrelated_position_ignored(self):
        positions = [
            {'product_id': 999, 'size': -10, 'entry_price': 500.0},
        ]
        realized, unrealized, total, c_info, p_info = self._calc(
            positions, 480.0, 430.0, 1, 2, 0.001, 0.001, 0
        )
        assert unrealized == 0  # product_id 999 doesn't match 1 or 2

    def test_loss_scenario(self):
        """Prices went up → loss for short seller."""
        positions = [
            {'product_id': 1, 'size': -10, 'entry_price': 500.0},
            {'product_id': 2, 'size': -10, 'entry_price': 450.0},
        ]
        realized, unrealized, total, c_info, p_info = self._calc(
            positions, 600.0, 550.0, 1, 2, 0.001, 0.001, 0
        )
        # Call: (500-600)*10*0.001 = -1.0
        # Put:  (450-550)*10*0.001 = -1.0
        assert unrealized == pytest.approx(-2.0)
        assert total == pytest.approx(-2.0)
