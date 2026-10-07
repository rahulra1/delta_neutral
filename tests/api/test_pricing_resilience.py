"""Tests for api.pricing resilience features:
  #1/#2 single-flight shared refresh (one network call serves many callers)
  #3    exponential backoff after failures (skip network inside the window)
  #4    serve stale cache during an outage instead of returning None
  #5    shorter HTTP timeout
"""
import time
import threading
from unittest.mock import patch, MagicMock

import pytest

import api.pricing as pricing


def _reset_state():
    with pricing._cache_lock:
        pricing._cache.clear()
    with pricing._group_locks_guard:
        pricing._group_locks.clear()
        pricing._group_state.clear()


def _fake_response(result):
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = {'success': True, 'result': result}
    return r


BOARD = [
    {'product_id': 1002, 'mark_price': '450.0', 'greeks': {'delta': '-0.20'}},
    {'product_id': 1003, 'mark_price': '120.0', 'greeks': {'delta': '-0.10'}},
]


class TestResilience:

    def setup_method(self):
        _reset_state()

    def test_timeout_is_short(self):
        assert pricing._HTTP_TIMEOUT == (3, 8)

    def test_single_flight_one_call_serves_many(self):
        """Many concurrent cache-missing callers → ONE network request."""
        call_count = {'n': 0}
        barrier = threading.Barrier(8)

        def slow_get(*a, **k):
            call_count['n'] += 1
            time.sleep(0.1)  # hold the single-flight lock briefly
            return _fake_response(BOARD)

        with patch('api.pricing.requests.get', side_effect=slow_get), \
             patch('api.pricing.get_headers', return_value={}):
            results = [None] * 8

            def worker(i):
                barrier.wait()
                results[i] = pricing.get_current_price(1002, 'BTC')

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # All callers got the price...
        assert all(r == {'mark_price': 450.0, 'delta': -0.20} for r in results)
        # ...but the network was hit only ONCE thanks to single-flight.
        assert call_count['n'] == 1

    def test_serves_stale_on_failure(self):
        """After a good fetch, if the API then fails, a stale-but-recent value is
        returned instead of None (fix #4)."""
        with patch('api.pricing.requests.get', return_value=_fake_response(BOARD)), \
             patch('api.pricing.get_headers', return_value={}):
            first = pricing.get_current_price(1002, 'BTC')
        assert first == {'mark_price': 450.0, 'delta': -0.20}

        # Expire the fresh window but stay within stale window.
        with pricing._cache_lock:
            pricing._cache[1002]['ts'] = time.time() - (pricing._CACHE_TTL + 1)
        # Clear backoff so a refresh is attempted, but make it fail.
        pricing._group_state['options:BTC']['next_try'] = 0.0

        with patch('api.pricing.requests.get', side_effect=Exception('API down')), \
             patch('api.pricing.get_headers', return_value={}):
            stale = pricing.get_current_price(1002, 'BTC')
        assert stale == {'mark_price': 450.0, 'delta': -0.20}  # stale served, not None

    def test_stale_expires_returns_none(self):
        with patch('api.pricing.requests.get', return_value=_fake_response(BOARD)), \
             patch('api.pricing.get_headers', return_value={}):
            pricing.get_current_price(1002, 'BTC')
        # Age beyond stale TTL.
        with pricing._cache_lock:
            pricing._cache[1002]['ts'] = time.time() - (pricing._STALE_TTL + 1)
        pricing._group_state['options:BTC']['next_try'] = 0.0
        with patch('api.pricing.requests.get', side_effect=Exception('down')), \
             patch('api.pricing.get_headers', return_value={}):
            assert pricing.get_current_price(1002, 'BTC') is None

    def test_backoff_skips_network(self):
        """Inside the backoff window after a failure, no network call is made (fix #3)."""
        # First call fails → sets a backoff window.
        with patch('api.pricing.requests.get', side_effect=Exception('down')) as g, \
             patch('api.pricing.get_headers', return_value={}):
            assert pricing.get_current_price(1002, 'BTC') is None
            first_calls = g.call_count

        state = pricing._group_state['options:BTC']
        assert state['fail_count'] == 1
        assert state['next_try'] > time.time()  # backoff window active

        # Second call while backing off → must NOT hit the network again.
        with patch('api.pricing.requests.get', side_effect=Exception('down')) as g2, \
             patch('api.pricing.get_headers', return_value={}):
            assert pricing.get_current_price(1003, 'BTC') is None
            assert g2.call_count == 0  # skipped due to backoff
