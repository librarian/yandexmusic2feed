import asyncio
from pathlib import Path

import httpx

from app.config import Settings
from app.main import create_app


def test_health_and_media_range(tmp_path: Path):
    data = tmp_path / "data"
    media = tmp_path / "media"
    data.mkdir()
    media.mkdir()
    settings = Settings("test", data, media, "https://example.test", 300, 192, 0)
    app = create_app(settings, start_scheduler=False)
    audio = media / "podcast" / "Season 01" / "01 - Тест.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"0123456789")
    with app.state.database.connect() as db:
        db.execute(
            """INSERT INTO albums(id,title,directory_name,created_at,updated_at)
            VALUES (1,'Test','podcast','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"""
        )
        db.execute(
            """INSERT INTO episodes(track_id,album_id,season_number,episode_number,title,
            discovered_at,published_at,local_path,media_size)
            VALUES ('123',1,1,1,'Episode','2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00',?,10)""",
            (str(audio),),
        )
        db.execute(
            """INSERT INTO episodes(track_id,album_id,season_number,episode_number,title,
            discovered_at,published_at)
            VALUES ('456',1,2,1,'On demand','2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00')"""
        )

    class DownloadGateway:
        def download_track(self, track_id: str, destination: Path) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"on-demand-audio")

    app.state.service.gateway = DownloadGateway()

    async def immediate(function, *args):
        return function(*args)

    app.state.thread_runner = immediate

    async def verify():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            response = await client.get(
                "/media/123.mp3", headers={"Range": "bytes=2-5"}
            )
            head = await client.head("/media/123.mp3")
            lazy_head = await client.head("/media/456.mp3")
            assert lazy_head.status_code == 200
            assert lazy_head.headers["x-yandexmusic2feed-downloaded"] == "false"
            with app.state.database.connect() as db:
                assert db.execute(
                    "SELECT local_path FROM episodes WHERE track_id='456'"
                ).fetchone()[0] is None
            lazy_get = await client.get(
                "/media/456.mp3", headers={"Range": "bytes=1-3"}
            )
        assert response.status_code == 206
        assert response.content == b"2345"
        assert response.headers["accept-ranges"] == "bytes"
        assert "filename*=UTF-8''" in response.headers["content-disposition"]
        assert head.status_code == 200
        assert head.headers["content-length"] == "10"
        assert lazy_get.status_code == 206
        assert lazy_get.content == b"n-d"
        with app.state.database.connect() as db:
            lazy_path = db.execute(
                "SELECT local_path FROM episodes WHERE track_id='456'"
            ).fetchone()[0]
        assert lazy_path and Path(lazy_path).is_file()

    asyncio.run(verify())
