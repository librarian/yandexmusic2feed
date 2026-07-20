from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles

from .auth import TokenManager
from .config import Settings
from .db import Database
from .feed import build_feed
from .service import PodcastService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=PROJECT_DIR / "templates")


async def scheduler(service: PodcastService, poll_seconds: int) -> None:
    while True:
        try:
            await asyncio.to_thread(service.sync_due)
        except Exception:
            LOGGER.exception("Scheduler cycle failed")
        await asyncio.sleep(poll_seconds)


def logged_background(function, *args) -> None:
    LOGGER.info("Background task started: %s args=%s", function.__name__, args)
    try:
        function(*args)
    except Exception:
        LOGGER.exception("Background operation failed")
    else:
        LOGGER.info("Background task finished: %s", function.__name__)


def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    settings.covers_dir.mkdir(parents=True, exist_ok=True)
    database = Database(settings.database_path)
    database.initialize()
    token_manager = TokenManager(
        settings.data_dir,
        environment_token=settings.token,
        client_id=settings.yandex_client_id,
        client_secret=settings.yandex_client_secret,
        device_name=settings.yandex_device_name,
    )
    service = PodcastService(settings, database, token_provider=token_manager.access_token)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = None
        if start_scheduler:
            task = asyncio.create_task(
                scheduler(service, settings.scheduler_poll_seconds), name="album-scheduler"
            )
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    application = FastAPI(title="YandexMusic2Feed", lifespan=lifespan)
    application.state.settings = settings
    application.state.database = database
    application.state.service = service
    application.state.token_manager = token_manager
    application.state.thread_runner = asyncio.to_thread
    application.mount("/static", StaticFiles(directory=PROJECT_DIR / "static"), name="static")

    @application.get("/")
    def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "albums": service.list_albums(),
                "base_url": settings.public_base_url,
                "auth_status": token_manager.status(),
            },
        )

    @application.get("/auth")
    def auth_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={
                "auth_status": token_manager.status(),
                "pending": token_manager.pending_status(),
            },
        )

    @application.post("/auth/device/start")
    def auth_start():
        try:
            token_manager.start_device_auth()
        except Exception as error:
            LOGGER.exception("Could not start Yandex device authorization")
            raise HTTPException(502, f"Could not start device authorization: {error}") from error
        return RedirectResponse("/auth", status_code=303)

    @application.post("/auth/device/poll")
    async def auth_poll(request: Request):
        try:
            status = await request.app.state.thread_runner(token_manager.poll_device_auth)
        except Exception as error:
            LOGGER.exception("Could not poll Yandex device authorization")
            raise HTTPException(502, f"Could not complete device authorization: {error}") from error
        return {"status": status}

    @application.post("/albums")
    def add_album(
        background: BackgroundTasks,
        album_id: int = Form(...),
        interval_hours: int = Form(24),
    ):
        service.register_album(album_id, interval_hours)
        background.add_task(logged_background, service.sync_album, album_id, True)
        return RedirectResponse(f"/albums/{album_id}", status_code=303)

    @application.get("/albums/{album_id}")
    def album_page(request: Request, album_id: int):
        album, seasons, episodes = service.get_album(album_id)
        if not album:
            raise HTTPException(404, "Album is not configured")
        return templates.TemplateResponse(
            request=request,
            name="album.html",
            context={
                "album": album,
                "seasons": seasons,
                "episodes": episodes,
                "base_url": settings.public_base_url,
            },
        )

    @application.post("/albums/{album_id}/settings")
    def album_settings(
        album_id: int,
        background: BackgroundTasks,
        interval_hours: int = Form(24),
        enabled: str | None = Form(None),
        seasons: list[int] = Form(default=[]),
    ):
        service.update_album(album_id, interval_hours, enabled is not None, seasons)
        background.add_task(logged_background, service.download_selected, album_id)
        return RedirectResponse(f"/albums/{album_id}", status_code=303)

    @application.post("/albums/{album_id}/sync")
    def sync_album(album_id: int, background: BackgroundTasks):
        background.add_task(logged_background, service.sync_album, album_id, True)
        return RedirectResponse(f"/albums/{album_id}", status_code=303)

    @application.post("/episodes/{track_id}/download")
    def download_episode(track_id: str, album_id: int, background: BackgroundTasks):
        background.add_task(logged_background, service.download_episode, track_id)
        return RedirectResponse(f"/albums/{album_id}", status_code=303)

    @application.get("/{album_id}.xml")
    def feed(album_id: int):
        album, _, episodes = service.get_album(album_id)
        if not album:
            raise HTTPException(404, "Album is not configured")
        return Response(
            build_feed(album, episodes, settings.public_base_url),
            media_type="application/rss+xml; charset=utf-8",
        )

    @application.api_route("/media/{track_id}.mp3", methods=["GET", "HEAD"])
    async def media(request: Request, track_id: str):
        with database.connect() as db:
            episode = db.execute(
                "SELECT local_path FROM episodes WHERE track_id=?", (track_id,)
            ).fetchone()
        if not episode:
            raise HTTPException(404, "Episode is unknown")
        if not episode["local_path"] and request.method == "HEAD":
            return Response(
                status_code=200,
                media_type="audio/mpeg",
                headers={
                    "Accept-Ranges": "bytes",
                    "X-YandexMusic2Feed-Downloaded": "false",
                },
            )
        if not episode["local_path"]:
            LOGGER.info("On-demand download requested for track %s", track_id)
            try:
                await request.app.state.thread_runner(service.download_episode, track_id)
            except Exception as error:
                LOGGER.exception("On-demand download failed for track %s", track_id)
                raise HTTPException(502, f"Episode download failed: {error}") from error
            with database.connect() as db:
                episode = db.execute(
                    "SELECT local_path FROM episodes WHERE track_id=?", (track_id,)
                ).fetchone()
        if not episode or not episode["local_path"]:
            raise HTTPException(502, "Episode download did not produce a local file")
        path = Path(episode["local_path"]).resolve()
        media_root = settings.media_dir.resolve()
        if media_root not in path.parents or not path.is_file():
            raise HTTPException(404, "Episode file is unavailable")
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = request.headers.get("range")
        if range_header:
            try:
                unit, requested = range_header.split("=", 1)
                if unit != "bytes" or "," in requested:
                    raise ValueError
                first, last = requested.split("-", 1)
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                else:
                    suffix = int(last)
                    start = max(0, size - suffix)
                end = min(end, size - 1)
                if start < 0 or start > end:
                    raise ValueError
                status = 206
            except ValueError:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

        async def stream_file():
            remaining = end - start + 1
            with path.open("rb") as source:
                source.seek(start)
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Disposition": f"inline; filename=episode.mp3; filename*=UTF-8''{quote(path.name)}",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        if request.method == "HEAD":
            return Response(status_code=status, media_type="audio/mpeg", headers=headers)
        return StreamingResponse(stream_file(), status_code=status, media_type="audio/mpeg", headers=headers)

    @application.get("/covers/{album_id}.jpg")
    async def cover(album_id: int):
        path = settings.covers_dir / f"{album_id}.jpg"
        if not path.is_file():
            raise HTTPException(404, "Cover is unavailable")
        return FileResponse(path, media_type="image/jpeg")

    @application.get("/health")
    async def health():
        with database.connect() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    return application
