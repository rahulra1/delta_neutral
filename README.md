# Delta Neutral Options Strategy Bot

Automated delta-neutral options selling bot for **BTC options** on [Delta Exchange India](https://india.delta.exchange). Sells a call and put at matching deltas (short strangle), monitors in real-time via WebSocket, and rebalances when premiums deviate.

## How It Works

1. **Entry** — Finds BTC call and put options near a target delta (default ±0.20) for a given expiry and sells both.
2. **Monitoring** — Tracks mark prices via WebSocket (REST API fallback) every few seconds.
3. **Adjustment** — When either leg's premium rises by 40%+, closes the *opposite* leg and re-enters a new option matching the triggered leg's current delta. Both baselines reset.
4. **Exit** — Closes all positions when total P&L (realized + unrealized) hits the target (default ±$10).

## Project Structure

```
delta_neutral/
├── main.py                  # Entry point
├── config/
│   └── __init__.py          # All settings (API keys, strategy params)
├── auth/
│   ├── signature.py         # HMAC-SHA256 signature generation
│   ├── headers.py           # Authenticated request headers
│   └── connection.py        # API connectivity & IP whitelist check
├── api/
│   ├── option_chain.py      # Fetch BTC option chain by expiry
│   ├── delta_finder.py      # Find options closest to target delta
│   ├── product_details.py   # Contract specs (value, currency)
│   ├── orders.py            # Place market orders
│   ├── pricing.py           # Get current mark price & greeks
│   ├── positions.py         # Fetch open positions & entry prices
│   └── pnl.py               # P&L calculation (realized + unrealized)
├── websocket/
│   └── __init__.py          # WebSocket manager for real-time feeds
└── strategy/
    └── __init__.py          # Core strategy (init, monitor, adjust, close)
```

## Setup

### Prerequisites

- Python 3.8+
- Delta Exchange India account with API key enabled
- API key IP whitelisted

### Install Dependencies

```bash
pip install requests websocket-client
```

### Configure API Keys

Set environment variables (recommended):

```bash
export DELTA_API_KEY="your-api-key"
export DELTA_API_SECRET="your-api-secret"
```

Or edit `config/__init__.py` directly.

### Strategy Parameters

All configurable in `config/__init__.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `EXPIRY_DATE` | `01-04-2026` | Option expiry date (DD-MM-YYYY) |
| `TARGET_DELTA` | `0.20` | Target delta for option selection |
| `DELTA_TOLERANCE` | `0.05` | Acceptable delta deviation |
| `LOT_SIZE` | `10` | Contracts per leg (10 = 0.01 BTC) |
| `PREMIUM_INCREASE_THRESHOLD` | `0.4` | 40% premium rise triggers adjustment |
| `TARGET_PNL` | `10` | Exit when total P&L hits ±$10 |
| `MONITORING_INTERVAL` | `5` | Seconds between status updates |

## Usage

```bash
cd delta_neutral
python main.py
```

The bot will:
1. Verify API connection and IP whitelisting
2. Fetch the option chain and find matching delta options
3. Sell both call and put
4. Start WebSocket monitoring with periodic status logs
5. Auto-adjust or exit based on thresholds

Press `Ctrl+C` to stop gracefully (closes all positions).

## Adjustment Logic

When a leg's premium increases ≥40% from its baseline:

- **Call premium spikes** → Close PUT, sell new PUT matching the call's current delta
- **Put premium spikes** → Close CALL, sell new CALL matching the put's current delta

Both entry baselines reset after each adjustment. Realized P&L from closed legs accumulates.

## Risk Considerations

- **No per-leg stop-loss** — only the combined P&L target triggers exit
- **Expiry risk** — near-expiry options have extreme theta decay and volatile delta
- **Slippage** — market orders may fill at different prices than mark
- **Shared state** — WebSocket callbacks and the monitor loop access strategy state without locks; race conditions are possible under rapid price moves

## License

For personal use. Use at your own risk.
