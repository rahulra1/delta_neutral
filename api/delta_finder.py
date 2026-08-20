import logging

logger = logging.getLogger(__name__)

# Delta-based scoring weights (normalized min-max approach)
W_DELTA_CLOSENESS = 0.50   # Distance from target delta (most critical)
W_DELTA_VOLUME = 0.25      # Immediate liquidity — ensures quick entry/exit
W_DELTA_OI = 0.25          # Structural liquidity — ensures market depth


def _score_by_delta(candidates, target_delta, is_put=False):
    """
    Score candidates using min-max normalization.

    Formula:
        Closeness Score (C_i) = 1 - (error_i - min_error) / (max_error - min_error)
        Volume Score   (V_i) = (volume_i - min_vol) / (max_vol - min_vol)
        OI Score       (O_i) = (oi_i - min_oi) / (max_oi - min_oi)

        Final Score = C_i × 0.50 + V_i × 0.25 + O_i × 0.25

    Score closest to 1.0 wins.
    """
    if not candidates:
        return []

    # Step 1: Compute delta error for each candidate
    for c in candidates:
        c['_delta_error'] = abs(abs(c['delta']) - target_delta) if is_put else abs(c['delta'] - target_delta)

    # Step 2: Get min/max for normalization
    errors = [c['_delta_error'] for c in candidates]
    volumes = [c.get('volume', 0) for c in candidates]
    ois = [c.get('oi', 0) for c in candidates]

    min_err, max_err = min(errors), max(errors)
    min_vol, max_vol = min(volumes), max(volumes)
    min_oi, max_oi = min(ois), max(ois)

    err_range = (max_err - min_err) or 1.0
    vol_range = (max_vol - min_vol) or 1.0
    oi_range = (max_oi - min_oi) or 1.0

    # Step 3: Normalize and compute weighted score
    for c in candidates:
        # Closeness: 1 = best (lowest error), 0 = worst (highest error)
        c['_closeness_score'] = 1.0 - ((c['_delta_error'] - min_err) / err_range)

        # Volume: 1 = best (highest volume), 0 = worst (lowest volume)
        c['_volume_score'] = (c.get('volume', 0) - min_vol) / vol_range

        # OI: 1 = best (highest OI), 0 = worst (lowest OI)
        c['_oi_score'] = (c.get('oi', 0) - min_oi) / oi_range

        # Final weighted score (higher = better)
        c['_final_score'] = (
            W_DELTA_CLOSENESS * c['_closeness_score'] +
            W_DELTA_VOLUME * c['_volume_score'] +
            W_DELTA_OI * c['_oi_score']
        )

    # Sort by final score descending (highest score = best)
    candidates.sort(key=lambda x: -x['_final_score'])

    return candidates


