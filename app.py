import threading
import queue
import uuid
import config as default_config
from flask import Flask, render_template, request, jsonify, Response, redirect, session, url_for
from functools import wraps
from auth import check_api_connection
from strategy import DeltaNeutralStrategy
from trade_history import record_start, record_end, get_history
from models import init_db, create_user, verify_user, get_user, update_api_keys, get_profiles, get_profile, create_profile, update_profile, delete_profile, get_user_credits, deduct_credits, add_credits, set_user_plan, get_credit_history, is_admin, set_admin, get_all_users, get_all_plans, CREDIT_COSTS

app = Flask(__name__)
app.secret_key = 'delta-neutral-bot-secret-key-change-me'

init_db()

# {sid: {thread, strategy, log_queue, running, params, user_id}}
strategies = {}

# Unified tracker for all strategies from any source
# {sid: {source, name, status, user_id, pnl, started_at, details, ...}}
all_tracked = {}

def track_strategy(sid, source, name, user_id, details=None):
    """Register a strategy in the unified tracker."""
    from datetime import datetime
    all_tracked[sid] = {
        'sid': sid, 'source': source, 'name': name,
        'user_id': user_id, 'status': 'running',
        'started_at': datetime.now().isoformat(),
        'pnl': 0, 'details': details or {},
    }

def update_tracked(sid, **kwargs):
    if sid in all_tracked:
        all_tracked[sid].update(kwargs)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id') or not is_admin(session['user_id']):
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


def current_user_id():
    return session.get('user_id')


class LogCapture:
    """Thread-aware stdout that routes print() to the correct strategy's log queue."""
    _local = threading.local()

    def __init__(self, original):
        self.original = original

    def write(self, text):
        self.original.write(text)
        q = getattr(LogCapture._local, 'log_queue', None)
        if q and text.strip():
            q.put(text.strip())

    def flush(self):
        self.original.flush()


# Install once at import time — all threads share this, but each routes to its own queue
import sys
sys.stdout = LogCapture(sys.stdout)


def run_strategy(sid, params):
    entry = strategies[sid]

    # Route this thread's print() to this strategy's log queue
    LogCapture._local.log_queue = entry['log_queue']

    try:
        # Set per-thread API keys from profile or default
        from config import set_thread_credentials
        profile_id = entry.get('profile_id')
        if profile_id:
            p = get_profile(int(profile_id), entry['user_id'])
            if p:
                set_thread_credentials(p['api_key'], p['api_secret'], p.get('broker', 'demo'))
            else:
                entry['log_queue'].put("❌ Profile not found.")
                entry['running'] = False
                return
        else:
            user = get_user(entry['user_id'])
            if not user or not user.get('api_key') or not user.get('api_secret'):
                entry['log_queue'].put("❌ API keys not configured. Go to Profile to add them.")
                entry['running'] = False
                return
            set_thread_credentials(user['api_key'], user['api_secret'], 'demo')

        if not check_api_connection():
            entry['log_queue'].put("❌ Cannot proceed without proper API access")
            entry['running'] = False
            return

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

        s.monitor_and_adjust()
    except Exception as e:
        entry['log_queue'].put(f"✗ Error: {e}")
        if entry.get('strategy'):
            entry['strategy'].close_all_positions()
    finally:
        pnl = entry['strategy'].cumulative_realized_pnl if entry.get('strategy') else 0
        adj = entry['strategy'].adjustment_count if entry.get('strategy') else 0
        record_end(sid, pnl, adj)
        update_tracked(sid, status='completed', pnl=round(pnl, 2))
        if entry.get('strategy'):
            entry['strategy'].ws_manager.stop()
        LogCapture._local.log_queue = None
        entry['running'] = False
        entry['log_queue'].put("__STOPPED__")


# ── Auth Routes ──

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if not username or not password:
            return render_template('login.html', error='Username and password required')
        if len(password) < 6:
            return render_template('login.html', error='Password must be at least 6 characters')
        if password != confirm:
            return render_template('login.html', error='Passwords do not match')
        if create_user(username, password):
            user = verify_user(username, password)
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='Username already taken')
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = verify_user(request.form.get('username', ''), request.form.get('password', ''))
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html', error=None)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = get_user(current_user_id())
    if request.method == 'POST':
        api_key = request.form.get('api_key', '').strip()
        api_secret = request.form.get('api_secret', '').strip()
        update_api_keys(current_user_id(), api_key, api_secret)
        return render_template('profile.html', username=user['username'], api_key=api_key, api_secret=api_secret, success='API keys saved successfully', profiles=get_profiles(current_user_id()))
    return render_template('profile.html', username=user['username'], api_key=user['api_key'] or '', api_secret=user['api_secret'] or '', success=None, profiles=get_profiles(current_user_id()))


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


