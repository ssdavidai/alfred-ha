"""Constants for the Alfred Black HA integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "alfred"

# Config-entry keys.
CONF_BASE_URL: Final = "base_url"
CONF_CHANNEL_TOKEN: Final = "channel_token"

# ctrl-api endpoint that the integration POSTs each conversation turn to.
# Matches `packages/ctrl/src/api/routes/channels_ha.ts` in `ssdavidai/alfred`
# (issue #111 PR1). The integration MUST keep this path in lockstep with the
# server; the server's tests and the integration's tests assert the same.
API_PATH_TURN: Final = "/api/v1/channels/ha/turn"

# Default timeout for one /turn round-trip (seconds). Hermes-main answers
# in 1–3s typically; 30s covers slow vault searches and the occasional
# cold-start. PR6 will lower this once streaming is in place.
#
# Belt-and-braces with the ctrl-api preflight short-circuit
# (ssdavidai/alfred — fix(ctrl): /channels/ha/turn — short-circuit
# alfred-ha preflight): the server now replies in <100ms for the
# preflight magic text, so config_flow never waits on Hermes. But a
# generous timeout still protects every OTHER turn — e.g. if Hermes is
# mid-restart, a real /turn from HA Assist should wait rather than
# surface `cannot_connect` to the operator.
DEFAULT_TIMEOUT: Final = 30.0

# Bearer-token shape: tokens minted via /api/v1/channel-tokens/mint with
# channel='ha-conversation' are prefixed `ha_` followed by 48 hex chars
# (see `packages/ctrl/src/db/channelTokens.ts` — sha256 of the raw is what's
# stored). The config_flow uses this prefix to give the operator a fast
# "paste check" error before round-tripping to the server.
TOKEN_PREFIX: Final = "ha_"
TOKEN_HEX_LEN: Final = 48
