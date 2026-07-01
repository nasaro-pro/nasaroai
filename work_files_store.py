"""Unified work-files library — save/load user artifacts across tools."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from auth_store import _connect, _lock

VALID_TYPES = frozenset({"image", "video", "audio", "code", "ppt", "doc", "collab"})


def ensure_work_files_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS work_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_subject TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'doc',
            title TEXT NOT NULL DEFAULT '',
            content_url_or_path TEXT NOT NULL DEFAULT '',
            text_content TEXT NOT NULL DEFAULT '',
            thumbnail_url TEXT NOT NULL DEFAULT '',
            source_tool TEXT NOT NULL DEFAULT '',
            project_id INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            is_pinned INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_files_owner ON work_files(owner_subject, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_files_type ON work_files(owner_subject, type)"
    )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner_subject": row["owner_subject"],
        "type": row["type"],
        "title": row["title"],
        "content_url_or_path": row["content_url_or_path"],
        "text_content": row["text_content"],
        "thumbnail_url": row["thumbnail_url"],
        "source_tool": row["source_tool"],
        "project_id": row["project_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "size_bytes": row["size_bytes"],
        "is_pinned": bool(row["is_pinned"]),
    }


def save_work_file(
    owner_subject: str,
    *,
    file_type: str = "doc",
    title: str = "",
    content_url: str = "",
    text_content: str = "",
    thumbnail_url: str = "",
    source_tool: str = "",
    project_id: int | None = None,
    size_bytes: int = 0,
    is_pinned: bool = False,
) -> dict[str, Any]:
    ft = (file_type or "doc").strip().lower()
    if ft not in VALID_TYPES:
        ft = "doc"
    now = time.time()
    text = (text_content or "")[:500_000]
    url = (content_url or "")[:2000]
    thumb = (thumbnail_url or url or "")[:2000]
    title_clean = (title or "작업물").strip()[:200] or "작업물"
    sz = int(size_bytes or len(text.encode("utf-8", errors="ignore")))
    with _lock:
        conn = _connect()
        try:
            ensure_work_files_table(conn)
            cur = conn.execute(
                """
                INSERT INTO work_files (
                    owner_subject, type, title, content_url_or_path, text_content,
                    thumbnail_url, source_tool, project_id, created_at, updated_at,
                    size_bytes, is_pinned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_subject,
                    ft,
                    title_clean,
                    url,
                    text,
                    thumb,
                    (source_tool or "")[:64],
                    project_id,
                    now,
                    now,
                    sz,
                    1 if is_pinned else 0,
                ),
            )
            conn.commit()
            fid = int(cur.lastrowid)
            row = conn.execute("SELECT * FROM work_files WHERE id = ?", (fid,)).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row)


