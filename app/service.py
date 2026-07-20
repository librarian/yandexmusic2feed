from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Settings
from .db import Database
from .yandex import AlbumMetadata, YandexGateway

LOGGER = logging.getLogger(__name__)
SAFE_COMPONENT = re.compile(r"[^\w .()\[\]-]+", re.UNICODE)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def source_publication_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        LOGGER.warning("Ignoring invalid source publication date %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return iso(parsed.astimezone(UTC))


def safe_component(value: str, limit: int = 120) -> str:
    cleaned = SAFE_COMPONENT.sub("_", value).strip(" ._")
    return (cleaned or "Untitled")[:limit].rstrip(" .")


class PodcastService:
    def __init__(self, settings: Settings, database: Database, token_provider=None):
        self.settings = settings
        self.database = database
        self.gateway = YandexGateway(
            token_provider or settings.token, settings.download_bitrate_kbps
        )
        self.sync_lock = threading.Lock()

    def list_albums(self):
        with self.database.connect() as db:
            return db.execute(
                """SELECT a.*,
                (SELECT COUNT(*) FROM seasons s WHERE s.album_id=a.id) season_count,
                (SELECT COUNT(*) FROM episodes e WHERE e.album_id=a.id) episode_count,
                (SELECT COUNT(*) FROM episodes e WHERE e.album_id=a.id AND e.local_path IS NOT NULL) downloaded_count
                FROM albums a ORDER BY lower(a.title), a.id"""
            ).fetchall()

    def get_album(self, album_id: int):
        with self.database.connect() as db:
            album = db.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()
            if not album:
                return None, [], []
            seasons = db.execute(
                "SELECT * FROM seasons WHERE album_id=? ORDER BY number DESC", (album_id,)
            ).fetchall()
            episodes = db.execute(
                """SELECT * FROM episodes WHERE album_id=?
                ORDER BY season_number DESC, episode_number DESC""",
                (album_id,),
            ).fetchall()
            return album, seasons, episodes

    def add_album(self, album_id: int, interval_hours: int = 24) -> None:
        self.register_album(album_id, interval_hours)
        self.sync_album(album_id, download_selected=True)

    def register_album(self, album_id: int, interval_hours: int = 24) -> None:
        if interval_hours not in (24, 168):
            raise ValueError("Interval must be daily or weekly")
        now = iso(utcnow())
        with self.database.connect() as db:
            db.execute(
                """INSERT INTO albums(id, interval_hours, created_at, updated_at, next_check_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET interval_hours=excluded.interval_hours,
                enabled=1, updated_at=excluded.updated_at""",
                (album_id, interval_hours, now, now, now),
            )
        LOGGER.info(
            "Registered album %s with a %s-hour check interval",
            album_id,
            interval_hours,
        )

    def update_album(
        self, album_id: int, interval_hours: int, enabled: bool, selected_seasons: list[int]
    ) -> None:
        if interval_hours not in (24, 168):
            raise ValueError("Interval must be daily or weekly")
        with self.database.connect() as db:
            db.execute(
                "UPDATE albums SET interval_hours=?, enabled=?, updated_at=? WHERE id=?",
                (interval_hours, int(enabled), iso(utcnow()), album_id),
            )
            db.execute("UPDATE seasons SET selected=0 WHERE album_id=?", (album_id,))
            if selected_seasons:
                marks = ",".join("?" for _ in selected_seasons)
                db.execute(
                    f"UPDATE seasons SET selected=1 WHERE album_id=? AND number IN ({marks})",
                    (album_id, *selected_seasons),
                )
        LOGGER.info(
            "Updated album %s: enabled=%s interval_hours=%s selected_seasons=%s",
            album_id,
            enabled,
            interval_hours,
            sorted(selected_seasons),
        )

    def sync_due(self) -> None:
        now = iso(utcnow())
        with self.database.connect() as db:
            due = db.execute(
                """SELECT id FROM albums WHERE enabled=1
                AND (next_check_at IS NULL OR next_check_at <= ?) ORDER BY id""",
                (now,),
            ).fetchall()
        if due:
            LOGGER.info("Scheduler found %s due album(s): %s", len(due), [r["id"] for r in due])
        for row in due:
            try:
                self.sync_album(row["id"], download_selected=True)
            except Exception:
                LOGGER.exception("Scheduled sync failed for album %s", row["id"])

    def sync_album(self, album_id: int, download_selected: bool = True) -> None:
        with self.sync_lock:
            started = time.monotonic()
            LOGGER.info(
                "Sync started for album %s (download_selected=%s)",
                album_id,
                download_selected,
            )
            try:
                metadata = self.gateway.album(album_id)
                self._store_metadata(metadata)
                if download_selected:
                    self.download_selected(album_id, lock=False)
                self._mark_checked(album_id, None)
                LOGGER.info(
                    "Sync finished for album %s in %.1fs",
                    album_id,
                    time.monotonic() - started,
                )
            except Exception as error:
                self._mark_checked(album_id, str(error))
                LOGGER.exception(
                    "Sync failed for album %s after %.1fs",
                    album_id,
                    time.monotonic() - started,
                )
                raise

    def _store_metadata(self, metadata: AlbumMetadata) -> None:
        now = utcnow()
        now_text = iso(now)
        seasons = sorted({episode.season for episode in metadata.episodes}) or [1]
        LOGGER.info(
            "Discovered album %s %r: %s episode(s) across season(s) %s",
            metadata.album_id,
            metadata.title,
            len(metadata.episodes),
            seasons,
        )
        with self.database.connect() as db:
            album = db.execute("SELECT * FROM albums WHERE id=?", (metadata.album_id,)).fetchone()
            directory = album["directory_name"] if album and album["directory_name"] else ""
            if not directory:
                directory = safe_component(f"{metadata.album_id} - {metadata.title}")
            cover_path = album["cover_path"] if album else None
            db.execute(
                """UPDATE albums SET title=?, directory_name=?, artists=?, description=?,
                cover_path=?, updated_at=? WHERE id=?""",
                (
                    metadata.title,
                    directory,
                    metadata.artists,
                    metadata.description,
                    cover_path,
                    now_text,
                    metadata.album_id,
                ),
            )
            existing_seasons = {
                row["number"]
                for row in db.execute(
                    "SELECT number FROM seasons WHERE album_id=?", (metadata.album_id,)
                )
            }
            newest = max(seasons)
            for number in seasons:
                count = sum(e.season == number for e in metadata.episodes)
                db.execute(
                    """INSERT INTO seasons(album_id, number, selected, episode_count)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(album_id, number) DO UPDATE SET episode_count=excluded.episode_count""",
                    (metadata.album_id, number, int(number == newest), count),
                )
            for rank, episode in enumerate(
                sorted(metadata.episodes, key=lambda e: (e.season, e.episode)), 1
            ):
                published = iso(now - timedelta(minutes=len(metadata.episodes) - rank))
                source_published = source_publication_time(episode.source_published_at)
                db.execute(
                    """INSERT INTO episodes(
                    track_id, album_id, season_number, episode_number, title, artists,
                    duration_ms, available, discovered_at, published_at, source_published_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(track_id) DO UPDATE SET
                    album_id=excluded.album_id, season_number=excluded.season_number,
                    episode_number=excluded.episode_number, title=excluded.title,
                    artists=excluded.artists, duration_ms=excluded.duration_ms,
                    available=excluded.available,
                    source_published_at=COALESCE(
                        excluded.source_published_at, episodes.source_published_at
                    )""",
                    (
                        episode.track_id,
                        metadata.album_id,
                        episode.season,
                        episode.episode,
                        episode.title,
                        episode.artists,
                        episode.duration_ms,
                        int(episode.available),
                        now_text,
                        published,
                        source_published,
                    ),
                )
        if metadata.cover_url:
            cover = self.settings.covers_dir / f"{metadata.album_id}.jpg"
            try:
                self.gateway.download_cover(metadata.cover_url, cover)
                with self.database.connect() as db:
                    db.execute(
                        "UPDATE albums SET cover_path=? WHERE id=?",
                        (str(cover), metadata.album_id),
                    )
            except Exception:
                LOGGER.warning("Could not download cover for %s", metadata.album_id, exc_info=True)

    def _mark_checked(self, album_id: int, error: str | None) -> None:
        now = utcnow()
        with self.database.connect() as db:
            album = db.execute(
                "SELECT interval_hours FROM albums WHERE id=?", (album_id,)
            ).fetchone()
            if not album:
                return
            db.execute(
                """UPDATE albums SET last_checked_at=?, next_check_at=?, last_error=?, updated_at=?
                WHERE id=?""",
                (
                    iso(now),
                    iso(now + timedelta(hours=album["interval_hours"])),
                    error,
                    iso(now),
                    album_id,
                ),
            )

    def download_selected(self, album_id: int, lock: bool = True) -> None:
        if lock:
            with self.sync_lock:
                return self.download_selected(album_id, lock=False)
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT e.* FROM episodes e JOIN seasons s
                ON s.album_id=e.album_id AND s.number=e.season_number
                WHERE e.album_id=? AND s.selected=1 AND e.local_path IS NULL
                AND e.available=1 ORDER BY e.season_number, e.episode_number""",
                (album_id,),
            ).fetchall()
        LOGGER.info(
            "Album %s has %s pending episode(s) in selected seasons",
            album_id,
            len(rows),
        )
        for row in rows:
            self.download_episode(row["track_id"], lock=False)

    def download_episode(self, track_id: str, lock: bool = True) -> None:
        if lock:
            with self.sync_lock:
                return self.download_episode(track_id, lock=False)
        with self.database.connect() as db:
            row = db.execute(
                """SELECT e.*, a.directory_name FROM episodes e JOIN albums a ON a.id=e.album_id
                WHERE e.track_id=?""",
                (track_id,),
            ).fetchone()
        if not row or row["local_path"]:
            return
        free_gb = shutil.disk_usage(self.settings.media_dir).free / (1024**3)
        if free_gb < self.settings.min_free_gb:
            raise RuntimeError(
                f"Only {free_gb:.1f} GiB free; minimum is {self.settings.min_free_gb:.1f} GiB"
            )
        season_dir = self.settings.media_dir / row["directory_name"] / f"Season {row['season_number']:02d}"
        filename = f"{row['episode_number']:02d} - {safe_component(row['title'], 160)}.mp3"
        destination = season_dir / filename
        temporary = destination.with_suffix(".mp3.part")
        started = time.monotonic()
        LOGGER.info(
            "Download started: album=%s season=%s episode=%s track=%s title=%r",
            row["album_id"],
            row["season_number"],
            row["episode_number"],
            track_id,
            row["title"],
        )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.gateway.download_track(track_id, temporary)
            os.replace(temporary, destination)
            with self.database.connect() as db:
                db.execute(
                    """UPDATE episodes SET local_path=?, media_size=?, downloaded_at=?,
                    download_error=NULL WHERE track_id=?""",
                    (str(destination), destination.stat().st_size, iso(utcnow()), track_id),
                )
            LOGGER.info(
                "Download finished: track=%s bytes=%s elapsed=%.1fs path=%s",
                track_id,
                destination.stat().st_size,
                time.monotonic() - started,
                destination,
            )
        except Exception as error:
            temporary.unlink(missing_ok=True)
            with self.database.connect() as db:
                db.execute(
                    "UPDATE episodes SET download_error=? WHERE track_id=?",
                    (str(error), track_id),
                )
            LOGGER.exception(
                "Download failed: track=%s elapsed=%.1fs",
                track_id,
                time.monotonic() - started,
            )
            raise
