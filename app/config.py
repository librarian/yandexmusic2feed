from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    token: str
    data_dir: Path
    media_dir: Path
    public_base_url: str
    scheduler_poll_seconds: int
    download_bitrate_kbps: int
    min_free_gb: float
    yandex_client_id: str | None = None
    yandex_client_secret: str | None = None
    yandex_device_name: str = "YandexMusic2Feed"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            token=os.environ.get("TOKEN", ""),
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            media_dir=Path(os.environ.get("MEDIA_DIR", "/podcasts")),
            public_base_url=os.environ.get(
                "PUBLIC_BASE_URL", "http://localhost:8000"
            ).rstrip("/"),
            scheduler_poll_seconds=int(os.environ.get("SCHEDULER_POLL_SECONDS", "300")),
            download_bitrate_kbps=int(os.environ.get("DOWNLOAD_BITRATE_KBPS", "192")),
            min_free_gb=float(os.environ.get("MIN_FREE_GB", "10")),
            yandex_client_id=os.environ.get("YANDEX_CLIENT_ID") or None,
            yandex_client_secret=os.environ.get("YANDEX_CLIENT_SECRET") or None,
            yandex_device_name=os.environ.get("YANDEX_DEVICE_NAME", "YandexMusic2Feed"),
        )

    @property
    def database_path(self) -> Path:
        renamed = self.data_dir / "yandexmusic2feed.sqlite3"
        legacy = self.data_dir / "yandex2feed.sqlite3"
        return legacy if legacy.exists() and not renamed.exists() else renamed

    @property
    def covers_dir(self) -> Path:
        return self.data_dir / "covers"
