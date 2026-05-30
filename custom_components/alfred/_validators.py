"""Pure helpers for the Alfred Black HA integration.

These helpers are intentionally HA-free so they can be unit-tested without
spinning up the full Home Assistant test harness. The `config_flow` and
`conversation` modules import from here; nothing else should.

Specifically:

- `_normalise_base_url` — trims trailing slashes, rejects obviously bad URLs.
- `_token_shape_ok` — fast local check before round-tripping a token.
- `_extract_speech` — defensively pulls the speech-plain branch from a
  ctrl-api `/turn` envelope.
- `_preflight` — POSTs a no-op turn to verify the host/token pair.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .const import API_PATH_TURN, DEFAULT_TIMEOUT, TOKEN_HEX_LEN, TOKEN_PREFIX

_LOGGER = logging.getLogger(__name__)


def _normalise_base_url(raw: str) -> str:
    """Trim trailing slashes and reject obviously malformed URLs."""
    stripped = raw.strip().rstrip("/")
    parsed = urlparse(stripped)
    if parsed.scheme not in ("https", "http"):
        raise ValueError("scheme")
    if not parsed.netloc:
        raise ValueError("netloc")
    return stripped


def _token_shape_ok(raw: str) -> bool:
    """Fast local check before round-tripping to the server.

    Channel tokens are `ha_` + 48 hex chars (see `channel_tokens.ts` in
    `ssdavidai/alfred`). This catches paste-with-spaces, prefix-only,
    and obviously-wrong-prefix mistakes cheaply.
    """
    tok = raw.strip()
    if not tok.startswith(TOKEN_PREFIX):
        return False
    body = tok[len(TOKEN_PREFIX):]
    if len(body) != TOKEN_HEX_LEN:
        return False
    try:
        int(body, 16)
    except ValueError:
        return False
    return True


def _extract_speech(envelope: Any) -> str:
    """Pull `response.speech.plain.speech` out of the ctrl-api envelope.

    Defensive: returns "" for any malformed branch rather than crashing
    the HA pipeline. The server contract guarantees the shape on 200.
    """
    if not isinstance(envelope, dict):
        return ""
    response = envelope.get("response")
    if not isinstance(response, dict):
        return ""
    speech_block = response.get("speech")
    if not isinstance(speech_block, dict):
        return ""
    plain = speech_block.get("plain")
    if not isinstance(plain, dict):
        return ""
    speech = plain.get("speech")
    if not isinstance(speech, str):
        return ""
    return speech


async def _preflight(
    session: aiohttp.ClientSession,
    base_url: str,
    token: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """Probe `{base_url}{API_PATH_TURN}` with a no-op turn.

    `timeout` is the total round-trip cap in seconds. Defaults to
    `DEFAULT_TIMEOUT`; the OptionsFlow can pass a tenant-specific value.

    Returns:
        None on success. An error key string (`invalid_auth`, `cannot_connect`,
        `invalid_url`) on failure.
    """
    url = f"{base_url}{API_PATH_TURN}"
    payload = {
        "text": "__alfred_ha_preflight__",
        "conversationId": "preflight",
        "language": "en",
        "agentId": "preflight",
        "haInstallId": "preflight",
    }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with session.post(
            url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 401:
                return "invalid_auth"
            if resp.status in (200, 202):
                return None
            _LOGGER.warning(
                "alfred: preflight to %s returned HTTP %s", url, resp.status
            )
            return "cannot_connect"
    except aiohttp.InvalidURL:
        return "invalid_url"
    except TimeoutError:
        return "cannot_connect"
    except aiohttp.ClientError as err:
        _LOGGER.warning("alfred: preflight client error: %s", err)
        return "cannot_connect"
