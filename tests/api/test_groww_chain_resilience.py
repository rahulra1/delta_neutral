"""Tests for api.groww.get_groww_chain resilience (mirrors pricing resilience):
  #1/#2 single-flight shared refresh (one SDK call serves many concurrent callers)
  #3    exponential backoff after failures (skip network inside the window)
  #4    serve stale cache during an outage instead of (None, None, None)

All SDK/network access is mocked via api.groww._get_client, so these run offline
with no credentials.
"""
import time
import threading
from unittest.mock import patch, MagicMock

import pytest

import api.groww as groww


def _reset_state():
    with groww._chain_cache_lock:
        groww._chain_cache.clear()
    with groww._chain_refresh_locks_guard:
        groww._chain_refresh_locks.clear()
        groww._chain_backoff.clear()


def _fake_client(strikes=None):
    """A fake Groww SDK client whose get_option_chain returns a minimal payload."""
    client = MagicMock()
    client.get_option_chain.return_value = {
        'underlying_ltp': 22729.2,
        'strikes': strikes if strikes is not None else {
            '22750': {
                'CE': {'trading_symbol': 'NIFTY-CE-22750', 'ltp': 151.15,
                       'open_interest': 25155, 'volume': 100,
                       'greeks': {'delta': 0.5, 'iv': 12.0}},
                'PE': {'trading_symbol': 'NIFTY-PE-22750', 'ltp': 154.05,
                       'open_interest': 16746, 'volume': 90,
                       'greeks': {'delta': -0.5, 'iv': 12.0}},
            },
        },
    }
    return client


class TestGrowwChainResilience:

    def setup_method(self):
        _reset_state()

    def test_single_flight_one_call_serves_many(self):
        """Many concurrent cache-missing callers → ONE SDK get_option_chain call."""
        client = _fake_client()
        barrier = threading.Barrier(8)

        def slow_chain(*a, **k):
            time.sleep(0.1)
            return client.get_option_chain.return_value

        client.get_option_chain.side_effect = slow_chain

        with patch('api.groww._get_client', return_value=client):
            results = [None] * 8

            def worker(i):
                barrier.wait()
                results[i] = groww.get_groww_chain('NIFTY', '13-10-2026')

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Everyone got a chain...
        assert all(r[0] and r[1] == 22729.2 for r in results)
        # ...but the SDK was called only ONCE (single-flight).
        assert client.get_option_chain.call_count == 1

    def test_cache_hit_avoids_second_call(self):
        client = _fake_client()
        with patch('api.groww._get_client', return_value=client):
            groww.get_groww_chain('NIFTY', '13-10-2026')
            groww.get_groww_chain('NIFTY', '13-10-2026')  # within TTL
        assert client.get_option_chain.call_count == 1

    def test_serves_stale_on_failure(self):
        """After a good fetch, if the SDK then fails, a stale-but-recent chain is
        served instead of (None, None, None) (fix #4)."""
        client = _fake_client()
        with patch('api.groww._get_client', return_value=client):
            chain, spot, exp = groww.get_groww_chain('NIFTY', '13-10-2026')
            assert chain and spot == 22729.2

            # Expire fresh window but stay within stale window.
            with groww._chain_cache_lock:
                groww._chain_cache[('NIFTY', '13-10-2026')]['ts'] = \
                    time.time() - (groww._CACHE_TTL + 1)
            # Clear backoff so a refresh is attempted, but make it fail.
            groww._chain_backoff[('NIFTY', '13-10-2026')]['next_try'] = 0.0
            client.get_option_chain.side_effect = Exception('API down')

            chain2, spot2, exp2 = groww.get_groww_chain('NIFTY', '13-10-2026')

        assert chain2 is not None       # stale served, not None
        assert spot2 == 22729.2

    def test_stale_expires_returns_none(self):
        client = _fake_client()
        with patch('api.groww._get_client', return_value=client):
            groww.get_groww_chain('NIFTY', '13-10-2026')
            with groww._chain_cache_lock:
                groww._chain_cache[('NIFTY', '13-10-2026')]['ts'] = \
                    time.time() - (groww._CHAIN_STALE_TTL + 1)
            groww._chain_backoff[('NIFTY', '13-10-2026')]['next_try'] = 0.0
            client.get_option_chain.side_effect = Exception('down')
            result = groww.get_groww_chain('NIFTY', '13-10-2026')
        assert result == (None, None, None)

    def test_backoff_skips_network(self):
        """Inside the backoff window after a failure, no SDK call is made (fix #3)."""
        client = _fake_client()
        client.get_option_chain.side_effect = Exception('down')
        with patch('api.groww._get_client', return_value=client):
            # First miss → fails → sets backoff.
            assert groww.get_groww_chain('NIFTY', '13-10-2026') == (None, None, None)
            first = client.get_option_chain.call_count
            assert first >= 1

            state = groww._chain_backoff[('NIFTY', '13-10-2026')]
            assert state['fail_count'] >= 1
            assert state['next_try'] > time.time()

            # Second call while backing off → must NOT hit the SDK again.
            before = client.get_option_chain.call_count
            assert groww.get_groww_chain('NIFTY', '13-10-2026') == (None, None, None)
            assert client.get_option_chain.call_count == before  # skipped
