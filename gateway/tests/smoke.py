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
    # Braucht den Deezer-Katalog. Ist der gerade nicht erreichbar, ist das
    # kein Fehler dieses Codes - dann wird der Test uebersprungen statt
    # faelschlich rot zu werden.
    angefordert = client.post("/api/download", json={"provider_id": "1109731"},
                              headers=headers)
    if angefordert.status_code != 200:
        print(f"UEBERSPRUNGEN Warteschlangen-Eintrag entfernen "
              f"(Katalog nicht erreichbar: HTTP {angefordert.status_code})")
    else:
        vorher = len(client.get("/api/queue").json()["items"])
        geloescht = client.delete("/api/queue/mgv-dz-1109731", headers=headers)
        nachher = len(client.get("/api/queue").json()["items"])
        check("Eintrag laesst sich entfernen",
              geloescht.status_code == 200 and nachher == vorher - 1,
              f"{vorher} -> {nachher}")

    clear = client.post("/api/queue/clear-failed", headers=headers)
    check("Fehlgeschlagene aufraeumen antwortet", clear.status_code == 200, clear.text[:80])

    # --- Benutzerverwaltung -----------------------------------------------
    check("Erstkonto ist Administrator",
          client.get("/api/auth/me").json().get("role") == "admin")

    angelegt = client.post("/api/users", headers=headers,
                           json={"username": "mitbewohner", "password": "einlangespasswort",
                                 "role": "user"})
    check("Benutzer anlegen", angelegt.status_code == 200, angelegt.text[:90])
    neue_id = angelegt.json().get("id")

    doppelt = client.post("/api/users", headers=headers,
                          json={"username": "mitbewohner", "password": "einlangespasswort"})
    check("Doppelter Benutzername -> 409", doppelt.status_code == 409)

    # Ohne Passwort anlegen: eines wird erzeugt und genau einmal geliefert.
    ohne = client.post("/api/users", headers=headers,
                       json={"username": "gast", "role": "user"})
    check("Benutzer ohne Passwort anlegen", ohne.status_code == 200, ohne.text[:90])
    erzeugtes = ohne.json().get("password") or ""
    check("Erzeugtes Passwort wird einmal geliefert", len(erzeugtes) >= 16,
          f"{len(erzeugtes)} Zeichen")
    check("Erzeugtes Passwort funktioniert",
          client.post("/api/auth/login",
                      json={"username": "gast", "password": erzeugtes}).status_code == 200)
    # Die Sitzung des Testclients gehoert jetzt 'gast' - zurueck zum Admin.
    client.post("/api/auth/login", json={"username": "admin", "password": "supersecret123"})
    headers = {"X-CSRF-Token": client.cookies.get("mst_csrf", "")}

    gast_id = ohne.json()["id"]
    neu = client.patch(f"/api/users/{gast_id}", headers=headers,
                       json={"generate_password": True})
    check("Administrator kann Passwort erzeugen lassen",
          neu.status_code == 200 and len(neu.json().get("password") or "") >= 16,
          neu.text[:90])
    check("Das alte Passwort gilt danach nicht mehr",
          client.post("/api/auth/login",
                      json={"username": "gast", "password": erzeugtes}).status_code == 401)
    client.post("/api/auth/login", json={"username": "admin", "password": "supersecret123"})
    headers = {"X-CSRF-Token": client.cookies.get("mst_csrf", "")}
    client.delete(f"/api/users/{gast_id}", headers=headers)

    kurz = client.post("/api/users", headers=headers,
                       json={"username": "kurz", "password": "zukurz"})
    check("Zu kurzes Passwort wird abgelehnt", kurz.status_code == 422)

    liste = client.get("/api/users").json()["users"]
    eigene_id = next(u["id"] for u in liste if u["self"])
    selbst = client.delete(f"/api/users/{eigene_id}", headers=headers)
    check("Eigenes Konto ist nicht loeschbar", selbst.status_code == 409, selbst.text[:80])

    check("Benutzerliste zeigt beide", len(liste) == 2, str([u["username"] for u in liste]))

    # Der letzte Administrator darf sich nicht selbst entmachten.
    admin_id = next(u["id"] for u in liste if u["role"] == "admin")
    entmachten = client.patch(f"/api/users/{admin_id}", headers=headers, json={"role": "user"})
    check("Letzter Administrator bleibt Administrator", entmachten.status_code == 409,
          entmachten.text[:90])

    # --- Was ein normaler Benutzer darf und was nicht ---------------------
    # Zweite Sitzung OHNE with: die Lifespan laeuft bereits, ein zweiter
    # Kontextmanager wuerde sie erneut starten und die Datenbank unter der
    # ersten Sitzung wegziehen.
    gast = TestClient(app)
    anmeldung = gast.post("/api/auth/login",
                          json={"username": "mitbewohner", "password": "einlangespasswort"})
    check("Neuer Benutzer kann sich anmelden", anmeldung.status_code == 200,
          anmeldung.text[:90])
    check("Neuer Benutzer hat die Rolle 'user'",
          anmeldung.json().get("role") == "user", anmeldung.text[:70])
    gast_headers = {"X-CSRF-Token": anmeldung.json().get("csrf", "")}

    # Erlaubt: das, wofuer man den Zugang gibt.
    check("Benutzer darf die Uebersicht sehen", gast.get("/api/status").status_code == 200)
    check("Benutzer darf die Warteschlange sehen", gast.get("/api/queue").status_code == 200)
    check("Benutzer darf Jobs sehen", gast.get("/api/jobs").status_code == 200)

    # Verboten: alles, was einstellt, loescht oder Interna zeigt.
    verboten = {
        "Protokoll": gast.get("/api/logs"),
        "Diagnose": gast.get("/api/diagnostics"),
        "Client-Zugriffe": gast.get("/api/client-activity"),
        "Navidrome-Zugang": gast.get("/api/navidrome/credentials"),
        "Deemix-ARL": gast.get("/api/deemix/arl"),
        "Bibliothek": gast.get("/api/library/stats"),
        "Duplikate": gast.get("/api/library/dupes"),
        "Benutzerliste": gast.get("/api/users"),
        "Bibliotheks-Scan": gast.post("/api/library/scan", headers=gast_headers),
        "Warteschlange leeren": gast.post("/api/queue/clear-failed", headers=gast_headers),
        "Benutzer anlegen": gast.post(
            "/api/users", headers=gast_headers,
            json={"username": "heimlich", "password": "einlangespasswort"}),
    }
    for name, antwort in verboten.items():
        check(f"Benutzer darf nicht: {name}", antwort.status_code == 403,
              f"HTTP {antwort.status_code}")

    # --- Eigenes Passwort ohne Kenntnis des alten zuruecksetzen -----------
    # Eigener Client ohne Kontextmanager: dessen Lifespan wuerde beim
    # Verlassen die gemeinsame Datenbank schliessen, und ueber den
    # Admin-Client zu gehen wuerde dessen Sitzung wegziehen.
    verlierer = TestClient(app)
    anmeldung = verlierer.post(
        "/api/auth/login", json={"username": "mitbewohner", "password": "einlangespasswort"})
    vh = {"X-CSRF-Token": anmeldung.json().get("csrf", "")}
    antwort = verlierer.post("/api/auth/password/reset", headers=vh, json={})
    neues = antwort.json().get("password") or ""
    check("Passwort ohne Kenntnis des alten zuruecksetzbar",
          antwort.status_code == 200 and len(neues) >= 16, antwort.text[:90])
    # Die eigene Sitzung muss dabei sterben, sonst ist es kein Zuruecksetzen.
    check("Zuruecksetzen beendet die eigene Sitzung",
          verlierer.get("/api/auth/me").status_code == 401)
    check("Das neue Passwort funktioniert",
          verlierer.post("/api/auth/login",
                         json={"username": "mitbewohner",
                               "password": neues}).status_code == 200)
    check("Das alte Passwort funktioniert nicht mehr",
          verlierer.post("/api/auth/login",
                         json={"username": "mitbewohner",
                               "password": "einlangespasswort"}).status_code == 401)

    logs = client.get("/api/logs?level=all&limit=50")
    check("Protokoll ist abrufbar",
          logs.status_code == 200 and len(logs.json()["entries"]) > 0,
          str(len(logs.json().get("entries", []))) + " Eintraege")
    check("Protokoll kennt Bereiche", "system" in logs.json()["categories"],
          str(logs.json()["categories"]))
    gefiltert = client.get("/api/logs?level=error").json()["entries"]
    check("Protokollfilter greift", all(e["level"] == "error" for e in gefiltert),
          f"{len(gefiltert)} Fehler")
    volltext = client.get("/api/logs?q=Gateway").json()["entries"]
    check("Protokoll-Volltextsuche greift",
          all("Gateway" in e["message"] or "Gateway" in (e["data"] or "")
              for e in volltext), f"{len(volltext)} Treffer")

    # Ohne Navidrome-Zugang laesst sich der Client-Test nicht ausfuehren.
    test = client.get("/api/client-test")
    check("Client-Test verlangt Navidrome-Zugang", test.status_code == 409,
          test.text[:90])

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

