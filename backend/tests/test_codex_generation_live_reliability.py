from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend import codex_generation_store as store
from backend.codex_canary_loop_integration import CodexCanaryLoopIntegrationError
from backend.codex_generation_live_reliability import (
    FailClosedCodexCanaryLoopIntegration,
    RichGenerationNotification,
    ReliableCodexGenerationWorker,
    enrich_generation_notification,
)
from backend.codex_generation_protocol import (
    CodexProcessActivityGate,
    ThreadStartResult,
    TurnStartResult,
)
from backend.codex_generation_worker import CodexGenerationEventInbox


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeProtocol:
    def __init__(self, root: Path):
        self.config = SimpleNamespace(workspace_root=root)
        self.calls = []
        self.resume_pages = []

    async def start_thread(self, *, api_session, attempt_id, persona):
        self.calls.append(("thread/start", api_session, attempt_id, persona))
        return ThreadStartResult(
            thread_id="thr-1",
            model="gpt-5.6-sol",
            model_provider="openai",
            reasoning_effort="high",
            cwd=self.config.workspace_root / "sessions" / api_session / attempt_id,
        )

    async def start_turn(self, **kwargs):
        self.calls.append(("turn/start", kwargs))
        return TurnStartResult("turn-1", "inProgress")

    async def resume_thread(self, **kwargs):
        self.calls.append(("thread/resume", kwargs))
        if self.resume_pages:
            return self.resume_pages.pop(0)
        return {"data": []}

    async def interrupt(self, **kwargs):
        self.calls.append(("turn/interrupt", kwargs))

    async def unsubscribe(self, **kwargs):
        self.calls.append(("thread/unsubscribe", kwargs))


