"""SQLite storage: cached catalog, favorites, watch history, downloads.

Everything the UI reads comes from here, never from the provider directly.
That is what makes the app open instantly and keep working offline.
"""
import sqlite3
import threading
import time
from typing import Any, Iterable

import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    kind TEXT, id TEXT, name TEXT,
    PRIMARY KEY (kind, id)
);

CREATE TABLE IF NOT EXISTS live (
    stream_id INTEGER PRIMARY KEY,
    name TEXT, category_id TEXT, icon TEXT,
    epg_channel_id TEXT, tv_archive INTEGER, num INTEGER
);
CREATE INDEX IF NOT EXISTS live_cat ON live(category_id);

CREATE TABLE IF NOT EXISTS vod (
    stream_id INTEGER PRIMARY KEY,
    name TEXT, category_id TEXT, icon TEXT,
    ext TEXT, rating REAL, added INTEGER, num INTEGER
);
CREATE INDEX IF NOT EXISTS vod_cat ON vod(category_id);

CREATE TABLE IF NOT EXISTS series (
    series_id INTEGER PRIMARY KEY,
    name TEXT, category_id TEXT, cover TEXT, plot TEXT, genre TEXT,
    rating REAL, release_date TEXT, last_modified INTEGER,
    episodes_synced_at INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS series_cat ON series(category_id);

CREATE TABLE IF NOT EXISTS episodes (
    episode_id INTEGER PRIMARY KEY,
    series_id INTEGER, season INTEGER, ep_num INTEGER,
    title TEXT, ext TEXT, duration_secs INTEGER, added INTEGER
);
CREATE INDEX IF NOT EXISTS ep_series ON episodes(series_id, season, ep_num);

CREATE TABLE IF NOT EXISTS favorites (
    kind TEXT, item_id INTEGER, added_at INTEGER,
    PRIMARY KEY (kind, item_id)
);

-- One row per watched item. For episodes, series_id lets us roll history up
-- to the show so "continue watching" can answer at the series level.
CREATE TABLE IF NOT EXISTS history (
    kind TEXT, item_id INTEGER, series_id INTEGER,
    position_sec REAL DEFAULT 0, duration_sec REAL DEFAULT 0,
    completed INTEGER DEFAULT 0, watched_at INTEGER,
    PRIMARY KEY (kind, item_id)
);
CREATE INDEX IF NOT EXISTS hist_recent ON history(watched_at DESC);
CREATE INDEX IF NOT EXISTS hist_series ON history(series_id, watched_at DESC);

CREATE TABLE IF NOT EXISTS downloads (
    kind TEXT, item_id INTEGER, series_id INTEGER,
    title TEXT, url TEXT, local_path TEXT,
    bytes_done INTEGER DEFAULT 0, total_bytes INTEGER DEFAULT 0,
    status TEXT DEFAULT 'queued', error TEXT, added_at INTEGER,
    PRIMARY KEY (kind, item_id)
);
CREATE INDEX IF NOT EXISTS dl_status ON downloads(status);

-- unicode61 with diacritic folding so Arabic and Latin titles both search
-- sensibly regardless of how they were typed.
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    name,
    kind    UNINDEXED,
    item_id UNINDEXED,
    tokenize="unicode61 remove_diacritics 2"
);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def query(sql: str, params: Iterable = ()) -> list[dict]:
    with _lock:
        rows = connect().execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def one(sql: str, params: Iterable = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable = ()) -> None:
    with _lock:
        c = connect()
        c.execute(sql, tuple(params))
        c.commit()


def executemany(sql: str, seq: Iterable[Iterable]) -> None:
    with _lock:
        c = connect()
        c.executemany(sql, seq)
        c.commit()


def get_meta(key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else default


def set_meta(key: str, value: Any) -> None:
    execute("INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))


def now() -> int:
    return int(time.time())
