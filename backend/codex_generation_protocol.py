"""P2-A Codex App Server generation protocol foundation.

This module deliberately owns no subprocess and has no HTTP or chat routing entry point.
It is a narrow generation facade intended to run over the same private App Server
transport as the P1 account-control facade once that transport is shared.

Safety properties:
- generation is disabled by default;
- only an explicit generation RPC allow-list may cross the transport;
- companion threads must be durable paginated threads;
- environment/tool surfaces are denied by construction;
- raw upstream error text/config values never leave this module;
- no API<->Codex fallback decision lives here.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


MAX_USER_TEXT_CHARS = 1_048_576
MAX_ASSISTANT_TEXT_CHARS = 64_000
MAX_ID_CHARS = 160
MAX_MODEL_CHARS = 128
MAX_MCP_SERVERS = 128
MAX_TURN_PAGE = 16
DEFAULT_RECOVERY_TURN_PAGE = 8

GENERATION_RPC_METHODS = frozenset({
    "account/read",
    "config/read",
    "model/list",
    "thread/start",
    "thread/resume",
    "thread/unsubscribe",
    "turn/start",
    "turn/interrupt",
})

GENERATION_NOTIFICATIONS = frozenset({
    "turn/started",
    "turn/completed",
    "error",
    "thread/tokenUsage/updated",
})

KNOWN_CODEX_ERROR_INFO = frozenset({
    "unauthorized",
    "rateLimitExceeded",
    "usageLimitExceeded",
    "contextWindowExceeded",
    "serverOverloaded",
    "responseStreamDisconnected",
    "badRequest",
    "sandboxError",
    "internalServerError",
    "other",
})

# Mirrors the deny-by-default profile used by Codex isolated/structured threads.
# P2-A intentionally keeps this explicit rather than relying on current defaults.
HARDENED_FEATURE_OVERRIDES: Mapping[str, bool] = {
    "features.shell_tool": False,
    "features.unified_exec": False,
    "features.apps": False,
    "features.plugins": False,
    "features.multi_agent": False,
    "features.multi_agent_v2": False,
    "features.image_generation": False,
    "features.memories": False,
    "features.hooks": False,
    "features.skills": False,
    "features.tool_suggest": False,
    "features.update_plan": False,
    "features.request_user_input": False,
    "features.standalone_web_search": False,
    "features.web_search_request": False,
    "include_permissions_instructions": False,
    "include_apps_instructions": False,
    "include_collaboration_mode_instructions": False,
    "include_environment_context": False,
}

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class CodexGenerationError(RuntimeError):
    """Fixed, data-free generation failure safe for logs/status surfaces."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category

    def __repr__(self) -> str:
        return f"<CodexGenerationError category={self.category!r}>"


class CodexRpcTransport(Protocol):
    """Private shared App Server transport contract expected by this facade."""

    async def request(self, method: str, params: Mapping[str, object]) -> object: ...


@dataclass(frozen=True)
class CodexGenerationConfig:
    enabled: bool
    workspace_root: Path
    model_policy: str = "default"
    recovery_turn_page: int = DEFAULT_RECOVERY_TURN_PAGE

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        persistent_root: Path = Path("/var/data"),
    ) -> "CodexGenerationConfig":
        env = os.environ if environ is None else environ
        raw_enabled = str(env.get("CODEX_GENERATION_ENABLED", "false"))
        if raw_enabled not in {"true", "false"}:
            raise CodexGenerationError("invalid_codex_generation_enabled")
        workspace = Path(
            str(env.get("CODEX_GENERATION_WORKSPACE", persistent_root / "codex-workspace"))
        )
        if not workspace.is_absolute() or ".." in workspace.parts:
            raise CodexGenerationError("invalid_codex_generation_workspace")
        policy = str(env.get("CODEX_GENERATION_MODEL", "default"))
        if policy != "default" and not _SAFE_MODEL.fullmatch(policy):
            raise CodexGenerationError("invalid_codex_generation_model")
        raw_page = str(env.get("CODEX_GENERATION_RECOVERY_TURNS", DEFAULT_RECOVERY_TURN_PAGE))
        if not raw_page.isascii() or not raw_page.isdecimal():
            raise CodexGenerationError("invalid_codex_generation_recovery_turns")
        page = int(raw_page)
        if page < 1 or page > MAX_TURN_PAGE:
            raise CodexGenerationError("invalid_codex_generation_recovery_turns")
        return cls(raw_enabled == "true", workspace, policy, page)


