"""HA service registrations for Alfred Black's Supervisor bridge.

Each service here becomes callable via:

    POST /api/services/alfred/<name>
    Authorization: Bearer <LLAT>
    Content-Type: application/json
    {... service-data ...}

…using HA's standard `POST /api/services/<domain>/<service>` route.
**This is the seam that gives any LLAT-bearing caller Supervisor scope
through Alfred** — HA-side auth (the LLAT) gates *who*; the bridge gates
*what* (`/share/...`, addon REST, host info, etc.).

Service surface:

| Service                                  | What it does |
|------------------------------------------|--------------|
| `alfred.supervisor_call`                 | generic REST passthrough — the escape hatch |
| `alfred.supervisor_addon_info`           | GET `/addons/<slug>/info` |
| `alfred.supervisor_addon_options_update` | POST `/addons/<slug>/options` (+ optional restart) |
| `alfred.supervisor_host_info`            | GET `/host/info` |
| `alfred.supervisor_os_info`              | GET `/os/info` |
| `alfred.supervisor_share_write`          | write base64 → `/share/<path>` |
| `alfred.supervisor_share_read`           | read `/share/<path>` → base64 |
| `alfred.supervisor_share_list`           | list directory under `/share/` |
| `alfred.supervisor_share_delete`         | delete a file under `/share/` |

Every service returns a JSON envelope via `ServiceResponse` (HA's typed
service-call return type). Errors are returned as `{error, ...}` rather
than raised — raising leaks the traceback into HA's log and the caller
gets an opaque 500.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .supervisor import (
    MAX_SHARE_BYTES,
    SHARE_ROOT,
    SharePathError,
    SupervisorClient,
    SupervisorUnavailable,
    safe_share_path,
    supervisor_available,
    supervisor_token,
)

_LOGGER = logging.getLogger(__name__)

# Service names — kept here as constants so callers (and tests) can
# import them without typo-magic strings.
SVC_SUPERVISOR_CALL = "supervisor_call"
SVC_SUPERVISOR_ADDON_INFO = "supervisor_addon_info"
SVC_SUPERVISOR_ADDON_OPTIONS_UPDATE = "supervisor_addon_options_update"
SVC_SUPERVISOR_HOST_INFO = "supervisor_host_info"
SVC_SUPERVISOR_OS_INFO = "supervisor_os_info"
SVC_SUPERVISOR_SHARE_WRITE = "supervisor_share_write"
SVC_SUPERVISOR_SHARE_READ = "supervisor_share_read"
SVC_SUPERVISOR_SHARE_LIST = "supervisor_share_list"
SVC_SUPERVISOR_SHARE_DELETE = "supervisor_share_delete"

ALL_SUPERVISOR_SERVICES = (
    SVC_SUPERVISOR_CALL,
    SVC_SUPERVISOR_ADDON_INFO,
    SVC_SUPERVISOR_ADDON_OPTIONS_UPDATE,
    SVC_SUPERVISOR_HOST_INFO,
    SVC_SUPERVISOR_OS_INFO,
    SVC_SUPERVISOR_SHARE_WRITE,
    SVC_SUPERVISOR_SHARE_READ,
    SVC_SUPERVISOR_SHARE_LIST,
    SVC_SUPERVISOR_SHARE_DELETE,
)

# Schemas — voluptuous for the HA service layer. Each schema only
# validates *shape*; semantic checks (path safety, size caps) happen
# inside the handler so we can return a structured error envelope
# rather than a vol.Invalid traceback.
_METHOD = vol.In(["GET", "POST", "PUT", "PATCH", "DELETE"])

SCHEMA_SUPERVISOR_CALL = vol.Schema(
    {
        vol.Required("method"): _METHOD,
        vol.Required("path"): cv.string,
        vol.Optional("json_body"): dict,
    }
)

SCHEMA_ADDON_INFO = vol.Schema({vol.Required("slug"): cv.string})

SCHEMA_ADDON_OPTIONS_UPDATE = vol.Schema(
    {
        vol.Required("slug"): cv.string,
        vol.Required("options"): dict,
        vol.Optional("restart", default=False): cv.boolean,
    }
)

SCHEMA_NO_ARGS = vol.Schema({})

SCHEMA_SHARE_WRITE = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Required("content_base64"): cv.string,
        # mkdir parents by default; operators occasionally want to fail
        # loud when writing into a path that should already exist.
        vol.Optional("create_parents", default=True): cv.boolean,
    }
)

SCHEMA_SHARE_READ = vol.Schema({vol.Required("path"): cv.string})
SCHEMA_SHARE_LIST = vol.Schema({vol.Required("path"): cv.string})
SCHEMA_SHARE_DELETE = vol.Schema({vol.Required("path"): cv.string})


def _unavailable_response(reason: str = "supervisor_unavailable") -> dict[str, Any]:
    """Build the standard "no supervisor" error envelope.

    Returning a structured envelope (rather than raising) lets external
    callers branch on `installation_type` and tell the user "this only
    works on HAOS/Supervised" without parsing a 500.
    """
    installation_type = "container_or_core"
    return {
        "error": reason,
        "installation_type": installation_type,
        "hint": (
            "Alfred's Supervisor bridge requires HA OS or HA Supervised "
            "(the addon-aware installation types). HA Container and HA "
            "Core venv installs don't expose Supervisor REST."
        ),
    }


async def _supervisor_client(hass: HomeAssistant) -> SupervisorClient | None:
    """Build a SupervisorClient if a token is present, else None."""
    token = supervisor_token()
    if not token:
        return None
    session = async_get_clientsession(hass)
    return SupervisorClient(session, token)


def _result(status: int, body: Any) -> dict[str, Any]:
    """Uniform success envelope for passthrough services."""
    return {"status": status, "body": body}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_supervisor_call(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Generic passthrough — the escape hatch for any Supervisor route.

    Useful when Supervisor adds a new endpoint and we don't want to ship
    a per-endpoint service. Carries the full status + body back to the
    caller so they can branch.
    """
    client = await _supervisor_client(hass)
    if client is None:
        return _unavailable_response()
    method: str = call.data["method"]
    path: str = call.data["path"]
    if not path.startswith("/"):
        path = "/" + path
    json_body = call.data.get("json_body")
    result = await client.request(method, path, json_body=json_body)
    return _result(result.status, result.body)


