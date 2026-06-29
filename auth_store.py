"""SQLite-backed accounts, sessions, user data, and daily quotas."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PERSIST_DIR = "/var/data"
_LOCAL_DB = os.path.join(BASE_DIR, "data", "nasaroai.db")
_PERSIST_DB = os.path.join(_PERSIST_DIR, "nasaroai.db")


def _resolve_db_path() -> str:
    """Use Render persistent disk when available so deploys do not wipe accounts."""
    env = os.environ.get("NASAROAI_DB_PATH", "").strip()
    if env:
        return env
    if os.path.isdir(_PERSIST_DIR):
        if not os.path.exists(_PERSIST_DB) and os.path.isfile(_LOCAL_DB):
            try:
                import shutil
                shutil.copy2(_LOCAL_DB, _PERSIST_DB)
            except OSError:
                pass
        return _PERSIST_DB
    return _LOCAL_DB


DB_PATH = _resolve_db_path()
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
PRESENCE_TTL_SECONDS = 120  # 온라인 = 최근 2분 내 활동

_lock = threading.Lock()

MEMBER_DAILY_COINS = 250.0
GUEST_DAILY_COINS = 70.0
MEMBER_INITIAL_COINS = 250.0
GUEST_INITIAL_COINS = 70.0
POOL_FEATURE = "_pool"
WORK_COIN_TIP_AMOUNT = 1.0
WORK_COIN_TIP_GRACE_SECONDS = 5.0

MEMBER_QUOTA_LIMITS: dict[str, float] = {
    "compare": MEMBER_DAILY_COINS,
    "debate": MEMBER_DAILY_COINS,
    "collab": MEMBER_DAILY_COINS,
    "agent": MEMBER_DAILY_COINS,
}
GUEST_QUOTA_LIMITS: dict[str, float] = {
    "compare": GUEST_DAILY_COINS,
    "debate": GUEST_DAILY_COINS,
    "collab": GUEST_DAILY_COINS,
    "agent": GUEST_DAILY_COINS,
}
QUOTA_LIMITS = MEMBER_QUOTA_LIMITS  # admin backward compat
QUOTA_ADMIN_FEATURES = set(MEMBER_QUOTA_LIMITS) | {POOL_FEATURE}


def daily_pool_limit(subject: str) -> float:
    overrides = _load_quota_limit_overrides(subject)
    if POOL_FEATURE in overrides:
        return float(overrides[POOL_FEATURE])
    return MEMBER_DAILY_COINS if (subject or "").startswith("user:") else GUEST_DAILY_COINS


def _total_coins_used(conn: sqlite3.Connection, subject: str, day_key: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) AS total FROM quota_usage WHERE subject = ? AND day_key = ?",
        (subject, day_key),
    ).fetchone()
    return float(row["total"]) if row else 0.0


def _load_quota_limit_overrides(subject: str) -> dict[str, float]:
    clean = (subject or "").strip()
    if not clean:
        return {}
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT feature, daily_limit FROM quota_limit_overrides WHERE subject = ?",
                (clean,),
            ).fetchall()
        finally:
            conn.close()
    return {str(r["feature"]): float(r["daily_limit"]) for r in rows}


def quota_limits_for_subject(subject: str) -> dict[str, float]:
    base = MEMBER_QUOTA_LIMITS if (subject or "").startswith("user:") else GUEST_QUOTA_LIMITS
    overrides = _load_quota_limit_overrides(subject)
    if not overrides:
        return dict(base)
    merged = dict(base)
    merged.update(overrides)
    return merged


def is_guest_subject(subject: str) -> bool:
    return not (subject or "").startswith("user:")


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_connection() -> Iterator[sqlite3.Connection]:
    """Shared SQLite access for modules outside auth_store (e.g. debate sessions)."""
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                CREATE TABLE IF NOT EXISTS user_data (
                    user_id INTEGER NOT NULL,
                    data_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(user_id, data_key),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                CREATE TABLE IF NOT EXISTS quota_usage (
                    subject TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(subject, feature, day_key)
                );
                CREATE TABLE IF NOT EXISTS share_links (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    user_id INTEGER,
                    device_id TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT 'web',
                    feature TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_activity_user ON activity_log(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_activity_device ON activity_log(device_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS support_inquiries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    device_id TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT 'web',
                    username TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS support_replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inquiry_id INTEGER NOT NULL,
                    from_admin INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(inquiry_id) REFERENCES support_inquiries(id)
                );
                CREATE TABLE IF NOT EXISTS public_works (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_public_works_created ON public_works(created_at DESC);
                CREATE TABLE IF NOT EXISTS work_likes (
                    work_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(work_id, user_id),
                    FOREIGN KEY(work_id) REFERENCES public_works(id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                CREATE TABLE IF NOT EXISTS work_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(work_id) REFERENCES public_works(id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_work_comments_work ON work_comments(work_id, created_at);
                CREATE TABLE IF NOT EXISTS user_chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    sender_id INTEGER NOT NULL,
                    sender_name TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_chat_room ON user_chat_messages(room_id, created_at);
                """
            )
            cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "user_email" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN user_email TEXT")
            session_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "last_seen_at" not in session_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN last_seen_at REAL NOT NULL DEFAULT 0")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS device_presence (
                    device_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL DEFAULT 'web',
                    last_seen_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_users_user_email
                ON users(user_email) WHERE user_email IS NOT NULL AND user_email != ''
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS banned_subjects (
                    subject TEXT PRIMARY KEY,
                    reason TEXT NOT NULL DEFAULT '',
                    banned_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS device_registry (
                    fingerprint TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_limit_overrides (
                    subject TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    daily_limit REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(subject, feature)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coin_balance (
                    subject TEXT PRIMARY KEY,
                    balance REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS friend_requests (
                    from_user_id INTEGER NOT NULL,
                    to_user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    PRIMARY KEY(from_user_id, to_user_id),
                    FOREIGN KEY(from_user_id) REFERENCES users(id),
                    FOREIGN KEY(to_user_id) REFERENCES users(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS friendships (
                    user_id INTEGER NOT NULL,
                    friend_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(user_id, friend_id),
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(friend_id) REFERENCES users(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL DEFAULT '',
                    created_by INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(created_by) REFERENCES users(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_group_members (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    joined_at REAL NOT NULL,
                    PRIMARY KEY(group_id, user_id),
                    FOREIGN KEY(group_id) REFERENCES chat_groups(id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS work_coin_tips (
                    work_id INTEGER NOT NULL,
                    from_user_id INTEGER NOT NULL,
                    amount REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    confirm_after REAL NOT NULL,
                    confirmed_at REAL,
                    PRIMARY KEY(work_id, from_user_id),
                    FOREIGN KEY(work_id) REFERENCES public_works(id),
                    FOREIGN KEY(from_user_id) REFERENCES users(id)
                )
                """
            )
            act_cols = {r[1] for r in conn.execute("PRAGMA table_info(activity_log)").fetchall()}
            if "is_secret" not in act_cols:
                conn.execute("ALTER TABLE activity_log ADD COLUMN is_secret INTEGER NOT NULL DEFAULT 0")
            if "question" not in act_cols:
                conn.execute("ALTER TABLE activity_log ADD COLUMN question TEXT NOT NULL DEFAULT ''")
            if "answer" not in act_cols:
                conn.execute("ALTER TABLE activity_log ADD COLUMN answer TEXT NOT NULL DEFAULT ''")
            conn.commit()
        finally:
            conn.close()


_USER_COLS = "id, email, user_email, display_name, created_at"


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"{salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
        return secrets.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _require_login_id(login_id: str) -> str:
    value = login_id.strip().lower()
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise ValueError("아이디에는 이메일 형식(@)을 사용할 수 없습니다.")
    if not re.fullmatch(r"[a-z0-9_]{3,32}", value):
        raise ValueError("아이디는 3~32자 (영문·숫자·_)만 사용 가능합니다.")
    return value


def _resolve_login_ident(username: str) -> str:
    value = username.strip().lower()
    if "@" in value:
        return _require_email(value)
    return _require_login_id(value)


def _require_email(email: str) -> str:
    value = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise ValueError("실제 사용 가능한 이메일 주소를 입력해주세요.")
    return value


def _value_taken(conn: sqlite3.Connection, value: str) -> bool:
    row = conn.execute(
        "SELECT id FROM users WHERE email = ? OR user_email = ? LIMIT 1",
        (value, value),
    ).fetchone()
    return row is not None


def signup(login_id: str, user_email: str, password: str) -> dict[str, Any]:
    if len(password) < 8:
        raise ValueError("비밀번호는 8자 이상이어야 합니다.")
    ident_norm = _require_login_id(login_id)
    email_norm = _require_email(user_email)
    if ident_norm == email_norm:
        raise ValueError("아이디와 이메일은 같을 수 없습니다.")
    with _lock:
        conn = _connect()
        try:
            id_taken = _value_taken(conn, ident_norm)
            email_taken = _value_taken(conn, email_norm)
        finally:
            conn.close()
    if id_taken:
        raise ValueError("이미 사용 중인 아이디입니다. 다른 아이디를 사용하거나 로그인하세요.")
    if email_taken:
        raise ValueError("이미 가입된 이메일입니다. 다른 이메일을 사용하거나 로그인하세요.")
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO users (email, user_email, password_hash, display_name, created_at)
                VALUES (?, ?, ?, '', ?)
                """,
                (ident_norm, email_norm, _hash_password(password), now),
            )
            user_id = cur.lastrowid
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("이미 사용 중인 아이디 또는 이메일입니다.") from exc
        finally:
            conn.close()
    token = create_session(int(user_id))
    log_login_event(int(user_id), "signup")
    log_activity(ident_norm, "auth", user_id=int(user_id), action="signup", detail=email_norm[:64])
    return {"token": token, "user": get_user_by_id(int(user_id))}


def login(username: str, password: str) -> dict[str, Any]:
    ident_norm = _resolve_login_ident(username)
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                f"SELECT {_USER_COLS}, password_hash FROM users WHERE email = ?",
                (ident_norm,),
            ).fetchone()
        finally:
            conn.close()
    if not row or not _verify_password(password, row["password_hash"]):
        raise ValueError("아이디 또는 비밀번호가 올바르지 않습니다.")
    token = create_session(int(row["id"]))
    log_login_event(int(row["id"]), "login")
    log_activity(ident_norm, "auth", user_id=int(row["id"]), action="login")
    return {"token": token, "user": _row_to_user(row)}


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires_at = now + SESSION_TTL_SECONDS
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO sessions (token, user_id, expires_at, last_seen_at) VALUES (?, ?, ?, ?)",
                (token, user_id, expires_at, now),
            )
            conn.commit()
        finally:
            conn.close()
    return token


def logout(token: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()


def get_user_by_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT u.id, u.email, u.user_email, u.display_name, u.created_at, s.expires_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = ?
                """,
                (token.strip(),),
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] < time.time():
                conn.execute("DELETE FROM sessions WHERE token = ?", (token.strip(),))
                conn.commit()
                return None
            now = time.time()
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token = ?",
                (now, token.strip()),
            )
            conn.commit()
        finally:
            conn.close()
    return _row_to_user(row)


def get_user_by_id(user_id: int) -> dict[str, Any]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                f"SELECT {_USER_COLS} FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        raise ValueError("사용자를 찾을 수 없습니다.")
    return _row_to_user(row)


def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    ident = row["email"]
    user_email = ""
    try:
        user_email = (row["user_email"] or "").strip()
    except (IndexError, KeyError):
        pass
    return {
        "id": row["id"],
        "username": ident,
        "email": user_email,
        "created_at": row["created_at"],
    }


def get_user_data(user_id: int) -> dict[str, Any]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT data_key, payload FROM user_data WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        finally:
            conn.close()
    out: dict[str, Any] = {}
    for row in rows:
        try:
            out[row["data_key"]] = json.loads(row["payload"])
        except json.JSONDecodeError:
            out[row["data_key"]] = row["payload"]
    return out


def set_user_data(user_id: int, data_key: str, payload: Any) -> None:
    now = time.time()
    blob = json.dumps(payload, ensure_ascii=False)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO user_data (user_id, data_key, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, data_key) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (user_id, data_key, blob, now),
            )
            conn.commit()
        finally:
            conn.close()


def delete_user_data(user_id: int, data_key: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM user_data WHERE user_id = ? AND data_key = ?",
                (user_id, data_key),
            )
            conn.commit()
        finally:
            conn.close()


def merge_user_data(user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_user_data(user_id)
    skip_empty_scalar_keys = {"extension_prefs", "ai_presets", "ai_settings"}
    replace_list_keys = {"ai_presets"}
    for key, value in payload.items():
        if key in skip_empty_scalar_keys and isinstance(value, dict) and not value:
            continue
        if key == "ai_presets" and isinstance(value, list) and not value:
            continue
        if key == "saved_works" and isinstance(value, list):
            if not value and isinstance(current.get(key), list) and current[key]:
                continue
            current[key] = value[:100]
            continue
        if key == "session_history" and isinstance(value, list):
            current[key] = value[:50]
            continue
        if key in replace_list_keys and isinstance(value, list):
            current[key] = value
            continue
        if key == "active_collab" and value is None:
            current.pop("active_collab", None)
            delete_user_data(user_id, "active_collab")
            continue
        if key not in current:
            current[key] = value
            continue
        if isinstance(current[key], list) and isinstance(value, list):
            seen = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in current[key]}
            for item in value:
                sig = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if sig not in seen:
                    current[key].append(item)
                    seen.add(sig)
        else:
            current[key] = value
    for key, value in current.items():
        set_user_data(user_id, key, value)
    return current


def log_usage_event(subject: str, feature: str) -> None:
    now = time.time()
    try:
        with _lock:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO usage_events (subject, feature, created_at) VALUES (?, ?, ?)",
                    (subject, feature, now),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def log_login_event(user_id: int, event_type: str = "login") -> None:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO login_events (user_id, event_type, created_at) VALUES (?, ?, ?)",
                (user_id, event_type, now),
            )
            conn.commit()
        finally:
            conn.close()


def touch_device_presence(device_id: str, platform: str = "web") -> None:
    dev = (device_id or "").strip()[:128]
    if not dev:
        return
    now = time.time()
    plat = (platform or "web")[:32]
    try:
        with _lock:
            conn = _connect()
            try:
                conn.execute(
                    """
                    INSERT INTO device_presence (device_id, platform, last_seen_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(device_id) DO UPDATE SET
                        platform = excluded.platform,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (dev, plat, now),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def get_active_session_count() -> int:
    now = time.time()
    cutoff = now - PRESENCE_TTL_SECONDS
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE expires_at > ? AND last_seen_at > ?",
                (now, cutoff),
            ).fetchone()
        finally:
            conn.close()
    return int(row["c"]) if row else 0


def get_online_presence_stats() -> dict[str, int]:
    now = time.time()
    cutoff = now - PRESENCE_TTL_SECONDS
    with _lock:
        conn = _connect()
        try:
            online_users = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE expires_at > ? AND last_seen_at > ?",
                (now, cutoff),
            ).fetchone()["c"]
            online_guests = conn.execute(
                "SELECT COUNT(*) AS c FROM device_presence WHERE last_seen_at > ?",
                (cutoff,),
            ).fetchone()["c"]
        finally:
            conn.close()
    users = int(online_users or 0)
    guests = int(online_guests or 0)
    return {
        "online_users": users,
        "online_guests": guests,
        "online_total": users + guests,
    }


def get_usage_by_hour(day_key: str | None = None) -> dict[str, int]:
    day_key = day_key or _day_key()
    # KST day window
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT feature, CAST(strftime('%H', datetime(created_at, 'unixepoch', '+9 hours')) AS INTEGER) AS hr,
                       COUNT(*) AS cnt
                FROM usage_events
                WHERE strftime('%Y-%m-%d', datetime(created_at, 'unixepoch', '+9 hours')) = ?
                GROUP BY feature, hr
                """,
                (day_key,),
            ).fetchall()
        finally:
            conn.close()
    out: dict[str, int] = {}
    for r in rows:
        key = f"{r['feature']}:{r['hr']:02d}"
        out[key] = int(r["cnt"])
    return out


def get_usage_by_hour_by_feature(day_key: str | None = None) -> dict[str, dict[str, int]]:
    """Per-feature hourly usage for admin charts (KST)."""
    day_key = day_key or _day_key()
    out: dict[str, dict[str, int]] = {f: {} for f in QUOTA_LIMITS}
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT feature, CAST(strftime('%H', datetime(created_at, 'unixepoch', '+9 hours')) AS INTEGER) AS hr,
                       COUNT(*) AS cnt
                FROM usage_events
                WHERE strftime('%Y-%m-%d', datetime(created_at, 'unixepoch', '+9 hours')) = ?
                GROUP BY feature, hr
                """,
                (day_key,),
            ).fetchall()
        finally:
            conn.close()
    for r in rows:
        feat = str(r["feature"])
        if feat not in out:
            out[feat] = {}
        out[feat][f"{int(r['hr']):02d}"] = int(r["cnt"])
    return out


def get_usage_by_hour_all_time() -> dict[str, int]:
    """Aggregate AI usage by KST hour-of-day (0–23) across all history."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT feature, CAST(strftime('%H', datetime(created_at, 'unixepoch', '+9 hours')) AS INTEGER) AS hr,
                       COUNT(*) AS cnt
                FROM usage_events
                GROUP BY feature, hr
                """
            ).fetchall()
        finally:
            conn.close()
    out: dict[str, int] = {}
    for r in rows:
        key = f"{r['feature']}:{int(r['hr']):02d}"
        out[key] = int(r["cnt"])
    return out


def get_usage_by_hour_by_feature_all_time() -> dict[str, dict[str, int]]:
    """Per-feature hourly usage aggregated across all history (KST hour-of-day)."""
    out: dict[str, dict[str, int]] = {f: {} for f in QUOTA_LIMITS}
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT feature, CAST(strftime('%H', datetime(created_at, 'unixepoch', '+9 hours')) AS INTEGER) AS hr,
                       COUNT(*) AS cnt
                FROM usage_events
                GROUP BY feature, hr
                """
            ).fetchall()
        finally:
            conn.close()
    for r in rows:
        feat = str(r["feature"])
        if feat not in out:
            out[feat] = {}
        out[feat][f"{int(r['hr']):02d}"] = int(r["cnt"])
    return out


def get_login_by_hour(day_key: str | None = None) -> dict[str, int]:
    day_key = day_key or _day_key()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT CAST(strftime('%H', datetime(created_at, 'unixepoch', '+9 hours')) AS INTEGER) AS hr,
                       COUNT(*) AS cnt
                FROM login_events
                WHERE event_type = 'login'
                  AND strftime('%Y-%m-%d', datetime(created_at, 'unixepoch', '+9 hours')) = ?
                GROUP BY hr
                """,
                (day_key,),
            ).fetchall()
        finally:
            conn.close()
    return {f"{int(r['hr']):02d}": int(r["cnt"]) for r in rows}


def get_member_activity_by_hour(day_key: str | None = None) -> dict[str, int]:
    """Member session touches (last_seen) by KST hour — proxy for active usage times."""
    day_key = day_key or _day_key()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT CAST(strftime('%H', datetime(last_seen_at, 'unixepoch', '+9 hours')) AS INTEGER) AS hr,
                       COUNT(*) AS cnt
                FROM sessions
                WHERE strftime('%Y-%m-%d', datetime(last_seen_at, 'unixepoch', '+9 hours')) = ?
                GROUP BY hr
                """,
                (day_key,),
            ).fetchall()
        finally:
            conn.close()
    return {f"{int(r['hr']):02d}": int(r["cnt"]) for r in rows}


def get_agent_activity_log(limit: int = 500) -> list[dict[str, Any]]:
    return get_activity_log(feature="agent", limit=limit)


def get_user_login_stats(user_id: int) -> dict[str, Any]:
    now = time.time()
    cutoff = now - PRESENCE_TTL_SECONDS
    with _lock:
        conn = _connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM login_events WHERE user_id = ? AND event_type = 'login'",
                (user_id,),
            ).fetchone()["c"]
            last = conn.execute(
                "SELECT created_at FROM login_events WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            active = conn.execute(
                """
                SELECT COUNT(*) AS c FROM sessions s
                WHERE s.user_id = ? AND s.expires_at > ? AND s.last_seen_at > ?
                """,
                (user_id, now, cutoff),
            ).fetchone()["c"]
            seen = conn.execute(
                "SELECT MAX(last_seen_at) AS t FROM sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
    last_seen = float(seen["t"] or 0) if seen and seen["t"] else None
    return {
        "login_count": int(total),
        "last_login_at": last["created_at"] if last else None,
        "active_sessions": int(active),
        "last_seen_at": last_seen,
        "is_online": int(active) > 0,
    }


def _day_key() -> str:
    # KST midnight reset approximation via UTC+9
    kst = time.gmtime(time.time() + 9 * 3600)
    return f"{kst.tm_year:04d}-{kst.tm_mon:02d}-{kst.tm_mday:02d}"


def is_subject_banned(subject: str) -> bool:
    clean = (subject or "").strip()
    if not clean:
        return False
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM banned_subjects WHERE subject = ? LIMIT 1",
                (clean,),
            ).fetchone()
        finally:
            conn.close()
    return row is not None


def set_subject_ban(subject: str, banned: bool, reason: str = "") -> None:
    clean = (subject or "").strip()
    if not clean:
        raise ValueError("subject required")
    with _lock:
        conn = _connect()
        try:
            if banned:
                conn.execute(
                    """
                    INSERT INTO banned_subjects (subject, reason, banned_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(subject) DO UPDATE SET reason = excluded.reason, banned_at = excluded.banned_at
                    """,
                    (clean, (reason or "")[:500], time.time()),
                )
            else:
                conn.execute("DELETE FROM banned_subjects WHERE subject = ?", (clean,))
            conn.commit()
        finally:
            conn.close()


def resolve_device_id(fingerprint: str, proposed_id: str = "") -> str:
    fp = (fingerprint or "").strip()[:128]
    proposed = (proposed_id or "").strip()[:128]
    with _lock:
        conn = _connect()
        try:
            if fp:
                row = conn.execute(
                    "SELECT device_id FROM device_registry WHERE fingerprint = ?",
                    (fp,),
                ).fetchone()
                if row:
                    return str(row["device_id"])
            if proposed and proposed.startswith("dev_"):
                if fp:
                    try:
                        conn.execute(
                            "INSERT INTO device_registry (fingerprint, device_id, created_at) VALUES (?, ?, ?)",
                            (fp, proposed, time.time()),
                        )
                        conn.commit()
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        row = conn.execute(
                            "SELECT device_id FROM device_registry WHERE fingerprint = ?",
                            (fp,),
                        ).fetchone()
                        if row:
                            return str(row["device_id"])
                return proposed
            new_id = "dev_" + secrets.token_urlsafe(12)
            if fp:
                conn.execute(
                    "INSERT INTO device_registry (fingerprint, device_id, created_at) VALUES (?, ?, ?)",
                    (fp, new_id, time.time()),
                )
                conn.commit()
            return new_id
        finally:
            conn.close()


def _initial_coins_for_subject(subject: str) -> float:
    return MEMBER_INITIAL_COINS if (subject or "").startswith("user:") else GUEST_INITIAL_COINS


def ensure_coin_balance(subject: str) -> float:
    clean = (subject or "").strip()
    if not clean:
        return 0.0
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT balance FROM coin_balance WHERE subject = ?",
                (clean,),
            ).fetchone()
            if row is not None:
                return float(row["balance"])
            initial = _initial_coins_for_subject(clean)
            now = time.time()
            conn.execute(
                "INSERT INTO coin_balance (subject, balance, updated_at) VALUES (?, ?, ?)",
                (clean, initial, now),
            )
            conn.commit()
            return initial
        finally:
            conn.close()


def get_coin_balance(subject: str) -> float:
    return ensure_coin_balance(subject)


def adjust_coin_balance(subject: str, delta: float) -> dict[str, Any]:
    clean = (subject or "").strip()
    if not clean:
        raise ValueError("invalid subject")
    ensure_coin_balance(clean)
    delta_val = float(delta)
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT balance FROM coin_balance WHERE subject = ?",
                (clean,),
            ).fetchone()
            bal = float(row["balance"]) if row else 0.0
            new_bal = max(0.0, bal + delta_val)
            conn.execute(
                "UPDATE coin_balance SET balance = ?, updated_at = ? WHERE subject = ?",
                (new_bal, time.time(), clean),
            )
            conn.commit()
        finally:
            conn.close()
    return {"subject": clean, "balance": round(new_bal, 2), "delta": round(delta_val, 2)}


def refund_coins(subject: str, amount: float, feature: str = "") -> dict[str, Any]:
    amt = max(0.0, float(amount))
    if amt <= 0:
        return {"subject": subject, "balance": get_coin_balance(subject), "refunded": 0}
    result = adjust_coin_balance(subject, amt)
    day_key = _day_key()
    feat = (feature or "_refund").strip() or "_refund"
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT count FROM quota_usage WHERE subject = ? AND feature = ? AND day_key = ?",
                (subject, feat, day_key),
            ).fetchone()
            used = float(row["count"]) if row else 0.0
            new_used = max(0.0, used - amt)
            conn.execute(
                """
                INSERT INTO quota_usage (subject, feature, day_key, count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(subject, feature, day_key) DO UPDATE SET count = excluded.count
                """,
                (subject, feat, day_key, new_used),
            )
            conn.commit()
        finally:
            conn.close()
    result["refunded"] = amt
    return result


def admin_set_coin_balance(subject: str, balance: float) -> dict[str, Any]:
    clean = (subject or "").strip()
    if not clean:
        raise ValueError("invalid subject")
    ensure_coin_balance(clean)
    new_bal = max(0.0, float(balance))
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE coin_balance SET balance = ?, updated_at = ? WHERE subject = ?",
                (new_bal, time.time(), clean),
            )
            conn.commit()
        finally:
            conn.close()
    return {"subject": clean, "balance": round(new_bal, 2)}


def admin_adjust_quota(subject: str, feature: str, delta: float) -> dict[str, Any]:
    """Admin: add (+) or subtract (-) coins from persistent balance."""
    clean_subject = (subject or "").strip()
    if not clean_subject:
        raise ValueError("invalid subject")
    result = adjust_coin_balance(clean_subject, float(delta))
    snap = get_quota_snapshot(clean_subject)
    return {
        "subject": clean_subject,
        "feature": feature or POOL_FEATURE,
        "balance": result["balance"],
        "delta": result["delta"],
        "remaining": result["balance"],
        "limit": result["balance"],
        "used": snap.get("total", {}).get("used", 0),
    }


def admin_set_quota_limit(subject: str, feature: str, daily_limit: float) -> dict[str, Any]:
    clean_subject = (subject or "").strip()
    feat = (feature or "").strip()
    if not clean_subject or feat not in QUOTA_ADMIN_FEATURES:
        raise ValueError("invalid subject or feature")
    limit_val = max(0.0, float(daily_limit))
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO quota_limit_overrides (subject, feature, daily_limit, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(subject, feature) DO UPDATE SET
                    daily_limit = excluded.daily_limit,
                    updated_at = excluded.updated_at
                """,
                (clean_subject, feat, limit_val, now),
            )
            conn.commit()
        finally:
            conn.close()
    snap = get_quota_snapshot(clean_subject)
    if feat == POOL_FEATURE:
        total_snap = snap.get("total", {})
        return {
            "subject": clean_subject,
            "feature": feat,
            "limit": limit_val,
            "used": total_snap.get("used", 0),
            "remaining": total_snap.get("remaining", limit_val),
            "day_key": snap.get("day_key"),
        }
    feat_snap = snap.get("features", {}).get(feat, {})
    return {
        "subject": clean_subject,
        "feature": feat,
        "limit": limit_val,
        "used": feat_snap.get("used", 0),
        "remaining": feat_snap.get("remaining", limit_val),
        "day_key": snap.get("day_key"),
    }


def check_and_consume_quota(
    subject: str,
    feature: str,
    amount: float = 1.0,
) -> tuple[bool, dict[str, Any]]:
    amt = max(0.0, float(amount))
    if is_subject_banned(subject):
        balance = get_coin_balance(subject)
        return False, {
            "feature": feature,
            "used": 0,
            "limit": balance,
            "remaining": balance,
            "balance": balance,
            "day_key": _day_key(),
            "banned": True,
            "guest": is_guest_subject(subject),
        }
    balance = get_coin_balance(subject)
    if balance + 1e-9 < amt:
        return False, {
            "feature": feature,
            "used": 0,
            "limit": balance,
            "remaining": balance,
            "balance": balance,
            "day_key": _day_key(),
            "guest": is_guest_subject(subject),
            "insufficient": True,
        }
    adjust_coin_balance(subject, -amt)
    day_key = _day_key()
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT count FROM quota_usage WHERE subject = ? AND feature = ? AND day_key = ?",
                (subject, feature, day_key),
            ).fetchone()
            used = float(row["count"]) if row else 0.0
            conn.execute(
                """
                INSERT INTO quota_usage (subject, feature, day_key, count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(subject, feature, day_key) DO UPDATE SET count = count + ?
                """,
                (subject, feature, day_key, amt, amt),
            )
            try:
                conn.execute(
                    "INSERT INTO usage_events (subject, feature, created_at) VALUES (?, ?, ?)",
                    (subject, feature, now),
                )
            except Exception:
                pass
            conn.commit()
            new_feature_used = used + amt
        finally:
            conn.close()
    new_balance = get_coin_balance(subject)
    return True, {
        "feature": feature,
        "used": new_feature_used,
        "limit": new_balance,
        "remaining": new_balance,
        "balance": new_balance,
        "total_used": new_feature_used,
        "day_key": day_key,
        "guest": is_guest_subject(subject),
    }


def get_quota_snapshot(subject: str) -> dict[str, Any]:
    day_key = _day_key()
    balance = get_coin_balance(subject)
    banned = is_subject_banned(subject)
    total_used_today = 0.0
    out: dict[str, Any] = {}
    with _lock:
        conn = _connect()
        try:
            total_used_today = _total_coins_used(conn, subject, day_key)
            for feature in MEMBER_QUOTA_LIMITS:
                row = conn.execute(
                    "SELECT count FROM quota_usage WHERE subject = ? AND feature = ? AND day_key = ?",
                    (subject, feature, day_key),
                ).fetchone()
                used = float(row["count"]) if row else 0.0
                out[feature] = {
                    "used": round(used, 2),
                    "limit": balance,
                    "remaining": balance,
                }
        finally:
            conn.close()
    total = {
        "used": round(total_used_today, 2),
        "limit": balance,
        "remaining": balance,
        "balance": balance,
    }
    return {
        "day_key": day_key,
        "features": out,
        "coins": out,
        "total": total,
        "balance": balance,
        "guest": is_guest_subject(subject),
        "banned": banned,
        "limits": quota_limits_for_subject(subject),
        "pool_limit": balance,
    }


def create_public_share(kind: str, title: str, payload: Any) -> str:
    share_id = secrets.token_urlsafe(9)
    now = time.time()
    blob = json.dumps(payload, ensure_ascii=False)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO share_links (id, kind, title, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (share_id, kind, title.strip()[:200], blob, now),
            )
            conn.commit()
        finally:
            conn.close()
    return share_id


def get_public_share(share_id: str) -> dict[str, Any] | None:
    clean = (share_id or "").strip()
    if not clean or len(clean) > 64:
        return None
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, kind, title, payload, created_at FROM share_links WHERE id = ?",
                (clean,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        payload = row["payload"]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "payload": payload,
        "created_at": row["created_at"],
    }


ADMIN_SESSION_TTL_SECONDS = 60 * 60 * 8


def verify_admin_password(password: str) -> bool:
    expected = os.getenv("ADMIN_PASSWORD", "050907").strip()
    if not expected:
        return False
    return secrets.compare_digest(str(password), str(expected))


def create_admin_session() -> str:
    token = "adm_" + secrets.token_urlsafe(28)
    expires_at = time.time() + ADMIN_SESSION_TTL_SECONDS
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO admin_sessions (token, expires_at) VALUES (?, ?)",
                (token, expires_at),
            )
            conn.commit()
        finally:
            conn.close()
    return token


def verify_admin_token(token: str | None) -> bool:
    if not token or not token.startswith("adm_"):
        return False
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT expires_at FROM admin_sessions WHERE token = ?",
                (token.strip(),),
            ).fetchone()
            if not row:
                return False
            if row["expires_at"] < time.time():
                conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token.strip(),))
                conn.commit()
                return False
        finally:
            conn.close()
    return True


def admin_logout(token: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token.strip(),))
            conn.commit()
        finally:
            conn.close()


