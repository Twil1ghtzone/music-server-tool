"""Download -> Staging -> Tagging -> Bibliothek -> Navidrome-ID.

Der Ablauf ist bewusst dateisystem-getrieben und nicht API-getrieben: der
Fertigstellungsnachweis ist eine Datei, die im Staging-Ordner auftaucht und
deren Groesse sich nicht mehr aendert. Das funktioniert unabhaengig davon,
welchen Dialekt der Deemix-Fork gerade spricht, und ueberlebt auch Dateien,
die auf anderem Weg dort landen.

Warum Staging statt direkt in die Bibliothek: Navidrome wuerde halbfertige
Dateien einlesen, und ein Abbruch mitten im Download hinterliesse Karteileichen
in der Datenbank. Erst nach Tag-Korrektur wird atomar in die Bibliothek
verschoben.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from ..clients import deemix, deezer, navidrome
from ..config import settings
from ..db import db
from ..events import emit
from ..logging_conf import get_logger
from ..subsonic import ids
from . import jobs, tags

log = get_logger("downloader")

POLL_INTERVAL = 2.0
# So viele Messungen mit unveraenderter Groesse gelten als "fertig geschrieben".
STABLE_CHECKS = 2
# Alles darunter ist mit Sicherheit ein Fragment, kein Track.
MIN_COMPLETE_BYTES = 64 * 1024
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ------------------------------------------------------------------ Pfade
def safe_component(value: str, fallback: str = "Unbekannt") -> str:
    cleaned = _ILLEGAL.sub("_", (value or "").strip()).strip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:120]


def plan_destination(source: Path, track: dict[str, Any]) -> Path:
    """Wohin die frisch geladene Datei in der Bibliothek gehoert.

    Standard ist `preserve`: die Struktur, die Deemix anhand der eigenen
    Trackname-/Album-/Playlist-Templates erzeugt hat, wird eins zu eins in die
    Bibliothek uebernommen. Das ist der einzige Weg, der garantiert zum
    bestehenden Bestand passt - denn der wurde von genau denselben Templates
    erzeugt, als Deemix noch direkt in die Bibliothek geschrieben hat.

    `tags` leitet den Pfad stattdessen selbst ab. Nur sinnvoll, wenn die
    Bibliothek ohnehin auf Interpret/Album/NN - Titel umgestellt werden soll.
    """
    if settings.import_layout != "tags":
        try:
            return settings.music_dir / source.relative_to(settings.staging_dir)
        except ValueError:
            # Datei liegt ausserhalb des Staging - dann bleibt nur der Name.
            return settings.music_dir / source.name
    return target_path(track, source)


def target_path(track: dict[str, Any], source: Path) -> Path:
    artist = safe_component(track.get("album_artist") or track.get("artist") or "", "Unbekannter Interpret")
    album = safe_component(track.get("album") or "", "Unbekanntes Album")
    title = safe_component(track.get("title") or source.stem, source.stem)
    number = track.get("track_no")
    prefix = f"{int(number):02d} - " if isinstance(number, int) and number > 0 else ""
    return settings.music_dir / artist / album / f"{prefix}{title}{source.suffix.lower()}"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(2, 100):
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem} ({int(time.time())}){suffix}")


def scan_audio(root: Path) -> dict[Path, int]:
    found: dict[Path, int] = {}
    if not root.exists():
        return found
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.lower().endswith(settings.audio_extensions):
                continue
            path = Path(dirpath) / name
            try:
                found[path] = path.stat().st_size
            except OSError:
                continue
    return found


def move_into_library(source: Path, destination: Path) -> Path:
    """Atomar, wenn moeglich; sonst kopieren und erst danach ersetzen.

    os.replace ist atomar, aber nur innerhalb eines Dateisystems. Staging und
    Bibliothek liegen auf einem NAS oft auf demselben Pool - dann greift der
    schnelle Weg. Andernfalls wird ueber eine .part-Datei kopiert, damit
    Navidrome nie eine halbe Datei sieht.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = unique_path(destination)
    try:
        os.replace(source, destination)
        return destination
    except OSError:
        temp = destination.with_suffix(destination.suffix + ".part")
        shutil.copy2(source, temp)
        os.replace(temp, destination)
        source.unlink(missing_ok=True)
        return destination