def find_target_delta_options(option_chain, target_delta, tolerance):
    """
    Find the best call and put options near target_delta (e.g. 0.20),
    scored by: Closeness (50%) + Volume (25%) + OI (25%) using min-max normalization.

    The strike scoring closest to 1.0 is the winner.

    Args:
        option_chain: API response from get_option_chain() — Delta Exchange format
                      OR a list of {strike, call, put} dicts — Groww chain format
        target_delta: Target delta to match (e.g. 0.20)
        tolerance: Acceptable delta deviation (e.g. 0.05)

    Returns:
        (best_call, best_put) tuple, each a dict with option details or None
    """
    calls, puts = [], []

    # Detect format: Delta Exchange (has 'success' + 'result') vs Groww chain (list of rows)
    if isinstance(option_chain, dict) and 'result' in option_chain:
        # Delta Exchange format
        if not option_chain.get('success'):
            return None, None

        for option in option_chain.get('result', []):
            if not option.get('greeks') or not option['greeks'].get('delta'):
                continue
            delta = float(option['greeks']['delta'])
            contract_type = option.get('contract_type', '')
            mark_price = float(option.get('mark_price', 0))
            if mark_price <= 0:
                continue

            oi = float(option.get('oi', 0) or 0)
            volume = float(option.get('volume', 0) or 0)

            quotes = option.get('quotes', {}) or {}
            best_bid = float(quotes.get('best_bid', 0) or 0)
            best_ask = float(quotes.get('best_ask', 0) or 0)
            spread_pct = ((best_ask - best_bid) / mark_price) if (best_bid > 0 and best_ask > 0) else 1.0

            entry = {
                'symbol': option.get('symbol', ''),
                'product_id': option.get('product_id'),
                'delta': delta,
                'mark_price': mark_price,
                'strike_price': option.get('strike_price'),
                'oi': oi,
                'volume': volume,
                'spread_pct': spread_pct,
                'best_bid': best_bid,
                'best_ask': best_ask,
            }

            if 'call_options' in contract_type and delta > 0:
                calls.append(entry)
            elif 'put_options' in contract_type and delta < 0:
                puts.append(entry)

    elif isinstance(option_chain, list):
        # Groww chain format: list of {strike, call, put} dicts
        for row in option_chain:
            strike = float(row.get('strike', 0))

            if row.get('call'):
                c = row['call']
                mark_price = float(c.get('mark_price', 0) or 0)
                delta = float(c.get('delta', 0) or 0)
                if mark_price > 0 and delta > 0:
                    calls.append({
                        'symbol': c.get('trading_symbol') or c.get('symbol', ''),
                        'product_id': c.get('product_id'),
                        'delta': delta,
                        'mark_price': mark_price,
                        'strike_price': strike,
                        'oi': float(c.get('oi', 0) or 0),
                        'volume': float(c.get('volume', 0) or 0),
                        'spread_pct': 0.0,
                        'best_bid': float(c.get('bid', 0) or 0),
                        'best_ask': float(c.get('ask', 0) or 0),
                    })

            if row.get('put'):
                p = row['put']
                mark_price = float(p.get('mark_price', 0) or 0)
                delta = float(p.get('delta', 0) or 0)
                if mark_price > 0 and delta < 0:
                    puts.append({
                        'symbol': p.get('trading_symbol') or p.get('symbol', ''),
                        'product_id': p.get('product_id'),
                        'delta': delta,
                        'mark_price': mark_price,
                        'strike_price': strike,
                        'oi': float(p.get('oi', 0) or 0),
                        'volume': float(p.get('volume', 0) or 0),
                        'spread_pct': 0.0,
                        'best_bid': float(p.get('bid', 0) or 0),
                        'best_ask': float(p.get('ask', 0) or 0),
                    })
    else:
        logger.error("Unsupported option_chain format")
        return None, None

    # Filter: only consider options within a reasonable delta range
    max_delta = target_delta * 2.5   # e.g. 0.50 for target 0.20
    min_delta = target_delta * 0.25  # e.g. 0.05 for target 0.20

    eligible_calls = [c for c in calls if min_delta <= c['delta'] <= max_delta]
    eligible_puts = [p for p in puts if min_delta <= abs(p['delta']) <= max_delta]

    # Score using normalized formula: Closeness 50% + Volume 25% + OI 25%
    scored_calls = _score_by_delta(eligible_calls, target_delta, is_put=False)
    scored_puts = _score_by_delta(eligible_puts, target_delta, is_put=True)

    best_call = None
    best_put = None

    if scored_calls:
        best_call = scored_calls[0]
        delta_dist = abs(best_call['delta'] - target_delta)
        if delta_dist <= tolerance:
            logger.info(
                f"✓ Call selected: {best_call['symbol']} | delta={best_call['delta']:.4f} | "
                f"OI={best_call['oi']:.0f} | vol={best_call['volume']:.0f} | "
                f"score={best_call['_final_score']:.3f}"
            )
        else:
            logger.warning(
                f"⚠ Call outside tolerance: {best_call['symbol']} | delta={best_call['delta']:.4f} "
                f"(target={target_delta}±{tolerance}) | OI={best_call['oi']:.0f} | "
                f"vol={best_call['volume']:.0f} | score={best_call['_final_score']:.3f}"
            )
    else:
        logger.warning(f"✗ No suitable call found (target delta={target_delta})")

    if scored_puts:
        best_put = scored_puts[0]
        delta_dist = abs(abs(best_put['delta']) - target_delta)
        if delta_dist <= tolerance:
            logger.info(
                f"✓ Put selected: {best_put['symbol']} | delta={best_put['delta']:.4f} | "
                f"OI={best_put['oi']:.0f} | vol={best_put['volume']:.0f} | "
                f"score={best_put['_final_score']:.3f}"
            )
        else:
            logger.warning(
                f"⚠ Put outside tolerance: {best_put['symbol']} | delta={best_put['delta']:.4f} "
                f"(target=-{target_delta}±{tolerance}) | OI={best_put['oi']:.0f} | "
                f"vol={best_put['volume']:.0f} | score={best_put['_final_score']:.3f}"
            )
    else:
        logger.warning(f"✗ No suitable put found (target delta=-{target_delta})")

    # Clean up internal scoring fields
    for opt in [best_call, best_put]:
        if opt:
            opt.pop('_delta_error', None)
            opt.pop('_closeness_score', None)
            opt.pop('_volume_score', None)
            opt.pop('_oi_score', None)
            opt.pop('_final_score', None)

    return best_call, best_put


