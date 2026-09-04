"""Benutzerverwaltung fuer das Dashboard.

Zwei Rollen, bewusst nicht mehr:

  admin  darf alles - Einstellungen, Zugangsdaten, Bibliothekswerkzeuge,
         Duplikate, Protokolle und die Benutzerverwaltung selbst.
  user   darf suchen, Titel anfordern und die Warteschlange sehen. Also
         genau das, wofuer man Mitbewohnern einen Zugang gibt.

Wichtig zur Abgrenzung: das sind Konten fuer das WEB-DASHBOARD. Die
Musik-Clients melden sich weiterhin mit ihren Navidrome-Konten an - der
Gateway reicht diese Anmeldung unveraendert weiter und legt dafuer keine
eigenen Benutzer an.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import security
from ..db import db
from ..events import emit
from ..logging_conf import get_logger

log = get_logger("api.users")
router = APIRouter(prefix="/api/users", tags=["users"])

ROLES = ("admin", "user")


class CreateUserBody(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9._@-]+$")
    # Leer lassen: dann wird eins erzeugt und einmalig zurueckgegeben.
    password: str | None = Field(default=None, min_length=10, max_length=256)
    role: str = Field(default="user", pattern="^(admin|user)$")


class UpdateUserBody(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    password: str | None = Field(default=None, min_length=10, max_length=256)
    # Statt selbst eins auszudenken: erzeugen lassen und einmal anzeigen.
    generate_password: bool = False


async def _admin_count(exclude: int | None = None) -> int:
    if exclude is None:
        return int(await db.fetch_value(
            "SELECT COUNT(*) FROM app_user WHERE role = 'admin'", (), 0) or 0)
    return int(await db.fetch_value(
        "SELECT COUNT(*) FROM app_user WHERE role = 'admin' AND id != ?", (exclude,), 0) or 0)


@router.get("")
async def list_users(user: dict = Depends(security.admin_only)) -> dict:
    rows = await db.fetch_all(
        "SELECT id, username, role, totp_enabled, created_at, last_login_at "
        "FROM app_user ORDER BY role, username"
    )
    for row in rows:
        row["sessions"] = int(await db.fetch_value(
            "SELECT COUNT(*) FROM app_session WHERE user_id = ? "
            "AND expires_at > datetime('now')",
            (row["id"],), 0) or 0)
        row["self"] = row["id"] == user["id"]
    return {"users": rows}


@router.post("")
async def create_user(
    body: CreateUserBody, user: dict = Depends(security.guarded_admin)
) -> dict:
    exists = await db.fetch_one(
        "SELECT 1 FROM app_user WHERE username = ?", (body.username,)
    )
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Dieser Benutzername ist vergeben")

    password = body.password or security.generate_password()
    user_id = await db.execute(
        "INSERT INTO app_user(username, password_hash, role) VALUES (?,?,?)",
        (body.username, security.hash_password(password), body.role),
    )
    if not body.password:
        security.log_password_banner(
            body.username, password, "Neuer Dashboard-Zugang wurde angelegt"
        )
    await emit(
        f"Benutzer angelegt: {body.username} ({body.role})", category="auth", level="warn"
    )
    return {
        "id": user_id,
        "username": body.username,
        "role": body.role,
        # Nur wenn wir es erzeugt haben - ein selbst gesetztes Passwort geben
        # wir nicht zurueck, es ist ohnehin bekannt.
        "password": None if body.password else password,
    }


@router.patch("/{user_id}")
async def update_user(
    user_id: int, body: UpdateUserBody, user: dict = Depends(security.guarded_admin)
) -> dict:
    target = await db.fetch_one("SELECT * FROM app_user WHERE id = ?", (user_id,))
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Benutzer nicht gefunden")

    if body.role and body.role != target["role"]:
        # Der letzte Administrator darf sich nicht selbst entmachten - sonst
        # kommt niemand mehr an die Einstellungen.
        if target["role"] == "admin" and body.role != "admin" and await _admin_count(user_id) == 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Das ist der letzte Administrator. Erst einen zweiten anlegen.",
            )
        await db.execute("UPDATE app_user SET role = ? WHERE id = ?", (body.role, user_id))
        await emit(
            f"Rolle geaendert: {target['username']} -> {body.role}",
            category="auth", level="warn",
        )

    erzeugt: str | None = None
    if body.generate_password:
        erzeugt = await security.reset_password(
            user_id, target["username"],
            f"Passwort fuer '{target['username']}' von einem Administrator zurueckgesetzt",
        )
        await emit(
            f"Passwort zurueckgesetzt: {target['username']}", category="auth", level="warn"
        )
    elif body.password:
        await db.execute(
            "UPDATE app_user SET password_hash = ? WHERE id = ?",
            (security.hash_password(body.password), user_id),
        )
        # Ein zurueckgesetztes Passwort muss alte Sitzungen beenden, sonst
        # bleibt der alte Zugang trotz neuem Passwort bestehen.
        await db.execute("DELETE FROM app_session WHERE user_id = ?", (user_id,))
        await emit(
            f"Passwort zurueckgesetzt: {target['username']}", category="auth", level="warn"
        )

    return {"ok": True, "password": erzeugt}


@router.delete("/{user_id}")
async def delete_user(
    user_id: int, request: Request, user: dict = Depends(security.guarded_admin)
) -> dict:
    if user_id == user["id"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Das eigene Konto laesst sich nicht loeschen - sonst sperrt man sich aus.",
        )
    target = await db.fetch_one("SELECT * FROM app_user WHERE id = ?", (user_id,))
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Benutzer nicht gefunden")
    if target["role"] == "admin" and await _admin_count(user_id) == 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Das ist der letzte Administrator."
        )

    # ON DELETE CASCADE raeumt die Sitzungen mit ab.
    await db.execute("DELETE FROM app_user WHERE id = ?", (user_id,))
    await emit(f"Benutzer geloescht: {target['username']}", category="auth", level="warn")
    return {"ok": True}
