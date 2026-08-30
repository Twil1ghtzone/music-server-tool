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

Die vier Dienste aus [docker-compose.yml](docker-compose.yml) übernehmen und
starten:

```bash
docker compose up -d
```

Das war es. Keine `.env`, keine Ordner anlegen, keine Rechte setzen:

- Fehlende Verzeichnisse legt Docker beim ersten Start an.
- Die Rechte darauf zieht der Gateway-Container selbst gerade — er startet als
  root, richtet `/data`, `/staging` und `/quarantine` für UID 1000 ein und
  läuft danach unprivilegiert weiter. Die Bibliothek unter `/music` fasst er
  dabei bewusst nicht an.
- Das Session-Geheimnis erzeugt er einmalig und legt es unter `/data` ab, damit
  Neustarts niemanden abmelden.
- Zugriff auf die Navidrome-API besorgt er sich selbst (siehe unten).

Das Passwort fürs Dashboard steht nach dem ersten Start im Log — es erscheint
genau einmal:

```bash
docker compose logs gateway-api | grep -A2 Start-Passwort
```

| Adresse | Zweck |
|---|---|
| `http://<host>:8080` | Dashboard |
| `http://<host>:8080/rest/…` | **Subsonic-Endpunkt für die Clients** |
| `http://<host>:4533` | Navidrome direkt (Nutzerverwaltung) |

> **Wichtig:** Der Client wird auf **Port 8080** eingerichtet, nicht auf 4533.
> Benutzername und Passwort bleiben die aus Navidrome — der Gateway prüft sie
> dort und speichert selbst kein Subsonic-Passwort.

### Variante: Clients müssen gar nichts umstellen

Wer seine Geräte nicht anfassen will — oder wer immer wieder in die
Port-Falle tritt — lässt den Gateway einfach **auf 4533 antworten**. Dann läuft
jeder vorhandene Client unverändert durch ihn hindurch. In
`docker-compose.yml` beide Kommentare umdrehen:

```yaml
  navidrome:
    ports:
      # - 4533:4533
      - 4534:4533          # Navidromes Weboberfläche zieht auf 4534

  gateway-api:
    ports:
      - 8080:8080
      - 4533:8080          # Gateway übernimmt den Client-Port
```

Danach `docker compose up -d`. Musik-Clients bleiben auf `…:4533` und sehen ab
sofort auch die ergänzten Titel; Navidromes eigene Oberfläche erreichst du
unter `…:4534`, das Dashboard weiterhin unter `…:8080`.

### Warum Navidromes eigene Oberfläche keine ungeladenen Titel zeigt

Das ist keine Fehlfunktion, sondern der Aufbau: der Gateway steht **vor**
Navidrome. Wer Navidrome auf Port 4533 aufruft, redet direkt mit Navidrome —
der Gateway ist in diesem Weg gar nicht enthalten und kann dort nichts
ergänzen.

```
Client → :8080 Gateway → :4533 Navidrome     ergänzte Titel sichtbar
Browser →               :4533 Navidrome      nur die echte Bibliothek
```

Navidrome dazu zu bringen wäre nur möglich, indem man erfundene Einträge in
seine Datenbank schreibt. Das ist bewusst nicht gebaut: es würde Navidromes
Scanner, Wiedergabezähler und Playlists mit Titeln füllen, die es als Datei
nicht gibt — und der erste Full-Scan räumt sie wieder weg.

Ergänzte Titel erscheinen also ausschließlich über Port 8080, und dort auch
nur in der **Suche** — nicht beim Blättern durch Alben oder Interpreten. Diese
Ansichten werden unverändert durchgereicht, sonst würde der Katalog die
Bibliothek fluten.

Ob der Gateway seine Arbeit tut, beantwortet **Diagnose → Was ein Musik-Client
sieht**: der Test fragt den eigenen Subsonic-Endpunkt genauso ab wie
Substreamer. Kommen dort Titel mit Marker zurück, liegt ein verbleibendes
Problem im Client — fast immer zeigt er noch auf 4533.

### Deemix verbinden (Deezer-ARL)

**Dass die Deemix-Oberfläche angemeldet ist, reicht nicht.** Deemix hält die
Deezer-Sitzung pro HTTP-Sitzung (`sessionDZ[req.session.id]`) — der Gateway ist
ein eigener Client mit eigener Sitzung und bekommt `NotLoggedIn`, obwohl im
Browser oben „You are logged in as …" steht.

Deshalb meldet sich der Gateway selbst an. Den ARL im Dashboard unter
**Diagnose → Deemix-Anmeldung** eintragen. Er wird vor dem Speichern gegen
Deezer geprüft und danach nie wieder ausgegeben; im Dashboard stehen nur die
letzten Zeichen.

Läuft eine Anfrage später in `NotLoggedIn` — etwa weil Deemix neu gestartet
wurde — meldet sich der Gateway automatisch neu an und versucht es erneut.

