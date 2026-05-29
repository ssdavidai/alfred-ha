# alfred-ha

Home Assistant integration for **Alfred Black** — Alfred plugged into HA's `ConversationEntity` surface as the LLM brain behind Assist.

When installed, "Alfred" appears on `Settings → Voice assistants → Conversation agent` alongside *OpenAI Conversation*, *Google Generative AI*, *Anthropic*, and *Ollama*. Select it as the brain for the default Assist pipeline and HA Assist's typed turns route to your Alfred tenant, answered by Hermes-main with full tenant context (vault, briefings, calendar, paperclip).

This integration is part of the plan in [`ssdavidai/alfred#111`](https://github.com/ssdavidai/alfred/issues/111). It pairs with (but is independent of) the forthcoming **alfred-ha-voice** integration, which owns the audio leg (STT/TTS via Wyoming + HA Voice satellite).

## Status

| Capability | Status | Tracking |
|---|---|---|
| HACS-installable skeleton | shipping in v0.1 | this repo (PR #1) |
| Non-streaming conversation turn → Alfred | shipping in v0.1 | this repo (PR #1) |
| HA tool partitioning (`HassTurnOn`, `HassClimate`, …) | not yet | `ssdavidai/alfred#111` PR3 |
| Curated MCP catalog per HA turn | not yet | `ssdavidai/alfred#111` PR4 |
| Voice-context primer + room enrichment | not yet | `ssdavidai/alfred#111` PR5 |
| Streaming + hassil fast-path | not yet | `ssdavidai/alfred#111` PR6 |
| HACS public catalog submission | not yet | tracked post-v0.1 |

## Install (HACS custom repository)

This integration is not yet in the public HACS catalog. Add it as a custom repository.

1. In Home Assistant, open **HACS → Integrations**.
2. Click the **⋮** menu (top right) → **Custom repositories**.
3. **Repository:** `https://github.com/ssdavidai/alfred-ha`
4. **Category:** `Integration`
5. Click **Add**. Alfred Black now appears in HACS — click it, then **Download**.
6. Restart Home Assistant.

## Configure

Before configuring, mint a channel token from your Alfred tenant.

- **Via the UI:** open `https://<your-tenant>.alfred.black/study` → *Channel tokens* → mint a new one for channel `ha-conversation`. The raw token is shown **exactly once** — copy it now.
- **Via the API:** `POST /api/v1/channel-tokens/mint` with `{"channel":"ha-conversation","label":"My HA"}` and your operator key. The response includes the raw token under `token`.

Then in Home Assistant:

1. **Settings → Devices & Services → Add Integration → Alfred Black**.
2. **Alfred tenant URL:** the full origin, e.g. `https://home.alfred.black` (or your Tailscale MagicDNS host for non-public tenants).
3. **Channel token:** the `ha_…` token you just minted.
4. Hit **Submit**. The integration runs a preflight against `/api/v1/channels/ha/turn`; you'll see an inline error if either field is wrong.

Finally, set Alfred as your conversation agent on **Settings → Voice assistants → Assist → Conversation agent**.

## Architecture (one-paragraph)

Each HA turn is a single non-streaming HTTPS POST from the integration to `https://<tenant>/api/v1/channels/ha/turn` with body `{text, conversationId, language, agentId, haInstallId}` and `Authorization: Bearer <token>`. ctrl-api validates the bearer against the `channel_tokens` table (channel = `ha-conversation`), rate-limits per `haInstallId` (30 turns/min), and forwards to Hermes-main with session key `ha-<haInstallId>`. Hermes' reply is wrapped in HA's `ConversationEntity` envelope (`response.speech.plain.speech`) and returned. See `packages/ctrl/src/api/routes/channels_ha.ts` in `ssdavidai/alfred` for the server side.

## Voice

Voice (STT + TTS + satellite glue) ships as a separate integration once that lands — see **`ssdavidai/alfred-ha-voice`** (not yet created). v0.1 of this integration handles typed Assist only; once the voice integration arrives, Alfred answers voice turns automatically because HA's pipeline picks the conversation agent for both surfaces.

## Issues

File integration bugs against the platform repo: [`ssdavidai/alfred`](https://github.com/ssdavidai/alfred/issues). HACS-specific install issues can go here.

## License

MIT — see `LICENSE`.
