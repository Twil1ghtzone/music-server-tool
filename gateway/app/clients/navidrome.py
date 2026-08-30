"""Navidrome-Client (Subsonic-API).

Nutzt den Admin-Account ausschliesslich fuer Betriebsaufgaben: Scan ausloesen,
Scan-Status abfragen und neu importierte Dateien auf ihre echte Navidrome-ID
aufloesen. Der Client-Verkehr laeuft nicht hierueber, sondern durch den Proxy.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

import httpx

from ..config import settings
from ..logging_conf import get_logger
from . import http

log = get_logger("navidrome")

API_VERSION = "1.16.1"
CLIENT_NAME = "music-gateway"


class NavidromeError(RuntimeError):
    pass


def auth_params(user: str | None = None, password: str | None = None) -> dict[str, str]:
    """Subsonic-Token-Auth: t = md5(passwort + salt)."""
    user = user or settings.navidrome_user
    password = password if password is not None else settings.navidrome_password
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    return {
        "u": user,
        "t": token,
        "s": salt,
        "v": API_VERSION,
        "c": CLIENT_NAME,
        "f": "json",
    }


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    body = payload.get("subsonic-response") or {}
    if body.get("status") == "failed":
        err = body.get("error") or {}
        raise NavidromeError(f"{err.get('code')}: {err.get('message')}")
    return body


# ------------------------------------------------- Geliehene Zugangsdaten
# Damit der Stack ohne eine einzige Pflichtangabe startet, kann der Gateway
# ohne eigenes Navidrome-Passwort auskommen: sobald ein Client sich erfolgreich
# durch den Proxy anmeldet, merkt er sich dessen Subsonic-Token und nutzt es
# fuer seine eigenen Aufrufe (Titel nach dem Import auf die Navidrome-ID
# aufloesen, Scan anstossen).
#
# Warum das geht: Subsonic-Token sind t = md5(passwort + salt) mit frei
# gewaehltem Salt und ohne Ablauf oder Einmalgebrauch. Dasselbe Tripel ist
# beliebig oft wiederverwendbar.
#
# Der Preis, ehrlich benannt: das Tripel liegt in der setting-Tabelle und ist
# fuer API-Zugriffe so maechtig wie das Passwort selbst. Es ist derselbe Wert,
# den der Client ohnehin bei jeder Anfrage ueber die Leitung schickt, und die
# Datenbank liegt neben navidrome.db. Wer das nicht will, setzt
# NAVIDROME_PASSWORD - dann wird nichts geliehen und nichts gespeichert.
#
# Einschraenkung: startScan verlangt einen Admin. Ist der angemeldete Client
# kein Admin, schlaegt nur der Scan-Anstoss fehl - ND_MONITORCHANGES faengt
# das auf, und die Titelsuche zum Aufloesen funktioniert fuer jeden Benutzer.
_BORROW_KEY = "navidrome.borrowed_auth"
_borrowed: dict[str, str] | None = None


async def remember_credentials(params: dict[str, str]) -> None:
    """Nach erfolgreicher Client-Anmeldung aufrufen."""
    global _borrowed
    if settings.navidrome_password:
        return
    keep = {k: v for k, v in params.items() if k in ("u", "t", "s", "p") and v}
    if not keep.get("u") or _borrowed == keep:
        return
    _borrowed = keep
    try:
        from ..db import db

        await db.set_setting(_BORROW_KEY, json.dumps(keep))
        log.info("Zugangsdaten von Benutzer '%s' uebernommen", keep["u"])
    except Exception as exc:  # pragma: no cover
        log.debug("Zugangsdaten nicht gespeichert: %s", exc)


async def _credentials() -> dict[str, str]:
    global _borrowed
    if settings.navidrome_password:
        return auth_params()

    if _borrowed is None:
        try:
            from ..db import db

            stored = await db.get_setting(_BORROW_KEY)
            _borrowed = json.loads(stored) if stored else {}
        except Exception:
            _borrowed = {}

    if _borrowed:
        return {**_borrowed, "v": API_VERSION, "c": CLIENT_NAME, "f": "json"}
    return auth_params()


def has_credentials() -> bool:
    return bool(settings.navidrome_password or _borrowed)


async def call(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = await _credentials()
    if params:
        query.update({k: v for k, v in params.items() if v is not None})
    resp = await http.navidrome().get(f"/rest/{endpoint}", params=query)
    resp.raise_for_status()
    return _unwrap(resp.json())


# ---------------------------------------------------------------- Betrieb
async def ping() -> bool:
    try:
        await call("ping")
        return True
    except Exception as exc:
        log.debug("Navidrome ping fehlgeschlagen: %s", exc)
        return False


async def start_scan(full: bool = False) -> dict[str, Any]:
    return (await call("startScan", {"fullScan": "true" if full else "false"})).get("scanStatus", {})


async def scan_status() -> dict[str, Any]:
    try:
        return (await call("getScanStatus")).get("scanStatus", {})
    except Exception:
        return {}


async def server_info() -> dict[str, Any]:
    try:
        body = await call("ping")
        return {
            "online": True,
            "version": body.get("version"),
            "type": body.get("type"),
            "serverVersion": body.get("serverVersion"),
            "openSubsonic": body.get("openSubsonic", False),
        }
    except Exception as exc:
        return {"online": False, "error": str(exc)}


# ------------------------------------------------------------ Bibliothek
async def search_songs(query: str, count: int = 20, offset: int = 0) -> list[dict]:
    body = await call(
        "search3",
        {
            "query": query,
            "songCount": count,
            "songOffset": offset,
            "albumCount": 0,
            "artistCount": 0,
        },
    )
    return (body.get("searchResult3") or {}).get("song") or []


async def get_song(song_id: str) -> dict | None:
    try:
        return (await call("getSong", {"id": song_id})).get("song")
    except NavidromeError:
        return None


async def album_list(kind: str = "newest", size: int = 12) -> list[dict]:
    body = await call("getAlbumList2", {"type": kind, "size": size})
    return (body.get("albumList2") or {}).get("album") or []


async def library_stats() -> dict[str, Any]:
    """Navidrome hat keinen dedizierten Stats-Endpunkt; getArtists liefert
    aber billig die Kuenstlerzahl, und getAlbumList2 die Albenzahl."""
    out: dict[str, Any] = {"artists": None, "albums": None}
    try:
        body = await call("getArtists")
        indexes = (body.get("artists") or {}).get("index") or []
        out["artists"] = sum(len(i.get("artist") or []) for i in indexes)
    except Exception:
        pass
    return out


# ------------------------------------------------------- Credential-Check
async def verify_client_credentials(params: dict[str, str]) -> bool:
    """Prueft die vom Client mitgeschickten Subsonic-Zugangsdaten, indem sie
    unveraendert an Navidrome weitergereicht werden. Der Gateway speichert
    dadurch selbst kein Subsonic-Passwort."""
    forward = {k: v for k, v in params.items() if k in ("u", "t", "s", "p", "c", "v")}
    forward.setdefault("v", API_VERSION)
    forward.setdefault("c", CLIENT_NAME)
    forward["f"] = "json"
    try:
        resp = await http.navidrome().get("/rest/ping.view", params=forward, timeout=10.0)
        if resp.status_code != 200:
            return False
        body = resp.json().get("subsonic-response") or {}
        return body.get("status") == "ok"
    except httpx.HTTPError as exc:
        log.warning("Credential-Check gegen Navidrome fehlgeschlagen: %s", exc)
        return False
