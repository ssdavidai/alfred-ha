"""Supervisor REST bridge for Alfred Black.

HA's long-lived access tokens (LLATs) deliberately don't grant Supervisor
scope — there is no way to mint an "addon API key" from inside HA Core.
That leaves any external operator (Alfred, in our case) unable to drive
addon configs, write to `/share/`, or restart the OS without falling back
to SSH or addon credentials.

The clean answer is to expose a thin bridge from a `"hassio": true`
custom_component: HA's addon SDK auto-injects a Supervisor token into the
Core process (env var `SUPERVISOR_TOKEN`), and any custom_component that
declares the dependency can use it to call Supervisor REST. We then
re-publish each call as an HA service (`alfred.supervisor_*`), which an
LLAT-authenticated `POST /api/services/alfred/<name>` can invoke.

That gives Alfred Supervisor scope through a tightly scoped surface, with
HA's own auth model gating who can use it. **The LLAT becomes the cap.**

This module owns the low-level pieces:

- `SupervisorClient` — minimal `aiohttp` wrapper around Supervisor REST.
- `SupervisorUnavailable` — raised when `SUPERVISOR_TOKEN` isn't set
  (e.g. HA Container, HA Core in venv, dev installs).
- `safe_share_path` — resolves a user-supplied `path` against `/share/`
  with `..`-traversal blocking, symlink-escape blocking, and an absolute
  vs. relative normalisation. **Load-bearing for security.**
- `SHARE_ROOT` / `MAX_SHARE_BYTES` — the share-root jail and the size cap.

The HA-service layer lives in `services.py`. Tests live in
`tests/components/alfred/test_supervisor_*.py`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Supervisor REST base. Inside HA Core's container, Supervisor is reachable
# at `http://supervisor` via the addon network. The token is auto-injected
# by Supervisor at start-time as `SUPERVISOR_TOKEN`.
SUPERVISOR_BASE: str = "http://supervisor"
SUPERVISOR_TOKEN_ENV: str = "SUPERVISOR_TOKEN"

# Share-folder jail. HA exposes `/share/` as a writeable surface mounted
# into Core and selected addons. We pin all share-* services to this root.
SHARE_ROOT: Path = Path("/share")

# Default size cap for share read/write (bytes). 10MB covers wake-word
# `.tflite` blobs (~1MB), STT model swap files (~5MB), and addon config
# snapshots without letting a runaway caller exhaust disk.
MAX_SHARE_BYTES: int = 10 * 1024 * 1024

# Default timeout for one Supervisor REST round-trip (seconds). Most
# Supervisor calls are fast (<1s); restart/install routes can take longer
# but we cap at 60s to fail loud on a wedged Supervisor.
DEFAULT_SUPERVISOR_TIMEOUT: float = 60.0


class SupervisorUnavailable(RuntimeError):
    """Raised when the Supervisor token isn't reachable.

    This typically means HA is running in Container mode (no Supervisor),
    in a venv, or in dev. The bridge surfaces it as a clean error envelope
    so external callers can branch on installation type instead of
    interpreting a generic 5xx.
    """

    def __init__(self, installation_type: str = "unknown") -> None:
        self.installation_type = installation_type
        super().__init__(
            f"Supervisor not reachable (installation_type={installation_type})"
        )


class SharePathError(ValueError):
    """Raised when a share-* service receives an unsafe path.

    Carries the original input + the rejection reason so the service
    layer can return a precise error to the caller without leaking the
    resolved-real-path (which could reveal mount layout).
    """

    def __init__(self, original: str, reason: str) -> None:
        self.original = original
        self.reason = reason
        super().__init__(f"unsafe share path ({reason}): {original!r}")


@dataclass(frozen=True)
class SupervisorResult:
    """A Supervisor REST response normalised for HA-service emission.

    `body` is the parsed JSON if `Content-Type: application/json`, else the
    raw text. `status` is the HTTP status. Both are returned to the caller
    so they can branch on either.
    """

    status: int
    body: Any


def supervisor_token() -> str | None:
    """Return the Supervisor token from the environment, or None.

    Reads `SUPERVISOR_TOKEN` (the canonical env var Supervisor injects
    into HA Core since 2021.10; older `HASSIO_TOKEN` is no longer set).
    Returns None if the env var is missing or empty so callers can branch
    cleanly instead of `KeyError`-ing.
    """
    token = os.environ.get(SUPERVISOR_TOKEN_ENV)
    if token:
        return token
    return None


def supervisor_available() -> bool:
    """Cheap probe: do we have a Supervisor token at all?"""
    return supervisor_token() is not None


def safe_share_path(raw: str, *, share_root: Path | None = None) -> Path:
    """Resolve `raw` against `share_root` and reject any escape.

    `share_root` defaults to the module-level `SHARE_ROOT` constant —
    we resolve the default at call time (not function-definition time)
    so monkeypatching the module constant in tests Just Works.

    The rules, in order:

    1. The input is normalised by stripping leading slashes — both
       `/share/foo.tflite` and `share/foo.tflite` and `foo.tflite` all
       refer to `/share/foo.tflite`. This is the shape Alfred's tools
       tend to emit; we don't want to surprise the operator.
    2. The composed path's `resolve(strict=False)` must be under
       `share_root.resolve()`. This is `..`-traversal blocking.
    3. If any intermediate component is a symlink whose target escapes
       `share_root`, reject. We walk parents to catch the case where a
       legitimate file rooted at `/share/foo/bar` traverses through a
       `/share/foo` symlink pointing at `/etc`.
    4. The basename must be non-empty and not `.` / `..`.
    5. No NUL bytes (defensive — some kernels truncate at NUL).

    Returns the resolved `Path`, ready to open. Raises `SharePathError`
    on any rejection with a structured reason.
    """
    if share_root is None:
        share_root = SHARE_ROOT
    if not isinstance(raw, str) or not raw:
        raise SharePathError(str(raw), "empty")
    if "\x00" in raw:
        raise SharePathError(raw, "nul_byte")

    # Normalise: drop "/share/" or "share/" prefix, drop leading "/".
    stripped = raw.strip()
    if not stripped:
        raise SharePathError(raw, "empty")
    # Reject pure-traversal inputs upfront.
    if stripped in (".", ".."):
        raise SharePathError(raw, "traversal")

    # Build a canonical version of share_root for prefix comparisons. We
    # compare against BOTH the raw and the resolved form so that
    # /var/folders/... vs /private/var/folders/... (macOS) and bind-mount
    # quirks on Linux don't break legitimate inputs.
    share_root_str = str(share_root)
    share_root_resolved = share_root.resolve(strict=False)
    share_root_resolved_str = str(share_root_resolved)

    # Allow `/share/...`, `share/...`, `<share_root>/...`, or bare relative paths.
    # We DON'T accept any other absolute path — `/etc/passwd` is rejected.
    if stripped.startswith("/"):
        # Must be the literal /share root, OR our configured share_root,
        # OR the resolved form of share_root. Anything else is an escape.
        if stripped in ("/share", share_root_str, share_root_resolved_str):
            raise SharePathError(raw, "is_share_root")

        prefixes = (
            "/share/",
            share_root_str + "/",
            share_root_resolved_str + "/",
        )
        matched_prefix = next(
            (p for p in prefixes if stripped.startswith(p)), None
        )
        if matched_prefix is None:
            raise SharePathError(raw, "outside_share_root")
        tail = stripped[len(matched_prefix):]
    else:
        if stripped.startswith("share/"):
            tail = stripped[len("share/"):]
        else:
            tail = stripped

    if not tail:
        raise SharePathError(raw, "is_share_root")

    # No bare "." or ".." components in the tail.
    parts = Path(tail).parts
    for part in parts:
        if part in ("", ".", ".."):
            raise SharePathError(raw, "traversal")

    candidate = share_root / tail
    # resolve(strict=False) collapses ".." across what's on disk + virtually.
    resolved = candidate.resolve(strict=False)

    # Symlink-escape check FIRST: walk the candidate path's ancestors. If
    # any ancestor that exists is a symlink whose target escapes the root,
    # reject as `symlink_escape` (a distinct, security-loud rejection
    # reason). We deliberately probe `candidate.parents`, not the resolved
    # form, so a symlinked intermediate is caught even if its target
    # accidentally lies under the share root.
    ancestors: list[Path] = []
    cur = candidate
    while True:
        ancestors.append(cur)
        if cur == cur.parent:
            break
        cur = cur.parent
        # Stop walking when we hit the share root (whether raw or resolved).
        if cur in (share_root, share_root_resolved):
            break

    for parent in ancestors:
        if parent in (share_root, share_root_resolved):
            continue
        # `is_symlink` doesn't raise for missing files — it returns False.
        if not parent.is_symlink():
            continue
        try:
            link_target = parent.resolve(strict=False)
        except OSError as exc:
            raise SharePathError(raw, "symlink_unreadable") from exc
        try:
            link_target.relative_to(share_root_resolved)
        except ValueError as exc:
            raise SharePathError(raw, "symlink_escape") from exc

    # Then the cheaper `..`-traversal check on the resolved path. If the
    # symlink walk didn't catch an escape, this catches the remaining
    # cases (e.g. a `..` snuck through normalisation).
    try:
        resolved.relative_to(share_root_resolved)
    except ValueError as exc:
        raise SharePathError(raw, "outside_share_root") from exc

    return resolved


class SupervisorClient:
    """Thin async wrapper around Supervisor REST.

    All calls share one timeout, one bearer token, and one base URL.
    Callers (the service layer) pass `method`, `path`, optional JSON body;
    we return a `SupervisorResult` carrying status + parsed body.

    We deliberately don't raise on non-2xx — the bridge passes the status
    through so the external caller can decide. The only exception is
    transport-level (`aiohttp.ClientError`, `TimeoutError`), which we
    surface as a synthetic 599-ish `SupervisorResult` with a structured
    error body so the service layer can return a clean envelope.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        *,
        base_url: str = SUPERVISOR_BASE,
        timeout: float = DEFAULT_SUPERVISOR_TIMEOUT,
    ) -> None:
        self._session = session
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> SupervisorResult:
        """Issue one Supervisor REST call."""
        url = f"{self._base_url}{path if path.startswith('/') else '/' + path}"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with self._session.request(
                method,
                url,
                json=json_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                status = resp.status
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if "application/json" in content_type:
                    try:
                        body: Any = await resp.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        body = await _safe_text(resp)
                else:
                    body = await _safe_text(resp)
                return SupervisorResult(status=status, body=body)
        except TimeoutError:
            _LOGGER.warning(
                "alfred-supervisor: %s %s timed out after %.1fs",
                method,
                url,
                self._timeout,
            )
            return SupervisorResult(
                status=599,
                body={"error": "supervisor_timeout", "method": method, "path": path},
            )
        except aiohttp.ClientError as err:
            _LOGGER.warning("alfred-supervisor: %s %s client error: %s", method, url, err)
            return SupervisorResult(
                status=599,
                body={"error": "supervisor_unreachable", "detail": str(err)},
            )


async def _safe_text(resp: aiohttp.ClientResponse) -> str:
    """Read a response body for logging/return without raising on decode."""
    try:
        return await resp.text()
    except Exception:  # noqa: BLE001 — log-only path
        return ""
