"""Tests for PortfolioStrangle early-monitoring behaviour.

Verifies the fix: monitoring must begin as soon as the FIRST slot's legs are
executed, rather than only after all entry slots are placed. Previously slots 1
and 2 ran unmonitored until the final (11:15) entry completed.
"""
import threading
import time
import pytest

from strategy.portfolio_strangle import PortfolioStrangle


@pytest.fixture
def strat(monkeypatch):
    s = PortfolioStrangle(asset='BTC', lot_size=1)
    # Make contract value deterministic
    monkeypatch.setattr('strategy.portfolio_strangle.get_contract_value', lambda a: 0.001)
    # No real persistence / DB
    monkeypatch.setattr(s, '_persist_state', lambda: None)
    s._running = True
    return s


def test_monitor_starts_after_first_slot(strat, monkeypatch):
    """Monitoring thread should be live while later entry slots are still pending."""
    monitor_started = threading.Event()
    entered_slots = []
    # Order in which events happen, to prove concurrency
    timeline = []

    # Each _open_strangle_slot returns one call+put leg pair
    def fake_open(tag):
        idx = len(entered_slots) + 1
        entered_slots.append(idx)
        timeline.append(f"enter_slot_{idx}")
        return [
            {'symbol': f'C{idx}', 'product_id': 10 + idx, 'side': 'sell', 'strike': 100000,
             'type': 'call', 'entry_price': 100.0, 'size': 1, 'sl_price': 300.0, 'stopped': False},
            {'symbol': f'P{idx}', 'product_id': 20 + idx, 'side': 'sell', 'strike': 90000,
             'type': 'put', 'entry_price': 100.0, 'size': 1, 'sl_price': 300.0, 'stopped': False},
        ]

    # Entry-time waits: slot 1 immediate; slots 2 and 3 block until we release them
    release_slot2 = threading.Event()

    def fake_wait_until_time(h, m):
        if entered_slots and not release_slot2.is_set():
            timeline.append("monitor_should_be_running_now")
            monitor_started.wait(timeout=2)   # prove monitor is alive before slot 2
            release_slot2.wait(timeout=2)

    # Monitor loop: record that it ran, then exit quickly
    real_monitor = strat._monitor_all_slots

    def wrapped_monitor(*args, **kwargs):
        timeline.append("monitor_started")
        monitor_started.set()
        # let entry loop proceed with slots 2 & 3
        release_slot2.set()
        # Force a fast exit: no legs data -> flip running off after one pass
        strat._running_ticks = getattr(strat, '_running_ticks', 0)
        # Stop after a brief moment
        def killer():
            time.sleep(0.2); strat._running = False
        threading.Thread(target=killer, daemon=True).start()
        return real_monitor(*args, **kwargs)

    monkeypatch.setattr(strat, '_open_strangle_slot', fake_open)
    monkeypatch.setattr(strat, '_wait_until_time', fake_wait_until_time)
    monkeypatch.setattr(strat, '_monitor_all_slots', wrapped_monitor)
    monkeypatch.setattr('strategy.portfolio_strangle.get_current_price',
                        lambda pid, a: {'mark_price': 100.0})
    monkeypatch.setattr(strat, 'monitor_interval', 0.02)

    strat._run_day_session('[TEST]', 1)

    # monitor must have started
    assert monitor_started.is_set(), "monitor never started"
    # And it started while slot 2 was still pending: 'monitor_started' must appear
    # before 'enter_slot_2' in the timeline.
    assert 'monitor_started' in timeline
    assert 'enter_slot_2' in timeline
    assert timeline.index('monitor_started') < timeline.index('enter_slot_2'), \
        f"monitor started too late; timeline={timeline}"
