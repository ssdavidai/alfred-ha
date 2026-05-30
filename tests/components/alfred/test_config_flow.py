"""Tests for `custom_components.alfred.config_flow`.

These focus on the pure helpers and the `_preflight` HTTP round-trip. The
HA-side `async_step_user` chrome is covered separately by the
`pytest-homeassistant-custom-component` runner once we wire CI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.alfred._validators import (
    _normalise_base_url,
    _preflight,
    _token_shape_ok,
)
from custom_components.alfred.config_flow import AlfredOptionsFlow
from custom_components.alfred.const import (
    CONF_TIMEOUT,
    DEFAULT_TIMEOUT,
)


# ---------------------------------------------------------------------------
# _normalise_base_url
# ---------------------------------------------------------------------------


class TestNormaliseBaseUrl:
    def test_https_host_passes(self):
        assert _normalise_base_url("https://home.alfred.black") == "https://home.alfred.black"

    def test_trailing_slash_stripped(self):
        assert _normalise_base_url("https://home.alfred.black/") == "https://home.alfred.black"

    def test_multiple_trailing_slashes_stripped(self):
        assert _normalise_base_url("https://home.alfred.black///") == "https://home.alfred.black"

    def test_whitespace_trimmed(self):
        assert _normalise_base_url("  https://home.alfred.black  ") == "https://home.alfred.black"

    def test_http_allowed_for_local_tailscale(self):
        # Some Tailscale-only deploys serve HTTP on a MagicDNS host.
        assert _normalise_base_url("http://alfred.tailfoo.ts.net") == "http://alfred.tailfoo.ts.net"

    def test_no_scheme_rejected(self):
        with pytest.raises(ValueError):
            _normalise_base_url("home.alfred.black")

    def test_unknown_scheme_rejected(self):
        with pytest.raises(ValueError):
            _normalise_base_url("ftp://home.alfred.black")

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError):
            _normalise_base_url("")

    def test_scheme_only_rejected(self):
        with pytest.raises(ValueError):
            _normalise_base_url("https://")


# ---------------------------------------------------------------------------
# _token_shape_ok
# ---------------------------------------------------------------------------


class TestTokenShape:
    def test_valid_token_passes(self):
        tok = "ha_" + "a" * 48
        assert _token_shape_ok(tok) is True

    def test_mixed_hex_passes(self):
        tok = "ha_" + "deadbeefcafefacefeedfacecafebabe" + "0" * 16
        assert _token_shape_ok(tok) is True

    def test_uppercase_hex_passes(self):
        # int(_, 16) is case-insensitive.
        tok = "ha_" + "DEADBEEF" * 6
        assert _token_shape_ok(tok) is True

    def test_wrong_prefix_rejected(self):
        tok = "pcp_" + "a" * 48
        assert _token_shape_ok(tok) is False

    def test_no_prefix_rejected(self):
        assert _token_shape_ok("a" * 48) is False

    def test_too_short_rejected(self):
        tok = "ha_" + "a" * 47
        assert _token_shape_ok(tok) is False

    def test_too_long_rejected(self):
        tok = "ha_" + "a" * 49
        assert _token_shape_ok(tok) is False

    def test_non_hex_body_rejected(self):
        tok = "ha_" + "z" * 48
        assert _token_shape_ok(tok) is False

    def test_whitespace_trimmed(self):
        tok = "  ha_" + "a" * 48 + "  "
        assert _token_shape_ok(tok) is True

    def test_empty_rejected(self):
        assert _token_shape_ok("") is False


# ---------------------------------------------------------------------------
# _preflight — HTTP round-trip
# ---------------------------------------------------------------------------


def _make_session(status: int, *, raises: BaseException | None = None) -> MagicMock:
    """Build a MagicMock that mimics an aiohttp.ClientSession.post() context."""
    session = MagicMock(spec=aiohttp.ClientSession)

    resp_cm = MagicMock()
    response = MagicMock()
    response.status = status
    resp_cm.__aenter__ = AsyncMock(return_value=response)
    resp_cm.__aexit__ = AsyncMock(return_value=False)

    if raises is not None:
        session.post = MagicMock(side_effect=raises)
    else:
        session.post = MagicMock(return_value=resp_cm)
    return session


class TestPreflight:
    @pytest.mark.asyncio
    async def test_200_returns_none(self):
        session = _make_session(200)
        result = await _preflight(session, "https://home.alfred.black", "ha_" + "a" * 48)
        assert result is None

    @pytest.mark.asyncio
    async def test_202_also_succeeds(self):
        # 202 covers a server that journals + queues without blocking.
        session = _make_session(202)
        result = await _preflight(session, "https://home.alfred.black", "ha_" + "a" * 48)
        assert result is None

    @pytest.mark.asyncio
    async def test_401_returns_invalid_auth(self):
        session = _make_session(401)
        result = await _preflight(session, "https://home.alfred.black", "ha_" + "a" * 48)
        assert result == "invalid_auth"

    @pytest.mark.asyncio
    async def test_500_returns_cannot_connect(self):
        session = _make_session(500)
        result = await _preflight(session, "https://home.alfred.black", "ha_" + "a" * 48)
        assert result == "cannot_connect"

    @pytest.mark.asyncio
    async def test_404_returns_cannot_connect(self):
        # If /turn isn't there, the host is wrong or the server is stale.
        session = _make_session(404)
        result = await _preflight(session, "https://home.alfred.black", "ha_" + "a" * 48)
        assert result == "cannot_connect"

    @pytest.mark.asyncio
    async def test_connector_error_returns_cannot_connect(self):
        # Simulate DNS failure / refused connection. ClientConnectorError's
        # __init__ wants a connection_key and an OSError — easier to raise
        # the base ClientError sentinel here.
        session = _make_session(0, raises=aiohttp.ClientError("boom"))
        result = await _preflight(session, "https://nope.example", "ha_" + "a" * 48)
        assert result == "cannot_connect"

    @pytest.mark.asyncio
    async def test_timeout_returns_cannot_connect(self):
        session = _make_session(0, raises=TimeoutError())
        result = await _preflight(session, "https://home.alfred.black", "ha_" + "a" * 48)
        assert result == "cannot_connect"

    @pytest.mark.asyncio
    async def test_url_path_appends_turn_endpoint(self):
        session = _make_session(200)
        await _preflight(session, "https://home.alfred.black", "ha_" + "a" * 48)
        # First positional arg is the URL.
        called_url = session.post.call_args.args[0]
        assert called_url == "https://home.alfred.black/api/v1/channels/ha/turn"

    @pytest.mark.asyncio
    async def test_bearer_header_set(self):
        session = _make_session(200)
        await _preflight(session, "https://home.alfred.black", "ha_token")
        headers = session.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer ha_token"

    @pytest.mark.asyncio
    async def test_payload_shape_matches_ctrl_api_contract(self):
        # ctrl-api validates camelCase keys (channels_ha.ts).
        session = _make_session(200)
        await _preflight(session, "https://home.alfred.black", "ha_" + "a" * 48)
        body = session.post.call_args.kwargs["json"]
        assert set(body.keys()) >= {
            "text",
            "conversationId",
            "language",
            "agentId",
            "haInstallId",
        }

    @pytest.mark.asyncio
    async def test_default_timeout_is_default_const(self):
        # Belt-and-braces guard: if DEFAULT_TIMEOUT is ever bumped in
        # const.py, _preflight's default kwarg must move with it.
        session = _make_session(200)
        await _preflight(session, "https://home.alfred.black", "ha_" + "a" * 48)
        ct = session.post.call_args.kwargs["timeout"]
        # aiohttp.ClientTimeout exposes `.total` for the round-trip cap.
        assert ct.total == DEFAULT_TIMEOUT

    @pytest.mark.asyncio
    async def test_explicit_timeout_kwarg_honoured(self):
        # The OptionsFlow override path: caller passes `timeout=` and
        # the ClientTimeout we build MUST carry that value, not the default.
        session = _make_session(200)
        await _preflight(
            session,
            "https://home.alfred.black",
            "ha_" + "a" * 48,
            timeout=7.5,
        )
        ct = session.post.call_args.kwargs["timeout"]
        assert ct.total == 7.5
        # Sanity: explicitly NOT the default.
        assert ct.total != DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# AlfredOptionsFlow — per-entry timeout override
# ---------------------------------------------------------------------------


class _FakeEntry:
    """Minimal `ConfigEntry` stand-in carrying just `.options`."""

    def __init__(self, options: dict | None = None):
        self.options = options or {}


class TestOptionsFlow:
    @pytest.mark.asyncio
    async def test_show_form_uses_default_when_no_override(self):
        # Fresh entry, no options yet → form's `timeout` default is the
        # current `DEFAULT_TIMEOUT` (90s as of v1.1.3).
        flow = AlfredOptionsFlow(_FakeEntry())
        result = await flow.async_step_init(user_input=None)
        assert result["type"] == "form"
        assert result["step_id"] == "init"

        schema = result["data_schema"]
        # voluptuous Schema.schema → dict of {Marker: validator};
        # the default lives on the Marker.
        markers = {str(k): k for k in schema.schema}
        timeout_marker = markers[CONF_TIMEOUT]
        assert timeout_marker.default() == DEFAULT_TIMEOUT

    @pytest.mark.asyncio
    async def test_show_form_uses_existing_override(self):
        # Entry already has a stored override → that becomes the new default.
        flow = AlfredOptionsFlow(_FakeEntry({CONF_TIMEOUT: 45.0}))
        result = await flow.async_step_init(user_input=None)
        schema = result["data_schema"]
        markers = {str(k): k for k in schema.schema}
        assert markers[CONF_TIMEOUT].default() == 45.0

    @pytest.mark.asyncio
    async def test_submit_creates_entry_with_user_value(self):
        # Happy path: user enters 120s, options-flow persists it.
        flow = AlfredOptionsFlow(_FakeEntry())
        result = await flow.async_step_init(user_input={CONF_TIMEOUT: 120.0})
        assert result["type"] == "create_entry"
        assert result["data"] == {CONF_TIMEOUT: 120.0}

    @pytest.mark.asyncio
    async def test_submit_round_trips_value_unchanged(self):
        # 30 (low end) round-trips as-is — the OptionsFlow doesn't coerce
        # or clamp here; cv.positive_float in the schema is the only gate.
        flow = AlfredOptionsFlow(_FakeEntry({CONF_TIMEOUT: 90.0}))
        result = await flow.async_step_init(user_input={CONF_TIMEOUT: 30.0})
        assert result["data"] == {CONF_TIMEOUT: 30.0}
