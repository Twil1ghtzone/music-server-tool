# music-server-tool

Ein Gateway zwischen **Navidrome** und **Deemix**: Titel, die noch nicht in der
Bibliothek liegen, tauchen trotzdem in der Suche jedes Subsonic-Clients auf und
werden beim ersten Abspielversuch automatisch heruntergeladen, getaggt,
einsortiert und von Navidrome indexiert. Dazu kommt ein Web-Dashboard für
Betrieb, Duplikatbereinigung und Metadatenpflege.

```
Substreamer / Symfonium / DSub
              │
              ▼
   ┌──────────────────────┐        ┌─────────────┐
   │  gateway-api  :8080  │───────▶│  navidrome  │
   │  Subsonic-Proxy      │        │    :4533    │
   │  Web-Dashboard       │        └──────┬──────┘
   └──────────┬───────────┘               │ ro
              │ Job-Queue (SQLite)        │
   ┌──────────▼───────────┐        ┌──────▼──────────┐
   │  gateway-worker      │───────▶│  /mnt/tank/     │
   │  Download · Import   │        │  music          │
   │  Scan · Dedup · Tags │        └─────────────────┘
   └──────────┬───────────┘               ▲
              │                           │ atomarer Move
       ┌──────▼──────┐            ┌───────┴─────────┐
       │   deemix    │───────────▶│  music-staging  │
       │    :6595    │            └─────────────────┘
       └─────────────┘
```

---

## Was es kann

**Just-in-Time-Downloads.** Die Suche im Client liefert lokale Treffer und
ergänzt sie um Katalogtreffer, markiert mit `[Nicht heruntergeladen]`. Der erste
Play-Request stößt den Download an. Nach dem Import verschwindet der Marker und
der Titel spielt normal — auch aus Playlists heraus, in denen die alte ID steht.

**Dashboard.** Live-Übersicht über Warteschlange, Jobs, Speicherplatz,
Navidrome-Status und Ereignisse (Server-Sent Events). Login mit Argon2id,
Session-Cookies, CSRF-Schutz, optional TOTP-Zweifaktor, Brute-Force-Bremse pro
IP und pro Konto.

**Bibliothekspflege.** Dreistufige Duplikaterkennung (Byte-Hash → Audio-Stream-
Hash → Chromaprint), Tag-Editor mit Batch-Modus, Mängelbericht über die
gesamte Bibliothek.

---

## Schnellstart

```bash
cp .env.example .env
```

In `.env` mindestens setzen: `MUSIC_DIR`, `STAGING_DIR`, `QUARANTINE_DIR`,
`NAVIDROME_PASSWORD`, `GATEWAY_ADMIN_PASSWORD`, `DEEZER_ARL`.

Session-Secret erzeugen:

```bash
openssl rand -hex 32
```

Rechte setzen und starten:

```bash
chmod 600 .env && docker compose up -d --build
```

Dann:

| Adresse | Zweck |
|---|---|
| `http://<host>:8080` | Dashboard |
| `http://<host>:8080/rest/…` | **Subsonic-Endpunkt für die Clients** |
| `http://<host>:4533` | Navidrome direkt (Erstkonfiguration, Nutzeranlage) |

> **Wichtig:** Der Client wird auf **Port 8080** eingerichtet, nicht auf 4533.
> Benutzername und Passwort bleiben die aus Navidrome — der Gateway prüft sie
> dort und speichert selbst kein Subsonic-Passwort.

### Reihenfolge bei der Ersteinrichtung

1. `docker compose up -d navidrome` — Navidrome starten, im Browser den
   Admin-Account anlegen. Genau dieses Passwort kommt in `NAVIDROME_PASSWORD`.
2. `docker compose up -d` — restliche Container starten.
3. Dashboard öffnen, unter **Diagnose** prüfen, ob Navidrome, Deezer, Deemix und
   ffmpeg/fpcalc grün sind.
4. Unter **Bibliothek → Neu indexieren** den ersten Index aufbauen.
5. Einen Client auf Port 8080 einrichten und nach einem Titel suchen, den es
   lokal nicht gibt.

---

## Konfiguration

Die vollständige Liste steht kommentiert in [.env.example](.env.example). Die
Schalter, die das Verhalten wirklich ändern:

| Variable | Bedeutung |
|---|---|
| `GATEWAY_STREAM_MODE` | `defer` (robust) oder `stream` (bessere UX) — siehe unten |
| `GATEWAY_MARKER_SUFFIX` | Text hinter noch nicht geladenen Titeln |
| `GATEWAY_PROVIDER_LIMIT` | Wie viele Katalogtreffer maximal angehängt werden |
| `GATEWAY_PROVIDER_SEARCH` | `false` schaltet die Katalogsuche komplett ab |
| `GATEWAY_WORKER_CONCURRENCY` | Parallele Jobs im Worker |
| `DEEMIX_BITRATE` | `1` = MP3 128, `3` = MP3 320, `9` = FLAC |

### `defer` gegen `stream`

Beide Modi lösen denselben Download aus, unterscheiden sich aber in dem, was der
Client währenddessen zu hören bekommt.

**`defer` (Standard).** Der Gateway liefert sofort einen kurzen Hinweiston aus.
Der Download läuft im Hintergrund, nach ein paar Sekunden startet man den Titel
erneut und er spielt. Ein HTTP-Fehler wäre technisch ehrlicher, bricht aber in
jedem getesteten Client die Warteschlange ab — ein gültiger, kurzer Audiostream
nicht.

**`stream`.** Der Gateway hält die Verbindung offen, sendet den Hinweiston in
Schleife und hängt den echten Titel an, sobald er importiert ist. MP3-Frames
sind selbstbeschreibend, deshalb übersteht der Decoder den Übergang. Preis:
kein Seeking (`Accept-Ranges: none`), keine `Content-Length`, und Clients mit
kurzem Timeout steigen aus. Erst umstellen, wenn `defer` läuft.

---

## Aufbau

```
gateway/app/
├── main.py            ASGI-App: /rest (Proxy), /api (Dashboard), / (Web)
├── worker.py          Eigener Prozess für alle langlaufenden Jobs
├── config.py          Konfiguration aus der Umgebung
├── db.py              SQLite (WAL), Schema, Verbindungspool
├── security.py        Argon2id, Sessions, CSRF, Rate-Limit, TOTP
├── events.py          Ereignisbus über die event_log-Tabelle
├── clients/
│   ├── navidrome.py   Subsonic-Client für Scan und ID-Auflösung
│   ├── deezer.py      Katalogsuche über die öffentliche Deezer-API
│   └── deemix.py      Download-Auslöser mit Transport-Erkennung
├── subsonic/
│   ├── proxy.py       Der Proxy: durchreichen, abfangen, ID übersetzen
│   ├── ids.py         Virtuelle IDs und ihr dauerhaftes Mapping
│   ├── payload.py     XML/JSON/JSONP-Serialisierung
│   └── auth.py        Credential-Pass-Through an Navidrome
├── services/
│   ├── jobs.py        Job-Queue auf SQLite
│   ├── downloader.py  Download → Staging → Tags → Bibliothek → ID
│   ├── scanner.py     Index, Hashes, ffprobe, Fingerprints
│   ├── dedupe.py      Duplikatgruppen, Keeper-Bewertung, Quarantäne
│   ├── tags.py        mutagen: Lesen, Schreiben, Validieren
│   └── ffmpeg.py      Subprozess-Brücke zu ffmpeg/ffprobe/fpcalc
├── api/               REST für das Dashboard
└── web/               Dashboard (kein Build-Schritt)
```

### Entscheidungen, die den Rest erklären

**Der Proxy reicht alles durch, was er nicht ausdrücklich abfängt.** Abgefangen
werden nur `search2`, `search3`, `getSong`, `getAlbum`, `stream`, `download`
und `getCoverArt`. Alles andere — Playlists, Scrobbling, Cover, Lyrics,
Range-Requests, Transcoding-Parameter — geht byteweise weiter und wird nie
deserialisiert.

**Von Navidrome wird immer JSON angefordert, auch wenn der Client XML will.**
Manipuliert wird nur ein dict; die Serialisierung ins Zielformat passiert erst
am Ende. XML-Bäume zu patchen wäre deutlich fehleranfälliger.

**Virtuelle IDs werden nie gelöscht.** Clients legen sie in Playlists und
Offline-Caches ab. Eine einmal ausgelieferte ID muss für immer auflösbar
bleiben, sonst entstehen tote Einträge auf Geräten, an die man nicht herankommt.

