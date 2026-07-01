"""Model pricing catalog, site popups, and admin coin grant helpers."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from auth_store import _connect, _lock, adjust_coin_balance

# 1 coin = $0.001; values are fixed per call/image/clip — not token-based.
MODEL_PRICING_SEED: list[dict[str, Any]] = [
    # chat — standard 1-turn cost estimates
    {"label": "OpenAI", "modality": "chat", "coin_cost": 3, "unit_label": "call"},
    {"label": "Anthropic", "modality": "chat", "coin_cost": 12, "unit_label": "call"},
    {"label": "Anthropic-Opus", "modality": "chat", "coin_cost": 20, "unit_label": "call"},
    {"label": "Google", "modality": "chat", "coin_cost": 2, "unit_label": "call"},
    {"label": "xAI", "modality": "chat", "coin_cost": 3, "unit_label": "call"},
    {"label": "Perplexity", "modality": "chat", "coin_cost": 2, "unit_label": "call"},
    {"label": "DeepSeek", "modality": "chat", "coin_cost": 1, "unit_label": "call"},
    {"label": "DeepSeek-Flash", "modality": "chat", "coin_cost": 1, "unit_label": "call"},
    {"label": "DeepSeek-Pro", "modality": "chat", "coin_cost": 1, "unit_label": "call"},
    {"label": "GLM", "modality": "chat", "coin_cost": 3, "unit_label": "call"},
    {"label": "MiniMax", "modality": "chat", "coin_cost": 1, "unit_label": "call"},
    # code — per call
    {"label": "Qwen-Coder", "modality": "code", "coin_cost": 2, "unit_label": "call"},
    {"label": "DeepSeek-Coder", "modality": "code", "coin_cost": 1, "unit_label": "call"},
    {"label": "Codex", "modality": "code", "coin_cost": 5, "unit_label": "call"},
    {"label": "Claude-Code", "modality": "code", "coin_cost": 12, "unit_label": "call"},
    # image — per image
    {"label": "Seedream 4.5", "modality": "image", "coin_cost": 40, "unit_label": "image"},
    {"label": "Grok Imagine", "modality": "image", "coin_cost": 20, "unit_label": "image"},
    {"label": "Grok Imagine Pro", "modality": "image", "coin_cost": 70, "unit_label": "image"},
    {"label": "Nano Banana Pro", "modality": "image", "coin_cost": 100, "unit_label": "image"},
    {"label": "Flux Pro", "modality": "image", "coin_cost": 55, "unit_label": "image"},
    # audio — per clip
    {"label": "GPT-4o Mini TTS", "modality": "audio", "coin_cost": 8, "unit_label": "clip"},
    {"label": "Gemini Flash TTS", "modality": "audio", "coin_cost": 6, "unit_label": "clip"},
    {"label": "Voxtral Mini TTS", "modality": "audio", "coin_cost": 5, "unit_label": "clip"},
    # video — per 5s clip
    {"label": "Veo 3.1 Fast", "modality": "video", "coin_cost": 1100, "unit_label": "clip_5s"},
    {"label": "Veo 3.1 Standard", "modality": "video", "coin_cost": 2800, "unit_label": "clip_5s"},
    {"label": "Kling 3.0 Standard", "modality": "video", "coin_cost": 630, "unit_label": "clip_5s"},
    {"label": "Sora 2 Pro", "modality": "video", "coin_cost": 2000, "unit_label": "clip_5s"},
]

CHAT_PRICING_LABELS: tuple[str, ...] = tuple(
    r["label"] for r in MODEL_PRICING_SEED if r["modality"] == "chat"
)
IMAGE_PRICING_LABELS: tuple[str, ...] = tuple(
    r["label"] for r in MODEL_PRICING_SEED if r["modality"] == "image"
)
VIDEO_PRICING_LABELS: tuple[str, ...] = tuple(
    r["label"] for r in MODEL_PRICING_SEED if r["modality"] == "video"
)
CODE_PRICING_LABELS: tuple[str, ...] = tuple(
    r["label"] for r in MODEL_PRICING_SEED if r["modality"] == "code"
)
AUDIO_PRICING_LABELS: tuple[str, ...] = tuple(
    r["label"] for r in MODEL_PRICING_SEED if r["modality"] == "audio"
)


def ensure_pricing_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS model_pricing (
            label TEXT NOT NULL,
            modality TEXT NOT NULL DEFAULT 'chat',
            coin_cost INTEGER NOT NULL DEFAULT 1,
            min_coin_charge INTEGER NOT NULL DEFAULT 1,
            unit_label TEXT NOT NULL DEFAULT 'call',
            is_active INTEGER NOT NULL DEFAULT 1,
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (label, modality)
        );
        CREATE TABLE IF NOT EXISTS site_popup_notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'event',
            day_key TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS popup_dismissals (
            subject TEXT NOT NULL,
            day_key TEXT NOT NULL,
            notice_id INTEGER NOT NULL,
            dismissed_at REAL NOT NULL,
            PRIMARY KEY (subject, day_key, notice_id)
        );
        CREATE TABLE IF NOT EXISTS admin_coin_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL,
            target_mode TEXT NOT NULL DEFAULT 'all',
            target_filter TEXT NOT NULL DEFAULT '{}',
            scheduled_at REAL,
            executed_at REAL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            result_summary TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS media_jobs (
            job_id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            label TEXT NOT NULL,
            modality TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            prompt TEXT NOT NULL DEFAULT '',
            result_url TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            coin_cost INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS studio_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            files_json TEXT NOT NULL DEFAULT '{}',
            thumbnail TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_studio_projects_subject ON studio_projects(subject, updated_at DESC);
        """
    )
    mj_cols = {r[1] for r in conn.execute("PRAGMA table_info(media_jobs)").fetchall()}
    if "progress_stage" not in mj_cols:
        conn.execute("ALTER TABLE media_jobs ADD COLUMN progress_stage TEXT NOT NULL DEFAULT 'queued'")
    if "feature" not in mj_cols:
        conn.execute("ALTER TABLE media_jobs ADD COLUMN feature TEXT NOT NULL DEFAULT 'studio'")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(model_pricing)").fetchall()}
    migrations = [
        ("coin_cost", "INTEGER NOT NULL DEFAULT 1"),
        ("min_coin_charge", "INTEGER NOT NULL DEFAULT 1"),
        ("modality", "TEXT NOT NULL DEFAULT 'chat'"),
        ("unit_label", "TEXT NOT NULL DEFAULT 'call'"),
        ("is_active", "INTEGER NOT NULL DEFAULT 1"),
        ("updated_at", "REAL NOT NULL DEFAULT 0"),
    ]
    for col, ddl in migrations:
        if col not in cols:
            conn.execute(f"ALTER TABLE model_pricing ADD COLUMN {col} {ddl}")