def list_users_admin() -> list[dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                f"SELECT {_USER_COLS} FROM users ORDER BY id DESC"
            ).fetchall()
        finally:
            conn.close()
    return [_row_to_user(r) for r in rows]


def get_user_quota_totals(user_id: int) -> dict[str, float]:
    prefix = f"user:{user_id}"
    day_key = _day_key()
    totals: dict[str, float] = {}
    with _lock:
        conn = _connect()
        try:
            for feature in MEMBER_QUOTA_LIMITS:
                row = conn.execute(
                    "SELECT count FROM quota_usage WHERE subject = ? AND feature = ? AND day_key = ?",
                    (prefix, feature, day_key),
                ).fetchone()
                totals[feature] = float(row["count"]) if row else 0.0
            all_time = conn.execute(
                "SELECT feature, SUM(count) AS total FROM quota_usage WHERE subject = ? GROUP BY feature",
                (prefix,),
            ).fetchall()
        finally:
            conn.close()
    totals["_all_time"] = {r["feature"]: float(r["total"]) for r in all_time}
    return totals


def get_admin_dashboard() -> dict[str, Any]:
    users = list_users_admin()
    day_key = _day_key()
    enriched = []
    for u in users:
        uid = int(u["id"])
        quotas = get_user_quota_totals(uid)
        data = get_user_data(uid)
        login_stats = get_user_login_stats(uid)
        collab_info = data.get("active_collab") or {}
        enriched.append({
            **u,
            "created_at_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(u["created_at"])),
            "quota_today": {k: round(float(quotas.get(k, 0)), 2) for k in MEMBER_QUOTA_LIMITS},
            "quota_all_time": quotas.get("_all_time", {}),
            "saved_results_count": len(data.get("saved_works") or []),
            "has_active_collab": bool(collab_info),
            "collab_task": (collab_info.get("task") or "")[:80] if collab_info else "",
            "collab_work_type": collab_info.get("plan", {}).get("work_type", "") if collab_info else "",
            "collab_stage_done": sum(
                1 for s in (collab_info.get("stageStates") or []) if s == "done"
            ) if collab_info else 0,
            "login_count": login_stats["login_count"],
            "last_login_iso": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(login_stats["last_login_at"])
            ) if login_stats.get("last_login_at") else "-",
            "last_seen_iso": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(login_stats["last_seen_at"])
            ) if login_stats.get("last_seen_at") else "-",
            "active_sessions": login_stats["active_sessions"],
            "is_online": login_stats.get("is_online", login_stats["active_sessions"] > 0),
        })
    with _lock:
        conn = _connect()
        try:
            total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            today_usage = conn.execute(
                "SELECT feature, SUM(count) AS total FROM quota_usage WHERE day_key = ? GROUP BY feature",
                (day_key,),
            ).fetchall()
            share_count = conn.execute("SELECT COUNT(*) AS c FROM share_links").fetchone()["c"]
        finally:
            conn.close()
    return {
        "day_key": day_key,
        "total_users": int(total_users),
        "active_sessions": get_active_session_count(),
        **get_online_presence_stats(),
        "share_links": int(share_count),
        "usage_today": {r["feature"]: round(float(r["total"]), 2) for r in today_usage},
        "usage_all_time": get_usage_totals_all_time(),
        "usage_today_total": round(
            sum(float(r["total"]) for r in today_usage), 2
        ) if today_usage else 0.0,
        "usage_all_time_total": _sum_usage_map(get_usage_totals_all_time()),
        "usage_today_member": get_usage_by_subject_prefix("user:", day_key),
        "usage_today_guest": get_usage_by_subject_prefix("device:", day_key),
        "usage_all_time_member": get_usage_by_subject_prefix("user:", None),
        "usage_all_time_guest": get_usage_by_subject_prefix("device:", None),
        "usage_today_member_total": _sum_usage_map(get_usage_by_subject_prefix("user:", day_key)),
        "usage_today_guest_total": _sum_usage_map(get_usage_by_subject_prefix("device:", day_key)),
        "usage_all_time_member_total": _sum_usage_map(get_usage_by_subject_prefix("user:", None)),
        "usage_all_time_guest_total": _sum_usage_map(get_usage_by_subject_prefix("device:", None)),
        "usage_by_hour": get_usage_by_hour(day_key),
        "usage_by_hour_by_feature": get_usage_by_hour_by_feature(day_key),
        "usage_by_hour_all_time": get_usage_by_hour_all_time(),
        "usage_by_hour_by_feature_all_time": get_usage_by_hour_by_feature_all_time(),
        "login_by_hour": get_login_by_hour(day_key),
        "member_activity_by_hour": get_member_activity_by_hour(day_key),
        "agent_activity": get_agent_activity_log(limit=500),
        "quota_limits": {
            "member": dict(MEMBER_QUOTA_LIMITS),
            "guest": dict(GUEST_QUOTA_LIMITS),
            "pool_member": MEMBER_DAILY_COINS,
            "pool_guest": GUEST_DAILY_COINS,
        },
        "users": enriched,
        "recent_activity": get_activity_log(limit=10000),
        "open_support_count": count_open_support(),
        "platform_stats": get_platform_stats(day_key),
        "platform_stats_detailed": get_platform_stats_detailed(day_key),
        "member_activity_by_feature": get_member_activity_by_feature(day_key),
        "member_activity_all_time": get_member_activity_by_feature_all_time(),
        "guest_activity_by_feature": get_guest_activity_by_feature(day_key),
        "guest_activity_all_time": get_guest_activity_by_feature_all_time(),
    }


