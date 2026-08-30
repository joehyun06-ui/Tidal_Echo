"""Side-effect-free authoritative provider status projection for P3 Web sessions.

P3 has per-session provider authority, so there is no single global generation
provider.  This module combines the already-authoritative Web session projection
with the public provider capability contract without consulting UI state, titles,
account RPCs, or the Codex App Server.
"""

from __future__ import annotations

from collections.abc import Mapping


API_PROVIDER = "api"
CODEX_PROVIDER = "codex"
VALID_PROVIDERS = frozenset({API_PROVIDER, CODEX_PROVIDER})
ERROR_CATEGORY = "p3_provider_status_unavailable"


class P3ProviderStatusError(RuntimeError):
    """Fixed, data-free status projection failure."""

    def __init__(self, category: str = ERROR_CATEGORY):
        super().__init__(category)
        self.category = category


def _raise() -> None:
    raise P3ProviderStatusError()


def _project_capabilities(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        _raise()
    if payload.get("ok") is not True or payload.get("contract_version") != 1:
        _raise()
    web = payload.get("web_sessions")
    if not isinstance(web, Mapping):
        _raise()
    if web.get("default_provider") != API_PROVIDER or web.get("provider_immutable") is not True:
        _raise()
    providers = web.get("providers")
    if not isinstance(providers, Mapping) or set(providers) != VALID_PROVIDERS:
        _raise()
    api = providers.get(API_PROVIDER)
    codex = providers.get(CODEX_PROVIDER)
    if not isinstance(api, Mapping) or set(api) != {"create"} or api.get("create") is not True:
        _raise()
    if (
        not isinstance(codex, Mapping)
        or set(codex) != {"create", "text_only"}
        or type(codex.get("create")) is not bool
        or codex.get("text_only") is not True
    ):
        _raise()
    return {
        "default_provider": API_PROVIDER,
        "provider_immutable": True,
        "providers": {
            API_PROVIDER: {"create": True},
            CODEX_PROVIDER: {
                "create": bool(codex["create"]),
                "text_only": True,
            },
        },
    }


def _project_active_session(payload: object) -> tuple[str | None, str | None]:
    if not isinstance(payload, Mapping):
        _raise()
    active = payload.get("active_session")
    sessions = payload.get("sessions")
    if not isinstance(active, str) or not isinstance(sessions, list) or len(sessions) > 10000:
        _raise()

    seen: set[str] = set()
    active_provider: str | None = None
    for item in sessions:
        if not isinstance(item, Mapping):
            _raise()
        session_id = item.get("id")
        provider = item.get("provider")
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 128
            or provider not in VALID_PROVIDERS
            or session_id in seen
        ):
            _raise()
        seen.add(session_id)
        if session_id == active:
            active_provider = str(provider)

    if active:
        if active_provider is None:
            _raise()
        return active, active_provider
    if sessions:
        _raise()
    return None, None


def project_provider_status(
    session_state: object,
    capabilities: object,
) -> dict[str, object]:
    """Project one authoritative, side-effect-free P3 browser status payload."""
    active_session, active_provider = _project_active_session(session_state)
    web_sessions = _project_capabilities(capabilities)
    return {
        "ok": True,
        "contract_version": 1,
        "active_session": active_session,
        "active_provider": active_provider,
        "web_sessions": web_sessions,
    }
