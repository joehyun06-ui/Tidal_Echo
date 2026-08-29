"""Opt-in api-loop integration controller for explicit Codex Web sessions.

P3-A makes the durable Web-session record the provider authority.  Existing rows
without a provider field remain API for backward compatibility.  Codex dispatch
requires both ``provider='codex'`` in that durable row and an active durable Codex
session pin; any disagreement fails closed rather than crossing providers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Mapping
from threading import RLock

from . import web_session_provider_authority
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


def _authority_status(category: str) -> int:
    if category in {
        "web_session_provider_invalid",
        "web_session_id_invalid",
        "web_session_title_invalid",
        "web_session_since_id_invalid",
        "web_session_created_at_invalid",
        "web_session_patch_invalid",
    }:
        return 400
    if category == "web_session_not_found":
        return 404
    if category in {"web_session_provider_immutable", "web_session_conflict"}:
        return 409
    return 503


class CodexCanaryLoopIntegration:
    def __init__(self, legacy, runtime) -> None:
        self.legacy = legacy
        self.runtime = runtime
        self._session_lock = RLock()
        self.session_authority = web_session_provider_authority.WebSessionProviderAuthority(
            legacy
        )
        self._original_create_session = legacy.create_session
        self._original_patch_session = legacy.patch_session
        self._original_save_sessions = legacy.save_sessions
        self._original_session_rows = legacy.session_rows
        self._original_active_session_id = legacy.active_session_id
        self._original_sessions_public = legacy.sessions_public

    def _authority_call(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except web_session_provider_authority.WebSessionProviderAuthorityError as exc:
            raise CodexCanaryLoopIntegrationError(
                exc.category,
                status_code=_authority_status(exc.category),
            ) from None

    def install_legacy_globals(self) -> None:
        """Use shared P1 control plus provider-aware serialized session mutations."""
        self.legacy.CODEX_CONTROL = LegacyControlAdapter(self.runtime.control)
        if getattr(self.legacy, "_CODEX_CANARY_SESSION_LOCK_INSTALLED", False):
            return

        def session_rows():
            with self._session_lock:
                return self._authority_call(self.session_authority.session_rows)

        def active_session_id():
            with self._session_lock:
                return self._authority_call(self.session_authority.active_session_id)

        def sessions_public():
            with self._session_lock:
                return self._authority_call(self.session_authority.sessions_public)

        def create_session(
            title="New chat",
            since_id=0,
            activate=True,
            provider=web_session_provider_authority.API_PROVIDER,
        ):
            if provider != web_session_provider_authority.API_PROVIDER:
                raise CodexCanaryLoopIntegrationError(
                    "web_session_provider_requires_async_creation",
                    status_code=409,
                )
            with self._session_lock:
                return self._authority_call(
                    self.session_authority.create_api_session,
                    title=title,
                    since_id=since_id,
                    activate=activate,
                )

        def patch_session(session_id, body):
            with self._session_lock:
                return self._authority_call(
                    self.session_authority.patch_session,
                    session_id,
                    body,
                )

        def save_sessions(rows, active=None):
            with self._session_lock:
                return self._authority_call(
                    self.session_authority.save_sessions,
                    rows,
                    active,
                )

        self.legacy.session_rows = session_rows
        self.legacy.active_session_id = active_session_id
        self.legacy.sessions_public = sessions_public
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

    def provider_for_session(self, session_id: str) -> str:
        with self._session_lock:
            return self._authority_call(
                self.session_authority.provider_for_session,
                session_id,
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
        provider = self.provider_for_session(session_id)
        pinned = bool(session_id and self.runtime.controller.is_pinned(session_id))
        if provider == web_session_provider_authority.API_PROVIDER:
            if pinned:
                raise CodexCanaryLoopIntegrationError(
                    "web_session_provider_authority_mismatch",
                    status_code=409,
                )
            return await self._legacy_ingest(body)
        if provider != web_session_provider_authority.CODEX_PROVIDER:
            raise CodexCanaryLoopIntegrationError(
                "web_session_provider_invalid",
                status_code=503,
            )
        if not self.runtime.generation_enabled:
            raise CodexCanaryLoopIntegrationError(
                "codex_generation_disabled",
                status_code=503,
            )
        if not pinned:
            raise CodexCanaryLoopIntegrationError(
                "web_session_provider_authority_mismatch",
                status_code=409,
            )
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
            raise CodexCanaryLoopIntegrationError(
                "web_session_provider_authority_mismatch",
                status_code=409,
            )
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

    async def create_web_session(
        self,
        *,
        provider: str,
        title: str = "New chat",
        since_id: int = 0,
        activate: bool = True,
    ) -> Mapping[str, object]:
        try:
            provider = web_session_provider_authority.normalize_provider(provider)
        except web_session_provider_authority.WebSessionProviderAuthorityError as exc:
            raise CodexCanaryLoopIntegrationError(
                exc.category,
                status_code=_authority_status(exc.category),
            ) from None
        if provider == web_session_provider_authority.API_PROVIDER:
            with self._session_lock:
                return self._authority_call(
                    self.session_authority.create_api_session,
                    title=title,
                    since_id=since_id,
                    activate=activate,
                )
        if not self.runtime.generation_enabled:
            raise CodexCanaryLoopIntegrationError("codex_generation_disabled", status_code=503)
        sid = (
            "api-"
            + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:4]
        )
        try:
            row = self.session_authority.new_row(
                title=title,
                since_id=since_id,
                provider=web_session_provider_authority.CODEX_PROVIDER,
                session_id=sid,
            )
        except web_session_provider_authority.WebSessionProviderAuthorityError as exc:
            raise CodexCanaryLoopIntegrationError(
                exc.category,
                status_code=_authority_status(exc.category),
            ) from None
        try:
            await self.runtime.controller.pin_session(sid)
        except CodexCanaryControllerError as exc:
            raise CodexCanaryLoopIntegrationError(exc.category, status_code=503) from None
        try:
            with self._session_lock:
                return self._authority_call(
                    self.session_authority.publish_row,
                    row,
                    activate=activate,
                )
        except Exception:
            try:
                self.runtime.controller.retire_session(sid)
            except Exception:
                pass
            raise

    async def create_canary_session(self, *, title: str = "Codex canary") -> Mapping[str, object]:
        return await self.create_web_session(
            provider=web_session_provider_authority.CODEX_PROVIDER,
            title=title,
            since_id=0,
            activate=False,
        )

    def patch_web_session(
        self,
        session_id: str,
        body: Mapping[str, object],
    ) -> Mapping[str, object]:
        with self._session_lock:
            return self._authority_call(
                self.session_authority.patch_session,
                session_id,
                body,
            )

    def sessions_public(self) -> Mapping[str, object]:
        with self._session_lock:
            return self._authority_call(self.session_authority.sessions_public)

    def retire_canary_session(self, api_session: str) -> Mapping[str, object]:
        try:
            return self.runtime.controller.retire_session(api_session)
        except CodexCanaryControllerError as exc:
            raise CodexCanaryLoopIntegrationError(exc.category, status_code=409) from None