SECRET_ACTIVITY_LABEL = "🔒 시크릿 (프라이버시 모드)"


def get_admin_setting(key: str, default: str = "") -> str:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT value FROM admin_settings WHERE key = ?",
                (key[:64],),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return default
    return str(row["value"] or default)


def set_admin_setting(key: str, value: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO admin_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key[:64], str(value)[:500]),
            )
            conn.commit()
        finally:
            conn.close()


def get_activity_retention_days() -> int:
    raw = get_admin_setting("activity_retention_days", "0").strip()
    try:
        days = int(raw)
    except ValueError:
        days = 0
    return max(0, min(3650, days))


def purge_expired_activity() -> int:
    days = get_activity_retention_days()
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "DELETE FROM activity_log WHERE created_at < ?",
                (cutoff,),
            )
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()


def log_activity(
    subject: str,
    feature: str,
    *,
    user_id: int | None = None,
    device_id: str = "",
    platform: str = "web",
    action: str = "",
    detail: str = "",
    is_secret: bool = False,
    question: str = "",
    answer: str = "",
) -> int | None:
    now = time.time()
    secret = 1 if is_secret else 0
    q_store = (question or "")[:4000]
    a_store = (answer or "")[:12000]
    d_store = (detail or "")[:500]
    if secret:
        q_store = ""
        a_store = ""
        if not d_store or d_store == q_store[:500]:
            d_store = SECRET_ACTIVITY_LABEL
    try:
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute(
                    """
                    INSERT INTO activity_log
                    (subject, user_id, device_id, platform, feature, action, detail,
                     is_secret, question, answer, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        subject[:128],
                        user_id,
                        (device_id or "")[:128],
                        (platform or "web")[:32],
                        feature[:32],
                        (action or feature)[:64],
                        d_store,
                        secret,
                        q_store,
                        a_store,
                        now,
                    ),
                )
                conn.commit()
                row_id = int(cur.lastrowid or 0) or None
            finally:
                conn.close()
        purge_expired_activity()
        return row_id
    except Exception:
        return None


def log_user_activity_detail(
    subject: str,
    feature: str,
    *,
    user_id: int | None = None,
    device_id: str = "",
    platform: str = "web",
    action: str = "",
    question: str = "",
    answer: str = "",
    is_secret: bool = False,
) -> int | None:
    preview = (question or action or feature)[:120]
    if is_secret:
        preview = SECRET_ACTIVITY_LABEL
    return log_activity(
        subject,
        feature,
        user_id=user_id,
        device_id=device_id,
        platform=platform,
        action=action or feature,
        detail=preview,
        is_secret=is_secret,
        question=question,
        answer=answer,
    )


def get_activity_log(
    user_id: int | None = None,
    device_id: str | None = None,
    *,
    feature: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(50000, limit))
    offset = max(0, offset)
    conditions: list[str] = []
    params: list[Any] = []
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    elif device_id:
        conditions.append("device_id = ?")
        params.append(device_id.strip())
    if feature:
        conditions.append("feature = ?")
        params.append(feature.strip()[:32])
    if q:
        like = f"%{q.strip()[:80]}%"
        conditions.append("(detail LIKE ? OR action LIKE ? OR subject LIKE ? OR device_id LIKE ?)")
        params.extend([like, like, like, like])
    where = " AND ".join(conditions) if conditions else "1=1"
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                f"""
                SELECT * FROM activity_log
                WHERE {where}
                ORDER BY created_at DESC LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        finally:
            conn.close()
    return [_activity_row(r, admin_view=True) for r in rows]


def count_activity_log(
    user_id: int | None = None,
    device_id: str | None = None,
    *,
    feature: str | None = None,
    q: str | None = None,
) -> int:
    conditions: list[str] = []
    params: list[Any] = []
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    elif device_id:
        conditions.append("device_id = ?")
        params.append(device_id.strip())
    if feature:
        conditions.append("feature = ?")
        params.append(feature.strip()[:32])
    if q:
        like = f"%{q.strip()[:80]}%"
        conditions.append("(detail LIKE ? OR action LIKE ? OR subject LIKE ? OR device_id LIKE ?)")
        params.extend([like, like, like, like])
    where = " AND ".join(conditions) if conditions else "1=1"
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM activity_log WHERE {where}",
                tuple(params),
            ).fetchone()
        finally:
            conn.close()
    return int(row["c"] or 0)


