from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend import codex_generation_store as store
from backend.codex_generation_protocol import (
    CodexGenerationError,
    CodexProcessActivityGate,
    GenerationNotification,
    ThreadStartResult,
    TurnStartResult,
)
from backend.codex_generation_worker import CodexGenerationEventInbox, CodexGenerationWorker


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class FakeProtocol:
    def __init__(self, root: Path):
        self.config = SimpleNamespace(workspace_root=root)
        self.calls = []
        self.start_turn_error = None
        self.start_thread_error = None
        self.resume_page = {
            "data": [{
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {"type": "userMessage", "id": "u1", "clientId": "codex-client-1", "content": []},
                    {"type": "agentMessage", "id": "a1", "text": "answer", "phase": "finalAnswer"},
                ],
            }]
        }

    async def start_thread(self, *, api_session, attempt_id, persona):
        self.calls.append(("thread/start", api_session, attempt_id, persona))
        if self.start_thread_error:
            raise self.start_thread_error
        cwd = self.config.workspace_root / "sessions" / api_session / attempt_id
        return ThreadStartResult(
            thread_id="thr-1",
            model="gpt-5.6-sol",
            model_provider="openai",
            reasoning_effort="high",
            cwd=cwd,
        )

    async def start_turn(self, **kwargs):
        self.calls.append(("turn/start", kwargs))
        if self.start_turn_error:
            raise self.start_turn_error
        return TurnStartResult("turn-1", "inProgress")

    async def resume_thread(self, **kwargs):
        self.calls.append(("thread/resume", kwargs))
        return self.resume_page

    async def interrupt(self, **kwargs):
        self.calls.append(("turn/interrupt", kwargs))

    async def unsubscribe(self, **kwargs):
        self.calls.append(("thread/unsubscribe", kwargs))


class CodexGenerationWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store_path = self.root / "codex-generation.db"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        store.initialize(self.store_path)
        store.pin_session(
            self.store_path,
            api_session="api-canary",
            model="gpt-5.6-sol",
            model_provider="openai",
            reasoning_effort="high",
            persona_hash=sha("persona"),
        )
        self.job = store.enqueue_job(
            self.store_path,
            api_session="api-canary",
            canonical_message_id=1,
            input_digest=sha("hello"),
            generation_id="codex-gen-1",
            client_message_id="codex-client-1",
            callback_identity="codex-callback-1",
        )
        self.protocol = FakeProtocol(self.workspace)
        self.inbox = CodexGenerationEventInbox()
        self.callbacks = []

    def worker(self, *, persona="persona", text="hello", callback_error=None):
        async def callback(job, answer, usage):
            self.callbacks.append((dict(job), answer, usage))
            if callback_error:
                raise callback_error
            return 77

        return CodexGenerationWorker(
            store_path=self.store_path,
            protocol=self.protocol,
            activity_gate=CodexProcessActivityGate(),
            persona_loader=lambda: persona,
            canonical_message_loader=lambda _job: text,
            completion_callback=callback,
            event_inbox=self.inbox,
            turn_timeout_seconds=1,
        )

    async def seed_completed_event(self):
        await self.inbox.on_event(GenerationNotification(
            method="thread/tokenUsage/updated",
            thread_id="thr-1",
            turn_id="turn-1",
            terminal=False,
            usage={"input_tokens": 4, "output_tokens": 2},
        ))
        await self.inbox.on_event(GenerationNotification(
            method="turn/completed",
            thread_id="thr-1",
            turn_id="turn-1",
            terminal=True,
        ))

    async def test_new_job_creates_thread_dispatches_turn_reconciles_and_completes(self):
        await self.seed_completed_event()
        worker = self.worker()
        self.assertTrue(await worker.run_once())
        job = store.get_job(self.store_path, self.job["id"])
        session = store.get_session(self.store_path, "api-canary")
        self.assertEqual((job["status"], job["assistant_message_id"]), ("completed", 77))
        self.assertEqual(session["thread_id"], "thr-1")
        self.assertEqual(self.callbacks[0][1], "answer")
        self.assertEqual(self.callbacks[0][2], {"input_tokens": 4, "output_tokens": 2})
        methods = [call[0] for call in self.protocol.calls]
        self.assertEqual(methods[:3], ["thread/start", "turn/start", "thread/resume"])
        self.assertIn("thread/unsubscribe", methods)

    async def test_turn_start_failure_is_uncertain_and_never_calls_completion(self):
        self.protocol.start_turn_error = RuntimeError("ambiguous transport failure")
        worker = self.worker()
        await worker.run_once()
        job = store.get_job(self.store_path, self.job["id"])
        self.assertEqual(job["status"], "dispatch_uncertain")
        self.assertEqual(self.callbacks, [])
        self.assertIsNone(store.claim_next_job(self.store_path))

    async def test_unresolved_recovery_fails_closed_without_requeue_or_callback(self):
        worker = self.worker()
        claimed = store.claim_next_job(self.store_path)
        cwd = str(self.workspace / "sessions" / "api-canary" / "attempt-1")
        store.begin_thread_dispatch(
            self.store_path,
            job_id=claimed["id"],
            thread_attempt_id="attempt-1",
            cwd=cwd,
        )
        store.bind_session_thread(
            self.store_path,
            job_id=claimed["id"],
            thread_attempt_id="attempt-1",
            thread_id="thr-1",
            cwd=cwd,
        )
        store.begin_turn_dispatch(self.store_path, job_id=claimed["id"])
        store.mark_dispatch_uncertain(self.store_path, job_id=claimed["id"])
        self.protocol.resume_page = {"data": []}
        await worker.run_once()
        job = store.get_job(self.store_path, claimed["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_category"], "codex_generation_reconcile_unresolved")
        self.assertEqual(self.callbacks, [])
        self.assertIsNone(store.claim_next_job(self.store_path))

    async def test_callback_failure_leaves_callback_pending_for_idempotent_retry(self):
        await self.seed_completed_event()
        worker = self.worker(callback_error=RuntimeError("ack lost"))
        await worker.run_once()
        job = store.get_job(self.store_path, self.job["id"])
        self.assertEqual(job["status"], "callback_pending")
        self.assertIsNone(job["assistant_message_id"])

    async def test_persona_contract_drift_fails_before_codex_rpc(self):
        worker = self.worker(persona="changed persona")
        await worker.run_once()
        job = store.get_job(self.store_path, self.job["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_category"], "codex_generation_persona_contract_changed")
        self.assertEqual(self.protocol.calls, [])

    async def test_canonical_input_drift_fails_before_codex_rpc(self):
        worker = self.worker(text="mutated")
        await worker.run_once()
        job = store.get_job(self.store_path, self.job["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_category"], "codex_generation_input_contract_changed")
        self.assertEqual(self.protocol.calls, [])

    async def test_transient_thread_start_failure_abandons_empty_attempt_and_requeues(self):
        self.protocol.start_thread_error = CodexGenerationError("codex_generation_unavailable")
        worker = self.worker()
        await worker.run_once()
        job = store.get_job(self.store_path, self.job["id"])
        self.assertEqual(job["status"], "queued")
        self.assertIsNone(job["thread_id"])
        self.assertIsNone(store.get_session(self.store_path, "api-canary")["thread_id"])


if __name__ == "__main__":
    unittest.main()
