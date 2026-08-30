"""Tag-Lesen und -Schreiben ueber mutagen.

Bewusst KEIN Subprozess auf eyeD3: pro Datei einen Prozess zu starten ist bei
einer Bibliothek mit zehntausenden Titeln der teuerste denkbare Weg, eyeD3
deckt FLAC/M4A/Opus nicht gleichwertig ab, und Textausgabe zu parsen ist
unnoetig fehleranfaellig. mutagen liest und schreibt alle relevanten Formate
im Prozess.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp4 import MP4

from ..logging_conf import get_logger

log = get_logger("tags")

FIELDS = ("title", "artist", "album", "album_artist", "track_no", "disc_no", "year", "genre")

_EASY_KEYS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "album_artist": "albumartist",
    "genre": "genre",
}


def _first(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None
    return str(value)


def _number(value: Any) -> int | None:
    text = _first(value)
    if not text:
        return None
    head = text.split("/")[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def _year(value: Any) -> int | None:
    text = _first(value)
    if not text or len(text) < 4:
        return None
    try:
        return int(text[:4])
    except ValueError:
        return None


def has_cover(path: Path) -> bool:
    try:
        audio = MutagenFile(path)
    except Exception:
        return False
    if audio is None:
        return False
    if isinstance(audio, FLAC):
        return bool(audio.pictures)
    if isinstance(audio, MP4):
        return bool(audio.tags and audio.tags.get("covr"))
    try:
        tags = ID3(path)
        return bool(tags.getall("APIC"))
    except (ID3NoHeaderError, Exception):
        pass
    pictures = getattr(audio, "pictures", None)
    return bool(pictures)


def read(path: Path) -> dict[str, Any]:
    """Liest die Felder, die fuer Anzeige, Import und Duplikatbewertung zaehlen."""
    out: dict[str, Any] = {k: None for k in FIELDS}
    try:
        audio = MutagenFile(path, easy=True)
    except Exception as exc:
        log.debug("Tags nicht lesbar (%s): %s", path, exc)
        return out
    if audio is None or audio.tags is None:
        out["has_cover"] = has_cover(path)
        return out

    tags = audio.tags
    for field, key in _EASY_KEYS.items():
        out[field] = _first(tags.get(key))
    out["track_no"] = _number(tags.get("tracknumber"))
    out["disc_no"] = _number(tags.get("discnumber"))
    out["year"] = _year(tags.get("date") or tags.get("originaldate") or tags.get("year"))
    out["has_cover"] = has_cover(path)
    return out


def write(path: Path, changes: dict[str, Any]) -> None:
    """Schreibt einzelne Felder zurueck. Nicht genannte Felder bleiben, wie sie
    sind - ein Batch-Edit darf niemals stillschweigend Tags loeschen."""
    audio = MutagenFile(path, easy=True)
    if audio is None:
        raise ValueError(f"Format nicht unterstuetzt: {path.name}")
    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception as exc:  # bereits vorhanden o.ae.
            log.debug("add_tags: %s", exc)

    for field, value in changes.items():
        if field not in FIELDS:
            continue
        key = _EASY_KEYS.get(field)
        if field == "track_no":
            key = "tracknumber"
        elif field == "disc_no":
            key = "discnumber"
        elif field == "year":
            key = "date"
        if not key:
            continue
        if value in (None, ""):
            audio.tags.pop(key, None)
        else:
            audio.tags[key] = [str(value)]
    audio.save()


def validate(tags: dict[str, Any]) -> list[str]:
    """Findet die Maengel, die spaeter in Navidrome sichtbar weh tun."""
    issues: list[str] = []
    if not (tags.get("title") or "").strip():
        issues.append("titel-fehlt")
    if not (tags.get("artist") or "").strip():
        issues.append("artist-fehlt")
    if not (tags.get("album") or "").strip():
        issues.append("album-fehlt")
    if not tags.get("album_artist"):
        issues.append("albumartist-fehlt")
    if not tags.get("year"):
        issues.append("jahr-fehlt")
    if not tags.get("track_no"):
        issues.append("tracknummer-fehlt")
    if not tags.get("has_cover"):
        issues.append("cover-fehlt")
    title = (tags.get("title") or "")
    if title != title.strip():
        issues.append("titel-leerzeichen")
    if title.isupper() and len(title) > 4:
        issues.append("titel-grossbuchstaben")
    return issues


def apply_from_provider(path: Path, track: dict[str, Any]) -> None:
    """Nach dem Download: die Katalogmetadaten als Wahrheit setzen, aber nur
    fuer Felder, die Deemix leer gelassen hat."""
    current = read(path)
    changes: dict[str, Any] = {}
    mapping = {
        "title": track.get("title"),
        "artist": track.get("artist"),
        "album": track.get("album"),
        "album_artist": track.get("album_artist") or track.get("artist"),
        "year": track.get("year"),
        "track_no": track.get("track_no"),
        "disc_no": track.get("disc_no"),
    }
    for field, value in mapping.items():
        if value and not current.get(field):
            changes[field] = value
    if changes:
        write(path, changes)
