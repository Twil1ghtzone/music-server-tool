"""Uebersicht: Gesamtzustand, letzte Alben, Live-Ereignisse.

Was das Dashboard beim Oeffnen braucht - und sonst nichts.
"""
from __future__ import annotations

import asyncio
import json
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from .. import events, security
from ..clients import navidrome
from ..config import settings
from ..db import db
from ..logging_conf import get_logger
from ..services import dedupe, downloader, jobs, scanner

log = get_logger("api.overview")
router = APIRouter(prefix="/api", tags=["overview"])


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
        "worker": await jobs.worker_status(),
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
