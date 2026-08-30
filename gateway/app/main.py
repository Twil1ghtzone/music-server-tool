"""ASGI-Einstiegspunkt des Gateways.

Ein Prozess bedient zwei sehr unterschiedliche Welten:

  /rest/*   Subsonic. Fremde Clients, eigene Auth, muss byte-transparent sein.
  /api/*    Dashboard. Eigene Session-Auth, CSRF, strenge Header.
  /         Statische Web-Oberflaeche.

Die Trennung wird bewusst auch in der Middleware durchgehalten: die strengen
Sicherheitsheader gelten fuer die Web-Oberflaeche, nicht fuer den Proxy-Pfad -
dort wuerden sie fremden Clients nur Header aufdraengen, die sie ignorieren.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .api import auth as auth_api
from .api import dashboard as dashboard_api
from .api import library as library_api
from .clients import http
from .config import ensure_dirs, settings
from .db import configure, db
from .events import emit
from .logging_conf import get_logger, setup_logging
from .security import ensure_admin_user
from .subsonic import proxy as subsonic_proxy

log = get_logger("main")

WEB_ROOT = Path(__file__).parent / "web"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    ensure_dirs()
    configure(settings.db_path)
    await db.connect()
    await ensure_admin_user()

    log.info("Gateway bereit")
    log.info("  Navidrome : %s", settings.navidrome_url)
    log.info("  Deemix    : %s", settings.deemix_url)
    log.info("  Musik     : %s", settings.music_dir)
    log.info("  Staging   : %s", settings.staging_dir)
    log.info("  Stream-Modus: %s", settings.stream_mode)
    if not settings.navidrome_password:
        log.warning(
            "NAVIDROME_PASSWORD ist leer - Scan-Trigger und ID-Aufloesung "
            "werden nicht funktionieren."
        )
    await emit("Gateway gestartet", category="system")

    try:
        yield
    finally:
        await http.close_all()
        await db.close()


app = FastAPI(
    title="music-server-tool Gateway",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/rest"):
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
    return response


@app.get("/healthz", include_in_schema=False)
async def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    try:
        await db.fetch_value("SELECT 1")
        return JSONResponse({"ready": True})
    except Exception as exc:
        return JSONResponse({"ready": False, "error": str(exc)}, status_code=503)


# Reihenfolge zaehlt: API und Proxy zuerst, der statische Mount ganz zuletzt.
app.include_router(auth_api.router)
app.include_router(dashboard_api.router)
app.include_router(library_api.router)
app.include_router(subsonic_proxy.router)

if WEB_ROOT.exists():
    app.mount("/", StaticFiles(directory=str(WEB_ROOT), html=True), name="web")
else:  # pragma: no cover
    log.warning("Web-Verzeichnis fehlt: %s", WEB_ROOT)