# ------------------------------------------------- Auswahl beim Bereinigen
# Der Scanner darf sich irren, solange ein Mensch hinsieht. Automatisch
# auswaehlen darf er nur, wo es nichts zu entscheiden gibt - diese Faelle
# sind hier festgehalten, damit sie es auch bleiben.

MUSIK = str(dedupe.settings.music_dir).replace("\\", "/")


def _datei(pfad, **rest):
    """Eine Indexzeile, wie sie aus media_file kaeme."""
    zeile = {
        "id": abs(hash(pfad)) % 100000, "path": f"{MUSIK}/{pfad}",
        "ext": "." + pfad.rsplit(".", 1)[-1].lower(),
        "size": 40_000_000, "bitrate": 900_000, "sample_rate": 44100,
        "duration": 240.0, "has_cover": 1, "protected": 0, "file_hash": None,
        "title": "Titel", "artist": "Interpret", "album": "Album",
        "album_artist": "Interpret", "year": 2011, "track_no": 1,
    }
    zeile.update(rest)
    return zeile


check("Kopie erkannt: (2)", dedupe.kopie_nummer("/m/A/B/Song (2).flac") == 2)
check("Kopie erkannt: (12) ohne Leerzeichen", dedupe.kopie_nummer("/m/A/B/Song(12).mp3") == 12)
check("Kopie erkannt: sauberer Name ist keine",
      dedupe.kopie_nummer("/m/A/B/Song.flac") == 0)
