"""Tests for EmaTrendFollower — focus on the exit-on-direction-change rule.

The strategy is long-only:
  - BULLISH coin in universe & not held -> BUY (open long)
  - held coin turns BEARISH               -> SELL (close long)

These tests mock all network calls (top coins, EMA signal, futures price,
product metadata) so they run offline and deterministically. dry_run=True
means no real orders are placed; positions are tracked in strategy.legs.
"""
import pytest
from unittest.mock import patch

from strategy.ema_trend_follower import EmaTrendFollower


# ---- helpers ---------------------------------------------------------------

def _coin(symbol, coin):
    return {'symbol': symbol, 'coin': coin, 'turnover_usd': 1e6, 'mark_price': 100.0}


def _signal(direction, spread=1.0):
    return {'direction': direction, 'ema_fast': 101.0, 'ema_slow': 100.0,
            'spread_pct': spread, 'bars': 200}


def _make_strategy():
    s = EmaTrendFollower(top_n=5, notional_usd=100, ema_resolution='1d', dry_run=True)
    s.initialize()
    return s


# A single-coin universe (SOLUSD) keeps the assertions simple.
UNIVERSE = [_coin('SOLUSD', 'SOL')]
PRICE = {'mark_price': 100.0, 'delta': 0}
META = (777, 1.0)  # (product_id, contract_value)


