"""Restart safety + re-attach for the recurring Strategy Builder (BuilderScheduler).

Regression: on resume, initialize() ran BEFORE restore_state(); initialize() persists, so the
DB row was overwritten with an empty open_positions/legs and a second restart lost every open
position (the broker still held them). These tests pin the fixed order and the adopt path.
"""
from unittest.mock import patch

import pytest

from strategy.builder_scheduler import BuilderScheduler, _expiry_from_symbol

CONFIG = {'name': 'Iron Condor', 'expiry': 'weekly', 'legs': [],
          'execution': {'days': [0, 1, 2, 3, 4], 'square_off': '17:15'},
          'risk': {'target_pct': 50, 'sl_pct': 100}}
LEGS = [
    {'product_id': 101, 'symbol': 'C-BTC-90000-301026', 'type': 'call', 'strike': 90000, 'side': 'sell', 'size': 10, 'entry_price': 400.0},
    {'product_id': 102, 'symbol': 'C-BTC-95000-301026', 'type': 'call', 'strike': 95000, 'side': 'buy', 'size': 10, 'entry_price': 150.0},
    {'product_id': 103, 'symbol': 'P-BTC-70000-301026', 'type': 'put', 'strike': 70000, 'side': 'sell', 'size': 10, 'entry_price': 380.0},
    {'product_id': 104, 'symbol': 'P-BTC-65000-301026', 'type': 'put', 'strike': 65000, 'side': 'buy', 'size': 10, 'entry_price': 140.0},
]
SAVED = {**CONFIG, 'cumulative_pnl': 12.5, 'total_entries': 3,
         'trade_log': [{'entry_id': 2, 'pnl': 12.5}],
         'open_positions': [{'entry_id': 3, 'expiry': '30-10-2026', 'legs': LEGS}]}


def _sched():
    s = BuilderScheduler(config=dict(CONFIG), asset='BTC', profile_id=1)
    s._sid = 'fc536bcf'
    s._user_id = 7
    s._api_key, s._api_secret, s._broker = 'k', 's', 'delta_exchange'
    return s


@pytest.fixture
def writes():
    """Capture every DB write of the row; neutralise threads/monitors."""
    calls = []
    with patch('models.update_strategy_db', side_effect=lambda sid, **kw: calls.append(kw)), \
         patch.object(BuilderScheduler, '_rearm_position', return_value=None):
        yield calls


def test_resume_order_keeps_open_positions_in_db(writes):
    s = _sched()
    s.restore_state(details=SAVED, legs=[])           # fixed order (app.py resume)
    s.initialize()
    last = writes[-1]
    assert last['details']['open_positions'][0]['entry_id'] == 3
    assert len(last['legs']) == 4
    assert last['details']['cumulative_pnl'] == 12.5 and last['details']['total_entries'] == 3
    assert all(w['details']['open_positions'] for w in writes), 'no write may contain an empty state'


def test_old_order_would_have_wiped_the_row(writes):
    """Documents the bug: initialize() first persists an empty state."""
    s = _sched()
    s.initialize()
    assert writes[0]['details']['open_positions'] == [] and writes[0]['legs'] == []


def test_expiry_from_symbol():
    assert _expiry_from_symbol('C-BTC-90000-301026') == '30-10-2026'
    assert _expiry_from_symbol('BTCUSD') is None


def test_adopt_positions_rearms_tracks_and_persists(writes):
    s = _sched()
    s._running = True
    with patch('api.position_tracker.position_tracker') as pt:
        pt.get_user_positions.return_value = []
        eid = s.adopt_positions(LEGS)
    assert eid == 1 and s.total_entries == 1
    pos = s._open_positions[0]
    assert pos['expiry'] == '30-10-2026' and pos['adopted'] is True and len(pos['legs']) == 4
    assert pt.open.call_count == 4
    assert writes[-1]['details']['open_positions'][0]['entry_id'] == 1
    assert s.trade_log[-1]['action'] == 'ADOPT'


def test_adopt_skips_legs_already_owned(writes):
    s = _sched()
    s.restore_state(details=SAVED, legs=[])
    with patch('api.position_tracker.position_tracker') as pt:
        pt.get_user_positions.return_value = []
        assert s.adopt_positions(LEGS) is None          # all four already belong to entry #3
    assert len(s._open_positions) == 1


def test_restore_registers_legs_with_position_tracker(writes):
    s = _sched()
    with patch('api.position_tracker.position_tracker') as pt:
        pt.get_user_positions.return_value = []
        s.restore_state(details=SAVED, legs=[])
    srcs = {c.kwargs['source'] for c in pt.open.call_args_list}
    assert pt.open.call_count == 4 and srcs == {'Strategy Builder (Recurring)'}
