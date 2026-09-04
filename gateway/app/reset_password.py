"""Passwort zuruecksetzen, wenn niemand mehr hineinkommt.

    docker exec music-gateway-api python -m app.reset_password
    docker exec music-gateway-api python -m app.reset_password mitbewohner

Ohne Argument wird der erste Administrator zurueckgesetzt. Das neue Passwort
steht danach auf der Ausgabe und im Containerlog.

Der Weg ueber das Dashboard (Konto -> Neues Passwort erzeugen) setzt eine
gueltige Sitzung voraus. Genau die fehlt, wenn man ausgesperrt ist - deshalb
dieser zweite Weg, der nur Zugriff auf den Container braucht.
"""
from __future__ import annotations

import asyncio
import sys

from .config import ensure_dirs, settings
from .db import configure, db
from .logging_conf import setup_logging
from .security import reset_password


async def main(username: str | None) -> int:
    setup_logging()
    ensure_dirs()
    configure(settings.db_path)
    await db.connect()
    try:
        if username:
            row = await db.fetch_one(
                "SELECT id, username FROM app_user WHERE username = ?", (username,)
            )
            if not row:
                vorhanden = await db.fetch_all("SELECT username FROM app_user ORDER BY username")
                print(f"Kein Benutzer '{username}'.", file=sys.stderr)
                print("Vorhanden: " + ", ".join(r["username"] for r in vorhanden),
                      file=sys.stderr)
                return 1
        else:
            row = await db.fetch_one(
                "SELECT id, username FROM app_user WHERE role = 'admin' ORDER BY id LIMIT 1"
            )
            if not row:
                print("Es gibt keinen Administrator in der Datenbank.", file=sys.stderr)
                return 1

        password = await reset_password(
            int(row["id"]), row["username"], "Passwort ueber die Kommandozeile zurueckgesetzt"
        )
        print()
        print(f"  Benutzer:  {row['username']}")
        print(f"  Passwort:  {password}")
        print()
        print("  Alle offenen Sitzungen wurden beendet.")
        print()
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None)))
