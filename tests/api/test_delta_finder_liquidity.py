"""Tests for the delta_finder liquidity gate.

Groww's option chain can include far-OTM strikes that technically exist (stale
ltp > 0) but have zero OI and zero volume — orders on them fail. The liquidity
gate must drop these BEFORE scoring so a far illiquid strike can't win on
delta-closeness, while still falling back if the whole chain is illiquid.
"""
import pytest

from api.delta_finder import find_target_delta_options


def _row(strike, call_delta=None, put_delta=None, call_oi=0, put_oi=0,
         call_vol=0, put_vol=0, ltp=100.0):
    row = {'strike': strike, 'call': None, 'put': None}
    if call_delta is not None:
        row['call'] = {'trading_symbol': f'OPT-CE-{strike}', 'product_id': None,
                       'delta': call_delta, 'mark_price': ltp,
                       'oi': call_oi, 'volume': call_vol}
    if put_delta is not None:
        row['put'] = {'trading_symbol': f'OPT-PE-{strike}', 'product_id': None,
                      'delta': put_delta, 'mark_price': ltp,
                      'oi': put_oi, 'volume': put_vol}
    return row


class TestLiquidityGate:

    def test_illiquid_far_strike_rejected_in_favor_of_liquid(self):
        """A far strike with delta closer to target but ZERO OI/volume must lose
        to a liquid strike slightly further in delta."""
        chain = [
            # Closer to target 0.20 but completely illiquid (should be dropped)
            _row(23050, call_delta=0.20, call_oi=0, call_vol=0),
            # Slightly further from 0.20 but liquid (should win)
            _row(23000, call_delta=0.24, call_oi=84840, call_vol=1200),
        ]
        best_call, _ = find_target_delta_options(chain, target_delta=0.20, tolerance=0.10)
        assert best_call is not None
        assert best_call['strike_price'] == 23000      # liquid strike chosen
        assert best_call['oi'] > 0

    def test_zero_oi_but_has_volume_is_kept(self):
        """OI=0 but volume>0 still represents a real market — must be kept."""
        chain = [
            _row(23050, call_delta=0.20, call_oi=0, call_vol=50),
        ]
        best_call, _ = find_target_delta_options(chain, target_delta=0.20, tolerance=0.10)
        assert best_call is not None
        assert best_call['strike_price'] == 23050

    def test_all_illiquid_falls_back(self):
        """If every candidate is illiquid, we still return something (with a
        warning) rather than trading nothing."""
        chain = [
            _row(23050, call_delta=0.20, call_oi=0, call_vol=0),
            _row(23100, call_delta=0.18, call_oi=0, call_vol=0),
        ]
        best_call, _ = find_target_delta_options(chain, target_delta=0.20, tolerance=0.10)
        assert best_call is not None  # fallback: closest delta among illiquid

    def test_liquid_put_selected(self):
        chain = [
            _row(22000, put_delta=-0.20, put_oi=0, put_vol=0),       # illiquid
            _row(22050, put_delta=-0.23, put_oi=15000, put_vol=400),  # liquid
        ]
        _, best_put = find_target_delta_options(chain, target_delta=0.20, tolerance=0.10)
        assert best_put is not None
        assert best_put['strike_price'] == 22050
