"""Rauchtest ohne laufende Nachbarn.

Startet den Gateway in einem Wegwerf-Verzeichnis, mit absichtlich toten
Adressen fuer Navidrome und Deemix, und prueft: antwortet alles, greift CSRF,
stimmt die Subsonic-Serialisierung, verhaelt sich die Duplikat-Logik richtig.

    cd gateway && python tests/smoke.py

Erwartung: kein 500, keine Ausnahme, Navidrome sauber als offline erkannt.
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = Path(tempfile.mkdtemp(prefix="mst-smoke-"))
for name in ("music", "staging", "quarantine", "data", "cache"):
    (BASE / name).mkdir()

os.environ.update(
    GATEWAY_ROLE="api",
    LOG_LEVEL="warning",
    DB_PATH=str(BASE / "data" / "gateway.db"),
    CACHE_DIR=str(BASE / "cache"),
    MUSIC_DIR=str(BASE / "music"),
    STAGING_DIR=str(BASE / "staging"),
    QUARANTINE_DIR=str(BASE / "quarantine"),
    # Absichtlich tote Ports: der Test darf keine Nachbarn brauchen.
    NAVIDROME_URL="http://127.0.0.1:59991",
    DEEMIX_URL="http://127.0.0.1:59992",
    # Bewusst leer: das ist der Auslieferungszustand. Der Gateway muss auch
    # ohne eigenes Navidrome-Passwort sauber hochkommen.
    NAVIDROME_PASSWORD="",
    GATEWAY_ADMIN_USER="admin",
    GATEWAY_ADMIN_PASSWORD="supersecret123",
    GATEWAY_SESSION_SECRET="0" * 64,
    GATEWAY_PROVIDER_SEARCH="false",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.clients import navidrome  # noqa: E402
from app.errors import PermanentError  # noqa: E402
from app.main import app  # noqa: E402
from app.services import dedupe, downloader, jobs  # noqa: E402
from app.subsonic import ids, payload  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, extra: str = "") -> None:
    print(f"{'OK  ' if condition else 'FAIL'} {label} {extra}")
    if not condition:
        failures.append(label)


# ------------------------------------------------------------------- HTTP
with TestClient(app) as client:
    check("healthz", client.get("/healthz").text == "ok")
    check("readyz", client.get("/readyz").json()["ready"] is True)
    check("me ohne Session -> 401", client.get("/api/auth/me").status_code == 401)

    bad = client.post("/api/auth/login", json={"username": "admin", "password": "falsch"})
    check("Login mit falschem Passwort -> 401", bad.status_code == 401)

    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "supersecret123"}
    )
    check("Login", login.status_code == 200, login.text[:120])
    headers = {"X-CSRF-Token": login.json()["csrf"]} if login.status_code == 200 else {}

    check("me mit Session", client.get("/api/auth/me").json().get("username") == "admin")

    status = client.get("/api/status")
    check("status", status.status_code == 200, status.text[:120])
    if status.status_code == 200:
        check("status: Navidrome offline erkannt",
              status.json()["navidrome"]["online"] is False)

    check("library/stats", client.get("/api/library/stats").status_code == 200)
    check("library/dupes leer", client.get("/api/library/dupes").json()["groups"] == [])
    check("library/issues", client.get("/api/library/issues").status_code == 200)
    check("jobs", client.get("/api/jobs").status_code == 200)
    check("diagnostics", client.get("/api/diagnostics").status_code == 200)

    check("Scan ohne CSRF-Header -> 403",
          client.post("/api/library/scan").status_code == 403)
    first = client.post("/api/library/scan", headers=headers)
    check("Scan mit CSRF-Header", first.status_code == 200, first.text[:120])
    second = client.post("/api/library/scan", headers=headers)
    check("Scan wird dedupliziert", second.json()["job"] == first.json()["job"])

    # Navidrome ist tot -> der Proxy muss trotzdem gueltiges Subsonic liefern.
    ping = client.get("/rest/ping.view", params={"u": "a", "p": "b", "c": "t"})
    check("Proxy ohne Navidrome -> gueltige Fehlerantwort",
          ping.status_code == 200 and "subsonic-response" in ping.text)

    unknown = client.get(
        "/rest/getSong.view", params={"id": "mgv-dz-999", "u": "a", "p": "b", "c": "t", "f": "json"}
    )
    check("Unbekannte virtuelle ID -> Fehlercode 70",
          unknown.json()["subsonic-response"]["error"]["code"] == 70)

    # Schutzschalter: nichts darf den vorhandenen Bestand anfassen.
    blocked = client.post("/api/library/dupes/apply", json={"groups": [1]}, headers=headers)
    check("Dedup-Anwenden ist gesperrt", blocked.status_code == 403, blocked.text[:100])
    blocked = client.patch("/api/library/files/1/tags", json={"title": "x"}, headers=headers)
    check("Tag-Schreiben ist gesperrt", blocked.status_code == 403, blocked.text[:100])

    # Ohne Navidrome-Zugangsdaten darf gar kein Scan-Job entstehen.
    blocked = client.post("/api/scan", json={"full": False}, headers=headers)
    check("Navidrome-Scan ohne Zugangsdaten -> 409", blocked.status_code == 409,
          blocked.text[:110])

    # Eintraege aus der Warteschlange entfernen - aber nie ein Mapping,
    # das ein Client in einer Playlist stehen haben koennte.
    client.post("/api/download", json={"provider_id": "1109731"}, headers=headers)
    vorher = len(client.get("/api/queue").json()["items"])
    geloescht = client.delete("/api/queue/mgv-dz-1109731", headers=headers)
    nachher = len(client.get("/api/queue").json()["items"])
    check("Eintrag laesst sich entfernen",
          geloescht.status_code == 200 and nachher == vorher - 1,
          f"{vorher} -> {nachher}")

    clear = client.post("/api/queue/clear-failed", headers=headers)
    check("Fehlgeschlagene aufraeumen antwortet", clear.status_code == 200, clear.text[:80])

    info = client.get("/api/navidrome/credentials")
    check("Zugangs-Status abfragbar",
          info.status_code == 200 and info.json()["configured"] is False, info.text[:110])

    # Navidrome ist im Test nicht erreichbar -> 503 statt stiller Ablage.
    rejected = client.post("/api/navidrome/credentials",
                           json={"username": "admin", "password": "x"}, headers=headers)
    check("Zugang ohne erreichbares Navidrome -> 503", rejected.status_code == 503,
          rejected.text[:110])

    report = client.get("/api/preflight")
    check("Preflight antwortet", report.status_code == 200, report.text[:100])
    if report.status_code == 200:
        names = {c["name"] for c in report.json()["checks"]}
        check("Preflight prueft Staging-Trennung", "Staging getrennt" in names)
        staging_check = next(
            c for c in report.json()["checks"] if c["name"] == "Staging getrennt"
        )
        check("Staging liegt ausserhalb der Bibliothek", staging_check["status"] == "ok",
              staging_check["detail"])

# ---------------------------------------------------------------- Einheiten
check("ID-Format", ids.make("dz", "123") == "mgv-dz-123")
check("ID erkennen", ids.is_virtual("mgv-dz-123") and not ids.is_virtual("abc123"))
check("ID parsen", ids.parse("mgv-dz-123") == ("dz", "123"))

xml = payload.to_xml(
    payload.envelope({"song": {"id": "x", "title": 'A & B "C"', "isDir": False}})
).decode()
check("XML: Escaping", """'A &amp; B "C"'""" in xml, xml[-90:])
check("XML: bool wird true/false", 'isDir="false"' in xml)
check("XML: Namensraum", 'xmlns="http://subsonic.org/restapi"' in xml)

song = payload.virtual_song(
    {"id": "mgv-dz-1", "title": "Choere", "artist": "Mark Forster", "album": "Tape",
     "duration": 200, "state": "virtual", "created_at": "2026-01-01T00:00:00.000Z"},
    " [Nicht heruntergeladen]",
)
check("Marker im Titel", song["title"].endswith("[Nicht heruntergeladen]"))
# docker compose verschluckt fuehrende Leerzeichen in .env-Werten. Der
# Abstand muss deshalb aus dem Code kommen, nicht aus der Konfiguration.
check("Marker ohne fuehrendes Leerzeichen bekommt trotzdem Abstand",
      payload.virtual_song({**song, "title": "Choere", "state": "virtual"},
                           "[Nicht heruntergeladen]")["title"]
      == "Choere [Nicht heruntergeladen]",
      payload.virtual_song({**song, "title": "Choere", "state": "virtual"},
                           "[Nicht heruntergeladen]")["title"])
check("Leerer Marker haengt nichts an",
      payload.virtual_song({**song, "title": "Choere", "state": "virtual"}, "")["title"]
      == "Choere")
check("Marker zeigt Zustand",
      payload.virtual_song({**song, "title": "X", "state": "downloading"}, " [x]")["title"]
      .endswith("[Wird geladen]"))

check("Pfad-Bereinigung", downloader.safe_component("AC/DC: Back?") == "AC_DC_ Back_")

# preserve: die von Deemix erzeugte Struktur muss unveraendert uebernommen
# werden, sonst weicht der Neuzugang vom bestehenden Bestand ab.
staged = BASE / "staging" / "Mark Forster" / "01 - Choere.mp3"
planned = downloader.plan_destination(staged, {"artist": "Egal", "title": "Egal"})
check("Import preserve: Struktur bleibt erhalten",
      planned == BASE / "music" / "Mark Forster" / "01 - Choere.mp3", str(planned))

flat = BASE / "staging" / "Mark Forster - Choere.mp3"
check("Import preserve: flache Ablage bleibt flach",
      downloader.plan_destination(flat, {}) == BASE / "music" / "Mark Forster - Choere.mp3")

# Begleitdateien: Navidrome ist per ND_LYRICSPRIORITY auf .lrc angewiesen.
staged.parent.mkdir(parents=True, exist_ok=True)
staged.write_bytes(b"audio")
staged.with_suffix(".lrc").write_text("[00:00.00] Text", encoding="utf-8")
(staged.parent / "cover.jpg").write_bytes(b"jpg")
moved_to = downloader.move_into_library(staged, planned)
carried = downloader.move_sidecars(staged, moved_to)
check("Lyrics werden mitgenommen", moved_to.with_suffix(".lrc").exists())
check("Cover wird mitgenommen", (moved_to.parent / "cover.jpg").exists(),
      str([p.name for p in carried]))

flac = {"ext": ".flac", "bitrate": 900000, "sample_rate": 44100, "title": "t", "artist": "a",
        "album": "b", "album_artist": "c", "year": 2001, "track_no": 1, "has_cover": 1,
        "duration": 200, "path": "/music/a/b/t.flac", "size": 30_000_000}
mp3 = {**flac, "ext": ".mp3", "bitrate": 128000, "path": "/music/a/b/t (1) copy.mp3",
       "size": 3_000_000}
check("Keeper: FLAC schlaegt MP3-Kopie", dedupe.keeper_score(flac) > dedupe.keeper_score(mp3),
      f"{dedupe.keeper_score(flac)} > {dedupe.keeper_score(mp3)}")

random.seed(1)
a = [random.getrandbits(32) for _ in range(200)]
check("Fingerprint: identisch -> 1.0", dedupe.similarity(a, a) == 1.0)
check("Fingerprint: Versatz toleriert", dedupe.similarity(a, a[7:]) > 0.99)
noisy = [x ^ (1 << random.randrange(32)) for x in a]
check("Fingerprint: leichtes Rauschen bleibt ueber der Schwelle",
      dedupe.similarity(a, noisy) >= dedupe.ACOUSTIC_MATCH_THRESHOLD,
      str(dedupe.similarity(a, noisy)))
# Unkorrelierte Fingerprints liegen bei ~0.5 (50 % Bitfehler), nicht bei 0.
other = [random.getrandbits(32) for _ in range(200)]
check("Fingerprint: fremder Titel unter der Schwelle",
      dedupe.similarity(a, other) < dedupe.ACOUSTIC_MATCH_THRESHOLD,
      str(dedupe.similarity(a, other)))

# Haengende Zustaende: ein Titel auf 'downloading' ohne Job muss aufgeloest
# werden, sonst laeuft er in der Oberflaeche ewig weiter.
import asyncio  # noqa: E402

from app import db as _dbmod  # noqa: E402


async def _orphan_case() -> tuple[str, int]:
    # Eigene Datenbank: die der App ist nach dem TestClient-Block geschlossen.
    _dbmod.configure(BASE / "data" / "orphan.db")
    await _dbmod.db.connect()
    await _dbmod.db.execute(
        "INSERT INTO virtual_track(id, provider, provider_id, title, state) "
        "VALUES ('mgv-dz-777', 'dz', '777', 'Haenger', 'downloading')"
    )
    geloest = await downloader.reset_orphaned_states()
    row = await _dbmod.db.fetch_one(
        "SELECT state FROM virtual_track WHERE id = 'mgv-dz-777'"
    )

    # Ein importierter Titel traegt ein Mapping, das Clients in Playlists
    # stehen haben koennen - der darf nicht entfernbar sein.
    await _dbmod.db.execute(
        "INSERT INTO virtual_track(id, provider, provider_id, title, state, navidrome_id) "
        "VALUES ('mgv-dz-888', 'dz', '888', 'Fertig', 'ready', 'nd-real-1')"
    )
    geschuetzt = False
    try:
        await downloader.forget_track("mgv-dz-888")
    except ValueError:
        geschuetzt = True
    noch_da = await _dbmod.db.fetch_one(
        "SELECT navidrome_id FROM virtual_track WHERE id = 'mgv-dz-888'"
    )
    await _dbmod.db.close()
    return row["state"], geloest, geschuetzt, bool(noch_da)


_state, _resolved, _protected, _still_there = asyncio.run(_orphan_case())
check("Haengender Titel wird beim Start geloest", _state == "failed" and _resolved == 1,
      f"{_state}, {_resolved} betroffen")
check("Importierter Titel ist vor dem Entfernen geschuetzt", _protected and _still_there)

# Fehlende Zugangsdaten sind kein Fall fuer Wiederholungen.
check("NoCredentials ist ein permanenter Fehler",
      issubclass(navidrome.NoCredentials, PermanentError))

# Backoff: ohne Wartezeit laufen alle Versuche in derselben Sekunde durch.
check("Backoff waechst", jobs.backoff_seconds(1) < jobs.backoff_seconds(2)
      < jobs.backoff_seconds(3), f"{[jobs.backoff_seconds(n) for n in (1,2,3)]}")
check("Erster Versuch wartet mindestens 30 s", jobs.backoff_seconds(1) >= 30)
check("Backoff ist gedeckelt", jobs.backoff_seconds(20) <= 1800,
      str(jobs.backoff_seconds(20)))

# Ohne gesetztes Geheimnis muss eines erzeugt UND behalten werden, sonst
# meldet jeder Neustart alle Browser ab.
from app import config  # noqa: E402

os.environ.pop("GATEWAY_SESSION_SECRET", None)
first = config._session_secret()
second = config._session_secret()
check("Session-Secret wird erzeugt", len(first) == 64, f"{len(first)} Zeichen")
check("Session-Secret ueberlebt den Neustart", first == second)
check("Session-Secret liegt im Datenverzeichnis",
      (BASE / "data" / "session.secret").exists())

print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Rauchtests bestanden.")
