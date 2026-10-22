"""Shared helpers for the visual Strategy Builder.

Used by both the one-shot deploy route (app.py) and the recurring BuilderScheduler
so expiry resolution and per-leg strike selection follow a single implementation.
"""

import re
import logging

logger = logging.getLogger(__name__)


def resolve_builder_expiry(expiry_key, expiries):
    """Resolve a builder expiry choice to a concrete DD-MM-YYYY date.

    Accepts logical types (weekly / next_weekly / monthly / next_monthly), the
    legacy current_week/next_week/current_month/next_month keys, or a literal
    DD-MM-YYYY. Returns a date string or None.
    """
    from api.expiry_resolver import resolve_expiry, EXPIRY_TYPES
    if not expiries:
        return None
    legacy = {'current_week': 'weekly', 'next_week': 'next_weekly',
              'current_month': 'monthly', 'next_month': 'next_monthly'}
    key = legacy.get(expiry_key, expiry_key)
    if key in EXPIRY_TYPES:
        return resolve_expiry(key, expiries)
    if key in expiries:
        return key
    return expiries[0]


def resolve_leg_to_option(leg, chain, atm_idx):
    """Resolve one builder leg config to a concrete option dict from the chain.

    Supports strike_mode='delta' (via api.delta_finder) and the default
    ATM/OTMn/ITMn offset. Returns a dict {product_id, symbol, strike, mark_price}
    or None if no option matches.
    """
    opt_type = 'call' if leg.get('type') == 'CE' else 'put'

    if leg.get('strike_mode') == 'delta':
        from api.delta_finder import find_target_delta_options
        tgt = abs(float(leg.get('target_delta', 0.20)))
        bc, bp = find_target_delta_options(
            chain, tgt, tolerance=float(leg.get('delta_tolerance', 0.10)))
        best = bc if opt_type == 'call' else bp
        if not best or not best.get('product_id'):
            return None
        return {'product_id': best.get('product_id'), 'symbol': best.get('symbol', ''),
                'strike': best.get('strike_price'), 'mark_price': best.get('mark_price', 0)}

    # Offset mode: ATM / OTMn / ITMn relative to ATM index.
    strike_key = leg.get('strike', 'ATM')
    m = re.match(r'(ATM|OTM|ITM)(\d*)', strike_key)
    offset = 0
    if m:
        offset = int(m.group(2)) if m.group(2) else 0
        if m.group(1) == 'OTM':
            offset = offset if opt_type == 'call' else -offset
        elif m.group(1) == 'ITM':
            offset = -offset if opt_type == 'call' else offset
    idx = max(0, min(atm_idx + offset, len(chain) - 1))
    opt = chain[idx].get(opt_type)
    if not opt or not opt.get('product_id'):
        return None
    return {'product_id': opt['product_id'], 'symbol': opt['symbol'],
            'strike': opt['strike'], 'mark_price': opt.get('mark_price', 0)}


def atm_index(chain, spot):
    """Index of the chain row whose strike is closest to spot."""
    strikes = [float(row['strike']) for row in chain]
    return min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
