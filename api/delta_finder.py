def find_target_delta_options(option_chain, target_delta, tolerance):
    if not option_chain or not option_chain.get('success'):
        return None, None

    calls, puts = [], []
    for option in option_chain.get('result', []):
        if not option.get('greeks') or not option['greeks'].get('delta'):
            continue
        delta = float(option['greeks']['delta'])
        contract_type = option.get('contract_type', '')
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

    # Try strict tolerance first, then fall back to closest available
    best_call = next((c for c in calls if abs(c['delta'] - target_delta) <= tolerance), None)
    best_put = next((p for p in puts if abs(abs(p['delta']) - target_delta) <= tolerance), None)

    # Fallback: pick closest if strict match fails
    if not best_call and calls:
        best_call = calls[0]
        print(f"⚠ No call within tolerance {tolerance}. Using closest: delta={best_call['delta']:.4f} ({best_call['symbol']})")
    if not best_put and puts:
        best_put = puts[0]
        print(f"⚠ No put within tolerance {tolerance}. Using closest: delta={best_put['delta']:.4f} ({best_put['symbol']})")

    return best_call, best_put