**Die Suche läuft über die öffentliche Deezer-API, nicht über Deemix.**
`api.deezer.com` ist dokumentiert, stabil und auth-frei; die Deemix-API wandert
zwischen Forks. Deemix macht nur noch das, was sonst niemand kann: den
Download. Das halbiert die Latenz im Suchpfad und macht ihn unabhängig davon,
ob der Deemix-Container gerade gesund ist.

**Deemix-Transport wird erkannt, nicht angenommen.** Eine Kandidatenliste wird
durchprobiert, der erste funktionierende Endpunkt gemerkt. Was der Container
tatsächlich beantwortet, steht im Dashboard unter *Diagnose*.

**Fertigstellung wird am Dateisystem festgestellt, nicht an einer API-Antwort.**
Eine neue Datei im Staging, deren Größe sich nicht mehr ändert, ist der
Nachweis. Das überlebt jeden API-Dialekt und fängt auch Dateien ein, die auf
anderem Weg dort landen.

**Der Worker ist ein eigener Prozess.** Ein Full-Scan oder ein Fingerprint-Lauf
darf nie im selben Event-Loop laufen wie der Proxy — sonst stottert die
Wiedergabe auf jedem Gerät im Haus.

---

## Duplikatbereinigung

Drei Stufen, jede nur auf dem, was die vorige übrig lässt:

| Stufe | Signal | Findet |
|---|---|---|
| 1 | `blake2b` über die Datei | byteweise identische Dateien |
| 2 | `md5` über den reinen Audiostream | gleiche Musik, andere Tags oder Cover |
| 3 | Chromaprint + Bitfehlerrate | gleiche Aufnahme, anderes Encoding |

Stufe 1 läuft **nur bei Größenkollision** — zwei byteweise identische Dateien
haben zwangsläufig dieselbe Größe, alles mit eindeutiger Größe kann den
Vollhash überspringen.

Stufe 2 ist der eigentliche Gewinn und der Grund, warum `fdupes` oder `rmlint`
bei Musik wenig finden: ein abweichender ID3-Block oder ein anderes eingebettetes
Cover macht zwei identische Aufnahmen byteweise verschieden.
`ffmpeg -c:a copy -f md5` ignoriert genau das, ohne zu dekodieren.

Stufe 3 vergleicht Roh-Fingerprints versatztolerant. Ohne Bucketing wäre das
quadratisch über die ganze Bibliothek; die oberen 16 Bit des ersten
Subfingerprints dienen als Bucket-Schlüssel.

### Zwei Regeln, die nicht abschaltbar sind

1. **Es wird nie automatisch gelöscht.** Ein Lauf erzeugt Vorschläge; das
   Anwenden ist ein eigener, ausdrücklicher Schritt im Dashboard.
2. **„Anwenden" heißt verschieben.** Die nicht behaltenen Dateien landen unter
   ihrem Originalpfad im Quarantäne-Ordner und lassen sich per Knopfdruck
   zurückholen.

Der Grund für Regel 2: Navidrome hängt Wiedergabezähler, Bewertungen und
Playlist-Einträge an seine eigenen `media_file`-IDs. Verschwindet eine Datei,
verschwindet diese Historie — und wenn sie in einer Playlist lag, reißt dort ein
Loch. Quarantäne macht den Schritt umkehrbar.

Die Keeper-Auswahl ist deterministisch: Format-Rang (verlustfrei schlägt
verlustbehaftet, unabhängig von der Bitrate) → Bitrate → Samplerate →
Tag-Vollständigkeit → Cover → Spieldauer → Pfad-Heuristik (`copy`, `(1)`,
`kopie` werden abgewertet). Übersteuerbar pro Gruppe im Dashboard.

---

## Sicherheit

**Dashboard (`/api/*`).** Argon2id, Session-Cookies (`HttpOnly`, `SameSite=Lax`,
`Secure` über `GATEWAY_SECURE_COOKIES`), CSRF per Double-Submit-Token, optional
TOTP, Rate-Limiting pro IP und pro Konto mit exponentiellem Backoff, konstante
Antwortzeit bei Fehllogins (keine Nutzer-Enumeration). In der DB liegt nur der
Hash des Session-Tokens.

**Subsonic (`/rest/*`).** Hier ist das alles prinzipiell nicht anwendbar: das
Protokoll schreibt `t = md5(passwort + salt)` vor, was ein reversibel
gespeichertes Passwort voraussetzt. Deshalb prüft der Gateway gar nicht selbst,
sondern reicht die Zugangsdaten an Navidrome weiter und übernimmt dessen Urteil
(kurz gecacht). Es gibt genau eine Passwortquelle im System, und der Gateway
speichert kein einziges Subsonic-Geheimnis. Gegen Raten über den Proxy gibt es
eine eigene Bremse.

