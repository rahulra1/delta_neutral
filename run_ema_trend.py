"""Run the EMA Trend Follower strategy standalone.

Long-only basket:
  - Universe: top 50 perpetuals by 24h turnover, refreshed hourly.
  - Signal: daily 20/50 EMA crossover (bullish = buy, bearish = exit).
  - $100 notional per coin.

DRY-RUN by default. Set DELTA_LIVE=1 to place real orders.

When deployed inside the web app, the strategy is created/persisted per-account
via the app registry (like EMA Spread); this runner is for standalone use.
"""
import os
import logging

import config
from auth import check_api_connection
from strategy.ema_trend_follower import EmaTrendFollower


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

    strategy = EmaTrendFollower(
        top_n=50,
        notional_usd=100,
        ema_fast=20,
        ema_slow=50,
        ema_resolution='1d',
        refresh_interval=3600,   # hourly
        dry_run=not live,
    )

    if not strategy.initialize():
        print("✗ Failed to start")
        return

    try:
        strategy.monitor()
    except KeyboardInterrupt:
        print("\n[EMA Trend] Interrupted — stopping (positions kept)...")
        strategy._running = False
        strategy._persist_state()

    print(f"\n[EMA Trend] Final realized PnL: ${strategy.cumulative_pnl:+.2f} | "
          f"Open positions: {len(strategy.legs)}")


if __name__ == '__main__':
    main()
