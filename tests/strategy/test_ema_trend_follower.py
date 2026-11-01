"""Tests for EmaTrendFollower (daily EMA10/30 long/short trend follower, v2).

All network calls are mocked: _fetch_universe (tickers), _fetch_daily (MARK/OI daily candles),
get_futures_price and _product_meta. dry_run=True so no orders are placed.
Decision-logic parity with the validated backtest is checked separately by
backtest/ema_trend_live_parity.py (295/295 identical trades).
"""
import threading
from unittest.mock import patch

import pytest

from strategy import ema_trend_follower as M
from strategy.ema_trend_follower import EmaTrendFollower, plan_day, coin_signal, ema_series, atr_pct_series

DAY = 86400
NOW = 1_800_000_000 // DAY * DAY + 600          # 00:10 UTC of some day
META = (777, 1.0)                               # (product_id, contract_value)


def _candles(closes, end_day=NOW // DAY * DAY - DAY, rng=0.04):
    """Closed daily candles ending yesterday; high/low = close +/- rng/2."""
    n = len(closes)
    return [(end_day - (n - 1 - i) * DAY, c, c * (1 + rng / 2), c * (1 - rng / 2), c)
            for i, c in enumerate(closes)]


UP = [100 + i for i in range(80)]                # EMA10 > EMA30
DOWN = [200 - i for i in range(80)]              # EMA10 < EMA30


def _strategy(**kw):
    s = EmaTrendFollower(dry_run=True, **kw)
    s.initialize()
    return s


def _run(s, series, oi_usd=1e6, price=100.0, now=NOW):
    """evaluate_once with mocked data. series: {symbol: closes}; BTCUSD must be included."""
    tickers = [{'symbol': sym, 'coin': sym[:-3], 'oi_value_usd': oi_usd, 'contract_value': 1.0}
               for sym in series]

    def fake_daily(self, symbol, now=None):
        if symbol not in series:
            return [], {}
        cs = _candles(series[symbol])
        return cs, {c[0]: oi_usd / c[4] for c in cs}          # OI contracts -> $oi_usd per day

    with patch.object(EmaTrendFollower, '_fetch_universe', return_value=tickers), \
         patch.object(EmaTrendFollower, '_fetch_daily', fake_daily), \
         patch('strategy.ema_trend_follower.get_futures_price', return_value={'mark_price': price}), \
         patch.object(EmaTrendFollower, '_product_meta', return_value=META), \
         patch.object(EmaTrendFollower, '_persist_state', return_value=None):
        s.evaluate_once(now=now)


# ---- pure helpers ----------------------------------------------------------------------------
def test_ema_seeded_with_first_close():
    e = ema_series([10, 10, 10, 20], 3)
    assert e[0] == 10 and e[-1] == pytest.approx(15.0)


def test_atr_pct_first_bar_uses_range():
    a = atr_pct_series([110], [90], [100])
    assert a[0] == pytest.approx(0.20)


def test_coin_signal_state_and_liquidity():
    g = coin_signal(_candles(UP), {}, 10, 30)
    assert g['state'] == 1 and g['age'] == 80
    g = coin_signal(_candles(DOWN), {}, 10, 30)
    assert g['state'] == -1


CFG = {'top_n': 10, 'ema_slow': 30, 'allow_shorts': True, 'btc_regime': True,
       'risk_per_atr_usd': 25.0, 'max_notional': 500.0, 'max_gross': 7500.0}


def _sig(state, liq=1e6, atr=0.05, age=80):
    return {'state': state, 'atr_pct': atr, 'liq30': liq, 'age': age, 'day': 0}


def test_plan_day_regime_gates_direction():
    sigs = {'AUSD': _sig(1), 'BUSD': _sig(-1)}
    _, e = plan_day(sigs, {}, {'_above_slow': True}, CFG)
    assert [(s, side) for s, side, _ in e] == [('AUSD', 1)]          # BTC up -> longs only
    _, e = plan_day(sigs, {}, {'_above_slow': False}, CFG)
    assert [(s, side) for s, side, _ in e] == [('BUSD', -1)]         # BTC down -> shorts only


def test_plan_day_sizing_and_caps():
    _, e = plan_day({'AUSD': _sig(1, atr=0.10)}, {}, {'_above_slow': True}, CFG)
    assert e[0][2] == pytest.approx(250.0)                           # $25 / 10% ATR
    _, e = plan_day({'AUSD': _sig(1, atr=0.01)}, {}, {'_above_slow': True}, CFG)
    assert e[0][2] == pytest.approx(500.0)                           # capped per coin
    many = {f'C{i}USD': _sig(1, liq=1e6 - i, atr=0.01) for i in range(20)}
    _, e = plan_day(many, {}, {'_above_slow': True}, dict(CFG, max_gross=1200.0))
    assert len(e) == 2                                               # gross cap 1200 / 500


def test_plan_day_universe_is_top_n_by_liquidity_and_age():
    sigs = {f'C{i}USD': _sig(1, liq=i) for i in range(15)}
    sigs['NEWUSD'] = _sig(1, liq=1e9, age=40)                        # too young (<= 50 days)
    _, e = plan_day(sigs, {}, {'_above_slow': True}, CFG)
    assert [s for s, _, _ in e] == [f'C{i}USD' for i in range(14, 4, -1)]


def test_plan_day_flip_exit_then_reentry_other_side():
    held = {'AUSD': {'side': 1, 'notional': 300}}
    x, e = plan_day({'AUSD': _sig(-1)}, held, {'_above_slow': False}, CFG)
    assert x == ['AUSD'] and [(s, side) for s, side, _ in e] == [('AUSD', -1)]


def test_plan_day_long_only():
    _, e = plan_day({'BUSD': _sig(-1)}, {}, {'_above_slow': False}, dict(CFG, allow_shorts=False))
    assert e == []


# ---- strategy integration ----------------------------------------------------------------------
def test_evaluate_opens_long_and_records_trade():
    s = _strategy()
    _run(s, {'BTCUSD': UP, 'SOLUSD': UP})
    syms = {l['symbol']: l for l in s.legs}
    assert set(syms) == {'BTCUSD', 'SOLUSD'}
    assert syms['SOLUSD']['side'] == 'buy'
    assert s.trade_log[-1]['action'] == 'BUY'
    assert s.last_eval_day == NOW // DAY * DAY


def test_flip_closes_and_books_pnl():
    s = _strategy()
    _run(s, {'BTCUSD': UP, 'SOLUSD': UP}, price=100.0)
    lots = next(l for l in s.legs if l['symbol'] == 'SOLUSD')['size']
    _run(s, {'BTCUSD': UP, 'SOLUSD': DOWN}, price=110.0, now=NOW + DAY)
    assert 'SOLUSD' not in {l['symbol'] for l in s.legs}             # long closed, BTC up -> no short
    close = [t for t in s.trade_log if t['action'] == 'CLOSE_BUY' and t['symbol'] == 'SOLUSD']
    assert close and close[0]['reason'] == 'ema_flip'
    assert s.cumulative_pnl == pytest.approx(10.0 * lots)


def test_skips_coin_when_one_lot_exceeds_cap():
    s = _strategy(notional_usd=50)
    _run(s, {'BTCUSD': UP, 'SOLUSD': UP}, price=100.0)                # 1 lot = $100 > $50 cap
    assert s.legs == []


def test_due_once_per_day_after_delay():
    s = _strategy()
    day = NOW // DAY * DAY
    assert s._due(day + 60) is False                                 # 00:01 UTC, before delay
    assert s._due(day + 6 * 60) is True
    s.last_eval_day = day
    assert s._due(day + 3600) is False                               # already done today
    assert s._due(day + DAY + 6 * 60) is True


def test_no_entries_without_btc_data():
    s = _strategy()
    _run(s, {'SOLUSD': UP})                                          # no BTCUSD candles
    assert s.legs == [] and s.last_eval_day == 0


def test_legacy_kwargs_accepted():
    s = EmaTrendFollower(top_n=50, min_oi_change_pct=10.0, tp_pct=0.1, sl_pct=0.05,
                         ema_resolution='1d', dry_run=True)
    assert s.top_n == 50 and s.ema_resolution == '1d'


def test_restore_state_roundtrip_and_version_gate():
    s = _strategy()
    legs = [{'symbol': 'SOLUSD', 'product_id': 1, 'coin': 'SOL', 'side': 'sell', 'size': 3,
             'entry_price': 150.0, 'contract_value': 1.0, 'opened_at': ''}]
    s.restore_state({'cumulative_pnl': 12.5, 'total_trades': 4, 'last_eval_day': 123,
                     'strategy_version': 'daily_ema_v2'}, legs)
    assert s.cumulative_pnl == 12.5 and s.total_trades == 4 and s.last_eval_day == 123
    assert s.legs[0]['notional'] == pytest.approx(450.0)
    s2 = _strategy()
    s2.restore_state({'last_eval_day': 123}, [])                     # old-version row
    assert s2.last_eval_day == 0


def test_close_all_flattens_and_stops():
    s = _strategy()
    _run(s, {'BTCUSD': UP, 'SOLUSD': UP, 'ETHUSD': UP})
    assert len(s.legs) == 3
    with patch('strategy.ema_trend_follower.get_futures_price', return_value={'mark_price': 100.0}), \
         patch.object(EmaTrendFollower, '_persist_state', return_value=None):
        s.close_all()
    assert s.legs == [] and s._running is False
    assert all(t['reason'] == 'shutdown' for t in s.trade_log if t['action'].startswith('CLOSE'))


def test_tracker_registration_source():
    s = _strategy()
    s._user_id = 42
    with patch('strategy.ema_trend_follower.position_tracker') as pt:
        _run(s, {'BTCUSD': UP, 'SOLUSD': UP})
    kwargs = [c.kwargs for c in pt.open.call_args_list]
    assert kwargs and all(k['source'] == 'EMA Trend' for k in kwargs)


def test_pnl_read_during_concurrent_mutation():
    s = _strategy()
    errors, stop = [], threading.Event()

    def reader():
        with patch('api.pricing.get_futures_prices_bulk', return_value={}):
            while not stop.is_set():
                try:
                    _ = s.pnl
                except Exception as e:      # pragma: no cover
                    errors.append(e)

    t = threading.Thread(target=reader)
    t.start()
    for i in range(30):
        with patch('strategy.ema_trend_follower.get_futures_price', return_value={'mark_price': 10.0}), \
             patch.object(EmaTrendFollower, '_product_meta', return_value=META):
            s._enter(f'C{i}USD', 'C', 'buy', 100.0)
            s._exit(f'C{i}USD', reason='test')
    stop.set()
    t.join()
    assert errors == [] and s.legs == []
