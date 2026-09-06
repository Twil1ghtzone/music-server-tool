"""Katalog: Interpret, Album, Playlist — plus Cover und Hoerprobe.

Warum Bilder und Ton durch den Gateway laufen und nicht direkt vom Client zu
Deezer: die Content-Security-Policy des Dashboards steht auf 'self'. Fremde
Hosts freizugeben waere der bequeme Weg und wuerde die Richtlinie fuer alle
Seiten aufweichen. Durchreichen kostet etwas Bandbreite und haelt sie dicht.

Der wichtige Teil daran ist, was hier NICHT passiert: es gibt keinen Endpunkt,
der eine beliebige URL entgegennimmt und abruft. Das waere ein offener
Weiterleiter - jeder angemeldete Benutzer koennte damit interne Adressen des
Servers abfragen. Stattdessen werden die Ziel-URLs aus geprueften Bausteinen
zusammengesetzt und das Ergebnis gegen eine Hostliste gehalten.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .. import security
from ..clients import deezer, http
from ..config import settings
from ..db import db
from ..logging_conf import get_logger

log = get_logger("api.catalog")
router = APIRouter(prefix="/api/catalog", tags=["catalog"])

# Bilder liegen unter einer festen, vorhersagbaren Adresse. Damit brauchen
# wir fuer ein Cover keinen zusaetzlichen API-Aufruf.
BILD_HOST = "https://e-cdns-images.dzcdn.net"
BILD_ARTEN = {"cover", "artist", "playlist", "misc"}
GROESSEN = {56, 120, 250, 500, 1000}
_MD5 = re.compile(r"^[0-9a-f]{32}$")
_ID = re.compile(r"^[0-9]{1,20}$")

# Hoerproben kommen ausschliesslich von diesen Hosts.
TON_HOSTS = ("cdnt-preview.dzcdn.net", "cdns-preview-0.dzcdn.net", "e-cdn-preview.dzcdn.net")


def _pruefe_id(wert: str, was: str) -> str:
    if not _ID.match(wert):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Ungueltige {was}")
    return wert


async def _markiere(tracks: list[dict]) -> list[dict]:
    """Traegt zu jedem Titel ein, was der Gateway ueber ihn weiss.

    Ohne das zeigt die Oberflaeche auf jeder Album- und Interpretenseite einen
    Ladeknopf - auch fuer Titel, die laengst in der Bibliothek liegen. Die
    Suche macht das schon; hier fehlte es. Eine Abfrage fuer die ganze Seite,
    nicht eine je Titel.
    """
    if not tracks:
        return tracks
    ids = [t["provider_id"] for t in tracks if t.get("provider_id")]
    if not ids:
        return tracks
    platzhalter = ",".join("?" * len(ids))
    zeilen = await db.fetch_all(
        "SELECT provider_id, state, navidrome_id, error FROM virtual_track "
        f"WHERE provider = ? AND provider_id IN ({platzhalter})",
        (deezer.PROVIDER, *ids),
    )
    nach_id = {z["provider_id"]: z for z in zeilen}
    for track in tracks:
        track["known"] = nach_id.get(track.get("provider_id"))
    return tracks


# --------------------------------------------------------------- Suche
@router.get("/search")
async def suche(
    user: dict = Depends(security.current_user),
    q: str = Query(min_length=1, max_length=200),
    kind: str = Query("track", pattern="^(track|album|artist|playlist)$"),
    limit: int = Query(25, ge=1, le=50),
) -> dict:
    treffer = await deezer.search(q, kind, limit)
    if kind == "track":
        treffer = await _markiere(treffer)
    return {"kind": kind, "query": q, "results": treffer}


# --------------------------------------------------------------- Detailseiten
@router.get("/artist/{artist_id}")
async def interpret(artist_id: str, user: dict = Depends(security.current_user)) -> dict:
    _pruefe_id(artist_id, "Interpreten-ID")
    kopf = await deezer.artist(artist_id)
    if not kopf:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interpret nicht gefunden")
    return {
        "artist": kopf,
        "top": await _markiere(await deezer.artist_top(artist_id)),
        "albums": await deezer.artist_albums(artist_id),
    }


@router.get("/album/{album_id}")
async def alben(album_id: str, user: dict = Depends(security.current_user)) -> dict:
    _pruefe_id(album_id, "Album-ID")
    kopf = await deezer.album(album_id)
    if not kopf:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Album nicht gefunden")
    kopf["tracklist"] = await _markiere(kopf.get("tracklist") or [])
    return kopf


@router.get("/playlist/{playlist_id}")
async def playlist(playlist_id: str, user: dict = Depends(security.current_user)) -> dict:
    _pruefe_id(playlist_id, "Playlist-ID")
    kopf = await deezer.playlist(playlist_id)
    if not kopf:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Playlist nicht gefunden")
    kopf["tracklist"] = await _markiere(kopf.get("tracklist") or [])
    return kopf


# --------------------------------------------------------------- Cover
@router.get("/cover/{art}/{md5}")
async def cover(
    art: str, md5: str,
    user: dict = Depends(security.current_user),
    s: int = Query(250),
) -> Response:
    """Bild durchreichen und auf Platte zwischenspeichern.

    Art und Pruefsumme sind streng validiert - daraus laesst sich keine
    andere Adresse als eine Bild-URL von Deezer bauen.
    """
    if art not in BILD_ARTEN or not _MD5.match(md5):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ungueltige Bildkennung")
    if s not in GROESSEN:
        s = 250

    ziel = settings.cache_dir / "covers" / f"{art}-{md5}-{s}.jpg"
    if ziel.exists():
        return Response(
            ziel.read_bytes(), media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=604800, immutable"},
        )

    url = f"{BILD_HOST}/images/{art}/{md5}/{s}x{s}-000000-80-0-0.jpg"
    try:
        antwort = await http.plain().get(url)
        antwort.raise_for_status()
    except Exception as exc:
        log.debug("Cover %s nicht ladbar: %s", url, exc)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cover nicht verfuegbar") from exc

    try:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_suffix(".part")
        tmp.write_bytes(antwort.content)
        tmp.replace(ziel)
    except OSError as exc:            # Kein Platz o.ae. - Bild trotzdem liefern.
        log.debug("Cover nicht zwischengespeichert: %s", exc)

    return Response(
        antwort.content, media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


# --------------------------------------------------------------- Hoerprobe
@router.get("/preview/{track_id}")
async def hoerprobe(track_id: str, user: dict = Depends(security.current_user)) -> Response:
    """30-Sekunden-Ausschnitt, den Deezer selbst oeffentlich anbietet.

    Die URL kommt aus der Katalogantwort und wird gegen die bekannten
    Vorschau-Hosts geprueft, bevor sie abgerufen wird.
    """
    _pruefe_id(track_id, "Titel-ID")
    url = await deezer.preview_url(track_id)
    if not url:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fuer diesen Titel gibt es keine Hoerprobe")

    host = urlparse(url).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in TON_HOSTS):
        log.warning("Hoerprobe von unerwartetem Host abgelehnt: %s", host)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Unerwartete Quelle")

    try:
        anfrage = http.plain().build_request("GET", url)
        antwort = await http.plain().send(anfrage, stream=True)
        antwort.raise_for_status()
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Hoerprobe nicht abrufbar: {exc}") from exc

    return StreamingResponse(
        antwort.aiter_raw(),
        media_type=antwort.headers.get("content-type", "audio/mpeg"),
        headers={"Cache-Control": "private, max-age=3600"},
        background=BackgroundTask(antwort.aclose),
    )
