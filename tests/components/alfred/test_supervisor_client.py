"""Tests for `custom_components.alfred.supervisor.SupervisorClient`.

The client is a thin async wrapper around aiohttp that:

- Prepends the Supervisor base URL.
- Attaches the `Authorization: Bearer <SUPERVISOR_TOKEN>` header.
- Parses JSON responses into Python; falls back to text otherwise.
- Returns transport errors as a synthetic 599 envelope (not a raise).

We mock `aiohttp.ClientSession` so the tests are hermetic — no actual
Supervisor required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.alfred.supervisor import (
    SUPERVISOR_BASE,
    SUPERVISOR_TOKEN_ENV,
    SupervisorClient,
    SupervisorResult,
    supervisor_available,
    supervisor_token,
)


def _request_cm(
    status: int,
    *,
    json_body: dict | None = None,
    text_body: str = "",
    content_type: str = "application/json",
    raises: BaseException | None = None,
):
    """Build a mocked aiohttp.ClientSession.request(...) context."""
    cm = MagicMock()
    response = MagicMock()
    response.status = status
    response.headers = {"Content-Type": content_type}
    response.json = AsyncMock(return_value=json_body if json_body is not None else {})
    response.text = AsyncMock(return_value=text_body)
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock(spec=aiohttp.ClientSession)
    if raises is not None:
        session.request = MagicMock(side_effect=raises)
    else:
        session.request = MagicMock(return_value=cm)
    return session


# ---------------------------------------------------------------------------
# supervisor_token / supervisor_available
# ---------------------------------------------------------------------------


class TestSupervisorTokenEnv:
    def test_returns_token_when_set(self, monkeypatch):
        monkeypatch.setenv(SUPERVISOR_TOKEN_ENV, "secret-token-12345")
        assert supervisor_token() == "secret-token-12345"
        assert supervisor_available() is True

    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv(SUPERVISOR_TOKEN_ENV, raising=False)
        assert supervisor_token() is None
        assert supervisor_available() is False

    def test_returns_none_when_empty(self, monkeypatch):
        monkeypatch.setenv(SUPERVISOR_TOKEN_ENV, "")
        assert supervisor_token() is None
        assert supervisor_available() is False


# ---------------------------------------------------------------------------
# SupervisorClient.request — happy paths
# ---------------------------------------------------------------------------


class TestRequestHappyPath:
    @pytest.mark.asyncio
    async def test_get_returns_parsed_json(self):
        session = _request_cm(200, json_body={"version": "12.0"})
        client = SupervisorClient(session, "token-x")
        result = await client.request("GET", "/os/info")
        assert isinstance(result, SupervisorResult)
        assert result.status == 200
        assert result.body == {"version": "12.0"}

    @pytest.mark.asyncio
    async def test_post_with_json_body(self):
        session = _request_cm(200, json_body={"ok": True})
        client = SupervisorClient(session, "token-x")
        result = await client.request(
            "POST", "/addons/foo/options", json_body={"options": {"a": 1}}
        )
        assert result.status == 200
        assert result.body == {"ok": True}
        # Verify the call was issued with the json_body.
        kwargs = session.request.call_args.kwargs
        assert kwargs["json"] == {"options": {"a": 1}}

    @pytest.mark.asyncio
    async def test_bearer_header_attached(self):
        session = _request_cm(200, json_body={})
        client = SupervisorClient(session, "secret-token-42")
        await client.request("GET", "/host/info")
        kwargs = session.request.call_args.kwargs
        assert kwargs["headers"]["Authorization"] == "Bearer secret-token-42"

    @pytest.mark.asyncio
    async def test_url_uses_supervisor_base(self):
        session = _request_cm(200, json_body={})
        client = SupervisorClient(session, "token-x")
        await client.request("GET", "/host/info")
        args = session.request.call_args.args
        # Positional: (method, url)
        assert args[0] == "GET"
        assert args[1] == f"{SUPERVISOR_BASE}/host/info"

    @pytest.mark.asyncio
    async def test_path_normalised_when_missing_leading_slash(self):
        session = _request_cm(200, json_body={})
        client = SupervisorClient(session, "token-x")
        await client.request("GET", "host/info")
        args = session.request.call_args.args
        assert args[1] == f"{SUPERVISOR_BASE}/host/info"

    @pytest.mark.asyncio
    async def test_non_json_response_returns_text(self):
        session = _request_cm(
            200, text_body="plain log line", content_type="text/plain"
        )
        client = SupervisorClient(session, "token-x")
        result = await client.request("GET", "/host/logs")
        assert result.status == 200
        assert result.body == "plain log line"


# ---------------------------------------------------------------------------
# SupervisorClient.request — error envelopes
# ---------------------------------------------------------------------------


class TestRequestErrorPaths:
    @pytest.mark.asyncio
    async def test_404_passes_through(self):
        session = _request_cm(404, json_body={"message": "addon not found"})
        client = SupervisorClient(session, "token-x")
        result = await client.request("GET", "/addons/nonexistent/info")
        # We do NOT raise on 4xx — the caller can branch.
        assert result.status == 404
        assert result.body == {"message": "addon not found"}

    @pytest.mark.asyncio
    async def test_500_passes_through(self):
        session = _request_cm(500, json_body={"error": "boom"})
        client = SupervisorClient(session, "token-x")
        result = await client.request("GET", "/host/info")
        assert result.status == 500

    @pytest.mark.asyncio
    async def test_timeout_returns_599(self):
        session = _request_cm(0, raises=TimeoutError())
        client = SupervisorClient(session, "token-x")
        result = await client.request("GET", "/host/info")
        assert result.status == 599
        assert isinstance(result.body, dict)
        assert result.body["error"] == "supervisor_timeout"

    @pytest.mark.asyncio
    async def test_client_error_returns_599(self):
        session = _request_cm(0, raises=aiohttp.ClientError("dns failure"))
        client = SupervisorClient(session, "token-x")
        result = await client.request("GET", "/host/info")
        assert result.status == 599
        assert result.body["error"] == "supervisor_unreachable"
        assert "dns failure" in result.body["detail"]

    @pytest.mark.asyncio
    async def test_malformed_json_falls_back_to_text(self):
        session = _request_cm(
            200, content_type="application/json"
        )
        # Force json() to raise.
        response_cm = session.request.return_value
        response = response_cm.__aenter__.return_value
        response.json = AsyncMock(side_effect=aiohttp.ContentTypeError(MagicMock(), ()))
        response.text = AsyncMock(return_value="<html>not json</html>")
        client = SupervisorClient(session, "token-x")
        result = await client.request("GET", "/host/info")
        assert result.status == 200
        assert result.body == "<html>not json</html>"
