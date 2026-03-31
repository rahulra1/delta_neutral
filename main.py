from auth import check_api_connection
from strategy import DeltaNeutralStrategy


def main():
    if not check_api_connection():
        print("❌ Cannot proceed without proper API access")
        return

    print("=" * 70)
    print("INITIALIZING DELTA NEUTRAL STRATEGY WITH WEBSOCKET")
    print("=" * 70)

    strategy = DeltaNeutralStrategy()

    if not strategy.initialize():
        print("✗ Strategy initialization failed")
        strategy.ws_manager.stop()
        return

    try:
        strategy.monitor_and_adjust()
    except Exception as e:
        print(f"✗ Strategy error: {e}")
        strategy.close_all_positions()
    finally:
        strategy.ws_manager.stop()

    print("=" * 70)
    print(f"STRATEGY COMPLETED | PnL: ${strategy.cumulative_realized_pnl:.2f} | Adjustments: {strategy.adjustment_count}")
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nPROGRAM INTERRUPTED BY USER")
    except Exception as e:
        print(f"❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
