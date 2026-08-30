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


async def _cached(key: str, factory, ttl: int | None = None) -> Any:
    ttl = ttl if ttl is not None else settings.search_cache_ttl
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = await factory()
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
