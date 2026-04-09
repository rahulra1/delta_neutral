import threading
import queue
import uuid
import config as default_config
from flask import Flask, render_template, request, jsonify, Response, redirect, session, url_for
from functools import wraps
from auth import check_api_connection
from strategy import DeltaNeutralStrategy
from trade_history import record_start, record_end, get_history
from models import init_db, create_user, verify_user, get_user, update_api_keys

app = Flask(__name__)
app.secret_key = 'delta-neutral-bot-secret-key-change-me'

init_db()

# {sid: {thread, strategy, log_queue, running, params, user_id}}
strategies = {}


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


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
    from config import set_thread_credentials
    entry = strategies[sid]

    # Route this thread's print() to this strategy's log queue
    LogCapture._local.log_queue = entry['log_queue']

    try:
        # Set per-thread API keys (isolated from other strategy threads)
        user = get_user(entry['user_id'])
        if not user or not user.get('api_key') or not user.get('api_secret'):
            entry['log_queue'].put("❌ API keys not configured. Go to Profile to add them.")
            entry['running'] = False
            return
        set_thread_credentials(user['api_key'], user['api_secret'])

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
            return render_template('register.html', error='Username and password required')
        if len(password) < 6:
            return render_template('register.html', error='Password must be at least 6 characters')
        if password != confirm:
            return render_template('register.html', error='Passwords do not match')
        if create_user(username, password):
            user = verify_user(username, password)
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        return render_template('register.html', error='Username already taken')
    return render_template('register.html', error=None)


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
        return render_template('profile.html', username=user['username'], api_key=api_key, api_secret=api_secret, success='API keys saved successfully')
    return render_template('profile.html', username=user['username'], api_key=user['api_key'] or '', api_secret=user['api_secret'] or '', success=None)


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
    e = strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return redirect(url_for('dashboard'))
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


@app.route('/start', methods=['POST'])
@login_required
def start():
    user = get_user(current_user_id())
    if not user.get('api_key') or not user.get('api_secret'):
        return jsonify(error="API keys not configured. Go to Profile first."), 400

    params = request.json
    sid = params.pop('sid', '') or str(uuid.uuid4())[:8]

    if sid in strategies and strategies[sid]['running']:
        return jsonify(error="Strategy already running"), 400

    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(), 'running': False, 'params': params, 'user_id': current_user_id()}
    strategies[sid] = entry
    record_start(sid, params)
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


@app.route('/api/expiries')
@login_required
def api_expiries():
    from api.chain import get_expiries
    asset = request.args.get('asset', 'BTC')
    return jsonify(expiries=get_expiries(asset))


@app.route('/api/chain')
@login_required
def api_chain():
    from api.chain import get_option_chain_full
    asset = request.args.get('asset', 'BTC')
    expiry = request.args.get('expiry', '')
    if not expiry:
        return jsonify(error="expiry required"), 400
    chain, spot, exp = get_option_chain_full(expiry, asset)
    if chain is None:
        return jsonify(error="Failed to fetch chain"), 500
    return jsonify(chain=chain, spot_price=spot, expiry=exp)


@app.route('/api/place-legs', methods=['POST'])
@login_required
def api_place_legs():
    """Place multiple option legs and optionally start monitoring."""
    from api.orders import place_order
    from config import set_thread_credentials
    user = get_user(current_user_id())
    if not user or not user.get('api_key') or not user.get('api_secret'):
        return jsonify(error="API keys not configured"), 400
    set_thread_credentials(user['api_key'], user['api_secret'])

    data = request.json
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

    # Start monitor if targets are set and all orders succeeded
    monitor_id = None
    if max_profit > 0 and max_loss > 0 and placed_legs and all(r['success'] for r in results):
        from strategy.monitor import StrategyMonitor
        asset = data.get('asset', 'BTC')
        lot_sizes = {'BTC': 0.001, 'ETH': 0.01}
        mon = StrategyMonitor(
            legs=placed_legs, max_profit=max_profit, max_loss=max_loss,
            asset=asset, lot_size=lot_sizes.get(asset, 0.001),
        )
        monitor_id = str(uuid.uuid4())[:8]
        active_monitors[monitor_id] = {'monitor': mon, 'user_id': current_user_id()}
        mon.start()

    return jsonify(results=results, monitor_id=monitor_id)


@app.route('/api/positions')
@login_required
def api_positions():
    """Return open option positions for the current user."""
    import re
    from api.positions import get_positions
    from config import set_thread_credentials
    user = get_user(current_user_id())
    if not user or not user.get('api_key') or not user.get('api_secret'):
        return jsonify(error="API keys not configured"), 400
    set_thread_credentials(user['api_key'], user['api_secret'])

    positions = get_positions()
    result = []
    for p in positions:
        size = int(p.get('size', 0))
        if size == 0:
            continue
        sym = p.get('product_symbol', '')
        # Parse type and strike from symbol: C-BTC-90000-170426 or P-ETH-2000-170426
        m = re.match(r'^(C|P)-(\w+)-(\d+)-\d+$', sym)
        opt_type = 'call' if (m and m.group(1) == 'C') else 'put' if m else 'unknown'
        strike = m.group(3) if m else '0'
        side = 'sell' if size < 0 else 'buy'
        result.append({
            'symbol': sym,
            'product_id': p.get('product_id'),
            'type': opt_type,
            'strike': strike,
            'side': side,
            'size': abs(size),
            'entry_price': float(p.get('entry_price', 0)),
            'mark_price': float(p.get('mark_price', 0)) if p.get('mark_price') else float(p.get('entry_price', 0)),
        })
    return jsonify(positions=result)


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
    user = get_user(current_user_id())
    set_thread_credentials(user['api_key'], user['api_secret'])
    entry['monitor'].stop()
    return jsonify(status="stopped")


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=5000)