def seed_model_pricing(conn: sqlite3.Connection) -> None:
    now = time.time()
    for row in MODEL_PRICING_SEED:
        cost = max(int(row.get("min_coin_charge", 1)), int(row["coin_cost"]))
        conn.execute(
            """
            INSERT INTO model_pricing (label, modality, coin_cost, min_coin_charge, unit_label, is_active, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(label, modality) DO UPDATE SET
                coin_cost = excluded.coin_cost,
                min_coin_charge = excluded.min_coin_charge,
                unit_label = excluded.unit_label,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (
                row["label"],
                row["modality"],
                cost,
                int(row.get("min_coin_charge", 1)),
                row.get("unit_label", "call"),
                now,
            ),
        )


def get_model_coin_cost(label: str, modality: str = "chat") -> int:
    clean_label = (label or "").strip()
    clean_mod = (modality or "chat").strip().lower() or "chat"
    if not clean_label:
        return 1
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT coin_cost, min_coin_charge FROM model_pricing
                WHERE label = ? AND modality = ? AND is_active = 1
                """,
                (clean_label, clean_mod),
            ).fetchone()
            if row:
                return max(int(row["min_coin_charge"] or 1), int(row["coin_cost"] or 1))
            row2 = conn.execute(
                """
                SELECT coin_cost, min_coin_charge FROM model_pricing
                WHERE label = ? AND is_active = 1
                ORDER BY CASE modality WHEN ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (clean_label, clean_mod),
            ).fetchone()
            if row2:
                return max(int(row2["min_coin_charge"] or 1), int(row2["coin_cost"] or 1))
        finally:
            conn.close()
    return 1


def list_pricing_catalog(active_only: bool = True) -> list[dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            q = "SELECT label, modality, coin_cost, min_coin_charge, unit_label, is_active FROM model_pricing"
            if active_only:
                q += " WHERE is_active = 1"
            q += " ORDER BY modality, coin_cost, label"
            rows = conn.execute(q).fetchall()
        finally:
            conn.close()
    return [
        {
            "label": r["label"],
            "modality": r["modality"],
            "coin_cost": max(int(r["min_coin_charge"] or 1), int(r["coin_cost"] or 1)),
            "unit_label": r["unit_label"],
            "is_active": bool(r["is_active"]),
        }
        for r in rows
    ]


def _day_key(ts: float | None = None) -> str:
    t = time.localtime(ts or time.time())
    return time.strftime("%Y-%m-%d", t)


def get_active_popup_for_subject(subject: str) -> dict[str, Any] | None:
    clean = (subject or "").strip() or "guest:anonymous"
    day = _day_key()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT id, title, body, kind, day_key
                FROM site_popup_notices
                WHERE day_key = ? AND is_active = 1
                ORDER BY id DESC LIMIT 1
                """,
                (day,),
            ).fetchone()
            if not row:
                return None
            dismissed = conn.execute(
                """
                SELECT 1 FROM popup_dismissals
                WHERE subject = ? AND day_key = ? AND notice_id = ?
                """,
                (clean, day, row["id"]),
            ).fetchone()
            if dismissed:
                return None
        finally:
            conn.close()
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "kind": row["kind"],
        "day_key": row["day_key"],
    }


