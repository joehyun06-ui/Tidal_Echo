"""Canary-scoped subscription reliability for reused Codex threads.

Codex App Server's ``thread/unsubscribe`` removes this connection's subscription
without immediately unloading the durable thread.  A later turn on the same thread
must therefore rejoin via ``thread/resume`` before ``turn/start`` so terminal
notifications are delivered in real time instead of waiting for timeout recovery.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from . import codex_generation_provider_binding as provider_binding
from . import codex_generation_store as store
from .codex_generation_live_reliability import (
    ReliableCodexGenerationRuntime,
    ReliableCodexGenerationWorker,
)
from .codex_generation_protocol import CodexGenerationError


class ResubscribingCodexGenerationWorker(ReliableCodexGenerationWorker):
    """Rejoin an already-bound durable thread before dispatching its next turn."""

    async def _process(self, job: Mapping[str, object]) -> None:
        job_id = int(job["id"])
        session = store.get_session(self.store_path, str(job["api_session"]))
        if session is None or session.get("status") != "active":
            store.mark_failed(self.store_path, job_id=job_id, category="session_unavailable")
            return

        # A freshly pinned session still uses the reviewed first-turn path.  Thread
        # start subscribes this connection, so an extra resume would be redundant.
        if not session.get("thread_id"):
            await super()._process(job)
            return

        try:
            persona = self._load_persona(session)
            text = await self._load_input(job)
        except CodexGenerationError as exc:
            store.mark_failed(self.store_path, job_id=job_id, category=exc.category)
            return

        async with self.activity_gate.generation():
            session = store.get_session(self.store_path, str(job["api_session"])) or session
            if not session.get("thread_id") or not session.get("cwd"):
                store.mark_failed(
                    self.store_path,
                    job_id=job_id,
                    category="codex_generation_session_unavailable",
                )
                return
            if session.get("model_provider") == provider_binding.UNRESOLVED_MODEL_PROVIDER:
                store.mark_failed(
                    self.store_path,
                    job_id=job_id,
                    category="codex_generation_provider_contract_changed",
                )
                return

            # The previous successful delivery unsubscribed this connection from the
            # durable thread.  Resume is the 0.147 contract for rejoining it and makes
            # subsequent turn notifications observable again.
            try:
                await self.protocol.resume_thread(
                    thread_id=str(session["thread_id"]),
                    model=str(session["model"]),
                    model_provider=str(session["model_provider"]),
                    reasoning_effort=session.get("reasoning_effort"),
                    cwd=Path(str(session["cwd"])),
                    persona=persona,
                )
            except CodexGenerationError as exc:
                store.mark_failed(self.store_path, job_id=job_id, category=exc.category)
                return
            except Exception:
                store.mark_failed(
                    self.store_path,
                    job_id=job_id,
                    category="codex_generation_rejoin_unavailable",
                )
                return

            await self._dispatch_turn(job_id, session, text, persona)


class ResubscribingCodexGenerationRuntime(ReliableCodexGenerationRuntime):
    """Live canary runtime that uses the subscription-aware generation worker."""

    def __init__(
        self,
        *,
        control_config,
        generation_config,
        relay_db,
        persona_loader,
        completion_callback,
    ) -> None:
        super().__init__(
            control_config=control_config,
            generation_config=generation_config,
            relay_db=relay_db,
            persona_loader=persona_loader,
            completion_callback=completion_callback,
        )
        self.worker = ResubscribingCodexGenerationWorker(
            store_path=generation_config.store_path,
            protocol=self.foundation.generation,
            activity_gate=self.foundation.activity_gate,
            persona_loader=persona_loader,
            canonical_message_loader=self._load_canonical_message,
            completion_callback=completion_callback,
            event_inbox=self.event_inbox,
        )
