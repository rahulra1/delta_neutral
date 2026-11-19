"""Scanner Overnight Options: PAPER default and LIVE (real Groww orders), Groww fully mocked."""
from datetime import datetime
from unittest import mock

import strategy.nse_scanner_overnight as so
from strategy.nse_scanner_overnight import NseScannerOvernight

IST = so.IST
CONTRACT = {'trading_symbol': 'RELIANCE26DEC1300CE', 'kind': 'CE', 'strike': 1300, 'expiry': '29-12-2026',
            'qty': 500, 'lot_size': 500, 'ltp': 20.0, 'spot': 1300.0, 'iv': 25}
SECTOR = {'index': 'NIFTY ENERGY', 'pchange': 1.2}
ROW = {'symbol': 'RELIANCE', 'direction': 'LONG', 'setup': 'LONG'}


def _s(dry_run):
    s = NseScannerOvernight(dry_run=dry_run)
    s._running = True
    return s


def _scan(s, live_order):
    overview = {'market': {'status': 'Open'},
                'sectors': [{'index': 'NIFTY ENERGY', 'group': 'Sectoral', 'bias': 'BULLISH', 'pchange': 1.2}]}
    detail = {'bias': 'BULLISH', 'index': 'NIFTY ENERGY', 'pchange': 1.2, 'stocks': [dict(ROW)]}
    now = datetime(2026, 12, 1, 15, 16, tzinfo=IST)
    with mock.patch('api.sector_scanner.get_sectors', return_value=overview), \
         mock.patch('api.sector_scanner.get_sector_stocks', return_value=detail), \
         mock.patch.object(s, '_atm_contract', return_value=(dict(CONTRACT), None)), \
         mock.patch.object(s, '_live_order', side_effect=live_order) as lo:
        s._scan_and_enter(now)
    return lo


def test_default_is_paper_and_places_no_orders():
    s = _s(True)
    assert s.dry_run and s._mode == 'PAPER' and s._base_params['dry_run'] is True
    lo = _scan(s, lambda *a, **k: (_ for _ in ()).throw(AssertionError('no orders in paper')))
    assert not lo.called and len(s._open_legs()) == 1
    assert s._open_legs()[0]['entry_price'] == 20.0


def test_live_buy_records_fill_price_and_order_id():
    s = _s(False)
    assert s._mode == 'LIVE' and s._base_params['dry_run'] is False
    lo = _scan(s, lambda sym, qty, side, fallback=None: (21.5, 'OID1', None))
    lo.assert_called_once_with('RELIANCE26DEC1300CE', 500, 'BUY', fallback=20.0)
    leg = s._open_legs()[0]
    assert leg['entry_price'] == 21.5 and leg['order_id'] == 'OID1'
    assert s.trade_log[0]['mode'] == 'LIVE'
    assert s._entered_day == '2026-12-01'


def test_live_buy_not_filled_is_not_recorded():
    s = _s(False)
    _scan(s, lambda *a, **k: (None, 'OID2', 'order OID2 REJECTED'))
    assert s._open_legs() == [] and s.total_trades == 0


def _open_leg(s):
    leg = {'symbol': 'RELIANCE26DEC1300CE', 'underlying': 'RELIANCE', 'type': 'call', 'strike': 1300,
           'expiry': '29-12-2026', 'side': 'buy', 'direction': 'LONG', 'qty': 500, 'size': 1, 'lot_size': 500,
           'entry_price': 20.0, 'current_mark': 20.0, 'current_pnl': 0.0, 'stock_entry': 1300.0,
           'stock_mark': 1300.0, 'sector': 'X', 'sector_pchg': 1.0, 'setup': 'LONG', 'rank': 1,
           'opened_at': datetime(2026, 12, 1, 15, 16, tzinfo=IST).isoformat(), 'stopped': False}
    s.legs = [leg]
    return leg


def test_live_exit_retries_until_sell_confirmed():
    s = _s(False)
    leg = _open_leg(s)
    with mock.patch.object(s, '_live_order', return_value=(None, None, 'rejected')):
        assert s._exit(leg, 'next_day_0917', opt=25.0) is False
    assert not leg['stopped'] and leg['exit_pending'] == 'next_day_0917' and s.cumulative_pnl == 0
    with mock.patch.object(s, '_live_order', return_value=(24.0, 'OID9', None)):
        assert s._exit(leg, 'next_day_0917', opt=25.0) is True
    assert leg['stopped'] and leg['exit_price'] == 24.0 and leg['exit_order_id'] == 'OID9'
    assert s.cumulative_pnl < (24.0 - 20.0) * 500           # net of charges
    assert s.trade_log[-1]['mode'] == 'LIVE'


def test_live_overnight_exit_sells_even_without_ltp():
    s = _s(False)
    _open_leg(s)
    now = datetime(2026, 12, 2, 9, 18, tzinfo=IST)
    with mock.patch.object(s, '_market_open', return_value=True), \
         mock.patch.object(s, '_marks', return_value=(None, None)), \
         mock.patch.object(s, '_live_order', return_value=(19.0, 'OID3', None)) as lo:
        s._exit_overnight(now)
    assert lo.call_args[0][2] == 'SELL' and s._open_legs() == []


def test_paper_overnight_exit_waits_for_ltp():
    s = _s(True)
    _open_leg(s)
    now = datetime(2026, 12, 2, 9, 18, tzinfo=IST)
    with mock.patch.object(s, '_market_open', return_value=True), \
         mock.patch.object(s, '_marks', return_value=(None, None)):
        s._exit_overnight(now)
    assert len(s._open_legs()) == 1                          # before 09:30 and no LTP: still waiting


def test_live_requires_groww_broker():
    s = _s(False)
    with mock.patch('config.get_api_key', return_value='k'), mock.patch('config.get_api_secret', return_value='s'), \
         mock.patch('config._thread_local', mock.Mock(broker='delta_exchange')):
        assert s.initialize() is False and not s._running


def test_app_builds_live_strategy_from_saved_params():
    import app
    s = app._build_sector_options({'mode': 'overnight', 'dry_run': False}, resume=True)
    assert isinstance(s, NseScannerOvernight) and not s.dry_run
    s = app._build_sector_options({'mode': 'overnight'}, resume=True)
    assert s.dry_run