async def _handle_addon_info(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """GET /addons/<slug>/info — current config + options + state."""
    client = await _supervisor_client(hass)
    if client is None:
        return _unavailable_response()
    slug = call.data["slug"]
    result = await client.request("GET", f"/addons/{slug}/info")
    return _result(result.status, result.body)


async def _handle_addon_options_update(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """POST /addons/<slug>/options — set options + (optionally) restart.

    Two REST calls in sequence when `restart=True`. We don't try to be
    clever about idempotency — the caller decides what `options` to send.
    """
    client = await _supervisor_client(hass)
    if client is None:
        return _unavailable_response()
    slug = call.data["slug"]
    options = call.data["options"]
    restart = call.data.get("restart", False)

    set_result = await client.request(
        "POST",
        f"/addons/{slug}/options",
        json_body={"options": options},
    )
    if not restart or set_result.status >= 400:
        return _result(set_result.status, set_result.body)

    restart_result = await client.request("POST", f"/addons/{slug}/restart")
    return {
        "options": _result(set_result.status, set_result.body),
        "restart": _result(restart_result.status, restart_result.body),
    }


async def _handle_host_info(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """GET /host/info."""
    client = await _supervisor_client(hass)
    if client is None:
        return _unavailable_response()
    result = await client.request("GET", "/host/info")
    return _result(result.status, result.body)


async def _handle_os_info(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """GET /os/info."""
    client = await _supervisor_client(hass)
    if client is None:
        return _unavailable_response()
    result = await client.request("GET", "/os/info")
    return _result(result.status, result.body)


async def _handle_share_write(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Write base64-encoded content to a path under /share/.

    This is the wake-word upload unblock. The Alfred caller base64s the
    `.tflite`, POSTs it via `POST /api/services/alfred/supervisor_share_write`,
    and the file lands at `/share/openwakeword/alfred.tflite` ready for
    the OpenWakeWord addon to pick up.
    """
    raw_path: str = call.data["path"]
    create_parents: bool = call.data.get("create_parents", True)

    # /share/ must already exist on a supported install. If not, the
    # caller is on Container HA and we treat it as supervisor-unavailable.
    # Check this BEFORE path validation so a Container-HA caller sees the
    # installation-type error instead of a generic "unsafe_path" message.
    if not SHARE_ROOT.exists():
        return _unavailable_response("share_root_missing")

    try:
        target = safe_share_path(raw_path, share_root=SHARE_ROOT)
    except SharePathError as exc:
        return {"error": "unsafe_path", "reason": exc.reason, "path": raw_path}

    try:
        content_bytes = base64.b64decode(call.data["content_base64"], validate=True)
    except (ValueError, TypeError) as exc:
        return {"error": "bad_base64", "detail": str(exc)}

    if len(content_bytes) > MAX_SHARE_BYTES:
        return {
            "error": "too_large",
            "size": len(content_bytes),
            "max": MAX_SHARE_BYTES,
        }

    def _write() -> dict[str, Any]:
        if create_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically: tmp file + rename inside the same dir so the
        # OpenWakeWord addon (which watches for full files) never sees a
        # half-flushed file.
        tmp = target.with_suffix(target.suffix + ".alfred-tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(content_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except Exception:
            # Best-effort cleanup of the tmp on failure.
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return {
            "ok": True,
            "path": str(target),
            "bytes_written": len(content_bytes),
        }

    try:
        return await hass.async_add_executor_job(_write)
    except OSError as exc:
        return {"error": "write_failed", "detail": str(exc)}


async def _handle_share_read(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Read a path under /share/ and return it base64-encoded.

    Size-capped at `MAX_SHARE_BYTES` so a runaway caller can't pull a
    multi-GB log.
    """
    raw_path: str = call.data["path"]
    if not SHARE_ROOT.exists():
        return _unavailable_response("share_root_missing")
    try:
        target = safe_share_path(raw_path, share_root=SHARE_ROOT)
    except SharePathError as exc:
        return {"error": "unsafe_path", "reason": exc.reason, "path": raw_path}

    def _read() -> dict[str, Any]:
        if not target.exists():
            return {"error": "not_found", "path": str(target)}
        if not target.is_file():
            return {"error": "not_a_file", "path": str(target)}
        size = target.stat().st_size
        if size > MAX_SHARE_BYTES:
            return {
                "error": "too_large",
                "size": size,
                "max": MAX_SHARE_BYTES,
            }
        with open(target, "rb") as fh:
            data = fh.read(MAX_SHARE_BYTES + 1)
        # Guard against TOCTOU file-grew-during-read.
        if len(data) > MAX_SHARE_BYTES:
            return {
                "error": "too_large",
                "size": len(data),
                "max": MAX_SHARE_BYTES,
            }
        return {
            "ok": True,
            "path": str(target),
            "size": len(data),
            "content_base64": base64.b64encode(data).decode("ascii"),
        }

    try:
        return await hass.async_add_executor_job(_read)
    except OSError as exc:
        return {"error": "read_failed", "detail": str(exc)}


async def _handle_share_list(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """List a directory under /share/.

    Returns `{ok, path, entries: [{name, is_dir, is_file, is_symlink, size}]}`
    so the caller can render a useful listing without a follow-up stat call.
    """
    raw_path: str = call.data["path"]
    if not SHARE_ROOT.exists():
        return _unavailable_response("share_root_missing")
    try:
        target = safe_share_path(raw_path, share_root=SHARE_ROOT)
    except SharePathError as exc:
        return {"error": "unsafe_path", "reason": exc.reason, "path": raw_path}

    def _list() -> dict[str, Any]:
        if not target.exists():
            return {"error": "not_found", "path": str(target)}
        if not target.is_dir():
            return {"error": "not_a_directory", "path": str(target)}
        entries: list[dict[str, Any]] = []
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            try:
                stat = child.lstat()
                size = stat.st_size if not child.is_symlink() else 0
            except OSError:
                size = 0
            entries.append(
                {
                    "name": child.name,
                    "is_dir": child.is_dir(),
                    "is_file": child.is_file(),
                    "is_symlink": child.is_symlink(),
                    "size": size,
                }
            )
        return {"ok": True, "path": str(target), "entries": entries}

    try:
        return await hass.async_add_executor_job(_list)
    except OSError as exc:
        return {"error": "list_failed", "detail": str(exc)}


async def _handle_share_delete(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Delete a file under /share/. Directories are intentionally unsupported.

    Removing a directory tree from a remote service feels like the kind of
    thing a future Alfred regression could wipe a tenant's wake-word
    library with. Operators who actually want directory-level cleanup can
    use `supervisor_call` to drive whatever surface they prefer.
    """
    raw_path: str = call.data["path"]
    if not SHARE_ROOT.exists():
        return _unavailable_response("share_root_missing")
    try:
        target = safe_share_path(raw_path, share_root=SHARE_ROOT)
    except SharePathError as exc:
        return {"error": "unsafe_path", "reason": exc.reason, "path": raw_path}

    def _delete() -> dict[str, Any]:
        if not target.exists():
            return {"error": "not_found", "path": str(target)}
        if target.is_dir():
            return {"error": "is_a_directory", "path": str(target)}
        target.unlink()
        return {"ok": True, "path": str(target)}

    try:
        return await hass.async_add_executor_job(_delete)
    except OSError as exc:
        return {"error": "delete_failed", "detail": str(exc)}


# ---------------------------------------------------------------------------
# Registration / teardown
# ---------------------------------------------------------------------------


async def async_register_supervisor_services(hass: HomeAssistant) -> None:
    """Register all Supervisor-bridge services with HA's service registry.

    Idempotent — re-registering on a config-entry reload would replace
    handlers cleanly. We log once at INFO so the operator's log makes it
    obvious the bridge is live + tells them whether Supervisor was found.
    """
    if not supervisor_available():
        _LOGGER.info(
            "alfred: Supervisor token not present (running on HA Container or "
            "Core venv?). Registering supervisor_* services anyway — they will "
            "return {error: 'supervisor_unavailable'} on call."
        )
    else:
        _LOGGER.info(
            "alfred: Supervisor token detected; supervisor_* services live."
        )

    # Bind a wrapper-per-handler that closes over `hass`. HA passes
    # `ServiceCall` to the handler; the closure injects `hass` so the
    # handlers stay easy to unit-test in isolation.
    def _bind(fn):
        async def _wrapped(call: ServiceCall) -> ServiceResponse:
            return await fn(hass, call)

        return _wrapped

    hass.services.async_register(
        DOMAIN,
        SVC_SUPERVISOR_CALL,
        _bind(_handle_supervisor_call),
        schema=SCHEMA_SUPERVISOR_CALL,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SVC_SUPERVISOR_ADDON_INFO,
        _bind(_handle_addon_info),
        schema=SCHEMA_ADDON_INFO,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SVC_SUPERVISOR_ADDON_OPTIONS_UPDATE,
        _bind(_handle_addon_options_update),
        schema=SCHEMA_ADDON_OPTIONS_UPDATE,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SVC_SUPERVISOR_HOST_INFO,
        _bind(_handle_host_info),
        schema=SCHEMA_NO_ARGS,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SVC_SUPERVISOR_OS_INFO,
        _bind(_handle_os_info),
        schema=SCHEMA_NO_ARGS,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SVC_SUPERVISOR_SHARE_WRITE,
        _bind(_handle_share_write),
        schema=SCHEMA_SHARE_WRITE,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SVC_SUPERVISOR_SHARE_READ,
        _bind(_handle_share_read),
        schema=SCHEMA_SHARE_READ,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SVC_SUPERVISOR_SHARE_LIST,
        _bind(_handle_share_list),
        schema=SCHEMA_SHARE_LIST,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SVC_SUPERVISOR_SHARE_DELETE,
        _bind(_handle_share_delete),
        schema=SCHEMA_SHARE_DELETE,
        supports_response=SupportsResponse.ONLY,
    )


async def async_unregister_supervisor_services(hass: HomeAssistant) -> None:
    """Unregister all Supervisor-bridge services on unload."""
    for name in ALL_SUPERVISOR_SERVICES:
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)
