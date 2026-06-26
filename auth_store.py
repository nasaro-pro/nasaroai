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
from typing import Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "nasaroai.db")
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days

_lock = threading.Lock()

QUOTA_LIMITS = {
    "compare": 50,
    "debate": 10,
    "collab": 5,
    "agent": 20,
}


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


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
                """
            )
            conn.commit()
        finally:
            conn.close()


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


def _normalize_email(email: str) -> str:
    value = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise ValueError("올바른 이메일 형식이 아닙니다.")
    return value


def signup(email: str, password: str, display_name: str = "") -> dict[str, Any]:
    if len(password) < 8:
        raise ValueError("비밀번호는 8자 이상이어야 합니다.")
    email_norm = _normalize_email(email)
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                (email_norm, _hash_password(password), display_name.strip(), now),
            )
            user_id = cur.lastrowid
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("이미 사용 중인 이메일입니다.") from exc
        finally:
            conn.close()
    token = create_session(int(user_id))
    return {"token": token, "user": get_user_by_id(int(user_id))}


def login(email: str, password: str) -> dict[str, Any]:
    email_norm = _normalize_email(email)
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, email, password_hash, display_name, created_at FROM users WHERE email = ?",
                (email_norm,),
            ).fetchone()
        finally:
            conn.close()
    if not row or not _verify_password(password, row["password_hash"]):
        raise ValueError("이메일 또는 비밀번호가 올바르지 않습니다.")
    token = create_session(int(row["id"]))
    return {"token": token, "user": _row_to_user(row)}


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + SESSION_TTL_SECONDS
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                (token, user_id, expires_at),
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
                SELECT u.id, u.email, u.display_name, u.created_at, s.expires_at
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
        finally:
            conn.close()
    return _row_to_user(row)


def get_user_by_id(user_id: int) -> dict[str, Any]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, email, display_name, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        raise ValueError("사용자를 찾을 수 없습니다.")
    return _row_to_user(row)


def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"] or row["email"].split("@")[0],
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


def merge_user_data(user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_user_data(user_id)
    skip_empty_scalar_keys = {"extension_prefs", "ai_presets"}
    replace_list_keys = {"saved_works", "ai_presets"}
    for key, value in payload.items():
        if key in skip_empty_scalar_keys and isinstance(value, dict) and not value:
            continue
        if key == "ai_presets" and isinstance(value, list) and not value:
            continue
        if key in replace_list_keys and isinstance(value, list):
            current[key] = value
            continue
        if value is None and key == "active_collab":
            current[key] = None
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


def _day_key() -> str:
    # KST midnight reset approximation via UTC+9
    kst = time.gmtime(time.time() + 9 * 3600)
    return f"{kst.tm_year:04d}-{kst.tm_mon:02d}-{kst.tm_mday:02d}"


def check_and_consume_quota(subject: str, feature: str) -> tuple[bool, dict[str, Any]]:
    limit = QUOTA_LIMITS.get(feature, 9999)
    day_key = _day_key()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT count FROM quota_usage WHERE subject = ? AND feature = ? AND day_key = ?",
                (subject, feature, day_key),
            ).fetchone()
            used = int(row["count"]) if row else 0
            if used >= limit:
                return False, {"feature": feature, "used": used, "limit": limit, "day_key": day_key}
            conn.execute(
                """
                INSERT INTO quota_usage (subject, feature, day_key, count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(subject, feature, day_key) DO UPDATE SET count = count + 1
                """,
                (subject, feature, day_key),
            )
            conn.commit()
            return True, {"feature": feature, "used": used + 1, "limit": limit, "day_key": day_key}
        finally:
            conn.close()


def get_quota_snapshot(subject: str) -> dict[str, Any]:
    day_key = _day_key()
    out = {}
    with _lock:
        conn = _connect()
        try:
            for feature, limit in QUOTA_LIMITS.items():
                row = conn.execute(
                    "SELECT count FROM quota_usage WHERE subject = ? AND feature = ? AND day_key = ?",
                    (subject, feature, day_key),
                ).fetchone()
                used = int(row["count"]) if row else 0
                out[feature] = {"used": used, "limit": limit, "remaining": max(0, limit - used)}
        finally:
            conn.close()
    return {"day_key": day_key, "features": out}


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
    expected = os.getenv("ADMIN_PASSWORD", "050907")
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
                "SELECT id, email, display_name, created_at FROM users ORDER BY id DESC"
            ).fetchall()
        finally:
            conn.close()
    return [_row_to_user(r) for r in rows]


def get_user_quota_totals(user_id: int) -> dict[str, int]:
    prefix = f"user:{user_id}"
    day_key = _day_key()
    totals: dict[str, int] = {}
    with _lock:
        conn = _connect()
        try:
            for feature in QUOTA_LIMITS:
                row = conn.execute(
                    "SELECT count FROM quota_usage WHERE subject = ? AND feature = ? AND day_key = ?",
                    (prefix, feature, day_key),
                ).fetchone()
                totals[feature] = int(row["count"]) if row else 0
            all_time = conn.execute(
                "SELECT feature, SUM(count) AS total FROM quota_usage WHERE subject = ? GROUP BY feature",
                (prefix,),
            ).fetchall()
        finally:
            conn.close()
    totals["_all_time"] = {r["feature"]: int(r["total"]) for r in all_time}
    return totals


def get_admin_dashboard() -> dict[str, Any]:
    users = list_users_admin()
    day_key = _day_key()
    enriched = []
    for u in users:
        uid = int(u["id"])
        quotas = get_user_quota_totals(uid)
        data = get_user_data(uid)
        enriched.append({
            **u,
            "created_at_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(u["created_at"])),
            "quota_today": {k: quotas.get(k, 0) for k in QUOTA_LIMITS},
            "quota_all_time": quotas.get("_all_time", {}),
            "saved_results_count": len(data.get("saved_works") or []),
            "has_active_collab": bool(data.get("active_collab")),
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
        "share_links": int(share_count),
        "usage_today": {r["feature"]: int(r["total"]) for r in today_usage},
        "users": enriched,
    }
