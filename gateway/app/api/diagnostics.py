"""Diagnose: Zugangsdaten, Startpruefung, Erreichbarkeit, Client-Sicht.

Alles, was die Frage "warum geht es nicht?" beantwortet - und die beiden
Zugaenge, ohne die der Gateway nichts tun kann.
"""
from __future__ import annotations

import asyncio

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .. import events, preflight, security
from ..clients import deemix, deezer, navidrome
from ..config import settings
from ..logging_conf import get_logger
from ..services import downloader, ffmpeg
from ..subsonic import proxy as subsonic_proxy

log = get_logger("api.diagnostics")
router = APIRouter(prefix="/api", tags=["diagnostics"])


class NavidromeCredentialsBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class ArlBody(BaseModel):
    arl: str = Field(min_length=8, max_length=512)


class TransportBody(BaseModel):
    method: str = Field(pattern="^(GET|POST)$")
    path: str = Field(min_length=1, max_length=200)
    style: str = Field(pattern="^(json|query)$")


# ------------------------------------------------------- Navidrome-Zugang
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


# ----------------------------------------------------------- Deemix-Zugang
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


@router.post("/deemix/arl/reveal")
async def reveal_deemix_arl(user: dict = Depends(security.guarded_admin)) -> dict:
    """Gibt den hinterlegten ARL heraus, damit man ihn kopieren kann.

    POST und nicht GET: das ist keine Abfrage, sondern eine Handlung mit
    Folgen - sie steht im Protokoll und laesst sich nicht aus Versehen
    ueber einen Link ausloesen.
    """
    arl = await deemix.arl_klartext()
    if not arl:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Es ist kein ARL hinterlegt.")
    await events.emit(
        f"ARL im Klartext abgerufen von {user.get('username')}",
        category="auth", level="warn",
    )
    return {"arl": arl}


@router.delete("/deemix/arl")
async def delete_deemix_arl(user: dict = Depends(security.guarded_admin)) -> dict:
    await deemix.clear_arl()
    return await deemix.arl_info()


# ------------------------------------------------------------ Client-Sicht
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


# ------------------------------------------------------------- Systemsicht
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


@router.post("/diagnostics/deemix-transport")
async def set_deemix_transport(
    body: TransportBody, user: dict = Depends(security.guarded_admin)
) -> dict:
    await deemix.set_transport(body.method, body.path, body.style)
    return {"ok": True}
