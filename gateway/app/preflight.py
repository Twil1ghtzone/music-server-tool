"""Startprüfung: passt die Konfiguration zum tatsächlichen System?

Laeuft beim Start der API (ins Log) und auf Abruf im Dashboard unter Diagnose.
Der Zweck ist, Fehler zu finden, die sonst erst beim ersten echten Download
auffallen - und zwar dann, wenn schon eine Datei unterwegs ist.

Drei Stufen:
  fail  Muss behoben werden, sonst geht etwas kaputt oder funktioniert nicht.
  warn  Laeuft, aber nicht so gut wie es koennte.
  ok    Passt.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path
from typing import Any

from .clients import deemix, deezer, navidrome
from .config import settings
from .logging_conf import get_logger
from .services import ffmpeg

log = get_logger("preflight")


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".mst-write-test-{secrets.token_hex(4)}"
        probe.write_bytes(b"x")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _same_filesystem(a: Path, b: Path) -> bool | None:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return None


# ------------------------------------------------------------------- Pfade
def path_checks() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    music, staging, quarantine = (
        settings.music_dir,
        settings.staging_dir,
        settings.quarantine_dir,
    )

    if not music.exists():
        out.append(_check("Musikverzeichnis", "fail", f"{music} existiert im Container nicht"))
    else:
        writable, error = _writable(music)
        out.append(
            _check("Musikverzeichnis", "ok" if writable else "fail",
                   str(music) if writable
                   else f"{music} ist nicht beschreibbar ({error}). Der Import kann keine "
                        f"Dateien ablegen - meist ein Rechteproblem mit PUID/PGID.")
        )

    writable, error = _writable(staging)
    out.append(
        _check("Staging-Verzeichnis", "ok" if writable else "fail",
               str(staging) if writable else f"{staging} nicht beschreibbar ({error})")
    )

    # Der wichtigste Test ueberhaupt: liegt Staging in der Bibliothek, sieht
    # Navidrome jede halbfertige Datei und nimmt sie in den Index auf.
    if staging.resolve() == music.resolve():
        out.append(
            _check("Staging getrennt", "fail",
                   "STAGING_DIR und MUSIC_DIR zeigen auf dasselbe Verzeichnis. "
                   "Navidrome wuerde unfertige Downloads indexieren.")
        )
    elif _is_inside(staging, music):
        out.append(
            _check("Staging getrennt", "fail",
                   f"{staging} liegt innerhalb von {music}. Navidrome scannt dort mit "
                   f"und nimmt halbfertige Dateien auf.")
        )
    else:
        out.append(_check("Staging getrennt", "ok", "ausserhalb der Bibliothek"))

    if _is_inside(quarantine, music):
        out.append(
            _check("Quarantaene getrennt", "fail",
                   f"{quarantine} liegt innerhalb von {music}. Aussortierte Dateien "
                   f"blieben damit im Navidrome-Index.")
        )
    else:
        writable, error = _writable(quarantine)
        out.append(
            _check("Quarantaene getrennt", "ok" if writable else "warn",
                   str(quarantine) if writable else f"nicht beschreibbar ({error})")
        )

    same = _same_filesystem(staging, music)
    if same is True:
        out.append(
            _check("Import ist atomar", "ok",
                   "Staging und Bibliothek liegen auf demselben Dateisystem, "
                   "os.replace greift")
        )
    elif same is False:
        out.append(
            _check("Import ist atomar", "warn",
                   "Staging und Bibliothek liegen auf verschiedenen Dateisystemen. "
                   "Der Import kopiert ueber eine .part-Datei - korrekt, aber langsamer.")
        )

    writable, error = _writable(settings.db_path.parent)
    out.append(
        _check("Datenverzeichnis", "ok" if writable else "fail",
               str(settings.db_path) if writable else f"nicht beschreibbar ({error})")
    )
    return out


# --------------------------------------------------------------- Einstellungen
def config_checks() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    if settings.stream_mode not in ("defer", "stream"):
        out.append(
            _check("Stream-Modus", "fail",
                   f"'{settings.stream_mode}' ist unbekannt, erlaubt sind defer und stream")
        )
    else:
        out.append(_check("Stream-Modus", "ok", settings.stream_mode))

    if settings.import_layout not in ("preserve", "tags"):
        out.append(
            _check("Import-Layout", "fail",
                   f"'{settings.import_layout}' ist unbekannt, erlaubt sind preserve und tags")
        )
    elif settings.import_layout == "preserve":
        out.append(
            _check("Import-Layout", "ok",
                   "preserve - die von Deemix erzeugte Struktur wird uebernommen")
        )
    else:
        out.append(
            _check("Import-Layout", "warn",
                   "tags - neue Dateien landen unter Interpret/Album/NN - Titel und "
                   "koennen damit anders liegen als der bestehende Bestand")
        )

    if os.environ.get("GATEWAY_SESSION_SECRET", "").strip():
        out.append(_check("Session-Secret", "ok", "aus der Umgebung"))
    elif (settings.db_path.parent / "session.secret").exists():
        out.append(
            _check("Session-Secret", "ok",
                   "automatisch erzeugt und unter /data abgelegt - Neustarts "
                   "melden niemanden ab")
        )
    else:
        out.append(
            _check("Session-Secret", "warn",
                   "fluechtig - das Datenverzeichnis ist nicht beschreibbar, "
                   "jeder Neustart meldet alle Sitzungen ab")
        )

    if settings.navidrome_password:
        out.append(_check("Navidrome-Zugang", "ok", f"eigener Account '{settings.navidrome_user}'"))
    elif navidrome.has_credentials():
        out.append(
            _check("Navidrome-Zugang", "ok",
                   "Zugangsdaten von einem angemeldeten Client uebernommen")
        )
    else:
        out.append(
            _check("Navidrome-Zugang", "warn",
                   "noch keine. Sobald sich ein Client ueber Port 8080 anmeldet, "
                   "uebernimmt der Gateway dessen Token fuer Scan und "
                   "ID-Aufloesung. Alternativ NAVIDROME_PASSWORD setzen.")
        )

    out.append(
        _check("Bereinigung freigeschaltet",
               "ok" if not settings.allow_dedupe_apply else "warn",
               "gesperrt - Duplikate werden nur angezeigt" if not settings.allow_dedupe_apply
               else "aktiv - Auswahl kann in die Quarantaene verschoben werden")
    )
    out.append(
        _check("Tag-Schreiben freigeschaltet",
               "ok" if not settings.allow_tag_write else "warn",
               "gesperrt - vorhandene Dateien werden nicht veraendert"
               if not settings.allow_tag_write else "aktiv - Tags koennen ueberschrieben werden")
    )
    return out


# ------------------------------------------------------------------ Dienste
async def service_checks() -> list[dict[str, str]]:
    """Alle vier Dienste parallel abfragen.

    Sequenziell summieren sich die Verbindungs-Timeouts nicht erreichbarer
    Dienste - und genau dann, wenn etwas kaputt ist, wartet man am laengsten
    auf die Seite, die einem sagen soll was kaputt ist.
    """
    out: list[dict[str, str]] = []

    info, catalog_ok, probe, tools = await asyncio.gather(
        navidrome.server_info(),
        deezer.healthy(),
        deemix.probe(),
        ffmpeg.available(),
    )

    if info.get("online"):
        out.append(
            _check("Navidrome", "ok",
                   f"{info.get('type', '?')} {info.get('serverVersion', '')}".strip())
        )
        # Erreichbar heisst noch nicht: Zugangsdaten stimmen.
        if not navidrome.has_credentials():
            out.append(
                _check("Navidrome-Anmeldung", "warn",
                       "noch keine Zugangsdaten - kommt mit der ersten "
                       "Client-Anmeldung ueber Port 8080")
            )
        else:
            try:
                await navidrome.call("getScanStatus")
                out.append(_check("Navidrome-Anmeldung", "ok", settings.navidrome_user))
            except Exception as exc:
                out.append(
                    _check("Navidrome-Anmeldung", "fail",
                           f"Zugangsdaten werden abgelehnt: {exc}")
                )
    else:
        out.append(_check("Navidrome", "fail", str(info.get("error", "nicht erreichbar"))))

    out.append(
        _check("Deezer-Katalog", "ok" if catalog_ok else "fail",
               "api.deezer.com erreichbar" if catalog_ok
               else "api.deezer.com nicht erreichbar - die Suche liefert nur lokale Treffer")
    )

    if probe.get("reachable"):
        transport = probe.get("known_transport")
        out.append(
            _check("Deemix", "ok" if transport else "warn",
                   " ".join(transport) if transport
                   else "erreichbar, Transport noch unbekannt - wird beim ersten "
                        "Download ermittelt")
        )
    else:
        out.append(_check("Deemix", "fail", "nicht erreichbar - Downloads schlagen fehl"))

    for name, needed_for in (
        ("ffmpeg", "Hinweiston und Audio-Hash"),
        ("ffprobe", "technische Metadaten"),
        ("fpcalc", "akustische Fingerprints"),
    ):
        out.append(
            _check(name, "ok" if tools.get(name) else "fail",
                   "vorhanden" if tools.get(name) else f"fehlt - {needed_for} faellt aus")
        )
    return out


# ------------------------------------------------------------------- Gesamt
async def run(include_services: bool = True) -> dict[str, Any]:
    checks = path_checks() + config_checks()
    if include_services:
        checks += await service_checks()

    counts = {"ok": 0, "warn": 0, "fail": 0}
    for item in checks:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    return {
        "ready": counts["fail"] == 0,
        "counts": counts,
        "checks": checks,
    }


async def log_summary() -> None:
    """Beim Start ins Log schreiben - der erste Ort, an dem jemand nachsieht."""
    try:
        result = await run(include_services=False)
    except Exception as exc:  # pragma: no cover
        log.warning("Startpruefung fehlgeschlagen: %s", exc)
        return

    for item in result["checks"]:
        if item["status"] == "fail":
            log.error("PRUEFUNG %s: %s", item["name"], item["detail"])
        elif item["status"] == "warn":
            log.warning("PRUEFUNG %s: %s", item["name"], item["detail"])
        else:
            log.info("PRUEFUNG %s: %s", item["name"], item["detail"])

    if not result["ready"]:
        log.error(
            "%d Pruefung(en) fehlgeschlagen - siehe Dashboard unter Diagnose.",
            result["counts"]["fail"],
        )
