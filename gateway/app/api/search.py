"""Kombinierte Suche: was lokal liegt und was der Katalog zusaetzlich kennt."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from .. import security
from ..clients import deezer, navidrome
from ..db import db
from ..logging_conf import get_logger

log = get_logger("api.search")
router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search")
async def search(
    user: dict = Depends(security.current_user),
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(100, ge=10, le=200,
                       description="Wie viele Treffer als Puffer geholt werden"),
) -> dict:
    # Ein Puffer statt genau einer Bildschirmseite: wer den gesuchten Titel
    # nicht unter den ersten Treffern sieht, blaettert weiter, ohne dass eine
    # neue Anfrage noetig waere.
    local_task = asyncio.create_task(navidrome.search_songs(q, count=limit))
    catalog_task = asyncio.create_task(deezer.search_tracks(q, limit=limit))
    local, catalog = await asyncio.gather(local_task, catalog_task, return_exceptions=True)

    local_rows = local if isinstance(local, list) else []
    catalog_rows = catalog if isinstance(catalog, list) else []

    # Navidromes Suche ist buchstabengetreu: "marc forster" findet lokal
    # nichts, obwohl die Titel da sind. Der Katalog kennt die richtige
    # Schreibweise - damit wird die lokale Suche einmal wiederholt.
    corrected: str | None = None
    if not local_rows and catalog_rows:
        artist = deezer.dominant_artist(catalog_rows)
        if artist and not deezer.looks_like(artist, q):
            try:
                local_rows = await navidrome.search_songs(artist, count=limit)
                if local_rows:
                    corrected = artist
            except Exception as exc:
                log.debug("Korrigierte Suche fehlgeschlagen: %s", exc)

    known = await db.fetch_all(
        "SELECT provider_id, state, navidrome_id, error FROM virtual_track WHERE provider = ?",
        (deezer.PROVIDER,),
    ) if catalog_rows else []
    by_id = {row["provider_id"]: row for row in known}
    for item in catalog_rows:
        item["known"] = by_id.get(item["provider_id"])

    return {"local": local_rows, "catalog": catalog_rows, "corrected": corrected}
