"""Dashboard-API: Status, Live-Ereignisse, Warteschlange, Suche, Diagnose."""
from __future__ import annotations

import asyncio
import json
import shutil

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import events, preflight, security
from ..clients import deemix, deezer, navidrome
from ..config import settings
from ..db import db
from ..logging_conf import get_logger
from ..services import dedupe, downloader, ffmpeg, jobs, scanner
from ..subsonic import proxy as subsonic_proxy

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
async def retry_job(job_id: int, user: dict = Depends(security.guarded_admin)) -> dict:
    if not await jobs.retry(job_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Job ist nicht wiederholbar")
    return {"ok": True}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, user: dict = Depends(security.guarded_admin)) -> dict:
    if not await jobs.cancel(job_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Job laeuft bereits oder ist beendet")
    return {"ok": True}


# ------------------------------------------------------- Downloads / Suche
@router.get("/queue")
async def queue(user: dict = Depends(security.current_user), limit: int = 100) -> dict:
    return {"items": await downloader.queue_overview(limit)}


@router.delete("/queue/{virtual_id}")
async def forget_queue_entry(
    virtual_id: str, user: dict = Depends(security.guarded_admin)
) -> dict:
    try:
        await downloader.forget_track(virtual_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"ok": True}


@router.post("/queue/clear-failed")
async def clear_failed_queue(user: dict = Depends(security.guarded_admin)) -> dict:
    return {"removed": await downloader.forget_failed()}


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

    # Navidromes Suche ist buchstabengetreu: "marc forster" findet lokal
    # nichts, obwohl die Titel da sind. Der Katalog kennt die richtige
    # Schreibweise - damit wird die lokale Suche einmal wiederholt.
    corrected: str | None = None
    if not local_rows and catalog_rows:
        artist = deezer.dominant_artist(catalog_rows)
        if artist and not deezer.looks_like(artist, q):
            try:
                local_rows = await navidrome.search_songs(artist, count=25)
                if local_rows:
                    corrected = artist
            except Exception as exc:
                log.debug("Korrigierte Suche fehlgeschlagen: %s", exc)

    known = await db.fetch_all(
        "SELECT provider_id, state, navidrome_id, error FROM virtual_track WHERE provider = ?",
        (deezer.PROVIDER,),
    ) if catalog_rows else []
    by_id = {row["provider_id"]: row for row in known}
    for item in catalog_rows:
        item["known"] = by_id.get(item["provider_id"])

    return {"local": local_rows, "catalog": catalog_rows, "corrected": corrected}


@router.post("/download")
async def request_download(
    body: DownloadBody, user: dict = Depends(security.guarded)
) -> dict:
    try:
        return await downloader.request_track(body.provider_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/scan")
async def trigger_scan(body: ScanBody, user: dict = Depends(security.guarded_admin)) -> dict:
    # Ohne Zugangsdaten waere der Job zum Scheitern verurteilt. Lieber hier
    # sagen warum, als drei Fehlermeldungen im Ereignisprotokoll erzeugen.
    if not await navidrome.has_credentials_async():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Navidrome-Scan braucht Zugangsdaten. Trag sie unter Diagnose ein - "
            "oder melde dich einmal mit einem Musik-Client auf Port 8080 an, dann "
            "uebernimmt der Gateway dessen Token. Navidrome erkennt neue Dateien "
            "ohnehin selbst (ND_MONITORCHANGES).",
        )
    job_id = await jobs.enqueue(
        jobs.NAVIDROME_SCAN,
        {"full": body.full},
        priority=jobs.PRIORITY_NORMAL,
        dedupe_key="scan:navidrome",
    )
    return {"job": job_id}


@router.post("/import-staging")
async def import_staging(user: dict = Depends(security.guarded_admin)) -> dict:
    job_id = await jobs.enqueue(
        jobs.IMPORT_STAGING, priority=jobs.PRIORITY_NORMAL, dedupe_key="import:staging"
    )
    return {"job": job_id}


# --------------------------------------------------------------- Diagnose
class NavidromeCredentialsBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


@router.get("/navidrome/credentials")
async def navidrome_credentials(user: dict = Depends(security.admin_only)) -> dict:
    return await navidrome.credentials_info()


@router.post("/navidrome/credentials")
async def set_navidrome_credentials(
    body: NavidromeCredentialsBody, user: dict = Depends(security.guarded_admin)
) -> dict:
    """Navidrome-Zugang von Hand hinterlegen.

    Wird sofort gegen Navidrome geprueft - ein Tippfehler faellt hier auf und
    nicht erst beim naechsten Download.
    """
    if not await navidrome.reachable():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Navidrome ist unter {settings.navidrome_url} nicht erreichbar",
        )
    try:
        await navidrome.set_credentials(body.username, body.password)
    except navidrome.NavidromeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await events.emit(f"Navidrome-Zugang hinterlegt: {body.username}", category="system")
    return await navidrome.credentials_info()