@app.route('/broker')
@login_required
def broker_page():
    return render_template('broker.html', username=session.get('username'))


@app.route('/broker/setup')
@login_required
def broker_setup_page():
    return render_template('broker_setup.html', username=session.get('username'))


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

@app.route('/')
@login_required
def dashboard():
    uid = current_user_id()
    strats = []
    for sid, e in strategies.items():
        if e.get('user_id') != uid:
            continue
        strats.append(dict(
            id=sid,
            name=e['params'].get('expiry_date', '?'),
            running=e['running'],
            pnl=round(e['strategy'].total_pnl, 2) if e.get('strategy') else 0,
        ))
    return render_template('dashboard.html', strategies=strats, username=session.get('username'))


@app.route('/api/dashboard')
@login_required
def api_dashboard():
    """Compute dashboard stats from trade history."""
    all_history = get_history()
    trades = all_history  # show all trades

    completed = [t for t in trades if t.get('status') == 'completed']
    running_count = sum(1 for sid, e in strategies.items() if e.get('user_id') == current_user_id() and e.get('running'))
    pnls = [t.get('pnl', 0) for t in completed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)

    # P&L over time for chart
    pnl_series = []
    cumulative = 0
    for t in completed:
        cumulative += t.get('pnl', 0)
        pnl_series.append({'date': (t.get('ended_at') or t.get('started_at', ''))[:10], 'pnl': round(cumulative, 2)})

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


@app.route('/api/strategies')
@login_required
def api_all_strategies():
    """Return all tracked strategies for the current user with live PnL."""
    uid = current_user_id()
    result = []
    for sid, t in all_tracked.items():
        if t['user_id'] != uid:
            continue
        entry = dict(t)
        # Update live PnL for running strategies
        if entry['status'] == 'running':
            if sid in strategies and strategies[sid].get('strategy'):
                s = strategies[sid]['strategy']
                entry['pnl'] = round(getattr(s, 'total_pnl', 0), 2)
            elif sid in active_monitors:
                mon = active_monitors[sid]['monitor']
                entry['pnl'] = round(mon.current_pnl, 2)
                if not mon.running:
                    entry['status'] = 'completed'
        result.append(entry)
    return jsonify(strategies=result)


@app.route('/api/strategies/<sid>/close', methods=['POST'])
@login_required
def api_close_strategy(sid):
    """Close a single strategy by sid."""
    uid = current_user_id()
    if sid not in all_tracked or all_tracked[sid]['user_id'] != uid:
        return jsonify(error="Not found"), 404

    from config import set_thread_credentials
    from api.orders import place_order

    # Resolve profile_id from whichever source has it
    profile_id = None
    if sid in strategies:
        profile_id = strategies[sid].get('profile_id')
    elif sid in active_monitors:
        profile_id = active_monitors[sid].get('profile_id')
    elif all_tracked[sid].get('details', {}).get('profile_id'):
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
    # Option Chain monitor
    if sid in active_monitors:
        active_monitors[sid]['monitor'].stop()
        closed = True
    # Option Chain with no monitor — close positions by reversing each leg
    if not closed and api_key:
        details = all_tracked[sid].get('details', {})
        placed_legs = details.get('legs', [])
        if isinstance(placed_legs, list) and placed_legs:
            for leg in placed_legs:
                close_side = 'buy' if leg['side'] == 'sell' else 'sell'
                place_order(leg['product_id'], leg['symbol'], int(leg['size']), close_side)
            closed = True

    update_tracked(sid, status='closed')
    return jsonify(success=closed, status='closed')


@app.route('/api/strategies/close-all', methods=['POST'])
@login_required
def api_close_all_strategies():
    """Close all running strategies for the current user."""
    uid = current_user_id()
    from config import set_thread_credentials
    closed_count = 0

    for sid, t in list(all_tracked.items()):
        if t['user_id'] != uid or t['status'] not in ('running', 'open (no monitor)'):
            continue

        # Resolve profile_id from all sources
        profile_id = None
        if sid in strategies:
            profile_id = strategies[sid].get('profile_id')
        elif sid in active_monitors:
            profile_id = active_monitors[sid].get('profile_id')
        elif t.get('details', {}).get('profile_id'):
            profile_id = t['details']['profile_id']

        api_key, api_secret, _, broker = get_profile_creds(profile_id)
        if api_key:
            set_thread_credentials(api_key, api_secret, broker)

        closed = False
        if sid in strategies and strategies[sid].get('strategy'):
            strategies[sid]['strategy'].running = False
            strategies[sid]['strategy'].close_all_positions()
            closed = True
        if sid in active_monitors:
            active_monitors[sid]['monitor'].stop()
            closed = True
        # Option Chain with no monitor
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


