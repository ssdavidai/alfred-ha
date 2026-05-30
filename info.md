# Alfred Black

Plug **Alfred Black** in as Home Assistant's conversation agent. Once configured, Alfred shows up on `Settings → Voice assistants → Conversation agent` alongside *OpenAI Conversation*, *Google Generative AI*, *Anthropic*, *Ollama*. Select it as the brain for the default Assist pipeline and Alfred answers HA Assist's typed and (later) voice turns with your full Alfred tenant context — vault, briefings, decisions, matters.

## What ships in v0.1

- Config flow with two fields: Alfred tenant URL + a channel token.
- A `ConversationEntity` that round-trips each typed turn to your tenant's `ctrl-api` via `POST /api/v1/channels/ha/turn`.
- One Hermes session per HA install (continuity across conversations).

## What ships in v1.1 (Supervisor bridge)

- A set of `alfred.supervisor_*` HA services that forward to Supervisor REST using HA's auto-injected `SUPERVISOR_TOKEN`. This gives any LLAT-bearing caller Supervisor scope — addon configs, host info, OS info, and `/share/` reads/writes — without needing SSH or per-addon credentials. **Unblocks wake-word model uploads in one HTTP call.** See the README's "Supervisor bridge" section for the full surface + examples.

## What v1.1.2 adds

- Generous 30s default request timeout so a cold Hermes turn doesn't surface a false `cannot_connect`. Pairs with a server-side preflight short-circuit in `ssdavidai/alfred` so config_flow itself never waits on Hermes.

## What is deferred

- **HA tool partitioning** (`HassTurnOn`, `HassClimate`, etc. translated to Hermes tools) — issue #111 PR3.
- **Voice-context primer** (room enrichment, household primer injected into every turn) — issue #111 PR5.
- **Streaming + hassil fast-path** — issue #111 PR6.
- **Voice** is a separate integration — see `ssdavidai/alfred-ha-voice` when it lands.

## Setup

1. From your tenant's `/study` page, mint an HA channel token (or call `POST /api/v1/channel-tokens/mint` with `{"channel":"ha-conversation"}`).
2. In Home Assistant: **Settings → Devices & Services → Add integration → Alfred Black**.
3. Paste the tenant URL (e.g. `https://home.alfred.black`) and the token.
4. **Settings → Voice assistants** → set Alfred as the conversation agent.

Issue tracker for bugs: `ssdavidai/alfred` (this is the platform repo; HA-side changes live there too).
