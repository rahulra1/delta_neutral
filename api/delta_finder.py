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
        if 'call_options' in contract_type and abs(delta - target_delta) <= tolerance:
            calls.append(entry)
        elif 'put_options' in contract_type and abs(abs(delta) - target_delta) <= tolerance:
            puts.append(entry)

    calls.sort(key=lambda x: abs(x['delta'] - target_delta))
    puts.sort(key=lambda x: abs(abs(x['delta']) - target_delta))
    return (calls[0] if calls else None), (puts[0] if puts else None)