def move_sidecars(source: Path, destination: Path) -> list[Path]:
    """Nimmt Lyrics und Cover mit, die Deemix neben den Track gelegt hat.

    Navidrome ist hier laut Konfiguration auf .lrc angewiesen
    (ND_LYRICSPRIORITY). Bliebe die Datei im Staging liegen, waere der Titel
    zwar da, der Songtext aber weg - und niemand wuesste, warum.

    Mitgenommen wird alles mit gleichem Dateinamen-Stamm plus die typischen
    Ordnerbilder (cover.jpg, folder.jpg).
    """
    moved: list[Path] = []
    candidates: list[Path] = []

    for extension in settings.sidecar_extensions:
        twin = source.with_suffix(extension)
        if twin.exists():
            candidates.append(twin)

    for name in ("cover", "folder", "front", "album"):
        for extension in (".jpg", ".jpeg", ".png", ".webp"):
            image = source.parent / f"{name}{extension}"
            if image.exists() and image not in candidates:
                candidates.append(image)

    for candidate in candidates:
        try:
            if candidate.stem == source.stem:
                target = destination.with_suffix(candidate.suffix)
            else:
                target = destination.parent / candidate.name
            if target.exists():
                # Vorhandenes Cover im Bestand nie ueberschreiben.
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate, target)
            moved.append(target)
        except OSError as exc:
            log.debug("Begleitdatei %s nicht verschoben: %s", candidate, exc)
    return moved


# --------------------------------------------------------------- Warten
async def wait_for_new_file(before: dict[Path, int], deadline: float,
                            job_id: int | None = None) -> Path | None:
    """Wartet auf eine neue, vollstaendig geschriebene Audiodatei im Staging.

    "Vollstaendig" heisst: die Groesse ist ueber mehrere Messungen unveraendert
    geblieben. Eine einzelne Wiederholung reicht nicht - eine kurze Pause im
    Download wuerde sonst als fertig durchgehen und eine halbe Datei landete
    in der Bibliothek.
    """
    sizes: dict[Path, int] = {}
    steady: dict[Path, int] = {}

    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        current = scan_audio(settings.staging_dir)
        fresh = [p for p in current if p not in before]

        for path in fresh:
            size = current[path]
            if size < MIN_COMPLETE_BYTES:
                continue
            if sizes.get(path) == size:
                steady[path] = steady.get(path, 0) + 1
                if steady[path] >= STABLE_CHECKS:
                    return path
            else:
                steady[path] = 0
            sizes[path] = size

        if job_id and fresh:
            await jobs.progress(
                job_id, 0.45, f"Deemix schreibt: {fresh[0].name} ({fresh[0].stat().st_size // 1024} KB)"
                if fresh[0].exists() else f"Deemix schreibt: {fresh[0].name}"
            )
    return None


async def resolve_navidrome_id(destination: Path, track: dict[str, Any],
                               deadline: float, job_id: int | None = None) -> str | None:
    """Findet die echte Navidrome-ID der frisch importierten Datei.

    Zuerst ueber den relativen Pfad (eindeutig), ersatzweise ueber Titel und
    Interpret. Navidromes ID-Ableitung ist ein Implementierungsdetail, auf das
    man sich nicht verlassen darf - also fragen statt raten.
    """
    try:
        relative = str(destination.relative_to(settings.music_dir))
    except ValueError:
        relative = destination.name
    needle = _norm(track.get("artist", ""), track.get("title", ""))
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        try:
            songs = await navidrome.search_songs(track.get("title") or destination.stem, count=50)
        except Exception as exc:
            log.debug("Navidrome-Suche fehlgeschlagen: %s", exc)
            songs = []

        for song in songs:
            path = (song.get("path") or "").replace("\\", "/")
            if path and (path == relative.replace("\\", "/") or path.endswith(destination.name)):
                return str(song.get("id"))
        for song in songs:
            if _norm(song.get("artist", ""), song.get("title", "")) == needle:
                return str(song.get("id"))

        if job_id:
            await jobs.progress(job_id, 0.85, f"Warte auf Navidrome-Scan (Versuch {attempt})")
        if attempt % 4 == 0:
            # Der Watcher kann eine Aenderung verpassen - dann nachhelfen.
            try:
                await navidrome.start_scan()
            except Exception:
                pass
        await asyncio.sleep(3.0)
    return None


