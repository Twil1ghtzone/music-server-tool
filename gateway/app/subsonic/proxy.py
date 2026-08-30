"""Der Subsonic-Proxy.

Grundhaltung: so wenig wie moeglich anfassen. Der Client spricht ab sofort
ausschliesslich mit diesem Dienst, also muss die gesamte Subsonic-API
funktionieren - inklusive Playlists, Cover, Scrobbling, Range-Requests und
Transcoding-Parametern. Alles, was wir nicht ausdruecklich abfangen, wird
byteweise durchgereicht und nie deserialisiert.

Abgefangen werden nur:
  search2 / search3   Ergaenzen der lokalen Treffer um Katalogtreffer
  getSong             Auskunft ueber einen noch nicht geladenen Titel
  getAlbum            Minimal-Album fuer einen virtuellen Titel
  stream / download   Loest den Download aus
  getCoverArt         Cover fuer virtuelle Titel

Zusaetzlich laeuft ueber JEDEN Request eine ID-Uebersetzung: eine virtuelle ID,
die inzwischen importiert wurde, wird transparent durch die echte Navidrome-ID
ersetzt. Genau das haelt Playlists und Warteschlangen auf den Geraeten am Leben.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Iterable

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from ..clients import deezer, http
from ..config import settings
from ..logging_conf import get_logger
from ..services import ffmpeg, jobs
from . import auth, ids, payload

log = get_logger("subsonic.proxy")

router = APIRouter()

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
}
DROP_REQUEST_HEADERS = HOP_BY_HOP | {"host", "content-length"}

# Parameter, die eine Track-/Album-/Artist-ID tragen koennen.
ID_PARAMS = {
    "id", "albumId", "artistId", "songId", "songIdToAdd", "songIdToRemove",
    "albumIdToAdd", "childId", "entryId",
}

INTERCEPTED = {
    "search2", "search3", "getSong", "getAlbum", "stream", "download", "getCoverArt",
}

# Wie lange der Proxy im Modus "stream" die Verbindung offen haelt.
STREAM_HOLD_SECONDS = 150.0
NOTICE_PACE_SECONDS = 7.0


# --------------------------------------------------------------- Parameter
async def _collect(request: Request) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Subsonic-Clients duerfen Parameter per Query ODER als Formular senden."""
    items: list[tuple[str, str]] = list(request.query_params.multi_items())
    ctype = (request.headers.get("content-type") or "").lower()
    if request.method == "POST" and "application/x-www-form-urlencoded" in ctype:
        form = await request.form()
        for key, value in form.multi_items():
            items.append((key, str(value)))
    flat = {k: v for k, v in items}
    return items, flat