check("Kopie erkannt: Jahreszahl im Namen ist keine Kopie",
      dedupe.kopie_nummer("/m/A/B/Live (1994).flac") == 1994 % 1000 if False else
      dedupe.kopie_nummer("/m/A/B/Live (1994).flac") == 0,
      "vierstellig faellt nicht unter das Muster")

# Format schlaegt alles Technische.
_flac = _datei("Nirvana/Nevermind/01 - Song.flac")
_mp3 = _datei("Nirvana/Nevermind/01 - Song.mp3", bitrate=320_000)
check("FLAC schlaegt MP3", dedupe.keeper_score(_flac) > dedupe.keeper_score(_mp3),
      f"{dedupe.keeper_score(_flac)} > {dedupe.keeper_score(_mp3)}")

# Das Original schlaegt die Kopie - auch wenn beide dasselbe Format haben.
_orig = _datei("Nirvana/Nevermind/01 - Song.flac")
_kopie = _datei("Nirvana/Nevermind/01 - Song (2).flac")
check("Original schlaegt nummerierte Kopie",
      dedupe.keeper_score(_orig) > dedupe.keeper_score(_kopie))

# Und zwar auch dann, wenn die Kopie das bessere Format hat: eine "(2).flac"
# neben einer "….flac" ist dieselbe Datei, kein Qualitaetsgewinn.
check("Kopie gewinnt nicht durch besseres Format",
      dedupe.keeper_score(_datei("A/B/S.flac")) >
      dedupe.keeper_score(_datei("A/B/S (2).flac")))

# Aber eine echte FLAC schlaegt eine MP3 auch dann, wenn die MP3 das
# Original ist - Qualitaet ist hier das staerkere Kriterium.
check("Echte FLAC schlaegt MP3 trotz gleicher Lage",
      dedupe.keeper_score(_datei("A/B/S.flac")) > dedupe.keeper_score(_datei("A/B/S.mp3")))

# Albumordner schlaegt Downloadordner.
_album = _datei("Nirvana/Nevermind/01 - Song.flac")
_lose = _datei("downloads/01 - Song.flac")
check("Albumordner schlaegt Downloadordner",
      dedupe.keeper_score(_album) > dedupe.keeper_score(_lose))

