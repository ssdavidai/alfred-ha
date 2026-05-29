"""Tests for graceful degradation on HA Container / Core (no Supervisor).

When `SUPERVISOR_TOKEN` isn't set:

- `supervisor_available()` returns False.
- `supervisor_token()` returns None.
- `_supervisor_client()` returns None.
- Every supervisor_* handler returns `{error: "supervisor_unavailable", ...}`
  rather than raising.

The Alfred caller can then branch on `error == "supervisor_unavailable"`
and tell the user "this HA install isn't HAOS/Supervised — upgrade to use
the wake-word upload" instead of "Alfred crashed".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.alfred import services as services_mod
from custom_components.alfred.services import (
    _handle_addon_info,
    _handle_addon_options_update,
    _handle_host_info,
    _handle_os_info,
    _handle_share_delete,
    _handle_share_list,
    _handle_share_read,
    _handle_share_write,
    _handle_supervisor_call,
    _supervisor_client,
)
from custom_components.alfred.supervisor import (
    SUPERVISOR_TOKEN_ENV,
    supervisor_available,
    supervisor_token,
)


class _FakeServiceCall:
    def __init__(self, data: dict):
        self.data = data


def _hass() -> MagicMock:
    return MagicMock()


class TestSupervisorAvailable:
    def test_no_env_no_token(self, monkeypatch):
        monkeypatch.delenv(SUPERVISOR_TOKEN_ENV, raising=False)
        assert supervisor_token() is None
        assert supervisor_available() is False

    def test_empty_env_no_token(self, monkeypatch):
        monkeypatch.setenv(SUPERVISOR_TOKEN_ENV, "")
        assert supervisor_token() is None
        assert supervisor_available() is False


class TestSupervisorClientReturnsNone:
    @pytest.mark.asyncio
    async def test_returns_none_when_token_missing(self, monkeypatch):
        monkeypatch.delenv(SUPERVISOR_TOKEN_ENV, raising=False)
        # _supervisor_client doesn't open a session if no token is present.
        result = await _supervisor_client(_hass())
        assert result is None


# All passthrough handlers must produce the structured-unavailable envelope
# when no Supervisor token is present. We parametrise to keep it tight.


PASSTHROUGH_HANDLERS = [
    (_handle_supervisor_call, {"method": "GET", "path": "/host/info"}),
    (_handle_addon_info, {"slug": "core_foo"}),
    (
        _handle_addon_options_update,
        {"slug": "core_foo", "options": {}, "restart": False},
    ),
    (_handle_host_info, {}),
    (_handle_os_info, {}),
]


@pytest.mark.parametrize("handler,data", PASSTHROUGH_HANDLERS)
@pytest.mark.asyncio
async def test_passthrough_handler_returns_unavailable_envelope(
    handler, data, monkeypatch
):
    monkeypatch.delenv(SUPERVISOR_TOKEN_ENV, raising=False)
    # Force _supervisor_client to None even if the test process happens to
    # have SUPERVISOR_TOKEN set in CI.
    monkeypatch.setattr(
        services_mod, "_supervisor_client", AsyncMock(return_value=None)
    )
    result = await handler(_hass(), _FakeServiceCall(data))
    assert result["error"] == "supervisor_unavailable"
    assert "installation_type" in result
    assert "hint" in result


# Share-* handlers don't strictly depend on the Supervisor token (they
# touch the local FS), but they DO require `/share/` to exist. If it
# doesn't, they return `supervisor_unavailable` with reason
# `share_root_missing` so the caller can branch the same way.


class TestShareHandlersWithoutShareRoot:
    @pytest.mark.asyncio
    async def test_share_write_returns_unavailable(self, monkeypatch, tmp_path):
        missing = tmp_path / "no-share"
        monkeypatch.setattr(
            "custom_components.alfred.supervisor.SHARE_ROOT", missing
        )
        monkeypatch.setattr(
            "custom_components.alfred.services.SHARE_ROOT", missing
        )
        result = await _handle_share_write(
            _hass(),
            _FakeServiceCall(
                {
                    "path": "x.bin",
                    "content_base64": "eA==",  # "x"
                }
            ),
        )
        assert result["error"] == "share_root_missing"

    @pytest.mark.asyncio
    async def test_share_read_returns_unavailable(self, monkeypatch, tmp_path):
        missing = tmp_path / "no-share"
        monkeypatch.setattr(
            "custom_components.alfred.supervisor.SHARE_ROOT", missing
        )
        monkeypatch.setattr(
            "custom_components.alfred.services.SHARE_ROOT", missing
        )
        result = await _handle_share_read(
            _hass(), _FakeServiceCall({"path": "x.bin"})
        )
        assert result["error"] == "share_root_missing"

    @pytest.mark.asyncio
    async def test_share_list_returns_unavailable(self, monkeypatch, tmp_path):
        missing = tmp_path / "no-share"
        monkeypatch.setattr(
            "custom_components.alfred.supervisor.SHARE_ROOT", missing
        )
        monkeypatch.setattr(
            "custom_components.alfred.services.SHARE_ROOT", missing
        )
        result = await _handle_share_list(
            _hass(), _FakeServiceCall({"path": "."})
        )
        assert result["error"] == "share_root_missing"

    @pytest.mark.asyncio
    async def test_share_delete_returns_unavailable(self, monkeypatch, tmp_path):
        missing = tmp_path / "no-share"
        monkeypatch.setattr(
            "custom_components.alfred.supervisor.SHARE_ROOT", missing
        )
        monkeypatch.setattr(
            "custom_components.alfred.services.SHARE_ROOT", missing
        )
        result = await _handle_share_delete(
            _hass(), _FakeServiceCall({"path": "x.bin"})
        )
        assert result["error"] == "share_root_missing"
