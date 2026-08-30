"""Virtuelle IDs und ihr dauerhaftes Mapping auf Navidrome-IDs.

Der wichtigste Vertrag im ganzen Proxy: eine einmal ausgelieferte virtuelle ID
muss fuer immer aufloesbar bleiben. Clients legen sie in Playlists, in der
Warteschlange und im Offline-Cache ab; ein verworfenes Mapping erzeugt tote
Eintraege auf Geraeten, an die wir nicht herankommen.

Deshalb: virtual_track-Zeilen werden nie geloescht, nur fortgeschrieben.
"""
from __future__ import annotations

import re
from typing import Any

from ..db import db

PREFIX = "mgv"
_PATTERN = re.compile(rf"^{PREFIX}-([a-z0-9]+)-([A-Za-z0-9_.:-]+)$")

# Hot-Path-Cache: id -> navidrome_id. Nur aufgeloeste Mappings landen hier,
# denn nur die sind unveraenderlich. Unaufgeloeste muessen frisch gelesen
# werden, weil der Worker sie jederzeit fertigstellen kann.
_resolved: dict[str, str] = {}
_RESOLVED_MAX = 20000


def make(provider: str, provider_id: str) -> str:
    return f"{PREFIX}-{provider}-{provider_id}"


def is_virtual(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX + "-")  # type: ignore[union-attr]


def parse(value: str) -> tuple[str, str] | None:
    match = _PATTERN.match(value or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def cache_resolved(virtual_id: str, navidrome_id: str) -> None:
    if len(_resolved) > _RESOLVED_MAX:
        _resolved.clear()
    _resolved[virtual_id] = navidrome_id


def cached(virtual_id: str) -> str | None:
    return _resolved.get(virtual_id)


async def resolve(virtual_id: str) -> str | None:
    """Virtuelle ID -> echte Navidrome-ID, sofern schon importiert."""
    hit = _resolved.get(virtual_id)
    if hit:
        return hit
    row = await db.fetch_one(
        "SELECT navidrome_id FROM virtual_track WHERE id = ? AND navidrome_id IS NOT NULL",
        (virtual_id,),
    )
    if row and row["navidrome_id"]:
        cache_resolved(virtual_id, row["navidrome_id"])
        return row["navidrome_id"]
    return None


async def load(virtual_id: str) -> dict[str, Any] | None:
    return await db.fetch_one("SELECT * FROM virtual_track WHERE id = ?", (virtual_id,))


async def upsert(track: dict[str, Any]) -> str:
    """Legt einen virtuellen Track an bzw. frischt seine Metadaten auf.
    navidrome_id und state bleiben dabei unangetastet."""
    vid = make(track["provider"], track["provider_id"])
    await db.execute(
        """
        INSERT INTO virtual_track
            (id, provider, provider_id, title, artist, album, album_artist,
             duration, track_no, disc_no, year, isrc, cover_url, source_url)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            title       = excluded.title,
            artist      = excluded.artist,
            album       = excluded.album,
            album_artist= excluded.album_artist,
            duration    = excluded.duration,
            cover_url   = excluded.cover_url,
            source_url  = excluded.source_url,
            updated_at  = datetime('now')
        """,
        (
            vid,
            track["provider"],
            track["provider_id"],
            track.get("title") or "",
            track.get("artist") or "",
            track.get("album") or "",
            track.get("album_artist") or track.get("artist") or "",
            int(track.get("duration") or 0),
            track.get("track_no"),
            track.get("disc_no"),
            track.get("year"),
            track.get("isrc"),
            track.get("cover_url"),
            track.get("source_url"),
        ),
    )
    return vid


async def upsert_many(tracks: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for track in tracks:
        ids.append(await upsert(track))
    return ids


async def set_state(virtual_id: str, state: str, *, error: str | None = None) -> None:
    await db.execute(
        "UPDATE virtual_track SET state = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
        (state, error, virtual_id),
    )


async def mark_ready(virtual_id: str, navidrome_id: str, local_path: str | None) -> None:
    await db.execute(
        "UPDATE virtual_track SET state = 'ready', navidrome_id = ?, local_path = ?, "
        "error = NULL, updated_at = datetime('now') WHERE id = ?",
        (navidrome_id, local_path, virtual_id),
    )
    cache_resolved(virtual_id, navidrome_id)


async def count_play_request(virtual_id: str) -> None:
    await db.execute(
        "UPDATE virtual_track SET play_requests = play_requests + 1, "
        "updated_at = datetime('now') WHERE id = ?",
        (virtual_id,),
    )


async def existing_provider_ids(provider: str, provider_ids: list[str]) -> dict[str, dict]:
    """Bulk-Lookup fuer die Suche: welche Treffer kennen wir schon?"""
    if not provider_ids:
        return {}
    placeholders = ",".join("?" * len(provider_ids))
    rows = await db.fetch_all(
        f"SELECT * FROM virtual_track WHERE provider = ? AND provider_id IN ({placeholders})",
        [provider, *provider_ids],
    )
    return {row["provider_id"]: row for row in rows}
