"""
Common P&L calculator for any strategy's legs.
Works for Delta Neutral, Option Chain, Strategy Builder, and Tracker strategies.
"""
from api.pricing import get_current_price
from config import get_contract_value


def compute_leg_pnl(entry_price, mark_price, size, side, contract_value):
    """Single-leg P&L calculation. Returns float.
    
    side: 'buy' or 'sell'
    """
    direction = 1 if side == 'buy' else -1
    return direction * (mark_price - entry_price) * size * contract_value


def compute_live_legs(legs, asset='BTC'):
    """
    Takes a list of legs and returns them enriched with live mark prices and P&L.
    
    Input legs: [{product_id, symbol, side, size, entry_price, type?, strike?, ...}]
    Returns: (enriched_legs, total_pnl)
    """
    # NSE/BSE legs (Groww): size is a quantity, marks come from Groww LTP by trading symbol.
    groww = asset not in ('BTC', 'ETH') or any(l.get('trading_symbol') for l in legs)
    groww_marks = {}
    if groww:
        from strategy.groww_exec import get_marks, GROWW_CONTRACT_VALUE
        lot_size = GROWW_CONTRACT_VALUE
        groww_marks = get_marks([l for l in legs if l.get('trading_symbol') or l.get('symbol')])
    else:
        lot_size = get_contract_value(asset)
    total_pnl = 0
    result = []

    for leg in legs:
        entry = float(leg.get('entry_price') or leg.get('entry') or 0)
        pid = leg.get('product_id')
        mark = entry  # fallback

        if groww:
            m = groww_marks.get(leg.get('trading_symbol') or leg.get('symbol'))
            if m:
                mark = float(m)
        elif pid:
            try:
                data = get_current_price(pid, asset)
                if data and data.get('mark_price'):
                    mark = float(data['mark_price'])
            except Exception:
                pass

        size = int(leg.get('size') or leg.get('lots') or 0)
        side = (leg.get('side') or '').lower()
        pnl = compute_leg_pnl(entry, mark, size, side, lot_size)

        result.append({
            'product_id': pid,
            'symbol': leg.get('symbol', ''),
            'type': leg.get('type', ''),
            'strike': leg.get('strike', ''),
            'side': side,
            'size': size,
            'entry_price': round(entry, 2),
            'current_mark': round(mark, 2),
            'current_pnl': round(pnl, 2),
            'delta': float(leg.get('delta') or 0),
        })
        total_pnl += pnl

    return result, round(total_pnl, 2)
