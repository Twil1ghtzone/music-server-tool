"""Job-Queue auf SQLite.

Warum kein Redis/Celery: es gibt genau einen Worker-Prozess und einen Nutzer.
Eine zusaetzliche Broker-Komponente waere ein weiterer Container, ein weiterer
Ausfallpunkt und ein weiterer Zustand, der zur DB divergieren kann. SQLite mit
BEGIN IMMEDIATE + RETURNING liefert hier ein korrektes atomares claim().
"""
from __future__ import annotations

import json
from typing import Any

from ..db import db
from ..logging_conf import get_logger

log = get_logger("jobs")

# Job-Typen
DOWNLOAD_TRACK = "download_track"
IMPORT_STAGING = "import_staging"
NAVIDROME_SCAN = "navidrome_scan"
LIBRARY_SCAN = "library_scan"
HASH_FILES = "hash_files"
FINGERPRINT = "fingerprint"
FIND_DUPES = "find_dupes"
APPLY_DUPES = "apply_dupes"

ACTIVE_STATES = ("pending", "running")

# Niedriger = wichtiger. Ein wartender Play-Request schlaegt jeden Wartungsjob.
PRIORITY_INTERACTIVE = 10
PRIORITY_NORMAL = 50
PRIORITY_BACKGROUND = 200


async def enqueue(
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = PRIORITY_NORMAL,
    dedupe_key: str | None = None,
    max_attempts: int = 3,
) -> int:
    """Stellt einen Job ein. Existiert unter dedupe_key bereits ein aktiver
    Job, wird dessen ID zurueckgegeben statt ein Duplikat anzulegen."""
    body = json.dumps(payload or {}, ensure_ascii=False)

    if dedupe_key:
        existing = await db.fetch_one(
            "SELECT id, state FROM job WHERE dedupe_key = ?", (dedupe_key,)
        )
        if existing:
            if existing["state"] in ACTIVE_STATES:
                return int(existing["id"])
            # Abgeschlossen/fehlgeschlagen -> Platz fuer einen neuen Lauf machen.
            await db.execute("DELETE FROM job WHERE id = ?", (existing["id"],))

    return int(
        await db.execute(
            "INSERT INTO job(type, payload, priority, dedupe_key, max_attempts) "
            "VALUES (?,?,?,?,?)",
            (job_type, body, priority, dedupe_key, max_attempts),
        )
    )


async def claim() -> dict[str, Any] | None:
    """Holt genau einen Job atomar aus der Queue."""
    async with db.transaction() as conn:
        cur = await conn.execute(
            """
            UPDATE job
               SET state = 'running',
                   attempts = attempts + 1,
                   started_at = datetime('now'),
                   updated_at = datetime('now'),
                   progress = 0
             WHERE id = (
                   SELECT id FROM job
                    WHERE state = 'pending'
                      AND (available_at IS NULL OR available_at <= datetime('now'))
                    ORDER BY priority ASC, id ASC
                    LIMIT 1)
            RETURNING *
            """
        )
        row = await cur.fetchone()
    if not row:
        return None
    job = dict(row)
    try:
        job["payload"] = json.loads(job["payload"] or "{}")
    except json.JSONDecodeError:
        job["payload"] = {}
    return job


async def progress(job_id: int, value: float, detail: str | None = None) -> None:
    await db.execute(
        "UPDATE job SET progress = ?, detail = COALESCE(?, detail), "
        "updated_at = datetime('now') WHERE id = ?",
        (max(0.0, min(1.0, value)), detail, job_id),
    )


async def succeed(job_id: int, detail: str | None = None) -> None:
    await db.execute(
        "UPDATE job SET state = 'done', progress = 1, detail = COALESCE(?, detail), "
        "last_error = NULL, finished_at = datetime('now'), updated_at = datetime('now') "
        "WHERE id = ?",
        (detail, job_id),
    )


def backoff_seconds(attempts: int) -> int:
    """30 s, 2 min, 8 min, ... gedeckelt bei einer halben Stunde.

    Ohne Wartezeit laufen alle Versuche in derselben Sekunde durch: der Worker
    holt den Job sofort wieder aus der Queue, und nach drei Zeilen im Log ist
    er endgueltig tot - lange bevor ein Dienst hochgefahren oder ein Zugang
    hinterlegt sein koennte.
    """
    return min(1800, 30 * (4 ** max(0, attempts - 1)))


async def fail(job_id: int, error: str, *, retry: bool = True) -> None:
    """Setzt zurueck auf pending, solange Versuche uebrig sind."""
    row = await db.fetch_one("SELECT attempts, max_attempts FROM job WHERE id = ?", (job_id,))
    can_retry = retry and row is not None and row["attempts"] < row["max_attempts"]

    if can_retry:
        delay = backoff_seconds(int(row["attempts"]))
        await db.execute(
            "UPDATE job SET state = 'pending', last_error = ?, "
            "available_at = datetime('now', ?), finished_at = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (error[:2000], f"+{delay} seconds", job_id),
        )
        log.info(
            "Job %s fehlgeschlagen, naechster Versuch in %d s: %s",
            job_id, delay, error[:200],
        )
        return

    await db.execute(
        "UPDATE job SET state = 'failed', last_error = ?, "
        "finished_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (error[:2000], job_id),
    )
    log.warning("Job %s endgueltig fehlgeschlagen: %s", job_id, error[:300])


async def cancel(job_id: int) -> bool:
    changed = await db.execute(
        "UPDATE job SET state = 'cancelled', finished_at = datetime('now'), "
        "updated_at = datetime('now') WHERE id = ? AND state IN ('pending','failed')",
        (job_id,),
    )
    return bool(changed)


async def retry(job_id: int) -> bool:
    # available_at zuruecksetzen: "Wiederholen" heisst jetzt, nicht irgendwann.
    changed = await db.execute(
        "UPDATE job SET state = 'pending', attempts = 0, last_error = NULL, "
        "available_at = NULL, finished_at = NULL, updated_at = datetime('now') "
        "WHERE id = ? AND state IN ('failed','cancelled')",
        (job_id,),
    )
    return bool(changed)


async def requeue_orphans() -> int:
    """Beim Worker-Start: Jobs, die beim letzten Absturz 'running' waren,
    zurueck in die Queue."""
    return await db.execute(
        "UPDATE job SET state = 'pending', available_at = NULL, "
        "updated_at = datetime('now') WHERE state = 'running'"
    )


async def stats() -> dict[str, int]:
    rows = await db.fetch_all("SELECT state, COUNT(*) AS n FROM job GROUP BY state")
    out = {"pending": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
    for row in rows:
        out[row["state"]] = int(row["n"])
    return out


async def listing(state: str | None = None, limit: int = 100) -> list[dict]:
    if state and state != "all":
        if state == "active":
            rows = await db.fetch_all(
                "SELECT * FROM job WHERE state IN ('pending','running') "
                "ORDER BY priority ASC, id ASC LIMIT ?",
                (limit,),
            )
        else:
            rows = await db.fetch_all(
                "SELECT * FROM job WHERE state = ? ORDER BY id DESC LIMIT ?", (state, limit)
            )
    else:
        rows = await db.fetch_all("SELECT * FROM job ORDER BY id DESC LIMIT ?", (limit,))
    for row in rows:
        try:
            row["payload"] = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            row["payload"] = {}
    return rows


async def prune(keep: int = 500) -> None:
    await db.execute(
        "DELETE FROM job WHERE state IN ('done','cancelled') AND id <= "
        "(SELECT COALESCE(MAX(id),0) FROM job) - ?",
        (keep,),
    )
