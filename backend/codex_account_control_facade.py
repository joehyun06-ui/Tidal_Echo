"""P1-compatible account-control facade over the shared App Server runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from .codex_app_server_control import sanitize_account, sanitize_rate_limits, sanitize_usage
from .codex_app_server_shared_transport import CodexScopedTransport, CodexTransportError
from .codex_generation_protocol import CodexGenerationError, CodexProcessActivityGate


P1_ACCOUNT_RPC_METHODS = frozenset({
    "account/read",
    "account/login/start",
    "account/login/cancel",
    "account/logout",
    "account/rateLimits/read",
    "account/usage/read",
})
P1_ACCOUNT_NOTIFICATIONS = frozenset({
    "account/login/completed",
    "account/updated",
    "account/rateLimits/updated",
})


class CodexAccountFacadeError(RuntimeError):
    """Fixed category matching the public P1 control-plane error style."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category

    def __repr__(self) -> str:
        return f"<CodexAccountFacadeError category={self.category!r}>"


def _bounded_text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        return ""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    return value


class CodexAccountControlFacade:
    """Narrow P1 account control whose transport can be shared with P2 generation."""

    def __init__(
        self,
        transport: CodexScopedTransport,
        activity_gate: CodexProcessActivityGate,
        *,
        enabled: bool = True,
    ) -> None:
        self._transport = transport
        self._activity_gate = activity_gate
        self._enabled = bool(enabled)
        self._login_lock = asyncio.Lock()
        self._login_id = ""
        self._login_starting = False
        self._completed_login_id = ""

    async def on_notification(self, method: str, params: Mapping[str, object]) -> None:
        if not self._enabled or method != "account/login/completed":
            return
        completed_id = _bounded_text(params.get("loginId"), 256)
        if not completed_id:
            return
        async with self._login_lock:
            if completed_id == self._login_id:
                self._login_id = ""
                self._login_starting = False
            elif self._login_starting:
                self._completed_login_id = completed_id

    async def _request(self, method: str, params: Mapping[str, object] | None = None) -> object:
        if not self._enabled:
            raise CodexAccountFacadeError("codex_control_disabled")
        try:
            async with self._activity_gate.control():
                return await self._transport.request(method, params or {})
        except CodexGenerationError as exc:
            if exc.category == "codex_generation_busy":
                raise CodexAccountFacadeError("codex_generation_busy") from None
            raise CodexAccountFacadeError("codex_app_server_unavailable") from None
        except CodexTransportError as exc:
            if exc.category == "codex_app_server_disabled":
                raise CodexAccountFacadeError("codex_control_disabled") from None
            if exc.category == "codex_app_server_timeout":
                raise CodexAccountFacadeError("codex_app_server_timeout") from None
            if exc.category == "codex_app_server_protocol_error":
                raise CodexAccountFacadeError("codex_app_server_protocol_error") from None
            raise CodexAccountFacadeError("codex_app_server_unavailable") from None

    async def status(self) -> dict[str, object]:
        try:
            account = sanitize_account(await self._request(
                "account/read", {"refreshToken": False}
            ))
        except CodexAccountFacadeError:
            raise
        except Exception:
            raise CodexAccountFacadeError("codex_app_server_protocol_error") from None
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
                raise CodexAccountFacadeError("codex_not_authenticated")
            return sanitize_usage(await self._request("account/usage/read"))
        except CodexAccountFacadeError:
            raise
        except Exception:
            raise CodexAccountFacadeError("codex_usage_unavailable") from None

    async def login_start(self) -> dict[str, str]:
        if not self._enabled:
            raise CodexAccountFacadeError("codex_control_disabled")
        async with self._login_lock:
            if self._login_starting or self._login_id:
                raise CodexAccountFacadeError("codex_login_in_progress")
            self._login_starting = True
        try:
            result = await self._request(
                "account/login/start", {"type": "chatgptDeviceCode"}
            )
            if not isinstance(result, dict):
                raise ValueError
            login_id = _bounded_text(result.get("loginId"), 256)
            verification_url = _bounded_text(result.get("verificationUrl"), 2048)
            user_code = _bounded_text(result.get("userCode"), 128)
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
        except CodexAccountFacadeError as exc:
            async with self._login_lock:
                self._login_starting = False
                self._completed_login_id = ""
            if exc.category in {"codex_control_disabled", "codex_generation_busy"}:
                raise
            raise CodexAccountFacadeError("codex_login_unavailable") from None
        except Exception:
            async with self._login_lock:
                self._login_starting = False
                self._completed_login_id = ""
            raise CodexAccountFacadeError("codex_login_unavailable") from None

    async def login_cancel(self) -> dict[str, bool]:
        if not self._enabled:
            raise CodexAccountFacadeError("codex_control_disabled")
        async with self._login_lock:
            login_id = self._login_id
        if not login_id:
            raise CodexAccountFacadeError("codex_login_unavailable")
        try:
            result = await self._request("account/login/cancel", {"loginId": login_id})
            if type(result) is not dict or result.get("status") not in {"canceled", "notFound"}:
                raise ValueError
        except CodexAccountFacadeError as exc:
            if exc.category in {"codex_control_disabled", "codex_generation_busy"}:
                raise
            raise CodexAccountFacadeError("codex_login_unavailable") from None
        except Exception:
            raise CodexAccountFacadeError("codex_login_unavailable") from None
        async with self._login_lock:
            if self._login_id == login_id:
                self._login_id = ""
            self._completed_login_id = ""
        return {"cancelled": True}

    async def logout(self) -> dict[str, bool]:
        await self._request("account/logout")
        async with self._login_lock:
            self._login_id = ""
            self._login_starting = False
            self._completed_login_id = ""
        return {"logged_out": True}
