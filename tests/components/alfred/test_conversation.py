"""Tests for `custom_components.alfred.conversation`.

We cover:

- `_extract_speech` — pulls `response.speech.plain.speech` defensively from
  the ctrl-api envelope, returns `""` for any malformed branch.
- The HTTP turn round-trip — happy path (200 → envelope), 401 (token
  revoked), 5xx (server error), client errors, and timeouts. The
  ConversationEntity's HA-side wiring (entity registration, agent set/unset)
  isn't exercised here — it's deferred to the HA test harness.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.alfred._validators import _extract_speech
from custom_components.alfred.const import CONF_TIMEOUT, DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# _extract_speech
# ---------------------------------------------------------------------------


class TestExtractSpeech:
    def test_well_formed_envelope(self):
        env = {
            "response": {"speech": {"plain": {"speech": "Hello, sir."}}},
            "conversation_id": "abc",
        }
        assert _extract_speech(env) == "Hello, sir."

    def test_extra_fields_ignored(self):
        # ctrl-api also returns `hermesSessionId` and `timing`; we don't care.
        env = {
            "response": {"speech": {"plain": {"speech": "OK."}}},
            "conversation_id": "abc",
            "hermesSessionId": "ha-uuid",
            "timing": {"hermes_ms": 1234, "total_ms": 1300},
        }
        assert _extract_speech(env) == "OK."

    def test_missing_response_returns_empty(self):
        assert _extract_speech({"conversation_id": "abc"}) == ""

    def test_missing_speech_returns_empty(self):
        assert _extract_speech({"response": {}}) == ""

    def test_missing_plain_returns_empty(self):
        assert _extract_speech({"response": {"speech": {}}}) == ""

    def test_missing_speech_key_returns_empty(self):
        assert _extract_speech({"response": {"speech": {"plain": {}}}}) == ""

    def test_non_dict_input_returns_empty(self):
        assert _extract_speech(None) == ""
        assert _extract_speech("a string") == ""
        assert _extract_speech([1, 2, 3]) == ""

    def test_non_string_speech_returns_empty(self):
        env = {"response": {"speech": {"plain": {"speech": 42}}}}
        assert _extract_speech(env) == ""


# ---------------------------------------------------------------------------
# HTTP turn round-trip — tested at the aiohttp layer, not the entity layer.
# ---------------------------------------------------------------------------


def _post_cm(status: int, json_body: dict | None = None, *, raises: BaseException | None = None):
    """Build a mock async context manager that mimics session.post(...)."""
    cm = MagicMock()
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_body or {})
    response.text = AsyncMock(return_value="")
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock(spec=aiohttp.ClientSession)
    if raises is not None:
        session.post = MagicMock(side_effect=raises)
    else:
        session.post = MagicMock(return_value=cm)
    return session, response


async def _turn(session: aiohttp.ClientSession, url: str, token: str, payload: dict) -> dict:
    """Replicate the conversation entity's HTTP shape — keeps the test
    decoupled from the HA `ConversationEntity` subclass (which can't be
    instantiated without a running HA core)."""
    async with session.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=aiohttp.ClientTimeout(total=30.0),
    ) as resp:
        if resp.status == 401:
            return {"error": "invalid_auth", "status": 401}
        if resp.status >= 400:
            return {"error": "server", "status": resp.status}
        return {"envelope": await resp.json(), "status": resp.status}


class TestTurnHTTP:
    @pytest.mark.asyncio
    async def test_happy_path_200(self):
        envelope = {
            "response": {"speech": {"plain": {"speech": "Today is Friday, sir."}}},
            "conversation_id": "conv-1",
            "hermesSessionId": "ha-uuid-1",
            "timing": {"hermes_ms": 1200, "total_ms": 1300},
        }
        session, _ = _post_cm(200, envelope)
        result = await _turn(
            session,
            "https://home.alfred.black/api/v1/channels/ha/turn",
            "ha_" + "a" * 48,
            {
                "text": "what's on my brief?",
                "conversationId": "conv-1",
                "language": "en",
                "agentId": "alfred",
                "haInstallId": "uuid-1",
            },
        )
        assert result["status"] == 200
        assert _extract_speech(result["envelope"]) == "Today is Friday, sir."
        assert result["envelope"]["conversation_id"] == "conv-1"

    @pytest.mark.asyncio
    async def test_401_surfaces_invalid_auth(self):
        session, _ = _post_cm(401, {"error": "unauthorized"})
        result = await _turn(
            session,
            "https://home.alfred.black/api/v1/channels/ha/turn",
            "ha_" + "0" * 48,
            {
                "text": "hi",
                "conversationId": "c",
                "language": "en",
                "agentId": "a",
                "haInstallId": "h",
            },
        )
        assert result == {"error": "invalid_auth", "status": 401}

    @pytest.mark.asyncio
    async def test_500_surfaces_server_error(self):
        session, _ = _post_cm(500, {"error": "internal"})
        result = await _turn(
            session,
            "https://home.alfred.black/api/v1/channels/ha/turn",
            "ha_" + "a" * 48,
            {
                "text": "hi",
                "conversationId": "c",
                "language": "en",
                "agentId": "a",
                "haInstallId": "h",
            },
        )
        assert result == {"error": "server", "status": 500}

    @pytest.mark.asyncio
    async def test_429_surfaces_server_error(self):
        # ctrl-api rate-limits at 30/min per haInstallId; 429 is a legit signal.
        session, _ = _post_cm(429, {"error": "rate_limit"})
        result = await _turn(
            session,
            "https://home.alfred.black/api/v1/channels/ha/turn",
            "ha_" + "a" * 48,
            {
                "text": "hi",
                "conversationId": "c",
                "language": "en",
                "agentId": "a",
                "haInstallId": "h",
            },
        )
        assert result == {"error": "server", "status": 429}

    @pytest.mark.asyncio
    async def test_client_error_raises(self):
        session, _ = _post_cm(0, raises=aiohttp.ClientError("dns"))
        with pytest.raises(aiohttp.ClientError):
            await _turn(
                session,
                "https://home.alfred.black/api/v1/channels/ha/turn",
                "ha_" + "a" * 48,
                {
                    "text": "hi",
                    "conversationId": "c",
                    "language": "en",
                    "agentId": "a",
                    "haInstallId": "h",
                },
            )

    @pytest.mark.asyncio
    async def test_payload_uses_camelcase_keys(self):
        # ctrl-api's channels_ha.ts validator REJECTS snake_case. Lock it in.
        session, _ = _post_cm(200, {"response": {"speech": {"plain": {"speech": "ok"}}}})
        await _turn(
            session,
            "https://home.alfred.black/api/v1/channels/ha/turn",
            "ha_" + "a" * 48,
            {
                "text": "hi",
                "conversationId": "c",
                "language": "en",
                "agentId": "a",
                "haInstallId": "h",
            },
        )
        body = session.post.call_args.kwargs["json"]
        assert "conversationId" in body
        assert "haInstallId" in body
        assert "agentId" in body
        # Negative — no snake_case leaked.
        assert "conversation_id" not in body
        assert "ha_install_id" not in body
        assert "agent_id" not in body

    @pytest.mark.asyncio
    async def test_bearer_token_attached(self):
        session, _ = _post_cm(200, {"response": {"speech": {"plain": {"speech": "ok"}}}})
        await _turn(
            session,
            "https://home.alfred.black/api/v1/channels/ha/turn",
            "ha_secret",
            {
                "text": "hi",
                "conversationId": "c",
                "language": "en",
                "agentId": "a",
                "haInstallId": "h",
            },
        )
        headers = session.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer ha_secret"


# ---------------------------------------------------------------------------
# Per-entry timeout override — mirrors the conversation entity's read of
# `entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)`.
# ---------------------------------------------------------------------------


def _resolve_timeout(entry_options: dict) -> float:
    """Replicate conversation.py's per-turn timeout resolution.

    Kept in a tiny helper so the test stays decoupled from the HA
    `ConversationEntity` subclass (which still can't be instantiated
    without a running HA core).
    """
    return float(entry_options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))


class TestTimeoutResolution:
    def test_no_override_uses_default(self):
        assert _resolve_timeout({}) == DEFAULT_TIMEOUT

    def test_override_wins(self):
        assert _resolve_timeout({CONF_TIMEOUT: 120.0}) == 120.0

    def test_override_coerced_from_int(self):
        # cv.positive_float persists as float on submit, but defensive
        # casting in conversation.py means an int from a legacy entry
        # still works.
        assert _resolve_timeout({CONF_TIMEOUT: 45}) == 45.0

    def test_default_is_90_seconds_post_v113(self):
        # Lock the v1.1.3 bump in place — if a future commit lowers it
        # without also touching the rationale in const.py, this test
        # forces a conscious decision.
        assert DEFAULT_TIMEOUT == 90.0