def _norm(*parts: str) -> str:
    joined = " ".join(p or "" for p in parts).lower()
    return "".join(ch for ch in joined if ch.isalnum())


# --------------------------------------------------------------- Job-Handler
async def handle_download(job: dict[str, Any]) -> str:
    """Bricht der Download ab, muss der Titel das auch anzeigen.

    Vorher blieb er auf "downloading" stehen: der Zustand wurde vor dem
    eigentlichen Versuch gesetzt und bei einer Ausnahme nie korrigiert. In der
    Oberflaeche sah ein laengst gescheiterter Download damit aus, als liefe er
    noch - und zwar fuer immer.
    """
    virtual_id: str = job["payload"]["virtual_id"]
    try:
        return await _run_download(job, virtual_id)
    except Exception as exc:
        await ids.set_state(virtual_id, "failed", error=f"{type(exc).__name__}: {exc}"[:500])
        raise


async def _run_download(job: dict[str, Any], virtual_id: str) -> str:
    payload = job["payload"]
    job_id = int(job["id"])

    row = await ids.load(virtual_id)
    if not row:
        raise RuntimeError(f"Unbekannter virtueller Titel: {virtual_id}")
    if row.get("navidrome_id"):
        return "War bereits importiert"

    label = f"{row.get('artist','')} - {row.get('title','')}"
    await ids.set_state(virtual_id, "downloading")
    await jobs.progress(job_id, 0.05, f"Starte Download: {label}")
    await emit(f"Download gestartet: {label}", category="download", data={"id": virtual_id})

    url = row.get("source_url") or payload.get("url")
    if not url:
        provider_track = await deezer.get_track(row["provider_id"])
        url = (provider_track or {}).get("source_url")
    if not url:
        raise RuntimeError("Keine Quell-URL fuer diesen Titel")

    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    before = scan_audio(settings.staging_dir)

    transport = await deemix.add_to_queue(url, settings.deemix_bitrate)
    await jobs.progress(job_id, 0.2, f"An Deemix uebergeben ({transport})")

    deadline = time.monotonic() + settings.download_timeout
    source = await wait_for_new_file(before, deadline, job_id)
    if not source:
        await ids.set_state(virtual_id, "failed", error="Deemix hat keine Datei geliefert")
        raise RuntimeError(
            "Zeitueberschreitung: keine neue Datei im Staging-Ordner. "
            "Pruefe ARL-Token und Deemix-Log."
        )

    await ids.set_state(virtual_id, "importing")
    await jobs.progress(job_id, 0.6, f"Importiere {source.name}")

    try:
        tags.apply_from_provider(source, dict(row))
    except Exception as exc:
        log.warning("Tag-Korrektur fehlgeschlagen (%s): %s", source.name, exc)

    destination = move_into_library(source, plan_destination(source, dict(row)))
    sidecars = move_sidecars(source, destination)
    _cleanup_empty_dirs(settings.staging_dir)
    await jobs.progress(
        job_id,
        0.75,
        f"In Bibliothek: {destination.name}"
        + (f" (+{len(sidecars)} Begleitdatei(en))" if sidecars else ""),
    )

    try:
        await navidrome.start_scan()
    except Exception as exc:
        log.debug("Scan-Trigger fehlgeschlagen (Watcher uebernimmt): %s", exc)

    nd_id = await resolve_navidrome_id(
        destination, dict(row), time.monotonic() + settings.resolve_timeout, job_id
    )
    if not nd_id:
        await ids.set_state(
            virtual_id, "importing", error="Datei importiert, Navidrome-ID noch offen"
        )
        raise RuntimeError(
            f"Datei liegt unter {destination}, aber Navidrome hat sie noch nicht indexiert"
        )

    await ids.mark_ready(virtual_id, nd_id, str(destination))
    await emit(f"Bereit: {label}", category="download", data={"id": virtual_id, "navidrome_id": nd_id})
    return f"{destination.name} -> Navidrome {nd_id}"


