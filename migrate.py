"""Re-seed: create a demo user and assign existing posts to them."""
import sqlite3
import uuid
import time
import sys
import hashlib
import secrets
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "gallery.db"

def hash_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32).hex()
    return f"{salt}${h}"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# Ensure tables exist (matches app.py)
conn.executescript("""
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    bio TEXT,
    created_at INTEGER NOT NULL
);
""")
# Add user_id column if missing
cols = [r["name"] for r in conn.execute("PRAGMA table_info(posts)").fetchall()]
if "user_id" not in cols:
    conn.execute("ALTER TABLE posts ADD COLUMN user_id TEXT")

# Create demo user if not exists
demo = conn.execute("SELECT * FROM users WHERE username = ?", ("demo",)).fetchone()
if not demo:
    demo_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (demo_id, "demo", hash_password("demo123"), int(time.time())),
    )
    print(f"Created user 'demo' (password: demo123)")
else:
    demo_id = demo["id"]
    print(f"User 'demo' already exists.")

# Assign all existing posts (which have author='neon_dreamer' etc) to demo user
# AND change their displayed author to 'demo'
n = conn.execute("UPDATE posts SET user_id = ?, author = ? WHERE user_id IS NULL OR user_id = ''", (demo_id, "demo")).rowcount
conn.commit()
print(f"Linked {n} legacy posts to demo user.")

conn.close()

