"""Auftraege: Liste, Wiederholung, Abbruch - und die beiden Ausloeser,
die selbst nur Auftraege erzeugen (Scan, Staging-Import)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from .. import security
from ..clients import navidrome
from ..logging_conf import get_logger
from ..services import jobs as jobs_service

log = get_logger("api.jobs")
router = APIRouter(prefix="/api", tags=["jobs"])


class ScanBody(BaseModel):
    full: bool = False


@router.get("/jobs")
async def list_jobs(
    user: dict = Depends(security.current_user),
    state: str = Query("all"),
    limit: int = Query(100, le=500),
) -> dict:
    return {
        "jobs": await jobs_service.listing(state, limit),
        "stats": await jobs_service.stats(),
    }


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: int, user: dict = Depends(security.guarded_admin)) -> dict:
    if not await jobs_service.retry(job_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Job ist nicht wiederholbar")
    return {"ok": True}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, user: dict = Depends(security.guarded_admin)) -> dict:
    if not await jobs_service.cancel(job_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Job laeuft bereits oder ist beendet")
    return {"ok": True}


@router.post("/scan")
async def trigger_scan(body: ScanBody, user: dict = Depends(security.guarded_admin)) -> dict:
    # Ohne Zugangsdaten waere der Job zum Scheitern verurteilt. Lieber hier
    # sagen warum, als drei Fehlermeldungen im Ereignisprotokoll erzeugen.
    if not await navidrome.has_credentials_async():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Navidrome-Scan braucht Zugangsdaten. Trag sie unter Diagnose ein - "
            "oder melde dich einmal mit einem Musik-Client auf Port 8080 an, dann "
            "uebernimmt der Gateway dessen Token. Navidrome erkennt neue Dateien "
            "ohnehin selbst (ND_MONITORCHANGES).",
        )
    job_id = await jobs_service.enqueue(
        jobs_service.NAVIDROME_SCAN,
        {"full": body.full},
        priority=jobs_service.PRIORITY_NORMAL,
        dedupe_key="scan:navidrome",
    )
    return {"job": job_id}


@router.post("/import-staging")
async def import_staging(user: dict = Depends(security.guarded_admin)) -> dict:
    job_id = await jobs_service.enqueue(
        jobs_service.IMPORT_STAGING,
        priority=jobs_service.PRIORITY_NORMAL,
        dedupe_key="import:staging",
    )
    return {"job": job_id}
