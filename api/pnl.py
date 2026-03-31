def calculate_total_pnl(positions, call_current_price, put_current_price,
                        call_product_id, put_product_id,
                        call_contract_value, put_contract_value,
                        cumulative_realized_pnl):
    total_unrealized_pnl = 0
    call_position_info = None
    put_position_info = None

    for pos in positions:
        product_id = pos.get('product_id')
        size = int(pos.get('size', 0))
        entry_price = float(pos.get('entry_price', 0))
        if size == 0:
            continue

        if product_id in (call_product_id, put_product_id):
            is_call = product_id == call_product_id
            current = call_current_price if is_call else put_current_price
            cv = call_contract_value if is_call else put_contract_value
            price_diff = entry_price - current
            unrealized = price_diff * abs(size) * cv
            total_unrealized_pnl += unrealized
            info = {
                'size': size, 'entry_price': entry_price,
                'current_price': current, 'price_diff': price_diff,
                'contract_value': cv, 'unrealized_pnl': unrealized
            }
            if is_call:
                call_position_info = info
            else:
                put_position_info = info

    total_pnl = cumulative_realized_pnl + total_unrealized_pnl
    return cumulative_realized_pnl, total_unrealized_pnl, total_pnl, call_position_info, put_position_info
