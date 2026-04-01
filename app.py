import threading
import queue
import uuid
import config as default_config
from types import SimpleNamespace
from flask import Flask, render_template, request, jsonify, Response, redirect, session, url_for
from functools import wraps
from auth import check_api_connection
from strategy import DeltaNeutralStrategy

app = Flask(__name__)
app.secret_key = 'delta-neutral-bot-secret-key-change-me'

LOGIN_ID = 'admin'
LOGIN_PASSWORD = 'admin123'

# {sid: {thread, strategy, log_queue, running, params}}
strategies = {}


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


class LogCapture:
    def __init__(self, original, q):
        self.original = original
        self.q = q

    def write(self, text):
        self.original.write(text)
        if text.strip():
            self.q.put(text.strip())

    def flush(self):
        self.original.flush()


def run_strategy(sid, params):
    import sys
    import config
    entry = strategies[sid]
    old_stdout = sys.stdout
    sys.stdout = LogCapture(old_stdout, entry['log_queue'])
    try:
        config.EXPIRY_DATE = params['expiry_date']
        config.TARGET_DELTA = float(params['target_delta'])
        config.DELTA_TOLERANCE = float(params['delta_tolerance'])
        config.LOT_SIZE = int(params['lot_size'])
        config.PREMIUM_INCREASE_THRESHOLD = float(params['premium_threshold']) / 100
        config.TARGET_PNL = float(params['target_pnl'])
        config.MONITORING_INTERVAL = int(params['monitoring_interval'])

        if not check_api_connection():
            entry['log_queue'].put("❌ Cannot proceed without proper API access")
            entry['running'] = False
            return

        s = DeltaNeutralStrategy()
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
        if entry.get('strategy'):
            entry['strategy'].ws_manager.stop()
        sys.stdout = old_stdout
        entry['running'] = False
        entry['log_queue'].put("__STOPPED__")


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['user_id'] == LOGIN_ID and request.form['password'] == LOGIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html', error=None)


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    strats = []
    for sid, e in strategies.items():
        strats.append(dict(
            id=sid,
            name=e['params'].get('expiry_date', '?'),
            running=e['running'],
            pnl=round(e['strategy'].total_pnl, 2) if e.get('strategy') else 0,
        ))
    return render_template('dashboard.html', strategies=strats)


@app.route('/strategy/new')
@login_required
def new_strategy():
    return render_template('index.html',
        sid='',
        expiry_date=default_config.EXPIRY_DATE,
        target_delta=default_config.TARGET_DELTA,
        delta_tolerance=default_config.DELTA_TOLERANCE,
        lot_size=default_config.LOT_SIZE,
        premium_threshold=int(default_config.PREMIUM_INCREASE_THRESHOLD * 100),
        target_pnl=default_config.TARGET_PNL,
        monitoring_interval=default_config.MONITORING_INTERVAL,
        running='false'
    )


@app.route('/strategy/<sid>')
@login_required
def view_strategy(sid):
    e = strategies.get(sid)
    if not e:
        return redirect(url_for('dashboard'))
    p = e['params']
    return render_template('index.html',
        sid=sid,
        expiry_date=p.get('expiry_date', default_config.EXPIRY_DATE),
        target_delta=p.get('target_delta', default_config.TARGET_DELTA),
        delta_tolerance=p.get('delta_tolerance', default_config.DELTA_TOLERANCE),
        lot_size=p.get('lot_size', default_config.LOT_SIZE),
        premium_threshold=p.get('premium_threshold', int(default_config.PREMIUM_INCREASE_THRESHOLD * 100)),
        target_pnl=p.get('target_pnl', default_config.TARGET_PNL),
        monitoring_interval=p.get('monitoring_interval', default_config.MONITORING_INTERVAL),
        running='true' if e['running'] else 'false'
    )


@app.route('/start', methods=['POST'])
@login_required
def start():
    params = request.json
    sid = params.pop('sid', '') or str(uuid.uuid4())[:8]

    if sid in strategies and strategies[sid]['running']:
        return jsonify(error="Strategy already running"), 400

    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(), 'running': False, 'params': params}
    strategies[sid] = entry
    entry['thread'] = threading.Thread(target=run_strategy, args=(sid, params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/stop', methods=['POST'])
@login_required
def stop():
    sid = request.json.get('sid')
    e = strategies.get(sid)
    if not e or not e['running'] or not e.get('strategy'):
        return jsonify(error="No strategy running"), 400
    e['strategy'].running = False
    e['strategy'].close_all_positions()
    return jsonify(status="stopping")


@app.route('/stream/<sid>')
@login_required
def stream(sid):
    e = strategies.get(sid)
    if not e:
        return Response("data: No strategy found\n\n", mimetype='text/event-stream')
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
    if not e or not e['running'] or not e.get('strategy'):
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
    entry = getattr(s, f'{leg}_actual_entry_price')
    cv = getattr(s, f'{leg}_contract_value')
    ws_data = s.ws_manager.get_latest_price(pos['symbol'])
    mark = ws_data['mark_price'] if ws_data else entry
    delta = ws_data.get('delta', 0) if ws_data else 0
    import config
    size = config.LOT_SIZE
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=5000)