# Schutz schlaegt jedes technische Kriterium.
_geschuetzt_mp3 = _datei("downloads/Song (2).mp3", bitrate=128_000,
                         protected=1, protect_why="playlist")
_freie_flac = _datei("Nirvana/Nevermind/01 - Song.flac")
check("Geschuetzte MP3 schlaegt freie FLAC",
      dedupe.keeper_score(_geschuetzt_mp3) > dedupe.keeper_score(_freie_flac),
      "eine Playlist wiegt schwerer als jedes Format")


def _sortiert(*dateien):
    return sorted(dateien, key=dedupe.keeper_score, reverse=True)


darf, warum = dedupe.auto_auswahl(_sortiert(_orig, _kopie))
check("Auto: nummerierte Kopie ist eindeutig", darf, warum)

darf, warum = dedupe.auto_auswahl(_sortiert(
    _datei("A/B/S.flac"), _datei("A/B/S (2).flac", protected=1, protect_why="favorit")))
check("Auto: geschuetztes Duplikat blockiert die Auswahl", not darf, warum)

# Steht der geschuetzte Titel als Sieger fest, darf die schlechtere Datei
# weg - das ist genau der Sinn der Sache.
darf, warum = dedupe.auto_auswahl(_sortiert(
    _datei("A/B/S.flac", protected=1, protect_why="playlist"), _datei("A/B/S (2).flac")))
check("Auto: geschuetzter Sieger raeumt seine Kopie weg", darf, warum)

# Aber nicht, wenn der Schutz die schlechtere Datei gewinnen laesst: eine
# MP3 in einer Playlist duerfte sonst die FLAC daneben verdraengen.
darf, warum = dedupe.auto_auswahl(_sortiert(
    _datei("A/B/S.mp3", bitrate=128_000, protected=1, protect_why="playlist"),
    _datei("A/B/S.flac")))
check("Auto: Schutz darf keine Qualitaet kosten", not darf, warum)

darf, warum = dedupe.auto_auswahl(_sortiert(
    _datei("A/B/Song.flac", file_hash="deadbeef"),
    _datei("C/D/Anderer Name.flac", file_hash="deadbeef")))
check("Auto: byteweise identisch ist eindeutig", darf, warum)

darf, warum = dedupe.auto_auswahl(_sortiert(
    _datei("Nirvana/Nevermind/01 - Song.flac"), _datei("downloads/Song.mp3")))
check("Auto: FLAC im Albumordner gegen lose MP3 ist eindeutig", darf, warum)

# Zwei gleichwertige Dateien an verschiedenen Orten: das ist eine
# Geschmacksfrage und keine technische. Der Mensch entscheidet.
darf, warum = dedupe.auto_auswahl(_sortiert(
    _datei("Nirvana/Nevermind/01 - Song.flac"),
    _datei("Sampler/Grunge Hits/04 - Song.flac")))
check("Auto: zwei gleichwertige Dateien bleiben offen", not darf, warum)

darf, warum = dedupe.auto_auswahl(_sortiert(
    _datei("A/B/Song.mp3", bitrate=320_000), _datei("A/B/Song v2.mp3", bitrate=192_000)))
check("Auto: nur Bitrate reicht nicht", not darf, warum)

# Frist: die Grenzen muessen halten, sonst laesst sich per API eine
# Null-Tage-Frist setzen und die Quarantaene waere wertlos.
check("Quarantaenefrist: Standard sind 21 Tage",
      dedupe.QUARANTAENE_TAGE_STANDARD == 21)
check("Quarantaenefrist: Untergrenze mindestens ein Tag",
      dedupe.QUARANTAENE_TAGE_MIN >= 1)


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
    # Lebenszeichen des Workers: ohne eines gilt er als tot, danach als lebend.
    vorher = await jobs.worker_status()
    await jobs.heartbeat()
    nachher = await jobs.worker_status()

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
    return row["state"], geloest, geschuetzt, bool(noch_da), vorher, nachher


(_state, _resolved, _protected, _still_there,
 _hb_vorher, _hb_nachher) = asyncio.run(_orphan_case())
check("Ohne Lebenszeichen gilt der Worker als tot",
      _hb_vorher["alive"] is False and _hb_vorher["ever_seen"] is False, str(_hb_vorher))