@app.route('/strategy/new')
@login_required
def new_strategy():
    asset = request.args.get('asset', 'BTC')
    return render_template('index.html',
        sid='',
        asset=asset,
        expiry_date=default_config.EXPIRY_DATE,
        target_delta=default_config.TARGET_DELTA,
        delta_tolerance=default_config.DELTA_TOLERANCE,
        lot_size=default_config.LOT_SIZE,
        premium_threshold=int(default_config.PREMIUM_INCREASE_THRESHOLD * 100),
        target_pnl=default_config.TARGET_PNL,
        monitoring_interval=default_config.MONITORING_INTERVAL,
        max_adjustments=default_config.MAX_ADJUSTMENTS,
        running='false',
        username=session.get('username')
    )


@app.route('/strategy/<sid>')
@login_required
def view_strategy(sid):
    uid = current_user_id()
    # Delta Neutral strategy with live logs
    e = strategies.get(sid)
    if e and e.get('user_id') == uid:
        p = e['params']
        return render_template('index.html',
            sid=sid,
            asset=p.get('asset', 'BTC'),
            expiry_date=p.get('expiry_date', default_config.EXPIRY_DATE),
            target_delta=p.get('target_delta', default_config.TARGET_DELTA),
            delta_tolerance=p.get('delta_tolerance', default_config.DELTA_TOLERANCE),
            lot_size=p.get('lot_size', default_config.LOT_SIZE),
            premium_threshold=p.get('premium_threshold', int(default_config.PREMIUM_INCREASE_THRESHOLD * 100)),
            target_pnl=p.get('target_pnl', default_config.TARGET_PNL),
            monitoring_interval=p.get('monitoring_interval', default_config.MONITORING_INTERVAL),
            max_adjustments=p.get('max_adjustments', default_config.MAX_ADJUSTMENTS),
            running='true' if e['running'] else 'false',
            username=session.get('username')
        )
    # Any tracked strategy (Option Chain, Strategy Builder, etc.)
    t = all_tracked.get(sid)
    if t and t['user_id'] == uid:
        return render_template('strategy_detail.html', sid=sid, username=session.get('username'))
    # Fallback: check trade_history.json
    for h in get_history():
        if h.get('sid') == sid:
            return render_template('strategy_detail.html', sid=sid, username=session.get('username'))
    return redirect(url_for('dashboard'))


@app.route('/api/strategy-detail/<sid>')
@login_required
def api_strategy_detail(sid):
    uid = current_user_id()
    # Check in-memory tracked strategies
    t = all_tracked.get(sid)
    if t and t['user_id'] == uid:
        entry = dict(t)
        if sid in strategies and strategies[sid].get('strategy'):
            entry['pnl'] = round(strategies[sid]['strategy'].total_pnl, 2)
        elif sid in active_monitors:
            mon = active_monitors[sid]['monitor']
            entry['pnl'] = round(mon.current_pnl, 2)
            entry['monitor'] = mon.get_status()
            if not mon.running:
                entry['status'] = 'completed'
        return jsonify(**entry)
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