Der ARL liegt damit in der Gateway-Datenbank. Das ist derselbe Wert, der
ohnehin in `./config/deemix` steht, aber es ist eine zweite Kopie: wer das
nicht möchte, lässt das Feld leer und nimmt in Kauf, dass keine Downloads
laufen.

### Navidrome verbinden

Der Gateway braucht selbst Zugriff auf die Navidrome-API, um importierte Titel
auf ihre echte ID aufzulösen und Scans anzustoßen. Dafür gibt es drei Wege, in
dieser Reihenfolge:

1. **Im Dashboard unter *Diagnose*** — Benutzer und Passwort eintragen, fertig.
   Der Zugang wird sofort gegen Navidrome geprüft; ein Tippfehler fällt dort
   auf und nicht erst beim ersten Download.
2. **Automatisch**, sobald sich ein Musik-Client über Port 8080 anmeldet
   (siehe unten). Ein von Hand eingetragener Zugang wird dabei nicht
   überschrieben.
3. **`NAVIDROME_PASSWORD`** in der Umgebung. Setzt die anderen beiden außer
   Kraft.

**Das Passwort wird in keinem Fall gespeichert.** Subsonic authentifiziert über
`md5(passwort + salt)` — dieses Tripel wird einmal erzeugt, und nur es landet
in der Datenbank. Ein Blick hinein gibt das Navidrome-Passwort nicht her.

Für den Scan-Anstoß braucht es einen Administrator; zum Auflösen von Titel-IDs
genügt jeder Navidrome-Benutzer. Ohne Scan-Rechte funktioniert trotzdem alles,
denn `ND_MONITORCHANGES` lässt Navidrome neue Dateien selbst bemerken.

### Wie der Gateway ohne Navidrome-Passwort auskommt

Der Gateway braucht selbst Zugriff auf die Navidrome-API, um importierte Titel
auf ihre echte ID aufzulösen und Scans anzustoßen. Statt dafür ein zweites
Passwort in der Konfiguration zu verlangen, **übernimmt er das Subsonic-Token
des ersten Clients, der sich erfolgreich über Port 8080 anmeldet.**

Das funktioniert, weil Subsonic-Token `md5(passwort + salt)` mit frei gewähltem
Salt sind — ohne Ablauf und ohne Einmalgebrauch. Dasselbe Tripel ist beliebig
oft wiederverwendbar.

Der Preis, offen benannt: das Tripel liegt in der Gateway-Datenbank und ist für
API-Zugriffe so mächtig wie das Passwort selbst. Es ist derselbe Wert, den der
Client ohnehin bei jeder Anfrage überträgt, und die Datenbank liegt neben
`navidrome.db`. Wer das nicht möchte, setzt `NAVIDROME_PASSWORD` — dann wird
nichts geliehen und nichts gespeichert.

Bis sich der erste Client angemeldet hat, zeigt die Startprüfung hier einen
Hinweis. Downloads funktionieren trotzdem: `ND_MONITORCHANGES` indexiert neue
Dateien auch ohne Scan-Anstoß.

### Erster Durchlauf

1. `docker compose up -d`
2. Dashboard öffnen, Passwort aus dem Log, unter **Konto** ändern.
3. **Diagnose** aufrufen. Die Startprüfung darf keine roten Einträge zeigen.
4. Einen Client auf Port 8080 einrichten (Navidrome-Zugangsdaten) und einmal
   irgendetwas suchen — damit hat der Gateway seinen API-Zugang.
5. **Bibliothek → Neu indexieren.** Reiner Lesevorgang.
6. Im Client einen Titel suchen, den es lokal nicht gibt.

---

## Umstieg von einem laufenden Setup

Wer Navidrome und Deemix schon betreibt, ändert an den bestehenden Diensten
**genau eine Zeile**: Deemix lädt nicht mehr direkt in die Bibliothek, sondern
in einen Staging-Ordner.

```yaml
    volumes:
      - ./config/deemix:/config
      - /mnt/tank/music-staging:/downloads    # war: /mnt/tank/music
```

In Deemix selbst ist nichts anzupassen. Der *Download Path* bleibt
`/downloads`, Trackname- und Album-Templates bleiben, wie sie sind — nur das
Volume dahinter zeigt woanders hin. Der Gateway übernimmt die Ordner- und
Dateistruktur, die Deemix daraus erzeugt, unverändert
(`GATEWAY_IMPORT_LAYOUT=preserve`). Neue Titel liegen damit exakt so wie der
bestehende Bestand, der ja von denselben Templates stammt.

Dazu kommen die beiden `gateway-*`-Dienste aus der Compose. Navidrome bleibt
unangetastet.

### Was am bestehenden Bestand verändert wird

