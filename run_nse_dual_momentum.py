"""Run the NSE Dual Momentum Stocks strategy standalone.

Intraday long/short equity basket:
  - At entry time (default 9:30 AM IST), BUY the top-N NSE gainers and SHORT the
    top-N losers, ₹10,000 each (whole shares).
  - Track each position: TP +5% / SL -3% (side-aware), checked every 30s.
  - Square off every open position at 15:15 IST (hard EOD 15:30). Never overnight.

PAPER by default (no real orders; fills simulated at last traded price).
Set NSE_LIVE=1 to place real intraday (MIS) equity orders via Groww.
"""
import os
import logging

import config
from strategy.nse_dual_momentum_stocks import NseDualMomentumStocks


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        datefmt='%H:%M:%S')
    live = os.environ.get('NSE_LIVE', '0') == '1'
    if live:
        config.set_thread_broker('groww')

    strategy = NseDualMomentumStocks(
        top_n=5,
        amount_per_stock=10000,
        tp_pct=0.05,
        sl_pct=0.03,
        entry_hour=9, entry_minute=30,
        exit_hour=15, exit_minute=15,
        dry_run=not live,
        run_immediately=os.environ.get('NSE_RUN_NOW', '0') == '1',
    )

    if not strategy.initialize():
        print("✗ Failed to start")
        return

    try:
        strategy.monitor()
    except KeyboardInterrupt:
        print("\n[NSE Dual Momentum] Interrupted — closing positions...")
        strategy.close_all()

    print(f"\n[NSE Dual Momentum] Final realized PnL: ₹{strategy.cumulative_pnl:+.2f} | "
          f"Open: {sum(1 for l in strategy.legs if not l.get('stopped'))}")


if __name__ == '__main__':
    main()