def list_work_files(
    owner_subject: str,
    *,
    file_type: str = "",
    project_id: int | None = None,
    q: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    clauses = ["owner_subject = ?"]
    params: list[Any] = [owner_subject]
    ft = (file_type or "").strip().lower()
    if ft and ft in VALID_TYPES:
        clauses.append("type = ?")
        params.append(ft)
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(int(project_id))
    qs = (q or "").strip().lower()
    if qs:
        clauses.append("(LOWER(title) LIKE ? OR LOWER(text_content) LIKE ?)")
        params.extend([f"%{qs}%", f"%{qs}%"])
    where = " AND ".join(clauses)
    with _lock:
        conn = _connect()
        try:
            ensure_work_files_table(conn)
            rows = conn.execute(
                f"""
                SELECT id, owner_subject, type, title, content_url_or_path,
                       thumbnail_url, source_tool, project_id, created_at, updated_at,
                       size_bytes, is_pinned
                FROM work_files WHERE {where}
                ORDER BY is_pinned DESC, updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        finally:
            conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["is_pinned"] = bool(d.get("is_pinned"))
        out.append(d)
    return out


def get_work_file(file_id: int, owner_subject: str) -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        try:
            ensure_work_files_table(conn)
            row = conn.execute(
                "SELECT * FROM work_files WHERE id = ? AND owner_subject = ?",
                (int(file_id), owner_subject),
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row) if row else None


def update_work_file(
    file_id: int,
    owner_subject: str,
    *,
    title: str | None = None,
    is_pinned: bool | None = None,
    project_id: int | None = None,
    text_content: str | None = None,
) -> dict[str, Any] | None:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            ensure_work_files_table(conn)
            row = conn.execute(
                "SELECT * FROM work_files WHERE id = ? AND owner_subject = ?",
                (int(file_id), owner_subject),
            ).fetchone()
            if not row:
                return None
            fields: list[str] = ["updated_at = ?"]
            params: list[Any] = [now]
            if title is not None:
                fields.append("title = ?")
                params.append((title or "작업물").strip()[:200])
            if is_pinned is not None:
                fields.append("is_pinned = ?")
                params.append(1 if is_pinned else 0)
            if project_id is not None:
                fields.append("project_id = ?")
                params.append(project_id)
            if text_content is not None:
                fields.append("text_content = ?")
                params.append((text_content or "")[:500_000])
                fields.append("size_bytes = ?")
                params.append(len((text_content or "").encode("utf-8", errors="ignore")))
            params.extend([int(file_id), owner_subject])
            conn.execute(
                f"UPDATE work_files SET {', '.join(fields)} WHERE id = ? AND owner_subject = ?",
                params,
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM work_files WHERE id = ? AND owner_subject = ?",
                (int(file_id), owner_subject),
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row) if row else None


def delete_work_file(file_id: int, owner_subject: str) -> bool:
    with _lock:
        conn = _connect()
        try:
            ensure_work_files_table(conn)
            cur = conn.execute(
                "DELETE FROM work_files WHERE id = ? AND owner_subject = ?",
                (int(file_id), owner_subject),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def duplicate_work_file(file_id: int, owner_subject: str) -> dict[str, Any] | None:
    src = get_work_file(file_id, owner_subject)
    if not src:
        return None
    return save_work_file(
        owner_subject,
        file_type=src["type"],
        title=(src["title"] or "작업물") + " (복사)",
        content_url=src.get("content_url_or_path") or "",
        text_content=src.get("text_content") or "",
        thumbnail_url=src.get("thumbnail_url") or "",
        source_tool=src.get("source_tool") or "",
        project_id=src.get("project_id"),
        size_bytes=int(src.get("size_bytes") or 0),
        is_pinned=False,
    )


def count_work_files(owner_subject: str) -> int:
    with _lock:
        conn = _connect()
        try:
            ensure_work_files_table(conn)
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM work_files WHERE owner_subject = ?",
                (owner_subject,),
            ).fetchone()
        finally:
            conn.close()
    return int(row["c"]) if row else 0


def admin_list_work_files(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    with _lock:
        conn = _connect()
        try:
            ensure_work_files_table(conn)
            rows = conn.execute(
                """
                SELECT id, owner_subject, type, title, source_tool, created_at, updated_at, size_bytes, is_pinned
                FROM work_files ORDER BY created_at DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def admin_work_files_stats() -> dict[str, Any]:
    with _lock:
        conn = _connect()
        try:
            ensure_work_files_table(conn)
            total = conn.execute("SELECT COUNT(*) AS c FROM work_files").fetchone()["c"]
            by_type = conn.execute(
                "SELECT type, COUNT(*) AS c FROM work_files GROUP BY type ORDER BY c DESC"
            ).fetchall()
        finally:
            conn.close()
    return {
        "total": int(total),
        "by_type": {r["type"]: int(r["c"]) for r in by_type},
    }


def admin_delete_work_file(file_id: int) -> bool:
    with _lock:
        conn = _connect()
        try:
            ensure_work_files_table(conn)
            cur = conn.execute("DELETE FROM work_files WHERE id = ?", (int(file_id),))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