def _activity_row(row: sqlite3.Row, *, admin_view: bool = True) -> dict[str, Any]:
    keys = row.keys()
    is_secret = bool(row["is_secret"]) if "is_secret" in keys else False
    question = str(row["question"] or "") if "question" in keys else ""
    answer = str(row["answer"] or "") if "answer" in keys else ""
    detail = str(row["detail"] or "")
    if admin_view and is_secret:
        question = ""
        answer = ""
        detail = SECRET_ACTIVITY_LABEL
    return {
        "id": row["id"],
        "subject": row["subject"],
        "user_id": row["user_id"],
        "device_id": row["device_id"],
        "platform": row["platform"],
        "feature": row["feature"],
        "action": row["action"],
        "detail": detail,
        "is_secret": is_secret,
        "question": question,
        "answer": answer,
        "created_at": row["created_at"],
        "created_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["created_at"])),
    }


def get_activity_by_id(activity_id: int) -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM activity_log WHERE id = ?",
                (int(activity_id),),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    return _activity_row(row, admin_view=True)


def delete_activity_records(
    *,
    ids: list[int] | None = None,
    delete_all: bool = False,
) -> int:
    with _lock:
        conn = _connect()
        try:
            if delete_all:
                cur = conn.execute("DELETE FROM activity_log")
            elif ids:
                placeholders = ",".join("?" * len(ids))
                cur = conn.execute(
                    f"DELETE FROM activity_log WHERE id IN ({placeholders})",
                    [int(i) for i in ids],
                )
            else:
                return 0
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()


