"""Navidrome-Client (Subsonic-API).

Nutzt den Admin-Account ausschliesslich fuer Betriebsaufgaben: Scan ausloesen,
Scan-Status abfragen und neu importierte Dateien auf ihre echte Navidrome-ID
aufloesen. Der Client-Verkehr laeuft nicht hierueber, sondern durch den Proxy.
"""
from __future__ import annotations

import hashlib
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


async def call(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = auth_params()
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
