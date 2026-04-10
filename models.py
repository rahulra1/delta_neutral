import sqlite3
import os
import bcrypt
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), 'users.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    row = conn.execute('SELECT credits_remaining FROM user_credits WHERE user_id = ?', (user_id,)).fetchone()
    if not row or row['credits_remaining'] < cost:
        conn.close()
        return False, cost
    conn.execute('UPDATE user_credits SET credits_remaining = credits_remaining - ?, credits_used = credits_used + ? WHERE user_id = ?',
                 (cost, cost, user_id))
    conn.execute('INSERT INTO credit_transactions (user_id, amount, action, description) VALUES (?, ?, ?, ?)',
                 (user_id, -cost, action, description))
    conn.commit()
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
