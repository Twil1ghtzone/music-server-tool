"""Subprozess-Bruecke zu ffmpeg / ffprobe / fpcalc.

Leitlinie fuer "Subprozess oder Bibliothek": Subprozess nur dort, wo das CLI
ein stabiler Vertrag mit maschinenlesbarer Ausgabe ist und die Arbeitseinheit
gross genug ist, dass der Prozessstart nicht dominiert. Das trifft auf
ffprobe (-of json), ffmpeg (-f md5) und fpcalc (-json) zu. Tags dagegen laufen
ueber mutagen im Prozess - dort waere ein Prozessstart pro Datei reine
Verschwendung.

Alle Aufrufe: argv-Liste (nie shell=True), harter Timeout, begrenzte
Parallelitaet ueber ein Semaphor.
"""
from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path
from typing import Any

from ..config import settings
from ..logging_conf import get_logger

log = get_logger("ffmpeg")

_slots: asyncio.Semaphore | None = None
MAX_OUTPUT = 4 * 1024 * 1024


def _semaphore() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(settings.subprocess_slots)
    return _slots


class ToolError(RuntimeError):
    pass


async def run(argv: list[str], timeout: float = 120.0) -> tuple[int, bytes, bytes]:
    async with _semaphore():
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ToolError(f"Programm nicht gefunden: {argv[0]}") from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ToolError(f"Zeitueberschreitung: {' '.join(argv[:3])}")
    return proc.returncode or 0, out[:MAX_OUTPUT], err[:8192]


# ------------------------------------------------------------------ ffprobe
async def probe(path: Path) -> dict[str, Any]:
    code, out, err = await run(
        [
            settings.ffprobe_bin,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-select_streams", "a:0",
            str(path),
        ],
        timeout=60.0,
    )
    if code != 0:
        raise ToolError(err.decode("utf-8", "replace").strip() or f"ffprobe rc={code}")
    data = json.loads(out or b"{}")
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    return {
        "duration": _float(stream.get("duration") or fmt.get("duration")),
        "bitrate": _int(stream.get("bit_rate") or fmt.get("bit_rate")),
        "sample_rate": _int(stream.get("sample_rate")),
        "channels": _int(stream.get("channels")),
        "codec": stream.get("codec_name"),
    }


def _float(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- Audio-Hash
async def audio_hash(path: Path) -> str | None:
    """MD5 ueber den reinen Audiostream, ohne Tags und ohne Cover.

    Das ist der entscheidende Unterschied zu fdupes & Co.: zwei Dateien mit
    identischer Musik, aber unterschiedlichem ID3-Block oder Albumcover, sind
    byteweise verschieden - hier aber identisch.

    -c:a copy bedeutet: kein Dekodieren, kein Neukodieren. Kostet fast nichts.
    """
    code, out, err = await run(
        [
            settings.ffmpeg_bin,
            "-v", "error",
            "-i", str(path),
            "-map", "0:a:0",
            "-c:a", "copy",
            "-f", "md5", "-",
        ],
        timeout=120.0,
    )
    if code != 0:
        log.debug("audio_hash fehlgeschlagen fuer %s: %s", path, err[:200])
        return None
    text = out.decode("utf-8", "replace").strip()
    return text.split("=", 1)[1] if "=" in text else None


# ------------------------------------------------------------ Chromaprint
async def fingerprint(path: Path, length: int = 120) -> tuple[float, bytes] | None:
    """Roh-Fingerprint (Liste von 32-Bit-Subfingerprints) als Bytes.

    -raw statt der komprimierten Base64-Form, weil wir lokal vergleichen und
    dafuer die Bitmuster brauchen - nicht die AcoustID-Serverform.
    """
    code, out, err = await run(
        [
            settings.fpcalc_bin,
            "-raw",
            "-json",
            "-length", str(length),
            str(path),
        ],
        timeout=90.0,
    )
    if code != 0:
        log.debug("fpcalc fehlgeschlagen fuer %s: %s", path, err[:200])
        return None
    try:
        data = json.loads(out or b"{}")
    except json.JSONDecodeError:
        return None
    values = data.get("fingerprint")
    if not isinstance(values, list) or not values:
        return None
    packed = struct.pack(f"<{len(values)}I", *[v & 0xFFFFFFFF for v in values])
    return float(data.get("duration") or 0.0), packed


def unpack_fingerprint(blob: bytes) -> list[int]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}I", blob[: count * 4]))


# ------------------------------------------- Hinweis-Ton fuer den Proxy
_notice_path: Path | None = None
_notice_lock = asyncio.Lock()


async def notice_clip() -> Path | None:
    """Kurzer Hinweiston, der ausgeliefert wird, waehrend ein Titel laedt.

    Bewusst ein echter, dekodierbarer MP3-Stream: ein HTTP-Fehler an dieser
    Stelle laesst Subsonic-Clients die Warteschlange abbrechen, ein gueltiger
    Stream nicht.
    """
    global _notice_path
    if _notice_path and _notice_path.exists():
        return _notice_path

    async with _notice_lock:
        if _notice_path and _notice_path.exists():
            return _notice_path
        target = settings.cache_dir / "notice.mp3"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > 0:
            _notice_path = target
            return target

        # Kurzer Piep alle 2 s ueber 8 s - eindeutig als Systemton erkennbar.
        expr = "0.18*sin(2*PI*880*t)*lt(mod(t\\,2)\\,0.12)"
        code, _, err = await run(
            [
                settings.ffmpeg_bin,
                "-v", "error", "-y",
                "-f", "lavfi",
                "-i", f"aevalsrc={expr}:d=8:s=44100",
                "-c:a", "libmp3lame", "-b:a", "96k", "-ac", "1",
                str(target),
            ],
            timeout=30.0,
        )
        if code != 0 or not target.exists():
            log.warning("Hinweiston konnte nicht erzeugt werden: %s", err[:200])
            # Notfall: reine Stille, damit der Proxy trotzdem etwas Gueltiges hat.
            code, _, err = await run(
                [
                    settings.ffmpeg_bin,
                    "-v", "error", "-y",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "8", "-c:a", "libmp3lame", "-b:a", "64k",
                    str(target),
                ],
                timeout=30.0,
            )
            if code != 0:
                return None
        _notice_path = target
        return target


async def available() -> dict[str, bool]:
    """Fuer die Diagnose-Seite."""
    out: dict[str, bool] = {}
    for name, binary in (
        ("ffmpeg", settings.ffmpeg_bin),
        ("ffprobe", settings.ffprobe_bin),
        ("fpcalc", settings.fpcalc_bin),
    ):
        try:
            code, _, _ = await run([binary, "-version"], timeout=10.0)
            out[name] = code == 0
        except ToolError:
            out[name] = False
    return out