check("Nach dem Lebenszeichen gilt er als lebend",
      _hb_nachher["alive"] is True and _hb_nachher["age"] is not None, str(_hb_nachher))
check("Haengender Titel wird beim Start geloest", _state == "failed" and _resolved == 1,
      f"{_state}, {_resolved} betroffen")
check("Importierter Titel ist vor dem Entfernen geschuetzt", _protected and _still_there)

# Ein leeres Katalogergebnis darf nicht fuer die volle Cache-Dauer
# festgeschrieben werden - eine einzelne Stoerung liesse den Katalog sonst
# minutenlang leer wirken, obwohl Deezer laengst wieder antwortet.
import time as _zeit  # noqa: E402

from app.clients import deezer as _deezer  # noqa: E402


async def _cache_case() -> tuple[float, float]:
    _deezer._cache.clear()

    async def leer() -> list:
        return []

    async def voll() -> list:
        return [{"provider_id": "1"}]

    await _deezer._cached("test:leer", leer)
    frist_leer = _deezer._cache["test:leer"][0] - _zeit.monotonic()
    await _deezer._cached("test:voll", voll)
    frist_voll = _deezer._cache["test:voll"][0] - _zeit.monotonic()
    _deezer._cache.clear()
    return frist_leer, frist_voll


_leer_frist, _voll_frist = asyncio.run(_cache_case())
check("Leeres Katalogergebnis wird nur kurz gehalten",
      _leer_frist <= _deezer.EMPTY_TTL + 1, f"{_leer_frist:.0f} s")
check("Echtes Katalogergebnis wird normal gehalten",
      _voll_frist > _deezer.EMPTY_TTL + 1, f"{_voll_frist:.0f} s")

# Der Fortschritt aus Deemix' Warteschlange. Die Antwortform unterscheidet
# sich zwischen Forks - deshalb hier alle drei Bauarten pruefen, statt sich
# auf die zu verlassen, die gerade im Container laeuft.
from app.clients import deemix as _deemix  # noqa: E402

_URL = "https://www.deezer.com/album/302127"

_liste = {"queue": [{"link": _URL, "downloaded": 7, "size": 13}]}
_anteil, _text = _deemix.queue_progress(_liste, _URL)
check("Deemix-Fortschritt aus Liste", abs((_anteil or 0) - 7 / 13) < 0.001, str(_text))
check("Deemix-Fortschritt nennt Titelzahl", _text == "Deemix: Titel 7 von 13", str(_text))

_zuordnung = {"queueList": {"302127": {"link": _URL, "progress": 42, "status": "downloading"}}}
_anteil2, _text2 = _deemix.queue_progress(_zuordnung, _URL)
check("Deemix-Fortschritt aus Zuordnung", abs((_anteil2 or 0) - 0.42) < 0.001, str(_anteil2))

# Prozent oder Anteil - beide Schreibweisen kommen vor.
_anteil3, _ = _deemix.queue_progress({"queue": [{"link": _URL, "progress": 0.5}]}, _URL)
check("Deemix-Fortschritt akzeptiert Anteile", abs((_anteil3 or 0) - 0.5) < 0.001, str(_anteil3))

# Fremde Eintraege duerfen den eigenen Auftrag nicht ueberschreiben.
_fremd = {"queue": [{"link": "https://www.deezer.com/album/999", "downloaded": 1, "size": 2}]}
check("Deemix-Fortschritt ignoriert fremde Auftraege",
      _deemix.queue_progress(_fremd, _URL) == (None, None))
check("Deemix-Fortschritt haelt eine leere Antwort aus",
      _deemix.queue_progress({}, _URL) == (None, None))


# ------------------------------------------- Duplikatlauf von Anfang bis Ende
# Der eigentliche Beweis: ein Index mit echten Faellen durch den ganzen Ablauf
# schicken - suchen, gruppieren, auswaehlen, in Quarantaene, Frist abwarten,
# endgueltig loeschen. Ohne ffmpeg auf dieser Maschine wird der akustische
# Teil mit vorbereiteten Fingerabdruecken gefuettert; die Vergleichslogik
# selbst ist weiter oben einzeln geprueft.