async def _translate_ids(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Ersetzt bereits importierte virtuelle IDs durch die echte Navidrome-ID."""
    out: list[tuple[str, str]] = []
    for key, value in items:
        if key in ID_PARAMS and ids.is_virtual(value):
            real = ids.cached(value) or await ids.resolve(value)
            out.append((key, real or value))
        else:
            out.append((key, value))
    return out


def _forward_query(items: Iterable[tuple[str, str]], **overrides: str) -> list[tuple[str, str]]:
    keep = [(k, v) for k, v in items if k not in overrides]
    keep.extend(overrides.items())
    return keep


# ------------------------------------------------------------ Durchreichen
async def passthrough(request: Request, endpoint: str, items: list[tuple[str, str]]) -> Response:
    client = http.navidrome()
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST_HEADERS
    }
    body = await request.body() if request.method in ("POST", "PUT") else None

    upstream = client.build_request(
        request.method, f"/rest/{endpoint}", params=items, headers=headers, content=body
    )
    try:
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        log.error("Navidrome nicht erreichbar (%s): %s", endpoint, exc)
        return payload.error(
            payload.E_GENERIC, "Navidrome ist nicht erreichbar", dict(items)
        )

    out_headers = [
        (k, v) for k, v in response.headers.items() if k.lower() not in HOP_BY_HOP
    ]
    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers=dict(out_headers),
        background=BackgroundTask(response.aclose),
    )


async def upstream_json(endpoint: str, items: list[tuple[str, str]]) -> dict[str, Any]:
    """Fuer abgefangene Endpunkte: Antwort immer als JSON holen, egal was der
    Client wollte. Serialisiert wird erst am Schluss."""
    query = _forward_query([(k, v) for k, v in items if k != "callback"], f="json")
    response = await http.navidrome().get(f"/rest/{endpoint}", params=query, timeout=20.0)
    response.raise_for_status()
    return response.json()


# --------------------------------------------------------------- Hilfsdinge
def _norm(*parts: str) -> str:
    joined = " ".join(p or "" for p in parts).lower()
    return "".join(ch for ch in joined if ch.isalnum())


def _int_param(flat: dict[str, str], key: str, default: int) -> int:
    try:
        return int(flat.get(key, default))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------- Route
@router.api_route("/rest/{endpoint:path}", methods=["GET", "POST", "HEAD"])
async def subsonic_entry(endpoint: str, request: Request) -> Response:
    name = endpoint[:-5] if endpoint.endswith(".view") else endpoint
    items, flat = await _collect(request)
    items = await _translate_ids(items)
    flat = {k: v for k, v in items}

    if name not in INTERCEPTED:
        return await passthrough(request, endpoint, items)

    handler = {
        "search2": _handle_search,
        "search3": _handle_search,
        "getSong": _handle_get_song,
        "getAlbum": _handle_get_album,
        "stream": _handle_stream,
        "download": _handle_stream,
        "getCoverArt": _handle_cover,
    }[name]
    return await handler(request, endpoint, name, items, flat)


# ------------------------------------------------------------------ Suche
async def _handle_search(request, endpoint, name, items, flat) -> Response:
    query = (flat.get("query") or "").strip().strip('"')
    fmt = payload.wanted_format(flat)
    song_count = _int_param(flat, "songCount", 20)
    offset = _int_param(flat, "songOffset", 0)

    provider_wanted = (
        settings.provider_search_enabled
        and query
        and offset == 0
        and song_count > 0
    )

    # Lokale Suche und Katalogsuche laufen parallel - die Deezer-Antwort ist
    # damit praktisch gratis, solange Navidrome nicht schneller ist.
    nd_task = asyncio.create_task(upstream_json(endpoint, items))
    dz_task = (
        asyncio.create_task(deezer.search_tracks(query))
        if provider_wanted
        else None
    )

    try:
        document = await nd_task
    except Exception as exc:
        if dz_task:
            dz_task.cancel()
        log.error("Suche gegen Navidrome fehlgeschlagen: %s", exc)
        return await passthrough(request, endpoint, items)

    body = document.get("subsonic-response") or {}
    result_key = "searchResult3" if name == "search3" else "searchResult2"
    result = body.get(result_key)
    if result is None:
        result = {}
        body[result_key] = result

    local_songs = result.get("song") or []
    if not isinstance(local_songs, list):
        local_songs = [local_songs]

    if not dz_task:
        return payload.render(document, fmt, flat.get("callback"))

    try:
        candidates = await dz_task
    except Exception as exc:
        log.warning("Katalogsuche fehlgeschlagen: %s", exc)
        candidates = []

    # Navidromes Suche ist buchstabengetreu. Tippt jemand "marc forster",
    # liefert sie nichts - obwohl die Titel in der Bibliothek liegen. Der
    # Katalog kennt die richtige Schreibweise; damit wird einmal nachgefragt.
    if not local_songs and candidates:
        local_songs = await _retry_with_correction(endpoint, items, query, candidates, result_key)
        if local_songs:
            result["song"] = local_songs

    additions = await _virtual_additions(candidates, local_songs)
    if additions:
        result["song"] = local_songs + additions
    return payload.render(document, fmt, flat.get("callback"))


async def _retry_with_correction(endpoint, items, query, candidates, result_key) -> list[dict]:
    artist = deezer.dominant_artist(candidates)
    if not artist or deezer.looks_like(artist, query):
        return []
    try:
        document = await upstream_json(endpoint, _forward_query(items, query=artist))
    except Exception as exc:
        log.debug("Korrigierte Suche fehlgeschlagen: %s", exc)
        return []
    songs = ((document.get("subsonic-response") or {}).get(result_key) or {}).get("song") or []
    if not isinstance(songs, list):
        songs = [songs]
    if songs:
        log.debug("Suche '%s' ueber Katalogschreibweise '%s' aufgeloest", query, artist)
    return songs


async def _virtual_additions(candidates: list[dict], local_songs: list[dict]) -> list[dict]:
    if not candidates:
        return []

    local_keys = {_norm(s.get("artist", ""), s.get("title", "")) for s in local_songs}
    known = await ids.existing_provider_ids(
        deezer.PROVIDER, [c["provider_id"] for c in candidates]
    )

    additions: list[dict] = []
    for candidate in candidates:
        if len(additions) >= settings.provider_result_limit:
            break
        row = known.get(candidate["provider_id"])
        # Bereits importiert? Dann liefert Navidrome den Titel ohnehin.
        if row and row.get("state") == "ready" and row.get("navidrome_id"):
            continue
        if _norm(candidate["artist"], candidate["title"]) in local_keys:
            continue

        vid = await ids.upsert(candidate)
        row = row or {}
        merged = {**candidate, **row, "id": vid}
        merged.setdefault("state", "virtual")
        additions.append(payload.virtual_song(merged, settings.marker_suffix))

    return additions


# ---------------------------------------------------------------- getSong
async def _handle_get_song(request, endpoint, name, items, flat) -> Response:
    song_id = flat.get("id") or ""
    if not ids.is_virtual(song_id):
        return await passthrough(request, endpoint, items)

    row = await ids.load(song_id)
    if not row:
        return payload.error(payload.E_NOT_FOUND, "Titel nicht gefunden", flat)
    return payload.render(
        payload.envelope({"song": payload.virtual_song(row, settings.marker_suffix)}),
        payload.wanted_format(flat),
        flat.get("callback"),
    )


# --------------------------------------------------------------- getAlbum
async def _handle_get_album(request, endpoint, name, items, flat) -> Response:
    album_id = flat.get("id") or ""
    if not album_id.endswith("-album") or not ids.is_virtual(album_id):
        return await passthrough(request, endpoint, items)

    vid = album_id[: -len("-album")]
    row = await ids.load(vid)
    if not row:
        return payload.error(payload.E_NOT_FOUND, "Album nicht gefunden", flat)

    song = payload.virtual_song(row, settings.marker_suffix)
    album = {
        "id": album_id,
        "name": row.get("album") or row.get("title") or "",
        "artist": row.get("album_artist") or row.get("artist") or "",
        "artistId": f"{vid}-artist",
        "coverArt": vid,
        "songCount": 1,
        "duration": int(row.get("duration") or 0),
        "created": row.get("created_at") or "1970-01-01T00:00:00.000Z",
        "year": row.get("year") or 0,
        "song": [song],
    }
    return payload.render(
        payload.envelope({"album": album}), payload.wanted_format(flat), flat.get("callback")
    )


# --------------------------------------------------------------- Cover Art
async def _handle_cover(request, endpoint, name, items, flat) -> Response:
    cover_id = (flat.get("id") or "").removesuffix("-album").removesuffix("-artist")
    if not ids.is_virtual(cover_id):
        return await passthrough(request, endpoint, items)

    row = await ids.load(cover_id)
    if not row or not row.get("cover_url"):
        return payload.error(payload.E_NOT_FOUND, "Kein Cover vorhanden", flat)

    cached = settings.cache_dir / "covers" / f"{cover_id}.jpg"
    if not cached.exists():
        try:
            response = await http.plain().get(row["cover_url"])
            response.raise_for_status()
            cached.parent.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_suffix(".part")
            tmp.write_bytes(response.content)
            tmp.replace(cached)
        except Exception as exc:
            log.debug("Cover-Download fehlgeschlagen (%s): %s", cover_id, exc)
            return payload.error(payload.E_NOT_FOUND, "Cover nicht ladbar", flat)

    return FileResponse(
        cached,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ------------------------------------------------------------ stream/download
async def _handle_stream(request, endpoint, name, items, flat) -> Response:
    song_id = flat.get("id") or ""
    if not ids.is_virtual(song_id):
        return await passthrough(request, endpoint, items)

    # Ab hier erzeugen wir selbst Inhalte, also muessen wir die Zugangsdaten
    # pruefen - sonst waere das ein offener Download-Ausloeser.
    ip = (request.client.host if request.client else "unknown")
    if not await auth.verify(flat, ip):
        return payload.error(payload.E_AUTH, "Benutzername oder Passwort falsch", flat)

    row = await ids.load(song_id)
    if not row:
        return payload.error(payload.E_NOT_FOUND, "Titel nicht gefunden", flat)

    # Rennen mit dem Worker: eventuell ist der Titel seit dem Laden fertig.
    if row.get("navidrome_id"):
        ids.cache_resolved(song_id, row["navidrome_id"])
        return await passthrough(
            request, endpoint, _forward_query(items, id=row["navidrome_id"])
        )

    await ids.count_play_request(song_id)
    await _ensure_download_job(row)

    if settings.stream_mode == "stream":
        return await _hold_open(song_id)
    return await _deferred_notice(flat)


async def _ensure_download_job(row: dict) -> int:
    if row.get("state") in ("virtual", "failed", None):
        await ids.set_state(row["id"], "queued")
    return await jobs.enqueue(
        jobs.DOWNLOAD_TRACK,
        {
            "virtual_id": row["id"],
            "provider": row["provider"],
            "provider_id": row["provider_id"],
            "url": row.get("source_url"),
            "title": row.get("title"),
            "artist": row.get("artist"),
        },
        priority=jobs.PRIORITY_INTERACTIVE,
        dedupe_key=f"dl:{row['id']}",
    )


async def _deferred_notice(flat) -> Response:
    """Modus 'defer': gueltiger Audiostream mit Hinweiston.

    Ein 404 waere technisch ehrlicher, bricht aber in jedem getesteten Client
    die Warteschlange ab. Ein kurzer Ton ist die robustere Antwort.
    """
    clip = await ffmpeg.notice_clip()
    if not clip:
        return payload.error(
            payload.E_GENERIC, "Titel wird geladen, bitte gleich erneut versuchen", flat
        )
    return FileResponse(
        clip,
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Gateway-Status": "download-queued",
        },
    )


async def _hold_open(virtual_id: str) -> StreamingResponse:
    """Modus 'stream': Verbindung offen halten, Hinweiston senden, und sobald
    die Datei fertig importiert ist, nahtlos den echten Titel anhaengen.

    MP3-Frames sind selbstbeschreibend, deshalb laesst sich der echte Titel an
    den Hinweiston anhaengen, ohne dass der Decoder aussteigt. Bewusst ohne
    Content-Length und ohne Accept-Ranges: Seeking ist in diesem Modus nicht
    moeglich, und ein falsch behaupteter Range-Support waere schlimmer.
    """
    notice_path = await ffmpeg.notice_clip()
    notice = notice_path.read_bytes() if notice_path else b""

    async def generator():
        deadline = time.monotonic() + STREAM_HOLD_SECONDS
        target: Path | None = None
        while time.monotonic() < deadline:
            row = await ids.load(virtual_id)
            if row and row.get("state") == "ready" and row.get("local_path"):
                candidate = Path(row["local_path"])
                if candidate.exists():
                    target = candidate
                    break
            if row and row.get("state") == "failed":
                break
            if notice:
                yield notice
                await asyncio.sleep(NOTICE_PACE_SECONDS)
            else:
                await asyncio.sleep(1.0)

        if not target:
            return
        loop = asyncio.get_running_loop()
        with target.open("rb") as handle:
            while True:
                chunk = await loop.run_in_executor(None, handle.read, 262144)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        generator(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "Accept-Ranges": "none",
            "X-Gateway-Status": "streaming-while-downloading",
        },
    )
