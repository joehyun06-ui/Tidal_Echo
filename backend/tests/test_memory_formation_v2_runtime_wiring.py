from __future__ import annotations

import asyncio
import importlib
import json
import os
import tempfile
import unittest
from unittest import mock

from backend.tests._support import NoNetworkMixin, load_app, request


MEMORY_SECRET = "Synthetic-App-Candidate-HMAC-Key-2026!Z9q7"
V1_SESSION = "memory-formation-extractor-v1"
GATE = "MEMORY_FORMATION_V2_SHADOW_ENABLED"


def v1_output(signal_type: str, start: int, end: int) -> str:
    return json.dumps(
        {
            "version": "memory-formation-extractor-v1",
            "proposals": [{
                "signal_type": signal_type,
                "start": start,
                "end": end,
            }],
        },
        separators=(",", ":"),
    )


def v2_output(signal_type: str, spans: list[tuple[int, int]]) -> str:
    return json.dumps(
        {
            "version": "memory-formation-extractor-v2",
            "proposals": [{
                "signal_type": signal_type,
                "spans": [
                    {"start": start, "end": end}
                    for start, end in spans
                ],
            }],
        },
        separators=(",", ":"),
    )


def part_span(source: str, part: str) -> tuple[int, int]:
    start = source.index(part)
    return start, start + len(part)


