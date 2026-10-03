"""Autentikasi CloudConvert-X: akun (SQLite), hash kata sandi (argon2), sesi (Redis).

- Akun dibuat admin lewat `manage.py` (tidak ada pendaftaran terbuka).
- Kata sandi hanya disimpan sebagai hash argon2.
- Sesi = token acak di cookie HttpOnly; Redis hanya menyimpan SHA-256 dari token,
  sehingga bocornya isi Redis tidak membocorkan sesi yang masih berlaku.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

DB_PATH = os.environ.get("AUTH_DB", "/data/users.db")
SESSION_TTL = int(os.environ.get("SESSION_DAYS", "7")) * 86400
COOKIE = "ccx_session"
MIN_PASSWORD = 10
MAX_PASSWORD = 256

# Pembatasan percobaan masuk
FAIL_WINDOW = 900        # detik
MAX_FAILS_EMAIL = 5      # per email
MAX_FAILS_IP = 30        # per alamat IP

ph = PasswordHasher()
# Hash palsu: dipakai agar waktu respons sama baik email ada maupun tidak.
_DUMMY_HASH = ph.hash("hash-palsu-penyama-waktu")


# --------------------------------------------------------------- akun (SQLite)
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with closing(db()) as c, c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 email TEXT NOT NULL UNIQUE,
                 name TEXT NOT NULL,
                 password_hash TEXT NOT NULL,
                 active INTEGER NOT NULL DEFAULT 1,
                 created_at REAL NOT NULL
               )"""
        )


def norm_email(email: str) -> str:
    return (email or "").strip().lower()[:254]


def check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"Kata sandi minimal {MIN_PASSWORD} karakter")
    if len(password) > MAX_PASSWORD:
        raise ValueError(f"Kata sandi maksimal {MAX_PASSWORD} karakter")


def _public(row) -> dict:
    return {"id": row["id"], "email": row["email"], "name": row["name"]}


def create_user(email: str, name: str, password: str) -> int:
    email = norm_email(email)
    if not (3 <= len(email) <= 254 and "@" in email and " " not in email):
        raise ValueError("Email tidak valid")
    check_password(password)
    name = (name or "").strip()[:80] or email.split("@")[0]
    try:
        with closing(db()) as c, c:
            cur = c.execute(
                "INSERT INTO users(email, name, password_hash, active, created_at) VALUES (?,?,?,1,?)",
                (email, name, ph.hash(password), time.time()),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("Email sudah terdaftar") from None


def list_users() -> list:
    with closing(db()) as c:
        return [dict(r) for r in c.execute(
            "SELECT id, email, name, active, created_at FROM users ORDER BY id")]


def get_user_id(email: str):
    with closing(db()) as c:
        row = c.execute("SELECT id FROM users WHERE email=?", (norm_email(email),)).fetchone()
    return row["id"] if row else None


def set_active(email: str, active: bool):
    """Kembalikan id pengguna, atau None bila email tidak ada."""
    uid = get_user_id(email)
    if uid is None:
        return None
    with closing(db()) as c, c:
        c.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, uid))
    return uid


def set_password(email: str, password: str):
    check_password(password)
    uid = get_user_id(email)
    if uid is None:
        return None
    with closing(db()) as c, c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (ph.hash(password), uid))
    return uid


def authenticate(email: str, password: str):
    """Kembalikan data pengguna bila email+kata sandi benar dan akun aktif, selain itu None."""
    with closing(db()) as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (norm_email(email),)).fetchone()
    try:
        ph.verify(row["password_hash"] if row else _DUMMY_HASH, (password or "")[:MAX_PASSWORD])
        ok = True
    except (VerificationError, InvalidHashError):
        ok = False
    if not (row and ok and row["active"]):
        return None
    if ph.check_needs_rehash(row["password_hash"]):
        with closing(db()) as c, c:
            c.execute("UPDATE users SET password_hash=? WHERE id=?",
                      (ph.hash(password), row["id"]))
    return _public(row)


# -------------------------------------------------------------- sesi (Redis)
def _sid(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(r, user: dict) -> str:
    token = secrets.token_urlsafe(32)
    sid = _sid(token)
    pipe = r.pipeline()
    pipe.set(f"session:{sid}", json.dumps(user), ex=SESSION_TTL)
    pipe.sadd(f"usersess:{user['id']}", sid)
    pipe.expire(f"usersess:{user['id']}", SESSION_TTL)
    pipe.execute()
    return token


def get_session(r, token):
    """Kembalikan data pengguna dari sesi yang valid (sekaligus memperpanjang masa berlakunya)."""
    if not token or len(token) > 200:
        return None
    key = f"session:{_sid(token)}"
    raw = r.get(key)
    if not raw:
        return None
    r.expire(key, SESSION_TTL)
    return json.loads(raw)


def destroy_session(r, token) -> None:
    if not token or len(token) > 200:
        return
    sid = _sid(token)
    raw = r.get(f"session:{sid}")
    r.delete(f"session:{sid}")
    if raw:
        r.srem(f"usersess:{json.loads(raw)['id']}", sid)


def revoke_user_sessions(r, uid: int) -> int:
    """Cabut semua sesi seorang pengguna (dipakai saat dinonaktifkan / ganti kata sandi)."""
    sids = r.smembers(f"usersess:{uid}")
    for sid in sids:
        r.delete(f"session:{sid}")
    r.delete(f"usersess:{uid}")
    return len(sids)


# -------------------------------------------------- pembatasan percobaan masuk
def _fail_keys(email: str, ip: str):
    return ((f"loginfail:email:{email}", MAX_FAILS_EMAIL), (f"loginfail:ip:{ip}", MAX_FAILS_IP))


def throttle_wait(r, email: str, ip: str) -> int:
    """Sisa detik pemblokiran (0 = boleh mencoba)."""
    wait = 0
    for key, limit in _fail_keys(email, ip):
        n = r.get(key)
        if n and int(n) >= limit:
            wait = max(wait, r.ttl(key), 1)
    return wait


def throttle_fail(r, email: str, ip: str) -> None:
    for key, _ in _fail_keys(email, ip):
        if r.incr(key) == 1:
            r.expire(key, FAIL_WINDOW)


def throttle_clear(r, email: str) -> None:
    r.delete(f"loginfail:email:{email}")
