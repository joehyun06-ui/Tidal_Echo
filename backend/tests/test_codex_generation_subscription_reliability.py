from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend import codex_generation_store as store
from backend.codex_generation_live_reliability import RichGenerationNotification
from backend.codex_generation_protocol import (
    CodexGenerationError,
    CodexProcessActivityGate,
    ThreadStartResult,
    TurnStartResult,
)
from backend.codex_generation_subscription_reliability import (
    ResubscribingCodexGenerationWorker,
)
from backend.codex_generation_worker import CodexGenerationEventInbox


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeProtocol:
    def __init__(self, root: Path):
        self.config = SimpleNamespace(workspace_root=root)
        self.calls = []
        self.resume_error = None

    async def start_thread(self, *, api_session, attempt_id, persona):
        self.calls.append(("thread/start", api_session, attempt_id, persona))
        return ThreadStartResult(
            thread_id="thr-1",
            model="gpt-5.6-sol",
            model_provider="openai",
            reasoning_effort="high",
            cwd=self.config.workspace_root / "sessions" / api_session / attempt_id,
        )

    async def resume_thread(self, **kwargs):
        self.calls.append(("thread/resume", kwargs))
        if self.resume_error is not None:
            raise self.resume_error
        return {"data": []}

    async def start_turn(self, **kwargs):
        self.calls.append(("turn/start", kwargs))
        return TurnStartResult("turn-2", "inProgress")

    async def interrupt(self, **kwargs):
        self.calls.append(("turn/interrupt", kwargs))

    async def unsubscribe(self, **kwargs):
        self.calls.append(("thread/unsubscribe", kwargs))


class SubscriptionReliabilityTest(unittest.IsolatedAsyncioTestCase):
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
        self.protocol = FakeProtocol(self.workspace)
        self.inbox = CodexGenerationEventInbox()
        self.callbacks = []

    def worker(self):
        async def callback(job, answer, usage):
            self.callbacks.append((dict(job), answer, usage))
            return 88

        return ResubscribingCodexGenerationWorker(
            store_path=self.store_path,
            protocol=self.protocol,
            activity_gate=CodexProcessActivityGate(),
            persona_loader=lambda: "persona",
            canonical_message_loader=lambda _job: "second message",
            completion_callback=callback,
            event_inbox=self.inbox,
            turn_timeout_seconds=1,
        )

    def bind_completed_first_turn(self) -> None:
        seed = store.enqueue_job(
            self.store_path,
            api_session="api-canary",
            canonical_message_id=1,
            input_digest=sha("first message"),
            generation_id="codex-gen-1",
            client_message_id="codex-client-1",
            callback_identity="codex-callback-1",
        )
        claimed = store.claim_next_job(self.store_path)
        self.assertEqual(claimed["id"], seed["id"])
        cwd = self.workspace / "sessions" / "api-canary" / "attempt-1"
        store.begin_thread_dispatch(
            self.store_path,
            job_id=seed["id"],
            thread_attempt_id="attempt-1",
            cwd=str(cwd),
        )
        store.bind_session_thread(
            self.store_path,
            job_id=seed["id"],
            thread_attempt_id="attempt-1",
            thread_id="thr-1",
            cwd=str(cwd),
        )
        store.begin_turn_dispatch(self.store_path, job_id=seed["id"])
        store.record_turn_started(self.store_path, job_id=seed["id"], turn_id="turn-1")
        store.record_reconciled_turn(
            self.store_path,
            job_id=seed["id"],
            turn_id="turn-1",
            status="completed",
        )
        store.mark_completed(
            self.store_path,
            job_id=seed["id"],
            assistant_message_id=77,
        )

    async def test_reused_thread_resumes_before_turn_and_completes_without_recovery(self):
        self.bind_completed_first_turn()
        second = store.enqueue_job(
            self.store_path,
            api_session="api-canary",
            canonical_message_id=2,
            input_digest=sha("second message"),
            generation_id="codex-gen-2",
            client_message_id="codex-client-2",
            callback_identity="codex-callback-2",
        )
        await self.inbox.on_event(RichGenerationNotification(
            method="turn/completed",
            thread_id="thr-1",
            turn_id="turn-2",
            terminal=True,
            turn_status="completed",
            final_answer="second answer",
        ))

        self.assertTrue(await self.worker().run_once())

        job = store.get_job(self.store_path, second["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["attempt_count"], 1)
        self.assertEqual(job["recovery_count"], 0)
        self.assertEqual(job["assistant_message_id"], 88)
        self.assertEqual(self.callbacks[0][1], "second answer")
        methods = [call[0] for call in self.protocol.calls]
        self.assertEqual(methods[:2], ["thread/resume", "turn/start"])
        self.assertNotIn("thread/start", methods)
        self.assertIn("thread/unsubscribe", methods)

    async def test_rejoin_failure_happens_before_turn_start(self):
        self.bind_completed_first_turn()
        second = store.enqueue_job(
            self.store_path,
            api_session="api-canary",
            canonical_message_id=2,
            input_digest=sha("second message"),
            generation_id="codex-gen-2",
            client_message_id="codex-client-2",
            callback_identity="codex-callback-2",
        )
        self.protocol.resume_error = CodexGenerationError("codex_generation_unavailable")

        self.assertTrue(await self.worker().run_once())

        job = store.get_job(self.store_path, second["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_category"], "codex_generation_unavailable")
        self.assertEqual(
            [call[0] for call in self.protocol.calls],
            ["thread/resume"],
        )


if __name__ == "__main__":
    unittest.main()
