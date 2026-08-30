"""Event-Bus fuer das Dashboard.

API und Worker sind getrennte Prozesse. Statt eine zweite Infrastruktur
(Redis/Pub-Sub) einzufuehren, ist die event_log-Tabelle der Bus: der Worker
schreibt, der SSE-Endpunkt liest inkrementell ab der zuletzt gesehenen ID.
Bei einer WAL-SQLite kostet das pro Sekunde einen indizierten Range-Scan.
"""
from __future__ import annotations

import json
from typing import Any

from .db import db
from .logging_conf import get_logger

log = get_logger("events")

MAX_EVENTS = 2000


async def emit(
    message: str,
    *,
    category: str = "general",
    level: str = "info",
    data: dict[str, Any] | None = None,
) -> None:
    """Schreibt ein Ereignis in den Bus. Fehler hier duerfen nie den Aufrufer
    umbringen - Logging ist kein Grund, einen Download scheitern zu lassen."""
    try:
        await db.execute(
            "INSERT INTO event_log(level, category, message, data) VALUES (?,?,?,?)",
            (level, category, message, json.dumps(data, ensure_ascii=False) if data else None),
        )
    except Exception as exc:  # pragma: no cover
        log.warning("event_log-Schreibfehler: %s", exc)


async def tail(after_id: int, limit: int = 100) -> list[dict]:
    return await db.fetch_all(
        "SELECT id, ts, level, category, message, data FROM event_log "
        "WHERE id > ? ORDER BY id ASC LIMIT ?",
        (after_id, limit),
    )


async def latest_id() -> int:
    return int(await db.fetch_value("SELECT COALESCE(MAX(id), 0) FROM event_log", (), 0) or 0)


async def recent(limit: int = 100) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT id, ts, level, category, message, data FROM event_log "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return list(reversed(rows))


async def prune() -> None:
    """Haelt das Log klein. Wird vom Worker periodisch aufgerufen."""
    await db.execute(
        "DELETE FROM event_log WHERE id <= "
        "(SELECT MAX(id) FROM event_log) - ?",
        (MAX_EVENTS,),
    )
