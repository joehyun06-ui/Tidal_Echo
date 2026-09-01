from __future__ import annotations

import asyncio
import io
import json
import os
import unittest
from contextlib import asynccontextmanager, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi import FastAPI

from backend import (
    deployment_config,
    memory_hierarchy_summary_extractor_v2 as extractor,
    memory_hierarchy_summary_loopback_v2 as loopback,
    memory_hierarchy_summary_runtime_shadow as runtime,
    memory_hierarchy_summary as summary,
)


TOKEN = "internal-token-abcdefghijklmnopqrstuvwxyz-0123456789"
MODEL = "test-model"


def valid_messages(node_type: str = "topic"):
    payload = json.dumps(
        {
            "target_type": node_type,
            "records": [
                {
                    "memory_key": "runtime_shadow_memory_000001",
                    "kind": "project",
                    "first_observed_at": "2026-09-01T08:00:00+00:00",
                    "last_confirmed_at": "2026-09-01T08:00:00+00:00",
                    "content": "The backend runs on Render.",
                }
            ],
            "episode_groups": [],
        },
        separators=(",", ":"),
    )
    return (
        {"role": "developer", "content": extractor.EXTRACTOR_INSTRUCTION},
        {"role": "user", "content": payload},
    )


def valid_context(node_type: str = "topic"):
    return {
        "prompt_contract_version": "kelivo-provider-prompt-v1",
        "memory_hierarchy_summary_extractor": extractor.EXTRACTOR_CONTRACT_VERSION,
        "memory_hierarchy_summary_contract": summary.SUMMARY_CONTRACT_VERSION_V2,
        "summary_target_type": node_type,
    }


def fake_relay(*, memory_enabled=True):
    app = FastAPI()
    memory = SimpleNamespace(
        enabled=memory_enabled,
        configuration_valid=True,
        fingerprint_key_id="runtime-key",
        fingerprint_hmac_secret="Runtime-Shadow-HMAC-0123456789-AbCd!",
        max_item_chars=4096,
        sensitive_storage_enabled=False,
    )
    deployment = SimpleNamespace(
        memory=memory,
        persistent_root=Path("/tmp/runtime-shadow-root"),
        db_path=Path("/tmp/runtime-shadow-relay.db"),
        loop_config=Path("/tmp/runtime-shadow-loop.json"),
    )
    return SimpleNamespace(
        app=app,
        DEPLOYMENT=deployment,
        LOOP_INGEST_URL="http://127.0.0.1:3020/loop/ingest",
        API_LOOP_INTERNAL_TOKEN=TOKEN,
        loop_json=lambda _path: {},
    )


