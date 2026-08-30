"""Subsonic-Authentifizierung: Pass-Through statt eigener Nutzerverwaltung.

Das Subsonic-Protokoll schreibt t = md5(passwort + salt) vor. Ein Server, der
das pruefen will, muss das Passwort im Klartext (oder reversibel) vorhalten -
Argon2, JWT oder 2FA sind hier prinzipiell nicht anwendbar.

Konsequenz fuer die Sicherheit: der Gateway prueft gar nicht selbst. Er reicht
die Zugangsdaten unveraendert an Navidrome weiter und uebernimmt dessen Urteil.
Damit gibt es genau eine Passwortquelle im System und der Gateway speichert
kein einziges Subsonic-Geheimnis.

Gecacht wird nur das Ergebnis, kurz (Default 60 s), damit ein Client nicht bei
jedem Cover-Thumbnail einen zusaetzlichen Roundtrip ausloest.
"""
from __future__ import annotations

import hashlib
import time

from ..config import settings
from ..logging_conf import get_logger
from ..clients import navidrome

log = get_logger("subsonic.auth")

_cache: dict[str, tuple[float, bool]] = {}
_CACHE_MAX = 2048

# Einfache Bremse gegen Passwort-Raten ueber den Proxy: der Gateway darf kein
# billigeres Brute-Force-Orakel sein als Navidrome selbst.
_failures: dict[str, list[float]] = {}
FAIL_WINDOW = 300.0
FAIL_LIMIT = 20


def _key(params) -> str:
    raw = "|".join(
        str(params.get(k) or "") for k in ("u", "t", "s", "p", "apiKey")
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _throttled(ip: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _failures.get(ip, []) if now - t < FAIL_WINDOW]
    _failures[ip] = hits
    return len(hits) >= FAIL_LIMIT


def _note_failure(ip: str) -> None:
    _failures.setdefault(ip, []).append(time.monotonic())
    if len(_failures) > 4096:
        _failures.clear()


async def verify(params, ip: str = "unknown") -> bool:
    """True, wenn Navidrome die Zugangsdaten akzeptiert."""
    if not params.get("u"):
        return False
    if _throttled(ip):
        log.warning("Auth-Versuche von %s gedrosselt", ip)
        return False

    key = _key(params)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        if not hit[1]:
            _note_failure(ip)
        return hit[1]

    valid = await navidrome.verify_client_credentials(params)

    if len(_cache) > _CACHE_MAX:
        _cache.clear()
    # Fehlschlaege nur kurz cachen, damit eine Passwortaenderung schnell greift.
    ttl = settings.auth_cache_ttl if valid else 5
    _cache[key] = (now + ttl, valid)

    if valid:
        # Der Gateway braucht selbst Zugriff auf die Navidrome-API, um
        # importierte Titel auf ihre ID aufzuloesen. Statt dafuer ein zweites
        # Passwort in der Konfiguration zu verlangen, leiht er sich das Token
        # des ersten Clients, der sich erfolgreich anmeldet.
        await navidrome.remember_credentials(dict(params))
    else:
        _note_failure(ip)
    return valid


def invalidate() -> None:
    _cache.clear()