@router.delete("/navidrome/credentials")
async def delete_navidrome_credentials(user: dict = Depends(security.guarded_admin)) -> dict:
    await navidrome.clear_credentials()
    return await navidrome.credentials_info()


@router.get("/logs")
async def logs(
    user: dict = Depends(security.admin_only),
    level: str = Query("all"),
    category: str = Query("all"),
    q: str = Query("", max_length=200),
    limit: int = Query(300, le=1000),
) -> dict:
    return {
        "entries": await events.search(level, category, q or None, limit),
        "categories": await events.categories(),
    }


@router.get("/client-activity")
async def client_activity(user: dict = Depends(security.admin_only)) -> dict:
    """Die letzten Zugriffe von Musik-Clients auf den Subsonic-Endpunkt.

    Beantwortet ohne Raten, ob ein Client ueberhaupt hier ankommt. Bleibt die
    Liste leer, waehrend im Client gesucht wird, zeigt er woanders hin.
    """
    return {"requests": subsonic_proxy.recent_activity()}


@router.get("/client-test")
async def client_test(
    request: Request,
    user: dict = Depends(security.admin_only),
    q: str = Query("Mark Forster", max_length=200),
) -> dict:
    """Fragt den eigenen Subsonic-Endpunkt so ab, wie es ein Musik-Client tut.

    Damit laesst sich die Frage "liegt es am Gateway oder an meinem Client?"
    beantworten, ohne im Client herumzuraten: was hier herauskommt, bekommt
    auch Substreamer - vorausgesetzt, es zeigt auf Port 8080.
    """
    if not await navidrome.has_credentials_async():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Fuer den Test werden Navidrome-Zugangsdaten gebraucht - "
            "weiter oben auf dieser Seite eintragen.",
        )

    params = await navidrome._credentials()
    params.update({"query": q, "songCount": "20", "f": "json"})
    # Ueber die Schleife, nicht ueber den externen Namen: der loest im
    # Container womoeglich nicht auf. Der Port kommt aus der Anfrage, damit
    # der Test auch beim lokalen Entwicklungsstart und hinter einem
    # Reverse-Proxy die richtige Stelle trifft.
    port = request.url.port or (443 if request.url.scheme == "https" else 8080)
    url = f"http://127.0.0.1:{port}/rest/search3.view"

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.get(url, params=params)
        body = response.json().get("subsonic-response") or {}
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Der eigene Subsonic-Endpunkt antwortet nicht: {exc}",
        ) from exc

    if body.get("status") != "ok":
        error = body.get("error") or {}
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Subsonic-Fehler {error.get('code')}: {error.get('message')}",
        )

    songs = (body.get("searchResult3") or {}).get("song") or []
    if not isinstance(songs, list):
        songs = [songs]
    virtual = [s for s in songs if str(s.get("id", "")).startswith("mgv-")]
    local = [s for s in songs if not str(s.get("id", "")).startswith("mgv-")]

    return {
        "query": q,
        "local": len(local),
        "virtual": len(virtual),
        "beispiele": [s.get("title") for s in virtual[:5]],
        "url": f"http://<server>:8080/rest/search3.view?query={q}",
    }


class ArlBody(BaseModel):
    arl: str = Field(min_length=8, max_length=512)


@router.get("/deemix/arl")
async def deemix_arl(user: dict = Depends(security.admin_only)) -> dict:
    return await deemix.arl_info()


@router.post("/deemix/arl")
async def set_deemix_arl(body: ArlBody, user: dict = Depends(security.guarded_admin)) -> dict:
    """ARL hinterlegen, damit der Gateway sich selbst bei Deemix anmelden kann.

    Deemix haelt die Deezer-Sitzung pro HTTP-Sitzung. Dass die Weboberflaeche
    angemeldet ist, hilft dem Gateway nicht - er ist ein anderer Client.
    """
    try:
        info = await deemix.set_arl(body.arl)
    except deemix.DeemixUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except deemix.DeemixRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await events.emit(
        f"Bei Deemix angemeldet als {info.get('user') or 'unbekannt'}", category="system"
    )
    return {**info, **await deemix.arl_info()}


@router.delete("/deemix/arl")
async def delete_deemix_arl(user: dict = Depends(security.guarded_admin)) -> dict:
    await deemix.clear_arl()
    return await deemix.arl_info()


@router.get("/preflight")
async def preflight_report(user: dict = Depends(security.admin_only)) -> dict:
    """Passt die Konfiguration zum System? Vor dem ersten Download aufrufen."""
    return await preflight.run()


@router.get("/diagnostics")
async def diagnostics(user: dict = Depends(security.admin_only)) -> dict:
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
    body: TransportBody, user: dict = Depends(security.guarded_admin)
) -> dict:
    await deemix.set_transport(body.method, body.path, body.style)
    return {"ok": True}
