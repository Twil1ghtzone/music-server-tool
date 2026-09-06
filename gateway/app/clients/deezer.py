"""Deezer-Katalog ueber die oeffentliche REST-API.

Bewusste Architekturentscheidung: die SUCHE laeuft nicht ueber Deemix.
api.deezer.com ist eine dokumentierte, stabile, auth-freie REST-Schnittstelle,
waehrend die Deemix-API zwischen Forks und Versionen wandert. Deemix wird nur
noch fuer das gebraucht, was sonst niemand kann - den eigentlichen Download.

Das halbiert die Latenz im Suchpfad und macht ihn unabhaengig davon, ob der
Deemix-Container gerade gesund ist.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from ..config import settings
from ..logging_conf import get_logger
from . import http

log = get_logger("deezer")

PROVIDER = "dz"

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = asyncio.Lock()
_CACHE_MAX = 512


# Leere Ergebnisse nur ganz kurz halten. Eine einzelne Stoerung - ein
# abgebrochener Handshake, eine Zeitueberschreitung - wuerde sonst fuer die
# volle Cache-Dauer festgeschrieben: der Katalog bliebe minutenlang leer,
# obwohl Deezer laengst wieder antwortet, und niemand kaeme auf die Idee,
# dass die Ursache ein Cache-Eintrag von vor fuenf Minuten ist.
EMPTY_TTL = 15


async def _cached(key: str, factory, ttl: int | None = None) -> Any:
    ttl = ttl if ttl is not None else settings.search_cache_ttl
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = await factory()
    if not value:
        ttl = min(ttl, EMPTY_TTL)
    async with _cache_lock:
        if len(_cache) > _CACHE_MAX:
            # Billigste brauchbare Verdraengung: abgelaufene Eintraege raus.
            for k in [k for k, v in _cache.items() if v[0] <= now][:_CACHE_MAX // 2]:
                _cache.pop(k, None)
            if len(_cache) > _CACHE_MAX:
                _cache.clear()
        _cache[key] = (now + ttl, value)
    return value


def normalize(track: dict[str, Any]) -> dict[str, Any]:
    """Deezer-Track -> internes, providerneutrales Format."""
    album = track.get("album") or {}
    artist = track.get("artist") or {}
    return {
        "provider": PROVIDER,
        "provider_id": str(track.get("id")),
        "title": track.get("title_short") or track.get("title") or "",
        "artist": artist.get("name") or "",
        "album": album.get("title") or "",
        "album_artist": artist.get("name") or "",
        "duration": int(track.get("duration") or 0),
        "track_no": track.get("track_position"),
        "disc_no": track.get("disk_number"),
        "year": _year(track.get("release_date") or album.get("release_date")),
        "isrc": track.get("isrc"),
        "cover_url": album.get("cover_medium") or album.get("cover") or None,
        "source_url": track.get("link") or f"https://www.deezer.com/track/{track.get('id')}",
        "explicit": bool(track.get("explicit_lyrics")),
        "rank": int(track.get("rank") or 0),
    }


def normalize_album(album: dict[str, Any]) -> dict[str, Any]:
    artist = album.get("artist") or {}
    return {
        "id": str(album.get("id")),
        "title": album.get("title") or "",
        "artist": artist.get("name") or "",
        "artist_id": str(artist.get("id")) if artist.get("id") else None,
        "year": _year(album.get("release_date")),
        "tracks": album.get("nb_tracks"),
        "duration": album.get("duration"),
        "record_type": album.get("record_type"),
        "explicit": bool(album.get("explicit_lyrics")),
        "md5_image": album.get("md5_image"),
        "source_url": album.get("link") or f"https://www.deezer.com/album/{album.get('id')}",
    }


def normalize_artist(artist: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(artist.get("id")),
        "name": artist.get("name") or "",
        "albums": artist.get("nb_album"),
        "fans": artist.get("nb_fan"),
        # Interpreten tragen kein md5_image. Die Pruefsumme steckt im Pfad
        # der Bild-URL - aber NUR in den Groessenvarianten. Das schlichte
        # "picture" ist eine Weiterleitung (api.deezer.com/artist/<id>/image)
        # und enthaelt sie nicht.
        "md5_image": _md5_aus_bild(
            artist.get("picture_medium") or artist.get("picture_xl")
            or artist.get("picture_big") or artist.get("picture_small")
            or artist.get("picture")
        ),
        "source_url": artist.get("link") or f"https://www.deezer.com/artist/{artist.get('id')}",
    }


def normalize_playlist(playlist: dict[str, Any]) -> dict[str, Any]:
    creator = playlist.get("creator") or playlist.get("user") or {}
    return {
        "id": str(playlist.get("id")),
        "title": playlist.get("title") or "",
        "creator": creator.get("name") or "",
        "tracks": playlist.get("nb_tracks"),
        "duration": playlist.get("duration"),
        "description": playlist.get("description") or "",
        "md5_image": playlist.get("md5_image") or _md5_aus_bild(
            playlist.get("picture_medium") or playlist.get("picture_xl")
            or playlist.get("picture")
        ),
        "source_url": playlist.get("link") or f"https://www.deezer.com/playlist/{playlist.get('id')}",
    }


_MD5_IM_PFAD = re.compile(r"/images/[a-z]+/([0-9a-f]{32})/")


def _md5_aus_bild(url: str | None) -> str | None:
    treffer = _MD5_IM_PFAD.search(url or "")
    return treffer.group(1) if treffer else None


def _year(value: str | None) -> int | None:
    if not value or len(value) < 4:
        return None
    try:
        return int(value[:4])
    except ValueError:
        return None


async def search_tracks(query: str, limit: int | None = None) -> list[dict[str, Any]]:
    limit = limit or settings.provider_result_limit
    query = query.strip()
    if not query:
        return []

    async def _fetch() -> list[dict[str, Any]]:
        try:
            resp = await http.deezer().get(
                "/search", params={"q": query, "limit": min(limit, 50), "order": "RANKING"}
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("Deezer-Suche fehlgeschlagen (%s): %s", query, exc)
            return []
        return [normalize(t) for t in (data.get("data") or []) if t.get("readable", True)]

    return await _cached(f"search:{limit}:{query.lower()}", _fetch)


async def get_track(track_id: str) -> dict[str, Any] | None:
    async def _fetch() -> dict[str, Any] | None:
        try:
            resp = await http.deezer().get(f"/track/{track_id}")
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("Deezer-Track %s nicht abrufbar: %s", track_id, exc)
            return None
        if data.get("error"):
            return None
        return normalize(data)

    return await _cached(f"track:{track_id}", _fetch, ttl=3600)


# ------------------------------------------------------------------ Katalog
# Alles ueber dieselbe _cached-Huelle wie die Titelsuche: die Deezer-API ist
# schnell, aber jeder Seitenaufruf wuerde sonst mehrere Roundtrips ausloesen.

async def _hole(pfad: str, ttl: int | None = None, **params) -> dict[str, Any] | None:
    schluessel = f"{pfad}:{sorted(params.items())}"

    async def _fetch() -> dict[str, Any] | None:
        try:
            resp = await http.deezer().get(pfad, params=params or None)
            resp.raise_for_status()
            daten = resp.json()
        except Exception as exc:
            log.warning("Deezer %s fehlgeschlagen: %s", pfad, exc)
            return None
        if isinstance(daten, dict) and daten.get("error"):
            log.warning("Deezer %s meldet %s", pfad, daten["error"])
            return None
        return daten

    return await _cached(schluessel, _fetch, ttl)


async def artist(artist_id: str) -> dict[str, Any] | None:
    daten = await _hole(f"/artist/{artist_id}", ttl=3600)
    return normalize_artist(daten) if daten else None


async def artist_top(artist_id: str, limit: int = 25) -> list[dict[str, Any]]:
    daten = await _hole(f"/artist/{artist_id}/top", ttl=1800, limit=limit)
    return [normalize(t) for t in (daten or {}).get("data", [])]


async def artist_albums(artist_id: str, limit: int = 60) -> list[dict[str, Any]]:
    daten = await _hole(f"/artist/{artist_id}/albums", ttl=1800, limit=limit)
    alben = [normalize_album(a) for a in (daten or {}).get("data", [])]
    # Neueste zuerst; Alben ohne Jahr ans Ende.
    return sorted(alben, key=lambda a: a["year"] or 0, reverse=True)


async def album(album_id: str) -> dict[str, Any] | None:
    daten = await _hole(f"/album/{album_id}", ttl=3600)
    if not daten:
        return None
    kopf = normalize_album(daten)
    kopf["genres"] = [g.get("name") for g in (daten.get("genres") or {}).get("data", [])]
    # Die Titel im Album tragen das Albumbild nicht selbst - nachreichen,
    # damit die Oberflaeche nicht pro Titel nachfragen muss.
    kopf["tracklist"] = [
        {**normalize(t), "album": kopf["title"], "md5_image": kopf["md5_image"]}
        for t in (daten.get("tracks") or {}).get("data", [])
    ]
    return kopf


async def playlist(playlist_id: str) -> dict[str, Any] | None:
    daten = await _hole(f"/playlist/{playlist_id}", ttl=1800)
    if not daten:
        return None
    kopf = normalize_playlist(daten)
    kopf["tracklist"] = [
        {**normalize(t), "md5_image": (t.get("album") or {}).get("md5_image")}
        for t in (daten.get("tracks") or {}).get("data", [])
    ]
    return kopf


_SUCHE = {
    "track": ("/search", normalize),
    "album": ("/search/album", normalize_album),
    "artist": ("/search/artist", normalize_artist),
    "playlist": ("/search/playlist", normalize_playlist),
}


async def search(query: str, kind: str = "track", limit: int = 25) -> list[dict[str, Any]]:
    """Katalogsuche nach Titel, Album, Interpret oder Playlist."""
    if kind not in _SUCHE or not query.strip():
        return []
    pfad, wandeln = _SUCHE[kind]
    daten = await _hole(pfad, q=query.strip(), limit=min(limit, 50))
    return [wandeln(e) for e in (daten or {}).get("data", [])]


async def preview_url(track_id: str) -> str | None:
    """30-Sekunden-Ausschnitt eines Titels, direkt aus der Katalogantwort."""
    daten = await _hole(f"/track/{track_id}", ttl=3600)
    return (daten or {}).get("preview") or None


def dominant_artist(tracks: list[dict[str, Any]], sample: int = 6) -> str | None:
    """Haeufigster Interpret unter den vordersten Treffern.

    Dient als Rechtschreibkorrektur: Deezer findet bei "marc forster" trotzdem
    Mark Forster, Navidromes Suche dagegen ist buchstabengetreu und liefert
    nichts. Der Katalogtreffer verraet also die richtige Schreibweise, mit der
    sich die lokale Suche wiederholen laesst.
    """
    counts: dict[str, int] = {}
    for track in tracks[:sample]:
        name = (track.get("artist") or "").strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def looks_like(a: str, b: str) -> bool:
    """Grob gleich, wenn man Gross-/Kleinschreibung und Zeichen ignoriert."""
    norm = lambda s: "".join(ch for ch in (s or "").lower() if ch.isalnum())
    return norm(a) == norm(b)


async def healthy() -> bool:
    try:
        resp = await http.deezer().get("/track/3135556", timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False