def get_platform_stats(day_key: str | None = None) -> dict[str, int]:
    detailed = get_platform_stats_detailed(day_key)
    return {k: int(v.get("events", 0)) for k, v in detailed.items()}


def get_platform_stats_detailed(day_key: str | None = None) -> dict[str, dict[str, int]]:
    day_key = day_key or _day_key()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT platform,
                       COUNT(*) AS events,
                       COUNT(DISTINCT user_id) AS member_accounts,
                       COUNT(DISTINCT CASE WHEN user_id IS NULL AND device_id != '' THEN device_id END) AS guest_devices
                FROM activity_log
                WHERE strftime('%Y-%m-%d', datetime(created_at, 'unixepoch', '+9 hours')) = ?
                GROUP BY platform
                """,
                (day_key,),
            ).fetchall()
        finally:
            conn.close()
    return {
        str(r["platform"] or "web"): {
            "events": int(r["events"]),
            "member_accounts": int(r["member_accounts"] or 0),
            "guest_devices": int(r["guest_devices"] or 0),
        }
        for r in rows
    }


def _sum_usage_map(values: dict[str, float] | dict[str, int]) -> float:
    return round(sum(float(v) for v in (values or {}).values()), 2)


def get_guest_activity_by_feature(day_key: str | None = None) -> dict[str, int]:
    day_key = day_key or _day_key()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT feature, COUNT(*) AS cnt FROM activity_log
                WHERE user_id IS NULL
                  AND strftime('%Y-%m-%d', datetime(created_at, 'unixepoch', '+9 hours')) = ?
                GROUP BY feature
                """,
                (day_key,),
            ).fetchall()
        finally:
            conn.close()
    return {r["feature"]: int(r["cnt"]) for r in rows}


def get_member_activity_by_feature(day_key: str | None = None) -> dict[str, int]:
    day_key = day_key or _day_key()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT feature, COUNT(*) AS cnt FROM activity_log
                WHERE user_id IS NOT NULL
                  AND strftime('%Y-%m-%d', datetime(created_at, 'unixepoch', '+9 hours')) = ?
                GROUP BY feature
                """,
                (day_key,),
            ).fetchall()
        finally:
            conn.close()
    return {r["feature"]: int(r["cnt"]) for r in rows}


def get_usage_totals_all_time() -> dict[str, float]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT feature, SUM(count) AS total FROM quota_usage GROUP BY feature",
            ).fetchall()
        finally:
            conn.close()
    return {r["feature"]: round(float(r["total"]), 2) for r in rows}


def get_usage_by_subject_prefix(
    prefix: str,
    day_key: str | None = None,
) -> dict[str, float]:
    day_key = day_key or _day_key()
    like = f"{prefix}%"
    with _lock:
        conn = _connect()
        try:
            if day_key:
                rows = conn.execute(
                    """
                    SELECT feature, SUM(count) AS total FROM quota_usage
                    WHERE day_key = ? AND subject LIKE ?
                    GROUP BY feature
                    """,
                    (day_key, like),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT feature, SUM(count) AS total FROM quota_usage
                    WHERE subject LIKE ?
                    GROUP BY feature
                    """,
                    (like,),
                ).fetchall()
        finally:
            conn.close()
    return {r["feature"]: round(float(r["total"]), 2) for r in rows}


def get_member_activity_by_feature_all_time() -> dict[str, int]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT feature, COUNT(*) AS cnt FROM activity_log
                WHERE user_id IS NOT NULL
                GROUP BY feature
                """,
            ).fetchall()
        finally:
            conn.close()
    return {r["feature"]: int(r["cnt"]) for r in rows}


def get_guest_activity_by_feature_all_time() -> dict[str, int]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT feature, COUNT(*) AS cnt FROM activity_log
                WHERE user_id IS NULL
                GROUP BY feature
                """,
            ).fetchall()
        finally:
            conn.close()
    return {r["feature"]: int(r["cnt"]) for r in rows}


