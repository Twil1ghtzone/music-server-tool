"""Duplikaterkennung und -bereinigung.

Drei Arten, in aufsteigender Unschaerfe:
  exact     identische Bytes            -> file_hash
  audio     identische Musik, andere Tags/Cover -> audio_hash
  acoustic  gleiche Aufnahme, anderes Encoding  -> Chromaprint

Drei harte Regeln, die hier eingebaut und nicht abschaltbar sind:

1. "Anwenden" heisst verschieben, nicht loeschen. Die Verlierer landen im
   Quarantaene-Ordner unter ihrem Originalpfad und bleiben dort eine Frist
   lang liegen (Standard 21 Tage), bevor sie endgueltig verschwinden.
2. Eine geschuetzte Datei wird nie zum Entfernen ausgewaehlt - auch dann
   nicht, wenn sie technisch die schlechtere ist. Geschuetzt heisst: sie
   steht in einer Playlist, ist favorisiert oder bewertet.
3. Automatisch ausgewaehlt wird nur, wo der Fall eindeutig ist. Alles andere
   wird gezeigt und wartet auf eine Entscheidung.

Hintergrund zu Regel 1 und 2: Navidrome haengt Wiedergabezaehler, Bewertungen
und Playlist-Eintraege an seine eigenen media_file-IDs. Verschwindet eine
Datei, verschwindet diese Historie - und wenn ausgerechnet sie in einer
Playlist lag, reisst dort ein Loch, an das niemand mehr herankommt.
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from ..db import db
from ..events import emit
from ..logging_conf import get_logger
from ..clients import navidrome
from . import ffmpeg, jobs, protection

log = get_logger("dedupe")

# Lossless schlaegt lossy, unabhaengig von der Bitrate.
FORMAT_RANK = {
    ".flac": 100, ".wav": 95, ".aiff": 95, ".ape": 92, ".wv": 92,
    ".opus": 70, ".m4a": 62, ".aac": 60, ".ogg": 55, ".mp3": 50,
    ".mpc": 45, ".wma": 30,
}
_SUSPICIOUS = ("copy", "kopie", "duplicate", "dupe", "- kopie", "conflict")

# "Song (2).flac", "Song (3).mp3", "Song(12).flac" - die Form, die Windows,
# Browser und Deemix beim zweiten Schreiben derselben Datei erzeugen. Genau
# das ist der Fall, den man automatisch entscheiden kann: gleicher Name,
# angehaengte Nummer in Klammern.
KOPIE_MUSTER = re.compile(r"^(?P<name>.+?)\s*\((?P<n>\d{1,3})\)$")

# Ordner, die keine Originalalben sind. Was hier drin liegt, verliert gegen
# eine Datei, die im Albumordner des Interpreten steht.
_NEBENORTE = (
    "/downloads", "/download", "/incoming", "/staging", "/import", "/inbox",
    "/neu", "/new", "/unsortiert", "/unsorted", "/temp", "/tmp", "/sonstiges",
    "/various", "/diverse", "/singles", "/compilations", "/sampler",
)

ACOUSTIC_MATCH_THRESHOLD = 0.92
ACOUSTIC_MAX_OFFSET = 20
ACOUSTIC_DURATION_TOLERANCE = 8.0

# Der akustische Vergleich laeuft eimerweise. So viele Fingerabdruecke liegen
# hoechstens gleichzeitig im Speicher - damit bleibt der Lauf unabhaengig von
# der Groesse der Bibliothek berechenbar.
BUCKET_BATCH = 400


# ------------------------------------------------------------------ Bewertung
def kopie_nummer(pfad: str) -> int:
    """Die Zahl aus "Song (2).flac", oder 0 wenn der Name sauber ist."""
    treffer = KOPIE_MUSTER.match(Path(pfad).stem.strip())
    return int(treffer.group("n")) if treffer else 0


def _im_albumordner(row: dict[str, Any]) -> bool:
    """Liegt die Datei dort, wo ein Originalalbum liegen wuerde?

    Die Form ist "…/Interpret/Album/Titel.flac" - mindestens zwei Ebenen
    unter der Musikwurzel, und keine davon ein Sammelordner. Das ist keine
    Wissenschaft, aber es trennt zuverlaessig das gepflegte Album vom
    Download, der irgendwo gelandet ist.

    Geprueft wird ausschliesslich der Teil UNTERHALB der Musikwurzel. Der
    absolute Pfad taugt dafuer nicht: liegt die Bibliothek selbst unter
    /tmp oder /downloads, waere sonst jedes Album ein Nebenort.
    """
    try:
        relativ = Path(row["path"]).relative_to(settings.music_dir)
    except (ValueError, KeyError, TypeError):
        return False
    if len(relativ.parts) < 3:
        return False
    ordner = "/" + "/".join(relativ.parts[:-1]).replace("\\", "/").lower()
    return not any(ort in ordner for ort in _NEBENORTE)


def keeper_score(row: dict[str, Any]) -> float:
    """Je hoeher, desto eher bleibt die Datei erhalten.

    Reihenfolge der Kriterien ist bewusst deterministisch - derselbe Bestand
    ergibt immer denselben Vorschlag. Die Abstaende sind so gewaehlt, dass
    die wichtigen Kriterien die unwichtigen nicht ueberstimmen koennen:
    Schutz schlaegt alles, Format schlaegt Bitrate, Bitrate schlaegt Pfad.
    """
    # Geschuetzt heisst: Navidrome haengt Nutzerdaten daran. Der Abstand ist
    # absichtlich so gross, dass keine Summe technischer Kriterien ihn
    # aufholt - eine geschuetzte MP3 bleibt gegen eine unbenutzte FLAC.
    score = 10000.0 if row.get("protected") else 0.0

    score += float(FORMAT_RANK.get((row.get("ext") or "").lower(), 40))
    score += min(float(row.get("bitrate") or 0) / 10000.0, 40.0)
    score += min(float(row.get("sample_rate") or 0) / 4410.0, 20.0)

    for field in ("title", "artist", "album", "album_artist", "year", "track_no"):
        if row.get(field):
            score += 4
    if row.get("has_cover"):
        score += 12

    # Abgeschnittene Dateien verlieren: laenger ist im Zweifel vollstaendiger.
    score += min(float(row.get("duration") or 0) / 60.0, 10.0)

    # Das Original schlaegt die Kopie. "(2)" ist der haeufigste Fall
    # ueberhaupt und deshalb der klarste: 60 Punkte sind mehr als der
    # Abstand zwischen FLAC und MP3.
    nummer = kopie_nummer(row.get("path") or "")
    if nummer:
        score -= 60 + nummer

    # Das gepflegte Album schlaegt den losen Download.
    if _im_albumordner(row):
        score += 30

    pfad = (row.get("path") or "").lower()
    if any(token in pfad for token in _SUSPICIOUS):
        score -= 25
    # Bei sonst gleichem Stand gewinnt der kuerzere, aufgeraeumtere Pfad.
    score -= len(pfad) / 1000.0
    return round(score, 3)


def auto_auswahl(scored: list[dict[str, Any]]) -> tuple[bool, str]:
    """Darf der Scanner diese Gruppe selbst entscheiden?

    Automatisch heisst: ohne dass jemand hinsieht. Also nur, wenn der Fall
    keine zwei Lesarten hat. Alles andere wird gezeigt und wartet - lieber
    ein Vorschlag zu wenig als eine Datei zu viel im Quarantaeneordner.
    """
    keeper, rest = scored[0], scored[1:]

    if any(r.get("protected") for r in rest):
        return False, "Ein Duplikat ist in einer Playlist, favorisiert oder bewertet"

    # Der Schutz kann den Sieger erzwingen. Ist der geschuetzte Titel dabei
    # die technisch schlechtere Datei, waere die automatische Bereinigung ein
    # Qualitaetsverlust - eine 128er-MP3 in einer Playlist wuerde die FLAC
    # daneben verdraengen. Das ist keine Entscheidung fuer eine Maschine:
    # man kann die Playlist umhaengen und die FLAC behalten, aber das muss
    # jemand wollen.
    if keeper.get("protected"):
        keeper_rang = FORMAT_RANK.get((keeper.get("ext") or "").lower(), 40)
        besser = [r for r in rest
                  if FORMAT_RANK.get((r.get("ext") or "").lower(), 40) > keeper_rang]
        if besser:
            return False, ("Der geschuetzte Titel ist die technisch schlechtere Datei - "
                           "bitte selbst entscheiden")

    # Der haeufigste und klarste Fall: derselbe Name, einmal mit "(2)".
    kopien = [r for r in rest if kopie_nummer(r.get("path") or "")]
    if not kopie_nummer(keeper.get("path") or "") and len(kopien) == len(rest):
        gleicher_ordner = all(
            Path(r["path"]).parent == Path(keeper["path"]).parent for r in rest)
        if gleicher_ordner:
            gleiche_basis = all(
                KOPIE_MUSTER.match(Path(r["path"]).stem.strip()).group("name").strip().lower()
                == Path(keeper["path"]).stem.strip().lower() for r in rest)
            if gleiche_basis:
                return True, "Nummerierte Kopie derselben Datei im selben Ordner"
        return True, "Nummerierte Kopie"

    # Byteweise identisch: es gibt technisch nichts zu entscheiden.
    hashes = {r.get("file_hash") for r in scored}
    if len(hashes) == 1 and None not in hashes:
        return True, "Byteweise identische Dateien"

    # Deutlich bessere Qualitaet und das Original im Albumordner.
    keeper_rang = FORMAT_RANK.get((keeper.get("ext") or "").lower(), 40)
    if _im_albumordner(keeper):
        schlechter = all(
            FORMAT_RANK.get((r.get("ext") or "").lower(), 40) < keeper_rang for r in rest)
        if schlechter:
            return True, "Verlustfrei im Albumordner schlaegt verlustbehaftet"

    return False, "Kein eindeutiger Fall - bitte selbst entscheiden"


async def _store_group(kind: str, signature: str, rows: list[dict],
                       similarity: dict[int, float] | None = None) -> int | None:
    if len(rows) < 2:
        return None
    scored = sorted(rows, key=keeper_score, reverse=True)
    keeper = scored[0]
    wasted = sum(int(r.get("size") or 0) for r in scored[1:])
    darf_auto, warum = auto_auswahl(scored)

    async with db.transaction() as conn:
        cur = await conn.execute(
            "INSERT INTO dupe_group(kind, signature, keeper_id, files, wasted, auto_ok, auto_why) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(kind, signature) DO UPDATE SET "
            "keeper_id = excluded.keeper_id, files = excluded.files, "
            "wasted = excluded.wasted, auto_ok = excluded.auto_ok, "
            "auto_why = excluded.auto_why, state = "
            "CASE WHEN dupe_group.state = 'ignored' THEN 'ignored' ELSE 'open' END "
            "RETURNING id",
            (kind, signature, keeper["id"], len(scored), wasted, int(darf_auto), warum),
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

    # Ohne aktuellen Schutzabgleich wuerde die Auto-Auswahl auf veralteten
    # Markierungen arbeiten. Eine Playlist, die seit dem letzten Lauf
    # entstanden ist, muss vor dem Vorschlag bekannt sein, nicht danach.
    schutz = ""
    if await navidrome.has_credentials_async():
        try:
            await jobs.progress(job_id, 0.02, "Gleiche Playlists und Favoriten ab")
            schutz = await protection.handle_sync({"id": job_id, "payload": {}})
        except Exception as exc:
            # Der Lauf geht weiter, aber ohne Auto-Auswahl: was nicht als
            # geschuetzt bekannt ist, darf nicht ungefragt verschwinden.
            log.warning("Schutzabgleich fehlgeschlagen: %s", exc)
            await emit(
                f"Schutzabgleich fehlgeschlagen - es wird nichts automatisch "
                f"ausgewaehlt: {exc}",
                category="dedupe", level="warn",
            )
            schutz = "fehlgeschlagen"

    exact = await _group_by(job_id, "file_hash", "exact", 0.1, 0.4)
    audio = await _group_by(job_id, "audio_hash", "audio", 0.4, 0.7, exclude_single_file_hash=True)

    acoustic = 0
    if include_acoustic:
        acoustic = await _find_acoustic(job_id)

    if schutz == "fehlgeschlagen":
        # Lieber gar keinen Vorschlag als einen auf unsicherer Grundlage.
        await db.execute(
            "UPDATE dupe_group SET auto_ok = 0, "
            "auto_why = 'Schutzabgleich mit Navidrome fehlgeschlagen' "
            "WHERE state = 'open'")

    total = exact + audio + acoustic
    auto = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM dupe_group WHERE state = 'open' AND auto_ok = 1") or {}
    await emit(
        f"Duplikatsuche: {total} Gruppen (exakt {exact}, Audio {audio}, "
        f"akustisch {acoustic}), davon {auto.get('n', 0)} eindeutig",
        category="dedupe",
    )
    return (f"exakt: {exact}, audio: {audio}, akustisch: {acoustic}, "
            f"eindeutig: {auto.get('n', 0)}")


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
    """Stufe 3: gleiche Aufnahme trotz anderem Encoding.

    Die Fingerabdruecke werden eimerweise geholt, nicht am Stueck. Ein
    Rohfingerabdruck ist einige Kilobyte gross - bei hunderttausend Titeln
    waere "alles laden" ein Gigabyte im Speicher, und genau daran waere der
    Lauf auf einer grossen Bibliothek gescheitert. So haengt der Verbrauch
    nur noch an der Groesse des groessten Eimers.
    """
    eimer = await db.fetch_all(
        "SELECT f.bucket AS bucket, COUNT(*) AS n "
        "  FROM fingerprint f JOIN media_file m ON m.id = f.media_file_id "
        " WHERE m.missing = 0 "
        " GROUP BY f.bucket HAVING COUNT(*) > 1 "
        " ORDER BY f.bucket"
    )
    if not eimer:
        return 0

    already: set[int] = set(
        int(r["media_file_id"])
        for r in await db.fetch_all(
            "SELECT dm.media_file_id FROM dupe_member dm "
            "JOIN dupe_group dg ON dg.id = dm.group_id WHERE dg.kind IN ('exact','audio')"
        )
    )

    made = 0
    processed = 0
    for eintrag in eimer:
        processed += 1
        anzahl = int(eintrag["n"])
        # Ein sehr grosser Eimer bedeutet fast immer Stille oder ein Intro,
        # das viele Titel teilen. Ihn zu vergleichen kostet quadratisch und
        # bringt nichts.
        if anzahl > BUCKET_BATCH:
            log.debug("Eimer %s uebersprungen (%s Eintraege)", eintrag["bucket"], anzahl)
            await jobs.progress(job_id, 0.7 + 0.3 * processed / len(eimer),
                                f"Akustisch: Eimer {processed} von {len(eimer)}")
            continue

        bucket_rows = await db.fetch_all(
            "SELECT f.media_file_id AS id, f.duration, f.raw_fp, m.size "
            "  FROM fingerprint f JOIN media_file m ON m.id = f.media_file_id "
            " WHERE m.missing = 0 AND f.bucket = ?",
            (eintrag["bucket"],),
        )
        if len(bucket_rows) < 2:
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
        # Den Eimer wieder freigeben, bevor der naechste geholt wird.
        decoded.clear()
        await jobs.progress(job_id, 0.7 + 0.3 * processed / len(eimer),
                            f"Akustisch: {made} Gruppen, Eimer {processed} von {len(eimer)}")
    return made


# ------------------------------------------------------------- Anwenden
# ------------------------------------------------------------- Quarantaene
QUARANTAENE_TAGE_SCHLUESSEL = "dedupe.quarantine_days"
QUARANTAENE_TAGE_STANDARD = 21
QUARANTAENE_TAGE_MIN = 1
QUARANTAENE_TAGE_MAX = 365


async def quarantaene_tage() -> int:
    """Wie lange eine entfernte Datei liegen bleibt, bevor sie verschwindet."""
    roh = await db.get_setting(QUARANTAENE_TAGE_SCHLUESSEL)
    try:
        tage = int(roh) if roh is not None else QUARANTAENE_TAGE_STANDARD
    except (TypeError, ValueError):
        tage = QUARANTAENE_TAGE_STANDARD
    return max(QUARANTAENE_TAGE_MIN, min(QUARANTAENE_TAGE_MAX, tage))


async def setze_quarantaene_tage(tage: int) -> int:
    tage = max(QUARANTAENE_TAGE_MIN, min(QUARANTAENE_TAGE_MAX, int(tage)))
    await db.set_setting(QUARANTAENE_TAGE_SCHLUESSEL, str(tage))
    await emit(f"Quarantaenefrist auf {tage} Tage gesetzt", category="dedupe")
    return tage


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

    tage = await quarantaene_tage()
    frei_ab = (datetime.now(timezone.utc) + timedelta(days=tage)).strftime("%Y-%m-%d %H:%M:%S")

    moved = 0
    freed = 0
    geschuetzt_uebersprungen = 0
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
            # Letzte Sperre vor dem Verschieben. Die Auswahl kann aus der
            # Oberflaeche kommen, aus einem alten Vorschlag oder aus einem
            # Lauf von vorgestern - hier wird gegen den aktuellen Stand
            # geprueft, nicht gegen den, der beim Vorschlag galt.
            if member.get("protected"):
                geschuetzt_uebersprungen += 1
                log.info("Uebersprungen (geschuetzt: %s): %s",
                         member.get("protect_why"), member["path"])
                continue
            try:
                ziel = _quarantine(Path(member["path"]))
                await db.execute(
                    "INSERT INTO quarantine_item(original_path, stored_path, media_file_id, "
                    "group_id, size, reason, purge_at) VALUES (?,?,?,?,?,?,?)",
                    (member["path"], str(ziel), member["id"], group_id,
                     int(member.get("size") or 0), group.get("kind"), frei_ab),
                )
                await db.execute("UPDATE media_file SET missing = 1 WHERE id = ?", (member["id"],))
                freed += int(member.get("size") or 0)
                moved += 1
            except Exception as exc:
                log.warning("Quarantaene fehlgeschlagen (%s): %s", member["path"], exc)
        await db.execute("UPDATE dupe_group SET state = 'applied' WHERE id = ?", (group_id,))

    if moved:
        await jobs.enqueue(jobs.NAVIDROME_SCAN, priority=jobs.PRIORITY_BACKGROUND,
                           dedupe_key="scan:after-dedupe")
        await emit(
            f"{moved} Duplikat(e) in Quarantaene, {freed // (1024*1024)} MB freigegeben. "
            f"Endgueltige Loeschung ab {frei_ab} ({tage} Tage).",
            category="dedupe",
            level="warn",
        )
    if geschuetzt_uebersprungen:
        await emit(
            f"{geschuetzt_uebersprungen} Datei(en) uebersprungen: in einer Playlist, "
            f"favorisiert oder bewertet.",
            category="dedupe", level="warn",
        )
    return (f"{moved} Datei(en) verschoben, {freed} Bytes freigegeben, "
            f"{geschuetzt_uebersprungen} geschuetzt uebersprungen")


async def handle_purge(job: dict[str, Any]) -> str:
    """Loescht endgueltig, was seine Frist ueberstanden hat.

    Laeuft regelmaessig und ohne Rueckfrage - die Rueckfrage war das
    Verschieben in die Quarantaene, und die Frist war die Gelegenheit,
    es sich anders zu ueberlegen.
    """
    faellig = await db.fetch_all(
        "SELECT * FROM quarantine_item "
        " WHERE state = 'held' AND purge_at <= datetime('now') "
        " ORDER BY purge_at LIMIT 5000"
    )
    if not faellig:
        return "Nichts faellig"

    geloescht = 0
    frei = 0
    for eintrag in faellig:
        pfad = Path(eintrag["stored_path"])
        try:
            if pfad.exists():
                pfad.unlink()
                frei += int(eintrag.get("size") or 0)
            # Auch wenn die Datei schon weg ist: der Eintrag wird geschlossen,
            # sonst steht er ewig als faellig in der Liste.
            await db.execute(
                "UPDATE quarantine_item SET state = 'purged', "
                "purged_at = datetime('now') WHERE id = ?",
                (eintrag["id"],),
            )
            geloescht += 1
        except OSError as exc:
            log.warning("Endgueltiges Loeschen fehlgeschlagen (%s): %s", pfad, exc)

    if geloescht:
        await emit(
            f"{geloescht} Datei(en) nach Ablauf der Frist endgueltig geloescht, "
            f"{frei // (1024*1024)} MB",
            category="dedupe", level="warn",
        )
    return f"{geloescht} endgueltig geloescht, {frei} Bytes"


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
    eintraege = await db.fetch_all(
        "SELECT * FROM quarantine_item WHERE group_id = ? AND state = 'held'", (group_id,)
    )
    restored = 0
    for eintrag in eintraege:
        restored += await _hole_zurueck(eintrag)
    if restored:
        await db.execute("UPDATE dupe_group SET state = 'open' WHERE id = ?", (group_id,))
    return restored


async def restore_item(item_id: int) -> bool:
    """Holt eine einzelne Datei zurueck."""
    eintrag = await db.fetch_one(
        "SELECT * FROM quarantine_item WHERE id = ? AND state = 'held'", (item_id,))
    if not eintrag:
        return False
    return bool(await _hole_zurueck(eintrag))


async def _hole_zurueck(eintrag: dict) -> int:
    quelle = Path(eintrag["stored_path"])
    ziel = Path(eintrag["original_path"])
    if not quelle.exists():
        # Die Datei ist weg, der Eintrag bleibt sonst ewig offen stehen.
        await db.execute(
            "UPDATE quarantine_item SET state = 'lost' WHERE id = ?", (eintrag["id"],))
        return 0
    if ziel.exists():
        log.warning("Zurueckholen uebersprungen, Ziel belegt: %s", ziel)
        return 0
    try:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        os.replace(quelle, ziel)
    except OSError as exc:
        log.warning("Zurueckholen fehlgeschlagen (%s): %s", quelle, exc)
        return 0
    await db.execute("UPDATE quarantine_item SET state = 'restored' WHERE id = ?",
                     (eintrag["id"],))
    if eintrag.get("media_file_id"):
        await db.execute("UPDATE media_file SET missing = 0 WHERE id = ?",
                         (eintrag["media_file_id"],))
    return 1


async def quarantaene(state: str = "held", limit: int = 200, offset: int = 0) -> dict[str, Any]:
    """Was in der Quarantaene liegt, mit verbleibender Frist."""
    gesamt = await db.fetch_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes "
        "FROM quarantine_item WHERE state = ?", (state,)) or {}
    zeilen = await db.fetch_all(
        "SELECT q.*, "
        "  CAST((julianday(q.purge_at) - julianday('now')) AS INTEGER) AS days_left "
        "  FROM quarantine_item q WHERE q.state = ? "
        " ORDER BY q.purge_at LIMIT ? OFFSET ?",
        (state, limit, offset),
    )
    for zeile in zeilen:
        zeile["name"] = Path(zeile["original_path"]).name
        zeile["folder"] = str(Path(zeile["original_path"]).parent)
    return {
        "items": zeilen,
        "total": int(gesamt.get("n") or 0),
        "bytes": int(gesamt.get("bytes") or 0),
        "days": await quarantaene_tage(),
        "offset": offset,
        "limit": limit,
    }


# ------------------------------------------------------------------ Abfragen
async def groups(state: str = "open", limit: int = 25, offset: int = 0,
                 kind: str | None = None, nur_auto: bool = False) -> dict[str, Any]:
    """Eine Seite Duplikatgruppen.

    Seitenweise, weil eine grosse Bibliothek tausende Gruppen ergibt und
    niemand eine Liste mit tausend Eintraegen bedient - und weil das
    Zusammensuchen der Mitglieder je Gruppe eine eigene Abfrage kostet.
    """
    bedingungen = ["state = ?"]
    werte: list[Any] = [state]
    if kind and kind != "alle":
        bedingungen.append("kind = ?")
        werte.append(kind)
    if nur_auto:
        bedingungen.append("auto_ok = 1")
    where = " AND ".join(bedingungen)

    kopf = await db.fetch_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(wasted),0) AS wasted, "
        "  SUM(CASE WHEN auto_ok = 1 THEN 1 ELSE 0 END) AS auto "
        "FROM dupe_group WHERE " + where, werte) or {}

    rows = await db.fetch_all(
        "SELECT * FROM dupe_group WHERE " + where
        + " ORDER BY wasted DESC, id LIMIT ? OFFSET ?",
        (*werte, limit, offset),
    )
    for group in rows:
        group["members"] = await db.fetch_all(
            "SELECT m.id, m.path, m.size, m.ext, m.bitrate, m.sample_rate, m.duration, "
            "       m.has_cover, m.title, m.artist, m.album, m.year, m.track_no, "
            "       m.protected, m.protect_why, m.nd_id, dm.score, dm.similarity "
            "  FROM dupe_member dm JOIN media_file m ON m.id = dm.media_file_id "
            " WHERE dm.group_id = ? ORDER BY dm.score DESC",
            (group["id"],),
        )
        for mitglied in group["members"]:
            mitglied["copy_no"] = kopie_nummer(mitglied.get("path") or "")
            mitglied["name"] = Path(mitglied["path"]).name
            mitglied["folder"] = str(Path(mitglied["path"]).parent)
    return {
        "groups": rows,
        "total": int(kopf.get("n") or 0),
        "wasted": int(kopf.get("wasted") or 0),
        "auto": int(kopf.get("auto") or 0),
        "offset": offset,
        "limit": limit,
    }


async def auto_gruppen(state: str = "open") -> list[int]:
    """Die IDs aller Gruppen, die der Scanner selbst entscheiden wuerde."""
    zeilen = await db.fetch_all(
        "SELECT id FROM dupe_group WHERE state = ? AND auto_ok = 1 ORDER BY wasted DESC",
        (state,))
    return [int(z["id"]) for z in zeilen]


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
    auto = await db.fetch_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(wasted),0) AS wasted "
        "FROM dupe_group WHERE state = 'open' AND auto_ok = 1") or {}
    row["auto"] = int(auto.get("n") or 0)
    row["auto_wasted"] = int(auto.get("wasted") or 0)
    quar = await db.fetch_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes "
        "FROM quarantine_item WHERE state = 'held'") or {}
    row["quarantine"] = int(quar.get("n") or 0)
    row["quarantine_bytes"] = int(quar.get("bytes") or 0)
    row["quarantine_days"] = await quarantaene_tage()
    return row
