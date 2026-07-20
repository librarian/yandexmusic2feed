import sqlite3
from pathlib import Path

from app.config import Settings
from app.db import Database, SCHEMA
from app.feed import build_feed
from app.service import PodcastService
from app.yandex import AlbumMetadata, EpisodeMetadata


class FakeGateway:
    def album(self, album_id: int) -> AlbumMetadata:
        return AlbumMetadata(
            album_id=album_id,
            title="Example Podcast",
            artists="Example Studio",
            description="An example",
            cover_url=None,
            episodes=[
                EpisodeMetadata("101", 1, 1, "First", "Host", 60_000, True, "2024-01-02"),
                EpisodeMetadata("201", 2, 1, "Second season", "Host", 90_000, True, None),
                EpisodeMetadata("202", 2, 2, "Newest", "Host", 120_000, True, "2025-03-04"),
            ],
        )

    def download_track(self, track_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(("audio-" + track_id).encode())

    def download_cover(self, url: str, destination: Path) -> None:
        raise AssertionError("No cover expected")


def make_service(tmp_path: Path) -> PodcastService:
    data = tmp_path / "data"
    media = tmp_path / "media"
    data.mkdir()
    media.mkdir()
    settings = Settings(
        token="test",
        data_dir=data,
        media_dir=media,
        public_base_url="https://example.test",
        scheduler_poll_seconds=300,
        download_bitrate_kbps=192,
        min_free_gb=0,
    )
    database = Database(settings.database_path)
    database.initialize()
    service = PodcastService(settings, database)
    service.gateway = FakeGateway()
    return service


def test_database_migrates_existing_episode_table(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy_schema = SCHEMA.replace("    source_published_at TEXT,\n", "")
    connection = sqlite3.connect(path)
    connection.executescript(legacy_schema)
    connection.close()

    database = Database(path)
    database.initialize()
    with database.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(episodes)")}
    assert "source_published_at" in columns


def test_newest_season_is_selected_and_downloaded(tmp_path):
    service = make_service(tmp_path)
    service.add_album(42)
    album, seasons, episodes = service.get_album(42)

    assert album["title"] == "Example Podcast"
    assert {row["number"]: row["selected"] for row in seasons} == {2: 1, 1: 0}
    downloaded = {row["track_id"] for row in episodes if row["local_path"]}
    assert downloaded == {"201", "202"}
    assert all(Path(row["local_path"]).exists() for row in episodes if row["local_path"])


def test_season_selection_and_on_demand_download(tmp_path):
    service = make_service(tmp_path)
    service.add_album(42)
    service.update_album(42, 168, True, [1])
    service.download_selected(42)
    service.download_episode("101")  # idempotent second request

    album, seasons, episodes = service.get_album(42)
    assert album["interval_hours"] == 168
    assert {row["number"]: row["selected"] for row in seasons} == {2: 0, 1: 1}
    assert all(row["local_path"] for row in episodes)


def test_feed_contains_all_discovered_files(tmp_path):
    service = make_service(tmp_path)
    service.register_album(42)
    service.sync_album(42, download_selected=False)
    service.download_episode("202")
    album, _, episodes = service.get_album(42)

    xml = build_feed(album, episodes, service.settings.public_base_url).decode()
    assert "Newest" in xml
    assert "Second season" in xml
    assert "First" in xml
    assert "https://example.test/media/202.mp3" in xml
    assert "https://example.test/media/101.mp3" in xml
    assert "yandex-music:track:202" in xml
    assert "Tue, 04 Mar 2025 00:00:00 +0000" in xml
    assert "Tue, 02 Jan 2024 00:00:00 +0000" in xml
    assert "2026-" not in xml