class ReliableGenerationWorkerTest(unittest.IsolatedAsyncioTestCase):
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

    def worker(self):
        async def callback(job, answer, usage):
            self.callbacks.append((dict(job), answer, usage))
            return 77

        return ReliableCodexGenerationWorker(
            store_path=self.store_path,
            protocol=self.protocol,
            activity_gate=CodexProcessActivityGate(),
            persona_loader=lambda: "persona",
            canonical_message_loader=lambda _job: "hello",
            completion_callback=callback,
            event_inbox=self.inbox,
            turn_timeout_seconds=1,
        )

    async def test_completed_notification_delivers_without_history_round_trip(self):
        await self.inbox.on_event(RichGenerationNotification(
            method="turn/completed",
            thread_id="thr-1",
            turn_id="turn-1",
            terminal=True,
            turn_status="completed",
            final_answer="answer",
            usage={"output_tokens": 2},
        ))
        await self.worker().run_once()
        job = store.get_job(self.store_path, self.job["id"])
        self.assertEqual((job["status"], job["assistant_message_id"]), ("completed", 77))
        self.assertEqual(self.callbacks[0][1], "answer")
        self.assertEqual(self.callbacks[0][2], {"output_tokens": 2})
        methods = [call[0] for call in self.protocol.calls]
        self.assertEqual(methods[:2], ["thread/start", "turn/start"])
        self.assertNotIn("thread/resume", methods)

    async def test_missing_notification_text_gets_bounded_projection_grace(self):
        await self.inbox.on_event(RichGenerationNotification(
            method="turn/completed",
            thread_id="thr-1",
            turn_id="turn-1",
            terminal=True,
            turn_status="completed",
            final_answer=None,
        ))
        no_answer = {
            "data": [{
                "id": "turn-1",
                "status": "completed",
                "items": [{
                    "type": "userMessage",
                    "id": "u1",
                    "clientId": "codex-client-1",
                    "content": [],
                }],
            }]
        }
        with_answer = {
            "data": [{
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {
                        "type": "userMessage",
                        "id": "u1",
                        "clientId": "codex-client-1",
                        "content": [],
                    },
                    {
                        "type": "agentMessage",
                        "id": "a1",
                        "phase": "finalAnswer",
                        "text": "late answer",
                    },
                ],
            }]
        }
        self.protocol.resume_pages = [no_answer, with_answer]
        await self.worker().run_once()
        job = store.get_job(self.store_path, self.job["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(self.callbacks[0][1], "late answer")
        self.assertEqual(
            [call[0] for call in self.protocol.calls].count("thread/resume"),
            2,
        )

    async def test_persistently_empty_completed_turn_fails_without_new_turn(self):
        await self.inbox.on_event(RichGenerationNotification(
            method="turn/completed",
            thread_id="thr-1",
            turn_id="turn-1",
            terminal=True,
            turn_status="completed",
            final_answer=None,
        ))
        page = {
            "data": [{
                "id": "turn-1",
                "status": "completed",
                "items": [{
                    "type": "userMessage",
                    "id": "u1",
                    "clientId": "codex-client-1",
                    "content": [],
                }],
            }]
        }
        self.protocol.resume_pages = [page, page, page]
        await self.worker().run_once()
        job = store.get_job(self.store_path, self.job["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_category"], "codex_generation_empty_response")
        self.assertEqual(self.callbacks, [])
        self.assertEqual(
            [call[0] for call in self.protocol.calls].count("turn/start"),
            1,
        )


class CompletionProjectionTest(unittest.TestCase):
    def test_completed_notification_preserves_final_answer(self):
        event = enrich_generation_notification("turn/completed", {
            "threadId": "thr-1",
            "turn": {
                "id": "turn-1",
                "status": "completed",
                "items": [
                    {"type": "userMessage", "id": "u1", "clientId": "c1", "content": []},
                    {"type": "agentMessage", "id": "a1", "phase": "finalAnswer", "text": "done"},
                ],
            },
        })
        self.assertIsInstance(event, RichGenerationNotification)
        self.assertEqual(event.turn_status, "completed")
        self.assertEqual(event.final_answer, "done")


class FailClosedIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def legacy(self):
        class Legacy:
            TRANSIENT_CONTINUITY_ENABLED = False
            RELAY_DB = "/tmp/relay.db"
            CODEX_CONTROL = object()
            _CODEX_CANARY_SESSION_LOCK_INSTALLED = False

            def __init__(self):
                self.create_session = lambda *a, **k: None
                self.patch_session = lambda *a, **k: None
                self.save_sessions = lambda *a, **k: None
                self.legacy_calls = []

            def active_session_id(self):
                return "api-normal"

            async def handle_ingest(self, text, before_id, session_id, **kwargs):
                self.legacy_calls.append((text, before_id, session_id, kwargs))
                return {
                    "ok": True,
                    "callback_delivered": True,
                    "generation_id": "api-gen",
                    "stream_id": "api-stream",
                    "api_session": session_id,
                }

        return Legacy()

    async def test_pinned_session_generation_freeze_does_not_fallback_to_api(self):
        legacy = self.legacy()
        runtime = SimpleNamespace(
            generation_enabled=False,
            controller=SimpleNamespace(is_pinned=lambda sid: sid == "api-canary"),
        )
        integration = FailClosedCodexCanaryLoopIntegration(legacy, runtime)
        with self.assertRaises(CodexCanaryLoopIntegrationError) as raised:
            await integration.handle_ingest({
                "id": 41,
                "text": "hello",
                "session_id": "api-canary",
            })
        self.assertEqual(raised.exception.category, "codex_generation_disabled")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(legacy.legacy_calls, [])

    async def test_unpinned_session_generation_freeze_keeps_legacy_api_path(self):
        legacy = self.legacy()
        runtime = SimpleNamespace(
            generation_enabled=False,
            controller=SimpleNamespace(is_pinned=lambda _sid: False),
        )
        integration = FailClosedCodexCanaryLoopIntegration(legacy, runtime)
        result = await integration.handle_ingest({
            "id": 41,
            "text": "hello",
            "session_id": "api-normal",
        })
        self.assertTrue(result["callback_delivered"])
        self.assertEqual(len(legacy.legacy_calls), 1)


if __name__ == "__main__":
    unittest.main()
