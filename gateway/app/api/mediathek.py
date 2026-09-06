"""Die Navidrome-Bibliothek im Dashboard.

Bis hierher zeigte das Werkzeug nur, was es selbst indiziert hat: Pfade,
Groessen, Duplikate. Was tatsaechlich in der Musiksammlung steht - Alben mit
Coverbild, nach Interpret geordnet - stand nur in Navidrome.

Diese Seite holt es von dort. Nicht als zweite Bibliothek: die Wahrheit
bleibt Navidrome, hier wird nur gezeigt. Alles laeuft ueber die
Zugangsdaten des Gateways, damit das Dashboard keine eigene Subsonic-Sitzung
braucht.

Cover kommen ueber diesen Server, nicht direkt von Navidrome: die
Content-Security-Policy steht auf 'self'. Sie werden auf Platte
zwischengespeichert - und ein fehlendes Cover wird ebenfalls gemerkt.
Ohne das fragt die Oberflaeche bei jedem Neuzeichnen wieder nach, und
Navidromes Log fuellt sich mit Warnungen.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .. import security
from ..clients import http, navidrome
from ..config import settings
from ..logging_conf import get_logger

log = get_logger("api.mediathek")
router = APIRouter(prefix="/api/mediathek", tags=["mediathek"])

# Navidrome-IDs sind kurze alphanumerische Zeichenfolgen. Streng pruefen:
# der Wert geht in eine Anfrage an Navidrome und in einen Dateinamen.
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Alben ohne Cover werden gemerkt, damit nicht bei jedem Neuzeichnen erneut
# gefragt wird.
_OHNE_COVER: set[str] = set()

SORTIERUNG = {
    "newest": "Zuletzt hinzugefuegt",
    "alphabeticalByName": "Album A-Z",
    "alphabeticalByArtist": "Interpret A-Z",
    "frequent": "Oft gehoert",
    "recent": "Zuletzt gehoert",
    "starred": "Favoriten",
    "random": "Zufall",
}


def _pruefe_id(wert: str) -> str:
    if not _ID.match(wert):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ungueltige Kennung")
    return wert


async def _verlangt_zugang() -> None:
    if not await navidrome.has_credentials_async():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Kein Navidrome-Zugang hinterlegt. Verbinde dein Konto oben auf dieser "
            "Seite - dann erscheint hier deine Mediathek.",
        )


@router.get("/status")
async def status_(user: dict = Depends(security.current_user)) -> dict:
    """Steht ein Zugang, und was sagt Navidrome dazu?"""
    zugang = await navidrome.credentials_info()
    if not zugang.get("configured"):
        return {**zugang, "online": await navidrome.reachable(), "server": None}
    try:
        server = await navidrome.server_info()
    except Exception as exc:
        server = {"error": str(exc)}
    return {**zugang, "online": await navidrome.reachable(), "server": server}


@router.get("/albums")
async def albums(
    user: dict = Depends(security.current_user),
    sort: str = Query("newest"),
    limit: int = Query(48, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    await _verlangt_zugang()
    if sort not in SORTIERUNG:
        sort = "newest"
    # Eine Seite mehr holen, als gezeigt wird: daran erkennt die Oberflaeche,
    # ob es weitergeht, ohne die Gesamtzahl zu kennen. Navidrome nennt sie
    # ueber diesen Endpunkt naemlich nicht.
    try:
        body = await navidrome.call(
            "getAlbumList2", {"type": sort, "size": limit + 1, "offset": offset})
    except navidrome.NavidromeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    liste = (body.get("albumList2") or {}).get("album") or []
    return {
        "albums": liste[:limit],
        "more": len(liste) > limit,
        "offset": offset,
        "limit": limit,
        "sort": sort,
    }


@router.get("/album/{album_id}")
async def album(album_id: str, user: dict = Depends(security.current_user)) -> dict:
    await _verlangt_zugang()
    _pruefe_id(album_id)
    try:
        body = await navidrome.call("getAlbum", {"id": album_id})
    except navidrome.NavidromeError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    daten = body.get("album") or {}
    if not daten:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Album nicht gefunden")
    return daten


@router.get("/artists")
async def artists(user: dict = Depends(security.current_user)) -> dict:
    await _verlangt_zugang()
    try:
        body = await navidrome.call("getArtists")
    except navidrome.NavidromeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    gruppen = (body.get("artists") or {}).get("index") or []
    return {"index": gruppen}


@router.get("/artist/{artist_id}")
async def artist(artist_id: str, user: dict = Depends(security.current_user)) -> dict:
    await _verlangt_zugang()
    _pruefe_id(artist_id)
    try:
        body = await navidrome.call("getArtist", {"id": artist_id})
    except navidrome.NavidromeError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    daten = body.get("artist") or {}
    if not daten:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interpret nicht gefunden")
    return daten


@router.get("/search")
async def suche(
    user: dict = Depends(security.current_user),
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(30, ge=1, le=100),
) -> dict:
    await _verlangt_zugang()
    try:
        body = await navidrome.call(
            "search3",
            {"query": q, "songCount": limit, "albumCount": limit, "artistCount": limit},
        )
    except navidrome.NavidromeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    treffer = body.get("searchResult3") or {}
    return {
        "songs": treffer.get("song") or [],
        "albums": treffer.get("album") or [],
        "artists": treffer.get("artist") or [],
    }


@router.get("/stream/{song_id}")
async def stream(song_id: str, request: Request,
                 user: dict = Depends(security.current_user)) -> StreamingResponse:
    """Reicht einen Titel aus Navidrome durch.

    Das Dashboard hat keine Subsonic-Sitzung - der Weg ueber /rest/ waere
    also nur mit den Zugangsdaten des Musik-Clients gangbar. Hier laeuft es
    mit denen des Gateways, wie beim Cover. Bereichsanfragen werden
    weitergereicht, damit der Browser springen kann.
    """
    await _verlangt_zugang()
    _pruefe_id(song_id)
    params = await navidrome._credentials()
    params.update({"id": song_id})

    kopf = {}
    bereich = request.headers.get("range")
    if bereich:
        kopf["Range"] = bereich

    client = http.navidrome()
    anfrage = client.build_request(
        "GET", "/rest/stream.view", params=params, headers=kopf, timeout=None)
    try:
        antwort = await client.send(anfrage, stream=True)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Navidrome antwortet nicht: {exc}") from exc
    if antwort.status_code >= 400:
        await antwort.aclose()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Titel nicht abrufbar")

    durchreichen = {
        k: v for k, v in antwort.headers.items()
        if k.lower() in ("content-length", "content-range", "accept-ranges", "content-type")
    }
    return StreamingResponse(
        antwort.aiter_bytes(),
        status_code=antwort.status_code,
        headers=durchreichen,
        media_type=antwort.headers.get("content-type", "audio/mpeg"),
        background=BackgroundTask(antwort.aclose),
    )


@router.get("/cover/{cover_id}")
async def cover(
    cover_id: str, user: dict = Depends(security.current_user),
    s: int = Query(300, ge=32, le=1200),
) -> Response:
    """Coverbild aus Navidrome, zwischengespeichert.

    Auch das Fehlen wird gemerkt. Ohne das fragt eine Rasteransicht mit
    fuenfzig Alben bei jedem Neuzeichnen fuenfzig Mal nach - und wenn
    Navidrome das Bild nicht aufloesen kann, schreibt es je Anfrage eine
    Warnung in sein Log.
    """
    _pruefe_id(cover_id)
    merker = f"{cover_id}:{s}"
    if merker in _OHNE_COVER:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Kein Cover")

    ziel = settings.cache_dir / "ndcovers" / f"{cover_id}-{s}.jpg"
    if ziel.exists():
        return Response(ziel.read_bytes(), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=604800"})

    await _verlangt_zugang()
    params = await navidrome._credentials()
    params.update({"id": cover_id, "size": str(s)})
    try:
        antwort = await http.navidrome().get(
            "/rest/getCoverArt.view", params=params, timeout=20.0)
        antwort.raise_for_status()
    except Exception as exc:
        _OHNE_COVER.add(merker)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cover nicht abrufbar") from exc

    typ = antwort.headers.get("content-type", "")
    if not typ.startswith("image/"):
        # Navidrome antwortet bei einem Fehler mit einer Subsonic-Meldung,
        # nicht mit einem Bild.
        _OHNE_COVER.add(merker)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Kein Cover hinterlegt")

    try:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_suffix(".part")
        tmp.write_bytes(antwort.content)
        tmp.replace(ziel)
    except OSError as exc:
        log.debug("Cover nicht zwischengespeichert: %s", exc)

    return Response(antwort.content, media_type=typ,
                    headers={"Cache-Control": "private, max-age=604800"})
