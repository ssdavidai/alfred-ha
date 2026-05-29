"""Tests for `custom_components.alfred.services` — the HA-service handlers.

The handlers are the seam between HA's service-call layer and the
Supervisor REST + share filesystem. We test each one directly by:

- Building a mock `hass` with the methods the handler touches
  (`async_add_executor_job`, sometimes `services.async_register`).
- Passing a `_ServiceCall` (the stub from `conftest.py`) with the right
  data shape.
- Asserting the returned envelope.

The actual Supervisor REST round-trip is faked by patching
`_supervisor_client` to return a stand-in. Filesystem ops run against a
temp dir that we patch in as `SHARE_ROOT` via monkeypatch.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
)
from custom_components.alfred.supervisor import SupervisorResult


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeServiceCall:
    """Stand-in for HA's ServiceCall in unit tests."""

    def __init__(self, data: dict):
        self.data = data


def _fake_hass(*, share_root: Path | None = None) -> MagicMock:
    """Build a MagicMock that quacks like the HA `hass` object enough for
    our handlers.

    The only `hass` method the handlers call is `async_add_executor_job`,
    which we route through `asyncio.get_event_loop().run_in_executor(None, ...)`
    so the blocking filesystem ops actually execute.
    """
    hass = MagicMock()

    async def _exec(func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)

    hass.async_add_executor_job = _exec
    return hass


def _supervisor_client_stub(*results: SupervisorResult):
    """Return an async-callable that yields each `SupervisorResult` in turn."""
    queue = list(results)
    client = MagicMock()

    async def _request(method, path, *, json_body=None):
        if not queue:
            raise AssertionError("SupervisorClient stub exhausted")
        return queue.pop(0)

    client.request = _request
    return client


# ---------------------------------------------------------------------------
# Passthrough handlers
# ---------------------------------------------------------------------------


class TestSupervisorCall:
    @pytest.mark.asyncio
    async def test_get_forwards_method_and_path(self):
        client = _supervisor_client_stub(SupervisorResult(200, {"x": 1}))
        with patch.object(
            services_mod, "_supervisor_client", new=AsyncMock(return_value=client)
        ):
            hass = _fake_hass()
            call = _FakeServiceCall(
                {"method": "GET", "path": "/addons/foo/info"}
            )
            result = await _handle_supervisor_call(hass, call)
        assert result == {"status": 200, "body": {"x": 1}}

    @pytest.mark.asyncio
    async def test_post_with_body(self):
        client = _supervisor_client_stub(SupervisorResult(200, {"ok": True}))
        captured_body = {}

        async def _request(method, path, *, json_body=None):
            captured_body["json"] = json_body
            return SupervisorResult(200, {"ok": True})

        client.request = _request
        with patch.object(
            services_mod, "_supervisor_client", new=AsyncMock(return_value=client)
        ):
            hass = _fake_hass()
            call = _FakeServiceCall(
                {
                    "method": "POST",
                    "path": "/addons/foo/options",
                    "json_body": {"options": {"a": 1}},
                }
            )
            result = await _handle_supervisor_call(hass, call)
        assert result["status"] == 200
        assert captured_body["json"] == {"options": {"a": 1}}

    @pytest.mark.asyncio
    async def test_no_token_returns_unavailable(self):
        with patch.object(
            services_mod, "_supervisor_client", new=AsyncMock(return_value=None)
        ):
            hass = _fake_hass()
            call = _FakeServiceCall({"method": "GET", "path": "/host/info"})
            result = await _handle_supervisor_call(hass, call)
        assert result["error"] == "supervisor_unavailable"
        assert "installation_type" in result

    @pytest.mark.asyncio
    async def test_pass_through_propagates_4xx_body(self):
        # 4xx should NOT be transformed — Alfred wants to see addon-not-found.
        client = _supervisor_client_stub(
            SupervisorResult(404, {"message": "addon_not_found"})
        )
        with patch.object(
            services_mod, "_supervisor_client", new=AsyncMock(return_value=client)
        ):
            hass = _fake_hass()
            call = _FakeServiceCall(
                {"method": "GET", "path": "/addons/missing/info"}
            )
            result = await _handle_supervisor_call(hass, call)
        assert result == {"status": 404, "body": {"message": "addon_not_found"}}