@app.route('/start', methods=['POST'])
@login_required
@credits_required('deploy_live')
def start():
    params = request.json
    profile_id = params.pop('profile_id', None)

    # Validate credentials from profile or default
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected or keys not configured."), 400

    sid = params.pop('sid', '') or str(uuid.uuid4())[:8]

    if sid in strategies and strategies[sid]['running']:
        return jsonify(error="Strategy already running"), 400

    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(), 'running': False, 'params': params, 'user_id': current_user_id(), 'profile_id': profile_id}
    strategies[sid] = entry
    record_start(sid, params)
    track_strategy(sid, 'AlgoX DN', f"{params.get('asset','BTC')} {params.get('expiry_date','')}", current_user_id(), details=params)
    entry['thread'] = threading.Thread(target=run_strategy, args=(sid, params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/stop', methods=['POST'])
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
@login_required
def status(sid):
    e = strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(running=False)
    if not e['running'] or not e.get('strategy'):
        return jsonify(running=False)
    s = e['strategy']
    return jsonify(
        running=True,
        adjustment_count=s.adjustment_count,
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
    ws_data = s.ws_manager.get_latest_price(pos['symbol'])
    mark = ws_data['mark_price'] if ws_data else getattr(s, f'{leg}_actual_entry_price')
    delta = ws_data.get('delta', 0) if ws_data else 0

    # Use real position data from exchange for accurate P&L
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


@app.route('/performance')
@login_required
def performance():
    return render_template('performance.html', username=session.get('username'))


@app.route('/api/history')
@login_required
def api_history():
    uid = current_user_id()
    all_history = get_history()
    # Filter to only show history for strategies owned by this user
    user_sids = {sid for sid, e in strategies.items() if e.get('user_id') == uid}
    user_history = [h for h in all_history if h.get('sid') in user_sids]
    return jsonify(user_history)


# ── Option Chain Routes ──

active_monitors = {}  # {monitor_id: {monitor, user_id}}

@app.route('/option-chain')
@login_required
def option_chain_page():
    return render_template('option_chain.html', username=session.get('username'))


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
        chain, spot, exp = get_nse_chain(asset, expiry)
    if chain is None:
        return jsonify(error="Failed to fetch chain"), 500
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

    # Always track the strategy
    asset = data.get('asset', 'BTC')
    sid = str(uuid.uuid4())[:8]
    if placed_legs:
        leg_names = ', '.join(l['symbol'] for l in placed_legs[:3])
        track_strategy(sid, 'Option Chain', f"{asset} {leg_names}", current_user_id(),
                       details={'legs': placed_legs, 'max_profit': max_profit, 'max_loss': max_loss, 'asset': asset, 'profile_id': data.get('profile_id')})

    # Start monitor if targets are set and all orders succeeded
    monitor_id = None
    if max_profit > 0 and max_loss > 0 and placed_legs and all(r['success'] for r in results):
        from strategy.monitor import StrategyMonitor
        lot_sizes = {'BTC': 0.001, 'ETH': 0.01}
        mon = StrategyMonitor(
            legs=placed_legs, max_profit=max_profit, max_loss=max_loss,
            asset=asset, lot_size=lot_sizes.get(asset, 0.001),
        )
        monitor_id = sid
        active_monitors[monitor_id] = {'monitor': mon, 'user_id': current_user_id(), 'profile_id': data.get('profile_id')}
        mon.on_complete = lambda pnl, reason: update_tracked(sid, status='completed', pnl=round(pnl, 2))
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
        cv = 0.01 if asset == 'ETH' else 0.001
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
    data = request.json
    api_key, api_secret, _, broker = get_profile_creds(data.get('profile_id'))
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    set_thread_credentials(api_key, api_secret, broker)
    # To close: buy back if short, sell if long
    close_side = 'buy' if data['side'] == 'sell' else 'sell'
    result = place_order(data['product_id'], data['symbol'], int(data['size']), close_side)
    return jsonify(success=result is not None)

@app.route('/api/monitor/<mid>')
@login_required
def api_monitor_status(mid):
    entry = active_monitors.get(mid)
    if not entry or entry['user_id'] != current_user_id():
        return jsonify(error="Not found"), 404
    return jsonify(**entry['monitor'].get_status())


@app.route('/api/monitor/<mid>/stop', methods=['POST'])
@login_required
def api_monitor_stop(mid):
    entry = active_monitors.get(mid)
    if not entry or entry['user_id'] != current_user_id():
        return jsonify(error="Not found"), 404
    from config import set_thread_credentials
    api_key, api_secret, _, broker = get_profile_creds(entry.get('profile_id'))
    if api_key:
        set_thread_credentials(api_key, api_secret, broker)
    entry['monitor'].stop()
    return jsonify(status="stopped")


# ── Chart Routes ──

@app.route('/chart')
@login_required
def chart_page():
    return render_template('chart.html', username=session.get('username'))


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

@app.route('/strategy-builder')
@login_required
def strategy_builder_page():
    return render_template('strategy_builder.html', username=session.get('username'))


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

    if not placed_legs:
        return jsonify(error="All orders failed", results=results), 500

    sid = str(uuid.uuid4())[:8]
    track_strategy(sid, 'Strategy Builder', data.get('name', 'Unnamed'), current_user_id(), details=data)

    # Start monitor if risk targets are set
    risk = data.get('risk', {})
    sl_pct = float(risk.get('sl_pct', 0))
    tgt_pct = float(risk.get('target_pct', 0))
    total_premium = sum(l['entry_price'] * l['size'] for l in placed_legs if l['side'] == 'sell')
    lot_sizes = {'BTC': 0.001, 'ETH': 0.01}
    lot_size = lot_sizes.get(asset, 0.001)
    max_profit = total_premium * lot_size * tgt_pct / 100 if tgt_pct else 0
    max_loss = total_premium * lot_size * sl_pct / 100 if sl_pct else 0

    monitor_id = None
    if max_profit > 0 and max_loss > 0:
        mon = StrategyMonitor(
            legs=placed_legs, max_profit=max_profit, max_loss=max_loss,
            asset=asset, lot_size=lot_size,
        )
        monitor_id = sid
        active_monitors[monitor_id] = {'monitor': mon, 'user_id': current_user_id(), 'profile_id': data.get('profile_id')}
        mon.on_complete = lambda pnl, reason: update_tracked(sid, status='completed', pnl=round(pnl, 2))
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

@app.route('/admin')
@login_required
def admin_page():
    if not is_admin(current_user_id()):
        return redirect('/')
    return render_template('admin.html', username=session.get('username'))


@app.route('/api/admin/users')
@login_required
@admin_required
def api_admin_users():
    return jsonify(users=get_all_users())


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=5000)
