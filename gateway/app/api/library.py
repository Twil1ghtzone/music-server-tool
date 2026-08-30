"""Bibliotheks-API: Index, Duplikate, Tag-Werkzeuge."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .. import security
from ..config import settings
from ..db import db
from ..events import emit
from ..logging_conf import get_logger
from ..services import dedupe, jobs, scanner, tags

log = get_logger("api.library")
router = APIRouter(prefix="/api/library", tags=["library"])


class ApplyBody(BaseModel):
    groups: list[int] = Field(min_length=1, max_length=500)


class KeeperBody(BaseModel):
    media_file_id: int


class TagBody(BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    genre: str | None = None
    year: int | None = None
    track_no: int | None = None
    disc_no: int | None = None


class BatchTagBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=1000)
    changes: TagBody


def _require_tag_write() -> None:
    """Tags zu schreiben veraendert vorhandene Dateien. Standardmaessig aus,
    damit ein Fehlklick im ersten Betrieb den Bestand nicht anfasst."""
    if not settings.allow_tag_write:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Tag-Schreiben ist gesperrt. Zum Freischalten GATEWAY_ALLOW_TAG_WRITE=true "
            "setzen und den Stack neu starten.",
        )


# ------------------------------------------------------------------ Index
@router.get("/stats")
async def stats(user: dict = Depends(security.current_user)) -> dict:
    return await scanner.library_stats()


@router.post("/scan")
async def scan(user: dict = Depends(security.guarded)) -> dict:
    job_id = await jobs.enqueue(
        jobs.LIBRARY_SCAN, priority=jobs.PRIORITY_NORMAL, dedupe_key="scan:library"
    )
    return {"job": job_id}


@router.post("/fingerprint")
async def fingerprint(
    user: dict = Depends(security.guarded), limit: int = Query(5000, le=100000)
) -> dict:
    job_id = await jobs.enqueue(
        jobs.FINGERPRINT,
        {"limit": limit},
        priority=jobs.PRIORITY_BACKGROUND,
        dedupe_key="fingerprint:all",
    )
    return {"job": job_id}


@router.get("/files")
async def files(
    user: dict = Depends(security.current_user),
    q: str = Query("", max_length=200),
    issues_only: bool = False,
    limit: int = Query(100, le=500),
    offset: int = 0,
) -> dict:
    where = ["missing = 0"]
    params: list = []
    if q:
        where.append("(title LIKE ? OR artist LIKE ? OR album LIKE ? OR path LIKE ?)")
        needle = f"%{q}%"
        params.extend([needle] * 4)
    if issues_only:
        where.append("tag_issues IS NOT NULL")
    clause = " AND ".join(where)

    rows = await db.fetch_all(
        f"SELECT id, path, size, ext, bitrate, duration, title, artist, album, "
        f"album_artist, track_no, disc_no, year, has_cover, tag_issues "
        f"FROM media_file WHERE {clause} ORDER BY artist, album, track_no LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    total = int(
        await db.fetch_value(f"SELECT COUNT(*) FROM media_file WHERE {clause}", params, 0) or 0
    )
    return {"files": rows, "total": total, "limit": limit, "offset": offset}


# -------------------------------------------------------------- Duplikate
@router.get("/dupes")
async def list_dupes(
    user: dict = Depends(security.current_user),
    state: str = Query("open", pattern="^(open|applied|ignored)$"),
    limit: int = Query(100, le=300),
) -> dict:
    return {"groups": await dedupe.groups(state, limit), "summary": await dedupe.summary()}


@router.post("/dupes/find")
async def find_dupes(
    user: dict = Depends(security.guarded), acoustic: bool = False
) -> dict:
    job_id = await jobs.enqueue(
        jobs.FIND_DUPES,
        {"acoustic": acoustic},
        priority=jobs.PRIORITY_NORMAL,
        dedupe_key="dupes:scan",
    )
    return {"job": job_id}


@router.post("/dupes/apply")
async def apply_dupes(body: ApplyBody, user: dict = Depends(security.guarded)) -> dict:
    """Verschiebt die Nicht-Keeper in die Quarantaene. Kein Loeschen."""
    if not settings.allow_dedupe_apply:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Bereinigung ist gesperrt. Zum Freischalten GATEWAY_ALLOW_DEDUPE_APPLY=true "
            "setzen und den Stack neu starten.",
        )
    job_id = await jobs.enqueue(
        jobs.APPLY_DUPES,
        {"groups": body.groups},
        priority=jobs.PRIORITY_NORMAL,
    )
    return {"job": job_id, "quarantine": str(settings.quarantine_dir)}


@router.post("/dupes/{group_id}/keeper")
async def set_keeper(
    group_id: int, body: KeeperBody, user: dict = Depends(security.guarded)
) -> dict:
    member = await db.fetch_one(
        "SELECT 1 FROM dupe_member WHERE group_id = ? AND media_file_id = ?",
        (group_id, body.media_file_id),
    )
    if not member:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Datei gehoert nicht zu dieser Gruppe")
    await db.execute(
        "UPDATE dupe_group SET keeper_id = ? WHERE id = ? AND state = 'open'",
        (body.media_file_id, group_id),
    )
    return {"ok": True}


@router.post("/dupes/{group_id}/ignore")
async def ignore_group(group_id: int, user: dict = Depends(security.guarded)) -> dict:
    await db.execute("UPDATE dupe_group SET state = 'ignored' WHERE id = ?", (group_id,))
    return {"ok": True}


@router.post("/dupes/{group_id}/restore")
async def restore_group(group_id: int, user: dict = Depends(security.guarded)) -> dict:
    restored = await dedupe.restore(group_id)
    await emit(f"{restored} Datei(en) aus der Quarantaene zurueckgeholt", category="dedupe")
    return {"restored": restored}


# ------------------------------------------------------------------ Tags
@router.patch("/files/{file_id}/tags")
async def edit_tags(
    file_id: int, body: TagBody, user: dict = Depends(security.guarded)
) -> dict:
    _require_tag_write()
    row = await db.fetch_one("SELECT * FROM media_file WHERE id = ?", (file_id,))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Datei nicht gefunden")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(status.HTTP_410_GONE, "Datei liegt nicht mehr an diesem Pfad")

    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if not changes:
        return {"ok": True, "changed": 0}

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, tags.write, path, changes)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Schreiben fehlgeschlagen: {exc}")

    await _refresh_row(file_id, path)
    await emit(f"Tags geaendert: {path.name}", category="tags")
    return {"ok": True, "changed": len(changes)}


@router.post("/files/tags/batch")
async def batch_tags(body: BatchTagBody, user: dict = Depends(security.guarded)) -> dict:
    _require_tag_write()
    changes = {k: v for k, v in body.changes.model_dump(exclude_unset=True).items()}
    if not changes:
        return {"ok": True, "changed": 0}

    placeholders = ",".join("?" * len(body.ids))
    rows = await db.fetch_all(
        f"SELECT id, path FROM media_file WHERE id IN ({placeholders})", body.ids
    )
    loop = asyncio.get_running_loop()
    changed, failed = 0, []
    for row in rows:
        path = Path(row["path"])
        try:
            await loop.run_in_executor(None, tags.write, path, changes)
            await _refresh_row(int(row["id"]), path)
            changed += 1
        except Exception as exc:
            failed.append({"path": row["path"], "error": str(exc)})

    await emit(f"Batch-Tagging: {changed} Datei(en) geaendert", category="tags")
    return {"ok": True, "changed": changed, "failed": failed}


async def _refresh_row(file_id: int, path: Path) -> None:
    loop = asyncio.get_running_loop()
    meta = await loop.run_in_executor(None, tags.read, path)
    issues = tags.validate(meta)
    await db.execute(
        "UPDATE media_file SET title=?, artist=?, album=?, album_artist=?, "
        "track_no=?, disc_no=?, year=?, has_cover=?, tag_issues=? WHERE id = ?",
        (
            meta.get("title"),
            meta.get("artist"),
            meta.get("album"),
            meta.get("album_artist"),
            meta.get("track_no"),
            meta.get("disc_no"),
            meta.get("year"),
            1 if meta.get("has_cover") else 0,
            ",".join(issues) if issues else None,
            file_id,
        ),
    )


@router.get("/issues")
async def issues(user: dict = Depends(security.current_user)) -> dict:
    rows = await db.fetch_all(
        "SELECT tag_issues FROM media_file WHERE missing = 0 AND tag_issues IS NOT NULL"
    )
    counts: dict[str, int] = {}
    for row in rows:
        for issue in (row["tag_issues"] or "").split(","):
            if issue:
                counts[issue] = counts.get(issue, 0) + 1
    return {
        "total": len(rows),
        "by_issue": sorted(
            ({"issue": k, "count": v} for k, v in counts.items()),
            key=lambda x: x["count"],
            reverse=True,
        ),
    }
