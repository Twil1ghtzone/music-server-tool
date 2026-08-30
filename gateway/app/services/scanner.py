"""Bibliotheks-Index: Inventar, Hashes, technische Daten, Fingerprints.

Vierstufig, absteigend nach Kosten - jede Stufe laeuft nur auf dem, was die
vorige uebrig gelassen hat:

  Stufe 0  Inventar     stat() pro Datei, inkrementell ueber mtime+size
  Stufe 1  Datei-Hash   blake2b, NUR bei Groessenkollision (exakte Duplikate)
  Stufe 2  Audio-Hash   md5 des reinen Audiostreams (Duplikate trotz Tags)
  Stufe 3  Fingerprint  Chromaprint, findet auch andere Encodings

Stufe 1 nur bei Groessenkollision zu rechnen ist der wichtigste Trick: zwei
byteweise identische Dateien haben zwangslaeufig dieselbe Groesse, also kann
alles mit eindeutiger Groesse den vollstaendigen Hash ueberspringen.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any

from ..config import settings
from ..db import db
from ..events import emit
from ..logging_conf import get_logger
from . import ffmpeg, jobs, tags

log = get_logger("scanner")

BATCH = 500


# ------------------------------------------------------------- Stufe 0
def _walk(root: Path) -> list[tuple[str, int, float, str]]:
    rows: list[tuple[str, int, float, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in settings.audio_extensions:
                continue
            path = os.path.join(dirpath, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            rows.append((path, stat.st_size, stat.st_mtime, ext))
    return rows


async def handle_library_scan(job: dict[str, Any]) -> str:
    job_id = int(job["id"])
    root = settings.music_dir
    if not root.exists():
        raise RuntimeError(f"Musikverzeichnis nicht gefunden: {root}")

    await jobs.progress(job_id, 0.02, "Verzeichnis wird gelesen")
    loop = asyncio.get_running_loop()
    # Zeitmarke VOR dem Lauf: alles, was danach nicht angefasst wurde, fehlt.
    # (Eine relative Grenze wie '-1 minute' waere bei langen Scans falsch.)
    scan_started = await db.fetch_value("SELECT datetime('now')", (), None)
    entries = await loop.run_in_executor(None, _walk, root)
    await jobs.progress(job_id, 0.2, f"{len(entries)} Dateien gefunden")

    seen: set[str] = set()
    changed = 0
    for start in range(0, len(entries), BATCH):
        chunk = entries[start : start + BATCH]
        seen.update(row[0] for row in chunk)
        async with db.transaction() as conn:
            for path, size, mtime, ext in chunk:
                cur = await conn.execute(
                    """
                    INSERT INTO media_file(path, size, mtime, ext, missing, seen_at)
                    VALUES (?,?,?,?,0, datetime('now'))
                    ON CONFLICT(path) DO UPDATE SET
                        missing = 0,
                        seen_at = datetime('now'),
                        size    = excluded.size,
                        mtime   = excluded.mtime,
                        -- Datei veraendert? Dann sind alle abgeleiteten Werte hin.
                        file_hash  = CASE WHEN media_file.mtime != excluded.mtime
                                            OR media_file.size != excluded.size
                                          THEN NULL ELSE media_file.file_hash END,
                        audio_hash = CASE WHEN media_file.mtime != excluded.mtime
                                            OR media_file.size != excluded.size
                                          THEN NULL ELSE media_file.audio_hash END,
                        probed_at  = CASE WHEN media_file.mtime != excluded.mtime
                                            OR media_file.size != excluded.size
                                          THEN NULL ELSE media_file.probed_at END
                    """,
                    (path, size, mtime, ext),
                )
                changed += cur.rowcount or 0
        await jobs.progress(job_id, 0.2 + 0.6 * (start + len(chunk)) / max(len(entries), 1))

    # Verschwundene Dateien markieren statt loeschen: die Historie in
    # dupe_group/fingerprint bleibt so nachvollziehbar.
    missing = await db.execute(
        "UPDATE media_file SET missing = 1 WHERE seen_at < ? AND missing = 0",
        (scan_started,),
    )

    await jobs.progress(job_id, 0.95, "Folgejobs werden eingeplant")
    await jobs.enqueue(jobs.HASH_FILES, priority=jobs.PRIORITY_BACKGROUND, dedupe_key="hash:all")
    await emit(f"Bibliotheks-Scan: {len(entries)} Dateien", category="library")
    return f"{len(entries)} Dateien indexiert, {missing} als fehlend markiert"


# ------------------------------------------------------- Stufe 1 + 2 + Tags
def _blake2b(path: Path, chunk: int) -> str | None:
    digest = hashlib.blake2b(digest_size=16)
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(chunk)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


async def handle_hash_files(job: dict[str, Any]) -> str:
    job_id = int(job["id"])
    loop = asyncio.get_running_loop()

    # Stufe 1: nur Kandidaten mit kollidierender Dateigroesse.
    candidates = await db.fetch_all(
        """
        SELECT id, path FROM media_file
         WHERE missing = 0 AND file_hash IS NULL
           AND size IN (SELECT size FROM media_file
                         WHERE missing = 0 GROUP BY size HAVING COUNT(*) > 1)
        """
    )
    for index, row in enumerate(candidates, start=1):
        digest = await loop.run_in_executor(
            None, _blake2b, Path(row["path"]), settings.hash_chunk_size
        )
        if digest:
            await db.execute(
                "UPDATE media_file SET file_hash = ?, hashed_at = datetime('now') WHERE id = ?",
                (digest, row["id"]),
            )
        if index % 25 == 0:
            await jobs.progress(job_id, 0.3 * index / len(candidates), f"Datei-Hash {index}/{len(candidates)}")

    # Stufe 2 + Metadaten: fuer alles, was noch nicht analysiert wurde.
    pending = await db.fetch_all(
        "SELECT id, path FROM media_file WHERE missing = 0 AND probed_at IS NULL LIMIT 20000"
    )
    total = len(pending) or 1
    analysed = 0
    semaphore = asyncio.Semaphore(max(2, settings.subprocess_slots))

    async def analyse(row: dict) -> None:
        nonlocal analysed
        path = Path(row["path"])
        async with semaphore:
            audio = await ffmpeg.audio_hash(path)
            try:
                technical = await ffmpeg.probe(path)
            except ffmpeg.ToolError:
                technical = {}
        meta = await loop.run_in_executor(None, tags.read, path)
        issues = tags.validate(meta)
        await db.execute(
            """
            UPDATE media_file SET
                audio_hash = ?, duration = ?, bitrate = ?, sample_rate = ?,
                channels = ?, codec = ?, title = ?, artist = ?, album = ?,
                album_artist = ?, track_no = ?, disc_no = ?, year = ?,
                has_cover = ?, tag_issues = ?, probed_at = datetime('now')
             WHERE id = ?
            """,
            (
                audio,
                technical.get("duration"),
                technical.get("bitrate"),
                technical.get("sample_rate"),
                technical.get("channels"),
                technical.get("codec"),
                meta.get("title"),
                meta.get("artist"),
                meta.get("album"),
                meta.get("album_artist"),
                meta.get("track_no"),
                meta.get("disc_no"),
                meta.get("year"),
                1 if meta.get("has_cover") else 0,
                ",".join(issues) if issues else None,
                row["id"],
            ),
        )
        analysed += 1
        if analysed % 25 == 0:
            await jobs.progress(job_id, 0.3 + 0.7 * analysed / total, f"Analyse {analysed}/{total}")

    for start in range(0, len(pending), 50):
        await asyncio.gather(*(analyse(row) for row in pending[start : start + 50]))

    await jobs.enqueue(jobs.FIND_DUPES, priority=jobs.PRIORITY_BACKGROUND, dedupe_key="dupes:scan")
    return f"{len(candidates)} Datei-Hashes, {analysed} Dateien analysiert"


# ------------------------------------------------------------- Stufe 3
async def handle_fingerprint(job: dict[str, Any]) -> str:
    """Chromaprint fuer alles, was noch keinen Fingerprint hat.

    Der 32-Bit-Wert an Position 0 dient als Bucket: aehnliche Aufnahmen teilen
    sich hier meist die oberen Bits. Das reduziert den paarweisen Vergleich
    spaeter von O(n^2) auf etwas Handhabbares.
    """
    job_id = int(job["id"])
    limit = int(job["payload"].get("limit") or 5000)
    pending = await db.fetch_all(
        "SELECT m.id, m.path FROM media_file m "
        "LEFT JOIN fingerprint f ON f.media_file_id = m.id "
        "WHERE m.missing = 0 AND f.media_file_id IS NULL LIMIT ?",
        (limit,),
    )
    if not pending:
        return "Alle Dateien haben bereits einen Fingerprint"

    done = 0
    semaphore = asyncio.Semaphore(max(2, settings.subprocess_slots))

    async def one(row: dict) -> None:
        nonlocal done
        async with semaphore:
            result = await ffmpeg.fingerprint(Path(row["path"]))
        if result:
            duration, blob = result
            values = ffmpeg.unpack_fingerprint(blob)
            bucket = (values[0] >> 16) if values else 0
            await db.execute(
                "INSERT OR REPLACE INTO fingerprint(media_file_id, duration, raw_fp, bucket) "
                "VALUES (?,?,?,?)",
                (row["id"], duration, blob, bucket),
            )
        done += 1
        if done % 20 == 0:
            await jobs.progress(job_id, done / len(pending), f"Fingerprint {done}/{len(pending)}")

    for start in range(0, len(pending), 50):
        await asyncio.gather(*(one(row) for row in pending[start : start + 50]))

    await emit(f"{done} Fingerprints berechnet", category="library")
    return f"{done} Fingerprints erstellt"


# ------------------------------------------------------------------ Kennzahlen
async def library_stats() -> dict[str, Any]:
    row = await db.fetch_one(
        """
        SELECT COUNT(*) AS files,
               COALESCE(SUM(size), 0) AS bytes,
               COALESCE(SUM(duration), 0) AS seconds,
               SUM(CASE WHEN tag_issues IS NOT NULL THEN 1 ELSE 0 END) AS with_issues,
               SUM(CASE WHEN has_cover = 0 THEN 1 ELSE 0 END) AS without_cover,
               SUM(CASE WHEN probed_at IS NULL THEN 1 ELSE 0 END) AS unanalysed
          FROM media_file WHERE missing = 0
        """
    ) or {}
    row["fingerprinted"] = int(
        await db.fetch_value("SELECT COUNT(*) FROM fingerprint", (), 0) or 0
    )
    row["missing"] = int(
        await db.fetch_value("SELECT COUNT(*) FROM media_file WHERE missing = 1", (), 0) or 0
    )
    by_format = await db.fetch_all(
        "SELECT ext, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM media_file "
        "WHERE missing = 0 GROUP BY ext ORDER BY n DESC"
    )
    row["formats"] = by_format
    return row
