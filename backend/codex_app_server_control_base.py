"""Bounded ChatGPT account control over a pinned Codex App Server subprocess.

This module is deliberately a control plane only.  It contains no thread, turn,
item, tool, filesystem, MCP, review, shell, or model-generation operation.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from .deployment_config import CodexControlConfig


MAX_JSONL_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_CONTAINERS = 4096
MAX_OUTSTANDING_REQUESTS = 8
MAX_PROJECTED_RATE_LIMITS = 16
MAX_DAILY_USAGE_BUCKETS = 366
MAX_SAFE_NUMBER = 10**18
PROCESS_TEARDOWN_STEP_TIMEOUT_SECONDS = 2.0

P1_REQUEST_METHODS = frozenset({
    "initialize",
    "account/read",
    "account/login/start",
    "account/login/cancel",
    "account/logout",
    "account/rateLimits/read",
    "account/usage/read",
})
ALLOWED_NOTIFICATIONS = frozenset({
    "account/login/completed",
    "account/updated",
    "account/rateLimits/updated",
})
PARENT_ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "TZ",
)
KNOWN_PLAN_TYPES = frozenset({
    "free", "go", "plus", "pro", "team", "business", "enterprise", "edu",
})


class CodexControlError(RuntimeError):
    """A fixed, data-free control failure safe for an HTTP response."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category

    def __repr__(self) -> str:
        return f"<CodexControlError category={self.category!r}>"


class _UpstreamRpcError(Exception):
    pass


def _bundled_codex_path() -> str:
    # Intentionally runtime-only: disabled mode never imports the package.
    from codex_cli_bin import bundled_codex_path

    return str(bundled_codex_path())


