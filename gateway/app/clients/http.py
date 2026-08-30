"""Gemeinsame httpx-Clients.

Ein Client pro Ziel, prozessweit wiederverwendet. Das ist der wichtigste
Performance-Hebel im Proxy: keine TLS-/TCP-Handshakes pro Request, dafuer
Keep-Alive-Pools mit passender Groesse.
"""
from __future__ import annotations

import httpx

from ..config import settings

_clients: dict[str, httpx.AsyncClient] = {}


def _make(base_url: str, *, timeout: httpx.Timeout, max_conn: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        follow_redirects=False,
        limits=httpx.Limits(
            max_connections=max_conn,
            max_keepalive_connections=max_conn,
            keepalive_expiry=60.0,
        ),
        headers={"User-Agent": "music-server-tool/1.0"},
    )


def navidrome() -> httpx.AsyncClient:
    """Hoher Pool: hier laeuft der gesamte Subsonic-Verkehr durch.
    read=None, weil Streams beliebig lange laufen duerfen."""
    if "navidrome" not in _clients:
        _clients["navidrome"] = _make(
            settings.navidrome_url,
            timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=10.0),
            max_conn=64,
        )
    return _clients["navidrome"]


def deemix() -> httpx.AsyncClient:
    if "deemix" not in _clients:
        _clients["deemix"] = _make(
            settings.deemix_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=10.0),
            max_conn=8,
        )
    return _clients["deemix"]


def deezer() -> httpx.AsyncClient:
    if "deezer" not in _clients:
        _clients["deezer"] = _make(
            settings.deezer_api_url,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
            max_conn=8,
        )
    return _clients["deezer"]


def plain() -> httpx.AsyncClient:
    """Fuer beliebige URLs (Cover-Downloads)."""
    if "plain" not in _clients:
        _clients["plain"] = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=20.0, write=20.0, pool=5.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={"User-Agent": "music-server-tool/1.0"},
        )
    return _clients["plain"]


async def close_all() -> None:
    for client in _clients.values():
        try:
            await client.aclose()
        except Exception:  # pragma: no cover
            pass
    _clients.clear()