**Betrieb.** `no-new-privileges` auf allen Containern, Navidrome mountet die
Bibliothek read-only, Deemix hat im Normalbetrieb keinen veröffentlichten Port
(die Zeile in `docker-compose.yml` ist nur für die Fehlersuche da). Strenge
Sicherheitsheader inklusive CSP gelten für das Dashboard, nicht für den
Proxy-Pfad — dort würden sie fremden Clients nur Header aufdrängen.

**Die stärkste Maßnahme ist trotzdem eine andere:** den Dienst nicht ins offene
Internet stellen. Tailscale oder WireGuard sind hier mehr wert als jede
Härtung an der Anwendung. Wenn es doch öffentlich sein muss, gehört ein
Reverse-Proxy mit TLS davor und `GATEWAY_SECURE_COOKIES=true` gesetzt.

---

## Betrieb

**Sichern.** Wichtig sind `./data/gateway.db` (virtuelle IDs, Index,
Fingerprints, Duplikatgruppen) und `./data/navidrome/navidrome.db`
(Wiedergabezähler, Playlists, Bewertungen). Beide bei gestoppten Containern
kopieren oder `sqlite3 … ".backup"` verwenden.

**Wenn Downloads nicht ankommen.** *Diagnose* im Dashboard zeigt, welche
Deemix-Endpunkte antworten. Häufigste Ursache ist ein abgelaufener ARL;
zweithäufigste ein Fork, der einen anderen Pfad erwartet — der lässt sich dort
auch manuell festlegen. Was Deemix tatsächlich sagt: `docker compose logs deemix`.

**Wenn ein Titel im Client als geladen markiert bleibt.** Der Import hat
geklappt, die Navidrome-ID fehlt noch. *Übersicht → Navidrome-Scan*, dann
erledigt der Worker den Rest beim nächsten Versuch selbst.

**Wenn Dateien im Staging liegen bleiben.** *Warteschlange → Staging
importieren*. Der Worker prüft das ohnehin alle fünf Minuten.

---

## Grenzen

- **Deemix verstößt gegen die Deezer-ToS.** ARL-Tokens werden invalidiert,
  Accounts gebannt. Das ist ein operatives Risiko, kein theoretisches — der
  Fehlerpfad im Downloader ist genau deshalb ausgebaut.
- **Seeking funktioniert im Modus `stream` nicht.** Bewusst so: einen
  Range-Support zu behaupten, den es nicht gibt, wäre schlimmer.
- **Virtuelle Titel erscheinen nur in der Suche**, nicht in Albenlisten oder
  Genre-Browsern. Diese Endpunkte werden absichtlich nicht angefasst.
- **Akustisches Fingerprinting ist rechenintensiv.** Der erste Lauf über eine
  große Bibliothek dauert Stunden. Er läuft im Worker, mit niedriger Priorität,
  und lässt sich jederzeit unterbrechen — der Fortschritt bleibt erhalten.
- **Ein Nutzerkonto im Dashboard.** Mehrbenutzerbetrieb ist nicht vorgesehen;
  die Subsonic-Seite kennt dagegen alle Navidrome-Nutzer.

---

## Entwicklung

```bash
python -m venv .venv && .venv/bin/pip install -r gateway/requirements.txt
```

Dashboard lokal starten — legt ein Wegwerf-Datenverzeichnis an und braucht
weder Navidrome noch Deemix (beide erscheinen dann als offline):

```bash
cd gateway && python tests/devserver.py
```

Rauchtest, prüft API, CSRF, Subsonic-Serialisierung und Duplikat-Logik ohne
laufende Nachbarn:

```bash
cd gateway && python tests/smoke.py
```

Einzelne Prozesse von Hand:

```bash
cd gateway && uvicorn app.main:app --reload --port 8080
```

```bash
cd gateway && python -m app.worker
```

Für beides müssen `DB_PATH`, `MUSIC_DIR`, `STAGING_DIR`, `CACHE_DIR`,
`NAVIDROME_URL` und `NAVIDROME_PASSWORD` in der Umgebung stehen. `ffmpeg`,
`ffprobe` und `fpcalc` müssen im `PATH` liegen — im Container bringt das Image
sie mit.
