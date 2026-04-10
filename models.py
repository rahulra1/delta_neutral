import sqlite3
import os
import bcrypt

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
        api_secret TEXT DEFAULT ''
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
    # migrate: add columns if table already exists without them
    cols = [r['name'] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if 'api_key' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN api_key TEXT DEFAULT ''")
    if 'api_secret' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN api_secret TEXT DEFAULT ''")
    pcols = [r['name'] for r in conn.execute("PRAGMA table_info(profiles)").fetchall()]
    if 'broker' not in pcols:
        conn.execute("ALTER TABLE profiles ADD COLUMN broker TEXT NOT NULL DEFAULT 'demo'")
    conn.commit()
    conn.close()

def create_user(username, password):
    conn = get_db()
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        conn.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (username, hashed))
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
