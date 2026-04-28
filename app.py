import threading
import queue
import uuid
import os
import sys
import time
import logging
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
import config as default_config
from flask import Flask, request, jsonify, Response, session, g, send_from_directory
from functools import wraps
from auth import check_api_connection
from strategy import DeltaNeutralStrategy
from trade_history import record_start, record_end, get_history
from models import init_db, create_user, verify_user, get_user, update_api_keys, get_profiles, get_profile, create_profile, update_profile, delete_profile, get_user_credits, deduct_credits, add_credits, set_user_plan, get_credit_history, is_admin, set_admin, get_all_users, get_all_plans, CREDIT_COSTS, save_strategy, update_strategy_db, get_live_strategies, delete_strategy_db, get_db, save_pnl_snapshot, get_pnl_snapshots
from strategy.tracker import TrackedStrategy, registry
from api.position_tracker import position_tracker

logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder=None)
_secret = os.environ.get('FLASK_SECRET_KEY')
if not _secret:
    import secrets as _secrets
    _secret = _secrets.token_hex(32)
    logger.warning("⚠ FLASK_SECRET_KEY not set — using random key (sessions won't survive restarts)")
app.secret_key = _secret
JWT_SECRET = _secret

init_db()

# Lock protecting strategies, all_tracked, and active_monitors
_state_lock = threading.Lock()

# {sid: {thread, strategy, log_queue, running, params, user_id}}
strategies = {}

# Unified tracker for all strategies from any source
# {sid: {source, name, status, user_id, pnl, started_at, details, ...}}
all_tracked = {}

# {monitor_id: {monitor, user_id, profile_id}}
active_monitors = {}

# Resume strategies from DB on startup
def _resume_db_strategies():
    from models import get_db
    from strategy.monitor import StrategyMonitor
    import json as _json
    conn = get_db()
    rows = conn.execute("SELECT * FROM live_strategies WHERE status IN ('running', 'open (no monitor)')").fetchall()
    conn.close()
    for r in rows:
        d = dict(r)
        sid = d['sid']
        legs = _json.loads(d['legs']) if d['legs'] else []
        details = _json.loads(d['details']) if d['details'] else {}
        user_id = d['user_id']
        source = d['source']
        max_profit = d.get('max_profit', 0) or 0
        max_loss = d.get('max_loss', 0) or 0
        asset = d.get('asset', 'BTC')
        lot_size = d.get('lot_size', 0.001) or 0.001
        profile_id = d.get('profile_id')

        # 1. Restore all_tracked
        all_tracked[sid] = {
            'sid': sid, 'source': source, 'name': d['name'],
            'user_id': user_id, 'status': d['status'],
            'started_at': d['started_at'], 'pnl': d['pnl'] or 0,
            'details': details,
        }

        # 2. Restore position_tracker
        for leg in legs:
            position_tracker.open(user_id, leg.get('product_id'), leg.get('symbol') or '',
                type=leg.get('type') or '', strike=leg.get('strike') or '',
                side=leg.get('side') or '', size=int(leg.get('size') or 0),
                entry_price=float(leg.get('entry_price') or 0),
                asset=asset, source=source)

        if not legs:
            continue

        # Skip strategies where all legs have no product_id (invalid/empty data)
        valid_legs = [l for l in legs if l.get('product_id')]
        if not valid_legs:
            logger.warning(f"[resume] Skipping {sid} — no valid legs (missing product_id)")
            all_tracked[sid]['status'] = 'closed'
            try:
                update_strategy_db(sid, status='closed', exit_reason='invalid_legs')
            except Exception:
                pass
            continue

        if source in ('Option Chain', 'Strategy Builder') and max_profit > 0 and max_loss > 0:
            mon = StrategyMonitor(
                legs=legs, max_profit=max_profit, max_loss=max_loss,
                asset=asset, lot_size=lot_size,
            )
            mon.current_pnl = d['pnl'] or 0
            mon.user_id = user_id
            mon.sid = sid
            mon.profile_id = profile_id
            active_monitors[sid] = {'monitor': mon, 'user_id': user_id, 'profile_id': profile_id}
            mon.on_complete = lambda pnl, reason, s=sid: (update_tracked(s, status='completed', pnl=round(pnl, 2)), record_end(s, pnl, 0))
            mon._log("🔄 Resumed after restart")
            mon.start()
            logger.info(f"[resume] Resumed monitor {sid} — {d['name']}")
        # 4. Restore strategies dict (for AlgoX DN) — as TrackedStrategy since
        #    DeltaNeutralStrategy needs live WebSocket state that can't be restored
        elif source == 'AlgoX DN':
            strat = TrackedStrategy(
                sid=sid, source=source, name=d['name'],
                user_id=user_id, legs=legs, asset=asset,
                lot_size=lot_size, max_profit=max_profit, max_loss=max_loss,
                profile_id=profile_id, interval=d.get('interval', 10),
                details=details,
            )
            strat.started_at = d['started_at']
            strat.current_pnl = d['pnl'] or 0
            strat.adjustment_count = d.get('adjustment_count', 0)
            strat.log("🔄 Resumed after restart (monitoring only — adjustments disabled)")
            registry.register(strat)
            strat.start_monitoring()
            logger.info(f"[resume] Resumed DN strategy {sid} — {d['name']}")

        # 5. Everything else — use TrackedStrategy
        else:
            strat = TrackedStrategy(
                sid=sid, source=source, name=d['name'],
                user_id=user_id, legs=legs, asset=asset,
                lot_size=lot_size, max_profit=max_profit, max_loss=max_loss,
                profile_id=profile_id, interval=d.get('interval', 10),
                details=details,
            )
            strat.started_at = d['started_at']
            strat.current_pnl = d['pnl'] or 0
            strat.adjustment_count = d.get('adjustment_count', 0)
            strat.log("🔄 Resumed after restart")
            registry.register(strat)
            strat.start_monitoring()
            logger.info(f"[resume] Resumed strategy {sid} — {d['name']}")

_db_resumed = False

@app.before_request
def _resume_once():
    global _db_resumed
    if not _db_resumed:
        _db_resumed = True
        _resume_db_strategies()

def track_strategy(sid, source, name, user_id, details=None):
    """Register a strategy in the unified tracker."""
    with _state_lock:
        all_tracked[sid] = {
            'sid': sid, 'source': source, 'name': name,
            'user_id': user_id, 'status': 'running',
            'started_at': datetime.now().isoformat(),
            'pnl': 0, 'details': details or {},
        }
        started_at = all_tracked[sid]['started_at']
    try:
        legs = (details or {}).get('legs', [])
        save_strategy(sid, user_id, source, name, 'running',
                      started_at, details=details, legs=legs,
                      max_profit=(details or {}).get('max_profit', 0),
                      max_loss=(details or {}).get('max_loss', 0),
                      profile_id=(details or {}).get('profile_id'),
                      asset=(details or {}).get('asset', 'BTC'))
    except Exception:
        pass

def update_tracked(sid, **kwargs):
    with _state_lock:
        if sid in all_tracked:
            all_tracked[sid].update(kwargs)
    try:
        update_strategy_db(sid, **{k: v for k, v in kwargs.items()
                                   if k in ('status', 'pnl', 'details', 'legs', 'exit_reason', 'adjustment_count')})
    except Exception:
        pass


def _get_jwt_user_id():
    """Extract user_id from JWT Bearer token or query param."""
    token = None
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
    if not token:
        token = request.args.get('token')
    if token:
        try:
            payload = pyjwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            return payload.get('user_id')
        except pyjwt.ExpiredSignatureError:
            return None
        except pyjwt.InvalidTokenError:
            return None
    return None


def current_user_id():
    uid = _get_jwt_user_id()
    if uid:
        return uid
    return session.get('user_id')


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = current_user_id()
        if not uid:
            if request.path.startswith('/api/'):
                return jsonify(error='Unauthorized'), 401
            return jsonify(error='Unauthorized'), 401
        g.user_id = uid
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = current_user_id()
        if not uid or not is_admin(uid):
            return jsonify(error='Admin access required'), 403
        return f(*args, **kwargs)
    return decorated


def credits_required(action):
    """Decorator that checks and deducts credits before running the endpoint."""
    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            uid = current_user_id()
            ok, cost = deduct_credits(uid, action, f.__name__)
            if not ok:
                creds = get_user_credits(uid)
                return jsonify(error=f'Insufficient credits. Need {cost}, have {creds["credits_remaining"] if creds else 0}. Upgrade your plan.'), 402
            return f(*args, **kwargs)
        return decorated
    return wrapper


def _make_token(user_id):
    return pyjwt.encode({'user_id': user_id, 'exp': datetime.now(tz=timezone.utc) + timedelta(days=7)}, JWT_SECRET, algorithm='HS256')


# ── JWT Auth API ──

@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    d = request.json or {}
    user = verify_user(d.get('username', ''), d.get('password', ''))
    if not user:
        return jsonify(error='Invalid credentials'), 401
    return jsonify(token=_make_token(user['id']), user={'id': user['id'], 'username': user['username'], 'is_admin': bool(user.get('is_admin'))})


@app.route('/api/auth/register', methods=['POST'])
def api_auth_register():
    d = request.json or {}
    username = (d.get('username') or '').strip()
    password = d.get('password', '')
    if not username or len(password) < 6:
        return jsonify(error='Username required, password min 6 chars'), 400
    if not create_user(username, password):
        return jsonify(error='Username already taken'), 400
    user = verify_user(username, password)
    return jsonify(token=_make_token(user['id']), user={'id': user['id'], 'username': user['username'], 'is_admin': False})


# ── Serve React frontend ──

# React catch-all moved to end of file


class LogCapture:
    """Thread-aware stdout that routes print() to the correct strategy's log queue.
    Kept as fallback for any remaining print() calls or third-party library output."""
    _local = threading.local()

    def __init__(self, original):
        self.original = original

    def write(self, text):
        self.original.write(text)
        q = getattr(LogCapture._local, 'log_queue', None)
        if q and text.strip():
            q.put(text.strip())
        h = getattr(LogCapture._local, 'log_history', None)
        if h is not None and text.strip():
            h.append(text.strip())
            if len(h) > 500:
                del h[:len(h)-500]

    def flush(self):
        self.original.flush()