@dataclass(frozen=True)
class ModelSelection:
    model: str
    reasoning_effort: str | None


@dataclass(frozen=True)
class ThreadStartResult:
    thread_id: str
    model: str
    reasoning_effort: str | None
    cwd: Path


@dataclass(frozen=True)
class TurnStartResult:
    turn_id: str
    status: str


@dataclass(frozen=True)
class CorrelatedTurn:
    turn_id: str
    status: str
    final_answer: str | None


@dataclass(frozen=True)
class GenerationNotification:
    method: str
    thread_id: str
    turn_id: str
    terminal: bool
    will_retry: bool | None = None
    error_info: str | None = None
    usage: Mapping[str, int] | None = None


class CodexProcessActivityGate:
    """Keep P1 control RPCs from tearing down a shared process mid-generation.

    P2-A uses a single global generation slot. Read/control operations fail fast while
    generation is active; generation waits for an already-started control operation to
    leave before it begins. This preserves the existing P1 timeout/teardown semantics
    without letting a control timeout kill an active turn.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._control_count = 0
        self._generation_active = False

    @property
    def generation_active(self) -> bool:
        return self._generation_active

    @asynccontextmanager
    async def control(self) -> AsyncIterator[None]:
        async with self._condition:
            if self._generation_active:
                raise CodexGenerationError("codex_generation_busy")
            self._control_count += 1
        try:
            yield
        finally:
            async with self._condition:
                self._control_count -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def generation(self) -> AsyncIterator[None]:
        async with self._condition:
            while self._control_count:
                await self._condition.wait()
            if self._generation_active:
                raise CodexGenerationError("codex_generation_busy")
            self._generation_active = True
        try:
            yield
        finally:
            async with self._condition:
                self._generation_active = False
                self._condition.notify_all()


def _mapping(value: object, category: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise CodexGenerationError(category)
    return value


def _safe_id(value: object, category: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise CodexGenerationError(category)
    return value


def _safe_model(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_MODEL.fullmatch(value):
        raise CodexGenerationError("codex_generation_model_unavailable")
    return value


def _bounded_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, 10**18)


def input_digest(text: str) -> str:
    if not isinstance(text, str) or not text or len(text) > MAX_USER_TEXT_CHARS:
        raise CodexGenerationError("codex_generation_input_invalid")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def deterministic_workspace(root: Path, api_session: str, attempt_id: str) -> Path:
    _safe_id(api_session, "codex_generation_session_invalid")
    _safe_id(attempt_id, "codex_generation_attempt_invalid")
    path = root / "sessions" / api_session / attempt_id
    try:
        resolved_root = root.resolve(strict=False)
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise CodexGenerationError("codex_generation_workspace_invalid") from None
    if resolved_root == resolved or resolved_root not in resolved.parents:
        raise CodexGenerationError("codex_generation_workspace_invalid")
    return resolved


def extract_mcp_server_names(config_result: object) -> tuple[str, ...]:
    """Extract names only; never project MCP config values or secrets."""
    payload = _mapping(config_result, "codex_generation_config_invalid")
    candidate: object = payload
    for wrapper in ("config", "effectiveConfig", "effective_config"):
        nested = payload.get(wrapper)
        if isinstance(nested, dict):
            candidate = nested
            break
    candidate_map = _mapping(candidate, "codex_generation_config_invalid")
    raw = candidate_map.get("mcp_servers", candidate_map.get("mcpServers", {}))
    if raw is None:
        return ()
    if not isinstance(raw, dict) or len(raw) > MAX_MCP_SERVERS:
        raise CodexGenerationError("codex_generation_config_invalid")
    output: list[str] = []
    for name in raw:
        if not isinstance(name, str) or not _SAFE_ID.fullmatch(name):
            raise CodexGenerationError("codex_generation_config_invalid")
        output.append(name)
    return tuple(sorted(output))


def build_hardened_config(mcp_server_names: tuple[str, ...] = ()) -> dict[str, object]:
    config: dict[str, object] = dict(HARDENED_FEATURE_OVERRIDES)
    config["web_search"] = "disabled"
    for name in mcp_server_names:
        _safe_id(name, "codex_generation_config_invalid")
        config[f"mcp_servers.{name}.enabled"] = False
    return config


def resolve_model(model_list_result: object, policy: str = "default") -> ModelSelection:
    payload = _mapping(model_list_result, "codex_generation_model_unavailable")
    raw_models = payload.get("data", payload.get("models"))
    if not isinstance(raw_models, list) or not raw_models or len(raw_models) > 256:
        raise CodexGenerationError("codex_generation_model_unavailable")
    models: list[Mapping[str, object]] = [
        item for item in raw_models if isinstance(item, dict)
    ]
    if len(models) != len(raw_models):
        raise CodexGenerationError("codex_generation_model_unavailable")
    if policy == "default":
        matches = [item for item in models if item.get("isDefault") is True]
        if len(matches) != 1:
            raise CodexGenerationError("codex_generation_model_unavailable")
        selected = matches[0]
    else:
        matches = [item for item in models if item.get("model") == policy]
        if len(matches) != 1:
            raise CodexGenerationError("codex_generation_model_unavailable")
        selected = matches[0]
    model = _safe_model(selected.get("model"))
    effort = selected.get("defaultReasoningEffort")
    if effort is not None:
        if not isinstance(effort, str) or len(effort) > 32 or not effort.isascii():
            raise CodexGenerationError("codex_generation_model_unavailable")
    return ModelSelection(model, effort)


def require_chatgpt_account(account_result: object) -> None:
    payload = _mapping(account_result, "codex_generation_account_unavailable")
    account = payload.get("account")
    if not isinstance(account, dict):
        raise CodexGenerationError("codex_generation_account_unavailable")
    account_type = account.get("type", account.get("accountType"))
    if not isinstance(account_type, str) or account_type.casefold() not in {
        "chatgpt", "chatgptaccount"
    }:
        raise CodexGenerationError("codex_generation_account_unavailable")


def final_answer_from_turn(turn: Mapping[str, object]) -> str | None:
    items = turn.get("items")
    if not isinstance(items, list):
        return None
    final: str | None = None
    fallback: str | None = None
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        text = "".join(parts)
        if not text or len(text) > MAX_ASSISTANT_TEXT_CHARS:
            continue
        phase = item.get("phase")
        if phase in {"finalAnswer", "final_answer"}:
            final = text
        elif phase is None:
            fallback = text
    return final if final is not None else fallback


def correlated_turn_from_page(page: object, client_message_id: str) -> CorrelatedTurn | None:
    _safe_id(client_message_id, "codex_generation_client_id_invalid")
    payload = _mapping(page, "codex_generation_recovery_invalid")
    turns = payload.get("data", payload.get("turns"))
    if not isinstance(turns, list):
        raise CodexGenerationError("codex_generation_recovery_invalid")
    for raw_turn in turns:
        if not isinstance(raw_turn, dict):
            raise CodexGenerationError("codex_generation_recovery_invalid")
        items = raw_turn.get("items")
        if not isinstance(items, list):
            continue
        matched = False
        for item in items:
            if (
                isinstance(item, dict)
                and item.get("type") == "userMessage"
                and item.get("clientId") == client_message_id
            ):
                matched = True
                break
        if not matched:
            continue
        turn_id = _safe_id(raw_turn.get("id"), "codex_generation_recovery_invalid")
        status = raw_turn.get("status")
        if status not in {"inProgress", "completed", "failed", "interrupted"}:
            raise CodexGenerationError("codex_generation_recovery_invalid")
        return CorrelatedTurn(turn_id, str(status), final_answer_from_turn(raw_turn))
    return None


def project_notification(method: str, params: object) -> GenerationNotification | None:
    if method not in GENERATION_NOTIFICATIONS:
        return None
    payload = _mapping(params, "codex_generation_protocol_error")
    thread_id = _safe_id(payload.get("threadId"), "codex_generation_protocol_error")
    if method in {"turn/started", "turn/completed"}:
        turn = _mapping(payload.get("turn"), "codex_generation_protocol_error")
        turn_id = _safe_id(turn.get("id"), "codex_generation_protocol_error")
        status = turn.get("status")
        if not isinstance(status, str):
            raise CodexGenerationError("codex_generation_protocol_error")
        return GenerationNotification(
            method, thread_id, turn_id, method == "turn/completed"
        )
    if method == "error":
        turn_id = _safe_id(payload.get("turnId"), "codex_generation_protocol_error")
        will_retry = payload.get("willRetry")
        if not isinstance(will_retry, bool):
            raise CodexGenerationError("codex_generation_protocol_error")
        error = _mapping(payload.get("error"), "codex_generation_protocol_error")
        info = error.get("codexErrorInfo")
        safe_info = info if isinstance(info, str) and info in KNOWN_CODEX_ERROR_INFO else "other"
        return GenerationNotification(
            method,
            thread_id,
            turn_id,
            terminal=not will_retry,
            will_retry=will_retry,
            error_info=safe_info,
        )
    turn_id = _safe_id(payload.get("turnId"), "codex_generation_protocol_error")
    usage_raw = _mapping(payload.get("tokenUsage"), "codex_generation_protocol_error")
    last = usage_raw.get("last")
    usage: dict[str, int] = {}
    if isinstance(last, dict):
        aliases = {
            "input_tokens": ("inputTokens", "input_tokens"),
            "output_tokens": ("outputTokens", "output_tokens"),
            "cached_input_tokens": ("cachedInputTokens", "cached_input_tokens"),
            "reasoning_output_tokens": ("reasoningOutputTokens", "reasoning_output_tokens"),
        }
        for output_name, names in aliases.items():
            value = next((last[name] for name in names if name in last), None)
            projected = _bounded_nonnegative_int(value)
            if projected is not None:
                usage[output_name] = projected
    return GenerationNotification(method, thread_id, turn_id, False, usage=usage)


class CodexGenerationProtocol:
    """Strict P2 generation facade over an injected shared App Server transport."""

    def __init__(self, config: CodexGenerationConfig, transport: CodexRpcTransport) -> None:
        self.config = config
        self._transport = transport

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise CodexGenerationError("codex_generation_disabled")

    async def _request(self, method: str, params: Mapping[str, object]) -> object:
        self._require_enabled()
        if method not in GENERATION_RPC_METHODS:
            raise CodexGenerationError("codex_generation_protocol_error")
        try:
            return await self._transport.request(method, dict(params))
        except CodexGenerationError:
            raise
        except Exception:
            raise CodexGenerationError("codex_generation_unavailable") from None

    async def qualify(self) -> ModelSelection:
        require_chatgpt_account(await self._request("account/read", {"refreshToken": False}))
        return resolve_model(await self._request("model/list", {}), self.config.model_policy)

    async def start_thread(
        self,
        *,
        api_session: str,
        attempt_id: str,
        persona: str,
    ) -> ThreadStartResult:
        if not isinstance(persona, str) or not persona.strip() or len(persona) > 131_072:
            raise CodexGenerationError("codex_generation_persona_invalid")
        selection = await self.qualify()
        mcp_names = extract_mcp_server_names(await self._request("config/read", {}))
        cwd = deterministic_workspace(self.config.workspace_root, api_session, attempt_id)
        params: dict[str, object] = {
            "model": selection.model,
            "cwd": str(cwd),
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "baseInstructions": persona,
            "ephemeral": False,
            "historyMode": "paginated",
            "environments": [],
            "dynamicTools": [],
            "selectedCapabilityRoots": [],
            "experimentalRawEvents": False,
            "config": build_hardened_config(mcp_names),
        }
        raw = _mapping(
            await self._request("thread/start", params),
            "codex_generation_thread_start_failed",
        )
        thread = _mapping(raw.get("thread"), "codex_generation_thread_start_failed")
        thread_id = _safe_id(thread.get("id"), "codex_generation_thread_start_failed")
        if thread.get("ephemeral") is not False or thread.get("historyMode") != "paginated":
            raise CodexGenerationError("codex_generation_thread_contract_mismatch")
        response_model = _safe_model(raw.get("model"))
        if response_model != selection.model:
            raise CodexGenerationError("codex_generation_thread_contract_mismatch")
        response_cwd = raw.get("cwd")
        if not isinstance(response_cwd, str) or Path(response_cwd) != cwd:
            raise CodexGenerationError("codex_generation_thread_contract_mismatch")
        return ThreadStartResult(thread_id, response_model, selection.reasoning_effort, cwd)

    async def resume_thread(
        self,
        *,
        thread_id: str,
        model: str,
        reasoning_effort: str | None,
        cwd: Path,
        persona: str,
    ) -> Mapping[str, object]:
        thread_id = _safe_id(thread_id, "codex_generation_thread_invalid")
        model = _safe_model(model)
        if not isinstance(cwd, Path) or not cwd.is_absolute():
            raise CodexGenerationError("codex_generation_workspace_invalid")
        mcp_names = extract_mcp_server_names(await self._request("config/read", {}))
        params: dict[str, object] = {
            "threadId": thread_id,
            "model": model,
            "cwd": str(cwd),
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "baseInstructions": persona,
            "excludeTurns": True,
            "environments": [],
            "config": build_hardened_config(mcp_names),
            "initialTurnsPage": {
                "limit": self.config.recovery_turn_page,
                "sortDirection": "desc",
                "itemsView": "summary",
            },
        }
        if reasoning_effort is not None:
            params["effort"] = reasoning_effort
        result = _mapping(
            await self._request("thread/resume", params),
            "codex_generation_thread_resume_failed",
        )
        thread = _mapping(result.get("thread"), "codex_generation_thread_resume_failed")
        if thread.get("id") != thread_id or thread.get("historyMode") != "paginated":
            raise CodexGenerationError("codex_generation_thread_contract_mismatch")
        page = result.get("initialTurnsPage")
        return _mapping(page, "codex_generation_recovery_invalid")

    async def start_turn(
        self,
        *,
        thread_id: str,
        client_message_id: str,
        text: str,
        model: str,
        reasoning_effort: str | None,
    ) -> TurnStartResult:
        thread_id = _safe_id(thread_id, "codex_generation_thread_invalid")
        client_message_id = _safe_id(
            client_message_id, "codex_generation_client_id_invalid"
        )
        input_digest(text)
        params: dict[str, object] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
            "clientUserMessageId": client_message_id,
            "model": _safe_model(model),
            "environments": [],
        }
        if reasoning_effort is not None:
            params["effort"] = reasoning_effort
        raw = _mapping(
            await self._request("turn/start", params),
            "codex_generation_turn_start_failed",
        )
        turn = _mapping(raw.get("turn"), "codex_generation_turn_start_failed")
        turn_id = _safe_id(turn.get("id"), "codex_generation_turn_start_failed")
        status = turn.get("status")
        if status != "inProgress":
            raise CodexGenerationError("codex_generation_turn_contract_mismatch")
        return TurnStartResult(turn_id, status)

    async def interrupt(self, *, thread_id: str, turn_id: str) -> None:
        await self._request(
            "turn/interrupt",
            {
                "threadId": _safe_id(thread_id, "codex_generation_thread_invalid"),
                "turnId": _safe_id(turn_id, "codex_generation_turn_invalid"),
            },
        )

    async def unsubscribe(self, *, thread_id: str) -> None:
        await self._request(
            "thread/unsubscribe",
            {"threadId": _safe_id(thread_id, "codex_generation_thread_invalid")},
        )
