"""Pinned Codex 0.147.0 generation hardening at the transport boundary.

The deny-list mirrors OpenAI Codex's own temporary structured thread profile for
0.147.0. Keeping the rewrite at the scoped-transport boundary means even a caller
that accidentally omits one of these settings cannot widen the companion thread.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from .codex_app_server_shared_transport import CodexScopedTransport, CodexTransportError


OFFICIAL_0147_DENY_CONFIG: Mapping[str, object] = {
    "features.apps": False,
    "features.code_mode": False,
    "features.code_mode_only": False,
    "features.current_time_reminder": False,
    "features.deferred_executor": False,
    "features.enable_fanout": False,
    "features.goals": False,
    "features.hooks": False,
    "features.image_generation": False,
    "features.memories": False,
    "features.multi_agent": False,
    "features.multi_agent_v2": False,
    "features.plugins": False,
    "features.request_permissions_tool": False,
    "features.shell_snapshot": False,
    "features.shell_tool": False,
    "features.standalone_web_search": False,
    "features.token_budget": False,
    "features.tool_suggest": False,
    "features.unified_exec": False,
    "features.view_image": False,
    "orchestrator.skills.enabled": False,
    "skills.include_instructions": False,
    "token_budget.use_history_notes_extension": False,
    "tools.experimental_request_user_input.enabled": False,
    "tools.update_plan.enabled": False,
    "web_search": "disabled",
}

_MCP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_MCP_DOTTED = re.compile(r"^mcp_servers\.([A-Za-z0-9][A-Za-z0-9._:-]{0,159})\.enabled$")
_MAX_MCP_SERVERS = 128


def _effective_mcp_server_names(result: object) -> set[str]:
    """Project only MCP names from the pinned ConfigReadResponse wire shape."""
    if not isinstance(result, dict):
        raise CodexTransportError("codex_app_server_protocol_error")
    config = result.get("config")
    if not isinstance(config, dict):
        raise CodexTransportError("codex_app_server_protocol_error")
    additional = config.get("additional")
    if additional is None:
        additional = {}
    if not isinstance(additional, dict):
        raise CodexTransportError("codex_app_server_protocol_error")
    raw_mcp = additional.get("mcp_servers", {})
    if raw_mcp is None:
        raw_mcp = {}
    if not isinstance(raw_mcp, dict) or len(raw_mcp) > _MAX_MCP_SERVERS:
        raise CodexTransportError("codex_app_server_protocol_error")
    names: set[str] = set()
    for name in raw_mcp:
        if not isinstance(name, str) or _MCP_NAME.fullmatch(name) is None:
            raise CodexTransportError("codex_app_server_protocol_error")
        names.add(name)
    return names


class CodexGenerationHardeningTransport:
    """Generation-only scoped transport that force-applies the 0.147.0 isolation profile."""

    def __init__(self, transport: CodexScopedTransport) -> None:
        self._transport = transport

    async def request(self, method: str, params: Mapping[str, object]) -> object:
        if method not in {"thread/start", "thread/resume"}:
            return await self._transport.request(method, params)
        rewritten = dict(params)
        cwd = rewritten.get("cwd")
        if (
            not isinstance(cwd, str)
            or not cwd
            or "\x00" in cwd
            or not Path(cwd).is_absolute()
        ):
            raise CodexTransportError("codex_app_server_protocol_error")
        supplied_config = rewritten.get("config")
        if supplied_config is not None and not isinstance(supplied_config, dict):
            raise CodexTransportError("codex_app_server_protocol_error")
        mcp_names = _effective_mcp_server_names(
            await self._transport.request(
                "config/read",
                {"includeLayers": False, "cwd": cwd},
            )
        )
        if isinstance(supplied_config, dict):
            raw_mcp = supplied_config.get("mcp_servers")
            if isinstance(raw_mcp, dict):
                for name in raw_mcp:
                    if isinstance(name, str) and _MCP_NAME.fullmatch(name) is not None:
                        mcp_names.add(name)
            for key, value in supplied_config.items():
                if value is not False or not isinstance(key, str):
                    continue
                match = _MCP_DOTTED.fullmatch(key)
                if match is not None:
                    mcp_names.add(match.group(1))
        if len(mcp_names) > _MAX_MCP_SERVERS:
            raise CodexTransportError("codex_app_server_protocol_error")
        hardened = dict(OFFICIAL_0147_DENY_CONFIG)
        hardened["mcp_servers"] = {
            name: {"enabled": False} for name in sorted(mcp_names)
        }
        rewritten["config"] = hardened
        rewritten["runtimeWorkspaceRoots"] = []
        rewritten["approvalPolicy"] = "never"
        rewritten["sandbox"] = "read-only"
        if method == "thread/start":
            rewritten["environments"] = []
            rewritten["dynamicTools"] = []
            rewritten["selectedCapabilityRoots"] = []
            rewritten["experimentalRawEvents"] = False
        else:
            rewritten.pop("environments", None)
        return await self._transport.request(method, rewritten)
