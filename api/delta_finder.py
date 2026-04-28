import logging

logger = logging.getLogger(__name__)


def find_target_delta_options(option_chain, target_delta, tolerance):
    if not option_chain or not option_chain.get('success'):
        return None, None

    calls, puts = [], []
    for option in option_chain.get('result', []):
        if not option.get('greeks') or not option['greeks'].get('delta'):
            continue
        delta = float(option['greeks']['delta'])
        contract_type = option.get('contract_type', '')
        if float(option.get('mark_price', 0)) <= 0:
            continue  # skip options with no mark price
        entry = {
            'symbol': option['symbol'],
            'product_id': option['product_id'],
            'delta': delta,
            'mark_price': float(option['mark_price']),
            'strike_price': option.get('strike_price')
        }
        if 'call_options' in contract_type and delta > 0:
            calls.append(entry)
        elif 'put_options' in contract_type and delta < 0:
            puts.append(entry)

    # Sort by closeness to target delta
    calls.sort(key=lambda x: abs(x['delta'] - target_delta))
    puts.sort(key=lambda x: abs(abs(x['delta']) - target_delta))

    # Try strict tolerance first
    best_call = next((c for c in calls if abs(c['delta'] - target_delta) <= tolerance), None)
    best_put = next((p for p in puts if abs(abs(p['delta']) - target_delta) <= tolerance), None)

    # Fallback: pick closest OTM option (delta < 0.5 for calls, > -0.5 for puts)
    # This prevents selecting deep ITM options that are unsuitable for strangles
    max_call_delta = target_delta * 2.5  # e.g. 0.20 * 2.5 = 0.50 max
    max_put_delta = target_delta * 2.5

    if not best_call:
        otm_calls = [c for c in calls if c['delta'] <= max_call_delta]
        if otm_calls:
            best_call = otm_calls[0]
            logger.warning(f"⚠ No call within tolerance {tolerance}. Using closest OTM: delta={best_call['delta']:.4f} ({best_call['symbol']})")
        elif calls:
            logger.warning(f"✗ No suitable OTM call found (closest delta: {calls[0]['delta']:.4f}, max allowed: {max_call_delta:.2f})")

    if not best_put:
        otm_puts = [p for p in puts if abs(p['delta']) <= max_put_delta]
        if otm_puts:
            best_put = otm_puts[0]
            logger.warning(f"⚠ No put within tolerance {tolerance}. Using closest OTM: delta={best_put['delta']:.4f} ({best_put['symbol']})")
        elif puts:
            logger.warning(f"✗ No suitable OTM put found (closest delta: {puts[0]['delta']:.4f}, max allowed: -{max_put_delta:.2f})")

    return best_call, best_put
