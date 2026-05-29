"""Alfred Black — Home Assistant conversation agent.

This integration plugs Alfred into Home Assistant's conversation surface
(`Settings → Voice assistants → Conversation agent`). Each HA install pairs
to one Alfred tenant (a `https://<tenant>.alfred.black` host) via a
long-lived channel token minted from `/study` or `POST /api/v1/channel-tokens/mint`.

PR2 ships the skeleton — config flow + non-streaming `ConversationEntity`.
PR3 adds HA tool partitioning; PR5 adds voice-context primer; PR6 adds
streaming + a hassil fast-path. See `ssdavidai/alfred#111` for the full plan.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CONVERSATION]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Alfred from a config entry.

    No runtime state is stashed here — the conversation platform reads
    `entry.data` directly on each turn. Keeps the wiring trivial; PR5
    introduces a primer cache layer that will land in `hass.data[DOMAIN]`.
    """
    _LOGGER.debug(
        "alfred: setting up entry %s (base_url=%s)", entry.entry_id, entry.data.get("base_url")
    )
    hass.data.setdefault(DOMAIN, {})
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Alfred config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and DOMAIN in hass.data:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
