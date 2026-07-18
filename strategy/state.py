"""State persistence for DeltaNeutralStrategy.

Saves/loads strategy state to a JSON file so that on restart,
cumulative_realized_pnl, adjustment_count, positions, and other
critical state is preserved.
"""

import os
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Default state file location (relative to project root)
DEFAULT_STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
DEFAULT_STATE_FILE = os.path.join(DEFAULT_STATE_DIR, 'strategy_state.json')


def _ensure_dir(path):
    """Ensure the directory for the state file exists."""
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def save_state(strategy, state_file=None):
    """Save strategy state to disk.

    Persists everything needed to restore realized PnL and resume
    monitoring after a restart.
    """
    state_file = state_file or DEFAULT_STATE_FILE
    _ensure_dir(state_file)

    state = {
        'saved_at': datetime.now().isoformat(),
        'asset': strategy.asset,
        'expiry_date': strategy.expiry_date,
        'cumulative_realized_pnl': strategy.cumulative_realized_pnl,
        'realized_pnl_snapshot': strategy.realized_pnl_snapshot,
        'total_premium_collected': strategy.total_premium_collected,
        'target_pnl': strategy.target_pnl,
        'stop_loss': strategy.stop_loss,
        'adjustment_count': strategy.adjustment_count,
        'adjustment_history': strategy.adjustment_history,
        'call_position': strategy.call_position,
        'put_position': strategy.put_position,
        'call_entry_price': strategy.call_entry_price,
        'put_entry_price': strategy.put_entry_price,
        'call_actual_entry_price': strategy.call_actual_entry_price,
        'put_actual_entry_price': strategy.put_actual_entry_price,
        'call_contract_value': strategy.call_contract_value,
        'put_contract_value': strategy.put_contract_value,
        'lot_size': strategy.lot_size,
        'running': strategy.running,
    }

    try:
        # Write atomically: write to temp file then rename
        tmp_file = state_file + '.tmp'
        with open(tmp_file, 'w') as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_file, state_file)
        logger.debug(f"State saved: adj={state['adjustment_count']} rpnl=${state['cumulative_realized_pnl']:.2f}")
    except Exception as e:
        logger.error(f"Failed to save state: {e}")


def load_state(state_file=None):
    """Load strategy state from disk.

    Returns the state dict if file exists and is valid, None otherwise.
    """
    state_file = state_file or DEFAULT_STATE_FILE

    if not os.path.exists(state_file):
        logger.info("No state file found — starting fresh")
        return None

    try:
        with open(state_file, 'r') as f:
            state = json.load(f)
        logger.info(f"State loaded from {state_file}")
        logger.info(f"  Saved at: {state.get('saved_at')}")
        logger.info(f"  Realized PnL: ${state.get('cumulative_realized_pnl', 0):.2f}")
        logger.info(f"  Adjustments: {state.get('adjustment_count', 0)}")
        logger.info(f"  Call: {state.get('call_position', {}).get('symbol', 'N/A')}")
        logger.info(f"  Put: {state.get('put_position', {}).get('symbol', 'N/A')}")
        return state
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Failed to load state file: {e}")
        return None


def clear_state(state_file=None):
    """Remove the state file (called when strategy completes normally)."""
    state_file = state_file or DEFAULT_STATE_FILE
    if os.path.exists(state_file):
        os.remove(state_file)
        logger.info("State file cleared")