async def handle_import_staging(job: dict[str, Any]) -> str:
    """Raeumt den Staging-Ordner auf: alles, was dort liegt und keiner
    laufenden Anforderung zugeordnet ist, wandert nach Tag-Pruefung in die
    Bibliothek. Verhindert, dass abgebrochene Laeufe Dateien verwaisen lassen."""
    job_id = int(job["id"])
    files = sorted(scan_audio(settings.staging_dir))
    if not files:
        return "Staging ist leer"

    moved = 0
    for index, source in enumerate(files, start=1):
        try:
            meta = tags.read(source)
            meta.setdefault("album_artist", meta.get("artist"))
            destination = move_into_library(source, plan_destination(source, meta))
            move_sidecars(source, destination)
            moved += 1
            log.info("Staging-Import: %s", destination)
        except Exception as exc:
            log.warning("Import von %s fehlgeschlagen: %s", source, exc)
        await jobs.progress(job_id, index / len(files))

    _cleanup_empty_dirs(settings.staging_dir)
    if moved:
        try:
            await navidrome.start_scan()
        except Exception:
            pass
        await emit(f"{moved} Datei(en) aus dem Staging importiert", category="import")
    return f"{moved} von {len(files)} Datei(en) importiert"


async def handle_navidrome_scan(job: dict[str, Any]) -> str:
    full = bool(job["payload"].get("full"))
    await navidrome.start_scan(full=full)
    for _ in range(120):
        await asyncio.sleep(2.0)
        status = await navidrome.scan_status()
        if not status.get("scanning"):
            return f"Scan beendet ({status.get('count', '?')} Titel)"
        await jobs.progress(job["id"], 0.5, f"{status.get('count', 0)} Titel")
    return "Scan laeuft noch (Zeitfenster ueberschritten)"


def _cleanup_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root:
            continue
        if not dirnames and not filenames:
            try:
                path.rmdir()
            except OSError:
                pass


# ------------------------------------------------------- Manueller Anstoss
async def request_track(provider_id: str, *, provider: str = deezer.PROVIDER) -> dict[str, Any]:
    """Vom Dashboard aus: Titel suchen lassen und Download anstossen."""
    track = await deezer.get_track(provider_id)
    if not track:
        raise ValueError("Titel im Katalog nicht gefunden")
    virtual_id = await ids.upsert(track)
    row = await ids.load(virtual_id)
    assert row is not None
    if row.get("navidrome_id"):
        return {"id": virtual_id, "state": "ready", "job": None}
    await ids.set_state(virtual_id, "queued")
    job_id = await jobs.enqueue(
        jobs.DOWNLOAD_TRACK,
        {
            "virtual_id": virtual_id,
            "provider": provider,
            "provider_id": provider_id,
            "url": track.get("source_url"),
            "title": track.get("title"),
            "artist": track.get("artist"),
        },
        priority=jobs.PRIORITY_NORMAL,
        dedupe_key=f"dl:{virtual_id}",
    )
    await emit(
        f"Download angefordert: {track.get('artist')} - {track.get('title')}",
        category="download",
    )
    return {"id": virtual_id, "state": "queued", "job": job_id}


async def queue_overview(limit: int = 50) -> list[dict]:
    return await db.fetch_all(
        "SELECT id, title, artist, album, state, error, play_requests, updated_at "
        "FROM virtual_track WHERE state != 'virtual' ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )
