"""Groww (NSE/BSE options) execution helpers for the Strategy Builder.

Shared by the one-shot deploy route (app.py), the recurring BuilderScheduler and
StrategyMonitor so strike selection, order placement, marks and closing behave
the same everywhere. Leg 'size' on Groww is the contract QUANTITY (lots x lot
size), so P&L uses a contract value of 1.
"""

import re
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
GROWW_CONTRACT_VALUE = 1


def is_groww(broker):
    return broker == 'groww'


def market_open(now=None):
    """NSE/BSE F&O session: Mon-Fri 09:15-15:30 IST."""
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return (9, 15) <= (now.hour, now.minute) <= (15, 30)


def lot_size_for(asset):
    from strategy.nse_daily_strangle import NSE_LOT_SIZES
    return NSE_LOT_SIZES.get(asset, 1)


def resolve_leg(leg, chain, atm_idx):
    """Resolve a builder leg to a Groww option dict from a Groww chain, or None."""
    opt_type = 'call' if leg.get('type') == 'CE' else 'put'
    if leg.get('strike_mode') == 'delta':
        from api.delta_finder import find_target_delta_options
        tgt = abs(float(leg.get('target_delta', 0.20)))
        bc, bp = find_target_delta_options(
            chain, tgt, tolerance=float(leg.get('delta_tolerance', 0.10)))
        opt = bc if opt_type == 'call' else bp
    else:
        m = re.match(r'(ATM|OTM|ITM)(\d*)', leg.get('strike', 'ATM'))
        offset = 0
        if m:
            offset = int(m.group(2)) if m.group(2) else 0
            if m.group(1) == 'OTM':
                offset = offset if opt_type == 'call' else -offset
            elif m.group(1) == 'ITM':
                offset = -offset if opt_type == 'call' else offset
        idx = max(0, min(atm_idx + offset, len(chain) - 1))
        opt = chain[idx].get(opt_type)
    if not opt:
        return None
    tsym = opt.get('trading_symbol') or opt.get('symbol')
    if not tsym:
        return None
    return {'trading_symbol': tsym,
            'strike': opt.get('strike') or opt.get('strike_price'),
            'mark_price': float(opt.get('mark_price', 0) or 0)}


def place_legs(legs_cfg, chain, spot, asset, lots_per_leg, log=print):
    """Resolve all legs, then place them on Groww (BUY first, stop on first failure).

    Returns (placed_legs, results, error). `error` is set (and nothing placed)
    when a leg cannot be resolved. A placed leg is recorded only after Groww
    confirms it EXECUTED.
    """
    from strategy.builder_common import atm_index
    from api.groww import place_order, confirm_order_filled

    atm_idx = atm_index(chain, spot)
    lot_size = lot_size_for(asset)
    planned = []
    for leg in legs_cfg:
        opt = resolve_leg(leg, chain, atm_idx)
        if not opt:
            return [], [], f"No option found for leg {leg.get('type')} {leg.get('strike', '')}; no orders placed"
        planned.append((leg, opt))
    planned.sort(key=lambda t: 0 if str(t[0]['side']).lower() == 'buy' else 1)

    placed, results = [], []
    for leg, opt in planned:
        tsym = opt['trading_symbol']
        qty = int(leg.get('lots', 1)) * lots_per_leg * lot_size
        side = str(leg['side']).upper()
        resp = place_order(trading_symbol=tsym, quantity=qty, transaction_type=side,
                           order_type='MARKET', product='NRML')
        oid = (resp or {}).get('groww_order_id')
        if not oid:
            results.append({'symbol': tsym, 'side': side, 'size': qty, 'success': False,
                            'error': (resp or {}).get('error', 'no order id')})
            break
        filled, status = confirm_order_filled(oid)
        results.append({'symbol': tsym, 'side': side, 'size': qty, 'success': filled,
                        'order_id': oid, 'status': status})
        if not filled:
            break
        log(f"[Groww] {side} {tsym} x{qty} EXECUTED (price ~{opt['mark_price']:.2f} = chain LTP)")
        placed.append({'product_id': tsym, 'symbol': tsym, 'trading_symbol': tsym,
                       'type': leg['type'], 'strike': opt['strike'], 'side': leg['side'],
                       'size': qty, 'entry_price': opt['mark_price'],
                       'entry_mark': opt['mark_price'], 'order_id': oid})
    return placed, results, None


def get_marks(legs):
    """{trading_symbol: ltp} for the given legs (missing/zero LTP omitted)."""
    from api.groww import get_option_ltps
    syms = [l.get('trading_symbol') or l['symbol'] for l in legs]
    try:
        raw = get_option_ltps(syms) or {}
    except Exception as e:
        logger.warning("Groww marks failed: %s", e)
        return {}
    return {s: v for s, v in raw.items() if v}


def close_legs(legs, log=print):
    """Close legs with opposite MARKET orders (buy-backs of shorts first).

    Returns the list of legs that could NOT be confirmed closed (empty = all closed).
    """
    from api.groww import place_order, confirm_order_filled
    ordered = sorted(legs, key=lambda l: 0 if l['side'] == 'sell' else 1)  # sell legs need a BUY to close
    failed = []
    for l in ordered:
        tsym = l.get('trading_symbol') or l['symbol']
        side = 'BUY' if l['side'] == 'sell' else 'SELL'
        log(f"[Groww] closing: {side} {tsym} x{l['size']}")
        resp = place_order(trading_symbol=tsym, quantity=l['size'], transaction_type=side,
                           order_type='MARKET', product='NRML')
        oid = (resp or {}).get('groww_order_id')
        if oid:
            ok, status = confirm_order_filled(oid)
        else:
            ok, status = False, (resp or {}).get('error', 'no order id')
        if not ok:
            log(f"[Groww] ⚠ close FAILED for {tsym}: {status}")
            failed.append(l)
    return failed
