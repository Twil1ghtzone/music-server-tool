"""Downloads und Warteschlange: anfordern, ansehen, vergessen."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .. import security
from ..logging_conf import get_logger
from ..services import downloader

log = get_logger("api.downloads")
router = APIRouter(prefix="/api", tags=["downloads"])


class DownloadBody(BaseModel):
    provider_id: str = Field(min_length=1, max_length=64)


class ReleaseBody(BaseModel):
    kind: str = Field(pattern="^(album|playlist|artist)$")
    provider_id: str = Field(min_length=1, max_length=64, pattern=r"^[0-9]+$")


@router.get("/queue")
async def queue(user: dict = Depends(security.current_user), limit: int = 100) -> dict:
    return {"items": await downloader.queue_overview(limit)}


@router.delete("/queue/{virtual_id}")
async def forget_queue_entry(
    virtual_id: str, user: dict = Depends(security.guarded_admin)
) -> dict:
    try:
        await downloader.forget_track(virtual_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"ok": True}


@router.post("/queue/clear-failed")
async def clear_failed_queue(user: dict = Depends(security.guarded_admin)) -> dict:
    return {"removed": await downloader.forget_failed()}


@router.post("/download")
async def request_download(
    body: DownloadBody, user: dict = Depends(security.guarded)
) -> dict:
    try:
        return await downloader.request_track(body.provider_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/download/release")
async def request_release(body: ReleaseBody, user: dict = Depends(security.guarded)) -> dict:
    """Album, Playlist oder Interpret am Stueck herunterladen."""
    try:
        return await downloader.request_release(body.kind, body.provider_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