def build_child_environment(
    config: CodexControlConfig,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct a minimal environment from empty, never from os.environ.copy()."""
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


def _validate_json_structure(value: object) -> None:
    remaining = MAX_JSON_CONTAINERS
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise ValueError
        if current is None or isinstance(current, (bool, int, str)):
            if isinstance(current, str) and len(current) > MAX_JSONL_BYTES:
                raise ValueError
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError
            continue
        if isinstance(current, list):
            remaining -= 1
            if remaining < 0 or len(current) > MAX_JSON_CONTAINERS:
                raise ValueError
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            remaining -= 1
            if remaining < 0 or len(current) > MAX_JSON_CONTAINERS:
                raise ValueError
            for key, item in current.items():
                if not isinstance(key, str) or len(key) > 256:
                    raise ValueError
                stack.append((item, depth + 1))
            continue
        raise ValueError


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError
        output[key] = value
    return output


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _bounded_text(value: object, *, maximum: int, fallback: str = "") -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        return fallback
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return fallback
    return value


def _bounded_number(value: object, *, maximum: float = MAX_SAFE_NUMBER) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > maximum:
        return None
    return int(value) if isinstance(value, int) else result


def _field(source: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in source:
            return source[name]
    return None


def _mapping_payload(result: object, *wrappers: str) -> Mapping[str, object]:
    if not isinstance(result, dict):
        raise ValueError
    current: Mapping[str, object] = result
    for name in wrappers:
        nested = current.get(name)
        if isinstance(nested, dict):
            current = nested
            break
    return current


def sanitize_account(result: object) -> dict[str, object]:
    payload = _mapping_payload(result)
    account = payload.get("account")
    requires = payload.get("requiresOpenaiAuth", payload.get("requires_openai_auth", False))
    requires_auth = requires if isinstance(requires, bool) else True
    if not isinstance(account, dict):
        return {
            "connected": False,
            "account_type": "",
            "plan_type": "unknown",
            "requires_openai_auth": requires_auth,
        }
    raw_type = _bounded_text(
        _field(account, "type", "accountType", "account_type"), maximum=32
    ).casefold()
    if raw_type not in {"chatgpt", "chatgptaccount"}:
        return {
            "connected": False,
            "account_type": "",
            "plan_type": "unknown",
            "requires_openai_auth": True,
        }
    raw_plan = _bounded_text(
        _field(account, "planType", "plan_type", "plan"), maximum=32
    ).casefold()
    plan = raw_plan if raw_plan in KNOWN_PLAN_TYPES else "unknown"
    return {
        "connected": True,
        "account_type": "chatgpt",
        "plan_type": plan,
        "requires_openai_auth": requires_auth,
    }


def _project_rate_limit(value: object, fallback_id: str = "") -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    projected: dict[str, object] = {}
    limit_id = _bounded_text(
        _field(value, "limitId", "limit_id", "id"), maximum=64, fallback=fallback_id
    )
    limit_name = _bounded_text(
        _field(value, "limitName", "limit_name", "name"), maximum=96
    )
    plan_type = _bounded_text(
        _field(value, "planType", "plan_type"), maximum=32
    ).casefold()
    if limit_id:
        projected["limit_id"] = limit_id
    if limit_name:
        projected["limit_name"] = limit_name
    if plan_type:
        projected["plan_type"] = plan_type if plan_type in KNOWN_PLAN_TYPES else "unknown"
    window = value.get("primary") if isinstance(value.get("primary"), dict) else value
    numeric = (
        ("used_percent", ("usedPercent", "used_percent"), 100),
        ("window_duration_mins", ("windowDurationMins", "window_duration_mins"), 10**7),
        ("resets_at", ("resetsAt", "resets_at"), 10**13),
    )
    for output_name, names, maximum in numeric:
        raw_number = _field(window, *names)
        number = _bounded_number(raw_number, maximum=maximum)
        if raw_number is not None and number is None:
            raise ValueError
        if number is not None:
            projected[output_name] = number
    # RateLimitSnapshot.credits is an optional CreditsSnapshot object in the
    # pinned protocol.  P1 deliberately does not project that object or its
    # balance.  Earned reset credits are handled separately from
    # rateLimitResetCredits.availableCount below.
    for output_name, names in (
        ("reset_count", ("resetCount", "reset_count")),
    ):
        raw_number = _field(value, *names)
        number = _bounded_number(raw_number)
        if raw_number is not None and number is None:
            raise ValueError
        if number is not None:
            projected[output_name] = number
    reached = _field(value, "reached", "limitReached", "limit_reached")
    if reached is not None and not isinstance(reached, bool):
        raise ValueError
    if isinstance(reached, bool):
        projected["reached"] = reached
    reached_type = _field(value, "rateLimitReachedType", "rate_limit_reached_type")
    if reached_type is not None:
        classification = _bounded_text(reached_type, maximum=64)
        if not classification:
            raise ValueError
        projected["limit_reached"] = classification
    return projected or None


def sanitize_rate_limits(result: object) -> dict[str, object]:
    payload = _mapping_payload(result)
    multi = payload.get("rateLimitsByLimitId", payload.get("rate_limits_by_limit_id"))
    if multi is not None:
        raw = multi
    else:
        single = payload.get("rateLimits", payload.get("rate_limits"))
        raw = [] if single is None else [single]
    projected: list[dict[str, object]] = []
    if isinstance(raw, list):
        inputs = [("", item) for item in raw[: MAX_PROJECTED_RATE_LIMITS + 1]]
    elif isinstance(raw, dict):
        inputs = list(raw.items())[: MAX_PROJECTED_RATE_LIMITS + 1]
    else:
        raise ValueError
    if len(inputs) > MAX_PROJECTED_RATE_LIMITS:
        raise ValueError
    for key, value in inputs:
        item = _project_rate_limit(value, _bounded_text(key, maximum=64))
        if item is not None:
            projected.append(item)
    if raw and not projected:
        raise ValueError
    output: dict[str, object] = {"rate_limits": projected}
    reset_credits = payload.get(
        "rateLimitResetCredits", payload.get("rate_limit_reset_credits")
    )
    if reset_credits is not None:
        if not isinstance(reset_credits, dict):
            raise ValueError
        available = _bounded_number(
            _field(reset_credits, "availableCount", "available_count"),
            maximum=MAX_SAFE_NUMBER,
        )
        if available is None:
            raise ValueError
        output["reset_credit_count"] = available
    return output


def sanitize_usage(result: object) -> dict[str, object]:
    payload = _mapping_payload(result, "usage")
    summary_value = payload.get("summary")
    if summary_value is None:
        summary: Mapping[str, object] = payload
    elif isinstance(summary_value, dict):
        summary = summary_value
    else:
        raise ValueError
    output: dict[str, object] = {}
    numeric = (
        ("lifetime_tokens", ("lifetimeTokens", "lifetime_tokens"), MAX_SAFE_NUMBER),
        ("peak_daily_tokens", ("peakDailyTokens", "peak_daily_tokens"), MAX_SAFE_NUMBER),
        ("longest_running_turn_sec", ("longestRunningTurnSec", "longest_running_turn_sec"), 10**12),
        ("current_streak_days", ("currentStreakDays", "current_streak_days"), 10**7),
        ("longest_streak_days", ("longestStreakDays", "longest_streak_days"), 10**7),
    )
    for output_name, names, maximum in numeric:
        raw = _field(summary, *names)
        if raw is not None:
            number = _bounded_number(raw, maximum=maximum)
            if number is None:
                raise ValueError
            output[output_name] = number
    raw_buckets = _field(payload, "dailyUsageBuckets", "daily_usage_buckets", "dailyUsage")
    buckets: list[dict[str, object]] = []
    if raw_buckets is not None:
        if not isinstance(raw_buckets, list) or len(raw_buckets) > MAX_DAILY_USAGE_BUCKETS:
            raise ValueError
        for raw_bucket in raw_buckets:
            if not isinstance(raw_bucket, dict):
                raise ValueError
            start_date = _bounded_text(
                _field(raw_bucket, "startDate", "start_date", "date"), maximum=32
            )
            tokens = _bounded_number(_field(raw_bucket, "tokens"))
            if not start_date or tokens is None:
                raise ValueError
            buckets.append({"start_date": start_date, "tokens": tokens})
        output["daily_usage_buckets"] = buckets
    elif "dailyUsageBuckets" in payload or "daily_usage_buckets" in payload:
        output["daily_usage_buckets"] = []
    if not output and "summary" not in payload:
        raise ValueError
    return output


class CodexAppServerControl:
    """Single lazy process owner with a narrow account-control API."""

    def __init__(
        self,
        config: CodexControlConfig,
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
        self._startup_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self._pending: dict[int, tuple[Any, asyncio.Future]] = {}
        self._next_id = 1
        self._login_id = ""
        self._login_starting = False
        self._completed_login_id = ""
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def _require_enabled(self) -> None:
        if not self._config.enabled:
            raise CodexControlError("codex_control_disabled")

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
            raise CodexControlError("codex_app_server_unavailable") from None

    async def _start_process(self) -> None:
        owner = await self._launch()
        self._process = owner
        if self._closed:
            await self._fail_process("codex_app_server_unavailable", owner=owner)
            raise CodexControlError("codex_app_server_unavailable")
        if owner.stdin is None or owner.stdout is None:
            await self._fail_process("codex_app_server_unavailable", owner=owner)
            raise CodexControlError("codex_app_server_unavailable")
        self._reader_task = asyncio.create_task(self._reader_loop(owner))
        try:
            await self._request_started("initialize", {
                "clientInfo": {"name": "tidal-echo-provider-control", "version": "1"},
                "capabilities": {"experimentalApi": True},
            }, owner=owner)
            await self._write_message(
                {"method": "initialized", "params": {}}, owner=owner
            )
            if owner is not self._process or owner.returncode is not None:
                raise CodexControlError("codex_app_server_unavailable")
            self._ready_process = owner
        except asyncio.CancelledError:
            await self._fail_process("codex_app_server_unavailable", owner=owner)
            raise
        except CodexControlError:
            await self._fail_process("codex_app_server_unavailable", owner=owner)
            raise
        except Exception:
            await self._fail_process("codex_app_server_unavailable", owner=owner)
            raise CodexControlError("codex_app_server_unavailable") from None

    async def _ensure_started(self) -> None:
        self._require_enabled()
        if self._closed:
            raise CodexControlError("codex_app_server_unavailable")
        process = self._process
        if (
            process is not None
            and process is self._ready_process
            and process.returncode is None
        ):
            return
        async with self._start_lock:
            process = self._process
            if (
                process is not None
                and process is self._ready_process
                and process.returncode is None
            ):
                return
            startup = self._startup_task
            if startup is None or startup.done():
                startup = asyncio.create_task(self._start_process())
                self._startup_task = startup
        try:
            await asyncio.shield(startup)
        except asyncio.CancelledError:
            raise
        except CodexControlError as exc:
            raise CodexControlError(exc.category) from None
        except Exception:
            raise CodexControlError("codex_app_server_unavailable") from None
        process = self._process
        if (
            process is None
            or process is not self._ready_process
            or process.returncode is not None
        ):
            raise CodexControlError("codex_app_server_unavailable")

    async def _write_message(
        self, message: dict[str, object], *, owner: Any | None = None
    ) -> None:
        try:
            encoded = json.dumps(
                message, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8") + b"\n"
            if len(encoded) > MAX_JSONL_BYTES:
                raise ValueError
            process = self._process if owner is None else owner
            if process is None or process.returncode is not None or process.stdin is None:
                raise OSError
            async with self._write_lock:
                if process is not self._process or process.returncode is not None:
                    raise OSError
                process.stdin.write(encoded)
                await process.stdin.drain()
        except CodexControlError:
            raise
        except Exception:
            raise CodexControlError("codex_app_server_unavailable") from None

    async def _request_started(
        self,
        method: str,
        params: dict[str, object],
        *,
        owner: Any | None = None,
    ) -> object:
        if method not in P1_REQUEST_METHODS:
            raise CodexControlError("codex_app_server_protocol_error")
        if len(self._pending) >= MAX_OUTSTANDING_REQUESTS:
            raise CodexControlError("codex_app_server_unavailable")
        request_owner = self._process if owner is None else owner
        if request_owner is None or request_owner is not self._process:
            raise CodexControlError("codex_app_server_unavailable")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        pending = (request_owner, future)
        self._pending[request_id] = pending
        write_started = False
        try:
            write_started = True
            await self._write_message(
                {"id": request_id, "method": method, "params": params},
                owner=request_owner,
            )
            try:
                async with asyncio.timeout(self._config.request_timeout_seconds):
                    return await future
            except TimeoutError:
                await self._fail_process(
                    "codex_app_server_unavailable", owner=request_owner
                )
                raise CodexControlError("codex_app_server_timeout") from None
        except asyncio.CancelledError:
            if write_started:
                await self._fail_process(
                    "codex_app_server_unavailable", owner=request_owner
                )
            raise
        finally:
            if self._pending.get(request_id) == pending:
                self._pending.pop(request_id, None)

    async def _request(self, method: str, params: dict[str, object] | None = None) -> object:
        await self._ensure_started()
        return await self._request_started(method, params or {})

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
                    _validate_json_structure(message)
                    await self._consume_message(message, owner=owner)
                except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
                    category = "codex_app_server_protocol_error"
                    break
        except asyncio.CancelledError:
            return
        except Exception:
            category = "codex_app_server_protocol_error"
        finally:
            if owner is self._process:
                await self._fail_process(category, owner=owner, from_reader=True)

    async def _consume_message(self, message: object, *, owner: Any | None = None) -> None:
        message_owner = self._process if owner is None else owner
        if message_owner is not self._process:
            return
        if not isinstance(message, dict) or (
            "jsonrpc" in message and message.get("jsonrpc") != "2.0"
        ):
            raise ValueError
        if "method" in message:
            method = message.get("method")
            if not isinstance(method, str):
                raise ValueError
            if "id" in message:
                await self._write_message({
                    "id": message.get("id"),
                    "error": {"code": -32601, "message": "method not supported"},
                }, owner=message_owner)
                return
            if method not in ALLOWED_NOTIFICATIONS:
                return
            if method == "account/login/completed":
                params = message.get("params")
                completed_id = _bounded_text(
                    params.get("loginId") if isinstance(params, dict) else None,
                    maximum=256,
                )
                async with self._login_lock:
                    if completed_id and completed_id == self._login_id:
                        self._login_id = ""
                        self._login_starting = False
                    elif completed_id and self._login_starting:
                        self._completed_login_id = completed_id
            return
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            raise ValueError
        pending = self._pending.get(request_id)
        if pending is None or pending[0] is not message_owner:
            raise ValueError
        future = pending[1]
        if future.done():
            raise ValueError
        if ("result" in message) == ("error" in message):
            raise ValueError
        if "error" in message:
            future.set_exception(_UpstreamRpcError())
        else:
            future.set_result(message["result"])

    @staticmethod
    async def _bounded_teardown_await(awaitable: Awaitable[Any]) -> None:
        try:
            async with asyncio.timeout(PROCESS_TEARDOWN_STEP_TIMEOUT_SECONDS):
                await awaitable
        except (asyncio.CancelledError, TimeoutError, Exception):
            pass

    async def _fail_process(
        self,
        category: str,
        *,
        owner: Any | None = None,
        from_reader: bool = False,
    ) -> None:
        process = self._process if owner is None else owner
        if process is None or process is not self._process:
            return
        self._process = None
        self._ready_process = None
        reader, self._reader_task = self._reader_task, None
        error = CodexControlError(category)
        failed_futures: list[asyncio.Future] = []
        for request_id, (pending_owner, future) in tuple(self._pending.items()):
            if pending_owner is process:
                self._pending.pop(request_id, None)
                if not future.done():
                    failed_futures.append(future)
        if not from_reader:
            for future in failed_futures:
                if not future.done():
                    future.set_exception(error)
        if not from_reader and reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except Exception:
                pass
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
                await self._bounded_teardown_await(process.stdin.wait_closed())
            except Exception:
                pass
        if process is not None:
            try:
                await self._bounded_teardown_await(process.wait())
            except Exception:
                pass
        if not from_reader and reader is not None and reader is not asyncio.current_task():
            try:
                await self._bounded_teardown_await(reader)
            except (asyncio.CancelledError, Exception):
                pass
        if from_reader:
            for future in failed_futures:
                if not future.done():
                    future.set_exception(error)

    async def close(self) -> None:
        self._closed = True
        await self._fail_process("codex_app_server_unavailable")

    async def status(self) -> dict[str, object]:
        try:
            account = sanitize_account(await self._request(
                "account/read", {"refreshToken": False}
            ))
        except CodexControlError:
            raise
        except Exception:
            raise CodexControlError("codex_app_server_protocol_error") from None
        try:
            limits = sanitize_rate_limits(await self._request("account/rateLimits/read"))
        except Exception:
            limits = {"rate_limits": []}
        return {**account, **limits}

    async def usage(self) -> dict[str, object]:
        try:
            account = sanitize_account(await self._request(
                "account/read", {"refreshToken": False}
            ))
            if not account["connected"]:
                raise CodexControlError("codex_not_authenticated")
            return sanitize_usage(await self._request("account/usage/read"))
        except CodexControlError:
            raise
        except Exception:
            raise CodexControlError("codex_usage_unavailable") from None

    async def login_start(self) -> dict[str, str]:
        self._require_enabled()
        async with self._login_lock:
            if self._login_starting or self._login_id:
                raise CodexControlError("codex_login_in_progress")
            self._login_starting = True
        try:
            result = await self._request(
                "account/login/start", {"type": "chatgptDeviceCode"}
            )
            if not isinstance(result, dict):
                raise ValueError
            login_id = _bounded_text(result.get("loginId"), maximum=256)
            verification_url = _bounded_text(
                result.get("verificationUrl"), maximum=2048
            )
            user_code = _bounded_text(result.get("userCode"), maximum=128)
            if not login_id or not verification_url or not user_code:
                raise ValueError
            async with self._login_lock:
                if self._completed_login_id == login_id:
                    self._completed_login_id = ""
                    self._login_id = ""
                else:
                    self._login_id = login_id
                self._login_starting = False
            return {
                "verification_url": verification_url,
                "user_code": user_code,
                "status": "pending",
            }
        except CodexControlError as exc:
            async with self._login_lock:
                self._login_starting = False
                self._completed_login_id = ""
            if exc.category == "codex_control_disabled":
                raise
            raise CodexControlError("codex_login_unavailable") from None
        except Exception:
            async with self._login_lock:
                self._login_starting = False
                self._completed_login_id = ""
            raise CodexControlError("codex_login_unavailable") from None

    async def login_cancel(self) -> dict[str, bool]:
        self._require_enabled()
        async with self._login_lock:
            login_id = self._login_id
        if not login_id:
            raise CodexControlError("codex_login_unavailable")
        try:
            result = await self._request(
                "account/login/cancel", {"loginId": login_id}
            )
            if type(result) is not dict:
                raise ValueError
            status = result.get("status")
            if type(status) is not str or status not in {"canceled", "notFound"}:
                raise ValueError
        except Exception:
            raise CodexControlError("codex_login_unavailable") from None
        async with self._login_lock:
            if self._login_id == login_id:
                self._login_id = ""
            self._completed_login_id = ""
        return {"cancelled": True}

    async def logout(self) -> dict[str, bool]:
        try:
            await self._request("account/logout")
        except CodexControlError:
            raise
        except Exception:
            raise CodexControlError("codex_app_server_unavailable") from None
        async with self._login_lock:
            self._login_id = ""
            self._login_starting = False
            self._completed_login_id = ""
        return {"logged_out": True}
