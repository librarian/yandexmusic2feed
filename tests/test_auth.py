import stat
from types import SimpleNamespace

from app.auth import TokenManager


def test_stored_device_token_overrides_environment_token(tmp_path):
    manager = TokenManager(tmp_path, environment_token="legacy-token", clock=lambda: 100)
    manager._save("device-token", "refresh-token", 10_000_000, "bearer")

    assert manager.access_token() == "device-token"
    assert manager.status()["source"] == "device_flow"
    assert stat.S_IMODE(manager.token_path.stat().st_mode) == 0o600
    assert "device-token" not in str(manager.status())


def test_expiring_token_is_refreshed_and_rotated(tmp_path, monkeypatch):
    now = [100.0]
    manager = TokenManager(tmp_path, clock=lambda: now[0])
    manager._save("old-access", "old-refresh", 10, "bearer")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 20_000_000,
                "token_type": "bearer",
            }

    monkeypatch.setattr("app.auth.httpx.post", lambda *args, **kwargs: Response())

    assert manager.access_token() == "new-access"
    stored = manager._load()
    assert stored["refresh_token"] == "new-refresh"


def test_device_flow_is_polled_and_saved(tmp_path, monkeypatch):
    token = SimpleNamespace(
        access_token="device-access",
        refresh_token="device-refresh",
        expires_in=10_000_000,
        token_type="bearer",
    )

    class FakeClient:
        def request_device_code(self, **kwargs):
            return SimpleNamespace(
                device_code="private-device-code",
                user_code="ABCD-EFGH",
                verification_url="https://oauth.example/device",
                interval=1,
                expires_in=300,
            )

        def poll_device_token(self, *args, **kwargs):
            return token

    monkeypatch.setattr("app.auth.Client", FakeClient)
    manager = TokenManager(tmp_path, clock=lambda: 100)

    public = manager.start_device_auth()
    assert public["user_code"] == "ABCD-EFGH"
    assert "device_code" not in public
    assert manager.poll_device_auth() == "authorized"
    assert manager.access_token() == "device-access"
    assert manager.pending_status() is None

