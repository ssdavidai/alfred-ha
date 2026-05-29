"""Alfred Black — Home Assistant conversation agent + Supervisor bridge.

This integration plugs Alfred into Home Assistant's conversation surface
(`Settings → Voice assistants → Conversation agent`). Each HA install pairs
to one Alfred tenant (a `https://<tenant>.alfred.black` host) via a
long-lived channel token minted from `/study` or `POST /api/v1/channel-tokens/mint`.

It also ships a **Supervisor bridge** (since v1.1.0): a small set of HA
services (`alfred.supervisor_*`) that forward to Supervisor REST using the
auto-injected `SUPERVISOR_TOKEN`. This gives any LLAT-bearing caller
Supervisor scope through Alfred's component — see `services.py` and the
README's "Supervisor bridge" section.

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
from .services import (
    async_register_supervisor_services,
    async_unregister_supervisor_services,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CONVERSATION]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Alfred from a config entry.

    No runtime state is stashed here — the conversation platform reads
    `entry.data` directly on each turn. Keeps the wiring trivial; PR5
    introduces a primer cache layer that will land in `hass.data[DOMAIN]`.

    Supervisor-bridge services are registered once (the first time any
    entry sets up), and stay live for the process lifetime — they're
    process-scoped, not entry-scoped, because the Supervisor token is
    process-scoped. Re-registering on a second entry is a no-op thanks
    to HA's `services.has_service` guard in `async_register`.
    """
    _LOGGER.debug(
        "alfred: setting up entry %s (base_url=%s)", entry.entry_id, entry.data.get("base_url")
    )
    domain_data = hass.data.setdefault(DOMAIN, {})

    # Register the Supervisor bridge once per HA process. The flag in
    # `hass.data[DOMAIN]` keeps us idempotent across config-entry reloads.
    if not domain_data.get("_supervisor_services_registered"):
        await async_register_supervisor_services(hass)
        domain_data["_supervisor_services_registered"] = True

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Alfred config entry.

    Supervisor services are unregistered only when the *last* Alfred
    entry goes away — they're process-scoped, not entry-scoped, so
    tearing them down on a partial unload would break a sibling entry.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and DOMAIN in hass.data:
        hass.data[DOMAIN].pop(entry.entry_id, None)

        # Count surviving Alfred entries. If none remain, tear down the
        # supervisor services so a fresh setup re-registers cleanly.
        remaining_entries = [
            e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != entry.entry_id
        ]
        if not remaining_entries and hass.data[DOMAIN].get("_supervisor_services_registered"):
            await async_unregister_supervisor_services(hass)
            hass.data[DOMAIN].pop("_supervisor_services_registered", None)
    return unloaded
