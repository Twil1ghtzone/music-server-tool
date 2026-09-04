"""Login, Sitzung und Zwei-Faktor fuer das Dashboard."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import security
from ..db import db
from ..events import emit
from ..logging_conf import get_logger

log = get_logger("api.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    totp: str | None = Field(default=None, max_length=10)


class PasswordBody(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=10, max_length=256)


class TotpBody(BaseModel):
    code: str = Field(min_length=6, max_length=10)


@router.post("/login")
async def login(body: LoginBody, request: Request) -> JSONResponse:
    ip = security.client_ip(request)
    throttled, delay = await security.is_throttled(ip, body.username)
    if throttled:
        await emit(
            f"Login gedrosselt fuer {body.username} von {ip}", category="auth", level="warn"
        )
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Zu viele Fehlversuche. Bitte {delay} Sekunden warten.",
        )

    user = await db.fetch_one(
        "SELECT * FROM app_user WHERE username = ?", (body.username,)
    )

    # Konstante Antwortzeit: kein Rueckschluss darauf, ob es den Nutzer gibt.
    valid = bool(user) and security.verify_password(user["password_hash"], body.password)
    if not valid:
        await asyncio.sleep(0.35)
        await security.record_attempt(ip, body.username, False)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Benutzername oder Passwort falsch")

    if user["totp_enabled"]:
        if not body.totp or not security.verify_totp(user["totp_secret"] or "", body.totp):
            await security.record_attempt(ip, body.username, False)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Code der Authenticator-App falsch")

    if security.needs_rehash(user["password_hash"]):
        await db.execute(
            "UPDATE app_user SET password_hash = ? WHERE id = ?",
            (security.hash_password(body.password), user["id"]),
        )

    token, csrf = await security.create_session(int(user["id"]), request)
    await security.record_attempt(ip, body.username, True)
    await db.execute(
        "UPDATE app_user SET last_login_at = datetime('now') WHERE id = ?", (user["id"],)
    )
    await emit(f"Anmeldung: {body.username} von {ip}", category="auth")

    response = JSONResponse(
        {
            "username": user["username"],
            "role": user["role"],
            "totp_enabled": bool(user["totp_enabled"]),
            "csrf": csrf,
        }
    )
    security.set_auth_cookies(response, token, csrf)
    return response


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    token = request.cookies.get(security.SESSION_COOKIE)
    if token:
        await security.destroy_session(token)
    response = JSONResponse({"ok": True})
    security.clear_auth_cookies(response)
    return response


@router.get("/me")
async def me(request: Request) -> dict:
    user = await security.load_session_user(request.cookies.get(security.SESSION_COOKIE))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Nicht angemeldet")
    return {
        "username": user["username"],
        "role": user["role"],
        "totp_enabled": bool(user["totp_enabled"]),
        "csrf": request.cookies.get(security.CSRF_COOKIE),
    }


@router.post("/password")
async def change_password(
    body: PasswordBody, request: Request, user: dict = Depends(security.guarded)
) -> dict:
    row = await db.fetch_one("SELECT * FROM app_user WHERE id = ?", (user["id"],))
    if not row or not security.verify_password(row["password_hash"], body.current):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Aktuelles Passwort ist falsch")

    await db.execute(
        "UPDATE app_user SET password_hash = ? WHERE id = ?",
        (security.hash_password(body.new), user["id"]),
    )
    # Alle anderen Sitzungen beenden - ein Passwortwechsel soll wirken.
    current = request.cookies.get(security.SESSION_COOKIE) or ""
    await db.execute(
        "DELETE FROM app_session WHERE user_id = ? AND id != ?",
        (user["id"], security._digest(current)),
    )
    await emit("Passwort geaendert", category="auth", level="warn")
    return {"ok": True}


class ResetBody(BaseModel):
    totp: str | None = Field(default=None, max_length=10)


@router.post("/password/reset")
async def reset_own_password(
    body: ResetBody, user: dict = Depends(security.guarded)
) -> dict:
    """Neues Passwort erzeugen lassen, ohne das alte zu kennen.

    Gedacht fuer den Fall, dass das erzeugte Startpasswort verloren ging, die
    Sitzung im Browser aber noch steht.

    Abwaegung, offen benannt: wer eine gueltige Sitzung hat, kann damit das
    Passwort uebernehmen - beim regulaeren Wechsel ist das alte Passwort noetig,
    hier nicht. Ist Zwei-Faktor eingeschaltet, wird deshalb ein Code verlangt.
    Wer ganz ausgesperrt ist, nutzt "python -m app.reset_password" im Container.
    """
    row = await db.fetch_one("SELECT * FROM app_user WHERE id = ?", (user["id"],))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Benutzer nicht gefunden")

    if row["totp_enabled"]:
        if not body.totp or not security.verify_totp(row["totp_secret"] or "", body.totp):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Zwei-Faktor ist aktiv - bitte den Code aus der Authenticator-App eingeben.",
            )

    password = await security.reset_password(
        int(user["id"]), row["username"], "Passwort ueber das Dashboard zurueckgesetzt"
    )
    await emit(
        f"Passwort zurueckgesetzt: {row['username']} (alle Sitzungen beendet)",
        category="auth", level="warn",
    )
    # Auch im Rumpf, damit man es nicht im Log suchen muss - danach steht in
    # der Datenbank nur noch der Hash.
    return {"password": password, "username": row["username"]}


@router.post("/totp/setup")
async def totp_setup(user: dict = Depends(security.guarded)) -> dict:
    secret = security.new_totp_secret()
    await db.execute(
        "UPDATE app_user SET totp_secret = ?, totp_enabled = 0 WHERE id = ?",
        (secret, user["id"]),
    )
    return {"secret": secret, "uri": security.totp_uri(secret, user["username"])}


@router.post("/totp/enable")
async def totp_enable(body: TotpBody, user: dict = Depends(security.guarded)) -> dict:
    row = await db.fetch_one("SELECT totp_secret FROM app_user WHERE id = ?", (user["id"],))
    if not row or not row["totp_secret"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Zuerst /totp/setup aufrufen")
    if not security.verify_totp(row["totp_secret"], body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Code stimmt nicht")
    await db.execute("UPDATE app_user SET totp_enabled = 1 WHERE id = ?", (user["id"],))
    await emit("Zwei-Faktor aktiviert", category="auth")
    return {"ok": True}


@router.post("/totp/disable")
async def totp_disable(body: TotpBody, user: dict = Depends(security.guarded)) -> dict:
    row = await db.fetch_one("SELECT totp_secret FROM app_user WHERE id = ?", (user["id"],))
    if not row or not security.verify_totp(row["totp_secret"] or "", body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Code stimmt nicht")
    await db.execute(
        "UPDATE app_user SET totp_enabled = 0, totp_secret = NULL WHERE id = ?", (user["id"],)
    )
    await emit("Zwei-Faktor deaktiviert", category="auth", level="warn")
    return {"ok": True}
