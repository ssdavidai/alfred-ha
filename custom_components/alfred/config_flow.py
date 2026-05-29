"""Config flow for Alfred Black.

Two fields, no surprises:

- `base_url` — the tenant root (e.g. `https://home.alfred.black`).
- `channel_token` — a long-lived bearer minted from
  `POST /api/v1/channel-tokens/mint` with channel='ha-conversation'.

The flow validates the pair by POSTing a synthetic preflight turn to
`{base_url}/api/v1/channels/ha/turn`. A 200 means we're paired; a 401
means the token is wrong; anything else is surfaced as `cannot_connect`.

Pure helpers (URL normalisation, token shape check, preflight) live in
`_validators.py` so they're unit-testable without an HA harness.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ._validators import _normalise_base_url, _preflight, _token_shape_ok
from .const import CONF_BASE_URL, CONF_CHANNEL_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL): str,
        vol.Required(CONF_CHANNEL_TOKEN): str,
    }
)


class AlfredConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Alfred Black."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initial step — collect base_url + channel_token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                base_url = _normalise_base_url(user_input[CONF_BASE_URL])
            except ValueError:
                errors[CONF_BASE_URL] = "invalid_url"
                base_url = user_input[CONF_BASE_URL]

            token = user_input[CONF_CHANNEL_TOKEN].strip()
            if not _token_shape_ok(token):
                errors[CONF_CHANNEL_TOKEN] = "invalid_token_shape"

            if not errors:
                # Unique-by-host so an operator can't pair the same tenant twice.
                await self.async_set_unique_id(base_url.lower())
                self._abort_if_unique_id_configured()

                session = async_get_clientsession(self.hass)
                error_key = await _preflight(session, base_url, token)
                if error_key is None:
                    return self.async_create_entry(
                        title=urlparse(base_url).netloc or "Alfred",
                        data={
                            CONF_BASE_URL: base_url,
                            CONF_CHANNEL_TOKEN: token,
                        },
                    )
                if error_key == "invalid_auth":
                    errors[CONF_CHANNEL_TOKEN] = "invalid_auth"
                elif error_key == "invalid_url":
                    errors[CONF_BASE_URL] = "invalid_url"
                else:
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
