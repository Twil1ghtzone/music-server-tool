"""Ereignisprotokoll mit Filter."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .. import events, security

router = APIRouter(prefix="/api", tags=["logs"])


@router.get("/logs")
async def logs(
    user: dict = Depends(security.admin_only),
    level: str = Query("all"),
    category: str = Query("all"),
    q: str = Query("", max_length=200),
    limit: int = Query(300, le=1000),
) -> dict:
    return {
        "entries": await events.search(level, category, q or None, limit),
        "categories": await events.categories(),
    }
