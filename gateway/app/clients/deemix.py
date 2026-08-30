"""Deemix-Client mit Transport-Erkennung.

Ehrliche Vorbemerkung: "die Deemix-API" gibt es nicht. Original-deemix,
deemix-gui und die Forks (u.a. ghcr.io/bambanah/deemix) sprechen
unterschiedliche Dialekte - mal REST, mal socket.io, mal beides. Ein fest
verdrahteter Endpunkt waere die fragilste Stelle im ganzen System.

Deshalb: eine Kandidatenliste, die beim ersten erfolgreichen Aufruf ermittelt
und in der setting-Tabelle festgehalten wird. Das Ergebnis ist im Dashboard
unter "Diagnose" sichtbar und dort auch manuell ueberschreibbar.

Faellt alles aus, ist das kein stiller Fehler: der Job schlaegt mit klarer
Meldung fehl und der Downloader faellt auf die Ordner-Ueberwachung zurueck
(Dateien, die auf anderem Weg im Staging landen, werden trotzdem importiert).
"""
from __future__ import annotations

import json
from typing import Any

from ..db import db
from ..logging_conf import get_logger
from . import http

log = get_logger("deemix")

SETTING_KEY = "deemix.transport"


class DeemixUnavailable(RuntimeError):
    pass


# (Methode, Pfad, Art der Parameteruebergabe)
_ADD_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("POST", "/api/addToQueue", "json"),
    ("POST", "/api/addToQueue", "query"),
    ("GET", "/api/addToQueue", "query"),
    ("POST", "/api/queue/add", "json"),
    ("POST", "/api/download", "json"),
    ("GET", "/addToQueue", "query"),
)

_INFO_CANDIDATES: tuple[str, ...] = (
    "/api/getQueue",
    "/api/queue",
    "/api/getSettings",
    "/api/settings",
    "/api/loginArl",
    "/",
)


def _payload(url: str, bitrate: str) -> dict[str, Any]:
    # Verschiedene Forks erwarten verschiedene Schluessel - wir liefern alle,
    # unbekannte werden serverseitig ignoriert.
    return {"url": url, "urls": [url], "bitrate": str(bitrate), "quality": str(bitrate)}


async def _try_add(method: str, path: str, style: str, url: str, bitrate: str) -> tuple[bool, str]:
    body = _payload(url, bitrate)
    try:
        client = http.deemix()
        if style == "json":
            resp = await client.request(method, path, json=body)
        else:
            resp = await client.request(
                method, path, params={"url": url, "bitrate": str(bitrate)}
            )
    except Exception as exc:
        return False, f"{method} {path}: {exc}"

    if resp.status_code in (200, 201, 202, 204):
        text = (resp.text or "")[:200]
        if "error" in text.lower() and "false" not in text.lower():
            return False, f"{method} {path}: HTTP {resp.status_code} -> {text}"
        return True, f"{method} {path}: HTTP {resp.status_code}"
    return False, f"{method} {path}: HTTP {resp.status_code}"


async def add_to_queue(url: str, bitrate: str) -> str:
    """Stellt einen Deezer-Link in die Deemix-Warteschlange.

    Rueckgabe: Beschreibung des genutzten Transports (fuer das Job-Detail).
    """
    stored = await db.get_setting(SETTING_KEY)
    order: list[tuple[str, str, str]] = list(_ADD_CANDIDATES)
    if stored:
        try:
            preferred = tuple(json.loads(stored))
            if preferred in order:
                order.remove(preferred)  # type: ignore[arg-type]
            order.insert(0, preferred)  # type: ignore[arg-type]
        except Exception:
            pass

    errors: list[str] = []
    for candidate in order:
        ok, detail = await _try_add(*candidate, url=url, bitrate=bitrate)
        if ok:
            await db.set_setting(SETTING_KEY, json.dumps(list(candidate)))
            log.info("Deemix-Transport: %s", detail)
            return detail
        errors.append(detail)

    raise DeemixUnavailable(
        "Kein funktionierender Deemix-Endpunkt gefunden. Versuche:\n  " + "\n  ".join(errors)
    )


async def probe() -> dict[str, Any]:
    """Fuer die Diagnose-Seite: was antwortet der Deemix-Container ueberhaupt?"""
    results: list[dict[str, Any]] = []
    reachable = False
    for path in _INFO_CANDIDATES:
        try:
            resp = await http.deemix().get(path, timeout=5.0)
            reachable = True
            results.append(
                {
                    "path": path,
                    "status": resp.status_code,
                    "content_type": resp.headers.get("content-type", ""),
                    "preview": (resp.text or "")[:160],
                }
            )
        except Exception as exc:
            results.append({"path": path, "status": None, "error": str(exc)[:160]})
    stored = await db.get_setting(SETTING_KEY)
    return {
        "reachable": reachable,
        "known_transport": json.loads(stored) if stored else None,
        "endpoints": results,
    }


async def set_transport(method: str, path: str, style: str) -> None:
    await db.set_setting(SETTING_KEY, json.dumps([method, path, style]))


async def healthy() -> bool:
    for path in ("/api/getQueue", "/api/settings", "/"):
        try:
            resp = await http.deemix().get(path, timeout=4.0)
            if resp.status_code < 500:
                return True
        except Exception:
            continue
    return False