async def _dedupe_lauf():
    try:
        return await _dedupe_lauf_inner()
    finally:
        # Ohne das haelt ein offener aiosqlite-Thread den Prozess am Leben,
        # wenn unterwegs etwas schiefgeht - der Lauf haengt dann still.
        try:
            await _dbmod.db.close()
        except Exception:
            pass


async def _dedupe_lauf_inner():
    import struct
    from app.services import dedupe as _dd

    _dbmod.configure(BASE / "data" / "dedupe.db")
    await _dbmod.db.connect()

    musik = BASE / "music"

    async def lege_an(rel, inhalt, **rest):
        """Legt die Datei wirklich an - der Ablauf verschiebt sie spaeter."""
        ziel = musik / rel
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(inhalt)
        felder = {
            "path": str(ziel), "size": len(inhalt), "ext": ziel.suffix.lower(),
            "duration": 240.0, "bitrate": 900_000, "sample_rate": 44100,
            "has_cover": 1, "title": ziel.stem, "artist": "Interpret",
            "album": "Album", "album_artist": "Interpret", "year": 2011, "track_no": 1,
        }
        felder.update(rest)
        spalten = ",".join(felder)
        platz = ",".join("?" * len(felder))
        return int(await _dbmod.db.execute(
            f"INSERT INTO media_file({spalten}) VALUES ({platz})", tuple(felder.values())))

    # Fall 1: dieselbe Datei zweimal, einmal mit "(2)". Der klare Fall.
    bytes_a = b"AAAA" * 1000
    id_orig = await lege_an("Nirvana/Nevermind/01 - Song.flac", bytes_a, file_hash="h1")
    id_kopie = await lege_an("Nirvana/Nevermind/01 - Song (2).flac", bytes_a, file_hash="h1")

    # Fall 2: dieselbe Musik, aber eine davon steht in einer Playlist.
    bytes_b = b"BBBB" * 1000
    await lege_an("Avicii/True/09 - Hope.flac", bytes_b, file_hash="h2")
    id_gesch = await lege_an("Avicii/True/09 - Hope (2).flac", bytes_b, file_hash="h2",
                             protected=1, protect_why="playlist")

    # Fall 3: gleiche Musik, anderes Encoding - nur akustisch zu finden.
    id_flac = await lege_an("Swedish House Mafia/Until Now/03 - Greyhound.flac",
                            b"CCCC" * 1000, file_hash="h3", audio_hash="a3")
    id_mp3 = await lege_an("downloads/Greyhound.mp3", b"DDDD" * 500,
                           file_hash="h4", audio_hash="a4", ext=".mp3", bitrate=320_000)

    # Zwei Fingerabdruecke, die sich in wenigen Bits unterscheiden - so wie
    # zwei Kodierungen derselben Aufnahme.
    grund = [(i * 2654435761) & 0xFFFFFFFF for i in range(300)]
    leicht_anders = [w ^ (1 << (i % 3)) for i, w in enumerate(grund)]

    def packe(worte):
        return struct.pack(f"<{len(worte)}I", *worte)

    for datei_id, worte in ((id_flac, grund), (id_mp3, leicht_anders)):
        await _dbmod.db.execute(
            "INSERT INTO fingerprint(media_file_id, duration, raw_fp, bucket) "
            "VALUES (?,?,?,?)", (datei_id, 240.0, packe(worte), grund[0] >> 20))

    # --- Suchen -------------------------------------------------------------
    job = int(await _dbmod.db.execute(
        "INSERT INTO job(type, payload) VALUES ('find_dupes', '{}')"))
    bericht = await _dd.handle_find_dupes({"id": job, "payload": {"acoustic": True}})

    seite = await _dd.groups("open", limit=50)
    nach_art = {g["kind"]: g for g in seite["groups"]}

    # --- Wer soll bleiben? --------------------------------------------------
    exakt = [g for g in seite["groups"] if g["kind"] == "exact"]
    keeper_ids = {g["id"]: g["keeper_id"] for g in exakt}
    kopie_gruppe = next(
        (g for g in exakt if any(m["id"] == id_kopie for m in g["members"])), None)
    gesch_gruppe = next(
        (g for g in exakt if any(m["id"] == id_gesch for m in g["members"])), None)

    # --- Anwenden -----------------------------------------------------------
    # Der Schutzschalter steht im Auslieferungszustand auf "aus". settings ist
    # eingefroren, damit ihn niemand versehentlich zur Laufzeit umlegt - im
    # Test wird er deshalb ausdruecklich umgangen.
    object.__setattr__(_dd.settings, "allow_dedupe_apply", True)
    await _dd.setze_quarantaene_tage(21)
    job2 = int(await _dbmod.db.execute(
        "INSERT INTO job(type, payload) VALUES ('apply_dupes', '{}')"))
    angewandt = await _dd.handle_apply(
        {"id": job2, "payload": {"groups": [g["id"] for g in exakt]}})

    kopie_weg = not (musik / "Nirvana/Nevermind/01 - Song (2).flac").exists()
    gesch_da = (musik / "Avicii/True/09 - Hope (2).flac").exists()
    in_quarantaene = await _dd.quarantaene("held")

    # --- Frist: vor Ablauf passiert nichts ---------------------------------
    job3 = int(await _dbmod.db.execute(
        "INSERT INTO job(type, payload) VALUES ('purge_quarantine', '{}')"))
    frueh = await _dd.handle_purge({"id": job3, "payload": {}})
    noch_da = (await _dd.quarantaene("held"))["total"]

    # --- Frist abgelaufen ---------------------------------------------------
    await _dbmod.db.execute(
        "UPDATE quarantine_item SET purge_at = datetime('now', '-1 day') WHERE state = 'held'")
    spaet = await _dd.handle_purge({"id": job3, "payload": {}})
    nach_purge = (await _dd.quarantaene("held"))["total"]
    ordner_leer = not any((BASE / "quarantine").rglob("*.flac"))

    return {
        "bericht": bericht, "seite": seite, "nach_art": nach_art,
        "kopie_gruppe": kopie_gruppe, "gesch_gruppe": gesch_gruppe,
        "keeper_ids": keeper_ids, "id_orig": id_orig, "id_kopie": id_kopie,
        "id_gesch": id_gesch, "id_flac": id_flac, "id_mp3": id_mp3,
        "angewandt": angewandt, "kopie_weg": kopie_weg, "gesch_da": gesch_da,
        "quarantaene": in_quarantaene, "frueh": frueh, "noch_da": noch_da,
        "spaet": spaet, "nach_purge": nach_purge, "ordner_leer": ordner_leer,
    }


