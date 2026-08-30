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

import asyncio
import json
from typing import Any

from ..db import db
from ..errors import PermanentError
from ..logging_conf import get_logger
from . import http

log = get_logger("deemix")

SETTING_KEY = "deemix.transport"


class DeemixUnavailable(RuntimeError):
    """Kein Endpunkt hat die Anfrage angenommen."""


class DeemixRejected(PermanentError):
    """Deemix hat die Anfrage bewusst abgelehnt.

    Typisch NotLoggedIn (ARL fehlt oder ist abgelaufen) oder CantStream (der
    Titel ist mit diesem Konto oder in dieser Region nicht abrufbar). Beides
    aendert sich nicht durch Wiederholen - dafuer muss ein Mensch ran.
    """


# Was Deemix in errid meldet, in Klartext samt Handlungsanweisung.
_ERROR_HINTS = {
    "NotLoggedIn": "Deemix ist nicht bei Deezer angemeldet. Der ARL fehlt oder "
                   "ist abgelaufen - in der Deemix-Oberflaeche neu setzen.",
    "CantStream": "Deezer gibt diesen Titel fuer das hinterlegte Konto nicht her "
                  "(Region oder Abo-Stufe).",
    "wrongURL": "Deemix kann mit dieser URL nichts anfangen.",
    "notLoggedIn": "Deemix ist nicht bei Deezer angemeldet (ARL pruefen).",
}


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
    # Der Referenz-Fork liest req.body.url (String) und req.body.bitrate.
    # Die uebrigen Schluessel schaden nicht und decken abweichende Forks ab.
    body: dict[str, Any] = {"url": url, "urls": [url]}
    try:
        body["bitrate"] = int(bitrate)
    except (TypeError, ValueError):
        body["bitrate"] = str(bitrate)
    body["quality"] = body["bitrate"]
    return body


async def _try_add(method: str, path: str, style: str, url: str, bitrate: str) -> tuple[bool, str]:
    """Genau ein Kandidat. Rueckgabe: (angenommen, Beschreibung).

    Wichtig und lange falsch gemacht: Deemix antwortet auch im Fehlerfall mit
    HTTP 200 und teilt das Ergebnis im Rumpf mit - {"result": false, "errid":
    "NotLoggedIn"}. Wer nur den Statuscode prueft, haelt eine Ablehnung fuer
    einen Erfolg und wartet danach zehn Minuten auf eine Datei, die nie kommt.
    """
    body = _payload(url, bitrate)
    try:
        client = http.deemix()
        if style == "json":
            resp = await client.request(method, path, json=body)
        else:
            resp = await client.request(
                method, path, params={"url": url, "bitrate": str(body["bitrate"])}
            )
    except Exception as exc:
        return False, f"{method} {path}: {exc}"

    if resp.status_code not in (200, 201, 202, 204):
        return False, f"{method} {path}: HTTP {resp.status_code}"

    try:
        data = resp.json()
    except ValueError:
        # Kein JSON: unter diesem Pfad liegt die Weboberflaeche, nicht die API.
        return False, f"{method} {path}: Antwort ist kein JSON"

    if isinstance(data, dict) and data.get("result") is False:
        errid = str(data.get("errid") or "unbekannt")
        hint = _ERROR_HINTS.get(errid, "")
        raise DeemixRejected(f"Deemix lehnt ab ({errid}). {hint}".strip())

    return True, f"{method} {path}: angenommen"


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
        # DeemixRejected fliegt bewusst durch: der Endpunkt stimmt, Deemix
        # will nur nicht. Weitere Pfade zu probieren waere sinnlos und wuerde
        # die eigentliche Ursache hinter einer Sammelmeldung verstecken.
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
    """Fuer die Diagnose-Seite: was antwortet der Deemix-Container ueberhaupt?

    Alle Kandidaten parallel. Sequenziell waere jede nicht erreichbare Adresse
    ein voller Verbindungs-Timeout - bei sechs Kandidaten wartet der Nutzer
    dann eine halbe Minute auf eine Seite, die nur Status anzeigt.
    """

    async def one(path: str) -> dict[str, Any]:
        try:
            resp = await http.deemix().get(path, timeout=3.0)
            return {
                "path": path,
                "status": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "preview": (resp.text or "")[:160],
            }
        except Exception as exc:
            return {"path": path, "status": None, "error": str(exc)[:160]}

    results = list(await asyncio.gather(*(one(path) for path in _INFO_CANDIDATES)))
    reachable = any(item.get("status") is not None for item in results)
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
