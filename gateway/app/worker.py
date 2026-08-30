"""Worker-Prozess.

Eigener Container, aus demselben Image. Der Grund ist simpel: ein Full-Scan
oder ein 20-minuetiger Fingerprint-Lauf darf niemals im selben Prozess laufen
wie der Subsonic-Proxy. Ein blockierter Event-Loop dort heisst stotternde
Wiedergabe auf jedem Geraet im Haus.
"""
from __future__ import annotations

import asyncio
import signal
import time
from typing import Awaitable, Callable

from .clients import http
from .config import ensure_dirs, settings
from .db import configure, db
from .events import emit, prune as prune_events
from .logging_conf import get_logger, setup_logging
from .security import prune_attempts, prune_sessions
from .services import dedupe, downloader, jobs, scanner

log = get_logger("worker")

Handler = Callable[[dict], Awaitable[str]]

HANDLERS: dict[str, Handler] = {
    jobs.DOWNLOAD_TRACK: downloader.handle_download,
    jobs.IMPORT_STAGING: downloader.handle_import_staging,
    jobs.NAVIDROME_SCAN: downloader.handle_navidrome_scan,
    jobs.LIBRARY_SCAN: scanner.handle_library_scan,
    jobs.HASH_FILES: scanner.handle_hash_files,
    jobs.FINGERPRINT: scanner.handle_fingerprint,
    jobs.FIND_DUPES: dedupe.handle_find_dupes,
    jobs.APPLY_DUPES: dedupe.handle_apply,
}

IDLE_SLEEP = 1.5
MAINTENANCE_INTERVAL = 300.0

_stop = asyncio.Event()


async def run_job(job: dict) -> None:
    handler = HANDLERS.get(job["type"])
    job_id = int(job["id"])
    if handler is None:
        await jobs.fail(job_id, f"Unbekannter Job-Typ: {job['type']}", retry=False)
        return

    started = time.monotonic()
    log.info("Job %s (%s) gestartet", job_id, job["type"])
    try:
        detail = await handler(job)
    except asyncio.CancelledError:
        await jobs.fail(job_id, "Worker wurde beendet")
        raise
    except Exception as exc:
        log.exception("Job %s (%s) fehlgeschlagen", job_id, job["type"])
        await jobs.fail(job_id, f"{type(exc).__name__}: {exc}")
        await emit(
            f"Job fehlgeschlagen: {job['type']} - {exc}",
            category="job",
            level="error",
            data={"job": job_id},
        )
        return

    await jobs.succeed(job_id, detail)
    log.info("Job %s beendet nach %.1fs: %s", job_id, time.monotonic() - started, detail)


async def maintenance() -> None:
    """Haelt Tabellen klein und faengt liegengebliebene Staging-Dateien ein."""
    while not _stop.is_set():
        try:
            await asyncio.wait_for(_stop.wait(), timeout=MAINTENANCE_INTERVAL)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await jobs.prune()
            await prune_events()
            await prune_sessions()
            await prune_attempts()
            staged = downloader.scan_audio(settings.staging_dir)
            if staged:
                await jobs.enqueue(
                    jobs.IMPORT_STAGING,
                    priority=jobs.PRIORITY_BACKGROUND,
                    dedupe_key="import:staging",
                )
        except Exception as exc:  # pragma: no cover
            log.warning("Wartungslauf fehlgeschlagen: %s", exc)


async def main() -> None:
    setup_logging()
    ensure_dirs()
    configure(settings.db_path)
    await db.connect()

    orphans = await jobs.requeue_orphans()
    if orphans:
        log.info("%d unterbrochene Jobs zurueck in die Queue gestellt", orphans)

    log.info(
        "Worker bereit (Parallelitaet %d, Musik %s, Staging %s)",
        settings.worker_concurrency,
        settings.music_dir,
        settings.staging_dir,
    )
    await emit("Worker gestartet", category="system")

    running: set[asyncio.Task] = set()
    housekeeping = asyncio.create_task(maintenance())

    try:
        while not _stop.is_set():
            if len(running) >= settings.worker_concurrency:
                done, running = await asyncio.wait(
                    running, return_when=asyncio.FIRST_COMPLETED
                )
                continue

            job = await jobs.claim()
            if job is None:
                if running:
                    done, running = await asyncio.wait(
                        running, timeout=IDLE_SLEEP, return_when=asyncio.FIRST_COMPLETED
                    )
                else:
                    try:
                        await asyncio.wait_for(_stop.wait(), timeout=IDLE_SLEEP)
                    except asyncio.TimeoutError:
                        pass
                continue

            running.add(asyncio.create_task(run_job(job)))
    finally:
        housekeeping.cancel()
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        await http.close_all()
        await db.close()
        log.info("Worker beendet")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: _stop.set())


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