_dl = asyncio.run(_dedupe_lauf())

check("Lauf: findet exakte und akustische Duplikate",
      "exakt: 2" in _dl["bericht"] and "akustisch: 1" in _dl["bericht"], _dl["bericht"])
check("Lauf: akustische Gruppe gefunden trotz anderem Format",
      "acoustic" in _dl["nach_art"],
      "FLAC und MP3 derselben Aufnahme gehoeren zusammen")
check("Lauf: Seite meldet Gesamtzahl und Verschwendung",
      _dl["seite"]["total"] == 3 and _dl["seite"]["wasted"] > 0,
      f"{_dl['seite']['total']} Gruppen, {_dl['seite']['wasted']} Bytes")

check("Lauf: das Original bleibt, nicht die (2)",
      _dl["kopie_gruppe"] and _dl["kopie_gruppe"]["keeper_id"] == _dl["id_orig"])
check("Lauf: die nummerierte Kopie ist als solche erkannt",
      any(m["copy_no"] == 2 for m in (_dl["kopie_gruppe"] or {}).get("members", [])))
check("Lauf: eindeutige Gruppe ist zur Auto-Auswahl markiert",
      bool(_dl["kopie_gruppe"] and _dl["kopie_gruppe"]["auto_ok"]),
      (_dl["kopie_gruppe"] or {}).get("auto_why", ""))

check("Lauf: geschuetzte Datei wird als Keeper gewaehlt",
      _dl["gesch_gruppe"] and _dl["gesch_gruppe"]["keeper_id"] == _dl["id_gesch"],
      "was in einer Playlist steht, bleibt")
check("Lauf: geschuetzter Sieger darf seine Kopie automatisch abraeumen",
      _dl["gesch_gruppe"] and _dl["gesch_gruppe"]["auto_ok"],
      (_dl["gesch_gruppe"] or {}).get("auto_why", ""))

