from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    directory_name TEXT NOT NULL DEFAULT '',
    artists TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    cover_path TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_hours INTEGER NOT NULL DEFAULT 24 CHECK (interval_hours IN (24, 168)),
    last_checked_at TEXT,
    next_check_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seasons (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    selected INTEGER NOT NULL DEFAULT 0,
    episode_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (album_id, number)
);

CREATE TABLE IF NOT EXISTS episodes (
    track_id TEXT PRIMARY KEY,
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    season_number INTEGER NOT NULL,
    episode_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    artists TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER,
    available INTEGER NOT NULL DEFAULT 1,
    discovered_at TEXT NOT NULL,
    published_at TEXT NOT NULL,
    source_published_at TEXT,
    local_path TEXT,
    media_size INTEGER,
    downloaded_at TEXT,
    download_error TEXT,
    UNIQUE (album_id, season_number, episode_number, track_id)
);

CREATE INDEX IF NOT EXISTS episodes_album_order
ON episodes(album_id, season_number DESC, episode_number DESC);
CREATE INDEX IF NOT EXISTS albums_due
ON albums(enabled, next_check_at);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            episode_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(episodes)")
            }
            if "source_published_at" not in episode_columns:
                connection.execute(
                    "ALTER TABLE episodes ADD COLUMN source_published_at TEXT"
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