class TestExitOnDirectionChange:

    def _patched(self, direction_sequence):
        """Return a context manager that patches the strategy's dependencies.
        `direction_sequence` is a list of signal directions returned by
        successive ema_crossover_direction calls."""
        sigs = iter(direction_sequence)

        def _sig_side_effect(symbol, **kwargs):
            try:
                return _signal(next(sigs))
            except StopIteration:
                # Repeat the last direction if more calls happen than provided.
                return _signal(direction_sequence[-1])

        return _sig_side_effect

    def test_position_closed_when_direction_turns_bearish(self):
        """Core requirement: open a long while BULLISH, then it must be SOLD
        (closed) on the very next cycle once the signal flips to BEARISH."""
        s = _make_strategy()

        # Cycle 1: signal BULLISH -> entry pass buys SOLUSD.
        with patch('strategy.ema_trend_follower.get_top_coins_by_volume', return_value=UNIVERSE), \
             patch('strategy.ema_trend_follower.ema_crossover_direction', return_value=_signal('BULLISH')), \
             patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE), \
             patch.object(EmaTrendFollower, '_product_meta', return_value=META):
            s.evaluate_once()

        held = [l['symbol'] for l in s.legs]
        assert held == ['SOLUSD'], f"expected SOLUSD opened, got {held}"
        assert s.total_trades == 1

        # Cycle 2: signal now BEARISH -> exit pass must close SOLUSD.
        with patch('strategy.ema_trend_follower.get_top_coins_by_volume', return_value=UNIVERSE), \
             patch('strategy.ema_trend_follower.ema_crossover_direction', return_value=_signal('BEARISH')), \
             patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE), \
             patch.object(EmaTrendFollower, '_product_meta', return_value=META):
            s.evaluate_once()

        assert s.legs == [], f"position should be closed after bearish flip, still holding {s.legs}"
        # A SELL must have been recorded in the trade log.
        sells = [t for t in s.trade_log if t['action'] == 'SELL' and t['symbol'] == 'SOLUSD']
        assert len(sells) == 1
        assert sells[0]['reason'] == 'turned bearish'

    def test_position_held_while_still_bullish(self):
        """A coin that stays BULLISH must NOT be closed on subsequent cycles."""
        s = _make_strategy()
        common = dict(
            return_value=None,
        )
        with patch('strategy.ema_trend_follower.get_top_coins_by_volume', return_value=UNIVERSE), \
             patch('strategy.ema_trend_follower.ema_crossover_direction', return_value=_signal('BULLISH')), \
             patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE), \
             patch.object(EmaTrendFollower, '_product_meta', return_value=META):
            s.evaluate_once()  # open
            s.evaluate_once()  # still bullish -> keep

        assert [l['symbol'] for l in s.legs] == ['SOLUSD']
        # No sell should have happened, and it must not double-buy.
        assert not any(t['action'] == 'SELL' for t in s.trade_log)
        assert s.total_trades == 1, "should not re-buy a coin already held"

    def test_realized_pnl_booked_on_bearish_exit(self):
        """When closed, realized PnL = (exit - entry) * lots * contract_value
        must be added to cumulative_pnl. Entry @100, exit @110, 1 lot, cv=1 -> +10."""
        s = _make_strategy()
        entry_price = {'mark_price': 100.0, 'delta': 0}
        exit_price = {'mark_price': 110.0, 'delta': 0}

        with patch('strategy.ema_trend_follower.get_top_coins_by_volume', return_value=UNIVERSE), \
             patch('strategy.ema_trend_follower.ema_crossover_direction', return_value=_signal('BULLISH')), \
             patch('strategy.ema_trend_follower.get_futures_price', return_value=entry_price), \
             patch.object(EmaTrendFollower, '_product_meta', return_value=META):
            s.evaluate_once()

        assert len(s.legs) == 1
        assert s.legs[0]['entry_price'] == pytest.approx(100.0)
        assert s.legs[0]['size'] == 1  # $100 / (100 * 1) = 1 lot

        with patch('strategy.ema_trend_follower.get_top_coins_by_volume', return_value=UNIVERSE), \
             patch('strategy.ema_trend_follower.ema_crossover_direction', return_value=_signal('BEARISH')), \
             patch('strategy.ema_trend_follower.get_futures_price', return_value=exit_price), \
             patch.object(EmaTrendFollower, '_product_meta', return_value=META):
            s.evaluate_once()

        assert s.legs == []
        assert s.cumulative_pnl == pytest.approx(10.0), \
            f"expected +$10 realized, got {s.cumulative_pnl}"

    def test_neutral_signal_does_not_close(self):
        """A NEUTRAL signal on a held coin must NOT trigger an exit (only BEARISH does)."""
        s = _make_strategy()
        with patch('strategy.ema_trend_follower.get_top_coins_by_volume', return_value=UNIVERSE), \
             patch('strategy.ema_trend_follower.ema_crossover_direction', return_value=_signal('BULLISH')), \
             patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE), \
             patch.object(EmaTrendFollower, '_product_meta', return_value=META):
            s.evaluate_once()
        assert len(s.legs) == 1

        with patch('strategy.ema_trend_follower.get_top_coins_by_volume', return_value=UNIVERSE), \
             patch('strategy.ema_trend_follower.ema_crossover_direction', return_value=_signal('NEUTRAL')), \
             patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE), \
             patch.object(EmaTrendFollower, '_product_meta', return_value=META):
            s.evaluate_once()

        assert [l['symbol'] for l in s.legs] == ['SOLUSD'], "NEUTRAL must not close the position"
        assert not any(t['action'] == 'SELL' for t in s.trade_log)

    def test_close_all_closes_everything(self):
        """Stopping the strategy (close_all) must flatten all open positions."""
        multi_universe = [_coin('SOLUSD', 'SOL'), _coin('ETHUSD', 'ETH')]
        s = _make_strategy()
        with patch('strategy.ema_trend_follower.get_top_coins_by_volume', return_value=multi_universe), \
             patch('strategy.ema_trend_follower.ema_crossover_direction', return_value=_signal('BULLISH')), \
             patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE), \
             patch.object(EmaTrendFollower, '_product_meta', return_value=META):
            s.evaluate_once()
        assert len(s.legs) == 2

        with patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE):
            s.close_all()

        assert s.legs == []
        assert s._running is False
        sells = [t for t in s.trade_log if t['action'] == 'SELL']
        assert len(sells) == 2
        assert all(t['reason'] == 'shutdown' for t in sells)


