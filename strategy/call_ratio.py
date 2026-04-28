"""Monthly Call Ratio Spread Strategy (No-Brainer).
Buy 1 ATM+300 Call, Sell 2 ATM+600 Calls, Buy 1 ATM+1000 Call (hedge).
Target: 2.5% | SL: 3% | Hold: max 20 days | Zero adjustments."""

import logging
import time
from datetime import datetime
from api import (
    get_option_chain, get_product_details,
    place_order, get_current_price, get_positions,
    get_position_entry_price
)
from websocket import WebSocketManager
from strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


class CallRatioStrategy(BaseStrategy):
    def __init__(self, asset='BTC', expiry_date='', lot_size=10,
                 buy_offset_pct=2, sell_offset_pct=4, hedge_offset_pct=7,
                 target_pct=5, sl_pct=8, max_credit_pct=1.0,
                 monitoring_interval=30):
        self.asset = asset
        self.expiry_date = expiry_date
        self.lot_size = lot_size
        self.buy_offset_pct = buy_offset_pct    # % OTM for buy leg
        self.sell_offset_pct = sell_offset_pct   # % OTM for sell legs
        self.hedge_offset_pct = hedge_offset_pct # % OTM for hedge
        self.target_pct = target_pct
        self.sl_pct = sl_pct
        self.max_credit_pct = max_credit_pct
        self.monitoring_interval = monitoring_interval

        # Positions: {symbol, product_id, strike, side, size, entry_price, contract_value}
        self.legs = []
        self.deployed_margin = 0
        self.total_pnl = 0
        self.pnl_pct = 0
        self.status_msg = ''
        self.running = True
        self.ws_manager = WebSocketManager(self)

    def on_price_update(self, symbol, mark_price, delta):
        pass

    def _find_strike(self, chain, target_strike, contract_type):
        """Find closest option to target strike."""
        options = [o for o in chain if o.get('contract_type') == contract_type]
        if not options:
            return None
        return min(options, key=lambda o: abs(float(o.get('strike_price', 0)) - target_strike))

    def initialize(self):
        logger.info("=" * 70)
        logger.info("MONTHLY CALL RATIO SPREAD (NO-BRAINER)")
        logger.info("=" * 70)
        logger.info(f"Asset: {self.asset} | Expiry: {self.expiry_date} | Lots: {self.lot_size}")
        logger.info(f"Offsets: Buy +{self.buy_offset_pct}% | Sell +{self.sell_offset_pct}% (x2) | Hedge +{self.hedge_offset_pct}%")
        logger.info(f"Target: {self.target_pct}% | SL: {self.sl_pct}% | Max Credit: {self.max_credit_pct}%")
        logger.info("=" * 70)

        # Auto-select expiry if not provided
        if not self.expiry_date:
            logger.info("[1/5] Auto-selecting nearest monthly expiry...")
            from api.chain import get_expiries
            expiries = get_expiries(self.asset, min_days=7)
            if not expiries:
                logger.warning("✗ No expiries available")
                return False
            self.expiry_date = expiries[0]
            logger.info(f"✓ Selected: {self.expiry_date}")
        else:
            logger.info(f"[1/5] Using expiry: {self.expiry_date}")

        logger.info("[2/5] Fetching option chain...")
        from api.option_chain import get_option_chain as fetch_chain
        raw = fetch_chain(self.expiry_date, self.asset)
        if not raw:
            logger.warning("✗ Failed to fetch option chain")
            return False
        # raw is {success: bool, result: [...]} — extract the list
        chain = raw.get('result', []) if isinstance(raw, dict) else raw
        if not chain:
            logger.warning("✗ Empty option chain")
            return False

        # Get spot price
        calls = [o for o in chain if o.get('contract_type') == 'call_options']
        if not calls:
            logger.warning("✗ No call options found")
            return False
        spot = float(calls[0].get('spot_price', 0))
        if spot <= 0:
            # Estimate from ATM option
            spot = float(min(calls, key=lambda o: abs(float(o.get('mark_price', 999999))))
                        .get('strike_price', 0))
        logger.info(f"✓ Spot: {spot:.2f}")

        # Find strikes — compute from percentage offsets
        buy_strike = spot * (1 + self.buy_offset_pct / 100)
        sell_strike = spot * (1 + self.sell_offset_pct / 100)
        hedge_strike = spot * (1 + self.hedge_offset_pct / 100)
        logger.info(f"  Computed strikes: Buy={buy_strike:.0f} (+{self.buy_offset_pct}%) | Sell={sell_strike:.0f} (+{self.sell_offset_pct}%) | Hedge={hedge_strike:.0f} (+{self.hedge_offset_pct}%)")

        logger.info(f"[3/5] Finding options...")
        buy_opt = self._find_strike(chain, buy_strike, 'call_options')
        sell_opt = self._find_strike(chain, sell_strike, 'call_options')
        hedge_opt = self._find_strike(chain, hedge_strike, 'call_options')

        if not buy_opt or not sell_opt or not hedge_opt:
            logger.warning("✗ Could not find all required strikes")
            return False

        # Check they're different strikes
        if buy_opt['strike_price'] == sell_opt['strike_price']:
            logger.warning("✗ Buy and sell strikes are the same — increase offsets")
            return False

        logger.info(f"  BUY  1x {buy_opt['symbol']} @ Strike {buy_opt['strike_price']} | ${float(buy_opt.get('mark_price',0)):.2f}")
        logger.info(f"  SELL 2x {sell_opt['symbol']} @ Strike {sell_opt['strike_price']} | ${float(sell_opt.get('mark_price',0)):.2f}")
        logger.info(f"  BUY  1x {hedge_opt['symbol']} @ Strike {hedge_opt['strike_price']} | ${float(hedge_opt.get('mark_price',0)):.2f}")

        # Get contract values
        for opt in [buy_opt, sell_opt, hedge_opt]:
            details = get_product_details(opt['product_id'])
            opt['contract_value'] = details['contract_value'] if details else 0.001

        # Calculate net credit/debit
        buy_cost = float(buy_opt.get('mark_price', 0)) * self.lot_size * buy_opt['contract_value']
        sell_credit = float(sell_opt.get('mark_price', 0)) * 2 * self.lot_size * sell_opt['contract_value']
        hedge_cost = float(hedge_opt.get('mark_price', 0)) * self.lot_size * hedge_opt['contract_value']
        net = sell_credit - buy_cost - hedge_cost
        logger.info(f"  Net credit/debit: ${net:.2f}")

        logger.info("[4/5] Placing orders...")
        # Buy 1 lot
        r1 = place_order(buy_opt['product_id'], buy_opt['symbol'], self.lot_size, 'buy')
        if not r1:
            logger.warning("✗ Failed to place buy order"); return False
        # Sell 2 lots
        r2 = place_order(sell_opt['product_id'], sell_opt['symbol'], self.lot_size * 2, 'sell')
        if not r2:
            logger.warning("✗ Failed to place sell order"); return False
        # Buy hedge
        r3 = place_order(hedge_opt['product_id'], hedge_opt['symbol'], self.lot_size, 'buy')
        if not r3:
            logger.warning("✗ Failed to place hedge order"); return False

        time.sleep(2)

        # Record legs
        self.legs = [
            {'symbol': buy_opt['symbol'], 'product_id': buy_opt['product_id'],
             'strike': buy_opt['strike_price'], 'side': 'buy', 'size': self.lot_size,
             'entry_price': float(buy_opt.get('mark_price', 0)), 'contract_value': buy_opt['contract_value']},
            {'symbol': sell_opt['symbol'], 'product_id': sell_opt['product_id'],
             'strike': sell_opt['strike_price'], 'side': 'sell', 'size': self.lot_size * 2,
             'entry_price': float(sell_opt.get('mark_price', 0)), 'contract_value': sell_opt['contract_value']},
            {'symbol': hedge_opt['symbol'], 'product_id': hedge_opt['product_id'],
             'strike': hedge_opt['strike_price'], 'side': 'buy', 'size': self.lot_size,
             'entry_price': float(hedge_opt.get('mark_price', 0)), 'contract_value': hedge_opt['contract_value']},
        ]

        # Estimate deployed margin
        self.deployed_margin = abs(net) + sell_credit  # rough estimate
        if self.deployed_margin <= 0:
            self.deployed_margin = sell_credit * 2

        logger.info("[5/5] Starting WebSocket monitoring...")
        self.ws_manager.start()
        time.sleep(1)
        self.ws_manager.subscribe([l['symbol'] for l in self.legs])

        logger.info("=" * 70)
        logger.info("✓ CALL RATIO SPREAD INITIALIZED")
        logger.info(f"  Deployed margin (est): ${self.deployed_margin:.2f}")
        logger.info(f"  Target: ${self.deployed_margin * self.target_pct / 100:.2f} | SL: ${self.deployed_margin * self.sl_pct / 100:.2f}")
        logger.info("=" * 70)
        return True

    def monitor(self):
        logger.info(f"[MONITORING] Call Ratio — every {self.monitoring_interval}s. Target {self.target_pct}% | SL {self.sl_pct}%")
        iteration = 0
        try:
            while self.running:
                iteration += 1
                ts = datetime.now().strftime("%H:%M:%S")
                total_pnl = 0

                leg_info = []
                for leg in self.legs:
                    ws = self.ws_manager.get_latest_price(leg['symbol'])
                    if ws:
                        mark = ws['mark_price']
                    else:
                        data = get_current_price(leg['product_id'], self.asset)
                        mark = data['mark_price'] if data else leg['entry_price']

                    direction = 1 if leg['side'] == 'buy' else -1
                    pnl = direction * (mark - leg['entry_price']) * leg['size'] * leg['contract_value']
                    total_pnl += pnl
                    leg['current_mark'] = mark
                    leg['current_pnl'] = round(pnl, 2)
                    leg_info.append(f"{leg['side'][0].upper()}{leg['size']}@{leg['strike']}: ${mark:.2f} ({pnl:+.2f})")

                self.total_pnl = round(total_pnl, 2)
                self.pnl_pct = round(total_pnl / self.deployed_margin * 100, 2) if self.deployed_margin > 0 else 0

                logger.info(f"[{ts}] #{iteration} | PnL: ${self.total_pnl:.2f} ({self.pnl_pct:+.1f}%) | {' | '.join(leg_info)}")

                # Check exit conditions
                if self.pnl_pct >= self.target_pct:
                    logger.info(f"🎯 TARGET HIT! PnL: ${self.total_pnl:.2f} ({self.pnl_pct:.1f}%)")
                    self.close_all()
                    self.status_msg = f'Target hit: {self.pnl_pct:.1f}%'
                    break
                if self.pnl_pct <= -self.sl_pct:
                    logger.info(f"🛑 STOP LOSS HIT! PnL: ${self.total_pnl:.2f} ({self.pnl_pct:.1f}%)")
                    self.close_all()
                    self.status_msg = f'SL hit: {self.pnl_pct:.1f}%'
                    break

                time.sleep(self.monitoring_interval)
        except KeyboardInterrupt:
            logger.info("[STOPPED] Manual stop")
            self.close_all()

    def close_all(self):
        logger.info("[CLOSING] All legs...")
        for leg in self.legs:
            close_side = 'sell' if leg['side'] == 'buy' else 'buy'
            place_order(leg['product_id'], leg['symbol'], leg['size'], close_side)
        time.sleep(2)
        self.ws_manager.stop()
        self.running = False
        logger.info(f"✓ Closed | Final PnL: ${self.total_pnl:.2f} ({self.pnl_pct:.1f}%)")

    @property
    def pnl(self):
        return self.total_pnl
