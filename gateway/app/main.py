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

import mimetypes

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import preflight
from .api import (
    auth as auth_api,
    catalog as catalog_api,
    diagnostics as diagnostics_api,
    downloads as downloads_api,
    jobs as jobs_api,
    library as library_api,
    mediathek as mediathek_api,
    logs as logs_api,
    overview as overview_api,
    search as search_api,
    users as users_api,
)
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
        "media-src 'self'; "
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


# Ein Modul je Zustaendigkeit - neue Bereiche kommen hier in die Liste,
# nicht als weiterer Zweig in einer Sammeldatei.
# Reihenfolge zaehlt: API und Proxy zuerst, der statische Mount ganz zuletzt.
ROUTER = (
    auth_api,
    overview_api,
    search_api,
    catalog_api,
    downloads_api,
    jobs_api,
    logs_api,
    diagnostics_api,
    library_api,
    mediathek_api,
    users_api,
    subsonic_proxy,
)

for modul in ROUTER:
    app.include_router(modul.router)

# Windows-Python kennt woff2 nicht und liefert es als text/plain aus. Browser
# nehmen die Datei trotzdem an - der format()-Hinweis im @font-face entscheidet -
# aber ein Reverse-Proxy davor koennte sie deshalb falsch behandeln oder ein
# zweites Mal komprimieren. Einmal richtig anmelden kostet nichts.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")


class Oberflaeche(StaticFiles):
    """Statische Dateien mit einer Zwischenspeicher-Regel je Art.

    Der Grund ist ein Fehler, der nach einem Update auftrat: die Oberflaeche
    besteht aus zwei Dutzend ES-Modulen, die der Browser einzeln holt und
    einzeln zwischenspeichert. Ohne Anweisung entscheidet er je Datei selbst,
    ob er nachfragt - und laedt nach einem Update ein halb altes, halb neues
    Gemisch. Das Ergebnis ist keine kaputte Seite, sondern eine Fehlermeldung
    wie "does not provide an export named …", die aussieht, als sei die
    Auslieferung defekt.

    Also: Programmcode und Stylesheets muessen bei jedem Aufruf nachfragen
    (no-cache heisst "zwischenspeichern ja, aber vorher fragen" - bei
    Unveraendertem antwortet der Server mit 304 und schickt nichts).
    Schriften dagegen aendern sich nie und duerfen ein Jahr liegen bleiben.
    """

    UNVERAENDERLICH = ("/fonts/",)

    async def get_response(self, path: str, scope):
        antwort = await super().get_response(path, scope)
        pfad = "/" + path.replace("\\", "/").lstrip("/")
        if any(pfad.startswith(o) for o in self.UNVERAENDERLICH):
            antwort.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            antwort.headers["Cache-Control"] = "no-cache"
        return antwort


if WEB_ROOT.exists():
    app.mount("/", Oberflaeche(directory=str(WEB_ROOT), html=True), name="web")
else:  # pragma: no cover
    log.warning("Web-Verzeichnis fehlt: %s", WEB_ROOT)