class _StrategyQueueHandler(logging.Handler):
    """Logging handler that routes log records to the thread-local strategy queue."""
    def emit(self, record):
        msg = self.format(record)
        q = getattr(LogCapture._local, 'log_queue', None)
        if q and msg.strip():
            q.put(msg.strip())
        h = getattr(LogCapture._local, 'log_history', None)
        if h is not None and msg.strip():
            h.append(msg.strip())
            if len(h) > 500:
                del h[:len(h)-500]


def _setup_logging():
    """Configure root logger with both console and strategy-queue handlers."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        console = logging.StreamHandler(sys.__stderr__)
        console.setFormatter(logging.Formatter('%(message)s'))
        root.addHandler(console)
        queue_handler = _StrategyQueueHandler()
        queue_handler.setFormatter(logging.Formatter('%(message)s'))
        root.addHandler(queue_handler)


_setup_logging()


def _save_dn_legs(sid, s):
    """Extract call/put legs from a DeltaNeutralStrategy and persist to DB."""
    legs = []
    for leg_name in ('call', 'put'):
        pos = getattr(s, f'{leg_name}_position', None)
        if pos:
            legs.append({
                'product_id': pos.get('product_id'),
                'symbol': pos.get('symbol', ''),
                'type': leg_name,
                'strike': pos.get('strike_price', ''),
                'side': 'sell',
                'size': s.lot_size,
                'entry_price': round(getattr(s, f'{leg_name}_actual_entry_price', 0), 2),
            })
    if legs:
        try:
            update_strategy_db(sid, legs=legs, adjustment_count=s.adjustment_count)
        except Exception:
            pass


# Install once at import time — all threads share this, but each routes to its own queue
import sys
sys.stdout = LogCapture(sys.stdout)


def _setup_strategy_thread(entry):
    """Common setup for strategy runner threads: log routing + credential resolution.
    Returns True if setup succeeded, False if it failed (error already logged to queue)."""
    LogCapture._local.log_queue = entry['log_queue']
    LogCapture._local.log_history = entry['log_history']
    from config import set_thread_credentials
    profile_id = entry.get('profile_id')
    if not profile_id:
        entry['log_queue'].put("❌ No profile selected. Please select an API profile.")
        entry['running'] = False
        return False
    p = get_profile(int(profile_id), entry['user_id'])
    if not p:
        entry['log_queue'].put("❌ Profile not found.")
        entry['running'] = False
        return False
    set_thread_credentials(p['api_key'], p['api_secret'], p.get('broker'))
    if not check_api_connection():
        entry['log_queue'].put("❌ Cannot connect to API")
        entry['running'] = False
        return False
    return True


def _teardown_strategy_thread(entry):
    """Common cleanup for strategy runner threads."""
    LogCapture._local.log_queue = None
    LogCapture._local.log_history = None
    entry['running'] = False
    entry['log_queue'].put("__STOPPED__")


def run_strategy(sid, params):
    entry = strategies[sid]
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return

    try:
        s = DeltaNeutralStrategy(
            asset=params.get('asset', 'BTC'),
            expiry_date=params['expiry_date'],
            target_delta=float(params['target_delta']),
            delta_tolerance=float(params['delta_tolerance']),
            lot_size=int(params['lot_size']),
            premium_threshold=float(params['premium_threshold']) / 100,
            target_pnl=float(params['target_pnl']),
            max_adjustments=int(params['max_adjustments']),
            monitoring_interval=int(params['monitoring_interval']),
        )

        entry['strategy'] = s
        entry['running'] = True

        if not s.initialize():
            entry['log_queue'].put("✗ Strategy initialization failed")
            s.ws_manager.stop()
            entry['running'] = False
            return

        # Save legs to DB so they survive restart
        _save_dn_legs(sid, s)

        # Hook: save legs after each adjustment
        _orig_adjust = s.adjust_position
        def _hooked_adjust(*a, **kw):
            _orig_adjust(*a, **kw)
            _save_dn_legs(sid, s)
        s.adjust_position = _hooked_adjust

        s.monitor_and_adjust()
    except Exception as e:
        entry['log_queue'].put(f"✗ Error: {e}")
        if entry.get('strategy'):
            entry['strategy'].close_all_positions()
    finally:
        pnl = entry['strategy'].cumulative_realized_pnl if entry.get('strategy') else 0
        adj = entry['strategy'].adjustment_count if entry.get('strategy') else 0
        if entry.get('strategy'):
            _save_dn_legs(sid, entry['strategy'])
        record_end(sid, pnl, adj)
        update_tracked(sid, status='completed', pnl=round(pnl, 2))
        if entry.get('strategy'):
            entry['strategy'].ws_manager.stop()
        _teardown_strategy_thread(entry)


# ── Old template routes removed — React frontend serves all pages ──


# ── Profile API ──

def get_profile_creds(profile_id):
    """Get API credentials + broker from a profile, or fall back to user's default keys."""
    uid = current_user_id()
    if profile_id:
        p = get_profile(int(profile_id), uid)
        if p:
            return p['api_key'], p['api_secret'], p['name'], p.get('broker', 'demo')
    user = get_user(uid)
    if user and user.get('api_key') and user.get('api_secret'):
        return user['api_key'], user['api_secret'], 'Default', 'demo'
    return None, None, None, None


@app.route('/api/profiles')
@login_required
def api_profiles():
    return jsonify(profiles=get_profiles(current_user_id()))


@app.route('/api/brokers')
@login_required
def api_brokers():
    from config import BROKERS
    return jsonify(brokers=[
        {'id': k, 'name': mod.BROKER_NAME}
        for k, mod in BROKERS.items()
    ])


@app.route('/api/profiles', methods=['POST'])
@login_required
def api_create_profile():
    d = request.json
    name = (d.get('name') or '').strip()
    api_key = (d.get('api_key') or '').strip()
    api_secret = (d.get('api_secret') or '').strip()
    broker = (d.get('broker') or 'demo').strip()
    if not name or not api_key or not api_secret:
        return jsonify(error="Name, API key, and secret are required"), 400
    create_profile(current_user_id(), name, api_key, api_secret, broker)
    return jsonify(status="created")


@app.route('/api/profiles/<int:pid>', methods=['PUT'])
@login_required
def api_update_profile(pid):
    d = request.json
    update_profile(pid, current_user_id(), d.get('name',''), d.get('api_key',''), d.get('api_secret',''), d.get('broker', 'demo'))
    return jsonify(status="updated")


@app.route('/api/profiles/<int:pid>', methods=['DELETE'])
@login_required
def api_delete_profile(pid):
    delete_profile(pid, current_user_id())
    return jsonify(status="deleted")


@app.route('/api/test-connection')
@login_required
def api_test_connection():
    """Test if an API profile can connect to Delta Exchange."""
    from config import set_thread_credentials
    api_key, api_secret, _, broker = get_profile_creds(request.args.get('profile_id'))
    if not api_key:
        return jsonify(success=False, error="No keys")
    set_thread_credentials(api_key, api_secret, broker)
    try:
        ok = check_api_connection()
        return jsonify(success=ok)
    except Exception:
        return jsonify(success=False)


# ── Strategy Routes (per-user isolated) ──

@app.route('/api/dashboard')
@login_required
def api_dashboard():
    """Compute dashboard stats from trade history + DB."""
    uid = current_user_id()
    all_history = get_history()
    with _state_lock:
        user_sids = {sid for sid, e in strategies.items() if e.get('user_id') == uid}
        user_sids.update(sid for sid, t in all_tracked.items() if t.get('user_id') == uid)
    user_sids.update(s.sid for s in registry.get_user_strategies(uid))
    trades = [t for t in all_history if t.get('sid') in user_sids or t.get('user_id') == uid]

    # Include DB-tracked strategies not in trade history
    trade_sids = {t.get('sid') for t in trades}
    with _state_lock:
        for sid, t in all_tracked.items():
            if t.get('user_id') != uid or sid in trade_sids:
                continue
            trades.append({
                'sid': sid, 'user_id': uid, 'status': t.get('status', 'running'),
                'started_at': t.get('started_at', ''), 'ended_at': None,
                'pnl': t.get('pnl', 0), 'params': t.get('details', {}),
                'adjustments': 0,
            })

    # Also include completed/closed strategies from DB not yet in trades
    try:
        import json as _json
        conn = get_db()
        db_rows = conn.execute('SELECT * FROM live_strategies WHERE user_id=?', (uid,)).fetchall()
        conn.close()
        for r in db_rows:
            d = dict(r)
            if d['sid'] in trade_sids or d['sid'] in {t.get('sid') for t in trades}:
                continue
            trades.append({
                'sid': d['sid'], 'user_id': uid, 'status': d['status'],
                'started_at': d['started_at'], 'ended_at': None,
                'pnl': d.get('pnl', 0) or 0,
                'params': _json.loads(d.get('details') or '{}'),
                'adjustments': d.get('adjustment_count', 0),
            })
    except Exception:
        pass

    # Inject live PnL for running strategies into trade list
    with _state_lock:
        for t in trades:
            sid = t.get('sid')
            if t.get('status') == 'running':
                # Check active monitors (Option Chain / Strategy Builder)
                if sid in active_monitors and active_monitors[sid].get('user_id') == uid:
                    mon = active_monitors[sid]['monitor']
                    t['pnl'] = round(mon.current_pnl, 2)
                    if not mon.running:
                        t['status'] = 'completed'
                # Check old strategies dict (Delta Neutral)
                elif sid in strategies and strategies[sid].get('strategy'):
                    t['pnl'] = round(strategies[sid]['strategy'].total_pnl, 2)
                # Check IV Crush
                elif sid in iv_crush_strategies and iv_crush_strategies[sid].get('strategy'):
                    t['pnl'] = round(iv_crush_strategies[sid]['strategy'].total_pnl, 2)
                # Check Call Ratio
                elif sid in call_ratio_strategies and call_ratio_strategies[sid].get('strategy'):
                    t['pnl'] = round(call_ratio_strategies[sid]['strategy'].total_pnl, 2)
                # Check unified tracker
                rs = registry.get(sid)
                if rs and rs.running:
                    t['pnl'] = rs.current_pnl

        completed = [t for t in trades if t.get('status') == 'completed']
        running_count = sum(1 for sid, e in strategies.items() if e.get('user_id') == uid and e.get('running'))
        running_count += sum(1 for sid, e in active_monitors.items() if e.get('user_id') == uid and e['monitor'].running)
        running_count += sum(1 for sid, e in iv_crush_strategies.items() if e.get('user_id') == uid and e.get('running'))
        running_count += sum(1 for sid, e in call_ratio_strategies.items() if e.get('user_id') == uid and e.get('running'))
    running_count += len(registry.get_running(uid))
    pnls = [t.get('pnl', 0) for t in completed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)

    # P&L over time for chart — sorted by end date, deduplicated per day
    completed_sorted = sorted(completed, key=lambda t: t.get('ended_at') or t.get('started_at', ''))
    pnl_by_date = {}
    cumulative = 0
    for t in completed_sorted:
        cumulative += t.get('pnl', 0)
        date_key = (t.get('ended_at') or t.get('started_at', ''))[:10]
        pnl_by_date[date_key] = round(cumulative, 2)
    pnl_series = [{'date': d, 'pnl': p} for d, p in pnl_by_date.items()]

    # Asset allocation from params
    asset_counts = {}
    for t in trades:
        asset = t.get('params', {}).get('asset', 'BTC')
        asset_counts[asset] = asset_counts.get(asset, 0) + 1

    return jsonify(
        total_pnl=round(total_pnl, 2),
        open_positions=running_count,
        total_trades=len(completed),
        win_rate=round(len(wins)/len(completed)*100, 2) if completed else 0,
        avg_gain=round(sum(wins)/len(wins), 2) if wins else 0,
        avg_loss=round(sum(losses)/len(losses), 2) if losses else 0,
        big_win=round(max(wins), 2) if wins else 0,
        big_loss=round(min(losses), 2) if losses else 0,
        max_drawdown=round(min(pnls), 2) if pnls else 0,
        profitable_trades=len(wins),
        losing_trades=len(losses),
        pnl_series=pnl_series,
        asset_allocation=asset_counts,
        recent_trades=trades[-20:][::-1],
    )


