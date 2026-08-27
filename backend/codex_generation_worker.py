"""P2-B Codex generation worker over the durable store and P2-A protocol.

The worker is deliberately transport-agnostic: canonical input loading and relay
completion are injected. Nothing in this module is wired into api_loop yet.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Protocol

from . import codex_generation_store as store
from .codex_generation_protocol import (
    CodexGenerationError,
    CodexGenerationProtocol,
    CodexProcessActivityGate,
    GenerationNotification,
    correlated_turn_from_page,
    deterministic_workspace,
    input_digest,
)


class CanonicalMessageLoader(Protocol):
    def __call__(self, job: Mapping[str, object]) -> str | Awaitable[str]: ...


class CompletionCallback(Protocol):
    def __call__(
        self,
        job: Mapping[str, object],
        text: str,
        usage: Mapping[str, int] | None,
    ) -> int | Awaitable[int]: ...


class PersonaLoader(Protocol):
    def __call__(self) -> str: ...


_FATAL_PRE_TURN_CATEGORIES = frozenset({
    "codex_generation_disabled",
    "codex_generation_account_unavailable",
    "codex_generation_model_unavailable",
    "codex_generation_provider_unavailable",
    "codex_generation_persona_invalid",
    "codex_generation_thread_contract_mismatch",
})


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _persona_digest(persona: str) -> str:
    return hashlib.sha256(persona.encode("utf-8")).hexdigest()


class CodexGenerationEventInbox:
    """Small bounded per-turn event buffer safe when notifications beat the waiter."""

    def __init__(self, *, max_turns: int = 32, max_events_per_turn: int = 16) -> None:
        self._max_turns = max_turns
        self._max_events = max_events_per_turn
        self._queues: dict[str, asyncio.Queue[GenerationNotification]] = {}
        self._order: list[str] = []
        self._lock = asyncio.Lock()

    async def on_event(self, event: GenerationNotification) -> None:
        if not event.turn_id:
            return
        async with self._lock:
            queue = self._queues.get(event.turn_id)
            if queue is None:
                while len(self._queues) >= self._max_turns and self._order:
                    old = self._order.pop(0)
                    self._queues.pop(old, None)
                queue = asyncio.Queue(maxsize=self._max_events)
                self._queues[event.turn_id] = queue
                self._order.append(event.turn_id)
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def wait_terminal(
        self,
        turn_id: str,
        *,
        timeout_seconds: float,
    ) -> tuple[GenerationNotification, Mapping[str, int] | None]:
        async with self._lock:
            queue = self._queues.get(turn_id)
            if queue is None:
                queue = asyncio.Queue(maxsize=self._max_events)
                self._queues[turn_id] = queue
                self._order.append(turn_id)
        usage: Mapping[str, int] | None = None
        async with asyncio.timeout(timeout_seconds):
            while True:
                event = await queue.get()
                if event.usage is not None:
                    usage = event.usage
                if event.terminal:
                    return event, usage

    async def discard(self, turn_id: str) -> None:
        async with self._lock:
            self._queues.pop(turn_id, None)
            try:
                self._order.remove(turn_id)
            except ValueError:
                pass


class CodexGenerationWorker:
    def __init__(
        self,
        *,
        store_path: str | Path,
        protocol: CodexGenerationProtocol,
        activity_gate: CodexProcessActivityGate,
        persona_loader: PersonaLoader,
        canonical_message_loader: CanonicalMessageLoader,
        completion_callback: CompletionCallback,
        event_inbox: CodexGenerationEventInbox | None = None,
        turn_timeout_seconds: float = 120.0,
    ) -> None:
        self.store_path = Path(store_path)
        self.protocol = protocol
        self.activity_gate = activity_gate
        self.persona_loader = persona_loader
        self.canonical_message_loader = canonical_message_loader
        self.completion_callback = completion_callback
        self.event_inbox = event_inbox or CodexGenerationEventInbox()
        if turn_timeout_seconds <= 0 or turn_timeout_seconds > 900:
            raise ValueError("invalid_codex_generation_turn_timeout")
        self.turn_timeout_seconds = float(turn_timeout_seconds)

    async def on_generation_event(self, event: GenerationNotification) -> None:
        await self.event_inbox.on_event(event)

    async def run_once(self) -> bool:
        recovery = store.claim_recovery_job(self.store_path)
        if recovery is not None:
            await self._recover(recovery)
            return True
        job = store.claim_next_job(self.store_path)
        if job is None:
            return False
        await self._process(job)
        return True

    def _load_persona(self, session: Mapping[str, object]) -> str:
        persona = self.persona_loader()
        if not isinstance(persona, str) or not persona.strip():
            raise CodexGenerationError("codex_generation_persona_invalid")
        if _persona_digest(persona) != session.get("persona_hash"):
            raise CodexGenerationError("codex_generation_persona_contract_changed")
        return persona

    async def _load_input(self, job: Mapping[str, object]) -> str:
        text = await _maybe_await(self.canonical_message_loader(job))
        if not isinstance(text, str):
            raise CodexGenerationError("codex_generation_input_unavailable")
        if input_digest(text) != job.get("input_digest"):
            raise CodexGenerationError("codex_generation_input_contract_changed")
        return text

    async def _process(self, job: Mapping[str, object]) -> None:
        job_id = int(job["id"])
        session = store.get_session(self.store_path, str(job["api_session"]))
        if session is None or session.get("status") != "active":
            store.mark_failed(self.store_path, job_id=job_id, category="session_unavailable")
            return
        try:
            persona = self._load_persona(session)
            text = await self._load_input(job)
        except CodexGenerationError as exc:
            store.mark_failed(self.store_path, job_id=job_id, category=exc.category)
            return

        async with self.activity_gate.generation():
            session = store.get_session(self.store_path, str(job["api_session"])) or session
            if not session.get("thread_id"):
                ok = await self._create_thread(job, session, persona)
                if not ok:
                    return
                session = store.get_session(self.store_path, str(job["api_session"])) or session
            await self._dispatch_turn(job_id, session, text, persona)

    async def _create_thread(
        self,
        job: Mapping[str, object],
        session: Mapping[str, object],
        persona: str,
    ) -> bool:
        job_id = int(job["id"])
        attempt_id = f"attempt-{job_id}-{int(job['attempt_count'])}"
        cwd = deterministic_workspace(
            self.protocol.config.workspace_root,
            str(job["api_session"]),
            attempt_id,
        )
        store.begin_thread_dispatch(
            self.store_path,
            job_id=job_id,
            thread_attempt_id=attempt_id,
            cwd=str(cwd),
        )
        try:
            result = await self.protocol.start_thread(
                api_session=str(job["api_session"]),
                attempt_id=attempt_id,
                persona=persona,
            )
        except CodexGenerationError as exc:
            if exc.category in _FATAL_PRE_TURN_CATEGORIES:
                store.mark_failed(self.store_path, job_id=job_id, category=exc.category)
            else:
                store.abandon_thread_attempt_and_requeue(self.store_path, job_id=job_id)
            return False
        if result.model != session.get("model"):
            store.mark_failed(
                self.store_path, job_id=job_id, category="codex_generation_model_contract_changed"
            )
            return False
        pinned_provider = session.get("model_provider")
        if pinned_provider and result.model_provider != pinned_provider:
            store.mark_failed(
                self.store_path, job_id=job_id, category="codex_generation_provider_contract_changed"
            )
            return False
        if result.reasoning_effort != session.get("reasoning_effort"):
            store.mark_failed(
                self.store_path, job_id=job_id, category="codex_generation_effort_contract_changed"
            )
            return False
        store.bind_session_thread(
            self.store_path,
            job_id=job_id,
            thread_attempt_id=attempt_id,
            thread_id=result.thread_id,
            cwd=str(result.cwd),
        )
        return True

    async def _dispatch_turn(
        self,
        job_id: int,
        session: Mapping[str, object],
        text: str,
        persona: str,
    ) -> None:
        job = store.begin_turn_dispatch(self.store_path, job_id=job_id)
        try:
            started = await self.protocol.start_turn(
                thread_id=str(session["thread_id"]),
                client_message_id=str(job["client_message_id"]),
                text=text,
                model=str(session["model"]),
                reasoning_effort=session.get("reasoning_effort"),
            )
        except Exception:
            store.mark_dispatch_uncertain(self.store_path, job_id=job_id)
            return
        store.record_turn_started(self.store_path, job_id=job_id, turn_id=started.turn_id)
        await self._await_or_recover_terminal(job_id, started.turn_id, session, persona)

    async def _await_or_recover_terminal(
        self,
        job_id: int,
        turn_id: str,
        session: Mapping[str, object],
        persona: str,
    ) -> None:
        try:
            _event, usage = await self.event_inbox.wait_terminal(
                turn_id, timeout_seconds=self.turn_timeout_seconds
            )
        except TimeoutError:
            try:
                await self.protocol.interrupt(thread_id=str(session["thread_id"]), turn_id=turn_id)
            except Exception:
                pass
            store.mark_dispatch_uncertain(self.store_path, job_id=job_id)
            return
        finally:
            await self.event_inbox.discard(turn_id)
        await self._reconcile_and_maybe_deliver(job_id, session, persona, usage)

    async def _recover(self, job: Mapping[str, object]) -> None:
        job_id = int(job["id"])
        status = str(job["status"])
        if status == "thread_dispatching" and not job.get("thread_id"):
            store.abandon_thread_attempt_and_requeue(self.store_path, job_id=job_id)
            return
        session = store.get_session(self.store_path, str(job["api_session"]))
        if session is None or not session.get("thread_id"):
            return
        try:
            persona = self._load_persona(session)
        except CodexGenerationError as exc:
            store.mark_failed(self.store_path, job_id=job_id, category=exc.category)
            return
        async with self.activity_gate.generation():
            await self._reconcile_and_maybe_deliver(job_id, session, persona, None)

    async def _reconcile_and_maybe_deliver(
        self,
        job_id: int,
        session: Mapping[str, object],
        persona: str,
        usage: Mapping[str, int] | None,
    ) -> None:
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
            if job["status"] in {"turn_dispatching", "dispatch_uncertain"}:
                store.requeue_after_verified_turn_absent(self.store_path, job_id=job_id)
            return
        updated = store.record_reconciled_turn(
            self.store_path,
            job_id=job_id,
            turn_id=correlated.turn_id,
            status=correlated.status,
        )
        if updated["status"] == "failed":
            try:
                await self.protocol.unsubscribe(thread_id=str(session["thread_id"]))
            except Exception:
                pass
            return
        if updated["status"] == "in_progress":
            await self._await_or_recover_terminal(
                job_id, correlated.turn_id, session, persona
            )
            return
        if updated["status"] != "callback_pending":
            return
        if not correlated.final_answer:
            store.mark_failed(
                self.store_path, job_id=job_id, category="codex_generation_empty_response"
            )
            return
        try:
            assistant_message_id = await _maybe_await(
                self.completion_callback(updated, correlated.final_answer, usage)
            )
        except Exception:
            return
        if isinstance(assistant_message_id, bool) or not isinstance(assistant_message_id, int):
            return
        store.mark_completed(
            self.store_path,
            job_id=job_id,
            assistant_message_id=assistant_message_id,
        )
        try:
            await self.protocol.unsubscribe(thread_id=str(session["thread_id"]))
        except Exception:
            pass
