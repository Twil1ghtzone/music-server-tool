"""Duplikaterkennung und -bereinigung.

Drei Arten, in aufsteigender Unschaerfe:
  exact     identische Bytes            -> file_hash
  audio     identische Musik, andere Tags/Cover -> audio_hash
  acoustic  gleiche Aufnahme, anderes Encoding  -> Chromaprint

Zwei harte Regeln, die hier eingebaut und nicht abschaltbar sind:

1. Es wird nie automatisch geloescht. Ein Lauf erzeugt Vorschlaege; das
   Anwenden ist ein separater, ausdruecklicher Schritt.
2. "Anwenden" heisst verschieben, nicht loeschen. Die Verlierer landen im
   Quarantaene-Ordner unter ihrem Originalpfad. Endgueltiges Loeschen macht
   der Mensch, nachdem er nachgesehen hat.

Hintergrund zu Regel 2: Navidrome haengt Wiedergabezaehler, Bewertungen und
Playlist-Eintraege an seine eigenen media_file-IDs. Verschwindet eine Datei,
verschwindet diese Historie - und wenn ausgerechnet sie in einer Playlist lag,
reisst dort ein Loch. Quarantaene macht diesen Schritt umkehrbar.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from ..config import settings
from ..db import db
from ..events import emit
from ..logging_conf import get_logger
from . import ffmpeg, jobs

log = get_logger("dedupe")

# Lossless schlaegt lossy, unabhaengig von der Bitrate.
FORMAT_RANK = {
    ".flac": 100, ".wav": 95, ".aiff": 95, ".ape": 92, ".wv": 92,
    ".opus": 70, ".m4a": 62, ".aac": 60, ".ogg": 55, ".mp3": 50,
    ".mpc": 45, ".wma": 30,
}
_SUSPICIOUS = ("copy", "kopie", "duplicate", "dupe", "(1)", "(2)", "- kopie", "conflict")

ACOUSTIC_MATCH_THRESHOLD = 0.92
ACOUSTIC_MAX_OFFSET = 20
ACOUSTIC_DURATION_TOLERANCE = 8.0


# ------------------------------------------------------------------ Bewertung
def keeper_score(row: dict[str, Any]) -> float:
    """Je hoeher, desto eher bleibt die Datei erhalten.

    Reihenfolge der Kriterien ist bewusst deterministisch - derselbe Bestand
    ergibt immer denselben Vorschlag.
    """
    score = float(FORMAT_RANK.get((row.get("ext") or "").lower(), 40))
    score += min(float(row.get("bitrate") or 0) / 10000.0, 40.0)
    score += min(float(row.get("sample_rate") or 0) / 4410.0, 20.0)

    for field in ("title", "artist", "album", "album_artist", "year", "track_no"):
        if row.get(field):
            score += 4
    if row.get("has_cover"):
        score += 12

    # Abgeschnittene Dateien verlieren: laenger ist im Zweifel vollstaendiger.
    score += min(float(row.get("duration") or 0) / 60.0, 10.0)

    path = (row.get("path") or "").lower()
    if any(token in path for token in _SUSPICIOUS):
        score -= 25
    # Bei sonst gleichem Stand gewinnt der kuerzere, aufgeraeumtere Pfad.
    score -= len(path) / 1000.0
    return round(score, 3)


async def _store_group(kind: str, signature: str, rows: list[dict],
                       similarity: dict[int, float] | None = None) -> int | None:
    if len(rows) < 2:
        return None
    scored = sorted(rows, key=keeper_score, reverse=True)
    keeper = scored[0]
    wasted = sum(int(r.get("size") or 0) for r in scored[1:])

    async with db.transaction() as conn:
        cur = await conn.execute(
            "INSERT INTO dupe_group(kind, signature, keeper_id, files, wasted) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(kind, signature) DO UPDATE SET "
            "keeper_id = excluded.keeper_id, files = excluded.files, "
            "wasted = excluded.wasted, state = "
            "CASE WHEN dupe_group.state = 'ignored' THEN 'ignored' ELSE 'open' END "
            "RETURNING id",
            (kind, signature, keeper["id"], len(scored), wasted),
        )
        row = await cur.fetchone()
        group_id = int(row["id"])
        await conn.execute("DELETE FROM dupe_member WHERE group_id = ?", (group_id,))
        for member in scored:
            await conn.execute(
                "INSERT INTO dupe_member(group_id, media_file_id, score, similarity) "
                "VALUES (?,?,?,?)",
                (
                    group_id,
                    member["id"],
                    keeper_score(member),
                    (similarity or {}).get(member["id"]),
                ),
            )
    return group_id


# ------------------------------------------------------------- Stufe 1 + 2
async def handle_find_dupes(job: dict[str, Any]) -> str:
    job_id = int(job["id"])
    include_acoustic = bool(job["payload"].get("acoustic"))

    # Alte offene Vorschlaege verwerfen - der Bestand kann sich geaendert
    # haben. 'ignored' bleibt stehen, das ist eine Nutzerentscheidung.
    await db.execute("DELETE FROM dupe_group WHERE state = 'open'")

    exact = await _group_by(job_id, "file_hash", "exact", 0.0, 0.35)
    audio = await _group_by(job_id, "audio_hash", "audio", 0.35, 0.7, exclude_single_file_hash=True)

    acoustic = 0
    if include_acoustic:
        acoustic = await _find_acoustic(job_id)

    total = exact + audio + acoustic
    await emit(
        f"Duplikatsuche: {total} Gruppen (exakt {exact}, Audio {audio}, akustisch {acoustic})",
        category="dedupe",
    )
    return f"exakt: {exact}, audio: {audio}, akustisch: {acoustic}"


async def _group_by(job_id: int, column: str, kind: str, lo: float, hi: float,
                    exclude_single_file_hash: bool = False) -> int:
    signatures = await db.fetch_all(
        f"SELECT {column} AS sig, COUNT(*) AS n FROM media_file "
        f"WHERE missing = 0 AND {column} IS NOT NULL "
        f"GROUP BY {column} HAVING COUNT(*) > 1"
    )
    made = 0
    for index, entry in enumerate(signatures, start=1):
        rows = await db.fetch_all(
            f"SELECT * FROM media_file WHERE missing = 0 AND {column} = ?", (entry["sig"],)
        )
        if exclude_single_file_hash:
            # Byteweise identische Dateien sind schon in 'exact' erfasst.
            hashes = {r.get("file_hash") for r in rows}
            if len(hashes) == 1 and None not in hashes:
                continue
        if await _store_group(kind, f"{kind}:{entry['sig']}", rows):
            made += 1
        if index % 20 == 0:
            await jobs.progress(job_id, lo + (hi - lo) * index / len(signatures))
    return made


# ---------------------------------------------------------------- Stufe 3
def similarity(a: list[int], b: list[int], max_offset: int = ACOUSTIC_MAX_OFFSET) -> float:
    """Uebereinstimmung zweier Roh-Fingerprints, tolerant gegenueber Versatz.

    Chromaprint-Subfingerprints sind 32-Bit-Woerter; zwei Aufnahmen derselben
    Musik unterscheiden sich in wenigen Bits pro Wort. Verglichen wird die
    Bitfehlerrate ueber alle plausiblen Startversaetze - ein Intro von einer
    halben Sekunde Unterschied darf das Ergebnis nicht kippen.
    """
    if not a or not b:
        return 0.0
    best = 0.0
    for offset in range(-max_offset, max_offset + 1):
        if offset >= 0:
            left, right = a[offset:], b
        else:
            left, right = a, b[-offset:]
        span = min(len(left), len(right))
        if span < 40:
            continue
        errors = 0
        for i in range(span):
            errors += (left[i] ^ right[i]).bit_count()
        score = 1.0 - errors / (span * 32.0)
        if score > best:
            best = score
            if best > 0.99:
                break
    return round(best, 4)


async def _find_acoustic(job_id: int) -> int:
    rows = await db.fetch_all(
        "SELECT f.media_file_id AS id, f.duration, f.raw_fp, f.bucket, m.size "
        "  FROM fingerprint f JOIN media_file m ON m.id = f.media_file_id "
        " WHERE m.missing = 0 "
        " ORDER BY f.bucket"
    )
    if len(rows) < 2:
        return 0

    # Bucketing: nur Kandidaten mit demselben Prefix werden ueberhaupt
    # verglichen. Ohne das waere der Vergleich quadratisch ueber die ganze
    # Bibliothek und damit unbrauchbar.
    buckets: dict[int, list[dict]] = {}
    for row in rows:
        buckets.setdefault(int(row["bucket"]), []).append(row)

    already: set[int] = set(
        int(r["media_file_id"])
        for r in await db.fetch_all(
            "SELECT dm.media_file_id FROM dupe_member dm "
            "JOIN dupe_group dg ON dg.id = dm.group_id WHERE dg.kind IN ('exact','audio')"
        )
    )

    made = 0
    processed = 0
    for bucket_rows in buckets.values():
        processed += 1
        if len(bucket_rows) < 2 or len(bucket_rows) > 200:
            continue
        decoded = {
            int(r["id"]): ffmpeg.unpack_fingerprint(r["raw_fp"]) for r in bucket_rows
        }
        used: set[int] = set()
        for i, left in enumerate(bucket_rows):
            lid = int(left["id"])
            if lid in used or lid in already:
                continue
            cluster = [lid]
            scores = {lid: 1.0}
            for right in bucket_rows[i + 1 :]:
                rid = int(right["id"])
                if rid in used or rid in already:
                    continue
                if abs(float(left["duration"]) - float(right["duration"])) > ACOUSTIC_DURATION_TOLERANCE:
                    continue
                score = similarity(decoded[lid], decoded[rid])
                if score >= ACOUSTIC_MATCH_THRESHOLD:
                    cluster.append(rid)
                    scores[rid] = score
            if len(cluster) > 1:
                used.update(cluster)
                placeholders = ",".join("?" * len(cluster))
                members = await db.fetch_all(
                    f"SELECT * FROM media_file WHERE id IN ({placeholders})", cluster
                )
                if await _store_group(
                    "acoustic", f"acoustic:{min(cluster)}", members, scores
                ):
                    made += 1
        await jobs.progress(job_id, 0.7 + 0.3 * processed / max(len(buckets), 1))
    return made


# ------------------------------------------------------------- Anwenden
async def handle_apply(job: dict[str, Any]) -> str:
    """Verschiebt die Nicht-Keeper einer Gruppe in die Quarantaene."""
    if not settings.allow_dedupe_apply:
        # Schutzschalter fuer den ersten Betrieb: suchen und anzeigen ja,
        # anfassen nein. Erst freischalten, wenn die Vorschlaege geprueft sind.
        raise RuntimeError(
            "Bereinigung ist gesperrt. Zum Freischalten "
            "GATEWAY_ALLOW_DEDUPE_APPLY=true setzen und den Worker neu starten."
        )

    payload = job["payload"]
    group_ids: list[int] = [int(g) for g in payload.get("groups") or []]
    if not group_ids:
        return "Keine Gruppen angegeben"

    moved = 0
    freed = 0
    for group_id in group_ids:
        group = await db.fetch_one("SELECT * FROM dupe_group WHERE id = ?", (group_id,))
        if not group or group["state"] != "open":
            continue
        members = await db.fetch_all(
            "SELECT m.* FROM dupe_member dm JOIN media_file m ON m.id = dm.media_file_id "
            "WHERE dm.group_id = ?",
            (group_id,),
        )
        for member in members:
            if member["id"] == group["keeper_id"]:
                continue
            try:
                freed += int(member.get("size") or 0)
                _quarantine(Path(member["path"]))
                await db.execute("UPDATE media_file SET missing = 1 WHERE id = ?", (member["id"],))
                moved += 1
            except Exception as exc:
                log.warning("Quarantaene fehlgeschlagen (%s): %s", member["path"], exc)
        await db.execute("UPDATE dupe_group SET state = 'applied' WHERE id = ?", (group_id,))

    if moved:
        await jobs.enqueue(jobs.NAVIDROME_SCAN, priority=jobs.PRIORITY_BACKGROUND,
                           dedupe_key="scan:after-dedupe")
        await emit(
            f"{moved} Duplikat(e) in Quarantaene, {freed // (1024*1024)} MB freigegeben",
            category="dedupe",
            level="warn",
        )
    return f"{moved} Datei(en) verschoben, {freed} Bytes freigegeben"


def _quarantine(source: Path) -> Path:
    """Behaelt die Verzeichnisstruktur bei, damit ein Zurueckholen trivial ist."""
    try:
        relative = source.relative_to(settings.music_dir)
    except ValueError:
        relative = Path(source.name)
    destination = settings.quarantine_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination = destination.with_name(f"{destination.stem}.{os.getpid()}{destination.suffix}")
    try:
        os.replace(source, destination)
    except OSError:
        shutil.move(str(source), str(destination))
    return destination


async def restore(group_id: int) -> int:
    """Holt eine angewandte Gruppe aus der Quarantaene zurueck."""
    members = await db.fetch_all(
        "SELECT m.* FROM dupe_member dm JOIN media_file m ON m.id = dm.media_file_id "
        "WHERE dm.group_id = ?",
        (group_id,),
    )
    restored = 0
    for member in members:
        original = Path(member["path"])
        if original.exists():
            continue
        try:
            relative = original.relative_to(settings.music_dir)
        except ValueError:
            relative = Path(original.name)
        source = settings.quarantine_dir / relative
        if not source.exists():
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, original)
        await db.execute("UPDATE media_file SET missing = 0 WHERE id = ?", (member["id"],))
        restored += 1
    if restored:
        await db.execute("UPDATE dupe_group SET state = 'open' WHERE id = ?", (group_id,))
    return restored


# ------------------------------------------------------------------ Abfragen
async def groups(state: str = "open", limit: int = 100) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT * FROM dupe_group WHERE state = ? ORDER BY wasted DESC LIMIT ?", (state, limit)
    )
    for group in rows:
        group["members"] = await db.fetch_all(
            "SELECT m.id, m.path, m.size, m.ext, m.bitrate, m.duration, m.has_cover, "
            "       m.title, m.artist, m.album, dm.score, dm.similarity "
            "  FROM dupe_member dm JOIN media_file m ON m.id = dm.media_file_id "
            " WHERE dm.group_id = ? ORDER BY dm.score DESC",
            (group["id"],),
        )
    return rows


async def summary() -> dict[str, Any]:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS groups, COALESCE(SUM(files),0) AS files, "
        "COALESCE(SUM(wasted),0) AS wasted FROM dupe_group WHERE state = 'open'"
    ) or {}
    by_kind = await db.fetch_all(
        "SELECT kind, COUNT(*) AS n, COALESCE(SUM(wasted),0) AS wasted "
        "FROM dupe_group WHERE state = 'open' GROUP BY kind"
    )
    row["by_kind"] = by_kind
    return row
