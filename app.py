import threading
import queue
import config
from flask import Flask, render_template, request, jsonify, Response, redirect, session, url_for
from functools import wraps
from auth import check_api_connection
from strategy import DeltaNeutralStrategy

app = Flask(__name__)
app.secret_key = 'delta-neutral-bot-secret-key-change-me'

LOGIN_ID = 'admin'
LOGIN_PASSWORD = 'admin123'


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

strategy_thread = None
current_strategy = None
log_queue = queue.Queue()
strategy_running = False


class LogCapture:
    """Redirect print output to both console and SSE queue."""
    def __init__(self, original, q):
        self.original = original
        self.q = q

    def write(self, text):
        self.original.write(text)
        if text.strip():
            self.q.put(text.strip())

    def flush(self):
        self.original.flush()


def run_strategy(params):
    global current_strategy, strategy_running
    import sys
    old_stdout = sys.stdout
    sys.stdout = LogCapture(old_stdout, log_queue)
    try:
        config.EXPIRY_DATE = params['expiry_date']
        config.TARGET_DELTA = float(params['target_delta'])
        config.DELTA_TOLERANCE = float(params['delta_tolerance'])
        config.LOT_SIZE = int(params['lot_size'])
        config.PREMIUM_INCREASE_THRESHOLD = float(params['premium_threshold']) / 100
        config.TARGET_PNL = float(params['target_pnl'])
        config.MONITORING_INTERVAL = int(params['monitoring_interval'])

        if not check_api_connection():
            log_queue.put("❌ Cannot proceed without proper API access")
            strategy_running = False
            return

        current_strategy = DeltaNeutralStrategy()
        strategy_running = True

        if not current_strategy.initialize():
            log_queue.put("✗ Strategy initialization failed")
            current_strategy.ws_manager.stop()
            strategy_running = False
            return

        current_strategy.monitor_and_adjust()
    except Exception as e:
        log_queue.put(f"✗ Error: {e}")
        if current_strategy:
            current_strategy.close_all_positions()
    finally:
        if current_strategy:
            current_strategy.ws_manager.stop()
        sys.stdout = old_stdout
        strategy_running = False
        log_queue.put("__STOPPED__")


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['user_id'] == LOGIN_ID and request.form['password'] == LOGIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html', error=None)


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    return render_template('index.html',
        expiry_date=config.EXPIRY_DATE,
        target_delta=config.TARGET_DELTA,
        delta_tolerance=config.DELTA_TOLERANCE,
        lot_size=config.LOT_SIZE,
        premium_threshold=int(config.PREMIUM_INCREASE_THRESHOLD * 100),
        target_pnl=config.TARGET_PNL,
        monitoring_interval=config.MONITORING_INTERVAL,
        running='true' if strategy_running else 'false'
    )


@app.route('/start', methods=['POST'])
@login_required
def start():
    global strategy_thread, strategy_running
    if strategy_running:
        return jsonify(error="Strategy already running"), 400

    params = request.json
    strategy_thread = threading.Thread(target=run_strategy, args=(params,), daemon=True)
    strategy_thread.start()
    return jsonify(status="started")


@app.route('/stop', methods=['POST'])
@login_required
def stop():
    global current_strategy, strategy_running
    if not strategy_running or not current_strategy:
        return jsonify(error="No strategy running"), 400
    current_strategy.running = False
    current_strategy.close_all_positions()
    return jsonify(status="stopping")


@app.route('/stream')
@login_required
def stream():
    def generate():
        while True:
            try:
                msg = log_queue.get(timeout=30)
                if msg == "__STOPPED__":
                    yield f"event: stopped\ndata: done\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/status')
@login_required
def status():
    s = current_strategy
    if not s or not strategy_running:
        return jsonify(running=False)
    return jsonify(
        running=True,
        adjustment_count=s.adjustment_count,
        total_pnl=round(s.total_pnl, 2),
        realized_pnl=round(s.realized_pnl, 2),
        unrealized_pnl=round(s.unrealized_pnl, 2),
        call_symbol=s.call_position['symbol'] if s.call_position else None,
        put_symbol=s.put_position['symbol'] if s.put_position else None,
        call_entry=round(s.call_entry_price, 2),
        put_entry=round(s.put_entry_price, 2),
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=5000)