PEER_PORT = os.environ.get('ALGOX_PEER_PORT', '')  # Set by deploy script to old instance port


def _fetch_peer_strategies(uid, token):
    """Fetch running strategies from the peer (old) instance."""
    if not PEER_PORT:
        return []
    try:
        import requests as req
        r = req.get(f'http://127.0.0.1:{PEER_PORT}/api/strategies',
                     headers={'Authorization': f'Bearer {token}'}, timeout=3)
        if r.ok:
            return r.json().get('strategies', [])
    except Exception:
        pass
    return []


@app.route('/api/strategies')
@login_required
def api_all_strategies():
    """Return all tracked strategies for the current user with live PnL."""
    uid = current_user_id()
    from api.live_pnl import compute_live_legs
    result = []
    with _state_lock:
        for sid, t in all_tracked.items():
            if t['user_id'] != uid:
                continue
            entry = dict(t)
            if entry['status'] in ('running', 'open (no monitor)'):
                if sid in strategies and strategies[sid].get('strategy'):
                    s = strategies[sid]['strategy']
                    entry['pnl'] = round(getattr(s, 'total_pnl', 0), 2)
                elif sid in active_monitors:
                    mon = active_monitors[sid]['monitor']
                    entry['pnl'] = round(mon.current_pnl, 2)
                    if not mon.running:
                        entry['status'] = 'completed'
                elif sid in iv_crush_strategies and iv_crush_strategies[sid].get('strategy'):
                    entry['pnl'] = round(iv_crush_strategies[sid]['strategy'].total_pnl, 2)
                elif sid in call_ratio_strategies and call_ratio_strategies[sid].get('strategy'):
                    entry['pnl'] = round(call_ratio_strategies[sid]['strategy'].total_pnl, 2)
                else:
                    # No monitor — compute live P&L from legs
                    raw_legs = entry.get('details', {}).get('legs', [])
                    asset = entry.get('details', {}).get('asset', 'BTC')
                    if raw_legs:
                        _, pnl = compute_live_legs(raw_legs, asset)
                        entry['pnl'] = pnl
            result.append(entry)

    # Merge strategies from unified tracker
    for ts in registry.get_user_strategies(uid):
        if ts.sid not in {s['sid'] for s in result}:
            st = ts.get_status()
            st.pop('logs', None)
            result.append(st)

    # Merge running strategies from peer (old) instance
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    peer = _fetch_peer_strategies(uid, token)
    local_sids = {s['sid'] for s in result}
    for ps in peer:
        if ps['sid'] not in local_sids:
            ps['_peer'] = True
            result.append(ps)

    return jsonify(strategies=result)


@app.route('/api/strategies/<sid>/close', methods=['POST'])
@login_required
def api_close_strategy(sid):
    """Close a single strategy by sid."""
    uid = current_user_id()

    # If not local, proxy to peer
    with _state_lock:
        found = sid in all_tracked
    if not found and PEER_PORT:
        try:
            import requests as req
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            r = req.post(f'http://127.0.0.1:{PEER_PORT}/api/strategies/{sid}/close',
                         headers={'Authorization': f'Bearer {token}'}, timeout=10)
            return jsonify(r.json()), r.status_code
        except Exception:
            return jsonify(error="Not found"), 404

    with _state_lock:
        if sid not in all_tracked or all_tracked[sid]['user_id'] != uid:
            return jsonify(error="Not found"), 404

    from config import set_thread_credentials
    from api.orders import place_order

    # Resolve profile_id from whichever source has it
    with _state_lock:
        profile_id = None
        if sid in strategies:
            profile_id = strategies[sid].get('profile_id')
        elif sid in active_monitors:
            profile_id = active_monitors[sid].get('profile_id')
        elif sid in iv_crush_strategies:
            profile_id = iv_crush_strategies[sid].get('profile_id')
        elif sid in call_ratio_strategies:
            profile_id = call_ratio_strategies[sid].get('profile_id')
        if not profile_id:
            rs = registry.get(sid)
            if rs:
                profile_id = rs.profile_id
        if not profile_id and all_tracked.get(sid, {}).get('details', {}).get('profile_id'):
            profile_id = all_tracked[sid]['details']['profile_id']

    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if api_key:
        set_thread_credentials(api_key, api_secret, broker)

    closed = False
    # Delta Neutral strategy
    if sid in strategies:
        e = strategies[sid]
        if e.get('strategy'):
            e['strategy'].running = False
            e['strategy'].close_all_positions()
            closed = True
    # IV Crush strategy
    if not closed and sid in iv_crush_strategies:
        ic = iv_crush_strategies[sid]
        if ic.get('strategy'):
            ic['strategy'].running = False
            ic['strategy'].close_all()
            closed = True
    # Call Ratio strategy
    if not closed and sid in call_ratio_strategies:
        cr = call_ratio_strategies[sid]
        if cr.get('strategy'):
            cr['strategy'].running = False
            cr['strategy'].close_all()
            closed = True
    # Option Chain monitor
    if not closed and sid in active_monitors:
        active_monitors[sid]['monitor'].stop()
        closed = True
    # TrackedStrategy (registry)
    if not closed:
        rs = registry.get(sid)
        if rs and rs.user_id == uid:
            rs.close()
            closed = True
    # Fallback — close positions by reversing each leg
    if not closed and api_key:
        details = all_tracked[sid].get('details', {})
        placed_legs = details.get('legs', [])
        if isinstance(placed_legs, list) and placed_legs:
            failed = []
            for leg in placed_legs:
                try:
                    close_side = 'buy' if leg['side'] == 'sell' else 'sell'
                    result = place_order(leg['product_id'], leg['symbol'], int(leg['size']), close_side)
                    if result is None:
                        failed.append(leg.get('symbol', 'unknown'))
                except Exception as e:
                    failed.append(f"{leg.get('symbol')}: {e}")
            if failed:
                return jsonify(success=False, error=f"Failed to close: {', '.join(failed)}"), 500
            closed = True

    if not closed:
        return jsonify(success=False, error="Failed to close strategy"), 500

    update_tracked(sid, status='closed')
    return jsonify(success=True, status='closed')


@app.route('/api/strategies/close-all', methods=['POST'])
@login_required
def api_close_all_strategies():
    """Close all running strategies for the current user."""
    uid = current_user_id()
    from config import set_thread_credentials
    closed_count = 0

    with _state_lock:
        items_to_close = [(sid, dict(t)) for sid, t in all_tracked.items()
                          if t['user_id'] == uid and t['status'] in ('running', 'open (no monitor)')]

    for sid, t in items_to_close:

        # Resolve profile_id from all sources
        with _state_lock:
            profile_id = None
            if sid in strategies:
                profile_id = strategies[sid].get('profile_id')
            elif sid in active_monitors:
                profile_id = active_monitors[sid].get('profile_id')
            elif sid in iv_crush_strategies:
                profile_id = iv_crush_strategies[sid].get('profile_id')
            elif sid in call_ratio_strategies:
                profile_id = call_ratio_strategies[sid].get('profile_id')
            if not profile_id:
                rs = registry.get(sid)
                if rs:
                    profile_id = rs.profile_id
            if not profile_id and t.get('details', {}).get('profile_id'):
                profile_id = t['details']['profile_id']

        api_key, api_secret, _, broker = get_profile_creds(profile_id)
        if api_key:
            set_thread_credentials(api_key, api_secret, broker)

        closed = False
        if sid in strategies and strategies[sid].get('strategy'):
            strategies[sid]['strategy'].running = False
            strategies[sid]['strategy'].close_all_positions()
            closed = True
        if not closed and sid in iv_crush_strategies and iv_crush_strategies[sid].get('strategy'):
            iv_crush_strategies[sid]['strategy'].running = False
            iv_crush_strategies[sid]['strategy'].close_all()
            closed = True
        if not closed and sid in call_ratio_strategies and call_ratio_strategies[sid].get('strategy'):
            call_ratio_strategies[sid]['strategy'].running = False
            call_ratio_strategies[sid]['strategy'].close_all()
            closed = True
        if not closed and sid in active_monitors:
            active_monitors[sid]['monitor'].stop()
            closed = True
        if not closed:
            rs = registry.get(sid)
            if rs:
                rs.close()
                closed = True
        # Fallback — close by reversing legs
        if not closed and api_key:
            from api.orders import place_order
            placed_legs = t.get('details', {}).get('legs', [])
            if isinstance(placed_legs, list):
                for leg in placed_legs:
                    close_side = 'buy' if leg['side'] == 'sell' else 'sell'
                    place_order(leg['product_id'], leg['symbol'], int(leg['size']), close_side)

        update_tracked(sid, status='closed')
        closed_count += 1

    return jsonify(closed=closed_count)