def list_guest_devices(limit: int = 50) -> list[dict[str, Any]]:
    now = time.time()
    cutoff = now - PRESENCE_TTL_SECONDS
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT device_id, platform, MAX(created_at) AS last_at, COUNT(*) AS cnt
                FROM activity_log
                WHERE user_id IS NULL AND device_id != ''
                GROUP BY device_id
                ORDER BY last_at DESC LIMIT ?
                """,
                (max(1, min(200, limit)),),
            ).fetchall()
            presence_rows = conn.execute(
                "SELECT device_id, platform, last_seen_at FROM device_presence"
            ).fetchall()
        finally:
            conn.close()
    presence_map = {r["device_id"]: r for r in presence_rows}
    out = []
    for r in rows:
        pres = presence_map.get(r["device_id"])
        last_seen = float(pres["last_seen_at"]) if pres else float(r["last_at"])
        plat = pres["platform"] if pres else r["platform"]
        subject = f"device:{r['device_id']}"
        snap = get_quota_snapshot(subject)
        out.append({
            "device_id": r["device_id"],
            "platform": plat,
            "subject": subject,
            "last_at": float(r["last_at"]),
            "last_at_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_at"])),
            "activity_count": int(r["cnt"]),
            "last_seen_at": last_seen,
            "last_seen_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(last_seen)),
            "is_online": last_seen > cutoff,
            "banned": snap.get("banned", False),
            "quota_today": {k: v.get("used", 0) for k, v in snap.get("features", {}).items()},
            "quota_limits": snap.get("limits", GUEST_QUOTA_LIMITS),
        })
    return out


def search_users_admin(query: str, limit: int = 30) -> list[dict[str, Any]]:
    q = (query or "").strip().lower()
    users = list_users_admin()
    if not q:
        return users[:limit]
    out = []
    for u in users:
        hay = f"{u.get('username','')} {u.get('email','')}".lower()
        if q in hay:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def create_support_inquiry(
    message: str,
    *,
    user_id: int | None = None,
    device_id: str = "",
    platform: str = "web",
    username: str = "",
) -> dict[str, Any]:
    msg = message.strip()
    if not msg:
        raise ValueError("문의 내용을 입력해주세요.")
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO support_inquiries
                (user_id, device_id, platform, username, message, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (user_id, device_id[:128], platform[:32], username[:64], msg[:2000], now, now),
            )
            iid = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
    return {"id": int(iid), "status": "open"}


def count_open_support() -> int:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM support_inquiries WHERE status = 'open'"
            ).fetchone()
        finally:
            conn.close()
    return int(row["c"]) if row else 0


def list_support_inquiries(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(200, limit))
    with _lock:
        conn = _connect()
        try:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM support_inquiries WHERE status = ?
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM support_inquiries ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        finally:
            conn.close()
    return [_support_row(r) for r in rows]


def _support_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "device_id": row["device_id"],
        "platform": row["platform"],
        "username": row["username"],
        "message": row["message"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "created_at_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(row["created_at"])),
    }


def get_support_thread(inquiry_id: int) -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM support_inquiries WHERE id = ?", (inquiry_id,)
            ).fetchone()
            if not row:
                return None
            replies = conn.execute(
                "SELECT * FROM support_replies WHERE inquiry_id = ? ORDER BY created_at",
                (inquiry_id,),
            ).fetchall()
        finally:
            conn.close()
    return {
        **_support_row(row),
        "replies": [
            {
                "id": r["id"],
                "from_admin": bool(r["from_admin"]),
                "message": r["message"],
                "created_at_iso": time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(r["created_at"])
                ),
            }
            for r in replies
        ],
    }


def add_support_reply(inquiry_id: int, message: str, from_admin: bool = True) -> None:
    msg = message.strip()
    if not msg:
        raise ValueError("답변 내용이 비어 있습니다.")
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO support_replies (inquiry_id, from_admin, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (inquiry_id, 1 if from_admin else 0, msg[:2000], now),
            )
            status = "answered" if from_admin else "open"
            conn.execute(
                "UPDATE support_inquiries SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, inquiry_id),
            )
            conn.commit()
        finally:
            conn.close()


def list_user_support_inquiries(user_id: int, limit: int = 30) -> list[dict[str, Any]]:
    limit = max(1, min(100, limit))
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM support_inquiries WHERE user_id = ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
            out = []
            for row in rows:
                item = _support_row(row)
                replies = conn.execute(
                    "SELECT * FROM support_replies WHERE inquiry_id = ? ORDER BY created_at",
                    (row["id"],),
                ).fetchall()
                item["replies"] = [
                    {
                        "id": r["id"],
                        "from_admin": bool(r["from_admin"]),
                        "message": r["message"],
                        "created_at_iso": time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime(r["created_at"])
                        ),
                    }
                    for r in replies
                ]
                out.append(item)
        finally:
            conn.close()
    return out


def delete_support_inquiry(inquiry_id: int, user_id: int) -> None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT user_id FROM support_inquiries WHERE id = ?", (inquiry_id,)
            ).fetchone()
            if not row:
                raise ValueError("문의를 찾을 수 없습니다.")
            if int(row["user_id"] or 0) != int(user_id):
                raise ValueError("삭제 권한이 없습니다.")
            conn.execute("DELETE FROM support_replies WHERE inquiry_id = ?", (inquiry_id,))
            conn.execute("DELETE FROM support_inquiries WHERE id = ?", (inquiry_id,))
            conn.commit()
        finally:
            conn.close()


def delete_support_inquiry_admin(inquiry_id: int) -> None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id FROM support_inquiries WHERE id = ?", (inquiry_id,)
            ).fetchone()
            if not row:
                raise ValueError("문의를 찾을 수 없습니다.")
            conn.execute("DELETE FROM support_replies WHERE inquiry_id = ?", (inquiry_id,))
            conn.execute("DELETE FROM support_inquiries WHERE id = ?", (inquiry_id,))
            conn.commit()
        finally:
            conn.close()


def get_user_admin_detail(user_id: int) -> dict[str, Any]:
    user = get_user_by_id(user_id)
    data = get_user_data(user_id)
    login_stats = get_user_login_stats(user_id)
    quotas = get_user_quota_totals(user_id)
    activity = get_activity_log(user_id=user_id, limit=10000)
    platform_counts: dict[str, int] = {}
    for a in activity:
        platform_counts[a["platform"]] = platform_counts.get(a["platform"], 0) + 1
    inquiries = []
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id, message, status, created_at FROM support_inquiries WHERE user_id = ? ORDER BY created_at DESC LIMIT 20",
                (user_id,),
            ).fetchall()
        finally:
            conn.close()
    for r in rows:
        inquiries.append({
            "id": r["id"],
            "message": r["message"][:120],
            "status": r["status"],
            "created_at_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"])),
        })
    subject = f"user:{user_id}"
    snap = get_quota_snapshot(subject)
    pool_limit = daily_pool_limit(subject)
    return {
        **user,
        "subject": subject,
        "banned": snap.get("banned", False),
        "login_stats": login_stats,
        "is_online": login_stats.get("is_online", False),
        "last_seen_iso": time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(login_stats["last_seen_at"])
        ) if login_stats.get("last_seen_at") else "-",
        "last_login_iso": time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(login_stats["last_login_at"])
        ) if login_stats.get("last_login_at") else "-",
        "quota_today": {k: round(float(quotas.get(k, 0)), 2) for k in MEMBER_QUOTA_LIMITS},
        "quota_today_total": round(sum(float(quotas.get(k, 0)) for k in MEMBER_QUOTA_LIMITS), 2),
        "quota_snapshot": snap,
        "quota_limits": {"_pool": pool_limit, **snap.get("limits", MEMBER_QUOTA_LIMITS)},
        "pool_limit": pool_limit,
        "quota_all_time": quotas.get("_all_time", {}),
        "saved_results_count": len(data.get("saved_works") or []),
        "activity": activity,
        "platform_counts": platform_counts,
        "support_inquiries": inquiries,
    }


def _chat_room_id(user_a: int, user_b: int) -> str:
    lo, hi = sorted((int(user_a), int(user_b)))
    return f"{lo}:{hi}"


def list_public_works(limit: int = 50, viewer_id: int | None = None, friends_only: bool = False) -> list[dict[str, Any]]:
    confirm_pending_work_tips()
    limit = max(1, min(int(limit or 50), 100))
    with _lock:
        conn = _connect()
        try:
            if friends_only and viewer_id is not None:
                rows = conn.execute(
                    """
                    SELECT w.id, w.user_id, w.author_name, w.title, w.body, w.created_at,
                           (SELECT COALESCE(SUM(t.amount), 0) FROM work_coin_tips t
                            WHERE t.work_id = w.id AND t.status = 'confirmed') AS coin_total,
                           (SELECT COUNT(*) FROM work_comments wc WHERE wc.work_id = w.id) AS comment_count
                    FROM public_works w
                    INNER JOIN friendships f ON f.friend_id = w.user_id AND f.user_id = ?
                    ORDER BY w.created_at DESC
                    LIMIT ?
                    """,
                    (viewer_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT w.id, w.user_id, w.author_name, w.title, w.body, w.created_at,
                           (SELECT COALESCE(SUM(t.amount), 0) FROM work_coin_tips t
                            WHERE t.work_id = w.id AND t.status = 'confirmed') AS coin_total,
                           (SELECT COUNT(*) FROM work_comments wc WHERE wc.work_id = w.id) AS comment_count
                    FROM public_works w
                    ORDER BY w.created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            tip_map: dict[int, dict] = {}
            if viewer_id is not None:
                tip_rows = conn.execute(
                    """
                    SELECT work_id, status, confirm_after, amount
                    FROM work_coin_tips WHERE from_user_id = ?
                    """,
                    (viewer_id,),
                ).fetchall()
                for tr in tip_rows:
                    tip_map[int(tr["work_id"])] = {
                        "status": tr["status"],
                        "confirm_after": tr["confirm_after"],
                        "amount": tr["amount"],
                    }
        finally:
            conn.close()
    out: list[dict[str, Any]] = []
    now = time.time()
    for r in rows:
        wid = int(r["id"])
        tip = tip_map.get(wid) if viewer_id is not None else None
        pending = tip and tip["status"] == "pending" and now < float(tip["confirm_after"])
        out.append({
            "id": wid,
            "user_id": r["user_id"],
            "author_name": r["author_name"],
            "title": r["title"],
            "body": r["body"],
            "created_at": r["created_at"],
            "created_at_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"])),
            "coin_total": float(r["coin_total"] or 0),
            "like_count": int(r["coin_total"] or 0),
            "comment_count": int(r["comment_count"] or 0),
            "tipped_by_me": tip is not None and tip["status"] in ("pending", "confirmed"),
            "liked_by_me": tip is not None and tip["status"] in ("pending", "confirmed"),
            "tip_pending": bool(pending),
            "tip_confirm_after": float(tip["confirm_after"]) if tip else None,
            "tip_seconds_left": max(0, int(float(tip["confirm_after"]) - now)) if pending else 0,
        })
    return out


def create_public_work(user_id: int, author_name: str, title: str, body: str) -> dict[str, Any]:
    title = (title or "").strip()[:120]
    body = (body or "").strip()[:8000]
    if not body:
        raise ValueError("작업 내용을 입력하세요.")
    if not title:
        title = body[:40] + ("…" if len(body) > 40 else "")
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO public_works (user_id, author_name, title, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, (author_name or "").strip()[:64], title, body, now),
            )
            conn.commit()
            work_id = int(cur.lastrowid)
        finally:
            conn.close()
    items = list_public_works(limit=1, viewer_id=user_id)
    return next((i for i in items if i["id"] == work_id), {
        "id": work_id,
        "user_id": user_id,
        "author_name": author_name,
        "title": title,
        "body": body,
        "created_at": now,
        "created_at_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
        "like_count": 0,
        "comment_count": 0,
        "liked_by_me": False,
    })


def toggle_work_like(work_id: int, user_id: int) -> dict[str, Any]:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM public_works WHERE id = ?",
                (work_id,),
            ).fetchone()
            if not exists:
                raise ValueError("작업을 찾을 수 없습니다.")
            liked = conn.execute(
                "SELECT 1 FROM work_likes WHERE work_id = ? AND user_id = ?",
                (work_id, user_id),
            ).fetchone()
            if liked:
                conn.execute(
                    "DELETE FROM work_likes WHERE work_id = ? AND user_id = ?",
                    (work_id, user_id),
                )
                liked_by_me = False
            else:
                conn.execute(
                    "INSERT INTO work_likes (work_id, user_id, created_at) VALUES (?, ?, ?)",
                    (work_id, user_id, now),
                )
                liked_by_me = True
            conn.commit()
            count_row = conn.execute(
                "SELECT COUNT(*) AS c FROM work_likes WHERE work_id = ?",
                (work_id,),
            ).fetchone()
        finally:
            conn.close()
    return {"work_id": work_id, "liked_by_me": liked_by_me, "like_count": int(count_row["c"] or 0)}


def list_work_comments(work_id: int, limit: int = 80) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 80), 200))
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT id, work_id, user_id, author_name, body, created_at
                FROM work_comments
                WHERE work_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (work_id, limit),
            ).fetchall()
        finally:
            conn.close()
    return [{
        "id": r["id"],
        "work_id": r["work_id"],
        "user_id": r["user_id"],
        "author_name": r["author_name"],
        "body": r["body"],
        "created_at": r["created_at"],
        "created_at_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"])),
    } for r in rows]


def add_work_comment(work_id: int, user_id: int, author_name: str, body: str) -> dict[str, Any]:
    body = (body or "").strip()[:2000]
    if not body:
        raise ValueError("댓글 내용을 입력하세요.")
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM public_works WHERE id = ?",
                (work_id,),
            ).fetchone()
            if not exists:
                raise ValueError("작업을 찾을 수 없습니다.")
            cur = conn.execute(
                """
                INSERT INTO work_comments (work_id, user_id, author_name, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (work_id, user_id, (author_name or "").strip()[:64], body, now),
            )
            conn.commit()
            comment_id = int(cur.lastrowid)
        finally:
            conn.close()
    return {
        "id": comment_id,
        "work_id": work_id,
        "user_id": user_id,
        "author_name": author_name,
        "body": body,
        "created_at": now,
        "created_at_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
    }


