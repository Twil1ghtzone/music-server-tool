"""Dashboard-API: Status, Live-Ereignisse, Warteschlange, Suche, Diagnose."""
from __future__ import annotations

import asyncio
import json
import shutil

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import events, preflight, security
from ..clients import deemix, deezer, navidrome
from ..config import settings
from ..db import db
from ..logging_conf import get_logger
from ..services import dedupe, downloader, ffmpeg, jobs, scanner

log = get_logger("api.dashboard")
router = APIRouter(prefix="/api", tags=["dashboard"])


class DownloadBody(BaseModel):
    provider_id: str = Field(min_length=1, max_length=64)


class ScanBody(BaseModel):
    full: bool = False


# ------------------------------------------------------------------ Status
def _disk(path) -> dict:
    try:
        usage = shutil.disk_usage(path)
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        }
    except OSError as exc:
        return {"error": str(exc)}


@router.get("/status")
async def status_overview(user: dict = Depends(security.current_user)) -> dict:
    nd_info, job_stats, lib, dupes = await asyncio.gather(
        navidrome.server_info(),
        jobs.stats(),
        scanner.library_stats(),
        dedupe.summary(),
    )
    scan = await navidrome.scan_status()
    virtual = await db.fetch_one(
        "SELECT "
        " SUM(CASE WHEN state='ready' THEN 1 ELSE 0 END) AS ready,"
        " SUM(CASE WHEN state IN ('queued','downloading','importing') THEN 1 ELSE 0 END) AS active,"
        " SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed,"
        " COUNT(*) AS total "
        "FROM virtual_track"
    ) or {}

    return {
        "navidrome": {**nd_info, "scan": scan},
        "jobs": job_stats,
        "library": lib,
        "duplicates": dupes,
        "virtual": virtual,
        "storage": {
            "music": _disk(settings.music_dir),
            "staging": _disk(settings.staging_dir),
        },
        "config": {
            "stream_mode": settings.stream_mode,
            "provider_search": settings.provider_search_enabled,
            "marker": settings.marker_suffix,
            "worker_concurrency": settings.worker_concurrency,
        },
    }


@router.get("/recent")
async def recent(user: dict = Depends(security.current_user), limit: int = 12) -> dict:
    try:
        albums = await navidrome.album_list("newest", limit)
    except Exception as exc:
        albums = []
        log.debug("Alben nicht abrufbar: %s", exc)
    return {"albums": albums, "events": await events.recent(50)}


# ----------------------------------------------------------- Live-Ereignisse
@router.get("/events")
async def event_stream(request: Request) -> StreamingResponse:
    user = await security.load_session_user(request.cookies.get(security.SESSION_COOKIE))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Nicht angemeldet")

    async def generator():
        cursor = await events.latest_id()
        # SSE statt WebSocket: nur eine Richtung noetig, laeuft durch jeden
        # Reverse-Proxy und der Browser uebernimmt das Reconnect selbst.
        yield b": verbunden\n\n"
        idle = 0
        while True:
            if await request.is_disconnected():
                return
            fresh = await events.tail(cursor)
            for item in fresh:
                cursor = int(item["id"])
                payload = json.dumps(item, ensure_ascii=False)
                yield f"event: log\ndata: {payload}\n\n".encode()

            idle += 1
            if idle >= 2:
                idle = 0
                snapshot = {
                    "jobs": await jobs.stats(),
                    "active": await jobs.listing("active", 20),
                    "queue": await downloader.queue_overview(20),
                }
                yield f"event: state\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n".encode()
            await asyncio.sleep(1.5)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------- Jobs
