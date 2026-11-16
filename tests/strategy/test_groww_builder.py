"""Groww execution for the Strategy Builder: monitor TP/SL + close, leg placement."""
from unittest import mock
import strategy.groww_exec as gx
from strategy.monitor import StrategyMonitor

LEGS = [
    {'product_id': 'NIFTY1', 'symbol': 'NIFTY1', 'trading_symbol': 'NIFTY1', 'type': 'CE',
     'strike': 100, 'side': 'sell', 'size': 65, 'entry_price': 100.0},
    {'product_id': 'NIFTY2', 'symbol': 'NIFTY2', 'trading_symbol': 'NIFTY2', 'type': 'PE',
     'strike': 90, 'side': 'buy', 'size': 65, 'entry_price': 10.0},
]


def _mon(**kw):
    return StrategyMonitor(legs=[dict(l) for l in LEGS], max_profit=500, max_loss=500,
                           asset='NIFTY', broker='groww', **kw)


def test_monitor_uses_quantity_pnl():
    m = _mon()
    assert m.lot_size == 1 and m._groww


def test_close_failure_keeps_retrying_and_no_on_complete():
    done = []
    m = _mon(on_complete=lambda p, r: done.append((p, r)))
    m.running = True
    with mock.patch.object(gx, 'close_legs', return_value=[m.legs[0]]):
        m.exit_reason = 'max_loss'
        m._close_all()
    assert m.running and m._closing and not done
    with mock.patch.object(gx, 'close_legs', return_value=[]):
        m._close_all()
    assert not m.running and done


def test_close_legs_buys_back_shorts_first_and_reports_failures():
    calls = []

    def fake_place(trading_symbol, quantity, transaction_type, **k):
        calls.append((trading_symbol, transaction_type))
        return {'groww_order_id': 'x'} if trading_symbol == 'NIFTY1' else {'error': 'rejected'}
    with mock.patch('api.groww.place_order', fake_place), \
         mock.patch('api.groww.confirm_order_filled', return_value=(True, 'EXECUTED')):
        failed = gx.close_legs(list(reversed(LEGS)), log=lambda *_: None)
    assert calls == [('NIFTY1', 'BUY'), ('NIFTY2', 'SELL')]
    assert [l['symbol'] for l in failed] == ['NIFTY2']


def test_place_legs_buy_first_and_stops_on_failure():
    chain = [{'strike': '100', 'call': {'trading_symbol': 'C100', 'mark_price': 50, 'strike': '100'},
              'put': {'trading_symbol': 'P100', 'mark_price': 40, 'strike': '100'}}]
    cfg = [{'type': 'CE', 'side': 'sell', 'strike': 'ATM', 'lots': 1},
           {'type': 'PE', 'side': 'buy', 'strike': 'ATM', 'lots': 1}]
    order = []

    def fake_place(trading_symbol, quantity, transaction_type, **k):
        order.append((trading_symbol, transaction_type, quantity))
        return {'groww_order_id': trading_symbol}
    with mock.patch('api.groww.place_order', fake_place), \
         mock.patch('api.groww.confirm_order_filled', return_value=(True, 'EXECUTED')):
        placed, results, err = gx.place_legs(cfg, chain, 100, 'NIFTY', 1, log=lambda *_: None)
    assert err is None and [o[1] for o in order] == ['BUY', 'SELL']
    assert order[0][2] == gx.lot_size_for('NIFTY')
    assert len(placed) == 2
    # buy leg rejected -> sell leg never sent
    order.clear()
    with mock.patch('api.groww.place_order', lambda **k: {'error': 'no margin'}):
        placed, results, err = gx.place_legs(cfg, chain, 100, 'NIFTY', 1, log=lambda *_: None)
    assert placed == [] and len(results) == 1 and not results[0]['success']


def test_place_legs_unresolvable_leg_places_nothing():
    with mock.patch('api.groww.place_order') as po:
        placed, results, err = gx.place_legs(
            [{'type': 'CE', 'side': 'sell', 'strike': 'ATM'}],
            [{'strike': '100', 'call': None, 'put': None}], 100, 'NIFTY', 1)
    assert err and not placed and not po.called


def test_market_hours():
    from datetime import datetime
    assert gx.market_open(datetime(2026, 7, 15, 10, 0, tzinfo=gx.IST))
    assert not gx.market_open(datetime(2026, 7, 15, 16, 0, tzinfo=gx.IST))
    assert not gx.market_open(datetime(2026, 7, 18, 10, 0, tzinfo=gx.IST))


def test_scheduler_groww_entry_arms_groww_monitor():
    from strategy.builder_scheduler import BuilderScheduler
    cfg = {'legs': [{'type': 'CE', 'side': 'sell', 'strike': 'ATM', 'lots': 1}],
           'expiry': 'weekly', 'execution': {'lots': 1}, 'risk': {'sl_pct': 100, 'target_pct': 50}}
    s = BuilderScheduler(cfg, asset='NIFTY', profile_id=None)
    s._broker, s._running = 'groww', True
    chain = [{'strike': '100', 'call': {'trading_symbol': 'C100', 'mark_price': 50, 'strike': '100'}, 'put': None}]
    started = []
    with mock.patch('api.groww.get_groww_expiries', return_value=['30-12-2099']), \
         mock.patch('api.groww.get_groww_chain', return_value=(chain, 100, '30-12-2099')), \
         mock.patch('api.groww.place_order', return_value={'groww_order_id': 'o1'}), \
         mock.patch('api.groww.confirm_order_filled', return_value=(True, 'EXECUTED')), \
         mock.patch.object(StrategyMonitor, 'start', lambda self: started.append(self)), \
         mock.patch('threading.Thread'), \
         mock.patch.object(s, '_persist_state'), mock.patch.object(s, '_track_legs'):
        s._run_one_entry()
    assert len(started) == 1 and started[0]._groww
    assert s._cv == 1 and s.total_entries == 1
    # TP = 50% of net premium (50 * 65 qty)
    assert started[0].max_profit == 50 * gx.lot_size_for('NIFTY') * 0.5


def test_compute_live_legs_uses_groww_marks_and_quantity():
    from api.live_pnl import compute_live_legs
    legs = [dict(LEGS[0]), dict(LEGS[1])]
    with mock.patch.object(gx, 'get_marks', return_value={'NIFTY1': 80.0, 'NIFTY2': 20.0}):
        out, total = compute_live_legs(legs, 'NIFTY')
    # sell 100->80: +20*65 ; buy 10->20: +10*65
    assert out[0]['current_mark'] == 80.0 and total == 30 * 65
