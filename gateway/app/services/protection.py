"""Was der Duplikatscanner nicht anfassen darf.

Navidrome haengt Nutzerdaten an seine eigenen media_file-IDs: Playlist-
Eintraege, Favoriten, Bewertungen, Wiedergabezaehler. Verschwindet eine Datei,
verschwindet diese Historie mit ihr - und wenn sie in einer Playlist stand,
reisst dort ein Loch, an das niemand mehr herankommt.

Deshalb wird der Index vor jedem Bereinigungsvorschlag mit Navidrome
abgeglichen: jede Datei, an der ein Mensch etwas getan hat, bekommt eine
Schutzmarkierung. Der Scanner darf sie weiterhin in einer Gruppe zeigen -
man soll ja sehen, dass es sie doppelt gibt - aber nie zum Entfernen
auswaehlen.

Abgeglichen wird ueber ALLE bekannten Konten: den Zugang des Gateways und
jeden persoenlichen Zugang eines Dashboard-Benutzers. Sonst haette wer auch
immer den Lauf startet, die Playlists der anderen geloescht - Favoriten und
Bewertungen sind in Navidrome pro Benutzer, und ein Schutz, der nur den
eigenen kennt, ist keiner.

Nebenbei faellt dabei die Zuordnung Pfad -> Navidrome-ID ab. Die braucht die
Oberflaeche, um zu einer lokalen Datei Cover und Hoerprobe zu zeigen.
"""
from __future__ import annotations

from typing import Any

from ..clients import navidrome
from ..config import settings
from ..db import db
from ..events import emit
from ..logging_conf import get_logger
from . import jobs

log = get_logger("protection")

# Warum eine Datei geschuetzt ist. Reihenfolge = Rangfolge in der Anzeige:
# der schwerwiegendste Grund gewinnt.
GRUENDE = ("playlist", "favorit", "bewertet")


def _schluessel(pfad: str) -> str:
    """Vergleichbarer Pfad.

    Navidrome liefert den Pfad relativ zu seinem Musikordner, der Index haelt
    ihn absolut. Verglichen wird deshalb ueber die Kleinschreibung mit
    einheitlichen Trennern - und der Abgleich passiert ueber das Ende des
    Pfades, nicht ueber seinen Anfang.
    """
    return pfad.replace("\\", "/").strip("/").lower()


async def _index_nach_pfad() -> dict[str, int]:
    """Alle indizierten Dateien, nach vergleichbarem Pfad."""
    zeilen = await db.fetch_all("SELECT id, path FROM media_file WHERE missing = 0")
    return {_schluessel(z["path"]): int(z["id"]) for z in zeilen}


def _finde(index: dict[str, int], nd_pfad: str) -> int | None:
    """Sucht die Indexzeile zu einem Navidrome-Pfad.

    Erst der direkte Treffer, dann ueber die Endung: "/music/A/B.flac" im
    Index gegen "A/B.flac" von Navidrome. Ein Suffixvergleich ueber alle
    Schluessel waere quadratisch, deshalb wird der Musikordner davorgesetzt
    und nur das geprueft.
    """
    schluessel = _schluessel(nd_pfad)
    treffer = index.get(schluessel)
    if treffer is not None:
        return treffer
    mit_wurzel = _schluessel(f"{settings.music_dir}/{nd_pfad}")
    return index.get(mit_wurzel)