class TestAddonInfo:
    @pytest.mark.asyncio
    async def test_calls_correct_path(self):
        captured = {}

        async def _request(method, path, *, json_body=None):
            captured["method"] = method
            captured["path"] = path
            return SupervisorResult(200, {"state": "running"})

        client = MagicMock()
        client.request = _request

        with patch.object(
            services_mod, "_supervisor_client", new=AsyncMock(return_value=client)
        ):
            hass = _fake_hass()
            call = _FakeServiceCall({"slug": "core_openwakeword"})
            result = await _handle_addon_info(hass, call)
        assert captured == {"method": "GET", "path": "/addons/core_openwakeword/info"}
        assert result == {"status": 200, "body": {"state": "running"}}


class TestAddonOptionsUpdate:
    @pytest.mark.asyncio
    async def test_options_only_no_restart(self):
        calls = []

        async def _request(method, path, *, json_body=None):
            calls.append((method, path, json_body))
            return SupervisorResult(200, {})

        client = MagicMock()
        client.request = _request
        with patch.object(
            services_mod, "_supervisor_client", new=AsyncMock(return_value=client)
        ):
            hass = _fake_hass()
            call = _FakeServiceCall(
                {
                    "slug": "core_openwakeword",
                    "options": {"foo": "bar"},
                    "restart": False,
                }
            )
            result = await _handle_addon_options_update(hass, call)
        assert len(calls) == 1
        assert calls[0] == ("POST", "/addons/core_openwakeword/options", {"options": {"foo": "bar"}})
        assert result["status"] == 200

    @pytest.mark.asyncio
    async def test_options_then_restart(self):
        calls = []

        async def _request(method, path, *, json_body=None):
            calls.append((method, path, json_body))
            return SupervisorResult(200, {})

        client = MagicMock()
        client.request = _request
        with patch.object(
            services_mod, "_supervisor_client", new=AsyncMock(return_value=client)
        ):
            hass = _fake_hass()
            call = _FakeServiceCall(
                {
                    "slug": "core_openwakeword",
                    "options": {"foo": "bar"},
                    "restart": True,
                }
            )
            result = await _handle_addon_options_update(hass, call)
        assert len(calls) == 2
        assert calls[1][1] == "/addons/core_openwakeword/restart"
        assert "options" in result and "restart" in result

    @pytest.mark.asyncio
    async def test_skip_restart_on_options_failure(self):
        calls = []

        async def _request(method, path, *, json_body=None):
            calls.append((method, path))
            return SupervisorResult(400, {"message": "invalid_options"})

        client = MagicMock()
        client.request = _request
        with patch.object(
            services_mod, "_supervisor_client", new=AsyncMock(return_value=client)
        ):
            hass = _fake_hass()
            call = _FakeServiceCall(
                {
                    "slug": "core_openwakeword",
                    "options": {"foo": "bar"},
                    "restart": True,
                }
            )
            result = await _handle_addon_options_update(hass, call)
        assert len(calls) == 1  # Restart skipped because options 400'd
        assert result["status"] == 400


class TestHostAndOsInfo:
    @pytest.mark.asyncio
    async def test_host_info(self):
        captured = {}

        async def _request(method, path, *, json_body=None):
            captured["path"] = path
            return SupervisorResult(200, {"hostname": "homeassistant"})

        client = MagicMock()
        client.request = _request
        with patch.object(
            services_mod, "_supervisor_client", new=AsyncMock(return_value=client)
        ):
            result = await _handle_host_info(_fake_hass(), _FakeServiceCall({}))
        assert captured["path"] == "/host/info"
        assert result["body"]["hostname"] == "homeassistant"

    @pytest.mark.asyncio
    async def test_os_info(self):
        captured = {}

        async def _request(method, path, *, json_body=None):
            captured["path"] = path
            return SupervisorResult(200, {"version": "12.0"})

        client = MagicMock()
        client.request = _request
        with patch.object(
            services_mod, "_supervisor_client", new=AsyncMock(return_value=client)
        ):
            result = await _handle_os_info(_fake_hass(), _FakeServiceCall({}))
        assert captured["path"] == "/os/info"
        assert result["body"]["version"] == "12.0"