class TestThreadSafety:
    """The strategy guards self.legs with self._legs_lock because WebSocket/monitor
    callbacks and the eval loop touch shared state. These tests exercise concurrent
    access to confirm there are no crashes, lost/duplicated legs, or lock issues."""

    def test_concurrent_enter_exit_no_corruption(self):
        """Hammer _enter and _exit from many threads and confirm legs stays
        internally consistent (no duplicate symbols, no leftover after balanced ops)."""
        import threading
        s = _make_strategy()

        symbols = [f'C{i}USD' for i in range(20)]

        def do_enter(sym):
            with patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE), \
                 patch.object(EmaTrendFollower, '_product_meta', return_value=META):
                s._enter(sym, sym[:-3], _signal('BULLISH'))

        def do_exit(sym):
            with patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE):
                s._exit(sym, reason='test')

        # Phase 1: concurrent entries
        threads = [threading.Thread(target=do_enter, args=(sym,)) for sym in symbols]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        held = [l['symbol'] for l in s.legs]
        assert sorted(held) == sorted(symbols), "all concurrent entries should be recorded"
        assert len(held) == len(set(held)), "no duplicate legs under concurrency"

        # Phase 2: concurrent exits (balanced) -> everything closed
        threads = [threading.Thread(target=do_exit, args=(sym,)) for sym in symbols]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert s.legs == [], f"all positions should be closed, leftover: {s.legs}"

    def test_pnl_read_during_concurrent_mutation(self):
        """Reading the pnl property while legs are being mutated must never raise
        (it snapshots under the lock before iterating)."""
        import threading
        s = _make_strategy()
        stop = threading.Event()
        errors = []

        def mutate():
            i = 0
            while not stop.is_set():
                sym = f'M{i % 10}USD'
                with patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE), \
                     patch.object(EmaTrendFollower, '_product_meta', return_value=META):
                    s._enter(sym, sym[:-3], _signal('BULLISH'))
                with patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE):
                    s._exit(sym, reason='churn')
                i += 1

        def read_pnl():
            while not stop.is_set():
                try:
                    with patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE):
                        _ = s.pnl
                except Exception as e:  # pragma: no cover
                    errors.append(e)

        writers = [threading.Thread(target=mutate) for _ in range(3)]
        readers = [threading.Thread(target=read_pnl) for _ in range(3)]
        for t in writers + readers:
            t.start()
        import time as _t
        _t.sleep(0.3)
        stop.set()
        for t in writers + readers:
            t.join()

        assert not errors, f"pnl read raised under concurrency: {errors[:3]}"

    def test_close_all_during_concurrent_entries(self):
        """close_all() while entries are still streaming in must not crash and must
        end with _running False (the shutdown signal wins)."""
        import threading
        s = _make_strategy()
        errors = []

        def enter_many():
            for i in range(30):
                try:
                    with patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE), \
                         patch.object(EmaTrendFollower, '_product_meta', return_value=META):
                        s._enter(f'X{i}USD', f'X{i}', _signal('BULLISH'))
                except Exception as e:  # pragma: no cover
                    errors.append(e)

        writer = threading.Thread(target=enter_many)
        writer.start()
        with patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE):
            s.close_all()
        writer.join()

        assert not errors, f"concurrent close/enter raised: {errors[:3]}"
        assert s._running is False


class TestDashboardIntegration:
    """Positions must be registered with the shared position_tracker so they
    appear in the dashboard 'open positions' panel, and removed on exit."""

    def test_entry_registers_with_position_tracker(self):
        from api.position_tracker import position_tracker
        uid = 9901
        s = _make_strategy()
        s._user_id = uid
        s._sid = 'dashtest'

        # Clean any leftover for this uid
        for p in list(position_tracker.get_user_positions(uid)):
            position_tracker.close(uid, p.product_id)

        with patch('strategy.ema_trend_follower.get_top_coins_by_volume', return_value=UNIVERSE), \
             patch('strategy.ema_trend_follower.ema_crossover_direction', return_value=_signal('BULLISH')), \
             patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE), \
             patch.object(EmaTrendFollower, '_product_meta', return_value=META):
            s.evaluate_once()

        tracked = position_tracker.get_user_positions(uid)
        syms = [p.symbol for p in tracked]
        assert 'SOLUSD' in syms, "entry should register the position with position_tracker"
        sol = next(p for p in tracked if p.symbol == 'SOLUSD')
        assert sol.source == 'EMA Trend'
        assert sol.side == 'buy'

        # Exit must deregister it
        with patch('strategy.ema_trend_follower.get_top_coins_by_volume', return_value=UNIVERSE), \
             patch('strategy.ema_trend_follower.ema_crossover_direction', return_value=_signal('BEARISH')), \
             patch('strategy.ema_trend_follower.get_futures_price', return_value=PRICE), \
             patch.object(EmaTrendFollower, '_product_meta', return_value=META):
            s.evaluate_once()

        tracked_after = [p.symbol for p in position_tracker.get_user_positions(uid)]
        assert 'SOLUSD' not in tracked_after, "exit should remove the position from the tracker"


