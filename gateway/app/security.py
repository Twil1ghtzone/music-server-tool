"""Authentifizierung fuer das Dashboard.

Wichtig zur Abgrenzung: das hier gilt AUSSCHLIESSLICH fuer /api/* (Web-UI).
Der Subsonic-Pfad /rest/* kann diese Mechanismen prinzipiell nicht nutzen,
weil das Subsonic-Protokoll token+salt ueber MD5 vorschreibt. Dort validiert
der Proxy die Zugangsdaten stattdessen direkt gegen Navidrome und speichert
selbst kein einziges Passwort -> siehe subsonic/auth.py.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, status

from .config import settings
from .db import db
from .logging_conf import get_logger

log = get_logger("security")

SESSION_COOKIE = "mst_session"
CSRF_COOKIE = "mst_csrf"
CSRF_HEADER = "X-CSRF-Token"

# Kosten bewusst moderat: laeuft auf einem NAS, nicht auf einer Loginfarm.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)

# Brute-Force-Bremse
MAX_ATTEMPTS_PER_IP = 10
MAX_ATTEMPTS_PER_USER = 5
ATTEMPT_WINDOW_MINUTES = 15


# --------------------------------------------------------------- Passwoerter
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _hasher.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except Exception:
        return False


# ------------------------------------------------------------------ Bootstrap
async def ensure_admin_user() -> None:
    """Legt beim ersten Start den Admin an. Idempotent."""
    existing = await db.fetch_value("SELECT COUNT(*) FROM app_user", (), 0)
    if existing:
        return
    username = settings.admin_user or "admin"
    password = settings.admin_password
    generated = False
    if not password:
        password = generate_password()
        generated = True
    await db.execute(
        "INSERT INTO app_user(username, password_hash, role) VALUES (?, ?, 'admin')",
        (username, hash_password(password)),
    )

    if not generated:
        log.info("Dashboard-Benutzer '%s' angelegt.", username)
        return

    # Ohne gesetztes Passwort ist das Log der einzige Ort, an dem der Zugang
    # steht - danach liegt in der Datenbank nur noch der Argon2-Hash.
    log_password_banner(username, password, "Dashboard-Zugang wurde angelegt")


# -------------------------------------------------------------- Rate-Limiting
def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def record_attempt(ip: str, username: str | None, success: bool) -> None:
    await db.execute(
        "INSERT INTO login_attempt(ip, username, success) VALUES (?,?,?)",
        (ip, username, 1 if success else 0),
    )


async def is_throttled(ip: str, username: str | None) -> tuple[bool, int]:
    """Gibt (gesperrt, Wartezeit-Sekunden) zurueck.

    Zwei Achsen: pro IP (gegen breites Scannen) und pro Konto (gegen gezieltes
    Raten ueber wechselnde IPs). Backoff waechst exponentiell.
    """
    since = (datetime.now(timezone.utc) - timedelta(minutes=ATTEMPT_WINDOW_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    by_ip = int(
        await db.fetch_value(
            "SELECT COUNT(*) FROM login_attempt WHERE ip = ? AND success = 0 AND ts > ?",
            (ip, since),
            0,
        )
        or 0
    )
    by_user = 0
    if username:
        by_user = int(
            await db.fetch_value(
                "SELECT COUNT(*) FROM login_attempt WHERE username = ? AND success = 0 AND ts > ?",
                (username, since),
                0,
            )
            or 0
        )

    over_ip = max(0, by_ip - MAX_ATTEMPTS_PER_IP)
    over_user = max(0, by_user - MAX_ATTEMPTS_PER_USER)
    over = max(over_ip, over_user)
    if by_ip >= MAX_ATTEMPTS_PER_IP or by_user >= MAX_ATTEMPTS_PER_USER:
        delay = min(300, 5 * (2 ** min(over, 6)))
        return True, delay
    return False, 0


async def prune_attempts() -> None:
    await db.execute("DELETE FROM login_attempt WHERE ts < datetime('now', '-1 day')")


# --------------------------------------------------------------- Sessions
def _digest(token: str) -> str:
    """Nur der Hash landet in der DB - ein DB-Leak ergibt keine gueltige Session."""
    return hashlib.sha256((settings.session_secret + token).encode()).hexdigest()


async def create_session(user_id: int, request: Request) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
    await db.execute(
        "INSERT INTO app_session(id, user_id, expires_at, ip, user_agent) VALUES (?,?,?,?,?)",
        (
            _digest(token),
            user_id,
            expires.strftime("%Y-%m-%d %H:%M:%S"),
            client_ip(request),
            (request.headers.get("user-agent") or "")[:255],
        ),
    )
    return token, csrf


async def destroy_session(token: str) -> None:
    await db.execute("DELETE FROM app_session WHERE id = ?", (_digest(token),))


async def prune_sessions() -> None:
    await db.execute("DELETE FROM app_session WHERE expires_at < datetime('now')")


async def load_session_user(token: str | None) -> dict | None:
    if not token:
        return None
    row = await db.fetch_one(
        "SELECT u.id, u.username, u.role, u.totp_enabled, s.id AS sid "
        "FROM app_session s JOIN app_user u ON u.id = s.user_id "
        "WHERE s.id = ? AND s.expires_at > datetime('now')",
        (_digest(token),),
    )
    if not row:
        return None
    await db.execute("UPDATE app_session SET last_seen = datetime('now') WHERE id = ?", (row["sid"],))
    return row


def set_auth_cookies(response, token: str, csrf: str) -> None:
    common = {
        "httponly": True,
        "secure": settings.secure_cookies,
        "samesite": "lax",
        "path": "/",
        "max_age": settings.session_ttl_hours * 3600,
    }
    response.set_cookie(SESSION_COOKIE, token, **common)
    # CSRF-Cookie ist bewusst lesbar: Double-Submit-Pattern.
    response.set_cookie(CSRF_COOKIE, csrf, **{**common, "httponly": False})


def clear_auth_cookies(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


# ------------------------------------------------------------- Dependencies
async def current_user(request: Request) -> dict:
    user = await load_session_user(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Nicht angemeldet")
    return user


async def require_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    header = request.headers.get(CSRF_HEADER) or ""
    if not cookie or not hmac.compare_digest(cookie, header):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF-Token fehlt oder passt nicht")


async def guarded(request: Request) -> dict:
    """Auth + CSRF in einem Rutsch - der Standard fuer /api/*."""
    user = await current_user(request)
    await require_csrf(request)
    return user


def is_admin(user: dict) -> bool:
    return (user.get("role") or "user") == "admin"


async def admin_only(request: Request) -> dict:
    """Lesend, aber nur fuer Administratoren.

    Was hier haengt, verraet Betriebsinterna oder erlaubt Eingriffe: Pfade,
    Zugangsdaten-Status, Protokolle, Bibliothekswerkzeuge.
    """
    user = await current_user(request)
    if not is_admin(user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Dafuer werden Administratorrechte gebraucht.",
        )
    return user


async def guarded_admin(request: Request) -> dict:
    """Aendernd und nur fuer Administratoren."""
    user = await admin_only(request)
    await require_csrf(request)
    return user


# ------------------------------------------------------------------- 2FA
def generate_password() -> str:
    """Sprechbares, aber nicht ratbares Passwort.

    token_urlsafe liefert Zeichen, die sich schlecht abtippen lassen (- und _
    sehen in Logs nach Trennern aus). Deshalb ein eigener Zeichenvorrat ohne
    verwechselbare Zeichen - kein O/0, kein l/I/1.
    """
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(20))


def log_password_banner(username: str, password: str, grund: str) -> None:
    """Erzeugte Passwoerter gehoeren unuebersehbar ins Log - dort sucht man
    sie, wenn die Oberflaeche gerade nicht erreichbar ist."""
    line = "=" * 68
    log.warning("\n%s", line)
    log.warning("  %s", grund)
    log.warning("     Benutzer:  %s", username)
    log.warning("     Passwort:  %s", password)
    log.warning("  Diese Meldung erscheint nur einmal.")
    log.warning("%s\n", line)


async def reset_password(user_id: int, username: str, grund: str) -> str:
    """Neues Passwort erzeugen, setzen und alle Sitzungen beenden.

    Alle Sitzungen - auch die eigene. Ein Zuruecksetzen, nach dem der alte
    Zugang weiterlaeuft, waere keins.
    """
    password = generate_password()
    await db.execute(
        "UPDATE app_user SET password_hash = ? WHERE id = ?",
        (hash_password(password), user_id),
    )
    await db.execute("DELETE FROM app_session WHERE user_id = ?", (user_id,))
    log_password_banner(username, password, grund)
    return password


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="music-server-tool")


def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)
