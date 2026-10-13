"""Run the Dual Momentum Basket strategy standalone.

Twice-daily long/short basket:
  - At 06:00 and 18:00 IST, fetch the top-N 24h gainers and losers.
  - LONG the gainers, SHORT the losers, ~$100 notional each.
  - Track each position: TP +10% / SL -5% (side-aware), checked every 60s.
  - At each scheduled run, close any held coin no longer in its side's list.

DRY-RUN by default. Set DELTA_LIVE=1 to place real orders.

When deployed inside the web app, the strategy is created/persisted per-account
via the app registry (like EMA Spread); this runner is for standalone use.
"""
import os
import logging

import config
from auth import check_api_connection
from strategy.dual_momentum_basket import DualMomentumBasket


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )

    live = os.environ.get('DELTA_LIVE', '0') == '1'

    # Use the production India broker so the universe reflects the live market
    # (the default 'demo' broker points at testnet, which lists far fewer coins).
    config.set_thread_broker('delta_exchange')

    if live and not check_api_connection():
        print("❌ Cannot trade live without proper API access")
        return

    strategy = DualMomentumBasket(
        top_n=10,
        notional_usd=10,         # fixed $10 invested per coin
        run_hours=(6, 18),       # 06:00 and 18:00 IST
        tp_pct=0.10,             # +10% take profit
        sl_pct=0.05,             # -5% stop loss
        rotate_on_runs='all',    # close delisted coins at every run (6 AM & 6 PM)
        dry_run=not live,
    )

    if not strategy.initialize():
        print("✗ Failed to start")
        return

    # Optional: run one rebalance immediately on launch for testing, instead of
    # waiting for the next 06:00/18:00 slot. Set DUAL_RUN_NOW=1.
    if os.environ.get('DUAL_RUN_NOW', '0') == '1':
        strategy.run_rebalance()

    try:
        strategy.monitor()
    except KeyboardInterrupt:
        print("\n[Dual Momentum] Interrupted — stopping (positions kept)...")
        strategy._running = False
        strategy._persist_state()

    print(f"\n[Dual Momentum] Final realized PnL: ${strategy.cumulative_pnl:+.2f} | "
          f"Open positions: {len(strategy.legs)}")


if __name__ == '__main__':
    main()
