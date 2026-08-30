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
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import preflight
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
from .subsonic.payload import error_envelope, to_xml

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
    log.info("  Import-Layout: %s", settings.import_layout)
    await preflight.log_summary()
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


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Unerwartete Fehler landen im Protokoll, nicht nur im Container-Log.

    Sonst sieht man im Dashboard nur, dass etwas nicht ging, und muss fuer das
    Warum auf die Kommandozeile - genau der Bruch, der die Fehlersuche zaeh
    macht.
    """
    log.exception("Unbehandelter Fehler bei %s %s", request.method, request.url.path)
    await emit(
        f"{type(exc).__name__} bei {request.method} {request.url.path}: {exc}"[:500],
        category="fehler",
        level="error",
        data={"pfad": request.url.path, "methode": request.method},
    )
    if request.url.path.startswith("/rest"):
        # Subsonic-Clients brauchen eine protokollkonforme Antwort, sonst
        # bricht ihre Warteschlange ab.
        return Response(
            content=to_xml(error_envelope(0, "Interner Fehler im Gateway")),
            media_type="text/xml; charset=utf-8",
        )
    return JSONResponse({"detail": "Interner Fehler - siehe Protokoll"}, status_code=500)


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
