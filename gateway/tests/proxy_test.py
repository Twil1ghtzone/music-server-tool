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


server = ThreadingHTTPServer(("127.0.0.1", NAV_PORT), FakeNavidrome)
threading.Thread(target=server.serve_forever, daemon=True).start()

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
    DEEMIX_URL="http://127.0.0.1:59992",
    GATEWAY_ADMIN_USER="admin", GATEWAY_ADMIN_PASSWORD="devpassword123",
    GATEWAY_SESSION_SECRET="0" * 64,
)

from fastapi.testclient import TestClient  # noqa: E402

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

    # --- Zugang aus dem Dashboard -----------------------------------------
    client.delete("/api/navidrome/credentials",
                  headers={"X-CSRF-Token": client.cookies.get("mst_csrf", "")})

server.shutdown()
print()
if failures:
    print(f"{len(failures)} fehlgeschlagen: {failures}")
    sys.exit(1)
print("Suchpfade in Ordnung.")
