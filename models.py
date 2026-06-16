import sqlite3
import os
import bcrypt
from datetime import datetime, timedelta

DB_PATH = os.environ.get('ALGOX_DB_PATH', os.path.join(os.path.dirname(__file__), 'users.db'))

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        api_key TEXT DEFAULT '',
        api_secret TEXT DEFAULT '',
        is_admin INTEGER DEFAULT 0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        api_key TEXT NOT NULL,
        api_secret TEXT NOT NULL,
        broker TEXT NOT NULL DEFAULT 'demo',
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL DEFAULT 0,
        credits_per_month INTEGER DEFAULT 50,
        max_live_strategies INTEGER DEFAULT 1,
        max_brokers INTEGER DEFAULT 1
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS user_credits (
        user_id INTEGER PRIMARY KEY,
        plan_id INTEGER DEFAULT 1,
        credits_remaining INTEGER DEFAULT 50,
        credits_used INTEGER DEFAULT 0,
        plan_expires_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (plan_id) REFERENCES plans(id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS credit_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        action TEXT NOT NULL,
        description TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    # migrate existing tables
    cols = [r['name'] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if 'api_key' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN api_key TEXT DEFAULT ''")
    if 'api_secret' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN api_secret TEXT DEFAULT ''")
    if 'is_admin' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
    pcols = [r['name'] for r in conn.execute("PRAGMA table_info(profiles)").fetchall()]
    if 'broker' not in pcols:
        conn.execute("ALTER TABLE profiles ADD COLUMN broker TEXT NOT NULL DEFAULT 'demo'")
    # Seed default plans if empty
    if conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0:
        conn.execute("INSERT INTO plans (name, price, credits_per_month, max_live_strategies, max_brokers) VALUES ('Free', 0, 50, 1, 1)")
        conn.execute("INSERT INTO plans (name, price, credits_per_month, max_live_strategies, max_brokers) VALUES ('Basic', 499, 500, 5, 3)")
        conn.execute("INSERT INTO plans (name, price, credits_per_month, max_live_strategies, max_brokers) VALUES ('Pro', 999, 99999, 99, 99)")
    # Give credits to existing users who don't have a row yet
    for u in conn.execute("SELECT id FROM users WHERE id NOT IN (SELECT user_id FROM user_credits)").fetchall():
        conn.execute("INSERT INTO user_credits (user_id, plan_id, credits_remaining) VALUES (?, 1, 50)", (u['id'],))
    conn.execute('''CREATE TABLE IF NOT EXISTS live_strategies (
        sid TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        started_at TEXT NOT NULL,
        pnl REAL DEFAULT 0,
        details TEXT DEFAULT '{}',
        legs TEXT DEFAULT '[]',
        logs TEXT DEFAULT '[]',
        max_profit REAL DEFAULT 0,
        max_loss REAL DEFAULT 0,
        profile_id INTEGER,
        asset TEXT DEFAULT 'BTC',
        lot_size REAL DEFAULT 0.001,
        interval INTEGER DEFAULT 10,
        exit_reason TEXT,
        adjustment_count INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    # Migrate: add logs column if missing
    ls_cols = [r['name'] for r in conn.execute("PRAGMA table_info(live_strategies)").fetchall()]
    if 'logs' not in ls_cols:
        conn.execute("ALTER TABLE live_strategies ADD COLUMN logs TEXT DEFAULT '[]'")
    # P&L time-series snapshots for performance graphs
    conn.execute('''CREATE TABLE IF NOT EXISTS pnl_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        sid TEXT NOT NULL,
        pnl REAL NOT NULL,
        ts TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pnl_snap_user_ts ON pnl_snapshots(user_id, ts)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pnl_snap_sid ON pnl_snapshots(sid, ts)')
    # Auto-promote first user to admin
    conn.execute('UPDATE users SET is_admin = 1 WHERE id = 1')
    conn.commit()
    conn.close()

def create_user(username, password):
    conn = get_db()
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        conn.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (username, hashed))
        uid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute('INSERT INTO user_credits (user_id, plan_id, credits_remaining) VALUES (?, 1, 50)', (uid,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def verify_user(username, password):
    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    if row and bcrypt.checkpw(password.encode(), row['password_hash'].encode()):
        return dict(row)
    return None

def get_user(user_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def update_api_keys(user_id, api_key, api_secret):
    conn = get_db()
    conn.execute('UPDATE users SET api_key = ?, api_secret = ? WHERE id = ?', (api_key, api_secret, user_id))
    conn.commit()
    conn.close()


# ── Profiles ──

def get_profiles(user_id):
    conn = get_db()
    rows = conn.execute('SELECT * FROM profiles WHERE user_id = ? ORDER BY name', (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_profile(profile_id, user_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM profiles WHERE id = ? AND user_id = ?', (profile_id, user_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_profile(user_id, name, api_key, api_secret, broker='demo'):
    conn = get_db()
    conn.execute('INSERT INTO profiles (user_id, name, api_key, api_secret, broker) VALUES (?, ?, ?, ?, ?)',
                 (user_id, name, api_key, api_secret, broker))
    conn.commit()
    conn.close()


def update_profile(profile_id, user_id, name, api_key, api_secret, broker='demo'):
    conn = get_db()
    conn.execute('UPDATE profiles SET name = ?, api_key = ?, api_secret = ?, broker = ? WHERE id = ? AND user_id = ?',
                 (name, api_key, api_secret, broker, profile_id, user_id))
    conn.commit()
    conn.close()


def delete_profile(profile_id, user_id):
    conn = get_db()
    conn.execute('DELETE FROM profiles WHERE id = ? AND user_id = ?', (profile_id, user_id))
    conn.commit()
    conn.close()


# ── Credits ──

CREDIT_COSTS = {
    'deploy_live': 10,
    'paper_trade': 2,
    'deploy_builder': 10,
    'place_legs': 5,
}

def get_user_credits(user_id):
    conn = get_db()
    row = conn.execute('''SELECT uc.*, p.name as plan_name, p.credits_per_month, p.max_live_strategies, p.max_brokers
        FROM user_credits uc JOIN plans p ON uc.plan_id = p.id WHERE uc.user_id = ?''', (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def deduct_credits(user_id, action, description=''):
    cost = CREDIT_COSTS.get(action, 0)
    if cost == 0:
        return True, 0
    conn = get_db()
    try:
        row = conn.execute('SELECT credits_remaining FROM user_credits WHERE user_id = ?', (user_id,)).fetchone()
        if not row or row['credits_remaining'] < cost:
            return False, cost
        conn.execute('UPDATE user_credits SET credits_remaining = credits_remaining - ?, credits_used = credits_used + ? WHERE user_id = ?',
                     (cost, cost, user_id))
        conn.execute('INSERT INTO credit_transactions (user_id, amount, action, description) VALUES (?, ?, ?, ?)',
                     (user_id, -cost, action, description))
        conn.commit()
    finally:
        conn.close()
    return True, cost


def add_credits(user_id, amount, description='Admin grant'):
    conn = get_db()
    conn.execute('UPDATE user_credits SET credits_remaining = credits_remaining + ? WHERE user_id = ?', (amount, user_id))
    conn.execute('INSERT INTO credit_transactions (user_id, amount, action, description) VALUES (?, ?, ?, ?)',
                 (user_id, amount, 'admin_grant', description))
    conn.commit()
    conn.close()


def set_user_plan(user_id, plan_id):
    conn = get_db()
    plan = conn.execute('SELECT * FROM plans WHERE id = ?', (plan_id,)).fetchone()
    if not plan:
        conn.close()
        return False
    conn.execute('UPDATE user_credits SET plan_id = ?, credits_remaining = ? WHERE user_id = ?',
                 (plan_id, plan['credits_per_month'], user_id))
    conn.execute('INSERT INTO credit_transactions (user_id, amount, action, description) VALUES (?, ?, ?, ?)',
                 (user_id, plan['credits_per_month'], 'plan_change', f"Switched to {plan['name']}"))
    conn.commit()
    conn.close()
    return True


def get_credit_history(user_id, limit=50):
    conn = get_db()
    rows = conn.execute('SELECT * FROM credit_transactions WHERE user_id = ? ORDER BY created_at DESC LIMIT ?',
                        (user_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Admin ──

def is_admin(user_id):
    conn = get_db()
    row = conn.execute('SELECT is_admin FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    return bool(row and row['is_admin'])


def set_admin(user_id, val=True):
    conn = get_db()
    conn.execute('UPDATE users SET is_admin = ? WHERE id = ?', (1 if val else 0, user_id))
    conn.commit()
    conn.close()


def get_all_users():
    conn = get_db()
    rows = conn.execute('''SELECT u.id, u.username, u.is_admin,
        uc.credits_remaining, uc.credits_used, p.name as plan_name, p.id as plan_id
        FROM users u
        LEFT JOIN user_credits uc ON u.id = uc.user_id
        LEFT JOIN plans p ON uc.plan_id = p.id
        ORDER BY u.id''').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_plans():
    conn = get_db()
    rows = conn.execute('SELECT * FROM plans ORDER BY price').fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Live strategy persistence ---
import json as _json

def save_strategy(sid, user_id, source, name, status, started_at, pnl=0,
                  details=None, legs=None, logs=None, max_profit=0, max_loss=0,
                  profile_id=None, asset='BTC', lot_size=0.001, interval=10,
                  exit_reason=None, adjustment_count=0):
    conn = get_db()
    try:
        conn.execute('''INSERT OR REPLACE INTO live_strategies
            (sid, user_id, source, name, status, started_at, pnl, details, legs, logs,
             max_profit, max_loss, profile_id, asset, lot_size, interval,
             exit_reason, adjustment_count, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)''',
            (sid, user_id, source, name, status, started_at, pnl,
             _json.dumps(details or {}), _json.dumps(legs or []), _json.dumps(logs or []),
             max_profit, max_loss, profile_id, asset, lot_size, interval,
             exit_reason, adjustment_count))
        conn.commit()
    finally:
        conn.close()

_ALLOWED_STRATEGY_COLUMNS = frozenset({
    'status', 'pnl', 'details', 'legs', 'logs', 'max_profit', 'max_loss',
    'profile_id', 'asset', 'lot_size', 'interval', 'exit_reason', 'adjustment_count',
    'name', 'source',
})

def update_strategy_db(sid, **kwargs):
    conn = get_db()
    try:
        for k in ('details', 'legs', 'logs'):
            if k in kwargs:
                kwargs[k] = _json.dumps(kwargs[k])
        safe_kwargs = {k: v for k, v in kwargs.items() if k in _ALLOWED_STRATEGY_COLUMNS}
        if not safe_kwargs:
            return
        sets = ', '.join(f'{k}=?' for k in safe_kwargs)
        vals = list(safe_kwargs.values()) + [sid]
        conn.execute(f'UPDATE live_strategies SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE sid=?', vals)
        conn.commit()
    finally:
        conn.close()

def get_live_strategies(user_id):
    conn = get_db()
    try:
        rows = conn.execute('SELECT * FROM live_strategies WHERE user_id=? AND status IN (?,?)',
                            (user_id, 'running', 'open (no monitor)')).fetchall()
    finally:
        conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d['details'] = _json.loads(d['details'])
        d['legs'] = _json.loads(d['legs'])
        d['logs'] = _json.loads(d.get('logs') or '[]')
        result.append(d)
    return result

def delete_strategy_db(sid):
    conn = get_db()
    try:
        conn.execute('DELETE FROM live_strategies WHERE sid=?', (sid,))
        conn.commit()
    finally:
        conn.close()


# --- P&L snapshots ---

def save_pnl_snapshot(user_id, sid, pnl):
    conn = get_db()
    try:
        conn.execute('INSERT INTO pnl_snapshots (user_id, sid, pnl, ts) VALUES (?,?,?,?)',
                     (user_id, sid, round(pnl, 2), datetime.now().isoformat()))
        conn.commit()
    finally:
        conn.close()


def get_pnl_snapshots(user_id, sid=None, since=None):
    conn = get_db()
    try:
        q = 'SELECT sid, pnl, ts FROM pnl_snapshots WHERE user_id=?'
        params = [user_id]
        if sid:
            q += ' AND sid=?'
            params.append(sid)
        if since:
            q += ' AND ts>=?'
            params.append(since)
        q += ' ORDER BY ts'
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
