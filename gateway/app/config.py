"""Zentrale Konfiguration. Bewusst ohne pydantic-settings: weniger Abhaengig-
keiten, kein Import-Overhead, und alles was hier steht ist ohnehin ein flacher
Satz aus Umgebungsvariablen."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _session_secret() -> str:
    """Aus der Umgebung, sonst dauerhaft neben der Datenbank ablegen.

    Ohne Persistenz bekaeme jeder Neustart ein frisches Geheimnis und wuerde
    alle angemeldeten Browser abmelden. Die Datei liegt im Datenverzeichnis,
    damit API und Worker denselben Wert sehen - und damit der Stack ohne eine
    einzige Pflichtangabe in der .env startet.
    """
    explicit = _env("GATEWAY_SESSION_SECRET")
    if explicit:
        return explicit

    path = Path(_env("DB_PATH", "/data/gateway.db")).parent / "session.secret"
    try:
        if path.exists():
            stored = path.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_hex(32)
        path.write_text(generated, encoding="utf-8")
        os.chmod(path, 0o600)
        return generated
    except OSError:
        # Kein beschreibbares Datenverzeichnis: lieber fluechtig als gar nicht.
        return secrets.token_hex(32)


@dataclass(frozen=True)
class Settings:
    role: str = field(default_factory=lambda: _env("GATEWAY_ROLE", "api"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "info"))

    # --- Pfade ------------------------------------------------------------
    music_dir: Path = field(default_factory=lambda: Path(_env("MUSIC_DIR", "/music")))
    staging_dir: Path = field(default_factory=lambda: Path(_env("STAGING_DIR", "/staging")))
    quarantine_dir: Path = field(default_factory=lambda: Path(_env("QUARANTINE_DIR", "/quarantine")))
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", "/data/gateway.db")))
    cache_dir: Path = field(default_factory=lambda: Path(_env("CACHE_DIR", "/data/cache")))

    # --- Navidrome --------------------------------------------------------
    navidrome_url: str = field(default_factory=lambda: _env("NAVIDROME_URL", "http://navidrome:4533").rstrip("/"))
    navidrome_user: str = field(default_factory=lambda: _env("NAVIDROME_USER", "admin"))
    navidrome_password: str = field(default_factory=lambda: _env("NAVIDROME_PASSWORD"))

    # --- Deemix / Deezer --------------------------------------------------
    deemix_url: str = field(default_factory=lambda: _env("DEEMIX_URL", "http://deemix:6595").rstrip("/"))
    deemix_bitrate: str = field(default_factory=lambda: _env("DEEMIX_BITRATE", "3"))
    deezer_api_url: str = "https://api.deezer.com"

    # --- Auth / Web -------------------------------------------------------
    admin_user: str = field(default_factory=lambda: _env("GATEWAY_ADMIN_USER", "admin"))
    admin_password: str = field(default_factory=lambda: _env("GATEWAY_ADMIN_PASSWORD"))
    session_secret: str = field(default_factory=_session_secret)
    secure_cookies: bool = field(default_factory=lambda: _env_bool("GATEWAY_SECURE_COOKIES", False))
    session_ttl_hours: int = field(default_factory=lambda: _env_int("GATEWAY_SESSION_TTL_HOURS", 24 * 14))

    # --- Proxy-Verhalten --------------------------------------------------
    # defer  = Hinweis-Ton ausliefern, Download laeuft im Hintergrund
    # stream = Antwort offen halten und die Datei live mitstreamen
    stream_mode: str = field(default_factory=lambda: _env("GATEWAY_STREAM_MODE", "defer").lower())
    marker_suffix: str = field(
        default_factory=lambda: os.environ.get("GATEWAY_MARKER_SUFFIX", " [Nicht heruntergeladen]")
    )
    provider_result_limit: int = field(default_factory=lambda: _env_int("GATEWAY_PROVIDER_LIMIT", 15))
    provider_search_enabled: bool = field(default_factory=lambda: _env_bool("GATEWAY_PROVIDER_SEARCH", True))
    search_cache_ttl: int = field(default_factory=lambda: _env_int("GATEWAY_SEARCH_CACHE_TTL", 300))
    auth_cache_ttl: int = field(default_factory=lambda: _env_int("GATEWAY_AUTH_CACHE_TTL", 60))

    # --- Import -----------------------------------------------------------
    # preserve = die Ordner-/Dateistruktur uebernehmen, die Deemix anhand der
    #            eigenen Templates erzeugt hat. Standard, weil damit die neuen
    #            Dateien exakt so liegen wie der bestehende Bestand.
    # tags     = Zielpfad selbst aus den Tags ableiten (Interpret/Album/NN - Titel)
    import_layout: str = field(default_factory=lambda: _env("GATEWAY_IMPORT_LAYOUT", "preserve").lower())
    # Begleitdateien, die Deemix neben den Track legt (Lyrics, Cover).
    sidecar_extensions: tuple[str, ...] = (
        ".lrc", ".txt", ".jpg", ".jpeg", ".png", ".webp", ".nfo",
    )

    # --- Schutzschalter fuer den Bestand ----------------------------------
    # Beide standardmaessig AUS. Der Import legt nur neue Dateien an; alles,
    # was vorhandene Dateien anfasst, muss ausdruecklich freigeschaltet werden.
    allow_dedupe_apply: bool = field(default_factory=lambda: _env_bool("GATEWAY_ALLOW_DEDUPE_APPLY", False))
    allow_tag_write: bool = field(default_factory=lambda: _env_bool("GATEWAY_ALLOW_TAG_WRITE", False))

    # --- Worker -----------------------------------------------------------
    worker_concurrency: int = field(default_factory=lambda: _env_int("GATEWAY_WORKER_CONCURRENCY", 2))
    download_timeout: int = field(default_factory=lambda: _env_int("GATEWAY_DOWNLOAD_TIMEOUT", 600))
    resolve_timeout: int = field(default_factory=lambda: _env_int("GATEWAY_RESOLVE_TIMEOUT", 240))

    # --- Scanner ----------------------------------------------------------
    audio_extensions: tuple[str, ...] = (
        ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus",
        ".wma", ".wav", ".aiff", ".ape", ".wv", ".mpc",
    )
    hash_chunk_size: int = 1024 * 1024
    ffmpeg_bin: str = field(default_factory=lambda: _env("FFMPEG_BIN", "ffmpeg"))
    ffprobe_bin: str = field(default_factory=lambda: _env("FFPROBE_BIN", "ffprobe"))
    fpcalc_bin: str = field(default_factory=lambda: _env("FPCALC_BIN", "fpcalc"))
    subprocess_slots: int = field(default_factory=lambda: _env_int("GATEWAY_SUBPROCESS_SLOTS", max(2, (os.cpu_count() or 2))))


settings = Settings()


def ensure_dirs() -> None:
    for path in (settings.db_path.parent, settings.cache_dir, settings.cache_dir / "covers"):
        path.mkdir(parents=True, exist_ok=True)
