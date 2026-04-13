"""
Common P&L calculator for any strategy's legs.
Works for Delta Neutral, Option Chain, Strategy Builder, and Tracker strategies.
"""
from api.pricing import get_current_price


def compute_live_legs(legs, asset='BTC'):
    """
    Takes a list of legs and returns them enriched with live mark prices and P&L.
    
    Input legs: [{product_id, symbol, side, size, entry_price, type?, strike?, ...}]
    Returns: (enriched_legs, total_pnl)
    """
    lot_sizes = {'BTC': 0.001, 'ETH': 0.01}
    lot_size = lot_sizes.get(asset, 0.001)
    total_pnl = 0
    result = []

    for leg in legs:
        entry = float(leg.get('entry_price') or leg.get('entry') or 0)
        pid = leg.get('product_id')
        mark = entry  # fallback

        if pid:
            try:
                data = get_current_price(pid, asset)
                if data and data.get('mark_price'):
                    mark = float(data['mark_price'])
            except Exception:
                pass

        size = int(leg.get('size') or leg.get('lots') or 0)
        side = (leg.get('side') or '').lower()
        direction = 1 if side == 'buy' else -1
        pnl = direction * (mark - entry) * size * lot_size

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
