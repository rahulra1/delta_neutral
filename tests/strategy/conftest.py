"""Shared fixtures for strategy tests."""
import pytest


def make_option(symbol='BTC-C-100000', product_id=1001, strike=100000,
                mark_price=500.0, delta=0.20, contract_type='call_options',
                iv=0.8, spot_price=95000):
    return {
        'symbol': symbol, 'product_id': product_id,
        'strike_price': strike, 'mark_price': mark_price,
        'delta': delta, 'contract_type': contract_type,
        'iv': iv, 'spot_price': spot_price,
    }


CALL_OPT = make_option('BTC-C-100000', 1001, 100000, 500.0, 0.20, 'call_options')
PUT_OPT = make_option('BTC-P-90000', 1002, 90000, 450.0, -0.20, 'put_options')

PRODUCT_DETAILS = {'contract_value': 0.001, 'contract_unit_currency': 'BTC'}


@pytest.fixture
def call_opt():
    return dict(CALL_OPT)


@pytest.fixture
def put_opt():
    return dict(PUT_OPT)
