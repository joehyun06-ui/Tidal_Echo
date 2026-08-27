#!/usr/bin/env python3
"""Optional Render supervisor wrapper for the Codex Web canary.

The normal production start command continues to invoke ``scripts/render_start.py``.
This module is inert unless a future explicit startup-command change selects it.
Even then, Codex canary entrypoints remain default-off behind a strict flag.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from backend import deployment_config
from scripts import render_start


CANARY_FLAG = "CODEX_CANARY_ENTRYPOINTS_ENABLED"
GENERATION_FLAG = "CODEX_GENERATION_ENABLED"
LEGACY_API_LOOP = "examples.api_loop:app"
CANARY_API_LOOP = "examples.api_loop_codex_canary:app"
LEGACY_RELAY = "backend.legacy_chat_bridge_app:app"
CANARY_RELAY = "backend.codex_canary_relay_app:app"


def canary_entrypoints_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return the strict canary-entrypoint gate and reject fake generation activation."""
    env = os.environ if environ is None else environ
    enabled = deployment_config.parse_strict_bool(
        env.get(CANARY_FLAG, "false"),
        "invalid_codex_canary_entrypoints_enabled",
    )
    generation_enabled = deployment_config.parse_strict_bool(
        env.get(GENERATION_FLAG, "false"),
        "invalid_codex_generation_enabled",
    )
    if generation_enabled and not enabled:
        raise deployment_config.DeploymentConfigError(
            "codex_generation_requires_canary_entrypoints"
        )
    return enabled


def _replace_target(command: list[str], expected: str, replacement: str) -> list[str]:
    if command.count(expected) != 1:
        raise deployment_config.DeploymentConfigError(
            "codex_canary_supervisor_contract_invalid"
        )
    return [replacement if item == expected else item for item in command]


def child_commands(
    config: render_start.SupervisorConfig,
    executable: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    """Return legacy commands by default, or swap only the two reviewed entrypoints."""
    commands = render_start.child_commands(config, executable=executable)
    if not canary_entrypoints_enabled(environ):
        return commands
    commands = dict(commands)
    commands["api_loop"] = _replace_target(
        list(commands["api_loop"]), LEGACY_API_LOOP, CANARY_API_LOOP
    )
    commands["relay"] = _replace_target(
        list(commands["relay"]), LEGACY_RELAY, CANARY_RELAY
    )
    return commands


def main() -> int:
    """Run the existing supervisor with a scoped entrypoint selector override."""
    # Validate before any child process can be started.
    canary_entrypoints_enabled(os.environ)
    original = render_start.child_commands

    def selected(config, executable=None):
        return child_commands(config, executable=executable, environ=os.environ)

    render_start.child_commands = selected
    try:
        return render_start.main()
    finally:
        render_start.child_commands = original


if __name__ == "__main__":
    raise SystemExit(main())
