"""Bibliotheks-API: Index, Duplikate, Tag-Werkzeuge."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import security
from ..config import settings
from ..db import db
from ..events import emit
from ..logging_conf import get_logger
from ..services import dedupe, jobs, protection, scanner, tags

# Was der Browser zum Abspielen braucht. Alles andere geht als
# Bytestrom raus - der Browser sagt dann selbst, dass er es nicht kann.
MIME = {
    ".flac": "audio/flac", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".wav": "audio/wav", ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma",
}

log = get_logger("api.library")
router = APIRouter(prefix="/api/library", tags=["library"])


class ApplyBody(BaseModel):
    groups: list[int] = Field(min_length=1, max_length=500)


class KeeperBody(BaseModel):
    media_file_id: int


class TagBody(BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    genre: str | None = None
    year: int | None = None
    track_no: int | None = None
    disc_no: int | None = None


class BatchTagBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=1000)
    changes: TagBody


def _require_tag_write() -> None:
    """Tags zu schreiben veraendert vorhandene Dateien. Standardmaessig aus,
    damit ein Fehlklick im ersten Betrieb den Bestand nicht anfasst."""
    if not settings.allow_tag_write:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Tag-Schreiben ist gesperrt. Zum Freischalten GATEWAY_ALLOW_TAG_WRITE=true "
            "setzen und den Stack neu starten.",
        )


# ------------------------------------------------------------------ Index
@router.get("/stats")
async def stats(user: dict = Depends(security.admin_only)) -> dict:
    return await scanner.library_stats()


@router.post("/scan")
async def scan(user: dict = Depends(security.guarded_admin)) -> dict:
    job_id = await jobs.enqueue(
        jobs.LIBRARY_SCAN, priority=jobs.PRIORITY_NORMAL, dedupe_key="scan:library"
    )
    return {"job": job_id}


@router.post("/fingerprint")
async def fingerprint(
    user: dict = Depends(security.guarded_admin), limit: int = Query(5000, le=100000)
) -> dict:
    job_id = await jobs.enqueue(
        jobs.FINGERPRINT,
        {"limit": limit},
        priority=jobs.PRIORITY_BACKGROUND,
        dedupe_key="fingerprint:all",
    )
    return {"job": job_id}


@router.get("/files")
async def files(
    user: dict = Depends(security.admin_only),
    q: str = Query("", max_length=200),
    issues_only: bool = False,
    limit: int = Query(100, le=500),
    offset: int = 0,
) -> dict:
    where = ["missing = 0"]
    params: list = []
    if q:
        where.append("(title LIKE ? OR artist LIKE ? OR album LIKE ? OR path LIKE ?)")
        needle = f"%{q}%"
        params.extend([needle] * 4)
    if issues_only:
        where.append("tag_issues IS NOT NULL")
    clause = " AND ".join(where)

    rows = await db.fetch_all(
        f"SELECT id, path, size, ext, bitrate, duration, title, artist, album, "
        f"album_artist, track_no, disc_no, year, has_cover, tag_issues "
        f"FROM media_file WHERE {clause} ORDER BY artist, album, track_no LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    total = int(
        await db.fetch_value(f"SELECT COUNT(*) FROM media_file WHERE {clause}", params, 0) or 0
    )
    return {"files": rows, "total": total, "limit": limit, "offset": offset}


# -------------------------------------------------------------- Duplikate
@router.get("/dupes")
async def list_dupes(
    user: dict = Depends(security.admin_only),
    state: str = Query("open", pattern="^(open|applied|ignored)$"),
    kind: str = Query("alle", pattern="^(alle|exact|audio|acoustic)$"),
    auto: bool = Query(False, description="Nur Gruppen, die eindeutig sind"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    seite = await dedupe.groups(state, limit, offset, kind, auto)
    return {
        **seite,
        "summary": await dedupe.summary(),
        # Damit die Oberflaeche den Schutzschalter erklaeren kann, statt den
        # Knopf anzubieten und dann mit 403 zu antworten.
        "apply_allowed": settings.allow_dedupe_apply,
    }


@router.get("/dupes/auto")
async def dupes_auto(user: dict = Depends(security.admin_only)) -> dict:
    """Die Gruppen, bei denen der Scanner sich selbst sicher ist.

    Getrennt vom Listenendpunkt, weil die Oberflaeche sie ueber alle Seiten
    hinweg auswaehlen koennen muss - nicht nur die gerade sichtbaren.
    """
    ids = await dedupe.auto_gruppen()
    return {"groups": ids, "count": len(ids)}


@router.post("/dupes/find")
async def find_dupes(
    user: dict = Depends(security.guarded_admin), acoustic: bool = False
) -> dict:
    job_id = await jobs.enqueue(
        jobs.FIND_DUPES,
        {"acoustic": acoustic},
        priority=jobs.PRIORITY_NORMAL,
        dedupe_key="dupes:scan",
    )
    return {"job": job_id}


@router.post("/dupes/apply")
async def apply_dupes(body: ApplyBody, user: dict = Depends(security.guarded_admin)) -> dict:
    """Verschiebt die Nicht-Keeper in die Quarantaene. Kein Loeschen."""
    if not settings.allow_dedupe_apply:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Bereinigung ist gesperrt. Zum Freischalten GATEWAY_ALLOW_DEDUPE_APPLY=true "
            "setzen und den Stack neu starten.",
        )
    job_id = await jobs.enqueue(
        jobs.APPLY_DUPES,
        {"groups": body.groups},
        priority=jobs.PRIORITY_NORMAL,
    )
    return {"job": job_id, "quarantine": str(settings.quarantine_dir)}


@router.post("/dupes/{group_id}/keeper")
async def set_keeper(
    group_id: int, body: KeeperBody, user: dict = Depends(security.guarded_admin)
) -> dict:
    member = await db.fetch_one(
        "SELECT 1 FROM dupe_member WHERE group_id = ? AND media_file_id = ?",
        (group_id, body.media_file_id),
    )
    if not member:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Datei gehoert nicht zu dieser Gruppe")
    await db.execute(
        "UPDATE dupe_group SET keeper_id = ? WHERE id = ? AND state = 'open'",
        (body.media_file_id, group_id),
    )
    return {"ok": True}


@router.post("/dupes/{group_id}/ignore")
async def ignore_group(group_id: int, user: dict = Depends(security.guarded_admin)) -> dict:
    await db.execute("UPDATE dupe_group SET state = 'ignored' WHERE id = ?", (group_id,))
    return {"ok": True}


@router.post("/dupes/{group_id}/restore")
async def restore_group(group_id: int, user: dict = Depends(security.guarded_admin)) -> dict:
    restored = await dedupe.restore(group_id)
    await emit(f"{restored} Datei(en) aus der Quarantaene zurueckgeholt", category="dedupe")
    return {"restored": restored}


# ------------------------------------------------------------ Quarantaene
class FristBody(BaseModel):
    days: int = Field(ge=dedupe.QUARANTAENE_TAGE_MIN, le=dedupe.QUARANTAENE_TAGE_MAX)


@router.get("/quarantine")
async def quarantine_list(
    user: dict = Depends(security.admin_only),
    state: str = Query("held", pattern="^(held|restored|purged|lost)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    return await dedupe.quarantaene(state, limit, offset)


@router.post("/quarantine/days")
async def quarantine_days(
    body: FristBody, user: dict = Depends(security.guarded_admin)
) -> dict:
    """Die Frist gilt fuer kuenftige Verschiebungen.

    Was schon in der Quarantaene liegt, traegt sein eigenes Ablaufdatum in
    der Zeile - eine verlaengerte Frist belebt also nichts wieder, das schon
    zur Loeschung freigegeben war.
    """
    return {"days": await dedupe.setze_quarantaene_tage(body.days)}


@router.post("/quarantine/{item_id}/restore")
async def quarantine_restore(
    item_id: int, user: dict = Depends(security.guarded_admin)
) -> dict:
    if not await dedupe.restore_item(item_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Zurueckholen nicht moeglich - die Datei ist weg oder der Platz ist belegt.")
    await emit("Datei aus der Quarantaene zurueckgeholt", category="dedupe")
    return {"ok": True}


@router.post("/quarantine/purge")
async def quarantine_purge(user: dict = Depends(security.guarded_admin)) -> dict:
    """Loescht jetzt, was ohnehin faellig waere. Nicht mehr."""
    job_id = await jobs.enqueue(
        jobs.PURGE_QUARANTINE, priority=jobs.PRIORITY_NORMAL, dedupe_key="quarantine:purge")
    return {"job": job_id}


# ------------------------------------------------- Schutz vor Nutzerdaten
@router.get("/protection")
async def protection_state(user: dict = Depends(security.admin_only)) -> dict:
    return await protection.stand()


@router.post("/protection/sync")
async def protection_sync(user: dict = Depends(security.guarded_admin)) -> dict:
    job_id = await jobs.enqueue(
        jobs.SYNC_PROTECTION, priority=jobs.PRIORITY_NORMAL, dedupe_key="protection:sync")
    return {"job": job_id}


# ------------------------------------------- Cover und Hoerprobe je Datei
# Damit man ein Duplikat pruefen kann, ohne es herunterzuladen: das Cover
# zeigen und hineinhoeren. Beides holt der Gateway mit seinen eigenen
# Navidrome-Zugangsdaten - das Dashboard hat keine Subsonic-Sitzung.

# Ein Bild, das es nicht gibt, muss genauso gemerkt werden wie eines, das es
# gibt. Sonst fragt die Oberflaeche bei jedem Neuzeichnen wieder nach - und
# eine Liste mit fuenfzig Zeilen erzeugt im Minutentakt fuenfzig Anfragen.
_OHNE_COVER: set[int] = set()


@router.get("/files/{file_id}/cover")
async def file_cover(
    file_id: int, user: dict = Depends(security.current_user), s: int = Query(160, ge=32, le=1000)
) -> Response:
    """Das Titelbild einer lokalen Datei.

    Genommen wird es aus der Datei selbst. Navidrome danach zu fragen waere
    ein Netzaufruf je Bild, haengt daran, dass die Datei dort indiziert ist,
    und hat dessen Log mit "Parent folder not found" geflutet - eine Warnung
    je Zeile, bei jedem Neuzeichnen. Das Bild liegt hier auf der Platte.
    """
    if file_id in _OHNE_COVER:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Kein Cover in der Datei")

    zeile = await db.fetch_one("SELECT path FROM media_file WHERE id = ?", (file_id,))
    if not zeile:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Datei nicht im Index")
    pfad = Path(zeile["path"])
    try:
        pfad.resolve().relative_to(settings.music_dir.resolve())
    except (ValueError, OSError) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Pfad ausserhalb der Bibliothek") from exc

    # Auf Platte zwischengespeichert: das Bild aus einer FLAC zu lesen heisst,
    # die Datei zu oeffnen und zu parsen. Einmal reicht.
    ziel = settings.cache_dir / "filecovers" / f"{file_id}.img"
    if ziel.exists():
        return Response(ziel.read_bytes(), media_type=_bildtyp(ziel),
                        headers={"Cache-Control": "private, max-age=604800"})

    if not pfad.exists():
        raise HTTPException(status.HTTP_410_GONE, "Datei liegt nicht mehr an diesem Pfad")

    loop = asyncio.get_running_loop()
    treffer = await loop.run_in_executor(None, tags.cover_bytes, pfad)
    if not treffer:
        # Auch das Nichtvorhandensein merken - sonst wird die Datei bei jedem
        # Neuzeichnen erneut geoeffnet und geparst.
        _OHNE_COVER.add(file_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Kein Cover in der Datei")

    roh, typ = treffer
    try:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_suffix(".part")
        tmp.write_bytes(roh)
        tmp.replace(ziel)
        (ziel.parent / f"{file_id}.type").write_text(typ, encoding="utf-8")
    except OSError as exc:      # Kein Platz o.ae. - Bild trotzdem liefern.
        log.debug("Cover nicht zwischengespeichert: %s", exc)

    return Response(roh, media_type=typ,
                    headers={"Cache-Control": "private, max-age=604800"})


def _bildtyp(ziel: Path) -> str:
    merker = ziel.parent / f"{ziel.stem}.type"
    try:
        return merker.read_text(encoding="utf-8").strip() or "image/jpeg"
    except OSError:
        return "image/jpeg"


@router.get("/files/{file_id}/stream")
async def file_stream(file_id: int, request: Request,
                      user: dict = Depends(security.current_user)) -> StreamingResponse:
    """Reicht die Datei zum Probehoeren durch.

    Mit Bereichsanfragen, damit der Browser springen kann - ohne das laedt
    er beim Vorspulen jedes Mal von vorn.
    """
    zeile = await db.fetch_one("SELECT path FROM media_file WHERE id = ?", (file_id,))
    if not zeile:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Datei nicht im Index")
    pfad = Path(zeile["path"])
    # Der Pfad kommt aus dem eigenen Index, aber geprueft wird trotzdem:
    # ein Eintrag ausserhalb der Bibliothek waere ein Weg, beliebige Dateien
    # des Servers auszulesen.
    try:
        pfad.resolve().relative_to(settings.music_dir.resolve())
    except (ValueError, OSError) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Pfad ausserhalb der Bibliothek") from exc
    if not pfad.exists():
        raise HTTPException(status.HTTP_410_GONE, "Datei liegt nicht mehr an diesem Pfad")

    groesse = pfad.stat().st_size
    typ = MIME.get(pfad.suffix.lower(), "application/octet-stream")
    start, ende = 0, groesse - 1
    bereich = request.headers.get("range", "")
    if bereich.startswith("bytes="):
        roh = bereich[6:].split("-", 1)
        try:
            if roh[0]:
                start = int(roh[0])
            if len(roh) > 1 and roh[1]:
                ende = min(int(roh[1]), groesse - 1)
        except ValueError:
            start, ende = 0, groesse - 1
    if start > ende or start >= groesse:
        raise HTTPException(status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, "Bereich ungueltig")

    laenge = ende - start + 1

    def lies():
        with pfad.open("rb") as f:
            f.seek(start)
            rest = laenge
            while rest > 0:
                brocken = f.read(min(65536, rest))
                if not brocken:
                    return
                rest -= len(brocken)
                yield brocken

    kopf = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(laenge),
        "Cache-Control": "private, max-age=3600",
    }
    if bereich:
        kopf["Content-Range"] = f"bytes {start}-{ende}/{groesse}"
    return StreamingResponse(lies(), status_code=206 if bereich else 200,
                             media_type=typ, headers=kopf)


# ------------------------------------------------------------------ Tags
@router.patch("/files/{file_id}/tags")
async def edit_tags(
    file_id: int, body: TagBody, user: dict = Depends(security.guarded_admin)
) -> dict:
    _require_tag_write()
    row = await db.fetch_one("SELECT * FROM media_file WHERE id = ?", (file_id,))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Datei nicht gefunden")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(status.HTTP_410_GONE, "Datei liegt nicht mehr an diesem Pfad")

    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if not changes:
        return {"ok": True, "changed": 0}

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, tags.write, path, changes)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Schreiben fehlgeschlagen: {exc}")

    await _refresh_row(file_id, path)
    await emit(f"Tags geaendert: {path.name}", category="tags")
    return {"ok": True, "changed": len(changes)}


@router.post("/files/tags/batch")
async def batch_tags(body: BatchTagBody, user: dict = Depends(security.guarded_admin)) -> dict:
    _require_tag_write()
    changes = {k: v for k, v in body.changes.model_dump(exclude_unset=True).items()}
    if not changes:
        return {"ok": True, "changed": 0}

    placeholders = ",".join("?" * len(body.ids))
    rows = await db.fetch_all(
        f"SELECT id, path FROM media_file WHERE id IN ({placeholders})", body.ids
    )
    loop = asyncio.get_running_loop()
    changed, failed = 0, []
    for row in rows:
        path = Path(row["path"])
        try:
            await loop.run_in_executor(None, tags.write, path, changes)
            await _refresh_row(int(row["id"]), path)
            changed += 1
        except Exception as exc:
            failed.append({"path": row["path"], "error": str(exc)})

    await emit(f"Batch-Tagging: {changed} Datei(en) geaendert", category="tags")
    return {"ok": True, "changed": changed, "failed": failed}


async def _refresh_row(file_id: int, path: Path) -> None:
    loop = asyncio.get_running_loop()
    meta = await loop.run_in_executor(None, tags.read, path)
    issues = tags.validate(meta)
    await db.execute(
        "UPDATE media_file SET title=?, artist=?, album=?, album_artist=?, "
        "track_no=?, disc_no=?, year=?, has_cover=?, tag_issues=? WHERE id = ?",
        (
            meta.get("title"),
            meta.get("artist"),
            meta.get("album"),
            meta.get("album_artist"),
            meta.get("track_no"),
            meta.get("disc_no"),
            meta.get("year"),
            1 if meta.get("has_cover") else 0,
            ",".join(issues) if issues else None,
            file_id,
        ),
    )


@router.get("/issues")
async def issues(user: dict = Depends(security.admin_only)) -> dict:
    rows = await db.fetch_all(
        "SELECT tag_issues FROM media_file WHERE missing = 0 AND tag_issues IS NOT NULL"
    )
    counts: dict[str, int] = {}
    for row in rows:
        for issue in (row["tag_issues"] or "").split(","):
            if issue:
                counts[issue] = counts.get(issue, 0) + 1
    return {
        "total": len(rows),
        "by_issue": sorted(
            ({"issue": k, "count": v} for k, v in counts.items()),
            key=lambda x: x["count"],
            reverse=True,
        ),
    }
