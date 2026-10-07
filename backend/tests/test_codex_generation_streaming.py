from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend import codex_generation_store as store
from backend.codex_generation_progress import ReplyProgress, valid_snapshot
from backend.codex_generation_streaming import GenerationControls, GenerationControlError
from backend.codex_generation_streaming import StreamingCodexGenerationRuntime
from backend.codex_generation_live_reliability import enrich_generation_notification
from backend.tests import test_codex_generation_live_reliability as worker_fixture
from backend.tests import test_codex_web_completion as completion_fixture
from backend.tests import test_codex_generation_runtime as runtime_fixture
from backend.codex_web_completion import complete_codex_web_generation


class ReplyProgressTest(unittest.TestCase):
    def setUp(self):
        self.progress = ReplyProgress(max_turns=2)
        self.job = {"thread_id": "thr-1", "turn_id": "turn-1", "api_session": "a", "generation_id": "codex-gen-1", "canonical_message_id": 1, "created_at": "2026-10-07T00:00:00Z"}

    def emit(self, method, **kwargs):
        self.progress.receive(method, {"threadId": "thr-1", "turnId": "turn-1", **kwargs})

    def test_repeated_deltas_and_authoritative_item_completion(self):
        self.emit("item/started", item={"type": "agentMessage", "id": "a1", "text": "", "phase": "final_answer"})
        for _ in range(100):
            self.emit("item/agentMessage/delta", itemId="a1", delta="哈")
        self.assertEqual(self.progress.snapshot(self.job)["text"], "哈" * 100)
        self.emit("item/completed", item={"type": "agentMessage", "id": "a1", "text": "完整的答案", "phase": "final_answer"})
        self.emit("item/agentMessage/delta", itemId="a1", delta="迟到的文字")
        self.emit("item/started", item={"type": "agentMessage", "id": "a1", "text": "", "phase": "final_answer"})
        result = self.progress.snapshot(self.job)
        self.assertTrue(valid_snapshot(result))
        self.assertEqual(result["text"], "完整的答案")
        self.assertNotIn("thread_id", result)

    def test_no_reasoning_commentary_unknown_items_or_wrong_turn(self):
        self.emit("item/reasoning/textDelta", itemId="r1", delta="private reasoning")
        self.emit("item/started", item={"type": "agentMessage", "id": "c1", "text": "commentary", "phase": "commentary"})
        self.emit("item/agentMessage/delta", itemId="c1", delta="extra")
        self.emit("item/agentMessage/delta", itemId="unknown", delta="unknown")
        self.assertIsNone(self.progress.snapshot(self.job))
        self.emit("item/started", item={"type": "agentMessage", "id": "a1", "text": "visible"})
        self.progress.receive("item/agentMessage/delta", {"threadId": "wrong-thread", "turnId": "turn-1", "itemId": "a1", "delta": "bad"})
        self.assertEqual(self.progress.snapshot(self.job)["text"], "visible")

    def test_reply_and_turn_cache_are_bounded(self):
        self.emit("item/started", item={"type": "agentMessage", "id": "a1", "text": "x" * 64000})
        self.emit("item/agentMessage/delta", itemId="a1", delta="overflow")
        self.assertEqual(len(self.progress.snapshot(self.job)["text"]), 64000)
        for i in range(3):
            self.progress.receive("item/started", {"threadId": "thr-1", "turnId": f"next-{i}", "item": {"type": "agentMessage", "id": "a1", "text": "x"}})
        self.assertEqual(len(self.progress.turns), 2)
        self.assertIsNone(self.progress.snapshot(self.job))


class GenerationControlsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "generation.db"
        store.initialize(self.path)
        store.pin_session(self.path, api_session="a", model="fixture-model", model_provider="openai", reasoning_effort=None, persona_hash=hashlib.sha256(b"persona").hexdigest())
        self.job = store.enqueue_job(self.path, api_session="a", canonical_message_id=1, input_digest=hashlib.sha256(b"hello").hexdigest(), generation_id="codex-gen-1", client_message_id="codex-client-1", callback_identity="codex-callback-1")
        self.protocol = SimpleNamespace(interrupt=AsyncMock())
        self.controls = GenerationControls(self.path, self.protocol, ReplyProgress())

    def running(self):
        job = store.claim_next_job(self.path)
        cwd = str(Path(self.temp.name) / "workspace")
        store.begin_thread_dispatch(self.path, job_id=job["id"], thread_attempt_id="attempt-1", cwd=cwd)
        store.bind_session_thread(self.path, job_id=job["id"], thread_attempt_id="attempt-1", thread_id="thr-1", cwd=cwd)
        store.begin_turn_dispatch(self.path, job_id=job["id"])
        store.record_turn_started(self.path, job_id=job["id"], turn_id="turn-1")

    async def test_queued_stop_is_atomic_and_survives_reopen(self):
        result = await self.controls.stop("a", "codex-gen-1")
        self.assertEqual(result["generation"]["status"], "interrupted")
        self.assertTrue(result["generation"]["terminal"])
        self.assertIsNone(store.claim_next_job(self.path))
        self.protocol.interrupt.assert_not_awaited()
        store.initialize(self.path)
        self.assertEqual(GenerationControls(self.path, self.protocol, ReplyProgress()).status("a")["generation"]["status"], "interrupted")

    async def test_exact_turn_ack_is_not_terminal_and_duplicate_does_not_resend(self):
        self.running()
        result = await self.controls.stop("a", "codex-gen-1")
        self.assertEqual(result["generation"]["status"], "stopping")
        self.assertFalse(result["generation"]["terminal"])
        await self.controls.stop("a", "codex-gen-1")
        self.protocol.interrupt.assert_awaited_once_with(thread_id="thr-1", turn_id="turn-1")
        store.record_reconciled_turn(self.path, job_id=self.job["id"], turn_id="turn-1", status="interrupted")
        self.assertEqual(self.controls.status("a")["generation"]["status"], "interrupted")

    async def test_cross_session_stop_and_unbound_turn_fail_closed(self):
        with self.assertRaisesRegex(GenerationControlError, "not_found"):
            await self.controls.stop("b", "codex-gen-1")
        store.claim_next_job(self.path)
        with self.assertRaisesRegex(GenerationControlError, "not_interruptible"):
            await self.controls.stop("a", "codex-gen-1")
        self.protocol.interrupt.assert_not_awaited()

    async def test_unknown_interrupt_ack_never_reports_stopped_or_retries(self):
        self.running()
        self.protocol.interrupt.side_effect = TimeoutError()
        result = await self.controls.stop("a", "codex-gen-1")
        self.assertEqual(result["generation"]["status"], "stop_uncertain")
        self.assertFalse(result["generation"]["terminal"])
        await self.controls.stop("a", "codex-gen-1")
        self.assertEqual(self.protocol.interrupt.await_count, 1)

    async def test_completion_wins_stop_race_and_old_stop_never_targets_next_job(self):
        self.running()
        async def finish(**_):
            store.record_reconciled_turn(self.path, job_id=self.job["id"], turn_id="turn-1", status="completed")
            store.mark_completed(self.path, job_id=self.job["id"], assistant_message_id=2)
        self.protocol.interrupt.side_effect = finish
        result = await self.controls.stop("a", "codex-gen-1")
        self.assertEqual(result["generation"]["status"], "completed")
        store.enqueue_job(self.path, api_session="a", canonical_message_id=3, input_digest=hashlib.sha256(b"next").hexdigest(), generation_id="codex-gen-3", client_message_id="codex-client-3", callback_identity="codex-callback-3")
        await self.controls.stop("a", "codex-gen-1")
        self.assertEqual(self.protocol.interrupt.await_count, 1)
        self.assertEqual(self.controls.status("a")["generation"]["id"], "codex-gen-3")


class InterruptedReplyTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = worker_fixture.ReliableGenerationWorkerTest.asyncSetUp
    worker = worker_fixture.ReliableGenerationWorkerTest.worker

    async def test_interrupted_partial_uses_idempotent_delivery_and_retains_status(self):
        event = enrich_generation_notification("turn/completed", {"threadId": "thr-1", "turn": {
            "id": "turn-1", "status": "interrupted", "items": [{"id": "a1", "type": "agentMessage", "text": "已经写到这里", "phase": "final_answer"}],
        }})
        await self.inbox.on_event(event)
        await self.worker().run_once()
        self.assertEqual(self.callbacks[0][1], "已经写到这里")
        job = store.get_job(self.store_path, self.job["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["error_category"], "codex_turn_interrupted")
        self.assertEqual(GenerationControls(self.store_path, self.protocol, ReplyProgress()).status("api-canary")["generation"]["status"], "interrupted")

    async def test_interrupted_callback_retry_recovers_original_turn_without_new_generation(self):
        turn = {"id": "turn-1", "status": "interrupted", "items": [
            {"type": "userMessage", "id": "u1", "clientId": "codex-client-1", "content": []},
            {"type": "agentMessage", "id": "a1", "text": "partial", "phase": "final_answer"},
        ]}
        await self.inbox.on_event(enrich_generation_notification("turn/completed", {"threadId": "thr-1", "turn": turn}))
        worker = self.worker()
        callback = worker.completion_callback
        worker.completion_callback = AsyncMock(side_effect=OSError("fixture callback unavailable"))
        await worker.run_once()
        self.assertEqual(store.get_job(self.store_path, self.job["id"])["status"], "callback_pending")
        worker.completion_callback = callback
        self.protocol.resume_pages = [{"data": [turn]}]
        await worker.run_once()
        self.assertEqual(store.get_job(self.store_path, self.job["id"])["error_category"], "codex_turn_interrupted")
        self.assertEqual(self.callbacks[0][1], "partial")
        self.assertEqual(sum(call[0] == "turn/start" for call in self.protocol.calls), 1)


class StreamingRuntimeTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = GenerationControlsTest.asyncSetUp
    running = GenerationControlsTest.running

    async def test_many_progress_notifications_do_not_evict_terminal_or_block_reader(self):
        root = Path(self.temp.name)
        helper = runtime_fixture.CodexGenerationRuntimeTest()
        config = helper.config(root, True)
        delivered = asyncio.Event()
        snapshots = []
        async def publish(snapshot):
            snapshots.append(snapshot)
            delivered.set()
        runtime = StreamingCodexGenerationRuntime(
            control_config=helper.control(root), generation_config=config,
            relay_db=root / "relay.db", persona_loader=lambda: "persona",
            completion_callback=AsyncMock(return_value=2), progress_callback=publish,
        )
        runtime.worker.run_once = AsyncMock(return_value=False)
        self.path = config.store_path
        await runtime.start()
        try:
            store.pin_session(self.path, api_session="a", model="fixture-model", model_provider="openai", reasoning_effort=None, persona_hash=hashlib.sha256(b"persona").hexdigest())
            store.enqueue_job(self.path, api_session="a", canonical_message_id=1, input_digest=hashlib.sha256(b"hello").hexdigest(), generation_id="codex-gen-1", client_message_id="codex-client-1", callback_identity="codex-callback-1")
            self.running()
            dispatch = runtime.foundation.runtime._dispatch_notification
            context = {"threadId": "thr-1", "turnId": "turn-1"}
            await dispatch("item/started", {**context, "item": {"type": "agentMessage", "id": "a1", "text": "", "phase": "final_answer"}})
            for _ in range(200):
                await dispatch("item/agentMessage/delta", {**context, "itemId": "a1", "delta": "哈"})
            await dispatch("turn/completed", {"threadId": "thr-1", "turn": {"id": "turn-1", "status": "completed", "items": []}})
            terminal, _usage = await runtime.event_inbox.wait_terminal("turn-1", timeout_seconds=1)
            self.assertTrue(terminal.terminal)
            await asyncio.wait_for(delivered.wait(), timeout=1)
            self.assertEqual(snapshots[-1]["text"], "哈"*200)
            self.assertEqual(snapshots[-1]["generation_id"], "codex-gen-1")
            self.assertLess(len(snapshots), 3)
        finally:
            await runtime.close()


class InterruptedCompletionTest(unittest.TestCase):
    setUp = completion_fixture.CodexWebCompletionTest.setUp

    def test_stopped_partial_persists_once_with_stream_identity(self):
        args = dict(callback_identity="codex-callback-41", generation_id="codex-gen-41", client_message_id="codex-client-41", api_session="api-canary", reply_to=41, text="partial", ts="2026-10-07T00:00:00Z", finish_reason="interrupted")
        first = complete_codex_web_generation(self.path, **args)
        second = complete_codex_web_generation(self.path, **args)
        self.assertEqual(first["message"]["id"], second["message"]["id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["message"]["meta"]["stream_id"], "codex-gen-41")
        self.assertEqual(first["message"]["meta"]["finish_reason"], "interrupted")
