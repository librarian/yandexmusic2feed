from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
from yandex_music import Client

LOGGER = logging.getLogger(__name__)
OAUTH_TOKEN_URL = "https://oauth.yandex.ru/token"
REFRESH_AFTER_SECONDS = 90 * 24 * 60 * 60
REFRESH_BEFORE_EXPIRY_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class PendingDeviceAuth:
    device_code: str
    user_code: str
    verification_url: str
    interval: int
    expires_at: float


class TokenManager:
    """Persist device-flow credentials and refresh them without exposing secrets."""

    def __init__(
        self,
        data_dir: Path,
        environment_token: str = "",
        client_id: str | None = None,
        client_secret: str | None = None,
        device_name: str = "YandexMusic2Feed",
        clock: Callable[[], float] = time.time,
    ):
        self.token_path = data_dir / "oauth.json"
        self.device_id_path = data_dir / "device-id"
        self.environment_token = environment_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.device_name = device_name
        self.clock = clock
        self._lock = threading.RLock()
        self._pending: PendingDeviceAuth | None = None

    def access_token(self) -> str:
        with self._lock:
            stored = self._load()
            if stored:
                if self._should_refresh(stored):
                    stored = self._refresh(stored)
                return str(stored["access_token"])
            if self.environment_token:
                return self.environment_token
        raise RuntimeError(
            "Yandex Music is not authenticated; use /auth or configure TOKEN"
        )

    def status(self) -> dict[str, object]:
        with self._lock:
            stored = self._load()
            if stored:
                return {
                    "source": "device_flow",
                    "refreshable": bool(stored.get("refresh_token")),
                    "expires_at": stored.get("expires_at"),
                    "refreshed_at": stored.get("refreshed_at"),
                }
            return {
                "source": "environment" if self.environment_token else "missing",
                "refreshable": False,
                "expires_at": None,
                "refreshed_at": None,
            }

    def pending_status(self) -> dict[str, object] | None:
        with self._lock:
            pending = self._pending
            if not pending:
                return None
            if pending.expires_at <= self.clock():
                self._pending = None
                return None
            return {
                "user_code": pending.user_code,
                "verification_url": pending.verification_url,
                "interval": pending.interval,
                "expires_at": pending.expires_at,
            }

    def start_device_auth(self) -> dict[str, object]:
        with self._lock:
            client = Client()
            code = client.request_device_code(
                device_id=self._device_id(),
                device_name=self.device_name,
                client_id=self.client_id,
            )
            self._pending = PendingDeviceAuth(
                device_code=code.device_code,
                user_code=code.user_code,
                verification_url=code.verification_url,
                interval=max(1, int(code.interval)),
                expires_at=self.clock() + int(code.expires_in),
            )
            LOGGER.info(
                "Yandex device authorization started; code expires in %ss",
                code.expires_in,
            )
            return self.pending_status() or {}

    def poll_device_auth(self) -> str:
        with self._lock:
            pending = self._pending
            if not pending:
                return "expired"
            if pending.expires_at <= self.clock():
                self._pending = None
                return "expired"
            client = Client()
            token = client.poll_device_token(
                pending.device_code,
                client_id=self.client_id,
                client_secret=self.client_secret,
            )
            if token is None:
                return "pending"
            self._save(
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                expires_in=token.expires_in,
                token_type=token.token_type,
            )
            self._pending = None
            LOGGER.info("Yandex device authorization completed and credentials were stored")
            return "authorized"

    def _should_refresh(self, stored: dict[str, object]) -> bool:
        if not stored.get("refresh_token"):
            return False
        now = self.clock()
        expires_at = float(stored.get("expires_at") or 0)
        refreshed_at = float(stored.get("refreshed_at") or 0)
        return (
            expires_at <= now + REFRESH_BEFORE_EXPIRY_SECONDS
            or refreshed_at <= now - REFRESH_AFTER_SECONDS
        )

    def _refresh(self, stored: dict[str, object]) -> dict[str, object]:
        client_id, client_secret = self._oauth_credentials()
        response = httpx.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": stored["refresh_token"],
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        self._save(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token") or stored.get("refresh_token"),
            expires_in=payload.get("expires_in"),
            token_type=payload.get("token_type"),
        )
        LOGGER.info("Yandex OAuth credentials refreshed")
        return self._load() or {}

    def _save(
        self,
        access_token: str,
        refresh_token: str | None,
        expires_in: int | None,
        token_type: str | None,
    ) -> None:
        now = self.clock()
        payload = {
            "version": 1,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": token_type or "bearer",
            "refreshed_at": now,
            "expires_at": now + int(expires_in) if expires_in else None,
        }
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.token_path.with_suffix(".json.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output)
                output.write("\n")
            os.replace(temporary, self.token_path)
            os.chmod(self.token_path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def _load(self) -> dict[str, object] | None:
        if not self.token_path.is_file():
            return None
        with self.token_path.open(encoding="utf-8") as source:
            payload = json.load(source)
        if not payload.get("access_token"):
            raise RuntimeError("Stored OAuth file has no access_token")
        return payload

    def _device_id(self) -> str:
        if self.device_id_path.is_file():
            return self.device_id_path.read_text(encoding="ascii").strip()
        device_id = "ym2f-" + secrets.token_hex(12)
        descriptor = os.open(
            self.device_id_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.write(device_id + "\n")
        return device_id

    def _oauth_credentials(self) -> tuple[str, str]:
        if self.client_id and self.client_secret:
            return self.client_id, self.client_secret
        # yandex-music 3.0 uses these defaults for Device Flow. Importing them
        # here ensures refresh requests use the same OAuth application.
        from yandex_music._client.device_auth import (  # noqa: PLC0415
            _DEFAULT_CLIENT_ID,
            _DEFAULT_CLIENT_SECRET,
        )

        return self.client_id or _DEFAULT_CLIENT_ID, self.client_secret or _DEFAULT_CLIENT_SECRET
