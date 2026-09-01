from __future__ import annotations

import asyncio
import io
import json
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi import FastAPI

from backend import (
    deployment_config,
    memory_candidate_decision_ledger,
    memory_candidate_decision_v2,
    memory_explicit_actions,
    memory_hierarchy_episode_refinement as episode_refinement,
    memory_hierarchy_episode_refinement_extractor as episode_extractor,
    memory_hierarchy_live_refresh_shadow as live,
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_refinement as topic_refinement,
    memory_hierarchy_refinement_extractor as topic_extractor,
    memory_hierarchy_refinement_loopback as loopback,
    memory_hierarchy_semantic_rebuild as semantic,
    memory_hierarchy_summary_runtime_shadow as b6e,
    memory_service,
)


TOKEN = "live-refresh-internal-token-abcdefghijklmnopqrstuvwxyz-012345"
MODEL = "live-refresh-model"
PROMPT = "kelivo-provider-prompt-v1"
MEMORY_KEY = "live_refresh_memory_key_000001"


def fake_relay(*, summary_enabled=True, memory_enabled=True):
    app = FastAPI()
    memory = SimpleNamespace(
        enabled=memory_enabled,
        configuration_valid=True,
        fingerprint_key_id="live-refresh-key",
        fingerprint_hmac_secret="Live-Refresh-HMAC-0123456789-AbCd!",
        max_item_chars=4096,
        sensitive_storage_enabled=False,
    )
    relay = SimpleNamespace(
        app=app,
        DEPLOYMENT=SimpleNamespace(
            memory=memory,
            persistent_root=Path("/tmp/live-refresh-root"),
            db_path=Path("/tmp/live-refresh-relay.db"),
            loop_config=Path("/tmp/live-refresh-loop.json"),
        ),
        LOOP_INGEST_URL="http://127.0.0.1:3020/loop/ingest",
        API_LOOP_INTERNAL_TOKEN=TOKEN,
    )
    setattr(relay, b6e.ENABLED_MARKER, summary_enabled)
    setattr(relay, b6e.TASK_MARKER, None)
    return relay


def topic_messages():
    payload = json.dumps(
        {
            "records": [
                {
                    "memory_key": MEMORY_KEY,
                    "broad_topic": "topic.project",
                    "kind": "project",
                    "first_observed_at": "2026-09-01T08:00:00+00:00",
                    "last_confirmed_at": "2026-09-01T08:00:00+00:00",
                    "content": "A project fact.",
                }
            ]
        },
        separators=(",", ":"),
    )
    return (
        {"role": "developer", "content": topic_extractor.EXTRACTOR_INSTRUCTION},
        {"role": "user", "content": payload},
    )


def topic_context():
    return {
        "prompt_contract_version": PROMPT,
        "memory_hierarchy_refinement_extractor": topic_extractor.EXTRACTOR_CONTRACT_VERSION,
        "memory_hierarchy_refinement_contract": topic_refinement.REFINEMENT_CONTRACT_VERSION,
        "memory_hierarchy_projection_contract": hierarchy.PROJECTION_CONTRACT_VERSION,
    }


def episode_messages():
    payload = json.dumps(
        {
            "records": [
                {
                    "memory_key": "live_refresh_decision_000001",
                    "topic_key": "topic.project",
                    "kind": "decision",
                    "first_observed_at": "2026-09-01T08:00:00+00:00",
                    "last_confirmed_at": "2026-09-01T08:00:00+00:00",
                    "content": "A decision fact.",
                },
                {
                    "memory_key": "live_refresh_progress_000002",
                    "topic_key": "topic.project",
                    "kind": "task_or_progress",
                    "first_observed_at": "2026-09-01T08:01:00+00:00",
                    "last_confirmed_at": "2026-09-01T08:01:00+00:00",
                    "content": "A progress fact.",
                },
            ]
        },
        separators=(",", ":"),
    )
    return (
        {"role": "developer", "content": episode_extractor.EXTRACTOR_INSTRUCTION},
        {"role": "user", "content": payload},
    )


def episode_context():
    return {
        "prompt_contract_version": PROMPT,
        "memory_hierarchy_episode_refinement_extractor": episode_extractor.EXTRACTOR_CONTRACT_VERSION,
        "memory_hierarchy_episode_refinement_contract": episode_refinement.EPISODE_REFINEMENT_CONTRACT_VERSION,
        "memory_hierarchy_projection_contract": hierarchy.PROJECTION_CONTRACT_VERSION,
    }


def semantic_receipt(digest: str = "a" * 64, *, provider_failed=False):
    mode = semantic.TOPIC_MODE_PROVIDER_FAILED if provider_failed else semantic.TOPIC_MODE_BASELINE
    return SimpleNamespace(
        atomic_snapshot_digest=digest,
        atomic_count=2,
        topic_count=1,
        episode_count=0,
        node_count=2,
        dirty_node_count=0,
        topic_mode=mode,
        topic_provider_call_count=1,
        episode_mode=semantic.EPISODE_MODE_NONE,
        episode_provider_call_count=0,
        provider_failed=provider_failed,
    )


