"""Opt-in api-loop integration controller for the explicit Codex Web canary.

The current `examples.api_loop` remains the authority for every unpinned surface.
Only an explicitly pinned session is intercepted, and pinned ineligible input fails
closed rather than crossing back to the API provider.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Mapping
from threading import RLock

from .codex_account_control_facade import CodexAccountFacadeError
from .codex_app_server_control import CodexControlError
from .codex_canary_controller import CodexCanaryControllerError


class CodexCanaryLoopIntegrationError(RuntimeError):
    def __init__(self, category: str, *, status_code: int = 409):
        super().__init__(category)
        self.category = category
        self.status_code = status_code


class LegacyControlAdapter:
    """Translate shared P1 facade errors back to the existing api-loop contract."""

    def __init__(self, control) -> None:
        self._control = control

    async def _call(self, name: str):
        try:
            return await getattr(self._control, name)()
        except CodexAccountFacadeError as exc:
            raise CodexControlError(exc.category) from None

    async def status(self):
        return await self._call("status")

    async def usage(self):
        return await self._call("usage")

    async def login_start(self):
        return await self._call("login_start")

    async def login_cancel(self):
        return await self._call("login_cancel")

    async def logout(self):
        return await self._call("logout")

    async def close(self) -> None:
        # The parent CodexGenerationRuntime owns and closes the one shared process.
        return None


def build_completion_callback(legacy):
    async def completion_callback(job: Mapping[str, object], text: str, usage):
        payload = {
            "type": "reply",
            "text": text,
            "channel": "web",
            "source": "codex_generation",
            "provider": "codex",
            "api_session": str(job["api_session"]),
            "reply_to": str(job["canonical_message_id"]),
            "generation_id": str(job["generation_id"]),
            "client_message_id": str(job["client_message_id"]),
            "codex_callback_identity": str(job["callback_identity"]),
        }
        if usage:
            payload["usage"] = dict(usage)
        ok, body, uncertain = await legacy.relay_out(payload)
        if not ok:
            category = "codex_callback_uncertain" if uncertain else "codex_callback_failed"
            raise CodexCanaryLoopIntegrationError(category, status_code=504 if uncertain else 502)
        if not isinstance(body, dict):
            raise CodexCanaryLoopIntegrationError("codex_callback_invalid", status_code=502)
        message_id = body.get("id")
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise CodexCanaryLoopIntegrationError("codex_callback_invalid", status_code=502)
        return message_id

    return completion_callback


class CodexCanaryLoopIntegration:
    def __init__(self, legacy, runtime) -> None:
        self.legacy = legacy
        self.runtime = runtime
        self._session_lock = RLock()
        self._original_create_session = legacy.create_session
        self._original_patch_session = legacy.patch_session
        self._original_save_sessions = legacy.save_sessions

    def install_legacy_globals(self) -> None:
        """Use the shared P1 facade and serialize legacy session mutations in this entrypoint."""
        self.legacy.CODEX_CONTROL = LegacyControlAdapter(self.runtime.control)
        if getattr(self.legacy, "_CODEX_CANARY_SESSION_LOCK_INSTALLED", False):
            return

        def create_session(*args, **kwargs):
            with self._session_lock:
                return self._original_create_session(*args, **kwargs)

        def patch_session(*args, **kwargs):
            with self._session_lock:
                return self._original_patch_session(*args, **kwargs)

        def save_sessions(*args, **kwargs):
            with self._session_lock:
                return self._original_save_sessions(*args, **kwargs)

        self.legacy.create_session = create_session
        self.legacy.patch_session = patch_session
        self.legacy.save_sessions = save_sessions
        self.legacy._CODEX_CANARY_SESSION_LOCK_INSTALLED = True

    def _continuity_status(self, *, msg_id: int, text: str) -> str:
        if not self.legacy.TRANSIENT_CONTINUITY_ENABLED:
            return "empty"
        try:
            derived = self.legacy.continuity_context.derive_continuity_context(
                self.legacy.RELAY_DB,
                msg_id,
                text,
            )
        except Exception:
            self.legacy._log_continuity_context("unavailable")
            return "unavailable"
        if derived.developer_message is None:
            self.legacy._log_continuity_context(
                "empty",
                current_channel=derived.current_channel,
            )
            return "empty"
        self.legacy._log_continuity_context(
            "applied",
            current_channel=derived.current_channel,
            item_count=len(derived.items),
            total_chars=derived.total_chars,
        )
        return "applied"

    async def _legacy_ingest(self, body: Mapping[str, object]):
        text = str(body.get("text") or body.get("message") or "").strip()
        if not text:
            raise CodexCanaryLoopIntegrationError("empty text", status_code=400)
        msg_id = body.get("id")
        try:
            before_id = int(msg_id) if msg_id is not None else None
        except Exception:
            before_id = None
        session_id = str(
            body.get("session_id")
            or body.get("api_session")
            or self.legacy.active_session_id()
            or ""
        ).strip()
        return await self.legacy.handle_ingest(
            text,
            before_id,
            session_id,
            dry=bool(body.get("dry")),
            stream_id=str(body.get("stream_id") or "").strip(),
            generation_id=str(body.get("generation_id") or "").strip(),
            reply_to=str(body.get("reply_to") or "").strip(),
            channel=str(body.get("channel") or "").strip(),
            channel_account=str(body.get("channel_account") or "").strip(),
            channel_conversation=str(body.get("channel_conversation") or "").strip(),
        )

    async def handle_ingest(self, body: Mapping[str, object]):
        if not isinstance(body, dict):
            raise CodexCanaryLoopIntegrationError("invalid body", status_code=400)
        text = str(body.get("text") or body.get("message") or "").strip()
        if not text:
            raise CodexCanaryLoopIntegrationError("empty text", status_code=400)
        session_id = str(
            body.get("session_id")
            or body.get("api_session")
            or self.legacy.active_session_id()
            or ""
        ).strip()
        if not self.runtime.generation_enabled or not self.runtime.controller.is_pinned(session_id):
            return await self._legacy_ingest(body)
        if bool(body.get("dry")):
            raise CodexCanaryLoopIntegrationError("codex_canary_dry_unsupported", status_code=409)
        msg_id = body.get("id")
        if isinstance(msg_id, bool):
            raise CodexCanaryLoopIntegrationError("codex_canary_message_invalid", status_code=409)
        try:
            canonical_message_id = int(msg_id)
        except (TypeError, ValueError):
            raise CodexCanaryLoopIntegrationError("codex_canary_message_invalid", status_code=409) from None
        if canonical_message_id <= 0:
            raise CodexCanaryLoopIntegrationError("codex_canary_message_invalid", status_code=409)
        continuity_status = self._continuity_status(
            msg_id=canonical_message_id,
            text=text,
        )
        try:
            accepted = self.runtime.controller.admit_if_pinned(
                canonical_message_id=canonical_message_id,
                api_session=session_id,
                ingress_text=text,
                continuity_status=continuity_status,
            )
        except CodexCanaryControllerError as exc:
            raise CodexCanaryLoopIntegrationError(exc.category, status_code=409) from None
        if accepted is None:
            # Pin disappeared between the first read and admission. Do not cross provider.
            raise CodexCanaryLoopIntegrationError("codex_canary_session_unavailable", status_code=409)
        return {
            "ok": True,
            "queued": True,
            "provider": "codex",
            "generation_provider": "codex",
            "generation_id": accepted["generation_id"],
            "api_session": accepted["api_session"],
            "canonical_message_id": accepted["canonical_message_id"],
            "status": accepted["status"],
        }

    async def create_canary_session(self, *, title: str = "Codex canary") -> Mapping[str, object]:
        if not self.runtime.generation_enabled:
            raise CodexCanaryLoopIntegrationError("codex_generation_disabled", status_code=503)
        sid = (
            "api-"
            + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:4]
        )
        try:
            await self.runtime.controller.pin_session(sid)
        except CodexCanaryControllerError as exc:
            raise CodexCanaryLoopIntegrationError(exc.category, status_code=503) from None
        row = {
            "id": sid,
            "title": (title or "Codex canary").strip()[:120] or "Codex canary",
            "since_id": 0,
            "created_at": self.legacy.now_iso(),
        }
        try:
            with self._session_lock:
                rows = self.legacy.session_rows()
                if any(item.get("id") == sid for item in rows):
                    raise CodexCanaryLoopIntegrationError("codex_canary_session_conflict")
                rows.append(row)
                self._original_save_sessions(rows, None)
        except Exception:
            try:
                self.runtime.controller.retire_session(sid)
            except Exception:
                pass
            raise
        return row

    def retire_canary_session(self, api_session: str) -> Mapping[str, object]:
        try:
            return self.runtime.controller.retire_session(api_session)
        except CodexCanaryControllerError as exc:
            raise CodexCanaryLoopIntegrationError(exc.category, status_code=409) from None
