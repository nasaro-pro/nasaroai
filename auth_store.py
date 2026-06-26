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


def _normalize_username(username: str) -> str:
    value = username.strip().lower()
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return value
    if not re.fullmatch(r"[a-z0-9_]{3,32}", value):
        raise ValueError("아이디는 3~32자 (영문·숫자·_)만 사용 가능합니다.")
    return value


def _require_email(email: str) -> str:
    value = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise ValueError("실제 사용 가능한 이메일 주소를 입력해주세요.")
    return value


def signup(username: str, password: str, display_name: str = "") -> dict[str, Any]:
    if len(password) < 8:
        raise ValueError("비밀번호는 8자 이상이어야 합니다.")
    email_norm = _require_email(username)
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
            raise ValueError("이미 가입된 이메일입니다.") from exc
        finally:
            conn.close()
    token = create_session(int(user_id))
    log_login_event(int(user_id), "signup")
    return {"token": token, "user": get_user_by_id(int(user_id))}


def login(username: str, password: str) -> dict[str, Any]:
    try:
        email_norm = _require_email(username)
    except ValueError:
        email_norm = _normalize_username(username)
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
    log_login_event(int(row["id"]), "login")
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
    ident = row["email"]
    if "@" in ident:
        default_name = ident.split("@")[0]
    else:
        default_name = ident
    return {
        "id": row["id"],
        "username": ident,
        "email": ident,
        "display_name": row["display_name"] or default_name,
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


def get_active_session_count() -> int:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE expires_at > ?",
                (now,),
            ).fetchone()
        finally:
            conn.close()
    return int(row["c"]) if row else 0


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


def get_user_login_stats(user_id: int) -> dict[str, Any]:
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
                "SELECT COUNT(*) AS c FROM sessions s WHERE s.user_id = ? AND s.expires_at > ?",
                (user_id, time.time()),
            ).fetchone()["c"]
        finally:
            conn.close()
    return {
        "login_count": int(total),
        "last_login_at": last["created_at"] if last else None,
        "active_sessions": int(active),
    }


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
            log_usage_event(subject, feature)
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
        login_stats = get_user_login_stats(uid)
        collab_info = data.get("active_collab") or {}
        enriched.append({
            **u,
            "created_at_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(u["created_at"])),
            "quota_today": {k: quotas.get(k, 0) for k in QUOTA_LIMITS},
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
            "active_sessions": login_stats["active_sessions"],
            "is_online": login_stats["active_sessions"] > 0,
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
        "share_links": int(share_count),
        "usage_today": {r["feature"]: int(r["total"]) for r in today_usage},
        "usage_by_hour": get_usage_by_hour(day_key),
        "users": enriched,
        "recent_activity": get_activity_log(limit=30),
        "open_support_count": count_open_support(),
        "platform_stats": get_platform_stats(day_key),
    }


def log_activity(
    subject: str,
    feature: str,
    *,
    user_id: int | None = None,
    device_id: str = "",
    platform: str = "web",
    action: str = "",
    detail: str = "",
) -> None:
    now = time.time()
    try:
        with _lock:
            conn = _connect()
            try:
                conn.execute(
                    """
                    INSERT INTO activity_log
                    (subject, user_id, device_id, platform, feature, action, detail, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        subject[:128],
                        user_id,
                        (device_id or "")[:128],
                        (platform or "web")[:32],
                        feature[:32],
                        (action or feature)[:64],
                        (detail or "")[:500],
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def get_activity_log(
    user_id: int | None = None,
    device_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    limit = max(1, min(500, limit))
    with _lock:
        conn = _connect()
        try:
            if user_id is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM activity_log WHERE user_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            elif device_id:
                rows = conn.execute(
                    """
                    SELECT * FROM activity_log WHERE device_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (device_id.strip(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        finally:
            conn.close()
    return [_activity_row(r) for r in rows]


def _activity_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "subject": row["subject"],
        "user_id": row["user_id"],
        "device_id": row["device_id"],
        "platform": row["platform"],
        "feature": row["feature"],
        "action": row["action"],
        "detail": row["detail"],
        "created_at": row["created_at"],
        "created_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["created_at"])),
    }


def get_platform_stats(day_key: str | None = None) -> dict[str, int]:
    day_key = day_key or _day_key()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT platform, COUNT(*) AS cnt FROM activity_log
                WHERE strftime('%Y-%m-%d', datetime(created_at, 'unixepoch', '+9 hours')) = ?
                GROUP BY platform
                """,
                (day_key,),
            ).fetchall()
        finally:
            conn.close()
    return {r["platform"]: int(r["cnt"]) for r in rows}


def list_guest_devices(limit: int = 50) -> list[dict[str, Any]]:
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
        finally:
            conn.close()
    return [
        {
            "device_id": r["device_id"],
            "platform": r["platform"],
            "last_at_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_at"])),
            "activity_count": int(r["cnt"]),
        }
        for r in rows
    ]


def search_users_admin(query: str, limit: int = 30) -> list[dict[str, Any]]:
    q = (query or "").strip().lower()
    users = list_users_admin()
    if not q:
        return users[:limit]
    out = []
    for u in users:
        hay = f"{u.get('username','')} {u.get('display_name','')} {u.get('email','')}".lower()
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


def get_user_admin_detail(user_id: int) -> dict[str, Any]:
    user = get_user_by_id(user_id)
    data = get_user_data(user_id)
    login_stats = get_user_login_stats(user_id)
    quotas = get_user_quota_totals(user_id)
    activity = get_activity_log(user_id=user_id, limit=80)
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
    return {
        **user,
        "login_stats": login_stats,
        "last_login_iso": time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(login_stats["last_login_at"])
        ) if login_stats.get("last_login_at") else "-",
        "quota_today": {k: quotas.get(k, 0) for k in QUOTA_LIMITS},
        "quota_all_time": quotas.get("_all_time", {}),
        "saved_results_count": len(data.get("saved_works") or []),
        "activity": activity,
        "platform_counts": platform_counts,
        "support_inquiries": inquiries,
    }