async def handle_sync(job: dict[str, Any]) -> str:
    """Holt Playlists, Favoriten und Bewertungen und markiert den Index."""
    job_id = int(job["id"])

    konten = await navidrome.alle_konten()
    if not konten:
        # Ohne Zugang laesst sich nicht feststellen, was geschuetzt ist. Das
        # ist kein Grund weiterzumachen, sondern einer aufzuhoeren: ohne
        # diese Information waere jeder Bereinigungsvorschlag ein Risiko.
        raise RuntimeError(
            "Der Abgleich braucht Navidrome-Zugangsdaten. Ohne sie laesst sich "
            "nicht feststellen, welche Titel in Playlists stehen oder bewertet "
            "sind - und ohne das darf nichts bereinigt werden. Zugang unter "
            "Diagnose eintragen oder ein Konto unter Mediathek verbinden."
        )

    await jobs.progress(job_id, 0.05, "Lese den lokalen Index")
    index = await _index_nach_pfad()
    if not index:
        return "Der Index ist leer - erst die Bibliothek indizieren"

    # nd-ID -> Indexzeile. Wird beim Durchlauf gefuellt und danach fuer die
    # Playlist-Eintraege gebraucht, die nur IDs nennen.
    nach_nd: dict[str, int] = {}
    geschuetzt: dict[int, str] = {}
    gesehen = 0

    # --- Je Konto einmal durch die Bibliothek ------------------------------
    # Favoriten und Bewertungen sind in Navidrome pro Benutzer. Ein Durchlauf
    # mit einem Konto sieht die des anderen nicht - also alle abfragen.
    anteil_je_konto = 0.8 / len(konten)
    for nummer, konto in enumerate(konten):
        basis = 0.1 + nummer * anteil_je_konto
        wer = konto.get("u", "?")
        await jobs.progress(job_id, basis, f"Konto {nummer + 1} von {len(konten)}: {wer}")

        async for song in navidrome.iter_songs_mit(konto):
            gesehen += 1
            pfad = song.get("path") or ""
            nd_id = str(song.get("id") or "")
            if not pfad or not nd_id:
                continue
            zeile = _finde(index, pfad)
            if zeile is None:
                continue
            nach_nd[nd_id] = zeile

            if song.get("starred"):
                geschuetzt[zeile] = "favorit"
            elif song.get("userRating"):
                geschuetzt.setdefault(zeile, "bewertet")

            if gesehen % 2000 == 0:
                await jobs.progress(
                    job_id, min(basis + anteil_je_konto * 0.6, 0.88),
                    f"{wer}: {gesehen} Titel abgeglichen")

        # --- Playlists dieses Kontos ---------------------------------------
        try:
            listen = await navidrome.playlists_mit(konto)
        except Exception as exc:
            log.warning("Playlists von %s nicht lesbar: %s", wer, exc)
            listen = []
        for liste in listen:
            try:
                eintraege = await navidrome.playlist_songs_mit(konto, str(liste.get("id")))
            except Exception as exc:
                log.warning("Playlist %s nicht lesbar: %s", liste.get("name"), exc)
                continue
            for eintrag in eintraege:
                zeile = nach_nd.get(str(eintrag.get("id")))
                if zeile is None and eintrag.get("path"):
                    zeile = _finde(index, eintrag["path"])
                if zeile is not None:
                    # Playlist schlaegt alles: hier haengt eine Reihenfolge
                    # dran, die sich nicht wiederherstellen laesst.
                    geschuetzt[zeile] = "playlist"
        await jobs.progress(job_id, min(basis + anteil_je_konto * 0.85, 0.92),
                            f"{wer}: {len(listen)} Playlist(s)")

        # --- Favoriten nachziehen ------------------------------------------
        # getStarred2 ist billiger und vollstaendiger als das Feld am Titel:
        # manche Navidrome-Versionen liefern "starred" nur in der Detailansicht.
        try:
            for song in await navidrome.starred_songs_mit(konto):
                zeile = nach_nd.get(str(song.get("id")))
                if zeile is None and song.get("path"):
                    zeile = _finde(index, song["path"])
                if zeile is not None:
                    geschuetzt.setdefault(zeile, "favorit")
        except Exception as exc:
            log.warning("Favoriten von %s nicht abrufbar: %s", wer, exc)

    # --- Schreiben ----------------------------------------------------------
    await jobs.progress(job_id, 0.95, "Schreibe die Markierungen")
    async with db.transaction() as conn:
        await conn.execute("UPDATE media_file SET protected = 0, protect_why = NULL")
        for zeile, warum in geschuetzt.items():
            await conn.execute(
                "UPDATE media_file SET protected = 1, protect_why = ? WHERE id = ?",
                (warum, zeile),
            )
        for nd_id, zeile in nach_nd.items():
            await conn.execute("UPDATE media_file SET nd_id = ? WHERE id = ?", (nd_id, zeile))

    nach_grund: dict[str, int] = {}
    for warum in geschuetzt.values():
        nach_grund[warum] = nach_grund.get(warum, 0) + 1
    teile = ", ".join(f"{n} {g}" for g, n in sorted(nach_grund.items()))

    await emit(
        f"Abgleich mit Navidrome ueber {len(konten)} Konto/Konten: "
        f"{len(geschuetzt)} Titel geschuetzt ({teile or 'keine'}), "
        f"{len(nach_nd)} zugeordnet",
        category="dedupe",
    )
    return (f"{len(konten)} Konto/Konten, {gesehen} Titel gelesen, "
            f"{len(nach_nd)} zugeordnet, {len(geschuetzt)} geschuetzt "
            f"({teile or 'keine'})")


async def stand() -> dict[str, Any]:
    """Wie viel im Index geschuetzt und wie viel zugeordnet ist."""
    zeile = await db.fetch_one(
        "SELECT COUNT(*) AS gesamt, "
        "  SUM(CASE WHEN protected = 1 THEN 1 ELSE 0 END) AS geschuetzt, "
        "  SUM(CASE WHEN nd_id IS NOT NULL THEN 1 ELSE 0 END) AS zugeordnet "
        "FROM media_file WHERE missing = 0"
    ) or {}
    nach_grund = await db.fetch_all(
        "SELECT protect_why AS grund, COUNT(*) AS n FROM media_file "
        "WHERE protected = 1 GROUP BY protect_why"
    )
    return {**zeile, "by_reason": nach_grund}
