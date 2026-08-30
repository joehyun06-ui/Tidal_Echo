"""Public, secret-free Web provider capability projection for P3.

This module is intentionally independent from UI state.  The browser may use the
returned contract to decide which provider choices to offer for *new* Web sessions,
but durable per-session provider authority remains in the api-loop session store.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from backend import deployment_config


CONTROL_FLAG = "CODEX_CONTROL_ENABLED"
ENTRYPOINT_FLAG = "CODEX_CANARY_ENTRYPOINTS_ENABLED"
GENERATION_FLAG = "CODEX_GENERATION_ENABLED"


class WebProviderCapabilitiesError(RuntimeError):
    """Fixed, data-free capability projection failure."""

    def __init__(self, category: str = "web_provider_capabilities_unavailable"):
        super().__init__(category)
        self.category = category


def _strict_flag(environ: Mapping[str, str], name: str) -> bool:
    try:
        return deployment_config.parse_strict_bool(
            str(environ.get(name, "false")),
            "web_provider_capabilities_unavailable",
        )
    except deployment_config.DeploymentConfigError:
        raise WebProviderCapabilitiesError() from None


def public_capabilities(
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return the stable browser-facing new-session provider contract.

    Codex creation is advertised only when all runtime authorities required by the
    current P3 deployment are explicitly enabled.  No login/account state, secret,
    environment value, model configuration, or internal route is exposed.
    """
    env = os.environ if environ is None else environ
    control_enabled = _strict_flag(env, CONTROL_FLAG)
    entrypoints_enabled = _strict_flag(env, ENTRYPOINT_FLAG)
    generation_enabled = _strict_flag(env, GENERATION_FLAG)
    codex_create = control_enabled and entrypoints_enabled and generation_enabled
    return {
        "ok": True,
        "contract_version": 1,
        "web_sessions": {
            "default_provider": "api",
            "provider_immutable": True,
            "providers": {
                "api": {"create": True},
                "codex": {
                    "create": codex_create,
                    "text_only": True,
                },
            },
        },
    }