def dismiss_popup(subject: str, notice_id: int) -> dict[str, Any]:
    clean = (subject or "").strip() or "guest:anonymous"
    day = _day_key()
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO popup_dismissals (subject, day_key, notice_id, dismissed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(subject, day_key, notice_id) DO UPDATE SET dismissed_at = excluded.dismissed_at
                """,
                (clean, day, int(notice_id), now),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "subject": clean, "notice_id": int(notice_id)}


def admin_upsert_popup(title: str, body: str, kind: str = "event", day_key: str | None = None) -> dict[str, Any]:
    day = (day_key or _day_key()).strip()
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE site_popup_notices SET is_active = 0 WHERE day_key = ?",
                (day,),
            )
            cur = conn.execute(
                """
                INSERT INTO site_popup_notices (title, body, kind, day_key, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                ((title or "").strip()[:200], (body or "").strip()[:8000], (kind or "event").strip()[:32], day, now, now),
            )
            conn.commit()
            notice_id = int(cur.lastrowid)
        finally:
            conn.close()
    return {"id": notice_id, "day_key": day, "title": title, "kind": kind}


def admin_list_popups(limit: int = 30) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 30), 200))
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT id, title, body, kind, day_key, is_active, created_at, updated_at
                FROM site_popup_notices ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def admin_create_coin_grant(
    amount: float,
    target_mode: str,
    target_filter: dict | None = None,
    title: str = "",
    scheduled_at: float | None = None,
) -> dict[str, Any]:
    amt = max(1.0, float(amount))
    mode = (target_mode or "all").strip().lower()
    filt = target_filter or {}
    now = time.time()
    status = "pending"
    if scheduled_at and scheduled_at > now + 1:
        status = "scheduled"
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO admin_coin_grants (title, amount, target_mode, target_filter, scheduled_at, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (title or "").strip()[:200],
                    amt,
                    mode,
                    __import__("json").dumps(filt, ensure_ascii=False),
                    scheduled_at,
                    status,
                    now,
                ),
            )
            conn.commit()
            grant_id = int(cur.lastrowid)
        finally:
            conn.close()
    if status == "pending":
        return execute_coin_grant(grant_id)
    return {"id": grant_id, "status": status, "scheduled_at": scheduled_at}


def _subjects_for_grant(conn: sqlite3.Connection, mode: str, filt: dict) -> list[str]:
    subjects: list[str] = []
    min_balance = float(filt.get("min_balance", -1))
    if mode in ("all", "members"):
        q = "SELECT id FROM users"
        params: list[Any] = []
        if filt.get("registered_after"):
            q += " WHERE created_at >= ?"
            params.append(float(filt["registered_after"]))
        rows = conn.execute(q, params).fetchall()
        for r in rows:
            subj = f"user:{r['id']}"
            if min_balance >= 0:
                bal_row = conn.execute(
                    "SELECT balance FROM coin_balance WHERE subject = ?", (subj,)
                ).fetchone()
                bal = float(bal_row["balance"]) if bal_row else 0.0
                if bal > min_balance:
                    continue
            subjects.append(subj)
    if mode in ("all", "guests"):
        rows = conn.execute("SELECT device_id FROM device_registry").fetchall()
        for r in rows:
            did = (r["device_id"] or "").strip()
            if did:
                subjects.append(f"device:{did}")
    if mode == "subjects":
        for s in filt.get("subjects") or []:
            clean = str(s).strip()
            if clean:
                subjects.append(clean)
    return list(dict.fromkeys(subjects))