def list_chat_users(exclude_user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 100))
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT id, display_name, email, user_email, created_at
                FROM users
                WHERE id != ?
                ORDER BY display_name ASC, id ASC
                LIMIT ?
                """,
                (exclude_user_id, limit),
            ).fetchall()
        finally:
            conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        name = (r["display_name"] or "").strip()
        if not name:
            name = (r["user_email"] or r["email"] or f"user{r['id']}").split("@")[0]
        out.append({"id": r["id"], "display_name": name})
    return out


def get_chat_messages(user_id: int, peer_id: int, limit: int = 120) -> list[dict[str, Any]]:
    if user_id == peer_id:
        raise ValueError("자기 자신과는 채팅할 수 없습니다.")
    room = _chat_room_id(user_id, peer_id)
    limit = max(1, min(int(limit or 120), 300))
    with _lock:
        conn = _connect()
        try:
            peer = conn.execute("SELECT id FROM users WHERE id = ?", (peer_id,)).fetchone()
            if not peer:
                raise ValueError("상대 계정을 찾을 수 없습니다.")
            rows = conn.execute(
                """
                SELECT id, room_id, sender_id, sender_name, body, created_at
                FROM user_chat_messages
                WHERE room_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (room, limit),
            ).fetchall()
        finally:
            conn.close()
    return [{
        "id": r["id"],
        "room_id": r["room_id"],
        "sender_id": r["sender_id"],
        "sender_name": r["sender_name"],
        "body": r["body"],
        "created_at": r["created_at"],
        "created_at_iso": time.strftime("%H:%M", time.localtime(r["created_at"])),
        "is_mine": r["sender_id"] == user_id,
    } for r in rows]


def send_chat_message(user_id: int, sender_name: str, peer_id: int, body: str) -> dict[str, Any]:
    body = (body or "").strip()[:4000]
    if not body:
        raise ValueError("메시지를 입력하세요.")
    if user_id == peer_id:
        raise ValueError("자기 자신에게는 보낼 수 없습니다.")
    if not _are_friends(user_id, peer_id):
        raise ValueError("친구만 1:1 채팅할 수 있습니다. 먼저 친구를 추가하세요.")
    room = _chat_room_id(user_id, peer_id)
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            peer = conn.execute("SELECT id FROM users WHERE id = ?", (peer_id,)).fetchone()
            if not peer:
                raise ValueError("상대 계정을 찾을 수 없습니다.")
            cur = conn.execute(
                """
                INSERT INTO user_chat_messages (room_id, sender_id, sender_name, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (room, user_id, (sender_name or "").strip()[:64], body, now),
            )
            conn.commit()
            msg_id = int(cur.lastrowid)
        finally:
            conn.close()
    return {
        "id": msg_id,
        "room_id": room,
        "sender_id": user_id,
        "sender_name": sender_name,
        "body": body,
        "created_at": now,
        "created_at_iso": time.strftime("%H:%M", time.localtime(now)),
        "is_mine": True,
    }


def _user_display_name(row: sqlite3.Row) -> str:
    name = (row["display_name"] or "").strip()
    if name:
        return name
    return (row["user_email"] or row["email"] or f"user{row['id']}").split("@")[0]


def _are_friends(user_id: int, other_id: int) -> bool:
    if user_id == other_id:
        return True
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
                (user_id, other_id),
            ).fetchone()
            return row is not None
        finally:
            conn.close()


def search_users(query: str, viewer_id: int, limit: int = 30) -> list[dict[str, Any]]:
    q = (query or "").strip()
    limit = max(1, min(int(limit or 30), 50))
    with _lock:
        conn = _connect()
        try:
            if q:
                like = f"%{q}%"
                rows = conn.execute(
                    """
                    SELECT id, display_name, email, user_email, created_at
                    FROM users
                    WHERE id != ?
                      AND (display_name LIKE ? OR email LIKE ? OR user_email LIKE ?)
                    ORDER BY display_name ASC
                    LIMIT ?
                    """,
                    (viewer_id, like, like, like, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, display_name, email, user_email, created_at
                    FROM users WHERE id != ?
                    ORDER BY display_name ASC LIMIT ?
                    """,
                    (viewer_id, limit),
                ).fetchall()
            friend_ids = {
                int(r["friend_id"])
                for r in conn.execute(
                    "SELECT friend_id FROM friendships WHERE user_id = ?",
                    (viewer_id,),
                ).fetchall()
            }
            pending_out = {
                int(r["to_user_id"])
                for r in conn.execute(
                    "SELECT to_user_id FROM friend_requests WHERE from_user_id = ? AND status = 'pending'",
                    (viewer_id,),
                ).fetchall()
            }
            pending_in = {
                int(r["from_user_id"])
                for r in conn.execute(
                    "SELECT from_user_id FROM friend_requests WHERE to_user_id = ? AND status = 'pending'",
                    (viewer_id,),
                ).fetchall()
            }
        finally:
            conn.close()
    out = []
    for r in rows:
        uid = int(r["id"])
        rel = "none"
        if uid in friend_ids:
            rel = "friend"
        elif uid in pending_out:
            rel = "pending_sent"
        elif uid in pending_in:
            rel = "pending_received"
        out.append({
            "id": uid,
            "display_name": _user_display_name(r),
            "relation": rel,
        })
    return out