def summary_receipt(*, failed=0):
    return SimpleNamespace(
        target_count=2,
        cache_hit_count=2 if not failed else 1,
        generated_count=0,
        failed_count=failed,
        pruned_count=0,
        provider_call_count=0 if not failed else 1,
    )


class MemoryHierarchyRefinementLoopbackTests(unittest.IsolatedAsyncioTestCase):
    def _body(self, messages, session_id, context, max_tokens):
        return {
            "messages": list(messages),
            "session_id": session_id,
            "provider_model": MODEL,
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "context": context,
        }

    def test_accepts_only_exact_topic_and_episode_extractor_envelopes(self):
        topic_body = self._body(
            topic_messages(),
            topic_extractor.EXTRACTOR_SESSION_ID,
            topic_context(),
            topic_extractor.EXTRACTOR_MAX_TOKENS,
        )
        accepted, context, contract = loopback._validate_dispatch_body(
            topic_body, MODEL, PROMPT
        )
        self.assertEqual(accepted, topic_messages())
        self.assertEqual(context, topic_context())
        self.assertEqual(contract["instruction"], topic_extractor.EXTRACTOR_INSTRUCTION)

        episode_body = self._body(
            episode_messages(),
            episode_extractor.EXTRACTOR_SESSION_ID,
            episode_context(),
            episode_extractor.EXTRACTOR_MAX_TOKENS,
        )
        accepted, context, contract = loopback._validate_dispatch_body(
            episode_body, MODEL, PROMPT
        )
        self.assertEqual(accepted, episode_messages())
        self.assertEqual(context, episode_context())
        self.assertEqual(contract["instruction"], episode_extractor.EXTRACTOR_INSTRUCTION)

        variants = []
        arbitrary = dict(topic_body)
        arbitrary["session_id"] = "generic-provider-proxy"
        variants.append(arbitrary)
        wrong_prompt = dict(topic_body)
        wrong_prompt["messages"] = [
            {"role": "developer", "content": "arbitrary prompt"},
            topic_messages()[1],
        ]
        variants.append(wrong_prompt)
        wrong_context = dict(topic_body)
        wrong_context["context"] = {**topic_context(), "prompt_contract_version": "other-v1"}
        variants.append(wrong_context)
        bad_payload = dict(topic_body)
        bad_payload["messages"] = [
            topic_messages()[0],
            {"role": "user", "content": json.dumps({"records": [{"content": "secret"}]})},
        ]
        variants.append(bad_payload)

        for candidate in variants:
            with self.subTest(candidate=candidate):
                with self.assertRaises(loopback.MemoryHierarchyRefinementLoopbackError) as raised:
                    loopback._validate_dispatch_body(candidate, MODEL, PROMPT)
                self.assertEqual(raised.exception.category, "loopback_invalid_request")

    async def test_client_is_localhost_only_and_token_bound(self):
        seen = {}

        async def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["token"] = request.headers.get("x-api-loop-internal-token")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "text": "{}"})

        result = await loopback.generate_via_loopback(
            topic_messages(),
            topic_extractor.EXTRACTOR_SESSION_ID,
            MODEL,
            0.0,
            topic_extractor.EXTRACTOR_MAX_TOKENS,
            topic_context(),
            ingest_url="http://127.0.0.1:3020/loop/ingest",
            internal_token=TOKEN,
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(
            seen["url"],
            "http://127.0.0.1:3020/loop/memory/hierarchy-refinement",
        )
        self.assertEqual(seen["token"], TOKEN)
        self.assertEqual(seen["body"]["session_id"], topic_extractor.EXTRACTOR_SESSION_ID)
        self.assertEqual(result, {"text": "{}"})

        with self.assertRaises(loopback.MemoryHierarchyRefinementLoopbackError) as raised:
            await loopback.generate_via_loopback(
                topic_messages(),
                topic_extractor.EXTRACTOR_SESSION_ID,
                MODEL,
                0.0,
                topic_extractor.EXTRACTOR_MAX_TOKENS,
                topic_context(),
                ingest_url="https://example.com/loop/ingest",
                internal_token=TOKEN,
                transport=httpx.MockTransport(handler),
            )
        self.assertEqual(raised.exception.category, "loopback_unavailable")


class MemoryHierarchyLiveRefreshShadowTests(unittest.IsolatedAsyncioTestCase):
    def test_gate_defaults_off_is_strict_and_requires_b6e_when_enabled(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(live.enabled_from_environment())
        for raw in ("", " true", "maybe", "TRUE "):
            with self.subTest(raw=raw), mock.patch.dict(
                os.environ, {live.ENV_GATE: raw}, clear=True
            ):
                with self.assertRaises(deployment_config.DeploymentConfigError):
                    live.enabled_from_environment()

        relay = fake_relay(summary_enabled=False)
        before = relay.app.router.lifespan_context
        with mock.patch.dict(os.environ, {live.ENV_GATE: "false"}, clear=True):
            self.assertFalse(live.install(relay))
        self.assertIs(relay.app.router.lifespan_context, before)

        relay = fake_relay(summary_enabled=False)
        with mock.patch.dict(os.environ, {live.ENV_GATE: "true"}, clear=True):
            with self.assertRaises(deployment_config.DeploymentConfigError) as raised:
                live.install(relay)
        self.assertEqual(
            raised.exception.category,
            "memory_hierarchy_live_refresh_shadow_requires_summary_shadow",
        )

    async def test_mutation_hooks_schedule_only_committed_nonreplayed_explicit_and_approve(self):
        event = asyncio.Event()
        pending = set()
        loop = asyncio.get_running_loop()
        explicit_results = [
            memory_explicit_actions.ExplicitMemoryActionResult(
                request_id="r1",
                action_kind="remember",
                status="completed",
                category="created",
                memory_key="m" * 32,
                kind="project",
                scope_type="global_user",
                sensitivity="normal",
                replayed=False,
            ),
            memory_explicit_actions.ExplicitMemoryActionResult(
                request_id="r2",
                action_kind="remember",
                status="completed",
                category="suppressed",
                memory_key=None,
                kind="project",
                scope_type="global_user",
                sensitivity="normal",
                replayed=False,
            ),
            memory_explicit_actions.ExplicitMemoryActionResult(
                request_id="r3",
                action_kind="remember",
                status="completed",
                category="idempotent_existing",
                memory_key="m" * 32,
                kind="project",
                scope_type="global_user",
                sensitivity="normal",
                replayed=True,
            ),
        ]
        approve_binding = memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1(
            request_id="a" * 32,
            candidate_key="c" * 32,
            origin="operator_cli",
            decision="approve",
        )
        reject_binding = memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1(
            request_id="b" * 32,
            candidate_key="d" * 32,
            origin="operator_cli",
            decision="reject",
        )
        decision_results = [
            memory_candidate_decision_ledger.CandidateDecisionResultV1(
                approve_binding, replayed=False
            ),
            memory_candidate_decision_ledger.CandidateDecisionResultV1(
                reject_binding, replayed=False
            ),
            memory_candidate_decision_ledger.CandidateDecisionResultV1(
                approve_binding, replayed=True
            ),
        ]

        def fake_explicit(_self, *args, **kwargs):
            return explicit_results.pop(0)

        def fake_v2(_self, *, binding):
            return decision_results.pop(0)

        def fake_v1(_self, *, binding):
            return memory_candidate_decision_ledger.CandidateDecisionResultV1(
                binding, replayed=False
            )

        with mock.patch.object(
            memory_explicit_actions.MemoryActionEntryBackend, "_run", fake_explicit
        ), mock.patch.object(
            memory_candidate_decision_v2.CandidateDecisionWriterV2, "decide", fake_v2
        ), mock.patch.object(
            memory_service.CandidateDecisionWriter, "decide", fake_v1
        ):
            originals = live._install_mutation_hooks(loop, event, pending)
            try:
                memory_explicit_actions.MemoryActionEntryBackend._run(object())
                await asyncio.sleep(0)
                self.assertTrue(event.is_set())
                self.assertEqual(pending, {live._TRIGGER_EXPLICIT})
                event.clear()
                pending.clear()

                memory_explicit_actions.MemoryActionEntryBackend._run(object())
                memory_explicit_actions.MemoryActionEntryBackend._run(object())
                await asyncio.sleep(0)
                self.assertFalse(event.is_set())
                self.assertEqual(pending, set())

                memory_candidate_decision_v2.CandidateDecisionWriterV2.decide(
                    object(), binding=approve_binding
                )
                await asyncio.sleep(0)
                self.assertTrue(event.is_set())
                self.assertEqual(pending, {live._TRIGGER_APPROVE})
                event.clear()
                pending.clear()

                memory_candidate_decision_v2.CandidateDecisionWriterV2.decide(
                    object(), binding=reject_binding
                )
                memory_candidate_decision_v2.CandidateDecisionWriterV2.decide(
                    object(), binding=approve_binding
                )
                await asyncio.sleep(0)
                self.assertFalse(event.is_set())
                self.assertEqual(pending, set())
            finally:
                live._restore_mutation_hooks(originals)

    async def test_worker_waits_b6e_skips_same_digest_and_coalesces_bursts(self):
        relay = fake_relay()
        release_b6e = asyncio.Event()

        async def b6e_blocker():
            await release_b6e.wait()

        b6e_task = asyncio.create_task(b6e_blocker())
        setattr(relay, b6e.TASK_MARKER, b6e_task)
        event = asyncio.Event()
        pending = {live._TRIGGER_STARTUP}
        event.set()
        current = {"digest": "1" * 64}
        passes = []
        first_pass = asyncio.Event()
        second_pass = asyncio.Event()
        unchanged = asyncio.Event()
        completed_trigger_counts = []

        def digest(_relay):
            return current["digest"]

        async def refresh(_relay):
            passes.append(current["digest"])
            if len(passes) == 1:
                first_pass.set()
            else:
                second_pass.set()
            return semantic_receipt(current["digest"]), summary_receipt()

        def log_completed(_semantic, _summaries, *, trigger_count):
            completed_trigger_counts.append(trigger_count)

        def log_unchanged(*, trigger_count):
            completed_trigger_counts.append(-trigger_count)
            unchanged.set()

        with mock.patch.object(live, "_snapshot_digest", digest), \
             mock.patch.object(live, "_run_refresh_pass", refresh), \
             mock.patch.object(live, "_log_completed", log_completed), \
             mock.patch.object(live, "_log_unchanged", log_unchanged):
            worker = asyncio.create_task(live._worker(relay, event, pending))
            try:
                await asyncio.sleep(0)
                self.assertEqual(passes, [])
                release_b6e.set()
                await asyncio.wait_for(first_pass.wait(), timeout=1)
                self.assertEqual(passes, ["1" * 64])

                pending.add(live._TRIGGER_EXPLICIT)
                event.set()
                await asyncio.wait_for(unchanged.wait(), timeout=1)
                self.assertEqual(passes, ["1" * 64])

                current["digest"] = "2" * 64
                pending.update({live._TRIGGER_EXPLICIT, live._TRIGGER_APPROVE})
                event.set()
                await asyncio.wait_for(second_pass.wait(), timeout=1)
                self.assertEqual(passes, ["1" * 64, "2" * 64])
                self.assertEqual(completed_trigger_counts[0], 1)
                self.assertEqual(completed_trigger_counts[1], -1)
                self.assertEqual(completed_trigger_counts[2], 2)
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker
                if not b6e_task.done():
                    release_b6e.set()
                    await b6e_task

    async def test_incomplete_pass_does_not_mark_digest_processed(self):
        relay = fake_relay()
        event = asyncio.Event()
        pending = {live._TRIGGER_STARTUP}
        event.set()
        calls = []
        first = asyncio.Event()
        second = asyncio.Event()

        async def refresh(_relay):
            calls.append(1)
            if len(calls) == 1:
                first.set()
                return semantic_receipt(provider_failed=True), summary_receipt()
            second.set()
            return semantic_receipt(), summary_receipt()

        with mock.patch.object(live, "_await_b6e_startup", mock.AsyncMock()), \
             mock.patch.object(live, "_snapshot_digest", return_value="a" * 64), \
             mock.patch.object(live, "_run_refresh_pass", refresh), \
             mock.patch.object(live, "_log_completed"), \
             mock.patch.object(live, "_log_unchanged") as unchanged:
            worker = asyncio.create_task(live._worker(relay, event, pending))
            try:
                await asyncio.wait_for(first.wait(), timeout=1)
                pending.add(live._TRIGGER_EXPLICIT)
                event.set()
                await asyncio.wait_for(second.wait(), timeout=1)
                self.assertEqual(len(calls), 2)
                unchanged.assert_not_called()
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

    def test_logs_are_structural_and_routes_remain_internal_only(self):
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            live._log_completed(
                semantic_receipt(),
                summary_receipt(),
                trigger_count=2,
            )
        rendered = buffer.getvalue()
        self.assertIn("status=completed", rendered)
        self.assertIn("topic_mode=baseline", rendered)
        self.assertIn("summary_provider_calls=0", rendered)
        for forbidden in (
            "PRIVATE-SENSITIVE-DECISION",
            MEMORY_KEY,
            "topic.project",
            "candidate_key",
        ):
            self.assertNotIn(forbidden, rendered)

        root = Path(__file__).resolve().parents[2]
        loop_source = (root / "examples" / "api_loop_codex_canary.py").read_text(encoding="utf-8")
        relay_source = (root / "backend" / "p3_relay_app.py").read_text(encoding="utf-8")
        self.assertIn("memory_hierarchy_refinement_loopback.ENDPOINT", loop_source)
        self.assertIn("loop_memory_hierarchy_refinement", loop_source)
        self.assertNotIn("/app/memory/hierarchy-refinement", relay_source)
        self.assertNotIn("/memory/hierarchy-refinement", relay_source)


if __name__ == "__main__":
    unittest.main()
