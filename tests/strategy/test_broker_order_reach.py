"""Order-reaches-broker tests: only the Groww SDK client is faked, so the strategy code runs through the real
api.groww.place_order / confirm_order_filled / _live_order and we assert the exact payload Groww would get."""
from datetime import datetime
from unittest import mock

import api.groww as gw
import strategy.nse_scanner_overnight as so
from strategy.nse_scanner_overnight import NseScannerOvernight

IST = so.IST


class FakeGroww:
    """Stands in for growwapi.GrowwAPI. Records every call; orders fill instantly unless told otherwise."""

    def __init__(self, status='EXECUTED', fill=21.5):
        self.orders, self.status, self.fill = [], status, fill

    def place_order(self, **kw):
        self.orders.append(kw)
        return {'groww_order_id': f'GO{len(self.orders)}', 'order_status': 'NEW'}

    def get_order_status(self, groww_order_id, segment):
        return {'groww_order_id': groww_order_id, 'order_status': self.status, 'average_fill_price': self.fill}

    def cancel_order(self, **kw):
        return {}

    def get_ltp(self, segment, exchange_trading_symbols):
        keys = [exchange_trading_symbols] if isinstance(exchange_trading_symbols, str) else exchange_trading_symbols
        return {k: 22.0 for k in keys}


CONTRACT = {'trading_symbol': 'RELIANCE26DEC1300CE', 'kind': 'CE', 'strike': 1300, 'expiry': '2026-12-29',
            'qty': 500, 'lot_size': 500, 'ltp': 20.0, 'spot': 1300.0, 'iv': 25}


def _scan(s, now):
    overview = {'market': {'status': 'Open'},
                'sectors': [{'index': 'NIFTY ENERGY', 'group': 'Sectoral', 'bias': 'BULLISH', 'pchange': 1.2}]}
    detail = {'bias': 'BULLISH', 'index': 'NIFTY ENERGY', 'pchange': 1.2,
              'stocks': [{'symbol': 'RELIANCE', 'direction': 'LONG', 'setup': 'LONG'}]}
    with mock.patch('api.sector_scanner.get_sectors', return_value=overview), \
         mock.patch('api.sector_scanner.get_sector_stocks', return_value=detail), \
         mock.patch.object(s, '_atm_contract', return_value=(dict(CONTRACT), None)):
        s._scan_and_enter(now)


def test_overnight_live_buy_and_sell_reach_groww_with_correct_payload():
    fake = FakeGroww(fill=21.5)
    s = NseScannerOvernight(dry_run=False)
    s._running = True
    with mock.patch.object(gw, '_get_client', return_value=fake):
        _scan(s, datetime(2026, 12, 1, 15, 16, tzinfo=IST))
        # --- BUY reached the broker
        assert len(fake.orders) == 1
        buy = fake.orders[0]
        assert buy == dict(trading_symbol='RELIANCE26DEC1300CE', quantity=500, validity='DAY', exchange='NSE',
                           segment='FNO', product='NRML', order_type='MARKET', transaction_type='BUY')
        leg = s._open_legs()[0]
        assert leg['entry_price'] == 21.5 and leg['order_id'] == 'GO1'     # fill price from broker, not the LTP

        # --- next day 09:18: SELL reaches the broker
        fake.fill = 24.0
        with mock.patch.object(s, '_market_open', return_value=True), \
             mock.patch.object(s, '_marks', return_value=(1310.0, 23.0)):
            s._exit_overnight(datetime(2026, 12, 2, 9, 18, tzinfo=IST))
    assert len(fake.orders) == 2
    sell = fake.orders[1]
    assert sell['transaction_type'] == 'SELL' and sell['quantity'] == 500 and sell['product'] == 'NRML' \
        and sell['order_type'] == 'MARKET' and sell['trading_symbol'] == 'RELIANCE26DEC1300CE'
    assert s._open_legs() == [] and s.trade_log[-1]['price'] == 24.0 and s.trade_log[-1]['order_id'] == 'GO2'


def test_overnight_paper_sends_nothing_to_broker():
    fake = FakeGroww()
    s = NseScannerOvernight(dry_run=True)
    s._running = True
    with mock.patch.object(gw, '_get_client', return_value=fake):
        _scan(s, datetime(2026, 12, 1, 15, 16, tzinfo=IST))
        with mock.patch.object(s, '_market_open', return_value=True), \
             mock.patch.object(s, '_marks', return_value=(1310.0, 23.0)):
            s._exit_overnight(datetime(2026, 12, 2, 9, 18, tzinfo=IST))
    assert fake.orders == []


def test_rejected_buy_reaches_broker_but_is_not_recorded():
    fake = FakeGroww(status='REJECTED')
    s = NseScannerOvernight(dry_run=False)
    s._running = True
    with mock.patch.object(gw, '_get_client', return_value=fake):
        _scan(s, datetime(2026, 12, 1, 15, 16, tzinfo=IST))
    assert len(fake.orders) == 1 and s._open_legs() == []


def test_unfilled_sell_stays_open_and_is_resent():
    fake = FakeGroww()
    s = NseScannerOvernight(dry_run=False)
    s._running = True
    with mock.patch.object(gw, '_get_client', return_value=fake):
        _scan(s, datetime(2026, 12, 1, 15, 16, tzinfo=IST))
        fake.status = 'REJECTED'
        with mock.patch.object(s, '_market_open', return_value=True), \
             mock.patch.object(s, '_marks', return_value=(1310.0, 23.0)):
            s._exit_overnight(datetime(2026, 12, 2, 9, 18, tzinfo=IST))
            assert len(s._open_legs()) == 1 and s._open_legs()[0]['exit_pending']
            fake.status = 'EXECUTED'
            s._exit_overnight(datetime(2026, 12, 2, 9, 19, tzinfo=IST))
    sells = [o for o in fake.orders if o['transaction_type'] == 'SELL']
    assert len(sells) == 2 and s._open_legs() == []          # retried, then closed


def test_strategy_builder_groww_legs_reach_broker_buy_first():
    from strategy import groww_exec
    fake = FakeGroww(fill=50.0)
    chain = [{'strike': '24000', 'call': {'trading_symbol': 'NIFTY26DEC24000CE', 'mark_price': 50, 'strike': '24000'},
              'put': {'trading_symbol': 'NIFTY26DEC24000PE', 'mark_price': 40, 'strike': '24000'}}]
    cfg = [{'type': 'CE', 'side': 'sell', 'strike': 'ATM', 'lots': 1},
           {'type': 'PE', 'side': 'buy', 'strike': 'ATM', 'lots': 1}]
    with mock.patch.object(gw, '_get_client', return_value=fake):
        placed, results, err = groww_exec.place_legs(cfg, chain, 24000, 'NIFTY', 1, log=lambda *_: None)
    assert err is None and len(placed) == 2
    assert [(o['transaction_type'], o['trading_symbol']) for o in fake.orders] == \
        [('BUY', 'NIFTY26DEC24000PE'), ('SELL', 'NIFTY26DEC24000CE')]
    assert all(o['segment'] == 'FNO' and o['product'] == 'NRML' and o['order_type'] == 'MARKET'
               and o['quantity'] == groww_exec.lot_size_for('NIFTY') and o['exchange'] == 'NSE'
               for o in fake.orders)
