"""Run the Open Interest based options strategy (daily recurring)."""
from auth import check_api_connection
from strategy.oi_strategy import OIStrategy


def main():
    if not check_api_connection():
        print("❌ Cannot proceed without proper API access")
        return

    print("=" * 60)
    print("  OI STRATEGY — DAILY AUTO-TRADE")
    print("=" * 60)

    strategy = OIStrategy(
        asset='BTC',
        lot_size=100,
        target_pct=0.50,
        stop_loss_pct=0.50,
        entry_hour=18,     # 6:30 PM IST
        entry_minute=30,
    )

    if not strategy.initialize():
        print("✗ Failed to start")
        return

    try:
        strategy.monitor()
    except KeyboardInterrupt:
        print("\n[OI] Interrupted — closing positions...")
        strategy.close_all()

    print(f"\n[OI] Final | Days: {strategy.total_days_traded} | PnL: ${strategy.pnl:+.2f}")


if __name__ == "__main__":
    main()