def execute_coin_grant(grant_id: int) -> dict[str, Any]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, amount, target_mode, target_filter, status FROM admin_coin_grants WHERE id = ?",
                (grant_id,),
            ).fetchone()
            if not row:
                raise ValueError("grant not found")
            if row["status"] == "executed":
                return {"id": grant_id, "status": "executed", "already": True}
            filt = {}
            try:
                filt = __import__("json").loads(row["target_filter"] or "{}")
            except Exception:
                filt = {}
            subjects = _subjects_for_grant(conn, row["target_mode"], filt)
            amt = float(row["amount"])
        finally:
            conn.close()
    granted = 0
    for subj in subjects:
        try:
            adjust_coin_balance(subj, amt)
            granted += 1
        except Exception:
            pass
    summary = f"granted {amt} coins to {granted} subjects"
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE admin_coin_grants SET status = 'executed', executed_at = ?, result_summary = ?
                WHERE id = ?
                """,
                (now, summary, grant_id),
            )
            conn.commit()
        finally:
            conn.close()
    return {"id": grant_id, "status": "executed", "granted_count": granted, "amount": amt, "summary": summary}


def process_due_coin_grants() -> int:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT id FROM admin_coin_grants
                WHERE status = 'scheduled' AND scheduled_at IS NOT NULL AND scheduled_at <= ?
                """,
                (now,),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
        finally:
            conn.close()
    for gid in ids:
        execute_coin_grant(gid)
    return len(ids)


def create_media_job(
    job_id: str,
    subject: str,
    label: str,
    modality: str,
    prompt: str,
    coin_cost: int,
    feature: str = "studio",
) -> None:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(media_jobs)").fetchall()}
            if "feature" not in cols:
                conn.execute("ALTER TABLE media_jobs ADD COLUMN feature TEXT NOT NULL DEFAULT 'studio'")
            conn.execute(
                """
                INSERT INTO media_jobs (job_id, subject, label, modality, status, progress_stage, prompt, coin_cost, created_at, updated_at, feature)
                VALUES (?, ?, ?, ?, 'pending', 'queued', ?, ?, ?, ?, ?)
                """,
                (job_id, subject, label, modality, prompt[:4000], int(coin_cost), now, now, feature),
            )
            conn.commit()
        finally:
            conn.close()


def update_media_job(
    job_id: str,
    *,
    status: str | None = None,
    progress_stage: str | None = None,
    result_url: str | None = None,
    error: str | None = None,
) -> None:
    now = time.time()
    fields = ["updated_at = ?"]
    params: list[Any] = [now]
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if progress_stage is not None:
        fields.append("progress_stage = ?")
        params.append(progress_stage)
    if result_url is not None:
        fields.append("result_url = ?")
        params.append(result_url)
    if error is not None:
        fields.append("error = ?")
        params.append(error)
    params.append(job_id)
    with _lock:
        conn = _connect()
        try:
            conn.execute(f"UPDATE media_jobs SET {', '.join(fields)} WHERE job_id = ?", params)
            conn.commit()
        finally:
            conn.close()


def get_media_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM media_jobs WHERE job_id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def create_studio_project(subject: str, name: str, files: dict[str, str], thumbnail: str = "") -> dict[str, Any]:
    name = (name or "project").strip()[:120] or "project"
    now = time.time()
    payload = json.dumps(files or {}, ensure_ascii=False)
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO studio_projects (subject, name, files_json, thumbnail, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (subject, name, payload, (thumbnail or "")[:2000], now, now),
            )
            conn.commit()
            pid = int(cur.lastrowid)
        finally:
            conn.close()
    return {"id": pid, "subject": subject, "name": name, "files": files, "thumbnail": thumbnail, "updated_at": now}


def list_studio_projects(subject: str, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 100))
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT id, subject, name, files_json, thumbnail, created_at, updated_at
                FROM studio_projects WHERE subject = ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (subject, limit),
            ).fetchall()
        finally:
            conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            files = json.loads(r["files_json"] or "{}")
        except Exception:
            files = {}
        out.append({
            "id": r["id"],
            "subject": r["subject"],
            "name": r["name"],
            "files": files,
            "thumbnail": r["thumbnail"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return out


def get_studio_project(project_id: int, subject: str) -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM studio_projects WHERE id = ? AND subject = ?",
                (project_id, subject),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    try:
        files = json.loads(row["files_json"] or "{}")
    except Exception:
        files = {}
    return {
        "id": row["id"],
        "subject": row["subject"],
        "name": row["name"],
        "files": files,
        "thumbnail": row["thumbnail"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def delete_studio_project(project_id: int, subject: str) -> bool:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "DELETE FROM studio_projects WHERE id = ? AND subject = ?",
                (project_id, subject),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
