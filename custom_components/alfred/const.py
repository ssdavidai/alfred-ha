"""Constants for the Alfred Black HA integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "alfred"

# Config-entry keys.
CONF_BASE_URL: Final = "base_url"
CONF_CHANNEL_TOKEN: Final = "channel_token"

# Options-flow keys (lives in entry.options, not entry.data).
CONF_TIMEOUT: Final = "timeout"

# ctrl-api endpoint that the integration POSTs each conversation turn to.
# Matches `packages/ctrl/src/api/routes/channels_ha.ts` in `ssdavidai/alfred`
# (issue #111 PR1). The integration MUST keep this path in lockstep with the
# server; the server's tests and the integration's tests assert the same.
API_PATH_TURN: Final = "/api/v1/channels/ha/turn"

# Default timeout for one /turn round-trip (seconds). Bumped 30 → 90 in
# v1.1.3 after Sir's first real Assist test ("What's on my calendar
# tomorrow?") timed out at exactly 31s — Hermes-main was mid-tool-call
# through Composio gcal when the integration gave up.
#
# Tool-using turns routinely take 25–45s on a cold path because ctrl-api
# proxies to Hermes which proxies to the upstream tool (Composio, vault
# search, paperclip). Preflight is already short-circuited in ctrl-api
# (alfred#174) so a large timeout here only affects real Assist turns,
# not setup. PR6 will lower this once streaming is in place.
#
# Per-entry override: an OptionsFlow exposes `CONF_TIMEOUT` so a future
# operator on a fast network can lower it, or on a slow one can raise it,
# without needing a new release. The conversation entity and the
# preflight helper both honour `entry.options.get(CONF_TIMEOUT,
# DEFAULT_TIMEOUT)`.
DEFAULT_TIMEOUT: Final = 90.0

# Bearer-token shape: tokens minted via /api/v1/channel-tokens/mint with
# channel='ha-conversation' are prefixed `ha_` followed by 48 hex chars
# (see `packages/ctrl/src/db/channelTokens.ts` — sha256 of the raw is what's
# stored). The config_flow uses this prefix to give the operator a fast
# "paste check" error before round-tripping to the server.
TOKEN_PREFIX: Final = "ha_"
TOKEN_HEX_LEN: Final = 48