def list_friends(user_id: int) -> list[dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT u.id, u.display_name, u.email, u.user_email, f.created_at
                FROM friendships f
                JOIN users u ON u.id = f.friend_id
                WHERE f.user_id = ?
                ORDER BY u.display_name ASC
                """,
                (user_id,),
            ).fetchall()
        finally:
            conn.close()
    return [{
        "id": r["id"],
        "display_name": _user_display_name(r),
        "friend_since": r["created_at"],
    } for r in rows]


def list_friend_requests(user_id: int) -> dict[str, list[dict[str, Any]]]:
    with _lock:
        conn = _connect()
        try:
            incoming = conn.execute(
                """
                SELECT fr.from_user_id, fr.created_at, u.display_name, u.email, u.user_email
                FROM friend_requests fr
                JOIN users u ON u.id = fr.from_user_id
                WHERE fr.to_user_id = ? AND fr.status = 'pending'
                ORDER BY fr.created_at DESC
                """,
                (user_id,),
            ).fetchall()
            outgoing = conn.execute(
                """
                SELECT fr.to_user_id, fr.created_at, u.display_name, u.email, u.user_email
                FROM friend_requests fr
                JOIN users u ON u.id = fr.to_user_id
                WHERE fr.from_user_id = ? AND fr.status = 'pending'
                ORDER BY fr.created_at DESC
                """,
                (user_id,),
            ).fetchall()
        finally:
            conn.close()
    return {
        "incoming": [{
            "id": r["from_user_id"],
            "display_name": _user_display_name(r),
            "created_at": r["created_at"],
        } for r in incoming],
        "outgoing": [{
            "id": r["to_user_id"],
            "display_name": _user_display_name(r),
            "created_at": r["created_at"],
        } for r in outgoing],
    }


def send_friend_request(from_user_id: int, to_user_id: int) -> dict[str, Any]:
    if from_user_id == to_user_id:
        raise ValueError("자기 자신에게는 친구 요청을 보낼 수 없습니다.")
    if _are_friends(from_user_id, to_user_id):
        raise ValueError("이미 친구입니다.")
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            peer = conn.execute("SELECT id FROM users WHERE id = ?", (to_user_id,)).fetchone()
            if not peer:
                raise ValueError("계정을 찾을 수 없습니다.")
            existing = conn.execute(
                """
                SELECT status FROM friend_requests
                WHERE from_user_id = ? AND to_user_id = ?
                """,
                (from_user_id, to_user_id),
            ).fetchone()
            if existing and existing["status"] == "pending":
                raise ValueError("이미 친구 요청을 보냈습니다.")
            reverse = conn.execute(
                """
                SELECT status FROM friend_requests
                WHERE from_user_id = ? AND to_user_id = ?
                """,
                (to_user_id, from_user_id),
            ).fetchone()
            if reverse and reverse["status"] == "pending":
                conn.execute(
                    "UPDATE friend_requests SET status = 'accepted' WHERE from_user_id = ? AND to_user_id = ?",
                    (to_user_id, from_user_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO friendships (user_id, friend_id, created_at) VALUES (?, ?, ?)",
                    (from_user_id, to_user_id, now),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO friendships (user_id, friend_id, created_at) VALUES (?, ?, ?)",
                    (to_user_id, from_user_id, now),
                )
                conn.commit()
                return {"success": True, "status": "accepted", "auto_accepted": True}
            conn.execute(
                """
                INSERT INTO friend_requests (from_user_id, to_user_id, status, created_at)
                VALUES (?, ?, 'pending', ?)
                ON CONFLICT(from_user_id, to_user_id) DO UPDATE SET status = 'pending', created_at = excluded.created_at
                """,
                (from_user_id, to_user_id, now),
            )
            conn.commit()
        finally:
            conn.close()
    return {"success": True, "status": "pending"}


def respond_friend_request(user_id: int, from_user_id: int, accept: bool) -> dict[str, Any]:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT status FROM friend_requests
                WHERE from_user_id = ? AND to_user_id = ? AND status = 'pending'
                """,
                (from_user_id, user_id),
            ).fetchone()
            if not row:
                raise ValueError("친구 요청을 찾을 수 없습니다.")
            if accept:
                conn.execute(
                    "UPDATE friend_requests SET status = 'accepted' WHERE from_user_id = ? AND to_user_id = ?",
                    (from_user_id, user_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO friendships (user_id, friend_id, created_at) VALUES (?, ?, ?)",
                    (user_id, from_user_id, now),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO friendships (user_id, friend_id, created_at) VALUES (?, ?, ?)",
                    (from_user_id, user_id, now),
                )
            else:
                conn.execute(
                    "DELETE FROM friend_requests WHERE from_user_id = ? AND to_user_id = ?",
                    (from_user_id, user_id),
                )
            conn.commit()
        finally:
            conn.close()
    return {"success": True, "accepted": accept}


def confirm_pending_work_tips() -> None:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT t.work_id, t.from_user_id, t.amount, w.user_id AS author_id
                FROM work_coin_tips t
                JOIN public_works w ON w.id = t.work_id
                WHERE t.status = 'pending' AND t.confirm_after <= ?
                """,
                (now,),
            ).fetchall()
            for r in rows:
                author_subject = f"user:{r['author_id']}"
                ensure_coin_balance(author_subject)
                conn.execute(
                    "UPDATE coin_balance SET balance = balance + ?, updated_at = ? WHERE subject = ?",
                    (float(r["amount"]), now, author_subject),
                )
                conn.execute(
                    """
                    UPDATE work_coin_tips SET status = 'confirmed', confirmed_at = ?
                    WHERE work_id = ? AND from_user_id = ?
                    """,
                    (now, r["work_id"], r["from_user_id"]),
                )
            conn.commit()
        finally:
            conn.close()


def send_work_coin_tip(work_id: int, from_user_id: int, amount: float = WORK_COIN_TIP_AMOUNT) -> dict[str, Any]:
    confirm_pending_work_tips()
    amt = max(0.0, float(amount or WORK_COIN_TIP_AMOUNT))
    if amt <= 0:
        raise ValueError("코인 수량이 올바르지 않습니다.")
    now = time.time()
    confirm_after = now + WORK_COIN_TIP_GRACE_SECONDS
    from_subject = f"user:{from_user_id}"
    with _lock:
        conn = _connect()
        try:
            work = conn.execute(
                "SELECT id, user_id FROM public_works WHERE id = ?",
                (work_id,),
            ).fetchone()
            if not work:
                raise ValueError("작업을 찾을 수 없습니다.")
            if int(work["user_id"]) == from_user_id:
                raise ValueError("본인 작업에는 코인을 보낼 수 없습니다.")
            existing = conn.execute(
                "SELECT status, confirm_after FROM work_coin_tips WHERE work_id = ? AND from_user_id = ?",
                (work_id, from_user_id),
            ).fetchone()
            if existing:
                if existing["status"] == "confirmed":
                    raise ValueError("이미 코인을 보냈습니다.")
                if existing["status"] == "pending" and now < float(existing["confirm_after"]):
                    raise ValueError("취소 대기 중인 코인이 있습니다.")
            balance = ensure_coin_balance(from_subject)
            if balance + 1e-9 < amt:
                raise ValueError("코인이 부족합니다.")
            conn.execute(
                "UPDATE coin_balance SET balance = balance - ?, updated_at = ? WHERE subject = ?",
                (amt, now, from_subject),
            )
            conn.execute(
                """
                INSERT INTO work_coin_tips (work_id, from_user_id, amount, status, created_at, confirm_after)
                VALUES (?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(work_id, from_user_id) DO UPDATE SET
                    amount = excluded.amount,
                    status = 'pending',
                    created_at = excluded.created_at,
                    confirm_after = excluded.confirm_after,
                    confirmed_at = NULL
                """,
                (work_id, from_user_id, amt, now, confirm_after),
            )
            conn.commit()
        finally:
            conn.close()
    return {
        "work_id": work_id,
        "amount": amt,
        "status": "pending",
        "confirm_after": confirm_after,
        "seconds_left": WORK_COIN_TIP_GRACE_SECONDS,
    }


def cancel_work_coin_tip(work_id: int, from_user_id: int) -> dict[str, Any]:
    now = time.time()
    from_subject = f"user:{from_user_id}"
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT amount, status, confirm_after FROM work_coin_tips
                WHERE work_id = ? AND from_user_id = ?
                """,
                (work_id, from_user_id),
            ).fetchone()
            if not row:
                raise ValueError("보낸 코인이 없습니다.")
            if row["status"] != "pending":
                raise ValueError("이미 확정된 코인은 취소할 수 없습니다.")
            if now >= float(row["confirm_after"]):
                confirm_pending_work_tips()
                raise ValueError("취소 가능 시간이 지났습니다.")
            amt = float(row["amount"])
            conn.execute(
                "UPDATE coin_balance SET balance = balance + ?, updated_at = ? WHERE subject = ?",
                (amt, now, from_subject),
            )
            conn.execute(
                "DELETE FROM work_coin_tips WHERE work_id = ? AND from_user_id = ?",
                (work_id, from_user_id),
            )
            conn.commit()
        finally:
            conn.close()
    return {"success": True, "refunded": amt}


def _group_room_id(group_id: int) -> str:
    return f"g:{int(group_id)}"


def create_chat_group(creator_id: int, name: str, member_ids: list[int]) -> dict[str, Any]:
    title = (name or "").strip()[:80] or "단체방"
    members = sorted({int(creator_id)} | {int(m) for m in member_ids if int(m) != creator_id})
    if len(members) < 2:
        raise ValueError("단체방에는 2명 이상 필요합니다.")
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            for mid in members:
                if not conn.execute("SELECT 1 FROM users WHERE id = ?", (mid,)).fetchone():
                    raise ValueError(f"계정 {mid}을(를) 찾을 수 없습니다.")
            cur = conn.execute(
                "INSERT INTO chat_groups (name, created_by, created_at) VALUES (?, ?, ?)",
                (title, creator_id, now),
            )
            gid = int(cur.lastrowid)
            for mid in members:
                conn.execute(
                    "INSERT INTO chat_group_members (group_id, user_id, joined_at) VALUES (?, ?, ?)",
                    (gid, mid, now),
                )
            conn.commit()
        finally:
            conn.close()
    return {"id": gid, "name": title, "room_id": _group_room_id(gid), "member_count": len(members)}


def list_chat_rooms(user_id: int) -> list[dict[str, Any]]:
    rooms: list[dict[str, Any]] = []
    friends = list_friends(user_id)
    for f in friends:
        fid = int(f["id"])
        room = _chat_room_id(user_id, fid)
        rooms.append({
            "room_id": room,
            "room_type": "dm",
            "name": f["display_name"],
            "peer_id": fid,
        })
    with _lock:
        conn = _connect()
        try:
            groups = conn.execute(
                """
                SELECT g.id, g.name, g.created_at,
                       (SELECT COUNT(*) FROM chat_group_members gm WHERE gm.group_id = g.id) AS member_count
                FROM chat_groups g
                INNER JOIN chat_group_members m ON m.group_id = g.id AND m.user_id = ?
                ORDER BY g.created_at DESC
                """,
                (user_id,),
            ).fetchall()
        finally:
            conn.close()
    for g in groups:
        rooms.append({
            "room_id": _group_room_id(g["id"]),
            "room_type": "group",
            "group_id": g["id"],
            "name": g["name"],
            "member_count": int(g["member_count"] or 0),
        })
    return rooms


def _user_in_group(conn: sqlite3.Connection, group_id: int, user_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM chat_group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    ).fetchone()
    return row is not None


def get_room_messages(user_id: int, room_id: str, limit: int = 120) -> list[dict[str, Any]]:
    room_id = (room_id or "").strip()
    limit = max(1, min(int(limit or 120), 300))
    if room_id.startswith("g:"):
        group_id = int(room_id.split(":", 1)[1])
        with _lock:
            conn = _connect()
            try:
                if not _user_in_group(conn, group_id, user_id):
                    raise ValueError("이 방에 참여하지 않았습니다.")
                rows = conn.execute(
                    """
                    SELECT id, room_id, sender_id, sender_name, body, created_at
                    FROM user_chat_messages WHERE room_id = ? ORDER BY created_at ASC LIMIT ?
                    """,
                    (room_id, limit),
                ).fetchall()
            finally:
                conn.close()
    else:
        parts = room_id.split(":")
        if len(parts) != 2:
            raise ValueError("잘못된 채팅방입니다.")
        peer_id = int(parts[0]) if int(parts[0]) != user_id else int(parts[1])
        if not _are_friends(user_id, peer_id):
            raise ValueError("친구만 1:1 채팅할 수 있습니다.")
        return get_chat_messages(user_id, peer_id, limit=limit)
    return [{
        "id": r["id"],
        "room_id": r["room_id"],
        "sender_id": r["sender_id"],
        "sender_name": r["sender_name"],
        "body": r["body"],
        "created_at": r["created_at"],
        "created_at_iso": time.strftime("%H:%M", time.localtime(r["created_at"])),
        "is_mine": r["sender_id"] == user_id,
    } for r in rows]


def send_room_message(user_id: int, sender_name: str, room_id: str, body: str) -> dict[str, Any]:
    room_id = (room_id or "").strip()
    if room_id.startswith("g:"):
        group_id = int(room_id.split(":", 1)[1])
        body = (body or "").strip()[:4000]
        if not body:
            raise ValueError("메시지를 입력하세요.")
        now = time.time()
        with _lock:
            conn = _connect()
            try:
                if not _user_in_group(conn, group_id, user_id):
                    raise ValueError("이 방에 참여하지 않았습니다.")
                cur = conn.execute(
                    """
                    INSERT INTO user_chat_messages (room_id, sender_id, sender_name, body, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (room_id, user_id, (sender_name or "").strip()[:64], body, now),
                )
                conn.commit()
                msg_id = int(cur.lastrowid)
            finally:
                conn.close()
        return {
            "id": msg_id,
            "room_id": room_id,
            "sender_id": user_id,
            "sender_name": sender_name,
            "body": body,
            "created_at": now,
            "created_at_iso": time.strftime("%H:%M", time.localtime(now)),
            "is_mine": True,
        }
    parts = room_id.split(":")
    if len(parts) != 2:
        raise ValueError("잘못된 채팅방입니다.")
    peer_id = int(parts[0]) if int(parts[0]) != user_id else int(parts[1])
    if not _are_friends(user_id, peer_id):
        raise ValueError("친구만 1:1 채팅할 수 있습니다.")
    return send_chat_message(user_id, sender_name, peer_id, body)


def list_chat_users(exclude_user_id: int, limit: int = 50, friends_only: bool = True) -> list[dict[str, Any]]:
    if friends_only:
        return list_friends(exclude_user_id)
    return search_users("", exclude_user_id, limit=limit)