@router.get("/jobs")
async def list_jobs(
    user: dict = Depends(security.current_user),
    state: str = Query("all"),
    limit: int = Query(100, le=500),
) -> dict:
    return {"jobs": await jobs.listing(state, limit), "stats": await jobs.stats()}


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: int, user: dict = Depends(security.guarded)) -> dict:
    if not await jobs.retry(job_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Job ist nicht wiederholbar")
    return {"ok": True}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, user: dict = Depends(security.guarded)) -> dict:
    if not await jobs.cancel(job_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Job laeuft bereits oder ist beendet")
    return {"ok": True}


# ------------------------------------------------------- Downloads / Suche
@router.get("/queue")
async def queue(user: dict = Depends(security.current_user), limit: int = 100) -> dict:
    return {"items": await downloader.queue_overview(limit)}


@router.get("/search")
async def search(
    user: dict = Depends(security.current_user),
    q: str = Query(min_length=1, max_length=200),
) -> dict:
    local_task = asyncio.create_task(navidrome.search_songs(q, count=25))
    catalog_task = asyncio.create_task(deezer.search_tracks(q, limit=25))
    local, catalog = await asyncio.gather(local_task, catalog_task, return_exceptions=True)

    local_rows = local if isinstance(local, list) else []
    catalog_rows = catalog if isinstance(catalog, list) else []

    known = await db.fetch_all(
        "SELECT provider_id, state, navidrome_id FROM virtual_track WHERE provider = ?",
        (deezer.PROVIDER,),
    ) if catalog_rows else []
    by_id = {row["provider_id"]: row for row in known}
    for item in catalog_rows:
        item["known"] = by_id.get(item["provider_id"])

    return {"local": local_rows, "catalog": catalog_rows}


@router.post("/download")
async def request_download(
    body: DownloadBody, user: dict = Depends(security.guarded)
) -> dict:
    try:
        return await downloader.request_track(body.provider_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/scan")
async def trigger_scan(body: ScanBody, user: dict = Depends(security.guarded)) -> dict:
    job_id = await jobs.enqueue(
        jobs.NAVIDROME_SCAN,
        {"full": body.full},
        priority=jobs.PRIORITY_NORMAL,
        dedupe_key="scan:navidrome",
    )
    return {"job": job_id}


@router.post("/import-staging")
async def import_staging(user: dict = Depends(security.guarded)) -> dict:
    job_id = await jobs.enqueue(
        jobs.IMPORT_STAGING, priority=jobs.PRIORITY_NORMAL, dedupe_key="import:staging"
    )
    return {"job": job_id}


# --------------------------------------------------------------- Diagnose
@router.get("/preflight")
async def preflight_report(user: dict = Depends(security.current_user)) -> dict:
    """Passt die Konfiguration zum System? Vor dem ersten Download aufrufen."""
    return await preflight.run()


@router.get("/diagnostics")
async def diagnostics(user: dict = Depends(security.current_user)) -> dict:
    nd, dz, dx, tools = await asyncio.gather(
        navidrome.server_info(),
        deezer.healthy(),
        deemix.probe(),
        ffmpeg.available(),
    )
    return {
        "navidrome": nd,
        "deezer": {"reachable": dz},
        "deemix": dx,
        "tools": tools,
        "paths": {
            "music": {"path": str(settings.music_dir), "exists": settings.music_dir.exists()},
            "staging": {"path": str(settings.staging_dir), "exists": settings.staging_dir.exists()},
            "quarantine": {
                "path": str(settings.quarantine_dir),
                "exists": settings.quarantine_dir.exists(),
            },
            "database": {"path": str(settings.db_path), "exists": settings.db_path.exists()},
        },
        "staging_files": len(downloader.scan_audio(settings.staging_dir)),
    }


class TransportBody(BaseModel):
    method: str = Field(pattern="^(GET|POST)$")
    path: str = Field(min_length=1, max_length=200)
    style: str = Field(pattern="^(json|query)$")


@router.post("/diagnostics/deemix-transport")
async def set_deemix_transport(
    body: TransportBody, user: dict = Depends(security.guarded)
) -> dict:
    await deemix.set_transport(body.method, body.path, body.style)
    return {"ok": True}
