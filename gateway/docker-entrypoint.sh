#!/bin/sh
# ---------------------------------------------------------------------------
# Der Container richtet sich selbst ein.
#
# Ziel: die Compose laesst sich einsetzen, ohne dass vorher jemand Ordner
# anlegt oder Rechte setzt. Docker erzeugt fehlende Bind-Mount-Quellen als
# root:root - Deemix (UID 1000) koennte darin nicht schreiben. Solange wir
# noch root sind, wird das hier geradegezogen; danach laeuft die Anwendung
# unprivilegiert weiter.
# ---------------------------------------------------------------------------
set -e

ROLE="${1:-${GATEWAY_ROLE:-api}}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

start() {
  case "$ROLE" in
    api)
      exec uvicorn app.main:app \
        --host 0.0.0.0 --port 8080 \
        --no-access-log \
        --proxy-headers \
        --timeout-keep-alive 75
      ;;
    worker)
      exec python -m app.worker
      ;;
    shell)
      exec /bin/sh
      ;;
    *)
      exec "$@"
      ;;
  esac
}

if [ "$(id -u)" != "0" ]; then
  # Wurde in der Compose ein 'user:' gesetzt, koennen wir nichts vorbereiten -
  # dann muss die Umgebung schon passen. Die Startpruefung sagt, ob sie es tut.
  start "$@"
fi

for dir in /data /staging /quarantine; do
  mkdir -p "$dir" 2>/dev/null || true
done

# Nur die Mountpunkte selbst, nicht rekursiv: /staging und /quarantine koennen
# gross werden, und ihre Dateien gehoeren ohnehin schon dem richtigen Benutzer.
chown "$PUID:$PGID" /staging /quarantine 2>/dev/null || true
# Das Datenverzeichnis gehoert uns allein, hier ist rekursiv richtig.
chown -R "$PUID:$PGID" /data 2>/dev/null || true

# /music wird bewusst NICHT angefasst. Die bestehende Bibliothek gehoert dem
# Benutzer, nicht diesem Container - ein chown -R darueber waere genau die
# Art von Nebenwirkung, die niemand erwartet.
if [ -d /music ] && ! gosu "$PUID:$PGID" test -w /music; then
  echo "WARNUNG: /music ist fuer UID $PUID nicht beschreibbar." >&2
  echo "         Der Import kann keine Dateien ablegen. Auf dem Host:" >&2
  echo "         chown -R $PUID:$PGID <Musikverzeichnis>" >&2
fi

exec gosu "$PUID:$PGID" /usr/local/bin/docker-entrypoint.sh "$@"
