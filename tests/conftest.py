"""Shared fixtures for the Alfred Black HA integration tests.

These tests deliberately avoid pulling in the full
`pytest-homeassistant-custom-component` stack — that's a heavy dependency
that requires a real HA tree to import. PR2's tests target the pure pieces:

- `_validators._normalise_base_url` / `_token_shape_ok` / `_preflight`
- `_validators._extract_speech`

The integration's HA-side wiring (config-flow chrome, entity registration)
is covered by HA's own test runner — we'll add that in a CI follow-up using
`pytest-homeassistant-custom-component` once the skeleton stabilises.

To make `from custom_components.alfred._validators import ...` work in a
plain pytest environment, we stub the `homeassistant.*` modules that
`custom_components.alfred.__init__` and `config_flow` import at module
load. The stubs are sentinel objects — anything that calls into them
during test will raise loud and clear.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

# Make `custom_components.alfred.*` importable without installing.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _stub(name: str, **attrs) -> types.ModuleType:
    """Register a sentinel module under `name` with optional attributes."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# Minimal homeassistant.* stubs so `__init__.py` and `config_flow.py` can
# import without a real HA install. None of these are invoked by the unit
# tests below — they exist purely to satisfy module-load-time imports.
class _Sentinel:
    """Stand-in for HA classes we never instantiate in unit tests."""

    def __init_subclass__(cls, **kwargs):
        # Allow `class AlfredConfigFlow(ConfigFlow, domain=...)`.
        pass


class _Platform:
    CONVERSATION = "conversation"


_stub("homeassistant")
_stub("homeassistant.config_entries", ConfigEntry=_Sentinel, ConfigFlow=_Sentinel, ConfigFlowResult=dict)
_stub("homeassistant.const", Platform=_Platform, MATCH_ALL="*")


# `homeassistant.core` — stub the bits the Supervisor bridge touches.
# `ServiceCall` carries `data`; `ServiceResponse` is just a typing alias
# (dict); `SupportsResponse` is an enum-like sentinel.
class _ServiceCall:
    def __init__(self, data: dict | None = None):
        self.data = data or {}


class _SupportsResponse:
    ONLY = "only"
    OPTIONAL = "optional"
    NONE = "none"


_stub(
    "homeassistant.core",
    HomeAssistant=_Sentinel,
    ServiceCall=_ServiceCall,
    ServiceResponse=dict,
    SupportsResponse=_SupportsResponse,
)
_stub("homeassistant.helpers")
_stub(
    "homeassistant.helpers.aiohttp_client",
    async_get_clientsession=lambda hass: None,
)
_stub("homeassistant.helpers.entity_platform", AddEntitiesCallback=_Sentinel)


# `homeassistant.helpers.config_validation` — stub a couple of the
# voluptuous-adjacent helpers the services schema layer uses.
def _cv_string(value):
    if not isinstance(value, str):
        raise ValueError("expected string")
    return value


def _cv_boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    raise ValueError("expected boolean")


_cv_stub = types.ModuleType("homeassistant.helpers.config_validation")
_cv_stub.string = _cv_string
_cv_stub.boolean = _cv_boolean
sys.modules["homeassistant.helpers.config_validation"] = _cv_stub


# `homeassistant.helpers.intent` — stub the IntentResponse + error codes.
class _IntentResponseErrorCode:
    NO_INTENT_MATCH = "no_intent_match"
    FAILED_TO_HANDLE = "failed_to_handle"


class _IntentResponse:
    def __init__(self, language: str = "en"):
        self.language = language
        self.speech: str | None = None
        self.error_code: str | None = None
        self.error_message: str | None = None

    def async_set_speech(self, speech: str) -> None:
        self.speech = speech

    def async_set_error(self, code: str, message: str) -> None:
        self.error_code = code
        self.error_message = message


_stub(
    "homeassistant.helpers.intent",
    IntentResponse=_IntentResponse,
    IntentResponseErrorCode=_IntentResponseErrorCode,
)


# `homeassistant.components.conversation` — stub the ConversationEntity +
# helpers. The tests don't instantiate the entity; they exercise the HTTP
# round-trip and the pure helpers.
class _ConversationInput:
    def __init__(
        self,
        text: str = "",
        conversation_id: str | None = None,
        language: str | None = None,
        agent_id: str | None = None,
        device_id: str | None = None,
    ):
        self.text = text
        self.conversation_id = conversation_id
        self.language = language
        self.agent_id = agent_id
        self.device_id = device_id


class _ConversationResult:
    def __init__(self, response=None, conversation_id=None):
        self.response = response
        self.conversation_id = conversation_id


class _ConversationEntity(_Sentinel):
    pass


_stub(
    "homeassistant.components",
)
_stub(
    "homeassistant.components.conversation",
    ConversationEntity=_ConversationEntity,
    ConversationInput=_ConversationInput,
    ConversationResult=_ConversationResult,
    async_set_agent=lambda hass, entry, agent: None,
    async_unset_agent=lambda hass, entry: None,
)
