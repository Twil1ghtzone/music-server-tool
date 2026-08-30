"""Schlanker SQLite-Layer.

Warum kein ORM: der Hot-Path dieses Dienstes ist ein HTTP-Proxy. Jede
Millisekunde Mapping-Lookup zaehlt, und die Abfragen sind trivial. aiosqlite +
handgeschriebenes SQL ist hier schneller und besser vorhersagbar.

Nebenlaeufigkeit: SQLite erlaubt genau einen Schreiber. Deshalb eine dedizierte
Write-Verbindung hinter einem Lock und ein kleiner Pool aus Read-Verbindungen
(WAL erlaubt Lesen waehrend geschrieben wird).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite

from .logging_conf import get_logger

log = get_logger("db")

READ_POOL_SIZE = 4

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=10000",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-32000",     # ~32 MB Page-Cache
    "PRAGMA mmap_size=268435456",   # 256 MB memory-mapped I/O
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

-- ---------------------------------------------------------------- Accounts
CREATE TABLE IF NOT EXISTS app_user (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    totp_secret   TEXT,
    totp_enabled  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS app_session (
    id         TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    ip         TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS ix_session_user ON app_session(user_id);
CREATE INDEX IF NOT EXISTS ix_session_expires ON app_session(expires_at);

CREATE TABLE IF NOT EXISTS login_attempt (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL DEFAULT (datetime('now')),
    ip       TEXT NOT NULL,
    username TEXT,
    success  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_attempt_ip_ts ON login_attempt(ip, ts);
CREATE INDEX IF NOT EXISTS ix_attempt_user_ts ON login_attempt(username, ts);

-- ------------------------------------------------- Virtuelle Tracks (Proxy)
-- Zeilen hier werden NIE geloescht: Clients cachen die virtuelle ID in
-- Playlists und Warteschlangen, das Mapping muss dauerhaft aufloesbar bleiben.
CREATE TABLE IF NOT EXISTS virtual_track (
    id            TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,
    provider_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    artist        TEXT NOT NULL DEFAULT '',
    album         TEXT NOT NULL DEFAULT '',
    album_artist  TEXT NOT NULL DEFAULT '',
    duration      INTEGER NOT NULL DEFAULT 0,
    track_no      INTEGER,
    disc_no       INTEGER,
    year          INTEGER,
    isrc          TEXT,
    cover_url     TEXT,
    source_url    TEXT,
    navidrome_id  TEXT,
    local_path    TEXT,
    state         TEXT NOT NULL DEFAULT 'virtual',
    error         TEXT,
    play_requests INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(provider, provider_id)
);
CREATE INDEX IF NOT EXISTS ix_virtual_state ON virtual_track(state);
CREATE INDEX IF NOT EXISTS ix_virtual_nd ON virtual_track(navidrome_id);
CREATE INDEX IF NOT EXISTS ix_virtual_updated ON virtual_track(updated_at DESC);

-- --------------------------------------------------------------- Job-Queue
CREATE TABLE IF NOT EXISTS job (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    type         TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    state        TEXT NOT NULL DEFAULT 'pending',
    priority     INTEGER NOT NULL DEFAULT 100,
    progress     REAL NOT NULL DEFAULT 0,
    detail       TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error   TEXT,
    dedupe_key   TEXT UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_job_claim ON job(state, priority, id);
CREATE INDEX IF NOT EXISTS ix_job_updated ON job(updated_at DESC);

-- ------------------------------------------------------- Bibliotheks-Index
CREATE TABLE IF NOT EXISTS media_file (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL UNIQUE,
    size         INTEGER NOT NULL DEFAULT 0,
    mtime        REAL NOT NULL DEFAULT 0,
    ext          TEXT,
    file_hash    TEXT,
    audio_hash   TEXT,
    duration     REAL,
    bitrate      INTEGER,
    sample_rate  INTEGER,
    channels     INTEGER,
    codec        TEXT,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    album_artist TEXT,
    track_no     INTEGER,
    disc_no      INTEGER,
    year         INTEGER,
    has_cover    INTEGER NOT NULL DEFAULT 0,
    tag_issues   TEXT,
    missing      INTEGER NOT NULL DEFAULT 0,
    seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
    hashed_at    TEXT,
    probed_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_media_file_hash ON media_file(file_hash);
CREATE INDEX IF NOT EXISTS ix_media_audio_hash ON media_file(audio_hash);
CREATE INDEX IF NOT EXISTS ix_media_size ON media_file(size);
CREATE INDEX IF NOT EXISTS ix_media_missing ON media_file(missing);
CREATE INDEX IF NOT EXISTS ix_media_artist_title ON media_file(artist, title);

CREATE TABLE IF NOT EXISTS fingerprint (
    media_file_id INTEGER PRIMARY KEY REFERENCES media_file(id) ON DELETE CASCADE,
    duration      REAL NOT NULL DEFAULT 0,
    raw_fp        BLOB NOT NULL,
    bucket        INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_fingerprint_bucket ON fingerprint(bucket);

-- ------------------------------------------------------- Duplikat-Ergebnis
CREATE TABLE IF NOT EXISTS dupe_group (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    signature  TEXT NOT NULL,
    keeper_id  INTEGER REFERENCES media_file(id) ON DELETE SET NULL,
    files      INTEGER NOT NULL DEFAULT 0,
    wasted     INTEGER NOT NULL DEFAULT 0,
    state      TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(kind, signature)
);
CREATE INDEX IF NOT EXISTS ix_dupe_state ON dupe_group(state, wasted DESC);

CREATE TABLE IF NOT EXISTS dupe_member (
    group_id      INTEGER NOT NULL REFERENCES dupe_group(id) ON DELETE CASCADE,
    media_file_id INTEGER NOT NULL REFERENCES media_file(id) ON DELETE CASCADE,
    score         REAL NOT NULL DEFAULT 0,
    similarity    REAL,
    PRIMARY KEY (group_id, media_file_id)
);

-- ------------------------------------------------------------ Betrieb/Logs
CREATE TABLE IF NOT EXISTS event_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL DEFAULT (datetime('now')),
    level    TEXT NOT NULL DEFAULT 'info',
    category TEXT NOT NULL DEFAULT 'general',
    message  TEXT NOT NULL,
    data     TEXT
);
CREATE INDEX IF NOT EXISTS ix_event_ts ON event_log(id DESC);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

CURRENT_VERSION = 1


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._write: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._readers: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._all: list[aiosqlite.Connection] = []

    # -- Lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._write = await self._open()
        await self._migrate(self._write)
        for _ in range(READ_POOL_SIZE):
            await self._readers.put(await self._open(readonly=True))
        log.info("SQLite bereit: %s (WAL, %d Leser)", self._path, READ_POOL_SIZE)

    async def _open(self, readonly: bool = False) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        for pragma in _PRAGMAS:
            if readonly and pragma.startswith("PRAGMA journal_mode"):
                continue
            await conn.execute(pragma)
        self._all.append(conn)
        return conn

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        await conn.executescript(SCHEMA)
        cur = await conn.execute("SELECT version FROM schema_version LIMIT 1")
        row = await cur.fetchone()
        if row is None:
            await conn.execute("INSERT INTO schema_version(version) VALUES (?)", (CURRENT_VERSION,))
        elif row["version"] < CURRENT_VERSION:
            # Platz fuer zukuenftige Migrationsschritte.
            await conn.execute("UPDATE schema_version SET version = ?", (CURRENT_VERSION,))

    async def close(self) -> None:
        for conn in self._all:
            try:
                await conn.close()
            except Exception:  # pragma: no cover - best effort beim Shutdown
                pass
        self._all.clear()

    # -- Verbindungen ------------------------------------------------------
    @asynccontextmanager
    async def reader(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self._readers.get()
        try:
            yield conn
        finally:
            self._readers.put_nowait(conn)

    @asynccontextmanager
    async def writer(self) -> AsyncIterator[aiosqlite.Connection]:
        assert self._write is not None, "Database.connect() wurde nicht aufgerufen"
        async with self._write_lock:
            yield self._write

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self.writer() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                await conn.execute("ROLLBACK")
                raise
            else:
                await conn.execute("COMMIT")

    # -- Bequeme Helfer ----------------------------------------------------
    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        async with self.reader() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        async with self.reader() as conn:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fetch_value(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetch_one(sql, params)
        if not row:
            return default
        return next(iter(row.values()), default)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self.writer() as conn:
            cur = await conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        async with self.writer() as conn:
            await conn.executemany(sql, rows)

    # -- Settings ----------------------------------------------------------
    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = await self.fetch_one("SELECT value FROM setting WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO setting(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


db = Database(Path("/data/gateway.db"))


def configure(path: Path) -> Database:
    """Wird beim Startup aufgerufen, bevor connect() laeuft."""
    # Gleiche Instanz behalten, damit "from .db import db" stabil bleibt.
    db.__init__(path)
    return db