| Funktion | Standard | Wirkung auf vorhandene Dateien |
|---|---|---|
| Just-in-Time-Download | aktiv | legt **nur neue** Dateien an |
| Bibliotheks-Index | auf Knopfdruck | nur lesend |
| Duplikatsuche | auf Knopfdruck | nur lesend, erzeugt Vorschläge |
| Duplikate anwenden | **gesperrt** | verschiebt in Quarantäne — erst nach `GATEWAY_ALLOW_DEDUPE_APPLY=true` |
| Tags schreiben | **gesperrt** | überschreibt Tags — erst nach `GATEWAY_ALLOW_TAG_WRITE=true` |

Beide Schalter stehen bewusst auf `false`. Solange sie aus sind, kann der
Gateway keine vorhandene Datei anfassen — auch nicht durch einen Fehlklick.
Erst die Duplikatvorschläge im Dashboard prüfen, dann freischalten.

### Navidrome bleibt, wie es war

Die Compose startet Navidrome weiterhin als `user: "0:0"`. Das ist Absicht:
`./data/navidrome` wurde als root angelegt, und ein Wechsel auf `1000:1000`
würde die bestehende `navidrome.db` unbeschreibbar machen. Wer umstellen will,
macht vorher

```bash
docker compose stop navidrome && chown -R 1000:1000 ./data/navidrome
```

Die Bibliothek ist für Navidrome unverändert read-only eingehängt.

### Songtexte

Bei `ND_LYRICSPRIORITY: .lrc,embedded,.txt` schreibt Deemix `.lrc`-Dateien
neben den Track. Der Import nimmt sie mit — ebenso `cover.jpg` und
`folder.jpg`. Ein vorhandenes Cover in der Bibliothek wird dabei nie
überschrieben.

### Wenn `/music` nicht beschreibbar ist

Der Gateway läuft als UID 1000, genau wie Deemix. Schreibt Deemix heute schon
in die Bibliothek, passt es. Andernfalls meldet der Container es beim Start
und die Startprüfung zeigt es rot an:

```bash
chown -R 1000:1000 /mnt/tank/music
```

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
| `GATEWAY_IMPORT_LAYOUT` | `preserve` (Deemix-Struktur übernehmen) oder `tags` |
| `GATEWAY_ALLOW_DEDUPE_APPLY` | Duplikate in Quarantäne verschieben dürfen |
| `GATEWAY_ALLOW_TAG_WRITE` | Tags vorhandener Dateien überschreiben dürfen |

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

**Aktualisieren.** Im Stack-Verzeichnis:

```bash
docker compose pull && docker compose up -d
```

`docker restart` reicht **nicht** — das startet den bestehenden Container mit
seinem alten Image neu. Nur `up -d` erzeugt ihn mit dem frisch geladenen Image
neu. Danach im Browser einmal hart neu laden (Strg+Shift+R): Dashboard und
Skript liegen sonst noch im Cache.

Das Stack-Verzeichnis verrät der Container selbst:

```bash
docker inspect music-gateway-api --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
```

Ob die neue Fassung wirklich ausgeliefert wird:

```bash
curl -s http://localhost:8080/style.css | head -5
```

**Protokoll.** Die Seite *Protokoll* zeigt alle Ereignisse mit Filter nach
Stufe und Bereich sowie Volltextsuche; bei „mitlaufen" laufen neue Einträge
live ein. Unerwartete Fehler landen dort ebenfalls, nicht nur im
Container-Log — inklusive Pfad und Methode der Anfrage, die sie ausgelöst hat.

**Startprüfung.** *Diagnose* im Dashboard prüft Pfade, Rechte, Trennung von
Staging und Bibliothek, ob Import-Moves atomar sind, und ob Navidrome, Deezer,
Deemix, ffmpeg, ffprobe und fpcalc erreichbar sind. Dieselben Prüfungen
schreibt der API-Container beim Start ins Log:

```bash
docker compose logs gateway-api | grep PRUEFUNG
```

**`Conflict. The container name "/deemix" is already in use`.** Tritt beim
Umstieg auf: die alten Container gehören noch zum vorherigen Compose-Projekt
(dessen Name sich aus dem Verzeichnisnamen ergab), diese Datei setzt
`name: music-server-tool`. Damit ist es für Docker ein anderes Projekt, und
`container_name` ist Docker-weit eindeutig. Alte Container entfernen, dann neu
starten:

```bash
docker rm -f navidrome deemix
```

Gefahrlos: Navidromes Datenbank liegt in `./data/navidrome`, die
Deemix-Konfiguration samt ARL in `./config/deemix` — beides Bind-Mounts auf dem
Host, nichts davon steckt im Container.

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

Suchpfade gegen eine Navidrome-Attrappe im selben Prozess — prüft, was ein
Musik-Client tatsächlich zu sehen bekommt (braucht Internet für den Katalog):

```bash
cd gateway && python tests/proxy_test.py
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
