"""Live-canary reliability hardening layered over the reviewed P2 foundation.

This module is imported only by the alternate Codex canary entrypoint.  It keeps the
core P2 modules unchanged while live qualification is still in progress.

Safety properties:
- a ``turn/completed`` notification may carry the final answer directly to the
  durable callback path, avoiding a projection-materialization race;
- history reconciliation remains the durable fallback and is bounded;
- no reconciliation path ever calls ``turn/start``;
- a pinned canary fails closed while generation is frozen instead of crossing back
  to the API provider.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import codex_generation_store as store
from .codex_0147_wire_compat import correlated_turn_from_page, final_answer_from_turn
from .codex_canary_loop_integration import (
    CodexCanaryLoopIntegration,
    CodexCanaryLoopIntegrationError,
)
from .codex_generation_protocol import GenerationNotification, project_notification
from .codex_generation_runtime import CodexGenerationRuntime
from .codex_generation_runtime_config import compose_shared_transport_config
from .codex_generation_worker import CodexGenerationWorker
from .codex_shared_provider_foundation import SharedCodexProviderFoundation


_RECONCILE_ATTEMPTS = 3
_RECONCILE_GRACE_SECONDS = 0.20


@dataclass(frozen=True)
class RichGenerationNotification:
    method: str
    thread_id: str
    turn_id: str
    terminal: bool
    will_retry: bool | None = None
    error_info: str | None = None
    usage: Mapping[str, int] | None = None
    turn_status: str | None = None
    final_answer: str | None = None


def enrich_generation_notification(
    method: str,
    params: Mapping[str, object],
) -> GenerationNotification | RichGenerationNotification | None:
    """Preserve bounded final-turn data that the base projection intentionally drops."""
    event = project_notification(method, params)
    if event is None or method != "turn/completed":
        return event
    raw_turn = params.get("turn") if isinstance(params, dict) else None
    if not isinstance(raw_turn, dict):
        return event
    status = raw_turn.get("status")
    if status not in {"completed", "failed", "interrupted"}:
        return event
    answer = final_answer_from_turn(raw_turn) if status == "completed" else None
    return RichGenerationNotification(
        method=event.method,
        thread_id=event.thread_id,
        turn_id=event.turn_id,
        terminal=event.terminal,
        will_retry=event.will_retry,
        error_info=event.error_info,
        usage=event.usage,
        turn_status=str(status),
        final_answer=answer,
    )


class ReliableSharedCodexProviderFoundation(SharedCodexProviderFoundation):
    async def _on_generation_notification(
        self,
        method: str,
        params: Mapping[str, object],
    ) -> None:
        event = enrich_generation_notification(method, params)
        if event is None or self._generation_event_handler is None:
            return
        outcome = self._generation_event_handler(event)
        if inspect.isawaitable(outcome):
            task = asyncio.ensure_future(outcome)
            task.add_done_callback(self._consume_callback_result)


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class ReliableCodexGenerationWorker(CodexGenerationWorker):
    """Deliver completed notification text first; reconcile only as a bounded fallback."""

    async def _unsubscribe(self, session: Mapping[str, object]) -> None:
        try:
            await self.protocol.unsubscribe(thread_id=str(session["thread_id"]))
        except Exception:
            pass

    async def _deliver_answer(
        self,
        job: Mapping[str, object],
        answer: str,
        usage: Mapping[str, int] | None,
        session: Mapping[str, object],
    ) -> None:
        try:
            assistant_message_id = await _maybe_await(
                self.completion_callback(job, answer, usage)
            )
        except Exception:
            # ``callback_pending`` remains recoverable and idempotent.
            return
        if isinstance(assistant_message_id, bool) or not isinstance(assistant_message_id, int):
            return
        store.mark_completed(
            self.store_path,
            job_id=int(job["id"]),
            assistant_message_id=assistant_message_id,
        )
        await self._unsubscribe(session)

    async def _handle_completed_notification(
        self,
        job_id: int,
        event: GenerationNotification | RichGenerationNotification,
        session: Mapping[str, object],
        usage: Mapping[str, int] | None,
    ) -> bool:
        status = getattr(event, "turn_status", None)
        if event.method != "turn/completed" or status not in {
            "completed",
            "failed",
            "interrupted",
        }:
            return False
        updated = store.record_reconciled_turn(
            self.store_path,
            job_id=job_id,
            turn_id=event.turn_id,
            status=str(status),
        )
        if updated["status"] == "failed":
            await self._unsubscribe(session)
            return True
        if updated["status"] != "callback_pending":
            return False
        answer = getattr(event, "final_answer", None)
        if not isinstance(answer, str) or not answer:
            # The projection may lag the completion notification by a few hundred ms.
            return False
        await self._deliver_answer(updated, answer, usage, session)
        return True

    async def _await_or_recover_terminal(
        self,
        job_id: int,
        turn_id: str,
        session: Mapping[str, object],
        persona: str,
    ) -> None:
        try:
            event, usage = await self.event_inbox.wait_terminal(
                turn_id,
                timeout_seconds=self.turn_timeout_seconds,
            )
        except TimeoutError:
            try:
                await self.protocol.interrupt(
                    thread_id=str(session["thread_id"]),
                    turn_id=turn_id,
                )
            except Exception:
                pass
            store.mark_dispatch_uncertain(self.store_path, job_id=job_id)
            return
        finally:
            await self.event_inbox.discard(turn_id)
        if await self._handle_completed_notification(job_id, event, session, usage):
            return
        await self._reconcile_and_maybe_deliver(job_id, session, persona, usage)

    async def _reconcile_and_maybe_deliver(
        self,
        job_id: int,
        session: Mapping[str, object],
        persona: str,
        usage: Mapping[str, int] | None,
    ) -> None:
        for attempt in range(_RECONCILE_ATTEMPTS):
            job = store.get_job(self.store_path, job_id)
            if job is None:
                return
            try:
                page = await self.protocol.resume_thread(
                    thread_id=str(session["thread_id"]),
                    model=str(session["model"]),
                    model_provider=str(session["model_provider"]),
                    reasoning_effort=session.get("reasoning_effort"),
                    cwd=Path(str(session["cwd"])),
                    persona=persona,
                )
            except Exception:
                return
            correlated = correlated_turn_from_page(page, str(job["client_message_id"]))
            if correlated is None:
                if attempt + 1 < _RECONCILE_ATTEMPTS:
                    await asyncio.sleep(_RECONCILE_GRACE_SECONDS * (attempt + 1))
                    continue
                if job["status"] in {
                    "turn_dispatching",
                    "in_progress",
                    "dispatch_uncertain",
                    "callback_pending",
                }:
                    store.mark_failed(
                        self.store_path,
                        job_id=job_id,
                        category="codex_generation_reconcile_unresolved",
                    )
                return
            updated = store.record_reconciled_turn(
                self.store_path,
                job_id=job_id,
                turn_id=correlated.turn_id,
                status=correlated.status,
            )
            if updated["status"] == "failed":
                await self._unsubscribe(session)
                return
            if updated["status"] == "in_progress":
                await self._await_or_recover_terminal(
                    job_id,
                    correlated.turn_id,
                    session,
                    persona,
                )
                return
            if updated["status"] != "callback_pending":
                return
            if correlated.final_answer:
                await self._deliver_answer(
                    updated,
                    correlated.final_answer,
                    usage,
                    session,
                )
                return
            if attempt + 1 < _RECONCILE_ATTEMPTS:
                await asyncio.sleep(_RECONCILE_GRACE_SECONDS * (attempt + 1))
                continue
            store.mark_failed(
                self.store_path,
                job_id=job_id,
                category="codex_generation_empty_response",
            )
            return


class ReliableCodexGenerationRuntime(CodexGenerationRuntime):
    """Alternate-canary runtime composed with the live reliability worker/foundation."""

    def __init__(
        self,
        *,
        control_config,
        generation_config,
        relay_db,
        persona_loader,
        completion_callback,
    ) -> None:
        foundation = ReliableSharedCodexProviderFoundation(
            compose_shared_transport_config(control_config, generation_config),
            generation_config.generation,
            control_enabled=control_config.enabled,
            generation_event_handler=None,
        )
        super().__init__(
            control_config=control_config,
            generation_config=generation_config,
            relay_db=relay_db,
            persona_loader=persona_loader,
            completion_callback=completion_callback,
            _foundation=foundation,
        )
        foundation._generation_event_handler = self.event_inbox.on_event
        self.worker = ReliableCodexGenerationWorker(
            store_path=generation_config.store_path,
            protocol=self.foundation.generation,
            activity_gate=self.foundation.activity_gate,
            persona_loader=persona_loader,
            canonical_message_loader=self._load_canonical_message,
            completion_callback=completion_callback,
            event_inbox=self.event_inbox,
        )


class FailClosedCodexCanaryLoopIntegration(CodexCanaryLoopIntegration):
    """A pinned canary never crosses back to API merely because generation is frozen."""

    async def handle_ingest(self, body: Mapping[str, object]):
        if isinstance(body, dict):
            text = str(body.get("text") or body.get("message") or "").strip()
            session_id = str(
                body.get("session_id")
                or body.get("api_session")
                or self.legacy.active_session_id()
                or ""
            ).strip()
            if (
                text
                and session_id
                and not self.runtime.generation_enabled
                and self.runtime.controller.is_pinned(session_id)
            ):
                raise CodexCanaryLoopIntegrationError(
                    "codex_generation_disabled",
                    status_code=503,
                )
        return await super().handle_ingest(body)
