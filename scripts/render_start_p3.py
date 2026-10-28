#!/usr/bin/env python3
"""P3 production supervisor with durable Web-provider rollback protection.

With Codex entrypoints disabled, the reviewed production relay is already the
``backend.p3_relay_app:app`` wrapper. This module adds the localhost
``examples.api_loop_provider_guard:app`` provider/session guard while validating
that the public relay remains on the P3 contract.

When explicit Codex entrypoints are enabled, the qualified Codex api-loop is paired
with the production P3 Codex relay. The production relay keeps Codex queued-ack and
completion integration but does not expose qualification-era canary admin routes.
This file does not itself change provider authority or enable Codex.
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
P3_RELAY = "backend.p3_relay_app:app"
CODEX_RELAY = "backend.p3_codex_relay_app:app"


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


def _require_target(command: list[str], expected: str) -> list[str]:
    if command.count(expected) != 1:
        raise deployment_config.DeploymentConfigError(
            "p3_provider_supervisor_contract_invalid"
        )
    return command


def _require_p3_proxy_header_isolation(commands: dict[str, list[str]]) -> dict[str, list[str]]:
    """P3 must not undo the base supervisor's explicit proxy-header opt-out."""
    for process, host in (("api_loop", "127.0.0.1"), ("relay", "0.0.0.0")):
        command = commands.get(process)
        if (
            type(command) is not list
            or command.count("--no-proxy-headers") != 1
            or "--proxy-headers" in command
            or "--forwarded-allow-ips" in command
            or command.count("--host") != 1
        ):
            raise deployment_config.DeploymentConfigError(
                "p3_proxy_header_boundary_unreviewed"
            )
        index = command.index("--host")
        if index + 1 >= len(command) or command[index + 1] != host:
            raise deployment_config.DeploymentConfigError(
                "p3_proxy_header_boundary_unreviewed"
            )
    return commands


def _select_child_commands(
    base_selector: Callable[..., dict[str, list[str]]],
    config: render_start.SupervisorConfig,
    executable: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    # Protect the direct P3 selection path, even if callers skip base preflight.
    render_start.reject_unreviewed_browser_identity_ingress(
        os.environ if environ is None else environ
    )
    commands = dict(base_selector(config, executable=executable))
    if "api_loop" not in commands or "relay" not in commands:
        raise deployment_config.DeploymentConfigError(
            "p3_provider_supervisor_contract_invalid"
        )
    commands["api_loop"] = _replace_target(
        list(commands["api_loop"]),
        BASE_API_LOOP,
        GUARD_API_LOOP,
    )
    commands["relay"] = _require_target(
        list(commands["relay"]),
        P3_RELAY,
    )
    if not codex_entrypoints_enabled(environ):
        return _require_p3_proxy_header_isolation(commands)
    commands["api_loop"] = _replace_target(
        list(commands["api_loop"]),
        GUARD_API_LOOP,
        CODEX_API_LOOP,
    )
    commands["relay"] = _replace_target(
        list(commands["relay"]),
        P3_RELAY,
        CODEX_RELAY,
    )
    return _require_p3_proxy_header_isolation(commands)


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
    # The real Render startup command calls this P3 wrapper. Apply the
    # OIDC review interlock before changing selectors or starting children.
    try:
        render_start.reject_unreviewed_browser_identity_ingress(os.environ)
    except deployment_config.DeploymentConfigError:
        render_start.safe_log(
            "supervisor", os.getpid(), "preflight_failed",
            "unreviewed_memory_browser_identity_ingress",
        )
        return 2
    # Preserve the existing independent Codex qualification gate.
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