class MemoryFormationV2RuntimeWiringTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def load(
        self,
        *,
        gate: str | None,
        auto_formation: bool = True,
        natural_ingress: bool = False,
        persistence: bool = True,
    ):
        if gate is None:
            os.environ.pop(GATE, None)
        else:
            os.environ[GATE] = gate
        module = load_app(
            self.temp.name,
            telegram=False,
            kelivo=True,
            memory=True,
            memory_auto_formation=auto_formation,
            memory_natural_ingress_formation=natural_ingress,
            memory_secret=MEMORY_SECRET,
            memory_candidate_persistence=persistence,
        )
        patch = importlib.reload(
            importlib.import_module("backend.memory_formation_v2_runtime_patch")
        )
        return module, patch

    async def wait_for_shadow(self, module):
        for _ in range(600):
            task = getattr(module.app.state, "memory_formation_shadow_task", None)
            if task is None:
                return
            if task.done():
                await task
                await asyncio.sleep(0)
                return
            await asyncio.sleep(0.005)
        self.fail("memory formation shadow did not finish")

    async def post(self, module, key: str, source: str):
        return await request(
            module,
            "POST",
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer test-kelivo-key-distinct-1234567890",
                "Idempotency-Key": key,
            },
            json={
                "model": "ouou-home",
                "messages": [{"role": "user", "content": source}],
                "stream": False,
            },
        )

    def v2_extraction(self, patch, source: str, signal_type: str, spans):
        return patch.memory_formation_extractor_v2._parse_model_output(
            v2_output(signal_type, list(spans)),
            len(source),
        )

    def memory_counts(self, module) -> dict[str, int]:
        with module.db() as conn:
            return {
                table: int(conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0])
                for table in (
                    "memory_items",
                    "memory_candidate_sources",
                    "memory_auto_formation_runs",
                )
            }

    async def test_gate_defaults_off_and_does_not_patch_v1_task(self):
        module, patch = self.load(gate=None)
        original = module._run_memory_formation_shadow_task
        original_forward = module.forward_to_loop
        self.assertFalse(patch.install(module))
        self.assertIs(module._run_memory_formation_shadow_task, original)
        self.assertIs(module.forward_to_loop, original_forward)
        self.assertFalse(getattr(module, patch.ENABLED_MARKER, True))

    async def test_gate_is_strict_and_requires_existing_v1_auto_formation(self):
        module, patch = self.load(gate="not-a-bool")
        with self.assertRaises(module.deployment_config.DeploymentConfigError) as invalid:
            patch.install(module)
        self.assertEqual(
            invalid.exception.category,
            "invalid_memory_formation_v2_shadow_enabled",
        )

        other_temp = tempfile.TemporaryDirectory()
        self.addCleanup(other_temp.cleanup)
        os.environ[GATE] = "true"
        module = load_app(
            other_temp.name,
            telegram=False,
            kelivo=True,
            memory=True,
            memory_auto_formation=False,
        )
        patch = importlib.reload(
            importlib.import_module("backend.memory_formation_v2_runtime_patch")
        )
        with self.assertRaises(module.deployment_config.DeploymentConfigError) as relation:
            patch.install(module)
        self.assertEqual(
            relation.exception.category,
            "memory_formation_v2_shadow_requires_auto_formation",
        )

    async def test_kelivo_v1_persists_before_v2_shadow_and_v2_writes_zero(self):
        module, patch = self.load(gate="true")
        self.assertTrue(patch.install(module))
        source = (
            "Project Atlas uses PostgreSQL 16. filler. "
            "The project runs on port 5432."
        )
        first = "Project Atlas uses PostgreSQL 16."
        second = "The project runs on port 5432."
        calls = []
        order = []

        async def generate(messages, session, model, temperature, max_tokens, context):
            calls.append(session)
            if session == V1_SESSION:
                order.append("v1")
                start, end = part_span(source, first)
                return {"text": v1_output("project_fact", start, end)}
            order.append("main")
            return {"text": "authoritative reply", "usage": {}}

        async def extract_v2(**kwargs):
            order.append("v2")
            self.assertEqual(kwargs["source_text"], source)
            return self.v2_extraction(
                patch,
                source,
                "project_fact",
                [part_span(source, first), part_span(source, second)],
            )

        module.KELIVO_GENERATOR = generate
        with mock.patch.object(
            patch.memory_formation_v2_loopback,
            "extract_v2_via_loopback",
            side_effect=extract_v2,
        ) as v2_call, mock.patch("builtins.print") as printed:
            response = await self.post(module, "v2-runtime-key-0001", source)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["choices"][0]["message"]["content"],
                "authoritative reply",
            )
            await self.wait_for_shadow(module)

        self.assertEqual(calls, ["shared-test-session", V1_SESSION])
        self.assertEqual(order, ["main", "v1", "v2"])
        self.assertEqual(v2_call.await_count, 1)
        self.assertEqual(
            self.memory_counts(module),
            {
                "memory_items": 1,
                "memory_candidate_sources": 1,
                "memory_auto_formation_runs": 1,
            },
        )
        with module.db() as conn:
            memory = conn.execute(
                "SELECT normalized_content,status FROM memory_items"
            ).fetchone()
            source_row = conn.execute(
                """SELECT formation_contract_version,extractor_contract_version
                   FROM memory_candidate_sources"""
            ).fetchone()
            run = conn.execute(
                """SELECT formation_contract_version,extractor_contract_version
                   FROM memory_auto_formation_runs"""
            ).fetchone()
        self.assertEqual(memory["normalized_content"], first)
        self.assertEqual(memory["status"], "candidate")
        self.assertEqual(
            tuple(source_row),
            ("memory-formation-v1", "memory-formation-extractor-v1"),
        )
        self.assertEqual(
            tuple(run),
            ("memory-formation-v1", "memory-formation-extractor-v1"),
        )

        logs = " ".join(
            str(call.args[0])
            for call in printed.call_args_list
            if call.args
        )
        self.assertIn(
            "[memory-formation-v2-shadow] status=completed "
            "v1_status=completed v1_proposals=1 v1_candidates=1 "
            "v2_proposals=1 v2_candidates=1 v2_multi_span=1 v2_spans=2",
            logs,
        )
        self.assertNotIn("Project Atlas", logs)
        self.assertNotIn("5432", logs)

    async def test_v2_failure_cannot_rollback_v1_or_change_authoritative_response(self):
        module, patch = self.load(gate="true")
        patch.install(module)
        source = "Project Atlas uses Python."
        calls = []

        async def generate(messages, session, model, temperature, max_tokens, context):
            calls.append(session)
            if session == V1_SESSION:
                return {"text": v1_output("project_fact", 0, len(source))}
            return {"text": "stable main reply", "usage": {}}

        async def fail_v2(**_kwargs):
            raise patch.memory_formation_v2_loopback.MemoryFormationV2LoopbackError(
                "extractor_invalid_output"
            )

        module.KELIVO_GENERATOR = generate
        with mock.patch.object(
            patch.memory_formation_v2_loopback,
            "extract_v2_via_loopback",
            side_effect=fail_v2,
        ), mock.patch("builtins.print") as printed:
            response = await self.post(module, "v2-runtime-key-0002", source)
            await self.wait_for_shadow(module)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["choices"][0]["message"]["content"],
            "stable main reply",
        )
        self.assertEqual(calls, ["shared-test-session", V1_SESSION])
        self.assertEqual(
            self.memory_counts(module),
            {
                "memory_items": 1,
                "memory_candidate_sources": 1,
                "memory_auto_formation_runs": 1,
            },
        )
        logs = " ".join(
            str(call.args[0])
            for call in printed.call_args_list
            if call.args
        )
        self.assertIn(
            "[memory-formation-v2-shadow] status=failed "
            "category=extractor_invalid_output v1_status=completed "
            "v1_proposals=1 v1_candidates=1",
            logs,
        )

    async def test_web_app_send_waits_for_main_forward_before_v1_then_v2(self):
        module, patch = self.load(
            gate="true",
            natural_ingress=True,
        )
        source = (
            "Project Atlas uses Python. filler. "
            "The project runs on Render."
        )
        first = "Project Atlas uses Python."
        second = "The project runs on Render."
        forward_started = asyncio.Event()
        release_forward = asyncio.Event()
        calls = []
        order = []

        async def fake_forward(_msg):
            order.append("main-forward-start")
            forward_started.set()
            await release_forward.wait()
            order.append("main-forward-done")

        module.forward_to_loop = fake_forward
        patch.install(module)

        async def generate(messages, session, model, temperature, max_tokens, context):
            calls.append(session)
            self.assertEqual(session, V1_SESSION)
            order.append("v1")
            start, end = part_span(source, first)
            return {"text": v1_output("project_fact", start, end)}

        async def extract_v2(**kwargs):
            order.append("v2")
            return self.v2_extraction(
                patch,
                source,
                "project_fact",
                [part_span(source, first), part_span(source, second)],
            )

        module.KELIVO_GENERATOR = generate
        with mock.patch.object(
            patch.memory_formation_v2_loopback,
            "extract_v2_via_loopback",
            side_effect=extract_v2,
        ) as v2_call:
            response = await request(
                module,
                "POST",
                "/app/send",
                headers={"Authorization": "Bearer test-relay-secret"},
                json={"text": source, "api_session": "shared-test-session"},
            )
            self.assertEqual(response.status_code, 200)
            await asyncio.wait_for(forward_started.wait(), 1)
            await asyncio.sleep(0.02)
            self.assertEqual(calls, [])
            self.assertNotIn("v2", order)
            release_forward.set()
            await self.wait_for_shadow(module)

        self.assertEqual(calls, [V1_SESSION])
        self.assertEqual(
            order,
            ["main-forward-start", "main-forward-done", "v1", "v2"],
        )
        self.assertEqual(v2_call.await_count, 1)
        self.assertEqual(
            self.memory_counts(module),
            {
                "memory_items": 1,
                "memory_candidate_sources": 1,
                "memory_auto_formation_runs": 1,
            },
        )

    async def test_natural_ingress_without_tracked_forward_remains_supported(self):
        module, patch = self.load(
            gate="true",
            natural_ingress=True,
        )
        patch.install(module)
        source = "Project Atlas uses Python."
        calls = []

        async def generate(messages, session, model, temperature, max_tokens, context):
            calls.append(session)
            return {"text": v1_output("project_fact", 0, len(source))}

        async def extract_v2(**_kwargs):
            return self.v2_extraction(
                patch,
                source,
                "project_fact",
                [(0, len(source))],
            )

        module.KELIVO_GENERATOR = generate
        message = module.save_message(
            "in",
            "user",
            source,
            {"channel": "web", "source": "relay"},
        )
        with mock.patch.object(
            patch.memory_formation_v2_loopback,
            "extract_v2_via_loopback",
            side_effect=extract_v2,
        ):
            scheduled = module._schedule_natural_ingress_memory_formation_shadow(
                canonical_message_id=message["id"],
                channel="web",
                source="relay",
                generation_callable=generate,
            )
            self.assertTrue(scheduled)
            await self.wait_for_shadow(module)
        self.assertEqual(calls, [V1_SESSION])

    async def test_exact_request_replay_does_not_run_either_extractor_again(self):
        module, patch = self.load(gate="true")
        patch.install(module)
        source = "Project Atlas uses Python."
        calls = []

        async def generate(messages, session, model, temperature, max_tokens, context):
            calls.append(session)
            if session == V1_SESSION:
                return {"text": v1_output("project_fact", 0, len(source))}
            return {"text": "main reply", "usage": {}}

        async def extract_v2(**_kwargs):
            return self.v2_extraction(
                patch,
                source,
                "project_fact",
                [(0, len(source))],
            )

        module.KELIVO_GENERATOR = generate
        with mock.patch.object(
            patch.memory_formation_v2_loopback,
            "extract_v2_via_loopback",
            side_effect=extract_v2,
        ) as v2_call:
            first = await self.post(module, "v2-runtime-key-0003", source)
            self.assertEqual(first.status_code, 200)
            await self.wait_for_shadow(module)
            calls_after_fresh = tuple(calls)
            v2_after_fresh = v2_call.await_count
            replay = await self.post(module, "v2-runtime-key-0003", source)
            self.assertEqual(replay.status_code, 200)
            await asyncio.sleep(0)
            self.assertEqual(tuple(calls), calls_after_fresh)
            self.assertEqual(v2_call.await_count, v2_after_fresh)
        self.assertEqual(calls_after_fresh, (
            "shared-test-session",
            V1_SESSION,
        ))
        self.assertEqual(v2_after_fresh, 1)


if __name__ == "__main__":
    unittest.main()