# Premium-based scoring weights (normalized min-max approach)
W_CLOSENESS = 0.50   # Distance from target premium (most critical)
W_VOLUME = 0.25      # Immediate liquidity — ensures quick entry/exit
W_OI = 0.25          # Structural liquidity — ensures market depth


def _score_by_premium(candidates, target_premium):
    """
    Score candidates using min-max normalization.

    Formula:
        Closeness Score (C_i) = 1 - (error_i - min_error) / (max_error - min_error)
        Volume Score   (V_i) = (volume_i - min_vol) / (max_vol - min_vol)
        OI Score       (O_i) = (oi_i - min_oi) / (max_oi - min_oi)

        Final Score = C_i × 0.50 + V_i × 0.25 + O_i × 0.25

    Score closest to 1.0 wins.
    """
    if not candidates:
        return []

    # Step 1: Compute premium error for each candidate
    for c in candidates:
        c['_premium_error'] = abs(c['mark_price'] - target_premium)

    # Step 2: Get min/max for normalization
    errors = [c['_premium_error'] for c in candidates]
    volumes = [c.get('volume', 0) for c in candidates]
    ois = [c.get('oi', 0) for c in candidates]

    min_err, max_err = min(errors), max(errors)
    min_vol, max_vol = min(volumes), max(volumes)
    min_oi, max_oi = min(ois), max(ois)

    err_range = (max_err - min_err) or 1.0
    vol_range = (max_vol - min_vol) or 1.0
    oi_range = (max_oi - min_oi) or 1.0

    # Step 3: Normalize and compute weighted score
    for c in candidates:
        # Closeness: 1 = best (lowest error), 0 = worst (highest error)
        c['_closeness_score'] = 1.0 - ((c['_premium_error'] - min_err) / err_range)

        # Volume: 1 = best (highest volume), 0 = worst (lowest volume)
        c['_volume_score'] = (c.get('volume', 0) - min_vol) / vol_range

        # OI: 1 = best (highest OI), 0 = worst (lowest OI)
        c['_oi_score'] = (c.get('oi', 0) - min_oi) / oi_range

        # Final weighted score (higher = better)
        c['_final_score'] = (
            W_CLOSENESS * c['_closeness_score'] +
            W_VOLUME * c['_volume_score'] +
            W_OI * c['_oi_score']
        )

    # Sort by final score descending (highest score = best)
    candidates.sort(key=lambda x: -x['_final_score'])

    return candidates