# ---------------------------------------------------------------------------
# Share-write / read / list / delete — operate against a tmp dir.
# ---------------------------------------------------------------------------


@pytest.fixture
def share_root(tmp_path: Path, monkeypatch) -> Path:
    """Patch SHARE_ROOT in the supervisor + services modules to a tmp dir."""
    monkeypatch.setattr(
        "custom_components.alfred.supervisor.SHARE_ROOT", tmp_path
    )
    monkeypatch.setattr("custom_components.alfred.services.SHARE_ROOT", tmp_path)
    return tmp_path


class TestShareWrite:
    @pytest.mark.asyncio
    async def test_writes_file_and_returns_ok(self, share_root):
        content = b"\xfa\xfb\xfc" * 100
        call = _FakeServiceCall(
            {
                "path": "openwakeword/alfred.tflite",
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        )
        result = await _handle_share_write(_fake_hass(), call)
        assert result["ok"] is True
        assert result["bytes_written"] == len(content)
        written = (share_root / "openwakeword" / "alfred.tflite").read_bytes()
        assert written == content

    @pytest.mark.asyncio
    async def test_writes_atomically_via_rename(self, share_root):
        # After a successful write, no .alfred-tmp residue should remain.
        call = _FakeServiceCall(
            {
                "path": "foo.bin",
                "content_base64": base64.b64encode(b"hello").decode(),
            }
        )
        await _handle_share_write(_fake_hass(), call)
        residue = list(share_root.glob("*.alfred-tmp"))
        assert residue == []

    @pytest.mark.asyncio
    async def test_creates_parents(self, share_root):
        call = _FakeServiceCall(
            {
                "path": "deep/nested/path/alfred.tflite",
                "content_base64": base64.b64encode(b"hi").decode(),
            }
        )
        result = await _handle_share_write(_fake_hass(), call)
        assert result["ok"] is True
        assert (share_root / "deep" / "nested" / "path" / "alfred.tflite").exists()

    @pytest.mark.asyncio
    async def test_traversal_returns_error(self, share_root):
        call = _FakeServiceCall(
            {
                "path": "../etc/passwd",
                "content_base64": base64.b64encode(b"bad").decode(),
            }
        )
        result = await _handle_share_write(_fake_hass(), call)
        assert result["error"] == "unsafe_path"
        assert result["reason"] == "traversal"

    @pytest.mark.asyncio
    async def test_absolute_outside_share_returns_error(self, share_root):
        call = _FakeServiceCall(
            {
                "path": "/etc/passwd",
                "content_base64": base64.b64encode(b"bad").decode(),
            }
        )
        result = await _handle_share_write(_fake_hass(), call)
        assert result["error"] == "unsafe_path"
        assert result["reason"] == "outside_share_root"

    @pytest.mark.asyncio
    async def test_bad_base64_returns_error(self, share_root):
        call = _FakeServiceCall(
            {
                "path": "foo.bin",
                "content_base64": "!!!!not valid base64!!!!",
            }
        )
        result = await _handle_share_write(_fake_hass(), call)
        assert result["error"] == "bad_base64"

    @pytest.mark.asyncio
    async def test_too_large_returns_error(self, share_root, monkeypatch):
        monkeypatch.setattr(
            "custom_components.alfred.services.MAX_SHARE_BYTES", 100
        )
        call = _FakeServiceCall(
            {
                "path": "big.bin",
                "content_base64": base64.b64encode(b"x" * 200).decode(),
            }
        )
        result = await _handle_share_write(_fake_hass(), call)
        assert result["error"] == "too_large"
        assert result["max"] == 100

    @pytest.mark.asyncio
    async def test_share_root_missing_returns_unavailable(self, monkeypatch, tmp_path):
        # share_root pointed at a path that doesn't exist.
        missing = tmp_path / "no-such-share"
        monkeypatch.setattr(
            "custom_components.alfred.supervisor.SHARE_ROOT", missing
        )
        monkeypatch.setattr(
            "custom_components.alfred.services.SHARE_ROOT", missing
        )
        call = _FakeServiceCall(
            {
                "path": "x.bin",
                "content_base64": base64.b64encode(b"x").decode(),
            }
        )
        result = await _handle_share_write(_fake_hass(), call)
        assert result["error"] == "share_root_missing"


class TestShareRead:
    @pytest.mark.asyncio
    async def test_reads_file(self, share_root):
        (share_root / "openwakeword").mkdir()
        (share_root / "openwakeword" / "alfred.tflite").write_bytes(b"hello")
        call = _FakeServiceCall({"path": "openwakeword/alfred.tflite"})
        result = await _handle_share_read(_fake_hass(), call)
        assert result["ok"] is True
        assert base64.b64decode(result["content_base64"]) == b"hello"
        assert result["size"] == 5

    @pytest.mark.asyncio
    async def test_not_found(self, share_root):
        call = _FakeServiceCall({"path": "nope.bin"})
        result = await _handle_share_read(_fake_hass(), call)
        assert result["error"] == "not_found"

    @pytest.mark.asyncio
    async def test_directory_returns_error(self, share_root):
        (share_root / "openwakeword").mkdir()
        call = _FakeServiceCall({"path": "openwakeword"})
        result = await _handle_share_read(_fake_hass(), call)
        assert result["error"] == "not_a_file"

    @pytest.mark.asyncio
    async def test_too_large_rejected(self, share_root, monkeypatch):
        monkeypatch.setattr(
            "custom_components.alfred.services.MAX_SHARE_BYTES", 10
        )
        (share_root / "big.bin").write_bytes(b"x" * 100)
        call = _FakeServiceCall({"path": "big.bin"})
        result = await _handle_share_read(_fake_hass(), call)
        assert result["error"] == "too_large"

    @pytest.mark.asyncio
    async def test_traversal_rejected(self, share_root):
        call = _FakeServiceCall({"path": "../etc/passwd"})
        result = await _handle_share_read(_fake_hass(), call)
        assert result["error"] == "unsafe_path"


class TestShareList:
    @pytest.mark.asyncio
    async def test_lists_directory(self, share_root):
        (share_root / "openwakeword").mkdir()
        (share_root / "openwakeword" / "alfred.tflite").write_bytes(b"x" * 5)
        (share_root / "openwakeword" / "subdir").mkdir()
        call = _FakeServiceCall({"path": "openwakeword"})
        result = await _handle_share_list(_fake_hass(), call)
        assert result["ok"] is True
        names = {e["name"] for e in result["entries"]}
        assert names == {"alfred.tflite", "subdir"}
        for entry in result["entries"]:
            if entry["name"] == "alfred.tflite":
                assert entry["is_file"] is True
                assert entry["size"] == 5
            else:
                assert entry["is_dir"] is True

    @pytest.mark.asyncio
    async def test_not_found(self, share_root):
        call = _FakeServiceCall({"path": "no-such-dir"})
        result = await _handle_share_list(_fake_hass(), call)
        assert result["error"] == "not_found"

    @pytest.mark.asyncio
    async def test_file_returns_error(self, share_root):
        (share_root / "x.bin").write_bytes(b"x")
        call = _FakeServiceCall({"path": "x.bin"})
        result = await _handle_share_list(_fake_hass(), call)
        assert result["error"] == "not_a_directory"


class TestShareDelete:
    @pytest.mark.asyncio
    async def test_deletes_file(self, share_root):
        (share_root / "x.bin").write_bytes(b"x")
        call = _FakeServiceCall({"path": "x.bin"})
        result = await _handle_share_delete(_fake_hass(), call)
        assert result["ok"] is True
        assert not (share_root / "x.bin").exists()

    @pytest.mark.asyncio
    async def test_not_found(self, share_root):
        call = _FakeServiceCall({"path": "nope.bin"})
        result = await _handle_share_delete(_fake_hass(), call)
        assert result["error"] == "not_found"

    @pytest.mark.asyncio
    async def test_directory_rejected(self, share_root):
        (share_root / "subdir").mkdir()
        call = _FakeServiceCall({"path": "subdir"})
        result = await _handle_share_delete(_fake_hass(), call)
        assert result["error"] == "is_a_directory"

    @pytest.mark.asyncio
    async def test_traversal_rejected(self, share_root):
        call = _FakeServiceCall({"path": "../etc/passwd"})
        result = await _handle_share_delete(_fake_hass(), call)
        assert result["error"] == "unsafe_path"
