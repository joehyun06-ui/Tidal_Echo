"""Pinned Codex 0.147.0 generation hardening at the transport boundary.

The deny-list mirrors OpenAI Codex's own temporary structured thread profile for
0.147.0. Keeping the rewrite at the scoped-transport boundary means even a caller
that accidentally omits one of these settings cannot widen the companion thread.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

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

_MCP_DOTTED = re.compile(r"^mcp_servers\.([A-Za-z0-9][A-Za-z0-9._:-]{0,159})\.enabled$")


class CodexGenerationHardeningTransport:
    """Generation-only scoped transport that force-applies the 0.147.0 isolation profile."""

    def __init__(self, transport: CodexScopedTransport) -> None:
        self._transport = transport

    async def request(self, method: str, params: Mapping[str, object]) -> object:
        if method not in {"thread/start", "thread/resume"}:
            return await self._transport.request(method, params)
        rewritten = dict(params)
        supplied_config = rewritten.get("config")
        if supplied_config is not None and not isinstance(supplied_config, dict):
            raise CodexTransportError("codex_app_server_protocol_error")
        mcp_names: set[str] = set()
        if isinstance(supplied_config, dict):
            raw_mcp = supplied_config.get("mcp_servers")
            if isinstance(raw_mcp, dict):
                mcp_names.update(name for name in raw_mcp if isinstance(name, str))
            for key, value in supplied_config.items():
                if value is not False or not isinstance(key, str):
                    continue
                match = _MCP_DOTTED.fullmatch(key)
                if match is not None:
                    mcp_names.add(match.group(1))
        hardened = dict(OFFICIAL_0147_DENY_CONFIG)
        hardened["mcp_servers"] = {
            name: {"enabled": False} for name in sorted(mcp_names)
        }
        rewritten["config"] = hardened
        rewritten["environments"] = []
        rewritten["runtimeWorkspaceRoots"] = []
        if method == "thread/start":
            rewritten["dynamicTools"] = []
            rewritten["selectedCapabilityRoots"] = []
            rewritten["experimentalRawEvents"] = False
            rewritten["approvalPolicy"] = "never"
            rewritten["sandbox"] = "read-only"
        return await self._transport.request(method, rewritten)