check("Lauf: Kopie ist aus der Bibliothek verschwunden", _dl["kopie_weg"])
check("Lauf: geschuetzte Datei liegt noch da", _dl["gesch_da"])
check("Lauf: Quarantaene fuehrt Buch",
      _dl["quarantaene"]["total"] == 2 and _dl["quarantaene"]["days"] == 21,
      f"{_dl['quarantaene']['total']} Eintraege, {_dl['quarantaene']['days']} Tage")
check("Lauf: Quarantaene nennt die verbleibende Frist",
      all(i["days_left"] >= 20 for i in _dl["quarantaene"]["items"]),
      str([i["days_left"] for i in _dl["quarantaene"]["items"]]))

check("Lauf: vor Fristablauf wird nichts geloescht",
      _dl["frueh"] == "Nichts faellig" and _dl["noch_da"] == 2, _dl["frueh"])
check("Lauf: nach Fristablauf wird geloescht",
      _dl["nach_purge"] == 0 and "2 endgueltig geloescht" in _dl["spaet"], _dl["spaet"])
check("Lauf: der Quarantaeneordner ist danach leer", _dl["ordner_leer"])


# ------------------------------------------------------- Anmeldesperre
# Der Fall, der einen Benutzer mit dem richtigen Passwort ausgesperrt hat:
# fuenf Vertipper beim zwanzigstelligen Startpasswort, danach galt die Sperre
# das ganze 15-Minuten-Fenster - waehrend die Meldung "bitte 5 Sekunden
# warten" versprach. Und eine erfolgreiche Anmeldung raeumte die Zaehler nie
# ab, also fiel man beim naechsten Vertipper sofort wieder hinein.

async def _sperr_faelle():
    _dbmod.configure(BASE / "data" / "sperre.db")
    await _dbmod.db.connect()
    try:
        from app import security as _sec

        async def fehlversuche(n, name="opfer", ip="10.0.0.9"):
            for _ in range(n):
                await _sec.record_attempt(ip, name, False)

        # Unter der Grenze: frei.
        await fehlversuche(_sec.MAX_ATTEMPTS_PER_USER - 1)
        unter = await _sec.is_throttled("10.0.0.9", "opfer")

        # Grenze erreicht: gesperrt, mit einer Wartezeit groesser null.
        await fehlversuche(1)
        drueber = await _sec.is_throttled("10.0.0.9", "opfer")

        # Die genannte Wartezeit muss die Wahrheit sein: nach ihrem Ablauf
        # ist wieder frei. Statt zu warten wird der letzte Versuch
        # zurueckdatiert.
        await _dbmod.db.execute(
            "UPDATE login_attempt SET ts = datetime('now', '-120 seconds')")
        nach_wartezeit = await _sec.is_throttled("10.0.0.9", "opfer")

        # Und eine erfolgreiche Anmeldung raeumt auf.
        await fehlversuche(_sec.MAX_ATTEMPTS_PER_USER)
        vor_erfolg = await _sec.is_throttled("10.0.0.9", "opfer")
        await _sec.clear_attempts("opfer", "10.0.0.9")
        nach_erfolg = await _sec.is_throttled("10.0.0.9", "opfer")

        # Ein anderes Konto von einer anderen IP bleibt davon unberuehrt.
        await fehlversuche(_sec.MAX_ATTEMPTS_PER_USER, "fremder", "10.0.0.99")
        fremder = await _sec.is_throttled("10.0.0.99", "fremder")

        return unter, drueber, nach_wartezeit, vor_erfolg, nach_erfolg, fremder
    finally:
        try:
            await _dbmod.db.close()
        except Exception:
            pass


(_unter, _drueber, _nach_zeit, _vor_erfolg, _nach_erfolg, _fremder) = asyncio.run(_sperr_faelle())

check("Sperre: unter der Grenze bleibt offen", _unter == (False, 0), str(_unter))
check("Sperre: an der Grenze wird gesperrt", _drueber[0] and _drueber[1] > 0, str(_drueber))
check("Sperre: die genannte Wartezeit stimmt",
      _nach_zeit == (False, 0),
      "nach Ablauf der angesagten Zeit ist wieder offen, nicht erst nach 15 Minuten")
check("Sperre: erfolgreiche Anmeldung raeumt den Zaehler",
      _vor_erfolg[0] and _nach_erfolg == (False, 0),
      f"{_vor_erfolg} -> {_nach_erfolg}")
check("Sperre: fremdes Konto bleibt unberuehrt", _fremder[0], str(_fremder))


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
