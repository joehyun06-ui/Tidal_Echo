#!/usr/bin/env python3
"""P3 production supervisor with durable Web-provider rollback protection.

With Codex entrypoints disabled, the reviewed production supervisor is preserved
except that the localhost API-loop target is wrapped by
``examples.api_loop_provider_guard:app``. That guard keeps ordinary traffic on API
while refusing any durable Codex-authority Web session before API model work.

A later explicit Codex rollout can enable the existing alternate Codex api-loop and
relay behind the same strict canary/generation gates. This file does not itself
change provider authority or enable Codex.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend import deployment_config
from scripts import render_start


CANARY_FLAG = "CODEX_CANARY_ENTRYPOINTS_ENABLED"
GENERATION_FLAG = "CODEX_GENERATION_ENABLED"
BASE_API_LOOP = "examples.api_loop:app"
GUARD_API_LOOP = "examples.api_loop_provider_guard:app"
CODEX_API_LOOP = "examples.api_loop_codex_canary:app"
LEGACY_RELAY = "backend.legacy_chat_bridge_app:app"
CODEX_RELAY = "backend.codex_canary_relay_app:app"


def codex_entrypoints_enabled(environ: Mapping[str, str] | None = None) -> bool:
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
            "p3_provider_supervisor_contract_invalid"
        )
    return [replacement if item == expected else item for item in command]


def _select_child_commands(
    base_selector: Callable[..., dict[str, list[str]]],
    config: render_start.SupervisorConfig,
    executable: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    commands = dict(base_selector(config, executable=executable))
    commands["api_loop"] = _replace_target(
        list(commands["api_loop"]),
        BASE_API_LOOP,
        GUARD_API_LOOP,
    )
    if not codex_entrypoints_enabled(environ):
        return commands
    commands["api_loop"] = _replace_target(
        list(commands["api_loop"]),
        GUARD_API_LOOP,
        CODEX_API_LOOP,
    )
    commands["relay"] = _replace_target(
        list(commands["relay"]),
        LEGACY_RELAY,
        CODEX_RELAY,
    )
    return commands


def child_commands(
    config: render_start.SupervisorConfig,
    executable: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    return _select_child_commands(
        render_start.child_commands,
        config,
        executable=executable,
        environ=environ,
    )


def main() -> int:
    # Validate gates before any child process can start.
    codex_entrypoints_enabled(os.environ)
    original = render_start.child_commands

    def selected(config, executable=None):
        return _select_child_commands(
            original,
            config,
            executable=executable,
            environ=os.environ,
        )

    render_start.child_commands = selected
    try:
        return render_start.main()
    finally:
        render_start.child_commands = original


if __name__ == "__main__":
    raise SystemExit(main())
