import json
import os
from datetime import datetime

HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'trade_history.json')


def _load():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            return json.load(f)
    return []


def _save(data):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def record_start(sid, params, user_id=None):
    history = _load()
    history.append({
        'sid': sid,
        'user_id': user_id,
        'started_at': datetime.now().isoformat(),
        'ended_at': None,
        'params': params,
        'pnl': 0,
        'adjustments': 0,
        'status': 'running',
    })
    _save(history)


def record_end(sid, pnl, adjustments):
    history = _load()
    for entry in reversed(history):
        if entry['sid'] == sid and entry['status'] == 'running':
            entry['ended_at'] = datetime.now().isoformat()
            entry['pnl'] = round(pnl, 2)
            entry['adjustments'] = adjustments
            entry['status'] = 'completed'
            break
    _save(history)


def get_history():
    return _load()
