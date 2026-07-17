import logging

from auth import check_api_connection
from strategy.weekly_delta_neutral import WeeklyDeltaNeutral

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def main():
    if not check_api_connection():
        print("❌ Cannot proceed without proper API access")
        return

    # All strategy config lives in WeeklyDeltaNeutral
    strategy = WeeklyDeltaNeutral(
        asset='BTC',
        target_delta=0.20,
        delta_tolerance=0.05,
        lot_size=100,
        premium_threshold=0.4,
        tp_sl_percent=0.70,       # TP and SL = 70% of total premium collected
        max_adjustments=5,        # max 5 adjustments per cycle
        monitoring_interval=5,
        expiry_week=3,            # auto-select 3rd week Friday as expiry
        start_day='friday',       # can be any day: 'monday', 'tuesday', etc.
        entry_hour=21,            # 9 PM IST
        entry_minute=0,
    )

    if not strategy.initialize():
        print("✗ Strategy initialization failed")
        return

    try:
        strategy.monitor()
    except Exception as e:
        logger.error(f"Strategy error: {e}")
    finally:
        strategy.stop()

    print("=" * 70)
    print(f"STRATEGY STOPPED | Cumulative PnL: ${strategy.pnl:.2f} | Weeks: {strategy.weeks_traded}")
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[STOPPED] Program interrupted by user")
    except Exception as e:
        print(f"❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
