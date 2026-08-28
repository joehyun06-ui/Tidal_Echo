"""Private shared stdio transport for Codex App Server facades.

P2-A only: this module is not wired into the public relay or chat routing.  It owns
one lazy App Server process and can vend scoped transports whose RPC allow-lists
are fixed at construction.  P1 account control and P2 generation can therefore
share one process without widening either facade's method surface.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_JSONL_BYTES = 1024 * 1024
MAX_OUTSTANDING_REQUESTS = 16
PROCESS_TEARDOWN_STEP_TIMEOUT_SECONDS = 2.0
PARENT_ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "TZ",
)


class CodexTransportError(RuntimeError):
    """Fixed, data-free shared transport failure."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category

    def __repr__(self) -> str:
        return f"<CodexTransportError category={self.category!r}>"


@dataclass(frozen=True)
class CodexSharedTransportConfig:
    enabled: bool
    codex_home: Path
    workspace: Path
    request_timeout_seconds: float


NotificationHandler = Callable[[str, Mapping[str, object]], Awaitable[None] | None]


def _bundled_codex_path() -> str:
    from codex_cli_bin import bundled_codex_path

    return str(bundled_codex_path())


def build_child_environment(
    config: CodexSharedTransportConfig,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if parent is None else parent
    child: dict[str, str] = {}
    for name in PARENT_ENV_ALLOWLIST:
        value = source.get(name)
        if (
            isinstance(value, str)
            and value
            and len(value) <= 32768
            and "\x00" not in value
            and "\r" not in value
            and "\n" not in value
        ):
            child[name] = value
    child["CODEX_HOME"] = str(config.codex_home)
    child["HOME"] = str(config.codex_home)
    child["RUST_LOG"] = "warn"
    return child


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError
        output[key] = value
    return output


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _validate_message(value: object, *, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [4096]
    if depth > 24:
        raise ValueError
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > MAX_JSONL_BYTES:
            raise ValueError
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError
        return
    if isinstance(value, list):
        budget[0] -= 1
        if budget[0] < 0 or len(value) > 4096:
            raise ValueError
        for item in value:
            _validate_message(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        budget[0] -= 1
        if budget[0] < 0 or len(value) > 4096:
            raise ValueError
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise ValueError
            _validate_message(item, depth=depth + 1, budget=budget)
        return
    raise ValueError


class CodexScopedTransport:
    """RPC capability scoped to a fixed allow-list on one shared runtime."""

    def __init__(
        self,
        runtime: "CodexSharedAppServerRuntime",
        methods: frozenset[str],
        notifications: frozenset[str],
        handler: NotificationHandler | None,
    ) -> None:
        self._runtime = runtime
        self._methods = methods
        self._notifications = notifications
        self._handler = handler
        self._closed = False

    async def request(self, method: str, params: Mapping[str, object]) -> object:
        if self._closed or method not in self._methods:
            raise CodexTransportError("codex_app_server_protocol_error")
        return await self._runtime._request(method, params)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._runtime._remove_scope(self)


class CodexSharedAppServerRuntime:
    """Single lazy App Server process owner with scoped RPC/notification facades."""

    def __init__(
        self,
        config: CodexSharedTransportConfig,
        *,
        _runtime_resolver: Callable[[], str] | None = None,
        _process_launcher: Callable[..., Awaitable[Any] | Any] | None = None,
        _parent_environment: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._runtime_resolver = _runtime_resolver or _bundled_codex_path
        self._process_launcher = _process_launcher or asyncio.create_subprocess_exec
        self._parent_environment = _parent_environment
        self._process: Any | None = None
        self._ready_process: Any | None = None
        self._reader_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, tuple[Any, asyncio.Future]] = {}
        self._next_id = 1
        self._scopes: set[CodexScopedTransport] = set()
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def scope(
        self,
        *,
        methods: frozenset[str],
        notifications: frozenset[str] = frozenset(),
        handler: NotificationHandler | None = None,
    ) -> CodexScopedTransport:
        if "initialize" in methods:
            raise CodexTransportError("codex_app_server_protocol_error")
        scope = CodexScopedTransport(self, frozenset(methods), frozenset(notifications), handler)
        self._scopes.add(scope)
        return scope

    def _remove_scope(self, scope: CodexScopedTransport) -> None:
        self._scopes.discard(scope)

    async def _launch(self) -> Any:
        try:
            executable = self._runtime_resolver()
            if not isinstance(executable, str) or not executable or "\x00" in executable:
                raise ValueError
            command = (
                executable,
                "--config", 'approval_policy="never"',
                "--config", 'sandbox_mode="read-only"',
                "--config", "features.plugins=false",
                "--config", "features.web_search_request=false",
                "app-server", "--listen", "stdio://",
            )
            kwargs = {
                "stdin": asyncio.subprocess.PIPE,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.DEVNULL,
                "cwd": str(self._config.workspace),
                "env": build_child_environment(self._config, self._parent_environment),
                "limit": MAX_JSONL_BYTES + 1,
            }
            launched = self._process_launcher(*command, **kwargs)
            return await launched if inspect.isawaitable(launched) else launched
        except Exception:
            raise CodexTransportError("codex_app_server_unavailable") from None

    async def _ensure_started(self) -> Any:
        if not self._config.enabled:
            raise CodexTransportError("codex_app_server_disabled")
        if self._closed:
            raise CodexTransportError("codex_app_server_unavailable")
        if self._ready_process is not None and self._ready_process is self._process:
            if self._ready_process.returncode is None:
                return self._ready_process
        async with self._start_lock:
            if self._ready_process is not None and self._ready_process is self._process:
                if self._ready_process.returncode is None:
                    return self._ready_process
            owner = await self._launch()
            if owner.stdin is None or owner.stdout is None:
                await self._terminate_owner(owner)
                raise CodexTransportError("codex_app_server_unavailable")
            self._process = owner
            self._ready_process = None
            self._reader_task = asyncio.create_task(self._reader_loop(owner))
            try:
                await self._request_started(
                    "initialize",
                    {
                        "clientInfo": {"name": "tidal-echo-shared-provider", "version": "2"},
                        "capabilities": {"experimentalApi": True},
                    },
                    owner=owner,
                    allow_initialize=True,
                )
                await self._write_message({"method": "initialized", "params": {}}, owner=owner)
                if owner is not self._process or owner.returncode is not None:
                    raise CodexTransportError("codex_app_server_unavailable")
                self._ready_process = owner
                return owner
            except BaseException:
                await self._fail_process("codex_app_server_unavailable", owner=owner)
                raise

    async def _request(self, method: str, params: Mapping[str, object]) -> object:
        owner = await self._ensure_started()
        return await self._request_started(method, dict(params), owner=owner)

    async def _request_started(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        owner: Any,
        allow_initialize: bool = False,
    ) -> object:
        if not isinstance(method, str) or not method or len(method) > 128:
            raise CodexTransportError("codex_app_server_protocol_error")
        if method == "initialize" and not allow_initialize:
            raise CodexTransportError("codex_app_server_protocol_error")
        _validate_message(params)
        if owner is not self._process or owner.returncode is not None:
            raise CodexTransportError("codex_app_server_unavailable")
        if len(self._pending) >= MAX_OUTSTANDING_REQUESTS:
            raise CodexTransportError("codex_app_server_busy")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        pending = (owner, future)
        self._pending[request_id] = pending
        try:
            await self._write_message(
                {"id": request_id, "method": method, "params": dict(params)}, owner=owner
            )
            try:
                async with asyncio.timeout(self._config.request_timeout_seconds):
                    return await future
            except TimeoutError:
                await self._fail_process("codex_app_server_timeout", owner=owner)
                raise CodexTransportError("codex_app_server_timeout") from None
        finally:
            if self._pending.get(request_id) == pending:
                self._pending.pop(request_id, None)

    async def _write_message(self, message: Mapping[str, object], *, owner: Any) -> None:
        if owner is not self._process or owner.returncode is not None or owner.stdin is None:
            raise CodexTransportError("codex_app_server_unavailable")
        try:
            _validate_message(message)
            encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if not encoded or len(encoded) > MAX_JSONL_BYTES or b"\n" in encoded:
                raise ValueError
            async with self._write_lock:
                owner.stdin.write(encoded + b"\n")
                await owner.stdin.drain()
        except CodexTransportError:
            raise
        except Exception:
            await self._fail_process("codex_app_server_unavailable", owner=owner)
            raise CodexTransportError("codex_app_server_unavailable") from None

    async def _reader_loop(self, owner: Any) -> None:
        category = "codex_app_server_unavailable"
        try:
            while owner is self._process and owner.returncode is None:
                line = await owner.stdout.readline()
                if not line:
                    break
                if len(line) > MAX_JSONL_BYTES or not line.endswith(b"\n"):
                    category = "codex_app_server_protocol_error"
                    break
                try:
                    message = json.loads(
                        line,
                        object_pairs_hook=_reject_duplicate_keys,
                        parse_constant=_reject_json_constant,
                    )
                    _validate_message(message)
                    await self._consume_message(message, owner=owner)
                except Exception:
                    category = "codex_app_server_protocol_error"
                    break
        except asyncio.CancelledError:
            return
        except Exception:
            category = "codex_app_server_unavailable"
        finally:
            if owner is self._process:
                await self._fail_process(category, owner=owner, from_reader=True)

    async def _consume_message(self, message: object, *, owner: Any) -> None:
        if owner is not self._process or not isinstance(message, dict):
            return
        if "jsonrpc" in message and message.get("jsonrpc") != "2.0":
            raise ValueError
        method = message.get("method")
        if method is not None:
            if not isinstance(method, str) or not method:
                raise ValueError
            # Any App Server -> client request is denied. P2 threads must never require
            # approvals, tools, user-input requests, or elicitation to make progress.
            if "id" in message:
                await self._write_message(
                    {
                        "id": message.get("id"),
                        "error": {"code": -32601, "message": "method not supported"},
                    },
                    owner=owner,
                )
                return
            params = message.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise ValueError
            await self._dispatch_notification(method, params)
            return
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            raise ValueError
        pending = self._pending.get(request_id)
        if pending is None or pending[0] is not owner:
            raise ValueError
        future = pending[1]
        if future.done() or (("result" in message) == ("error" in message)):
            raise ValueError
        if "error" in message:
            future.set_exception(CodexTransportError("codex_app_server_rpc_error"))
        else:
            future.set_result(message["result"])

    async def _dispatch_notification(self, method: str, params: Mapping[str, object]) -> None:
        handlers: list[NotificationHandler] = []
        for scope in tuple(self._scopes):
            if scope._closed or method not in scope._notifications or scope._handler is None:
                continue
            handlers.append(scope._handler)
        for handler in handlers:
            try:
                outcome = handler(method, params)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                # Notification consumers are deliberately isolated from transport health.
                continue

    async def _terminate_owner(self, owner: Any) -> None:
        try:
            if owner.returncode is None:
                owner.terminate()
        except Exception:
            pass
        try:
            async with asyncio.timeout(PROCESS_TEARDOWN_STEP_TIMEOUT_SECONDS):
                await owner.wait()
        except Exception:
            try:
                if owner.returncode is None:
                    owner.kill()
            except Exception:
                pass

    async def _fail_process(
        self,
        category: str,
        *,
        owner: Any,
        from_reader: bool = False,
    ) -> None:
        if owner is not self._process:
            return
        self._process = None
        self._ready_process = None
        reader, self._reader_task = self._reader_task, None
        error = CodexTransportError(category)
        futures: list[asyncio.Future] = []
        for request_id, (pending_owner, future) in tuple(self._pending.items()):
            if pending_owner is owner:
                self._pending.pop(request_id, None)
                if not future.done():
                    futures.append(future)
        for future in futures:
            if not future.done():
                future.set_exception(error)
        if reader is not None and not from_reader and reader is not asyncio.current_task():
            reader.cancel()
        await self._terminate_owner(owner)

    async def close(self) -> None:
        self._closed = True
        owner = self._process
        if owner is not None:
            await self._fail_process("codex_app_server_unavailable", owner=owner)
        self._scopes.clear()
