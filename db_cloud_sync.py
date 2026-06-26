"""Optional SQLite backup to Supabase Storage (Render free tier has no persistent disk)."""

from __future__ import annotations

import hashlib
import logging
import os
import threading

import httpx

logger = logging.getLogger("nasaroai")

_BACKUP_NAME = "nasaroai.db"
_last_hash = ""
_upload_lock = threading.Lock()


def _config() -> tuple[str, str, str] | None:
    base = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    bucket = os.environ.get("SUPABASE_DB_BUCKET", "nasaro-backups").strip() or "nasaro-backups"
    if base and key:
        return base, key, bucket
    return None


def cloud_backup_enabled() -> bool:
    return _config() is not None


def restore_db_from_cloud(db_path: str) -> bool:
    cfg = _config()
    if not cfg:
        return False
    base, key, bucket = cfg
    url = f"{base}/storage/v1/object/{bucket}/{_BACKUP_NAME}"
    try:
        with httpx.Client(timeout=60) as client:
            res = client.get(url, headers={"Authorization": f"Bearer {key}"})
        if res.status_code == 404:
            logger.info("Cloud DB: no backup yet (first deploy)")
            return False
        if res.status_code != 200:
            logger.warning("Cloud DB restore HTTP %s", res.status_code)
            return False
        if len(res.content) < 256:
            return False
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = db_path + ".cloud"
        with open(tmp, "wb") as f:
            f.write(res.content)
        os.replace(tmp, db_path)
        global _last_hash
        _last_hash = hashlib.md5(res.content).hexdigest()
        logger.info("Cloud DB restored (%d bytes)", len(res.content))
        return True
    except Exception as exc:
        logger.warning("Cloud DB restore failed: %s", exc)
        return False


def upload_db_to_cloud(db_path: str) -> bool:
    cfg = _config()
    if not cfg or not os.path.isfile(db_path):
        return False
    base, key, bucket = cfg
    url = f"{base}/storage/v1/object/{bucket}/{_BACKUP_NAME}"
    try:
        with open(db_path, "rb") as f:
            data = f.read()
        with httpx.Client(timeout=120) as client:
            res = client.post(
                url,
                content=data,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/octet-stream",
                    "x-upsert": "true",
                },
            )
        if res.status_code not in (200, 201):
            logger.warning("Cloud DB backup HTTP %s: %s", res.status_code, res.text[:160])
            return False
        global _last_hash
        _last_hash = hashlib.md5(data).hexdigest()
        logger.info("Cloud DB backup ok (%d bytes)", len(data))
        return True
    except Exception as exc:
        logger.warning("Cloud DB backup failed: %s", exc)
        return False


def upload_db_if_changed(db_path: str) -> None:
    if not _config() or not os.path.isfile(db_path):
        return
    try:
        digest = hashlib.md5(open(db_path, "rb").read()).hexdigest()
    except OSError:
        return
    if digest == _last_hash:
        return
    with _upload_lock:
        upload_db_to_cloud(db_path)