class MemoryHierarchySummaryLoopbackV2Tests(unittest.IsolatedAsyncioTestCase):
    def test_dispatch_body_rejects_arbitrary_prompt_session_context_and_payload(self):
        messages = list(valid_messages())
        body = {
            "messages": messages,
            "session_id": extractor.EXTRACTOR_SESSION_ID,
            "provider_model": MODEL,
            "temperature": 0.0,
            "max_tokens": extractor.EXTRACTOR_MAX_TOKENS,
            "context": valid_context(),
        }
        accepted, context = loopback._validate_dispatch_body(
            body,
            MODEL,
            "kelivo-provider-prompt-v1",
        )
        self.assertEqual(accepted, tuple(messages))
        self.assertEqual(context, body["context"])

        variants = []
        wrong_prompt = dict(body)
        wrong_prompt["messages"] = [
            {"role": "developer", "content": "generic proxy prompt"},
            messages[1],
        ]
        variants.append(wrong_prompt)
        wrong_session = dict(body)
        wrong_session["session_id"] = "other-session"
        variants.append(wrong_session)
        wrong_context = dict(body)
        wrong_context["context"] = {**body["context"], "prompt_contract_version": "other-v1"}
        variants.append(wrong_context)
        wrong_payload = dict(body)
        wrong_payload["messages"] = [
            messages[0],
            {"role": "user", "content": json.dumps({"prompt": "arbitrary"})},
        ]
        variants.append(wrong_payload)

        for candidate in variants:
            with self.subTest(candidate=candidate):
                with self.assertRaises(loopback.MemoryHierarchySummaryLoopbackV2Error) as raised:
                    loopback._validate_dispatch_body(
                        candidate,
                        MODEL,
                        "kelivo-provider-prompt-v1",
                    )
                self.assertEqual(raised.exception.category, "loopback_invalid_request")

    async def test_client_is_localhost_only_token_bound_and_returns_raw_text_only(self):
        seen = {}

        async def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["token"] = request.headers.get("x-api-loop-internal-token")
            payload = json.loads(request.content)
            seen["payload"] = payload
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "text": '{"version":"memory-hierarchy-summary-extractor-v2","clauses":[]}',
                },
            )

        result = await loopback.generate_v2_via_loopback(
            valid_messages(),
            extractor.EXTRACTOR_SESSION_ID,
            MODEL,
            0.0,
            extractor.EXTRACTOR_MAX_TOKENS,
            valid_context(),
            ingest_url="http://127.0.0.1:3020/loop/ingest",
            internal_token=TOKEN,
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(
            seen["url"],
            "http://127.0.0.1:3020/loop/memory/hierarchy-summary-v2",
        )
        self.assertEqual(seen["token"], TOKEN)
        self.assertEqual(seen["payload"]["session_id"], extractor.EXTRACTOR_SESSION_ID)
        self.assertEqual(set(result), {"text"})

        with self.assertRaises(loopback.MemoryHierarchySummaryLoopbackV2Error) as raised:
            await loopback.generate_v2_via_loopback(
                valid_messages(),
                extractor.EXTRACTOR_SESSION_ID,
                MODEL,
                0.0,
                extractor.EXTRACTOR_MAX_TOKENS,
                valid_context(),
                ingest_url="https://example.com/loop/ingest",
                internal_token=TOKEN,
                transport=httpx.MockTransport(handler),
            )
        self.assertEqual(raised.exception.category, "loopback_unavailable")


class MemoryHierarchySummaryRuntimeShadowTests(unittest.IsolatedAsyncioTestCase):
    def test_gate_defaults_off_and_is_strict(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(runtime.enabled_from_environment())
        for raw in ("", " true", "maybe", "TRUE "):
            with self.subTest(raw=raw), mock.patch.dict(
                os.environ,
                {runtime.ENV_GATE: raw},
                clear=True,
            ):
                with self.assertRaises(deployment_config.DeploymentConfigError):
                    runtime.enabled_from_environment()

    def test_gate_off_does_not_replace_lifespan(self):
        relay = fake_relay()
        before = relay.app.router.lifespan_context
        with mock.patch.dict(os.environ, {runtime.ENV_GATE: "false"}, clear=True):
            self.assertFalse(runtime.install(relay))
        self.assertIs(relay.app.router.lifespan_context, before)
        self.assertTrue(getattr(relay, runtime.INSTALL_MARKER))
        self.assertFalse(getattr(relay, runtime.ENABLED_MARKER))

    def test_gate_relationships_fail_closed_before_runtime_task(self):
        no_memory = fake_relay(memory_enabled=False)
        with mock.patch.dict(
            os.environ,
            {
                runtime.ENV_GATE: "true",
                "CODEX_CANARY_ENTRYPOINTS_ENABLED": "true",
            },
            clear=True,
        ):
            with self.assertRaises(deployment_config.DeploymentConfigError) as raised:
                runtime.install(no_memory)
            self.assertEqual(
                raised.exception.category,
                "memory_hierarchy_summary_shadow_requires_memory",
            )

        no_codex = fake_relay(memory_enabled=True)
        with mock.patch.dict(
            os.environ,
            {
                runtime.ENV_GATE: "true",
                "CODEX_CANARY_ENTRYPOINTS_ENABLED": "false",
            },
            clear=True,
        ):
            with self.assertRaises(deployment_config.DeploymentConfigError) as raised:
                runtime.install(no_codex)
            self.assertEqual(
                raised.exception.category,
                "memory_hierarchy_summary_shadow_requires_codex_entrypoints",
            )

    async def test_gate_on_schedules_exactly_one_lifespan_task_and_cleans_it(self):
        relay = fake_relay()
        calls = []
        started = asyncio.Event()

        async def fake_run_once(_relay):
            calls.append(1)
            started.set()

        with mock.patch.dict(
            os.environ,
            {
                runtime.ENV_GATE: "true",
                "CODEX_CANARY_ENTRYPOINTS_ENABLED": "true",
            },
            clear=True,
        ), mock.patch.object(runtime, "run_once", fake_run_once):
            self.assertTrue(runtime.install(relay))
            async with relay.app.router.lifespan_context(relay.app):
                await asyncio.wait_for(started.wait(), timeout=1)
                await asyncio.sleep(0)
                self.assertEqual(calls, [1])
                task = getattr(relay, runtime.TASK_MARKER)
                self.assertIsNotNone(task)
            self.assertIsNone(getattr(relay, runtime.TASK_MARKER))
            self.assertEqual(calls, [1])

    async def test_run_once_emits_only_bounded_counts_and_failure_is_nonfatal(self):
        relay = fake_relay()
        hierarchy_receipt = SimpleNamespace(
            atomic_count=3,
            topic_count=1,
            node_count=2,
            dirty_node_count=2,
        )
        summary_receipt = SimpleNamespace(
            target_count=2,
            cache_hit_count=0,
            generated_count=2,
            failed_count=0,
            pruned_count=0,
            provider_call_count=2,
        )
        buffer = io.StringIO()
        with mock.patch.object(runtime, "_wait_for_loop", mock.AsyncMock()), \
             mock.patch.object(runtime, "_reader", return_value=object()), \
             mock.patch.object(
                 runtime.memory_hierarchy_rebuild,
                 "rebuild_baseline_hierarchy_v1",
                 return_value=hierarchy_receipt,
             ), \
             mock.patch.object(
                 runtime.memory_hierarchy_summary_rebuild_v2,
                 "rebuild_current_hierarchy_summaries_v2",
                 mock.AsyncMock(return_value=summary_receipt),
             ), \
             mock.patch.object(
                 runtime.deployment_config,
                 "resolve_kelivo_provider_contract_defaults",
                 return_value=SimpleNamespace(provider_model=MODEL),
             ), redirect_stderr(buffer):
            await runtime.run_once(relay)
        rendered = buffer.getvalue()
        self.assertIn("[memory-hierarchy-summary-shadow] status=completed", rendered)
        self.assertIn("atomics=3", rendered)
        self.assertIn("provider_calls=2", rendered)
        for forbidden in (
            "The backend runs on Render",
            "runtime_shadow_memory_000001",
            "topic.project",
        ):
            self.assertNotIn(forbidden, rendered)

        buffer = io.StringIO()
        with mock.patch.object(
            runtime,
            "_wait_for_loop",
            mock.AsyncMock(side_effect=RuntimeError("private loop detail")),
        ), redirect_stderr(buffer):
            await runtime.run_once(relay)
        rendered = buffer.getvalue()
        self.assertEqual(
            rendered.strip(),
            "[memory-hierarchy-summary-shadow] status=failed category=memory_hierarchy_summary_shadow_unavailable",
        )
        self.assertNotIn("private loop detail", rendered)

    def test_codex_api_loop_source_exposes_internal_route_and_public_relay_does_not(self):
        root = Path(__file__).resolve().parents[2]
        loop_source = (root / "examples" / "api_loop_codex_canary.py").read_text(encoding="utf-8")
        relay_source = (root / "backend" / "p3_relay_app.py").read_text(encoding="utf-8")
        self.assertIn("memory_hierarchy_summary_loopback_v2.ENDPOINT", loop_source)
        self.assertIn("loop_memory_hierarchy_summary_v2", loop_source)
        self.assertNotIn("/app/memory/hierarchy-summary", relay_source)
        self.assertNotIn("/memory/hierarchy-summary-v2", relay_source)


if __name__ == "__main__":
    unittest.main()
