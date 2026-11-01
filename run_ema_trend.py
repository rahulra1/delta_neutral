"""Run the EMA Trend Follower strategy standalone.

Daily EMA trend follower (see strategy/ema_trend_follower.py):
  - Universe: the 10 Delta perpetuals with the highest 30-day average open interest.
  - Signal: daily EMA10/30 — long while above, short while below; BTC regime filter.
  - Exit: when the coin's EMA state flips. Evaluated once a day just after 00:00 UTC.
  - Size: $25 per ATR20 move, max $500 per coin, max $7,500 gross.

DRY-RUN by default. Set DELTA_LIVE=1 to place real orders.

When deployed inside the web app, the strategy is created/persisted per-account
via the app registry; this runner is for standalone use.
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

    # Use the production India broker so orders go to the live market
    # (the default 'demo' broker points at testnet, which lists far fewer coins).
    config.set_thread_broker('delta_exchange')

    if live and not check_api_connection():
        print("❌ Cannot trade live without proper API access")
        return

    strategy = EmaTrendFollower(
        top_n=10,
        notional_usd=500,          # max notional per coin
        ema_fast=10,
        ema_slow=30,
        risk_per_atr_usd=25,
        max_gross_usd=7500,
        allow_shorts=True,
        btc_regime=True,
        refresh_interval=300,      # checks every 5 min; trades once per daily close
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
