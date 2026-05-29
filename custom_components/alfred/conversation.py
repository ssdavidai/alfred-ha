"""Alfred Black conversation entity.

PR2 ships a non-streaming `ConversationEntity` that POSTs each turn to
`{base_url}/api/v1/channels/ha/turn` and returns the speech-plain envelope.
Streaming is deferred to PR6 per spec §3.7.

The server's request/response contract is the one in
`packages/ctrl/src/api/routes/channels_ha.ts` (issue #111 PR1):

- request body (camelCase): {text, conversationId, language, agentId, haInstallId}
- response envelope: {response: {speech: {plain: {speech: "..."}}}, conversation_id, ...}

If the server adds fields later (e.g. `timing`, `hermesSessionId`) we ignore
them — only the speech-plain and conversation_id branches are load-bearing.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.components import conversation
from homeassistant.components.conversation import ConversationEntity, ConversationInput, ConversationResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import intent

from ._validators import _extract_speech
from .const import (
    API_PATH_TURN,
    CONF_BASE_URL,
    CONF_CHANNEL_TOKEN,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Alfred conversation entity from a config entry."""
    async_add_entities([AlfredConversationEntity(entry)])


class AlfredConversationEntity(ConversationEntity):
    """The Alfred Black conversation agent.

    One entity per config entry (one HA install per Alfred tenant). The
    `agent_id` HA assigns is opaque from Alfred's perspective; per-install
    continuity is keyed off `haInstallId` (== `hass.data['core.uuid']`
    fetched at turn-time), which ctrl-api uses as the Hermes session key.
    """

    _attr_has_entity_name = True
    _attr_name = "Alfred"
    _attr_supports_streaming = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialise from the persisted entry data."""
        self._entry = entry
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> list[str] | str:
        """Languages we'll accept turns in.

        Alfred is multilingual at the Hermes layer; we pass `language`
        through unchanged in the payload. MATCH_ALL signals to HA's
        pipeline picker that we don't constrain language at this layer.
        """
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """Register as an agent once the entity is attached."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self._entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister when the entity is being removed."""
        conversation.async_unset_agent(self.hass, self._entry)
        await super().async_will_remove_from_hass()

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        """Handle one conversation turn.

        Non-streaming: POST → parse envelope → return.
        """
        text = (user_input.text or "").strip()
        if not text:
            return _error_result(
                intent.IntentResponseErrorCode.NO_INTENT_MATCH,
                "Empty input.",
                user_input.conversation_id,
                user_input.language,
            )

        base_url: str = self._entry.data[CONF_BASE_URL]
        token: str = self._entry.data[CONF_CHANNEL_TOKEN]
        ha_install_id = await _resolve_ha_install_id(self.hass)

        payload: dict[str, Any] = {
            "text": text,
            "conversationId": user_input.conversation_id or "default",
            "language": user_input.language or "en",
            "agentId": user_input.agent_id or "alfred",
            "haInstallId": ha_install_id,
        }
        # `device_id` is None for typed Assist; PR5 wires room enrichment.
        if user_input.device_id:
            payload["deviceId"] = user_input.device_id

        url = f"{base_url}{API_PATH_TURN}"
        session = async_get_clientsession(self.hass)

        try:
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            ) as resp:
                if resp.status == 401:
                    return _error_result(
                        intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                        "Alfred token revoked or invalid — re-pair from /channels.",
                        user_input.conversation_id,
                        user_input.language,
                    )
                if resp.status >= 400:
                    body = await _safe_text(resp)
                    _LOGGER.warning(
                        "alfred: /turn returned HTTP %s: %s", resp.status, body[:200]
                    )
                    return _error_result(
                        intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                        f"Alfred returned HTTP {resp.status}.",
                        user_input.conversation_id,
                        user_input.language,
                    )
                envelope = await resp.json()
        except TimeoutError:
            return _error_result(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                "Alfred timed out.",
                user_input.conversation_id,
                user_input.language,
            )
        except aiohttp.ClientError as err:
            _LOGGER.warning("alfred: /turn client error: %s", err)
            return _error_result(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                "Could not reach Alfred.",
                user_input.conversation_id,
                user_input.language,
            )

        speech = _extract_speech(envelope)
        conversation_id = (
            envelope.get("conversation_id")
            if isinstance(envelope, dict)
            else None
        ) or user_input.conversation_id

        response = intent.IntentResponse(language=user_input.language or "en")
        response.async_set_speech(speech or "")
        return ConversationResult(
            response=response,
            conversation_id=conversation_id,
        )


async def _safe_text(resp: aiohttp.ClientResponse) -> str:
    """Read a response body for logging without raising on decode errors."""
    try:
        return await resp.text()
    except Exception:  # noqa: BLE001 — log-only path
        return "<unreadable>"


async def _resolve_ha_install_id(hass: HomeAssistant) -> str:
    """Return a stable identifier for this HA install.

    HA exposes `hass.data['core.uuid']` for installs created from
    2022.6+. We fall back to a constant `"unknown"` so dev installs
    that haven't been migrated still surface in audit. ctrl-api uses
    this as the Hermes session key — stability matters more than
    uniqueness across forks.
    """
    uuid = hass.data.get("core.uuid")
    if isinstance(uuid, str) and uuid:
        return uuid
    return "unknown"


def _error_result(
    code: intent.IntentResponseErrorCode,
    message: str,
    conversation_id: str | None,
    language: str | None,
) -> ConversationResult:
    """Construct a ConversationResult with an error response."""
    response = intent.IntentResponse(language=language or "en")
    response.async_set_error(code, message)
    return ConversationResult(response=response, conversation_id=conversation_id)
