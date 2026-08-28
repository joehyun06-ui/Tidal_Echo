"""Observable P1 account control without exposing raw Codex auth failures.

The pinned transport/control implementation lives in ``codex_app_server_control_base``.
This compatibility layer keeps its RPC/notification surface unchanged and only adds a
bounded login-attempt status for live qualification.
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import codex_app_server_control_base as _base
from .codex_app_server_control_base import *  # noqa: F401,F403


_LOGIN_IDLE = "idle"
_LOGIN_PENDING = "pending"
_LOGIN_SUCCEEDED = "succeeded"
_LOGIN_FAILED = "failed"
_LOGIN_CANCELLED = "cancelled"


class CodexAppServerControl(_base.CodexAppServerControl):
    """P1 control with fixed, data-free login completion observability."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._login_status = _LOGIN_IDLE
        self._raced_login_id = ""
        self._raced_login_status = ""

    @staticmethod
    async def _bounded_teardown_await(awaitable) -> None:
        """Keep public-module timeout patching compatible with the original P1 class."""
        try:
            async with asyncio.timeout(PROCESS_TEARDOWN_STEP_TIMEOUT_SECONDS):
                await awaitable
        except (asyncio.CancelledError, TimeoutError, Exception):
            pass

    async def _consume_message(self, message: object, *, owner: Any | None = None) -> None:
        if isinstance(message, dict) and message.get("method") == "account/login/completed":
            params = message.get("params")
            if not isinstance(params, dict):
                return
            completed_id = _base._bounded_text(params.get("loginId"), maximum=256)
            success = params.get("success")
            # 0.147.0 guarantees both fields. A malformed completion must not clear
            # the retained login id or poison the reader with upstream data.
            if not completed_id or type(success) is not bool:
                return
            completion_status = _LOGIN_SUCCEEDED if success else _LOGIN_FAILED
            async with self._login_lock:
                if completed_id == self._login_id:
                    self._login_status = completion_status
                elif self._login_starting:
                    self._raced_login_id = completed_id
                    self._raced_login_status = completion_status
            # The base implementation owns correlation cleanup. Raw ``error`` is
            # intentionally never copied, logged, returned, or retained here.
            await super()._consume_message(message, owner=owner)
            return
        await super()._consume_message(message, owner=owner)

    async def _fail_process(
        self,
        category: str,
        *,
        owner: Any | None = None,
        from_reader: bool = False,
    ) -> None:
        target = self._process if owner is None else owner
        owned = target is not None and target is self._process
        pending = self._login_starting or bool(self._login_id) or self._login_status == _LOGIN_PENDING
        await super()._fail_process(category, owner=owner, from_reader=from_reader)
        if not owned:
            return
        async with self._login_lock:
            if pending:
                self._login_status = _LOGIN_FAILED
            self._login_id = ""
            self._login_starting = False
            self._completed_login_id = ""
            self._raced_login_id = ""
            self._raced_login_status = ""

    async def status(self) -> dict[str, object]:
        result = await super().status()
        async with self._login_lock:
            attempt_status = self._login_status
        login_status = _LOGIN_SUCCEEDED if result.get("connected") is True else attempt_status
        return {**result, "login_status": login_status}

    async def login_start(self) -> dict[str, str]:
        async with self._login_lock:
            duplicate = self._login_starting or bool(self._login_id)
            if not duplicate:
                self._login_status = _LOGIN_PENDING
                self._raced_login_id = ""
                self._raced_login_status = ""
        try:
            result = await super().login_start()
        except _base.CodexControlError as exc:
            if exc.category not in {"codex_control_disabled", "codex_login_in_progress"}:
                async with self._login_lock:
                    self._login_status = _LOGIN_FAILED
                    self._raced_login_id = ""
                    self._raced_login_status = ""
            raise
        except Exception:
            async with self._login_lock:
                self._login_status = _LOGIN_FAILED
                self._raced_login_id = ""
                self._raced_login_status = ""
            raise

        async with self._login_lock:
            if self._login_id:
                self._login_status = _LOGIN_PENDING
                # A completion for an unrelated old attempt may race while the new
                # login/start response is in flight. Never let that stale id survive.
                if self._completed_login_id and self._completed_login_id != self._login_id:
                    self._completed_login_id = ""
                self._raced_login_id = ""
                self._raced_login_status = ""
            elif self._raced_login_id and self._raced_login_status:
                self._login_status = self._raced_login_status
                self._raced_login_id = ""
                self._raced_login_status = ""
        return result

    async def login_cancel(self) -> dict[str, bool]:
        result = await super().login_cancel()
        async with self._login_lock:
            self._login_status = _LOGIN_CANCELLED
            self._raced_login_id = ""
            self._raced_login_status = ""
        return result

    async def logout(self) -> dict[str, bool]:
        result = await super().logout()
        async with self._login_lock:
            self._login_status = _LOGIN_IDLE
            self._raced_login_id = ""
            self._raced_login_status = ""
        return result


def __getattr__(name: str):
    """Preserve private compatibility for existing tests/internal callers."""
    return getattr(_base, name)
