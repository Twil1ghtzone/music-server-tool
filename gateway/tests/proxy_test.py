"""Suchpfade gegen eine Navidrome-Attrappe.

Der Rauchtest kommt ohne Nachbarn aus, kann dafuer aber nicht pruefen, was ein
Musik-Client tatsaechlich zu sehen bekommt. Hier laeuft deshalb ein winziger
Navidrome-Ersatz im selben Prozess - bewusst buchstabengetreu suchend, wie das
Original.

    cd gateway && python tests/proxy_test.py

Braucht Internet: die Katalogtreffer kommen von der echten Deezer-API.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NAV_USER = "admin"
NAV_PASSWORD = "geheim123"
NAV_PORT = 45330

LOCAL_SONG = {
    "id": "nd-lokal-1", "parent": "nd-album-1", "isDir": False,
    "title": "Kogong", "album": "TAPE", "artist": "Mark Forster",
    "duration": 223, "suffix": "mp3", "bitRate": 320,
    "path": "Mark Forster/TAPE/03 - Kogong.mp3",
    "created": "2026-01-01T00:00:00.000Z", "type": "music",
}


# --------------------------------------------------------------- Attrappe
class FakeNavidrome(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    @staticmethod
    def _valid(query: dict) -> bool:
        user = (query.get("u") or [""])[0]
        token = (query.get("t") or [""])[0]
        salt = (query.get("s") or [""])[0]
        if user != NAV_USER or not token or not salt:
            return False
        return token == hashlib.md5((NAV_PASSWORD + salt).encode()).hexdigest()

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if not self._valid(query):
            body = {"subsonic-response": {"status": "failed", "version": "1.16.1",
                    "error": {"code": 40, "message": "Wrong username or password"}}}
        else:
            # Buchstabengetreu - genau das ist der Grund fuer die Korrektur
            # ueber den Katalog.
            term = (query.get("query") or [""])[0].strip().lower()
            songs = [LOCAL_SONG] if "mark forster" in term else []
            body = {"subsonic-response": {"status": "ok", "version": "1.16.1",
                    "type": "navidrome", "serverVersion": "0.55.0-fake",
                    "scanStatus": {"scanning": False, "count": 4711},
                    "albumList2": {"album": []},
                    "searchResult3": {"song": songs}}}

        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FakeDeemix(BaseHTTPRequestHandler):
    """Antwortet wie der echte Fork: immer HTTP 200, Ergebnis im Rumpf.

    Genau darauf ist der Gateway einmal hereingefallen - eine Ablehnung sah
    aus wie ein Erfolg.
    """

    mode = "reject"      # "reject" | "accept" | "needs-login"
    logged_in = False    # wie beim Original: Sitzungszustand im Server
    good_arl = "abcdef0123456789"

    def log_message(self, *args):
        pass

    def _send(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)

        if path == "/api/loginArl":
            arl = (json.loads(raw or b"{}") or {}).get("arl", "")
            if arl == FakeDeemix.good_arl:
                FakeDeemix.logged_in = True
                self._send({"status": 1, "arl": arl, "user": {"name": "Andrej"}})
            else:
                self._send({"status": 0, "arl": arl, "user": {}})
            return

        if path != "/api/addToQueue":
            self.send_response(404)
            self.end_headers()
            return

        if FakeDeemix.mode == "accept" or (
            FakeDeemix.mode == "needs-login" and FakeDeemix.logged_in
        ):
            self._send({"result": True, "data": {"obj": {"id": "1"}}})
        else:
            self._send({"result": False, "errid": "NotLoggedIn",
                        "data": {"url": "…", "bitrate": 3}})

    def do_GET(self):
        self._send({"result": True, "queue": []})


DEEMIX_PORT = 45331
server = ThreadingHTTPServer(("127.0.0.1", NAV_PORT), FakeNavidrome)
threading.Thread(target=server.serve_forever, daemon=True).start()
deemix_server = ThreadingHTTPServer(("127.0.0.1", DEEMIX_PORT), FakeDeemix)
threading.Thread(target=deemix_server.serve_forever, daemon=True).start()

# ------------------------------------------------------------- Umgebung
BASE = Path(tempfile.mkdtemp(prefix="mst-proxy-"))
for name in ("music", "staging", "quarantine", "data", "cache"):
    (BASE / name).mkdir()

os.environ.update(
    LOG_LEVEL="warning",
    DB_PATH=str(BASE / "data" / "gateway.db"), CACHE_DIR=str(BASE / "cache"),
    MUSIC_DIR=str(BASE / "music"), STAGING_DIR=str(BASE / "staging"),
    QUARANTINE_DIR=str(BASE / "quarantine"),
    NAVIDROME_URL=f"http://127.0.0.1:{NAV_PORT}",
    NAVIDROME_USER=NAV_USER, NAVIDROME_PASSWORD=NAV_PASSWORD,
    DEEMIX_URL=f"http://127.0.0.1:{DEEMIX_PORT}",
    GATEWAY_ADMIN_USER="admin", GATEWAY_ADMIN_PASSWORD="devpassword123",
    GATEWAY_SESSION_SECRET="0" * 64,
)

from fastapi.testclient import TestClient  # noqa: E402

from app.errors import PermanentError  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'OK  ' if cond else 'FAIL'} {label} {extra}")
    if not cond:
        failures.append(label)


def subsonic(query: str | None = None, **extra) -> dict:
    salt = "abcdef12"
    params = {
        "u": NAV_USER,
        "t": hashlib.md5((NAV_PASSWORD + salt).encode()).hexdigest(),
        "s": salt, "c": "test", "v": "1.16.1", "f": "json",
    }
    if query is not None:
        params["query"] = query
        params["songCount"] = "20"
    params.update(extra)
    return params


def split(songs: list[dict]) -> tuple[list[dict], list[dict]]:
    return ([s for s in songs if not s["id"].startswith("mgv-")],
            [s for s in songs if s["id"].startswith("mgv-")])


with TestClient(app) as client:
    client.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})

    # --- Dashboard --------------------------------------------------------
    right = client.get("/api/search", params={"q": "mark forster"}).json()
    check("Dashboard: korrekte Schreibweise findet lokal", len(right["local"]) > 0)
    check("Dashboard: keine Korrektur noetig", right["corrected"] is None)
    check("Dashboard: Katalog liefert Treffer", len(right["catalog"]) > 0)

    typo = client.get("/api/search", params={"q": "marc forster"}).json()
    check("Dashboard: Tippfehler findet trotzdem lokal", len(typo["local"]) > 0)
    check("Dashboard: Korrektur wird ausgewiesen",
          typo["corrected"] == "Mark Forster", str(typo["corrected"]))

    # --- Was ein Musik-Client sieht ---------------------------------------
    body = client.get("/rest/search3.view", params=subsonic("mark forster")).json()
    lokal, virtuell = split(body["subsonic-response"]["searchResult3"]["song"])
    check("Proxy: lokale Treffer bleiben erhalten", len(lokal) > 0)
    check("Proxy: virtuelle Titel werden ergaenzt", len(virtuell) > 0, f"{len(virtuell)}")
    check("Proxy: Marker nur an virtuellen Titeln",
          all("[Nicht heruntergeladen]" in s["title"] for s in virtuell)
          and all("[Nicht" not in s["title"] for s in lokal))
    check("Proxy: virtuelle Titel haben Pflichtfelder",
          all(s.get("duration") and s.get("suffix") and s.get("albumId") for s in virtuell))

    body = client.get("/rest/search3.view", params=subsonic("marc forster")).json()
    lokal, virtuell = split(body["subsonic-response"]["searchResult3"]["song"])
    check("Proxy: Tippfehler findet lokale Titel", len(lokal) > 0)
    check("Proxy: Tippfehler ergaenzt weiterhin Katalogtreffer", len(virtuell) > 0)

    # XML ist das Standardformat des Protokolls - viele Clients nutzen es.
    xml = client.get("/rest/search3.view", params=subsonic("mark forster", f="xml")).text
    check("Proxy: XML enthaelt virtuelle Titel", "mgv-dz-" in xml)
    check("Proxy: XML hat den Namensraum", 'xmlns="http://subsonic.org/restapi"' in xml)

    song = client.get(
        "/rest/getSong.view", params=subsonic(id=virtuell[0]["id"])
    ).json()["subsonic-response"]["song"]
    check("Proxy: getSong loest virtuelle ID auf", song["id"] == virtuell[0]["id"])

    # Manche Clients loesen albumId und artistId auf, bevor sie einen Treffer
    # anzeigen. Ein Fehler dabei laesst den Eintrag stillschweigend
    # verschwinden - beide muessen also antworten.
    album = client.get(
        "/rest/getAlbum.view", params=subsonic(id=song["albumId"])
    ).json()["subsonic-response"]
    check("Proxy: getAlbum auf virtuelle ID antwortet",
          album.get("status") == "ok" and album["album"]["songCount"] == 1,
          str(album.get("error", "")))

    artist = client.get(
        "/rest/getArtist.view", params=subsonic(id=song["artistId"])
    ).json()["subsonic-response"]
    check("Proxy: getArtist auf virtuelle ID antwortet",
          artist.get("status") == "ok" and artist["artist"]["name"],
          str(artist.get("error", "")))

    # Subsonic-Clients haengen dem Suchbegriff gern ein * an.
    body = client.get("/rest/search3.view", params=subsonic('"mark forster*"')).json()
    lokal, virtuell2 = split(body["subsonic-response"]["searchResult3"]["song"])
    check("Proxy: Suchbegriff mit * und Anfuehrungszeichen funktioniert",
          len(virtuell2) > 0 and len(lokal) > 0, f"{len(lokal)} lokal, {len(virtuell2)} virtuell")

    # --- Zugang aus dem Dashboard -----------------------------------------
    client.delete("/api/navidrome/credentials",
                  headers={"X-CSRF-Token": client.cookies.get("mst_csrf", "")})

# --- Deemix: Ablehnung mit HTTP 200 ---------------------------------------
import asyncio  # noqa: E402

from app.clients import deemix  # noqa: E402


async def _deemix_cases() -> tuple[str, str]:
    # Eigene Datenbank: die der App ist nach dem TestClient-Block zu, und
    # add_to_queue liest den gemerkten Transport aus der setting-Tabelle.
    from app import db as _dbmod

    _dbmod.configure(BASE / "data" / "deemix.db")
    await _dbmod.db.connect()

    FakeDeemix.mode = "reject"
    try:
        await deemix.add_to_queue("https://www.deezer.com/track/1", "3")
        abgelehnt = "faelschlich angenommen"
    except deemix.DeemixRejected as exc:
        abgelehnt = str(exc)
    except Exception as exc:
        abgelehnt = f"falscher Fehlertyp: {type(exc).__name__}"

    FakeDeemix.mode = "accept"
    try:
        angenommen = await deemix.add_to_queue("https://www.deezer.com/track/1", "3")
    except Exception as exc:
        angenommen = f"FEHLER {exc}"

    # ARL hinterlegen: falsche Werte duerfen nicht gespeichert werden.
    krumm = ""
    try:
        await deemix.set_arl("nicht-hex!!")
    except deemix.DeemixRejected as exc:
        krumm = str(exc)
    falsch = ""
    try:
        await deemix.set_arl("00112233445566")
    except deemix.DeemixRejected as exc:
        falsch = str(exc)
    gespeichert_nach_fehler = await deemix.arl_info()

    richtig = await deemix.set_arl(FakeDeemix.good_arl)
    info = await deemix.arl_info()

    # Der Kern: Deemix meldet NotLoggedIn, der Gateway meldet sich selbst an
    # und versucht erneut - ohne dass jemand eingreift.
    FakeDeemix.mode = "needs-login"
    FakeDeemix.logged_in = False
    try:
        selbstheilung = await deemix.add_to_queue("https://www.deezer.com/track/1", "3")
    except Exception as exc:
        selbstheilung = f"FEHLER {exc}"

    await _dbmod.db.close()
    return (abgelehnt, angenommen, krumm, falsch,
            gespeichert_nach_fehler, richtig, info, selbstheilung)


(_rejected, _accepted, _krumm, _falsch, _nach_fehler,
 _richtig, _info, _selbstheilung) = asyncio.run(_deemix_cases())

# Deemix lehnt mit HTTP 200 ab; der Gateway erkennt das, versucht sich
# anzumelden und meldet dann das eigentliche Hindernis - den fehlenden ARL.
check("Deemix: Ablehnung mit HTTP 200 wird erkannt",
      "ARL" in _rejected, _rejected[:90])
check("Deemix: Ablehnung ist ein permanenter Fehler",
      issubclass(deemix.DeemixRejected, PermanentError))
check("Deemix: Annahme wird als Erfolg gewertet",
      "angenommen" in _accepted, _accepted[:70])

check("ARL: Nicht-Hex wird abgewiesen", "Hex-Zeichen" in _krumm, _krumm[:70])
check("ARL: von Deezer abgelehnter Wert wird abgewiesen",
      "abgelehnt" in _falsch or "abgelaufen" in _falsch, _falsch[:70])
check("ARL: nichts Kaputtes wird gespeichert",
      _nach_fehler["configured"] is False, str(_nach_fehler))
check("ARL: gueltiger Wert meldet den Benutzer", _richtig.get("user") == "Andrej",
      str(_richtig))
check("ARL: wird nie im Klartext herausgegeben",
      _info["configured"] and FakeDeemix.good_arl not in json.dumps(_info),
      str(_info))
check("Deemix: NotLoggedIn loest eine eigene Anmeldung aus",
      "angenommen" in _selbstheilung, _selbstheilung[:80])

server.shutdown()
deemix_server.shutdown()
print()
if failures:
    print(f"{len(failures)} fehlgeschlagen: {failures}")
    sys.exit(1)
print("Suchpfade in Ordnung.")