@app.route('/api/strategy-detail/<sid>')
@login_required
def api_strategy_detail(sid):
    uid = current_user_id()
    from api.live_pnl import compute_live_legs

    # Check in-memory tracked strategies
    with _state_lock:
        t = all_tracked.get(sid)
        if t:
            t = dict(t)
    if t and t['user_id'] == uid:
        entry = dict(t)
        asset = entry.get('details', {}).get('asset', 'BTC')
        live_legs = []
        logs = []

        if sid in strategies and strategies[sid].get('strategy'):
            strat = strategies[sid]['strategy']
            entry['pnl'] = round(strat.total_pnl, 2)
            entry['realized_pnl'] = round(getattr(strat, 'realized_pnl', 0), 2)
            entry['unrealized_pnl'] = round(getattr(strat, 'unrealized_pnl', 0), 2)
            entry['adjustment_count'] = getattr(strat, 'adjustment_count', 0)
            entry['adjustment_history'] = getattr(strat, 'adjustment_history', [])
            entry['running'] = strategies[sid].get('running', False)
            logs = strategies[sid].get('log_history', [])
            for leg_name in ['call', 'put']:
                info = _leg_info(strat, leg_name)
                if info:
                    live_legs.append({
                        'symbol': info['symbol'], 'type': leg_name, 'strike': info['strike'],
                        'side': 'sell', 'size': info['size'], 'product_id': None,
                        'entry_price': info['entry'], 'current_mark': info['mark'],
                        'current_pnl': info['payoff'], 'delta': info['delta'],
                    })
        elif sid in iv_crush_strategies and iv_crush_strategies[sid].get('strategy'):
            ic = iv_crush_strategies[sid]
            strat = ic['strategy']
            entry['pnl'] = round(strat.total_pnl, 2)
            entry['realized_pnl'] = round(getattr(strat, 'realized_pnl', 0), 2)
            entry['unrealized_pnl'] = round(getattr(strat, 'unrealized_pnl', 0), 2)
            entry['running'] = ic.get('running', False)
            logs = ic.get('log_history', [])
            live_legs = _iv_crush_legs(strat)
            # Enrich with live marks
            for leg in live_legs:
                ws = strat.ws_manager.get_latest_price(leg['symbol'])
                mark = ws['mark_price'] if ws else leg['entry_price']
                d = -1  # short
                leg['current_mark'] = round(mark, 2)
                leg['current_pnl'] = round(d * (mark - leg['entry_price']) * leg['size'] * leg.get('contract_value', 0.001), 2)
        elif sid in call_ratio_strategies and call_ratio_strategies[sid].get('strategy'):
            cr = call_ratio_strategies[sid]
            strat = cr['strategy']
            entry['pnl'] = round(strat.total_pnl, 2)
            entry['running'] = cr.get('running', False)
            logs = cr.get('log_history', [])
            for leg in strat.legs:
                live_legs.append({
                    'product_id': leg.get('product_id'), 'symbol': leg['symbol'],
                    'type': 'call', 'strike': leg.get('strike', ''),
                    'side': leg['side'], 'size': leg['size'],
                    'entry_price': round(leg['entry_price'], 2),
                    'current_mark': round(leg.get('current_mark', leg['entry_price']), 2),
                    'current_pnl': leg.get('current_pnl', 0),
                })
        else:
            # Option Chain / Strategy Builder / Tracker — use common P&L calculator
            raw_legs = []
            if sid in active_monitors:
                mon = active_monitors[sid]['monitor']
                raw_legs = mon.legs
                logs = mon.get_status().get('logs', [])
                entry['running'] = mon.running
                if not mon.running:
                    entry['status'] = 'completed'
            rs = registry.get(sid)
            if rs:
                raw_legs = rs.legs
                logs = rs.get_logs(200)
                entry['running'] = rs.running
            if not raw_legs:
                raw_legs = entry.get('details', {}).get('legs', [])

            if raw_legs:
                live_legs, total_pnl = compute_live_legs(raw_legs, asset)
                entry['pnl'] = total_pnl

        entry['legs'] = live_legs
        entry['logs'] = logs
        # Include pnl_history from in-memory or DB snapshots
        pnl_history = []
        if sid in active_monitors:
            pnl_history = active_monitors[sid]['monitor'].pnl_history[-500:]
        rs = registry.get(sid)
        if rs and rs._pnl_history:
            pnl_history = rs._pnl_history[-500:]
        if not pnl_history:
            pnl_history = [(s['ts'], s['pnl']) for s in get_pnl_snapshots(uid, sid=sid)]
        entry['pnl_history'] = pnl_history
        return jsonify(**entry)
    # Try peer instance
    if PEER_PORT:
        try:
            import requests as req
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            r = req.get(f'http://127.0.0.1:{PEER_PORT}/api/strategy-detail/{sid}',
                        headers={'Authorization': f'Bearer {token}'}, timeout=3)
            if r.ok:
                return jsonify(r.json())
        except Exception:
            pass
    # Fallback: trade_history.json
    for h in get_history():
        if h.get('sid') == sid:
            return jsonify(
                sid=sid, source='Trade History', name=h.get('params', {}).get('asset', 'BTC') + ' ' + h.get('params', {}).get('expiry_date', ''),
                status=h.get('status', 'completed'), pnl=h.get('pnl', 0),
                started_at=h.get('started_at', ''), details=h.get('params', {}),
                adjustments=h.get('adjustments', 0), ended_at=h.get('ended_at', ''),
                user_id=uid,
            )
    return jsonify(error='Not found'), 404


def _validate_strategy_params(params):
    """Validate and clamp strategy parameters. Returns (cleaned_params, error_msg)."""
    try:
        p = {
            'asset': str(params.get('asset', 'BTC')),
            'expiry_date': str(params.get('expiry_date', '')),
            'target_delta': max(0.01, min(0.50, float(params.get('target_delta', 0.20)))),
            'delta_tolerance': max(0.01, min(0.20, float(params.get('delta_tolerance', 0.05)))),
            'lot_size': max(1, min(10000, int(params.get('lot_size', 100)))),
            'premium_threshold': max(5, min(200, float(params.get('premium_threshold', 40)))),
            'target_pnl': max(1, min(100000, float(params.get('target_pnl', 25)))),
            'max_adjustments': max(0, min(50, int(params.get('max_adjustments', 5)))),
            'monitoring_interval': max(2, min(300, int(params.get('monitoring_interval', 5)))),
        }
        if not p['expiry_date']:
            return None, "expiry_date is required"
        if p['asset'] not in ('BTC', 'ETH'):
            return None, f"Unsupported asset: {p['asset']}"
        return p, None
    except (ValueError, TypeError) as e:
        return None, f"Invalid parameter: {e}"


