# alfred-ha

Home Assistant integration for **Alfred Black** — Alfred plugged into HA's `ConversationEntity` surface as the LLM brain behind Assist.

When installed, "Alfred" appears on `Settings → Voice assistants → Conversation agent` alongside *OpenAI Conversation*, *Google Generative AI*, *Anthropic*, and *Ollama*. Select it as the brain for the default Assist pipeline and HA Assist's typed turns route to your Alfred tenant, answered by Hermes-main with full tenant context (vault, briefings, calendar, paperclip).

This integration is part of the plan in [`ssdavidai/alfred#111`](https://github.com/ssdavidai/alfred/issues/111). It pairs with (but is independent of) the forthcoming **alfred-ha-voice** integration, which owns the audio leg (STT/TTS via Wyoming + HA Voice satellite).

## Status

| Capability | Status | Tracking |
|---|---|---|
| HACS-installable skeleton | shipping in v0.1 | this repo (PR #1) |
| Non-streaming conversation turn → Alfred | shipping in v0.1 | this repo (PR #1) |
| **Supervisor bridge (LLAT → Supervisor REST)** | **shipping in v1.1** | this repo (PR #2) |
| Resilient config_flow preflight (30s timeout, server short-circuit) | shipping in v1.1.2 | this repo + ctrl-api short-circuit in `ssdavidai/alfred` |
| 90s default timeout for tool-using turns + per-entry override | shipping in v1.1.3 | this repo |
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

### Per-entry options (Configure)

After pairing, the Alfred entry card on **Settings → Devices & Services → Alfred Black** exposes a **Configure** button. Today it carries one knob:

- **Per-turn timeout (seconds)** — how long to wait for a single conversation turn before giving up. Defaults to **90s** in v1.1.3+ so tool-using turns (calendar lookups, vault search, integrations chained through Composio) don't surface a false `Alfred timed out.` on a cold path. Lower it on fast networks (e.g. 30s) if you want quicker failure; raise it on slow ones.

## Architecture (one-paragraph)

Each HA turn is a single non-streaming HTTPS POST from the integration to `https://<tenant>/api/v1/channels/ha/turn` with body `{text, conversationId, language, agentId, haInstallId}` and `Authorization: Bearer <token>`. ctrl-api validates the bearer against the `channel_tokens` table (channel = `ha-conversation`), rate-limits per `haInstallId` (30 turns/min), and forwards to Hermes-main with session key `ha-<haInstallId>`. Hermes' reply is wrapped in HA's `ConversationEntity` envelope (`response.speech.plain.speech`) and returned. See `packages/ctrl/src/api/routes/channels_ha.ts` in `ssdavidai/alfred` for the server side.

## Voice

Voice (STT + TTS + satellite glue) ships as a separate integration once that lands — see **`ssdavidai/alfred-ha-voice`** (not yet created). v0.1 of this integration handles typed Assist only; once the voice integration arrives, Alfred answers voice turns automatically because HA's pipeline picks the conversation agent for both surfaces.

## Supervisor bridge — call Supervisor via LLAT

Home Assistant's long-lived access tokens (LLATs) don't grant Supervisor scope — there's no way to mint an "addon API key" from inside HA. That blocks any external operator (Alfred, ad-hoc scripts) from driving addon configs, writing to `/share/`, or fetching host info without falling back to SSH or addon credentials.

Since **v1.1.0**, this integration declares `"hassio"` as a dependency, which makes HA auto-inject a Supervisor token into the integration's environment. We then re-publish a small surface of Supervisor REST as HA services (`alfred.supervisor_*`), callable via the standard `POST /api/services/<domain>/<service>` route with any LLAT.

**The result: any LLAT-bearing caller gets Supervisor scope through Alfred.** HA-side auth (the LLAT) gates *who*; the bridge gates *what* (`/share/...`, addon REST, host info, OS info).

### Services

| Service | Args | Result |
|---|---|---|
| `alfred.supervisor_call` | `{method, path, json_body?}` | `{status, body}` — generic passthrough |
| `alfred.supervisor_addon_info` | `{slug}` | `{status, body}` (addon config + options + state) |
| `alfred.supervisor_addon_options_update` | `{slug, options, restart?}` | `{status, body}` or `{options, restart}` |
| `alfred.supervisor_host_info` | — | `{status, body}` |
| `alfred.supervisor_os_info` | — | `{status, body}` |
| `alfred.supervisor_share_write` | `{path, content_base64, create_parents?}` | `{ok, path, bytes_written}` |
| `alfred.supervisor_share_read` | `{path}` | `{ok, path, size, content_base64}` |
| `alfred.supervisor_share_list` | `{path}` | `{ok, path, entries[]}` |
| `alfred.supervisor_share_delete` | `{path}` | `{ok, path}` |

All services support **`return_response=true`** — call them with `?return_response=true` (or the JSON-API `"return_response": true` field) to receive the response envelope back.

Error envelopes (`{error, ...}`) cover:

- `supervisor_unavailable` — running on HA Container / Core venv (no Supervisor); `installation_type` and `hint` accompany.
- `unsafe_path` — `/share/`-only paths; rejects `..` traversal, absolute escapes, and symlinks pointing outside `/share/`. `reason` is one of `traversal`, `outside_share_root`, `symlink_escape`, etc.
- `too_large` — write/read exceeds the 10MB cap.
- `bad_base64` — payload couldn't be decoded.
- `not_found` / `not_a_file` / `not_a_directory` / `is_a_directory` — filesystem-state mismatches.

### Examples

#### Upload an OpenWakeWord model from outside HA (bash)

```bash
HA=https://ha.example.com
LLAT=eyJhbGciOi…           # your long-lived access token

curl -sS -X POST \
  -H "Authorization: Bearer $LLAT" \
  -H "Content-Type: application/json" \
  "$HA/api/services/alfred/supervisor_share_write?return_response=true" \
  -d "$(jq -n \
        --arg path "/share/openwakeword/alfred.tflite" \
        --arg b64 "$(base64 -w0 < alfred.tflite)" \
        '{path: $path, content_base64: $b64}')"
```

Response:

```json
{
  "service_response": {
    "ok": true,
    "path": "/share/openwakeword/alfred.tflite",
    "bytes_written": 1048576
  }
}
```

#### Read addon config (python)

```python
import requests

HA = "https://ha.example.com"
LLAT = "eyJhbGciOi…"

resp = requests.post(
    f"{HA}/api/services/alfred/supervisor_addon_info",
    headers={"Authorization": f"Bearer {LLAT}"},
    params={"return_response": "true"},
    json={"slug": "core_openwakeword"},
)
print(resp.json()["service_response"]["body"])
```

#### Update addon options and restart (python)

```python
resp = requests.post(
    f"{HA}/api/services/alfred/supervisor_addon_options_update",
    headers={"Authorization": f"Bearer {LLAT}"},
    params={"return_response": "true"},
    json={
        "slug": "core_openwakeword",
        "options": {"models": ["alfred"]},
        "restart": True,
    },
)
```

#### Generic Supervisor REST passthrough (bash)

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $LLAT" \
  -H "Content-Type: application/json" \
  "$HA/api/services/alfred/supervisor_call?return_response=true" \
  -d '{"method": "GET", "path": "/supervisor/info"}'
```

### Security

These services run with **Supervisor scope** — they can read every addon config and write any file under `/share/`. The only gate between the network and Supervisor is HA's normal LLAT auth.

**Guard your LLAT accordingly.** Treat the LLAT used to call this bridge the same way you'd treat root SSH access:

- Mint a dedicated LLAT for each external caller (Alfred, scripts) so you can revoke per-caller. HA's *Profile → Long-Lived Access Tokens* shows the list.
- Don't paste the LLAT into pastebins, Discord, or AI chat windows.
- The bridge does **not** open `/share/` recursively for directory deletion (`supervisor_share_delete` rejects directories) — if you genuinely want recursive removal, use `supervisor_call` to drive whatever surface you prefer. We chose to make that an explicit two-step so a future Alfred regression can't wipe a tenant's wake-word library.

### Where it doesn't work

The bridge requires **HA OS** or **HA Supervised** (the installation types that ship Supervisor). On **HA Container** or **HA Core (venv)**, the services are still registered, but every call returns:

```json
{
  "error": "supervisor_unavailable",
  "installation_type": "container_or_core",
  "hint": "Alfred's Supervisor bridge requires HA OS or HA Supervised…"
}
```

That lets the caller branch on installation type instead of interpreting a generic 5xx.

## Issues

File integration bugs against the platform repo: [`ssdavidai/alfred`](https://github.com/ssdavidai/alfred/issues). HACS-specific install issues can go here.

## License

MIT — see `LICENSE`.