def find_target_premium_options(option_chain, target_premium, premium_tolerance_pct=0.50,
                                min_delta=0.05, max_delta=0.50):
    """
    Find the best call and put options near target_premium (e.g. ₹70),
    scored by: Closeness (50%) + Volume (25%) + OI (25%) using min-max normalization.

    The strike scoring closest to 1.0 is the winner.

    Args:
        option_chain: API response from get_option_chain() — Delta Exchange format
                      OR a list of {strike, call, put} dicts — Groww chain format
        target_premium: Target premium to match (e.g. 70.0)
        premium_tolerance_pct: Max deviation from target as a fraction (0.50 = ±50%)
        min_delta: Minimum abs(delta) to consider (filters out deep OTM illiquid)
        max_delta: Maximum abs(delta) to consider (filters out ITM options)

    Returns:
        (best_call, best_put) tuple, each a dict with option details or None
    """
    calls, puts = [], []

    # Detect format: Delta Exchange (has 'success' + 'result') vs Groww chain (list of rows)
    if isinstance(option_chain, dict) and 'result' in option_chain:
        # Delta Exchange format
        if not option_chain.get('success'):
            return None, None

        for option in option_chain.get('result', []):
            if not option.get('greeks') or not option['greeks'].get('delta'):
                continue
            delta = float(option['greeks']['delta'])
            contract_type = option.get('contract_type', '')
            mark_price = float(option.get('mark_price', 0))
            if mark_price <= 0:
                continue

            oi = float(option.get('oi', 0) or 0)
            volume = float(option.get('volume', 0) or 0)

            quotes = option.get('quotes', {}) or {}
            best_bid = float(quotes.get('best_bid', 0) or 0)
            best_ask = float(quotes.get('best_ask', 0) or 0)
            spread_pct = ((best_ask - best_bid) / mark_price) if (best_bid > 0 and best_ask > 0) else 1.0

            entry = {
                'symbol': option.get('symbol', ''),
                'product_id': option.get('product_id'),
                'delta': delta,
                'mark_price': mark_price,
                'strike_price': option.get('strike_price'),
                'oi': oi,
                'volume': volume,
                'spread_pct': spread_pct,
                'best_bid': best_bid,
                'best_ask': best_ask,
            }

            if 'call_options' in contract_type and delta > 0:
                calls.append(entry)
            elif 'put_options' in contract_type and delta < 0:
                puts.append(entry)

    elif isinstance(option_chain, list):
        # Groww chain format: list of {strike, call, put} dicts
        for row in option_chain:
            strike = float(row.get('strike', 0))

            if row.get('call'):
                c = row['call']
                mark_price = float(c.get('mark_price', 0) or 0)
                delta = float(c.get('delta', 0) or 0)
                if mark_price > 0 and delta > 0:
                    calls.append({
                        'symbol': c.get('trading_symbol') or c.get('symbol', ''),
                        'product_id': c.get('product_id'),
                        'delta': delta,
                        'mark_price': mark_price,
                        'strike_price': strike,
                        'oi': float(c.get('oi', 0) or 0),
                        'volume': float(c.get('volume', 0) or 0),
                        'spread_pct': 0.0,
                        'best_bid': float(c.get('bid', 0) or 0),
                        'best_ask': float(c.get('ask', 0) or 0),
                    })

            if row.get('put'):
                p = row['put']
                mark_price = float(p.get('mark_price', 0) or 0)
                delta = float(p.get('delta', 0) or 0)
                if mark_price > 0 and delta < 0:
                    puts.append({
                        'symbol': p.get('trading_symbol') or p.get('symbol', ''),
                        'product_id': p.get('product_id'),
                        'delta': delta,
                        'mark_price': mark_price,
                        'strike_price': strike,
                        'oi': float(p.get('oi', 0) or 0),
                        'volume': float(p.get('volume', 0) or 0),
                        'spread_pct': 0.0,
                        'best_bid': float(p.get('bid', 0) or 0),
                        'best_ask': float(p.get('ask', 0) or 0),
                    })
    else:
        logger.error("Unsupported option_chain format")
        return None, None

    # Filter by premium range (e.g. ₹35–₹105 for target ₹70 with 50% tolerance)
    premium_min = target_premium * (1 - premium_tolerance_pct)
    premium_max = target_premium * (1 + premium_tolerance_pct)

    eligible_calls = [
        c for c in calls
        if premium_min <= c['mark_price'] <= premium_max
        and min_delta <= c['delta'] <= max_delta
    ]
    eligible_puts = [
        p for p in puts
        if premium_min <= p['mark_price'] <= premium_max
        and min_delta <= abs(p['delta']) <= max_delta
    ]

    # Fallback: relax delta constraint if nothing found
    if not eligible_calls:
        eligible_calls = [c for c in calls if premium_min <= c['mark_price'] <= premium_max]
        if eligible_calls:
            logger.warning(
                f"⚠ No calls with delta {min_delta}-{max_delta} near ₹{target_premium}. "
                f"Relaxed delta filter — options may be illiquid."
            )

    if not eligible_puts:
        eligible_puts = [p for p in puts if premium_min <= p['mark_price'] <= premium_max]
        if eligible_puts:
            logger.warning(
                f"⚠ No puts with delta {min_delta}-{max_delta} near ₹{target_premium}. "
                f"Relaxed delta filter — options may be illiquid."
            )

    # Score using normalized formula: Closeness 50% + Volume 25% + OI 25%
    scored_calls = _score_by_premium(eligible_calls, target_premium)
    scored_puts = _score_by_premium(eligible_puts, target_premium)

    best_call = None
    best_put = None

    if scored_calls:
        best_call = scored_calls[0]
        logger.info(
            f"✓ Call selected (by premium): {best_call['symbol']} | "
            f"strike={best_call['strike_price']} | premium={best_call['mark_price']:.2f} | "
            f"delta={best_call['delta']:.4f} | OI={best_call['oi']:.0f} | vol={best_call['volume']:.0f} | "
            f"score={best_call['_final_score']:.3f}"
        )
    else:
        logger.warning(f"✗ No suitable call found near premium ₹{target_premium}")

    if scored_puts:
        best_put = scored_puts[0]
        logger.info(
            f"✓ Put selected (by premium): {best_put['symbol']} | "
            f"strike={best_put['strike_price']} | premium={best_put['mark_price']:.2f} | "
            f"delta={best_put['delta']:.4f} | OI={best_put['oi']:.0f} | vol={best_put['volume']:.0f} | "
            f"score={best_put['_final_score']:.3f}"
        )
    else:
        logger.warning(f"✗ No suitable put found near premium ₹{target_premium}")

    # Clean up internal scoring fields
    for opt in [best_call, best_put]:
        if opt:
            opt.pop('_premium_error', None)
            opt.pop('_closeness_score', None)
            opt.pop('_volume_score', None)
            opt.pop('_oi_score', None)
            opt.pop('_final_score', None)

    return best_call, best_put