@app.route('/start', methods=['POST'])
@app.route('/api/start', methods=['POST'])
@login_required
@credits_required('deploy_live')
def start():
    params = request.json
    profile_id = params.pop('profile_id', None)

    # Validate credentials from profile or default
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected or keys not configured."), 400

    # Validate strategy parameters
    clean_params, err = _validate_strategy_params(params)
    if err:
        return jsonify(error=err), 400

    sid = params.pop('sid', '') or str(uuid.uuid4())[:8]

    with _state_lock:
        if sid in strategies and strategies[sid]['running']:
            return jsonify(error="Strategy already running"), 400

    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(), 'log_history': [], 'running': False, 'params': clean_params, 'user_id': current_user_id(), 'profile_id': profile_id}
    with _state_lock:
        strategies[sid] = entry
    record_start(sid, clean_params, user_id=current_user_id())
    track_strategy(sid, 'AlgoX DN', f"{clean_params.get('asset','BTC')} {clean_params.get('expiry_date','')}", current_user_id(), details={**clean_params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_strategy, args=(sid, clean_params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/stop', methods=['POST'])
@app.route('/api/stop', methods=['POST'])
@login_required
def stop():
    sid = request.json.get('sid')
    e = strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(error="Not found"), 404
    if not e['running'] or not e.get('strategy'):
        return jsonify(error="No strategy running"), 400
    e['strategy'].running = False
    e['strategy'].close_all_positions()
    return jsonify(status="stopping")


@app.route('/stream/<sid>')
@app.route('/api/stream/<sid>')
@login_required
def stream(sid):
    e = strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']

    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__":
                    yield f"event: stopped\ndata: done\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/status/<sid>')
@app.route('/api/status/<sid>')
@login_required
def status(sid):
    e = strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        # Try peer
        if PEER_PORT:
            try:
                import requests as req
                token = request.headers.get('Authorization', '').replace('Bearer ', '')
                r = req.get(f'http://127.0.0.1:{PEER_PORT}/api/status/{sid}',
                            headers={'Authorization': f'Bearer {token}'}, timeout=3)
                if r.ok: return jsonify(r.json())
            except Exception: pass
        return jsonify(running=False)
    if not e['running'] or not e.get('strategy'):
        return jsonify(running=False)
    s = e['strategy']
    return jsonify(
        running=True,
        adjustment_count=s.adjustment_count,
        adjustment_history=s.adjustment_history,
        total_pnl=round(s.total_pnl, 2),
        realized_pnl=round(s.realized_pnl, 2),
        unrealized_pnl=round(s.unrealized_pnl, 2),
        call=_leg_info(s, 'call'),
        put=_leg_info(s, 'put'),
    )


def _leg_info(s, leg):
    pos = getattr(s, f'{leg}_position')
    if not pos:
        return None
    cv = getattr(s, f'{leg}_contract_value')

    # Try WebSocket first, then REST API for live price
    ws_data = s.ws_manager.get_latest_price(pos['symbol'])
    mark = None
    delta = 0
    if ws_data:
        mark = ws_data['mark_price']
        delta = ws_data.get('delta', 0)
    if not mark:
        try:
            from api.pricing import get_current_price
            rest_data = get_current_price(pos['product_id'], getattr(s, 'asset', 'BTC'))
            if rest_data:
                mark = rest_data.get('mark_price', 0)
                delta = rest_data.get('delta', 0)
        except Exception:
            pass
    if not mark:
        mark = getattr(s, f'{leg}_actual_entry_price')

    from api import get_position_entry_price
    real_entry, real_size = get_position_entry_price(pos['product_id'])
    entry = real_entry if real_entry else getattr(s, f'{leg}_actual_entry_price')
    size = abs(real_size) if real_size else s.lot_size
    payoff = (entry - mark) * size * cv

    return dict(
        symbol=pos['symbol'],
        strike=pos.get('strike_price', ''),
        entry=round(entry, 2),
        mark=round(mark, 2),
        delta=round(delta, 4),
        size=size,
        payoff=round(payoff, 2),
    )


@app.route('/api/history')
@login_required
def api_history():
    uid = current_user_id()
    all_history = get_history()
    user_sids = {sid for sid, e in strategies.items() if e.get('user_id') == uid}
    user_sids.update(sid for sid, t in all_tracked.items() if t.get('user_id') == uid)
    user_history = [h for h in all_history if h.get('sid') in user_sids or h.get('user_id') == uid]
    return jsonify(user_history)


@app.route('/api/pnl-series')
@login_required
def api_pnl_series():
    """Return cumulative P&L series for the performance chart with date range filtering."""
    uid = current_user_id()
    since = request.args.get('since')  # ISO date string e.g. '2025-01-01'
    until = request.args.get('until')  # ISO date string

    # Build trades list same as dashboard
    all_history = get_history()
    user_sids = {sid for sid, e in strategies.items() if e.get('user_id') == uid}
    user_sids.update(sid for sid, t in all_tracked.items() if t.get('user_id') == uid)
    user_sids.update(s.sid for s in registry.get_user_strategies(uid))
    trades = [t for t in all_history if t.get('sid') in user_sids or t.get('user_id') == uid]
    trade_sids = {t.get('sid') for t in trades}
    for sid_t, t in all_tracked.items():
        if t.get('user_id') != uid or sid_t in trade_sids:
            continue
        trades.append({'sid': sid_t, 'status': t.get('status', 'running'),
                       'started_at': t.get('started_at', ''), 'ended_at': None,
                       'pnl': t.get('pnl', 0), 'params': t.get('details', {})})
    try:
        import json as _json
        conn = get_db()
        db_rows = conn.execute('SELECT * FROM live_strategies WHERE user_id=?', (uid,)).fetchall()
        conn.close()
        existing_sids = {t.get('sid') for t in trades}
        for r in db_rows:
            d = dict(r)
            if d['sid'] in existing_sids:
                continue
            trades.append({'sid': d['sid'], 'status': d['status'],
                           'started_at': d['started_at'], 'ended_at': None,
                           'pnl': d.get('pnl', 0) or 0, 'params': _json.loads(d.get('details') or '{}')})
    except Exception:
        pass

    completed = [t for t in trades if t.get('status') == 'completed']
    completed.sort(key=lambda t: t.get('ended_at') or t.get('started_at', ''))

    # Apply date filters
    if since:
        completed = [t for t in completed if (t.get('ended_at') or t.get('started_at', ''))[:10] >= since]
    if until:
        completed = [t for t in completed if (t.get('ended_at') or t.get('started_at', ''))[:10] <= until]

    pnl_by_date = {}
    cumulative = 0
    for t in completed:
        cumulative += t.get('pnl', 0)
        date_key = (t.get('ended_at') or t.get('started_at', ''))[:10]
        pnl_by_date[date_key] = round(cumulative, 2)
    series = [{'date': d, 'pnl': p} for d, p in pnl_by_date.items()]

    # Also include DB snapshots for running strategies (intraday granularity)
    snapshots = get_pnl_snapshots(uid, since=since)
    if until:
        snapshots = [s for s in snapshots if s['ts'][:10] <= until]

    return jsonify(pnl_series=series, snapshots=snapshots)


# ── IV Crush Strategy Routes ──

iv_crush_strategies = {}  # {sid: {thread, strategy, log_queue, log_history, running, params, user_id}}

def _iv_crush_legs(s):
    """Extract legs list from IVCrushStrategy's call/put positions."""
    legs = []
    for name, pos, entry_p, cv in [
        ('call', s.call_position, s.call_entry_price, s.call_contract_value),
        ('put', s.put_position, s.put_entry_price, s.put_contract_value),
    ]:
        if pos:
            legs.append({
                'product_id': pos.get('product_id'), 'symbol': pos.get('symbol', ''),
                'type': name, 'strike': pos.get('strike_price', ''), 'side': 'sell',
                'size': s.lot_size, 'entry_price': round(entry_p, 2),
                'contract_value': cv,
            })
    return legs


def run_iv_crush(sid, params):
    entry = iv_crush_strategies[sid]
    uid = entry['user_id']
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return

    try:
        from strategy.iv_crush import IVCrushStrategy
        s = IVCrushStrategy(
            asset=params.get('asset', 'BTC'),
            expiry_date=params['expiry_date'],
            lot_size=int(params.get('lot_size', 10)),
            iv_rv_threshold=float(params.get('iv_rv_threshold', 1.3)),
            max_loss_pct=float(params.get('max_loss_pct', 50)),
            target_profit_pct=float(params.get('target_profit_pct', 30)),
            monitoring_interval=int(params.get('monitoring_interval', 10)),
        )
        entry['strategy'] = s
        entry['running'] = True
        record_start(sid, params, user_id=uid)
        if not s.initialize():
            entry['log_queue'].put(f"✗ Init failed: {s.status_msg or 'unknown'}")
            entry['running'] = False
            entry['log_queue'].put("__STOPPED__")
            return

        # Save legs to DB and register positions
        legs = _iv_crush_legs(s)
        try:
            update_strategy_db(sid, legs=legs)
        except Exception:
            pass
        for leg in legs:
            position_tracker.open(uid, leg['product_id'], leg['symbol'],
                type=leg['type'], strike=leg.get('strike', ''), side='sell',
                size=leg['size'], entry_price=leg['entry_price'],
                asset=params.get('asset', 'BTC'), source='IV Crush')

        # Wrap monitor to inject PnL snapshots
        import strategy.iv_crush as _iv_mod
        _orig_sleep = _iv_mod.time.sleep
        _tick = [0]
        def _snap_sleep(secs):
            _orig_sleep(secs)
            _tick[0] += 1
            if _tick[0] % 6 == 0:
                try:
                    save_pnl_snapshot(uid, sid, round(s.total_pnl, 2))
                    update_strategy_db(sid, pnl=round(s.total_pnl, 2), legs=_iv_crush_legs(s))
                except Exception:
                    pass
        _iv_mod.time.sleep = _snap_sleep
        try:
            s.monitor()
        finally:
            _iv_mod.time.sleep = _orig_sleep
    except Exception as e:
        entry['log_queue'].put(f"❌ Error: {e}")
    finally:
        pnl = round(getattr(entry.get('strategy'), 'total_pnl', 0), 2)
        record_end(sid, pnl, 0)
        update_tracked(sid, status='completed', pnl=pnl)
        _teardown_strategy_thread(entry)


@app.route('/api/iv-crush/start', methods=['POST'])
@login_required
def iv_crush_start():
    params = request.json
    profile_id = params.pop('profile_id', None)
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    sid = str(uuid.uuid4())[:8]
    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(), 'log_history': [],
             'running': False, 'params': params, 'user_id': current_user_id(), 'profile_id': profile_id}
    iv_crush_strategies[sid] = entry
    track_strategy(sid, 'IV Crush', f"{params.get('asset','BTC')} IV Crush {params.get('expiry_date','')}", current_user_id(), details={**params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_iv_crush, args=(sid, params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/api/iv-crush/stop', methods=['POST'])
@login_required
def iv_crush_stop():
    sid = request.json.get('sid')
    e = iv_crush_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(error="Not found"), 404
    if e.get('strategy'):
        e['strategy'].running = False
        e['strategy'].close_all()
    return jsonify(status="stopping")


@app.route('/api/iv-crush/stream/<sid>')
@login_required
def iv_crush_stream(sid):
    e = iv_crush_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']
    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__":
                    yield f"event: stopped\ndata: done\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/iv-crush/status/<sid>')
@login_required
def iv_crush_status(sid):
    e = iv_crush_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(running=False)
    s = e.get('strategy')
    if not e['running'] or not s:
        return jsonify(running=False, status_msg=getattr(s, 'status_msg', '') if s else '')
    pnl_pct = (s.total_pnl / s.total_premium * 100) if s.total_premium > 0 else 0
    return jsonify(
        running=True,
        total_pnl=round(s.total_pnl, 2),
        unrealized_pnl=round(s.unrealized_pnl, 2),
        total_premium=round(s.total_premium, 2),
        pnl_pct=round(pnl_pct, 1),
        iv_at_entry=round(s.iv_at_entry, 4),
        current_iv=round(s.current_iv, 4),
        iv_crush_pct=s.iv_crush_pct,
        iv_rv_ratio=round(s.iv_rv_ratio, 2),
        call=_iv_leg(s, 'call'),
        put=_iv_leg(s, 'put'),
    )


def _iv_leg(s, leg):
    pos = getattr(s, f'{leg}_position')
    if not pos:
        return None
    entry = getattr(s, f'{leg}_entry_price')
    ws = s.ws_manager.get_latest_price(pos['symbol'])
    mark = ws['mark_price'] if ws else entry
    return dict(symbol=pos['symbol'], strike=pos.get('strike_price', ''),
                entry=round(entry, 2), mark=round(mark, 2))


# ── Call Ratio Spread Routes ──

call_ratio_strategies = {}

def run_call_ratio(sid, params):
    entry = call_ratio_strategies[sid]
    uid = entry['user_id']
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return

    try:
        from strategy.call_ratio import CallRatioStrategy
        s = CallRatioStrategy(
            asset=params.get('asset', 'BTC'), expiry_date=params.get('expiry_date', ''),
            lot_size=int(params.get('lot_size', 10)),
            buy_offset_pct=float(params.get('buy_offset_pct', 2)),
            sell_offset_pct=float(params.get('sell_offset_pct', 4)),
            hedge_offset_pct=float(params.get('hedge_offset_pct', 7)),
            target_pct=float(params.get('target_pct', 5)),
            sl_pct=float(params.get('sl_pct', 8)),
            monitoring_interval=int(params.get('monitoring_interval', 30)),
        )
        entry['strategy'] = s; entry['running'] = True
        record_start(sid, params, user_id=uid)
        if not s.initialize():
            entry['log_queue'].put(f"✗ Init failed: {s.status_msg or 'unknown'}")
            entry['running'] = False
            entry['log_queue'].put("__STOPPED__")
            return

        # Save legs to DB and register positions
        try:
            update_strategy_db(sid, legs=s.legs)
        except Exception:
            pass
        for leg in s.legs:
            position_tracker.open(uid, leg['product_id'], leg['symbol'],
                type='call', strike=leg.get('strike', ''), side=leg['side'],
                size=leg['size'], entry_price=leg['entry_price'],
                asset=params.get('asset', 'BTC'), source='Call Ratio')

        # Wrap monitor to inject PnL snapshots
        import strategy.call_ratio as _cr_mod
        _orig_sleep = _cr_mod.time.sleep
        _tick = [0]
        def _snap_sleep(secs):
            _orig_sleep(secs)
            _tick[0] += 1
            if _tick[0] % 6 == 0:
                try:
                    save_pnl_snapshot(uid, sid, round(s.total_pnl, 2))
                    update_strategy_db(sid, pnl=round(s.total_pnl, 2), legs=s.legs)
                except Exception:
                    pass
        _cr_mod.time.sleep = _snap_sleep
        try:
            s.monitor()
        finally:
            _cr_mod.time.sleep = _orig_sleep
    except Exception as e:
        entry['log_queue'].put(f"❌ Error: {e}")
    finally:
        pnl = round(getattr(entry.get('strategy'), 'total_pnl', 0), 2)
        record_end(sid, pnl, 0)
        update_tracked(sid, status='completed', pnl=pnl)
        _teardown_strategy_thread(entry)


@app.route('/api/call-ratio/start', methods=['POST'])
@login_required
def call_ratio_start():
    params = request.json; profile_id = params.pop('profile_id', None)
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key: return jsonify(error="No API profile selected"), 400
    sid = str(uuid.uuid4())[:8]
    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(), 'log_history': [], 'running': False, 'params': params, 'user_id': current_user_id(), 'profile_id': profile_id}
    call_ratio_strategies[sid] = entry
    track_strategy(sid, 'Call Ratio', f"{params.get('asset','BTC')} Call Ratio", current_user_id(), details={**params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_call_ratio, args=(sid, params), daemon=True); entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/api/call-ratio/stop', methods=['POST'])
@login_required
def call_ratio_stop():
    sid = request.json.get('sid'); e = call_ratio_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id(): return jsonify(error="Not found"), 404
    if e.get('strategy'): e['strategy'].running = False; e['strategy'].close_all()
    return jsonify(status="stopping")


@app.route('/api/call-ratio/stream/<sid>')
@login_required
def call_ratio_stream(sid):
    e = call_ratio_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id(): return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']
    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__": yield f"event: stopped\ndata: done\n\n"; break
                yield f"data: {msg}\n\n"
            except queue.Empty: yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/call-ratio/status/<sid>')
@login_required
def call_ratio_status(sid):
    e = call_ratio_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id(): return jsonify(running=False)
    s = e.get('strategy')
    if not e['running'] or not s: return jsonify(running=False, status_msg=getattr(s, 'status_msg', '') if s else '')
    return jsonify(running=True, total_pnl=s.total_pnl, pnl_pct=s.pnl_pct, deployed_margin=round(s.deployed_margin, 2),
                   legs=[{'symbol': l['symbol'], 'strike': l['strike'], 'side': l['side'], 'size': l['size'],
                          'entry': round(l['entry_price'], 2), 'mark': round(l.get('current_mark', l['entry_price']), 2),
                          'pnl': l.get('current_pnl', 0)} for l in s.legs])


# ── Option Chain Routes ──

DELTA_ASSETS = {'BTC', 'ETH'}

@app.route('/api/expiries')
@login_required
def api_expiries():
    asset = request.args.get('asset', 'BTC')
    if asset in DELTA_ASSETS:
        from api.chain import get_expiries
        from config import set_thread_credentials
        profile_id = request.args.get('profile_id')
        api_key, api_secret, _, broker = get_profile_creds(profile_id)
        if api_key:
            set_thread_credentials(api_key, api_secret, broker)
        elif not profile_id:
            # No profile and no default keys — still set broker based on what we have
            set_thread_credentials('', '', 'demo')
        return jsonify(expiries=get_expiries(asset))
    from api.nse import get_nse_expiries
    return jsonify(expiries=get_nse_expiries(asset))


@app.route('/api/chain')
@login_required
def api_chain():
    asset = request.args.get('asset', 'BTC')
    expiry = request.args.get('expiry', '')
    if not expiry:
        return jsonify(error="expiry required"), 400
    if asset in DELTA_ASSETS:
        from api.chain import get_option_chain_full
        from config import set_thread_credentials
        profile_id = request.args.get('profile_id')
        api_key, api_secret, _, broker = get_profile_creds(profile_id)
        if api_key:
            set_thread_credentials(api_key, api_secret, broker)
        elif not profile_id:
            set_thread_credentials('', '', 'demo')
        chain, spot, exp = get_option_chain_full(expiry, asset)
    else:
        from api.nse import get_nse_chain
        try:
            chain, spot, exp = get_nse_chain(asset, expiry)
        except Exception as e:
            logger.error(f"NSE chain error: {e}")
            return jsonify(error=str(e)), 500
    if chain is None:
        return jsonify(error="No data for this expiry"), 500
    return jsonify(chain=chain, spot_price=spot, expiry=exp)


@app.route('/api/place-legs', methods=['POST'])
@login_required
@credits_required('place_legs')
def api_place_legs():
    """Place multiple option legs and optionally start monitoring."""
    from api.orders import place_order
    from config import set_thread_credentials
    data = request.json
    api_key, api_secret, pname, broker = get_profile_creds(data.get('profile_id'))
    if not api_key:
        return jsonify(error="No API profile selected or keys not configured"), 400
    set_thread_credentials(api_key, api_secret, broker)

    legs = data.get('legs', [])
    max_profit = float(data.get('max_profit', 0))
    max_loss = float(data.get('max_loss', 0))

    results = []
    placed_legs = []
    for leg in legs:
        result = place_order(leg['product_id'], leg['symbol'], int(leg['size']), leg['side'])
        ok = result is not None
        results.append({'symbol': leg['symbol'], 'side': leg['side'], 'size': leg['size'], 'success': ok})
        if ok:
            placed_legs.append({
                'product_id': leg['product_id'], 'symbol': leg['symbol'],
                'type': leg.get('type', ''), 'strike': leg.get('strike', ''),
                'side': leg['side'], 'size': int(leg['size']),
                'entry_price': float(leg.get('mark', 0)),
            })
            position_tracker.open(current_user_id(), leg['product_id'], leg['symbol'],
                type=leg.get('type', ''), strike=leg.get('strike', ''),
                side=leg['side'], size=int(leg['size']),
                entry_price=float(leg.get('mark', 0)),
                asset=data.get('asset', 'BTC'), source='Option Chain')

    # Always track the strategy
    asset = data.get('asset', 'BTC')
    sid = str(uuid.uuid4())[:8]
    if placed_legs:
        leg_names = ', '.join(l['symbol'] for l in placed_legs[:3])
        track_strategy(sid, 'Option Chain', f"{asset} {leg_names}", current_user_id(),
                       details={'legs': placed_legs, 'max_profit': max_profit, 'max_loss': max_loss, 'asset': asset, 'profile_id': data.get('profile_id')})
        record_start(sid, {
            'asset': asset, 'source': 'Option Chain',
            'legs': len(placed_legs), 'max_profit': max_profit, 'max_loss': max_loss,
            'expiry_date': data.get('expiry', ''),
            'lot_size': placed_legs[0]['size'] if placed_legs else 0,
            'leg_details': ', '.join(f"{l['side'].upper()} {l.get('type','')} {l.get('strike','')}" for l in placed_legs[:4]),
        }, user_id=current_user_id())

    # Start monitor if targets are set and all orders succeeded
    monitor_id = None
    if max_profit > 0 and max_loss > 0 and placed_legs and all(r['success'] for r in results):
        from strategy.monitor import StrategyMonitor
        from config import get_contract_value
        mon = StrategyMonitor(
            legs=placed_legs, max_profit=max_profit, max_loss=max_loss,
            asset=asset, lot_size=get_contract_value(asset),
        )
        mon.user_id = current_user_id()
        mon.sid = sid
        mon.profile_id = data.get('profile_id')
        monitor_id = sid
        active_monitors[monitor_id] = {'monitor': mon, 'user_id': current_user_id(), 'profile_id': data.get('profile_id')}
        mon.on_complete = lambda pnl, reason: (update_tracked(sid, status='completed', pnl=round(pnl, 2)), record_end(sid, pnl, 0))
        mon.start()
    elif placed_legs and not (max_profit > 0 and max_loss > 0):
        # No monitor — mark as completed immediately (manual trade)
        update_tracked(sid, status='open (no monitor)')

    return jsonify(results=results, monitor_id=monitor_id)


@app.route('/api/positions')
@login_required
def api_positions():
    """Return open option positions with live mark prices."""
    import re
    from api.positions import get_positions
    from config import set_thread_credentials
    api_key, api_secret, _, broker = get_profile_creds(request.args.get('profile_id'))
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    set_thread_credentials(api_key, api_secret, broker)

    positions = get_positions()

    # Fetch live tickers for mark prices
    mark_prices = {}
    try:
        import requests as req
        import config as cfg
        from auth import get_headers
        path = '/v2/tickers'
        qs = '?contract_types=call_options,put_options'
        headers = get_headers('GET', path, qs)
        resp = req.get(f'{cfg.BASE_URL}{path}{qs}', headers=headers, timeout=10)
        if resp.ok:
            for t in resp.json().get('result', []):
                mark_prices[t.get('product_id')] = float(t.get('mark_price', 0))
    except Exception:
        pass

    result = []
    for p in positions:
        size = int(p.get('size', 0))
        if size == 0:
            continue
        sym = p.get('product_symbol', '')
        m = re.match(r'^(C|P)-(\w+)-(\d+)-\d+$', sym)
        opt_type = 'call' if (m and m.group(1) == 'C') else 'put' if m else 'unknown'
        strike = m.group(3) if m else '0'
        side = 'sell' if size < 0 else 'buy'
        pid = p.get('product_id')
        entry = float(p.get('entry_price', 0))
        mark = mark_prices.get(pid, entry)
        # contract_value: BTC options = 0.001, ETH options = 0.01
        asset = m.group(2) if m else 'BTC'
        from config import get_contract_value
        cv = get_contract_value(asset)
        direction = 1 if side == 'buy' else -1
        pnl = direction * (mark - entry) * abs(size) * cv
        result.append({
            'symbol': sym, 'product_id': pid, 'type': opt_type,
            'strike': strike, 'side': side, 'size': abs(size),
            'entry_price': entry, 'mark_price': mark,
            'pnl': round(pnl, 2), 'asset': asset,
        })
    return jsonify(positions=result)



@app.route('/api/close-position', methods=['POST'])
@login_required
def api_close_position():
    """Close a single position leg."""
    from api.orders import place_order
    from config import set_thread_credentials
    data = request.json or {}
    for field in ('side', 'product_id', 'symbol', 'size'):
        if field not in data:
            return jsonify(error=f"Missing required field: {field}"), 400
    try:
        size = int(data['size'])
    except (ValueError, TypeError):
        return jsonify(error="size must be an integer"), 400
    if data['side'] not in ('buy', 'sell'):
        return jsonify(error="side must be 'buy' or 'sell'"), 400
    api_key, api_secret, _, broker = get_profile_creds(data.get('profile_id'))
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    set_thread_credentials(api_key, api_secret, broker)
    close_side = 'buy' if data['side'] == 'sell' else 'sell'
    result = place_order(data['product_id'], data['symbol'], size, close_side)
    if result is not None:
        position_tracker.close(current_user_id(), data['product_id'])
        # Also try closing on peer
        if PEER_PORT:
            try:
                import requests as req
                token = request.headers.get('Authorization', '').replace('Bearer ', '')
                req.post(f'http://127.0.0.1:{PEER_PORT}/api/close-position',
                         json=data, headers={'Authorization': f'Bearer {token}'}, timeout=5)
            except Exception:
                pass
    return jsonify(success=result is not None)

@app.route('/api/monitor/<mid>')
@login_required
def api_monitor_status(mid):
    entry = active_monitors.get(mid)
    if entry and entry['user_id'] == current_user_id():
        return jsonify(**entry['monitor'].get_status())
    if PEER_PORT:
        try:
            import requests as req
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            r = req.get(f'http://127.0.0.1:{PEER_PORT}/api/monitor/{mid}',
                        headers={'Authorization': f'Bearer {token}'}, timeout=3)
            if r.ok: return jsonify(r.json())
        except Exception: pass
    return jsonify(error="Not found"), 404


@app.route('/api/monitor/<mid>/stop', methods=['POST'])
@login_required
def api_monitor_stop(mid):
    entry = active_monitors.get(mid)
    if not entry or entry['user_id'] != current_user_id():
        if PEER_PORT:
            try:
                import requests as req
                token = request.headers.get('Authorization', '').replace('Bearer ', '')
                r = req.post(f'http://127.0.0.1:{PEER_PORT}/api/monitor/{mid}/stop',
                             headers={'Authorization': f'Bearer {token}'}, timeout=10)
                if r.ok: return jsonify(r.json())
            except Exception: pass
        return jsonify(error="Not found"), 404
    from config import set_thread_credentials
    api_key, api_secret, _, broker = get_profile_creds(entry.get('profile_id'))
    if api_key:
        set_thread_credentials(api_key, api_secret, broker)
    entry['monitor'].stop()
    return jsonify(status="stopped")


# ── Chart Routes ──

@app.route('/api/chart-data')
@login_required
def api_chart_data():
    from api.chart import get_candles, detect_structure, calc_indicators
    symbol = request.args.get('symbol', 'NIFTY')
    interval = request.args.get('interval', '1h')
    indicators = request.args.get('indicators', '').split(',') if request.args.get('indicators') else []
    candles = get_candles(symbol, interval)
    if not candles:
        return jsonify(error='Failed to fetch data'), 500
    structure = detect_structure(candles)
    ind_data = calc_indicators(candles, indicators) if indicators else {}
    return jsonify(candles=candles, indicators=ind_data, **structure)


# ── Strategy Builder Routes ──

@app.route('/api/strategy-builder/save', methods=['POST'])
@login_required
def api_save_strategy_builder():
    data = request.json
    data['user_id'] = current_user_id()
    sid = str(uuid.uuid4())[:8]
    saved = getattr(app, '_saved_strategies', {})
    saved[sid] = data
    app._saved_strategies = saved
    return jsonify(status='saved', sid=sid)


@app.route('/api/strategy-builder/deploy', methods=['POST'])
@login_required
@credits_required('deploy_builder')
def api_deploy_strategy_builder():
    from api.chain import get_option_chain_full, get_expiries
    from api.orders import place_order
    from config import set_thread_credentials
    from strategy.monitor import StrategyMonitor

    data = request.json
    api_key, api_secret, pname, broker = get_profile_creds(data.get('profile_id'))
    if not api_key:
        return jsonify(error="No API profile selected or keys not configured"), 400
    set_thread_credentials(api_key, api_secret, broker)

    asset = data.get('underlying', 'BTC')
    legs_cfg = data.get('legs', [])
    if not legs_cfg:
        return jsonify(error="No legs defined"), 400

    # Resolve expiry
    expiry_key = data.get('expiry', 'current_week')
    expiries = get_expiries(asset)
    if not expiries:
        return jsonify(error="Could not fetch expiries"), 500
    expiry_map = {'current_week': 0, 'next_week': 1, 'current_month': 0, 'next_month': 1}
    expiry = expiries[min(expiry_map.get(expiry_key, 0), len(expiries) - 1)] if expiry_key != 'custom' else expiries[0]

    # Fetch chain
    chain, spot, _ = get_option_chain_full(expiry, asset)
    if not chain or not spot:
        return jsonify(error="Failed to fetch option chain"), 500

    # Build sorted strike list and find ATM index
    strikes = [float(row['strike']) for row in chain]
    atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))

    # Resolve each leg to a real option
    import re
    results = []
    placed_legs = []
    lots_per_leg = int(data.get('execution', {}).get('lots', 1))
    for leg in legs_cfg:
        opt_type = 'call' if leg['type'] == 'CE' else 'put'
        strike_key = leg.get('strike', 'ATM')
        m = re.match(r'(ATM|OTM|ITM)(\d*)', strike_key)
        offset = 0
        if m:
            offset = int(m.group(2)) if m.group(2) else 0
            if m.group(1) == 'OTM':
                offset = offset if opt_type == 'call' else -offset
            elif m.group(1) == 'ITM':
                offset = -offset if opt_type == 'call' else offset
        idx = max(0, min(atm_idx + offset, len(chain) - 1))
        opt = chain[idx].get(opt_type)
        if not opt or not opt.get('product_id'):
            results.append({'strike': strike_key, 'type': leg['type'], 'success': False, 'error': 'No option found'})
            continue

        size = int(leg.get('lots', 1)) * lots_per_leg
        order = place_order(opt['product_id'], opt['symbol'], size, leg['side'])
        ok = order is not None
        results.append({'symbol': opt['symbol'], 'side': leg['side'], 'size': size, 'success': ok})
        if ok:
            placed_legs.append({
                'product_id': opt['product_id'], 'symbol': opt['symbol'],
                'type': leg['type'], 'strike': opt['strike'],
                'side': leg['side'], 'size': size,
                'entry_price': float(opt.get('mark_price', 0)),
            })
            position_tracker.open(current_user_id(), opt['product_id'], opt['symbol'],
                type=leg['type'], strike=opt['strike'],
                side=leg['side'], size=size,
                entry_price=float(opt.get('mark_price', 0)),
                asset=asset, source='Strategy Builder')

    if not placed_legs:
        return jsonify(error="All orders failed", results=results), 500

    sid = str(uuid.uuid4())[:8]
    data['legs'] = placed_legs  # overwrite abstract config with actual placed legs (with product_id)
    record_start(sid, {
        'asset': asset, 'source': 'Strategy Builder',
        'name': data.get('name', 'Unnamed'), 'legs': len(placed_legs),
        'expiry_date': expiry,
        'lot_size': lots_per_leg,
        'leg_details': ', '.join(f"{l['side'].upper()} {l.get('type','')} {l.get('strike','')}" for l in placed_legs[:4]),
    }, user_id=current_user_id())

    # Start monitor if risk targets are set
    risk = data.get('risk', {})
    sl_pct = float(risk.get('sl_pct', 0))
    tgt_pct = float(risk.get('target_pct', 0))
    total_premium = sum(l['entry_price'] * l['size'] for l in placed_legs if l['side'] == 'sell')
    lot_sizes = {'BTC': 0.001, 'ETH': 0.01}
    lot_size = lot_sizes.get(asset, 0.001)
    max_profit = total_premium * lot_size * tgt_pct / 100 if tgt_pct else 0
    max_loss = total_premium * lot_size * sl_pct / 100 if sl_pct else 0

    data['max_profit'] = max_profit
    data['max_loss'] = max_loss
    data['asset'] = asset
    data['lot_size'] = lot_size
    track_strategy(sid, 'Strategy Builder', data.get('name', 'Unnamed'), current_user_id(), details=data)

    monitor_id = None
    if max_profit > 0 and max_loss > 0:
        mon = StrategyMonitor(
            legs=placed_legs, max_profit=max_profit, max_loss=max_loss,
            asset=asset, lot_size=lot_size,
        )
        mon.user_id = current_user_id()
        mon.sid = sid
        mon.profile_id = data.get('profile_id')
        monitor_id = sid
        active_monitors[monitor_id] = {'monitor': mon, 'user_id': current_user_id(), 'profile_id': data.get('profile_id')}
        mon.on_complete = lambda pnl, reason: (update_tracked(sid, status='completed', pnl=round(pnl, 2)), record_end(sid, pnl, 0))
        mon.start()
        update_tracked(sid, status='running')
    else:
        update_tracked(sid, status='open (no monitor)')

    return jsonify(status='deployed', sid=sid, results=results, monitor_id=monitor_id)


@app.route('/api/strategy-builder/paper-trade', methods=['POST'])
@login_required
@credits_required('paper_trade')
def api_paper_trade_strategy_builder():
    data = request.json
    sid = str(uuid.uuid4())[:8]
    track_strategy(sid, 'Strategy Builder (Paper)', data.get('name', 'Unnamed'), current_user_id(), details=data)
    update_tracked(sid, status='paper')
    return jsonify(status='paper', sid=sid)


# ── Credits API ──

@app.route('/api/credits')
@login_required
def api_credits():
    creds = get_user_credits(current_user_id())
    return jsonify(creds or {})


@app.route('/api/credits/history')
@login_required
def api_credits_history():
    return jsonify(history=get_credit_history(current_user_id()))


@app.route('/api/credits/costs')
@login_required
def api_credit_costs():
    return jsonify(costs=CREDIT_COSTS)


# ── Admin Routes ──

@app.route('/api/admin/users')
@login_required
@admin_required
def api_admin_users():
    return jsonify(users=get_all_users())


@app.route('/api/admin/stats')
@login_required
@admin_required
def api_admin_stats():
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    creds = conn.execute('SELECT COALESCE(SUM(credits_remaining),0), COALESCE(SUM(credits_used),0) FROM user_credits').fetchone()
    plans_count = conn.execute('SELECT COUNT(*) FROM plans').fetchone()[0]
    conn.close()
    return jsonify(total_users=total, credits_available=creds[0], credits_used=creds[1], plans_count=plans_count)


@app.route('/api/admin/plans')
@login_required
@admin_required
def api_admin_plans():
    return jsonify(plans=get_all_plans())


@app.route('/api/admin/add-credits', methods=['POST'])
@login_required
@admin_required
def api_admin_add_credits():
    d = request.json
    uid = d.get('user_id')
    amount = int(d.get('amount', 0))
    desc = d.get('description', 'Admin grant')
    if not uid or amount == 0:
        return jsonify(error='user_id and amount required'), 400
    add_credits(uid, amount, desc)
    return jsonify(status='ok')


@app.route('/api/admin/set-plan', methods=['POST'])
@login_required
@admin_required
def api_admin_set_plan():
    d = request.json
    if not set_user_plan(d.get('user_id'), d.get('plan_id')):
        return jsonify(error='Invalid plan'), 400
    return jsonify(status='ok')


@app.route('/api/admin/set-admin', methods=['POST'])
@login_required
@admin_required
def api_admin_set_admin():
    d = request.json
    set_admin(d.get('user_id'), d.get('is_admin', False))
    return jsonify(status='ok')


@app.route('/api/admin/user-history/<int:uid>')
@login_required
@admin_required
def api_admin_user_history(uid):
    return jsonify(history=get_credit_history(uid, 100))


# ── Unified Strategy Tracker API ──

@app.route('/api/tracked-positions')
@login_required
def api_tracked_positions():
    """Return all positions — from broker + position tracker, deduplicated."""
    from api.live_pnl import compute_live_legs
    uid = current_user_id()

    # 1. Get positions from position tracker (Option Chain, Strategy Builder)
    tracked = position_tracker.to_list(uid, refresh=True)

    # 2. Get positions from broker API if profile provided
    broker_positions = []
    profile_id = request.args.get('profile_id', '')
    if profile_id:
        import re
        from api.positions import get_positions
        from config import set_thread_credentials
        api_key, api_secret, _, broker = get_profile_creds(profile_id)
        if api_key:
            set_thread_credentials(api_key, api_secret, broker)
            positions = get_positions()
            for p in positions:
                size = int(p.get('size', 0))
                if size == 0:
                    continue
                sym = p.get('product_symbol', '')
                m = re.match(r'^(C|P)-(\w+)-(\d+)-\d+$', sym)
                opt_type = 'call' if (m and m.group(1) == 'C') else 'put' if m else 'unknown'
                strike = m.group(3) if m else '0'
                side = 'sell' if size < 0 else 'buy'
                pid = p.get('product_id')
                entry = float(p.get('entry_price', 0))
                asset = m.group(2) if m else 'BTC'
                broker_positions.append({
                    'product_id': pid, 'symbol': sym, 'type': opt_type,
                    'strike': strike, 'side': side, 'size': abs(size),
                    'entry_price': entry, 'asset': asset, 'source': 'Broker',
                })

    # 3. Merge: broker positions + tracked (dedup by product_id)
    seen = set()
    merged = []
    for p in tracked:
        if p.get('product_id'):
            seen.add(p['product_id'])
        merged.append(p)
    for p in broker_positions:
        if p.get('product_id') not in seen:
            merged.append(p)

    # 3b. Merge positions from peer (old) instance
    if PEER_PORT:
        try:
            import requests as req
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            r = req.get(f'http://127.0.0.1:{PEER_PORT}/api/tracked-positions',
                        headers={'Authorization': f'Bearer {token}'}, params={'profile_id': profile_id}, timeout=3)
            if r.ok:
                for p in r.json().get('positions', []):
                    pid = p.get('product_id')
                    if pid and pid not in seen:
                        seen.add(pid)
                        p['_peer'] = True
                        merged.append(p)
        except Exception:
            pass

    # 4. Compute live prices for all
    if merged:
        from api.pricing import get_current_price
        for p in merged:
            if p.get('current_mark') and p.get('current_pnl') is not None:
                continue  # already has live data from tracker
            pid = p.get('product_id')
            asset = p.get('asset', 'BTC')
            if pid:
                try:
                    data = get_current_price(pid, asset)
                    if data and data.get('mark_price'):
                        mark = float(data['mark_price'])
                        entry = float(p.get('entry_price', 0))
                        lot_size = 0.01 if asset == 'ETH' else 0.001
                        d = 1 if p.get('side') == 'buy' else -1
                        p['current_mark'] = round(mark, 2)
                        p['mark_price'] = round(mark, 2)
                        p['current_pnl'] = round(d * (mark - entry) * int(p.get('size', 0)) * lot_size, 2)
                        p['pnl'] = p['current_pnl']
                except Exception:
                    pass

    total_pnl = sum(p.get('current_pnl') or p.get('pnl') or 0 for p in merged)
    return jsonify(positions=merged, total_pnl=round(total_pnl, 2))

@app.route('/api/tracker/strategies')
@login_required
def api_tracker_list():
    return jsonify(strategies=registry.all_statuses(current_user_id()))

@app.route('/api/tracker/<sid>')
@login_required
def api_tracker_detail(sid):
    s = registry.get(sid)
    if s and s.user_id == current_user_id():
        return jsonify(**s.get_status())
    # Fallback: old strategies dict
    e = strategies.get(sid)
    if e and e.get('user_id') == current_user_id():
        strat = e.get('strategy')
        return jsonify(sid=sid, source='AlgoX DN', name=e.get('params', {}).get('asset', 'BTC'),
            user_id=e['user_id'], status='running' if e.get('running') else 'completed',
            running=e.get('running', False), pnl=round(strat.total_pnl, 2) if strat else 0,
            legs=[], logs=[], details=e.get('params', {}))
    return jsonify(error='Not found'), 404

@app.route('/api/tracker/<sid>/logs')
@login_required
def api_tracker_logs(sid):
    last = int(request.args.get('last', 100))
    # Check unified tracker first
    s = registry.get(sid)
    if s and s.user_id == current_user_id():
        return jsonify(sid=sid, logs=s.get_logs(last), running=s.running, pnl=s.current_pnl, status=s.status)
    # Fallback: check old strategies dict (Delta Neutral strategies)
    e = strategies.get(sid)
    if e and e.get('user_id') == current_user_id():
        logs = list(e.get('log_history', []))
        strat = e.get('strategy')
        pnl = round(strat.total_pnl, 2) if strat else 0
        return jsonify(sid=sid, logs=logs[-last:], running=e.get('running', False), pnl=pnl, status='running' if e.get('running') else 'completed')
    # Check active monitors (Option Chain / Strategy Builder)
    m = active_monitors.get(sid)
    if m and m.get('user_id') == current_user_id():
        mon = m['monitor']
        st = mon.get_status()
        return jsonify(sid=sid, logs=st.get('logs', [])[-last:], running=mon.running, pnl=round(mon.current_pnl, 2), status='running' if mon.running else 'completed')
    # Proxy to peer
    if PEER_PORT:
        try:
            import requests as req
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            r = req.get(f'http://127.0.0.1:{PEER_PORT}/api/tracker/{sid}/logs',
                        headers={'Authorization': f'Bearer {token}'}, params={'last': last}, timeout=3)
            if r.ok:
                return jsonify(r.json())
        except Exception:
            pass
    return jsonify(error='Not found'), 404

@app.route('/api/tracker/<sid>/close', methods=['POST'])
@login_required
def api_tracker_close(sid):
    s = registry.get(sid)
    if s and s.user_id == current_user_id():
        s.close()
        return jsonify(status='closed', pnl=s.current_pnl)
    # Fallback: old strategies dict
    e = strategies.get(sid)
    if e and e.get('user_id') == current_user_id() and e.get('strategy'):
        e['strategy'].running = False
        e['strategy'].close_all_positions()
        return jsonify(status='closed')
    # Proxy to peer
    if PEER_PORT:
        try:
            import requests as req
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            r = req.post(f'http://127.0.0.1:{PEER_PORT}/api/tracker/{sid}/close',
                         headers={'Authorization': f'Bearer {token}'}, timeout=10)
            if r.ok:
                return jsonify(r.json())
        except Exception:
            pass
    return jsonify(error='Not found'), 404

@app.route('/api/tracker/close-all', methods=['POST'])
@login_required
def api_tracker_close_all():
    count = registry.close_all(current_user_id())
    return jsonify(closed=count)

@app.route('/api/tracker/deploy', methods=['POST'])
@login_required
def api_tracker_deploy():
    """Create and start monitoring a strategy from any source."""
    from config import set_thread_credentials
    data = request.json or {}
    api_key, api_secret, _, broker = get_profile_creds(data.get('profile_id'))
    if not api_key:
        return jsonify(error='No API profile selected'), 400
    set_thread_credentials(api_key, api_secret, broker)

    lot_sizes = {'BTC': 0.001, 'ETH': 0.01}
    asset = data.get('asset', 'BTC')

    strat = TrackedStrategy(
        source=data.get('source', 'Manual'),
        name=data.get('name', f"{asset} Strategy"),
        user_id=current_user_id(),
        legs=data.get('legs', []),
        asset=asset,
        lot_size=lot_sizes.get(asset, 0.001),
        max_profit=float(data.get('max_profit', 0)),
        max_loss=float(data.get('max_loss', 0)),
        profile_id=data.get('profile_id'),
        interval=int(data.get('interval', 10)),
        details=data.get('details', {}),
    )

    def on_done(pnl, reason):
        update_tracked(strat.sid, status='completed', pnl=round(pnl, 2))
        record_end(strat.sid, pnl, strat.adjustment_count)

    strat.on_complete = on_done
    registry.register(strat)
    track_strategy(strat.sid, strat.source, strat.name, current_user_id(), details=strat.details)
    record_start(strat.sid, data, user_id=current_user_id())
    strat.start_monitoring()

    return jsonify(sid=strat.sid, status='running')


# ── Serve React Frontend (catch-all — must be last) ──

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_react(path):
    dist = os.path.join(app.root_path, 'frontend', 'dist')
    if path and os.path.exists(os.path.join(dist, path)):
        return send_from_directory(dist, path)
    return send_from_directory(dist, 'index.html')


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=5000)
