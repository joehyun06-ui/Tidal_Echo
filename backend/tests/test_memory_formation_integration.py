from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from backend import memory_formation_integration as integration
from backend.memory_formation import (
    FORMATION_CONTRACT_VERSION,
    AutoMemoryProposalV1,
)
from backend.memory_formation_extractor import (
    EXTRACTOR_CONTRACT_VERSION,
    EXTRACTOR_SESSION_ID,
    AutoMemoryExtractionV1,
    MemoryFormationExtractorError,
)
from backend.tests._support import NoNetworkMixin, load_app, request


KELIVO_KEY = "test-kelivo-key-distinct-1234567890"
MEMORY_SECRET = "Synthetic-App-Candidate-HMAC-Key-2026!Z9q7"


def extractor_output(proposals):
    return json.dumps({
        "version": EXTRACTOR_CONTRACT_VERSION,
        "proposals": proposals,
    }, separators=(",", ":"))


class ShadowCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_owned_contract_versions_are_fixed(self):
        self.assertEqual(FORMATION_CONTRACT_VERSION, "memory-formation-v1")
        self.assertEqual(
            EXTRACTOR_CONTRACT_VERSION,
            "memory-formation-extractor-v1",
        )

    async def test_accepted_callback_receives_only_canonical_source_and_proposals(self):
        source = "I usually prefer tea."
        proposal = AutoMemoryProposalV1("durable_preference", 0, len(source))
        extraction = AutoMemoryExtractionV1((proposal,))
        extractor_calls = []
        callback_calls = []

        async def extract(received_source):
            extractor_calls.append(received_source)
            return extraction

        async def accepted(*args):
            callback_calls.append(args)

        result = await integration.run_memory_formation_shadow(
            47,
            source,
            extract,
            max_item_chars=777,
            accepted_proposals_callable=accepted,
        )
        self.assertEqual(extractor_calls, [source])
        self.assertEqual(callback_calls, [(47, source, extraction.proposals)])
        self.assertIs(callback_calls[0][2], extraction.proposals)
        self.assertEqual(
            dataclasses.asdict(result),
            {
                "status": "completed",
                "category": "completed",
                "proposal_count": 1,
                "candidate_count": 1,
            },
        )

    async def test_empty_accepted_proposals_still_invokes_callback(self):
        calls = []

        async def extract(_source):
            return AutoMemoryExtractionV1(())

        async def accepted(*args):
            calls.append(args)

        result = await integration.run_memory_formation_shadow(
            9,
            "canonical source",
            extract,
            max_item_chars=1000,
            accepted_proposals_callable=accepted,
        )
        self.assertEqual(calls, [(9, "canonical source", ())])
        self.assertEqual(
            (result.status, result.category, result.proposal_count),
            ("completed", "no_proposals", 0),
        )

    async def test_callback_failure_does_not_change_shadow_semantics(self):
        source = "Project Atlas uses Python."
        proposal = AutoMemoryProposalV1("project_fact", 0, len(source))

        async def extract(_source):
            return AutoMemoryExtractionV1((proposal,))

        async def fail(*_args):
            raise RuntimeError("PRIVATE CALLBACK DETAIL")

        result = await integration.run_memory_formation_shadow(
            11,
            source,
            extract,
            max_item_chars=1000,
            accepted_proposals_callable=fail,
        )
        self.assertEqual(
            (result.status, result.category, result.candidate_count),
            ("completed", "completed", 1),
        )
        self.assertNotIn("PRIVATE CALLBACK DETAIL", repr(result))

    async def test_callback_cancellation_propagates(self):
        async def extract(_source):
            return AutoMemoryExtractionV1(())

        async def cancel(*_args):
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await integration.run_memory_formation_shadow(
                12,
                "canonical source",
                extract,
                max_item_chars=1000,
                accepted_proposals_callable=cancel,
            )

    async def test_rejected_builder_output_never_invokes_callback(self):
        source = "Do not remember that I usually prefer coffee."
        proposal = AutoMemoryProposalV1(
            "durable_preference",
            source.index("I usually"),
            len(source),
        )
        callback = mock.AsyncMock()

        async def extract(_source):
            return AutoMemoryExtractionV1((proposal,))

        result = await integration.run_memory_formation_shadow(
            13,
            source,
            extract,
            max_item_chars=1000,
            accepted_proposals_callable=callback,
        )
        self.assertEqual(result.status, "failed")
        callback.assert_not_awaited()

    async def test_exact_canonical_source_and_max_chars_reach_phase4a(self):
        source = "I usually prefer tea."
        proposal = AutoMemoryProposalV1("durable_preference", 0, len(source))
        extraction = AutoMemoryExtractionV1((proposal,))
        extractor_calls = []
        builder_calls = []

        async def extract(received_source):
            extractor_calls.append(received_source)
            return extraction

        def build(*args, **kwargs):
            builder_calls.append((args, kwargs))
            return (object(),)

        with mock.patch.object(integration, "build_auto_memory_candidates", side_effect=build):
            result = await integration.run_memory_formation_shadow(
                47, source, extract, max_item_chars=777,
            )
        self.assertEqual(extractor_calls, [source])
        self.assertEqual(builder_calls, [((47, source, extraction.proposals), {
            "max_item_chars": 777,
        })])
        self.assertEqual(
            (result.status, result.category, result.proposal_count, result.candidate_count),
            ("completed", "completed", 1, 1),
        )

    async def test_candidates_are_discarded_and_result_contains_counts_only(self):
        secret = "PRIVATE-CANDIDATE-PLAINTEXT"
        proposal = AutoMemoryProposalV1("stable_profile", 0, 1)

        async def extract(_source):
            return AutoMemoryExtractionV1((proposal,))

        candidates = (SimpleNamespace(normalized_content=secret),)
        with mock.patch.object(
            integration, "build_auto_memory_candidates", return_value=candidates,
        ):
            result = await integration.run_memory_formation_shadow(
                1, "x", extract, max_item_chars=1000,
            )
        self.assertEqual(
            [field.name for field in dataclasses.fields(result)],
            ["status", "category", "proposal_count", "candidate_count"],
        )
        self.assertNotIn(secret, repr(result))
        self.assertEqual(repr(result), "<MemoryFormationShadowResult>")

    async def test_no_proposals_runs_phase4a_and_returns_no_candidates(self):
        async def extract(_source):
            return AutoMemoryExtractionV1(())

        with mock.patch.object(
            integration, "build_auto_memory_candidates", return_value=(),
        ) as build:
            result = await integration.run_memory_formation_shadow(
                1, "source", extract, max_item_chars=1000,
            )
        build.assert_called_once_with(1, "source", (), max_item_chars=1000)
        self.assertEqual(
            (result.status, result.category, result.proposal_count, result.candidate_count),
            ("completed", "no_proposals", 0, 0),
        )

    async def test_phase4a_full_source_veto_blocks_narrow_span_bypass(self):
        source = "Do not remember that I usually prefer coffee."
        selected = "I usually prefer coffee."
        start = source.index(selected)

        async def extract(_source):
            return AutoMemoryExtractionV1((AutoMemoryProposalV1(
                "durable_preference", start, start + len(selected),
            ),))

        result = await integration.run_memory_formation_shadow(
            1, source, extract, max_item_chars=1000,
        )
        self.assertEqual(
            (result.status, result.category, result.proposal_count, result.candidate_count),
            ("failed", "source_ineligible", 1, 0),
        )

    async def test_malformed_extractor_result_produces_no_candidates(self):
        secret = "PRIVATE-RAW-MODEL-OUTPUT"

        async def extract(_source):
            raise MemoryFormationExtractorError("extractor_invalid_output")

        with mock.patch.object(
            integration,
            "build_auto_memory_candidates",
            side_effect=AssertionError(secret),
        ):
            result = await integration.run_memory_formation_shadow(
                1, secret, extract, max_item_chars=1000,
            )
        self.assertEqual(
            (result.status, result.category, result.candidate_count),
            ("failed", "extractor_invalid_output", 0),
        )
        self.assertNotIn(secret, repr(result))

    async def test_one_invalid_proposal_rejects_all_without_partial_candidates(self):
        source = "I usually prefer tea."
        proposals = (
            AutoMemoryProposalV1("durable_preference", 0, len(source)),
            AutoMemoryProposalV1("stable_profile", len(source), len(source) + 1),
        )

        async def extract(_source):
            return AutoMemoryExtractionV1(proposals)

        result = await integration.run_memory_formation_shadow(
            1, source, extract, max_item_chars=1000,
        )
        self.assertEqual(
            (result.status, result.category, result.proposal_count, result.candidate_count),
            ("failed", "candidate_rejected", 2, 0),
        )


    async def test_extractor_timeout_produces_no_candidates_or_callback(self):
        callback = mock.AsyncMock()

        async def extract(_source):
            raise MemoryFormationExtractorError("extractor_timeout")

        with mock.patch.object(
            integration,
            "build_auto_memory_candidates",
            side_effect=AssertionError("builder must not run"),
        ):
            result = await integration.run_memory_formation_shadow(
                1,
                "source",
                extract,
                max_item_chars=1000,
                accepted_proposals_callable=callback,
            )

        self.assertEqual(
            (
                result.status,
                result.category,
                result.proposal_count,
                result.candidate_count,
            ),
            ("failed", "extractor_timeout", 0, 0),
        )
        callback.assert_not_awaited()
class CanonicalFormationSourceTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(
            self.temp.name,
            telegram=False,
            kelivo=True,
            memory=True,
        )
        self.headers = {
            "Authorization": f"Bearer {KELIVO_KEY}",
        }

        async def generate(*_args):
            return {"text": "main reply", "usage": {}}

        self.module.KELIVO_GENERATOR = generate

    async def complete(self, key, text="I usually prefer tea."):
        headers = {**self.headers, "Idempotency-Key": key}
        response = await request(
            self.module,
            "POST",
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "ouou-home",
                "messages": [{"role": "user", "content": text}],
                "stream": False,
            },
        )
        self.assertEqual(response.status_code, 200)
        return text

    async def test_loader_returns_only_exact_committed_canonical_id_and_text(self):
        text = await self.complete("canonical-source-key-0001")
        source = self.module.kelivo_service.load_completed_canonical_formation_source(
            self.module.DB_PATH,
            "primary-kelivo",
            "canonical-source-key-0001",
        )
        with self.module.db() as conn:
            row = conn.execute(
                """SELECT user_message_id FROM kelivo_requests
                   WHERE client_id=? AND idempotency_key=?""",
                ("primary-kelivo", "canonical-source-key-0001"),
            ).fetchone()
        self.assertEqual(source.canonical_message_id, row["user_message_id"])
        self.assertEqual(source.text, text)
        self.assertEqual(repr(source), "<CanonicalFormationSource>")
        self.assertNotIn(text, repr(source))

    async def test_loader_fails_closed_for_missing_incomplete_and_tampered_relationships(self):
        with self.assertRaises(self.module.kelivo_service.KelivoError):
            self.module.kelivo_service.load_completed_canonical_formation_source(
                self.module.DB_PATH, "primary-kelivo", "missing-request-key",
            )

        mutations = (
            ("status", "UPDATE kelivo_requests SET status='failed' WHERE idempotency_key=?"),
            ("message-id", "UPDATE kelivo_requests SET user_message_id=NULL WHERE idempotency_key=?"),
            ("direction", "UPDATE messages SET direction='out' WHERE id=?"),
            ("kind", "UPDATE messages SET kind='reply' WHERE id=?"),
            ("text", "UPDATE messages SET text='' WHERE id=?"),
            (
                "metadata",
                "UPDATE messages SET meta='{\"api_session\":\"wrong\",\"channel\":\"kelivo\",\"generation_id\":\"wrong\"}' WHERE id=?",
            ),
        )
        for index, (name, statement) in enumerate(mutations):
            key = f"canonical-tamper-key-{index:04d}"
            await self.complete(key)
            with self.module.db() as conn:
                request_row = conn.execute(
                    "SELECT user_message_id FROM kelivo_requests WHERE idempotency_key=?",
                    (key,),
                ).fetchone()
                parameter = key if name in {"status", "message-id"} else request_row["user_message_id"]
                conn.execute(statement, (parameter,))
                conn.commit()
            with self.subTest(name=name), self.assertRaises(
                self.module.kelivo_service.KelivoError
            ) as raised:
                self.module.kelivo_service.load_completed_canonical_formation_source(
                    self.module.DB_PATH, "primary-kelivo", key,
                )
            self.assertEqual(raised.exception.category, "canonical_source_unavailable")


class MemoryFormationAppIntegrationTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(
            self.temp.name,
            telegram=False,
            kelivo=True,
            memory=True,
            memory_auto_formation=True,
        )
        self.headers = {
            "Authorization": f"Bearer {KELIVO_KEY}",
        }

    async def asyncTearDown(self):
        task = getattr(self.module.app.state, "memory_formation_shadow_task", None)
        if task is not None and not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    def payload(self, text="I usually prefer tea."):
        return {
            "model": "ouou-home",
            "messages": [{"role": "user", "content": text}],
            "stream": False,
        }

    async def post(self, key, *, text="I usually prefer tea.", path="/v1/chat/completions", headers=None):
        request_headers = (
            {**self.headers, "Idempotency-Key": key}
            if headers is None else headers
        )
        return await request(
            self.module,
            "POST",
            path,
            headers=request_headers,
            json=self.payload(text),
        )

    async def wait_for_shadow_idle(self):
        for _ in range(200):
            task = getattr(self.module.app.state, "memory_formation_shadow_task", None)
            if task is None:
                return
            await asyncio.sleep(0.005)
        self.fail("shadow task did not finish")

    async def test_fresh_success_schedules_once_after_response_without_waiting_and_writes_nothing(self):
        source = "I usually prefer tea."
        extractor_started = asyncio.Event()
        release_extractor = asyncio.Event()
        calls = []

        async def generate(messages, session, model, temperature, max_tokens, context):
            calls.append((messages, session, model, temperature, max_tokens, context))
            if session == EXTRACTOR_SESSION_ID:
                extractor_started.set()
                await release_extractor.wait()
                return {"text": extractor_output([{
                    "signal_type": "durable_preference",
                    "start": 0,
                    "end": len(source),
                }])}
            return {"text": "main reply", "usage": {"total_tokens": 3}}

        self.module.KELIVO_GENERATOR = generate
        with self.module.db() as conn:
            schema_before = tuple(conn.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall())
        with mock.patch("builtins.print") as printed:
            response = await self.post("fresh-shadow-key-0001", text=source)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["choices"][0]["message"]["content"],
                "main reply",
            )
            await asyncio.wait_for(extractor_started.wait(), 2)
            shadow_task = self.module.app.state.memory_formation_shadow_task
            self.assertIsNotNone(shadow_task)
            self.assertFalse(shadow_task.done())
            release_extractor.set()
            await shadow_task
            await asyncio.sleep(0)

        self.assertEqual(len(calls), 2)
        main_call, extractor_call = calls
        self.assertEqual(main_call[1], "shared-test-session")
        self.assertEqual(extractor_call[1], EXTRACTOR_SESSION_ID)
        self.assertEqual(extractor_call[2], main_call[2])
        self.assertEqual(extractor_call[3], 0.0)
        self.assertLessEqual(extractor_call[4], 256)
        self.assertEqual(extractor_call[5], {
            "prompt_contract_version": "kelivo-provider-prompt-v1",
            "memory_formation_extractor": EXTRACTOR_CONTRACT_VERSION,
        })
        self.assertEqual(extractor_call[0][-1], {"role": "user", "content": source})

        with self.module.db() as conn:
            counts = {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "memory_items",
                    "memory_sources",
                    "memory_candidate_sources",
                    "memory_auto_formation_runs",
                    "kelivo_requests",
                    "messages",
                )
            }
            row = conn.execute(
                """SELECT status,provider_messages_json,context_bundle_json,response_json
                   FROM kelivo_requests"""
            ).fetchone()
            schema_after = tuple(conn.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall())
        self.assertEqual(counts, {
            "memory_items": 0,
            "memory_sources": 0,
            "memory_candidate_sources": 0,
            "memory_auto_formation_runs": 0,
            "kelivo_requests": 1,
            "messages": 2,
        })
        self.assertIsNone(self.module.MEMORY_CANDIDATE_PERSISTENCE)
        self.assertIsNone(self.module.MEMORY_PRIVILEGED_RUNTIME)
        logs = " ".join(
            str(call.args[0])
            for call in printed.call_args_list
            if call.args
        )
        self.assertIn(
            "[memory-formation-shadow] status=completed proposals=1 candidates=1",
            logs,
        )
        self.assertNotIn("[memory-candidate-persistence]", logs)
        self.assertEqual(row["status"], "completed")
        persisted_messages = json.loads(row["provider_messages_json"])
        persisted_bundle = json.loads(row["context_bundle_json"])
        self.assertEqual(persisted_messages, list(main_call[0]))
        self.assertEqual(persisted_bundle["provider_messages"], persisted_messages)
        persisted = row["provider_messages_json"] + row["context_bundle_json"] + row["response_json"]
        self.assertNotIn(EXTRACTOR_CONTRACT_VERSION, persisted)
        self.assertEqual(schema_after, schema_before)

    async def test_extractor_timeout_telemetry_is_bounded_and_data_free(self):
        secret = "PRIVATE-TIMEOUT-SOURCE-AND-ID"

        with mock.patch("builtins.print") as printed:
            self.module._log_memory_formation_shadow(
                status="failed",
                category="extractor_timeout",
            )

        logs = " ".join(
            str(call.args[0])
            for call in printed.call_args_list
            if call.args
        )
        self.assertEqual(
            logs,
            "[memory-formation-shadow] status=failed category=extractor_timeout",
        )
        self.assertNotIn(secret, logs)
    async def test_default_off_performs_no_shadow_lookup_call_or_log_and_preserves_contract(self):
        other_temp = tempfile.TemporaryDirectory()
        self.addCleanup(other_temp.cleanup)
        module = load_app(
            other_temp.name,
            telegram=False,
            kelivo=True,
            memory=True,
            memory_auto_formation=False,
        )
        calls = []

        async def generate(*args):
            calls.append(args)
            return {"text": "main reply", "usage": {}}

        module.KELIVO_GENERATOR = generate
        headers = {
            "Authorization": f"Bearer {KELIVO_KEY}",
            "Idempotency-Key": "default-off-key-0001",
        }
        with mock.patch.object(
            module.kelivo_service,
            "load_completed_canonical_formation_source",
            side_effect=AssertionError("source loader must not run"),
        ), mock.patch.object(
            module.memory_formation_extractor,
            "extract_auto_memory_proposals",
            side_effect=AssertionError("extractor must not run"),
        ), mock.patch("builtins.print") as printed:
            response = await request(
                module,
                "POST",
                "/v1/chat/completions",
                headers=headers,
                json=self.payload(),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(getattr(module.app.state, "memory_formation_shadow_task", None))
        self.assertFalse(any(
            call.args and "[memory-formation-shadow]" in str(call.args[0])
            for call in printed.call_args_list
        ))
        with module.db() as conn:
            row = conn.execute(
                """SELECT request_identity_hash,context_bundle_hash,context_bundle_json,
                          provider_messages_json,provider_model,effective_temperature,
                          effective_max_tokens,mapping_revision,api_session
                   FROM kelivo_requests"""
            ).fetchone()
        bundle = json.loads(row["context_bundle_json"])
        provider_messages = json.loads(row["provider_messages_json"])
        self.assertEqual(provider_messages, list(calls[0][0]))
        self.assertEqual(bundle["provider_messages"], provider_messages)
        self.assertFalse(any("formation" in key for key in bundle))
        self.assertEqual(
            row["context_bundle_hash"],
            module.kelivo_service.content_hash(bundle)[1],
        )
        expected_identity = module.kelivo_service.build_request_identity_hash(
            virtual_model="ouou-home",
            provider_model=row["provider_model"],
            client_id="primary-kelivo",
            api_session=row["api_session"],
            mapping_revision=row["mapping_revision"],
            persona_hash=bundle["persona"]["hash"],
            snapshot_correlations={
                key: None if bundle["snapshots"][key] is None else {
                    name: bundle["snapshots"][key][name]
                    for name in ("id", "version", "hash")
                }
                for key in ("system", "developer")
            },
            provider_messages=provider_messages,
            effective_temperature=row["effective_temperature"],
            effective_max_tokens=row["effective_max_tokens"],
        )
        self.assertEqual(row["request_identity_hash"], expected_identity)

    async def test_replay_schedules_zero_additional_extractions(self):
        calls = []

        async def generate(*args):
            calls.append(args)
            if args[1] == EXTRACTOR_SESSION_ID:
                return {"text": extractor_output([])}
            return {"text": "main reply", "usage": {}}

        self.module.KELIVO_GENERATOR = generate
        first = await self.post("replay-shadow-key-0001")
        self.assertEqual(first.status_code, 200)
        await self.wait_for_shadow_idle()
        calls_after_fresh = len(calls)
        replay = await self.post("replay-shadow-key-0001")
        self.assertEqual(replay.status_code, 200)
        await asyncio.sleep(0)
        self.assertEqual(len(calls), calls_after_fresh)
        self.assertIsNone(getattr(self.module.app.state, "memory_formation_shadow_task", None))

    async def test_failed_generation_and_completion_commit_schedule_zero(self):
        async def fail_generation(*_args):
            raise self.module.kelivo_service.GenerationError("synthetic_failure", False)

        self.module.KELIVO_GENERATOR = fail_generation
        with mock.patch.object(self.module, "_schedule_memory_formation_shadow") as scheduled:
            failed = await self.post("failed-shadow-key-0001")
        self.assertEqual(failed.status_code, 502)
        scheduled.assert_not_called()

        async def successful_generation(*_args):
            return {"text": "main reply", "usage": {}}

        self.module.KELIVO_GENERATOR = successful_generation
        with mock.patch.object(
            self.module.kelivo_service,
            "complete_request",
            side_effect=sqlite3.OperationalError("synthetic commit failure"),
        ), mock.patch.object(self.module, "_schedule_memory_formation_shadow") as scheduled:
            commit_failed = await self.post("commit-failed-shadow-key-0001")
        self.assertEqual(commit_failed.status_code, 504)
        scheduled.assert_not_called()

    async def test_blocked_path_schedules_zero(self):
        validated = self.module.kelivo_service.validate_completion(
            self.payload(), "ouou-home",
        )
        provider_defaults = SimpleNamespace(
            provider_model="test-provider-model",
            temperature=0.7,
            max_tokens=2000,
        )
        blocked = self.module.kelivo_service.PreparedRequest(
            "blocked",
            "generation",
            "shared-test-session",
            error_category="idempotency_in_progress",
        )
        with mock.patch.object(
            self.module.kelivo_service, "lookup_request", return_value=blocked,
        ), mock.patch.object(self.module, "_schedule_memory_formation_shadow") as scheduled:
            response = await self.module._run_completion_state_machine(
                validated,
                client_id="primary-kelivo",
                model_alias="ouou-home",
                provider_defaults=provider_defaults,
                effective_temperature=0.7,
                effective_max_tokens=2000,
                persona_source=self.module.KELIVO_PERSONA_SOURCE,
                idempotency_key="blocked-shadow-key-0001",
            )
        self.assertEqual(response.status_code, 409)
        scheduled.assert_not_called()

    async def test_operit_success_schedules_zero(self):
        other_temp = tempfile.TemporaryDirectory()
        self.addCleanup(other_temp.cleanup)
        module = load_app(
            other_temp.name,
            telegram=False,
            kelivo=True,
            auto_idempotency=True,
            operit_share=True,
            memory=True,
            memory_auto_formation=True,
            memory_secret=MEMORY_SECRET,
            memory_candidate_persistence=True,
        )
        calls = []

        async def generate(*args):
            calls.append(args)
            return {"text": "operit reply", "usage": {}}

        module.KELIVO_GENERATOR = generate
        with mock.patch.object(
            module.MEMORY_CANDIDATE_PERSISTENCE,
            "persist",
            wraps=module.MEMORY_CANDIDATE_PERSISTENCE.persist,
        ) as persisted:
            response = await request(
                module,
                "POST",
                "/v1/operit/share",
                headers={"Authorization": "Bearer test-operit-share-key-distinct-1234567890"},
                json=self.payload("shared text"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0][1], EXTRACTOR_SESSION_ID)
        self.assertIsNone(getattr(module.app.state, "memory_formation_shadow_task", None))
        persisted.assert_not_called()

    async def test_extractor_failure_cannot_change_response_or_completed_request(self):
        secret = "PRIVATE-EXTRACTOR-RAW-OUTPUT"
        calls = []

        async def generate(*args):
            calls.append(args)
            if args[1] == EXTRACTOR_SESSION_ID:
                return {"text": secret}
            return {"text": "main reply", "usage": {}}

        self.module.KELIVO_GENERATOR = generate
        with mock.patch("builtins.print") as printed:
            response = await self.post("extractor-failure-key-0001")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["choices"][0]["message"]["content"], "main reply")
            await self.wait_for_shadow_idle()
        with self.module.db() as conn:
            row = conn.execute(
                """SELECT status,error_category,response_json FROM kelivo_requests
                   WHERE idempotency_key=?""",
                ("extractor-failure-key-0001",),
            ).fetchone()
            request_count = conn.execute("SELECT count(*) FROM kelivo_requests").fetchone()[0]
        self.assertEqual((row["status"], row["error_category"]), ("completed", None))
        self.assertEqual(json.loads(row["response_json"]), response.json())
        self.assertEqual(request_count, 1)
        logs = " ".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("status=failed category=extractor_invalid_output", logs)
        self.assertNotIn(secret, logs)

    async def test_post_commit_scheduler_exception_cannot_change_authoritative_response(self):
        sentinel = "PRIVATE-POST-COMMIT-SCHEDULER-SENTINEL"

        async def generate(*_args):
            return {"text": "exact authoritative reply", "usage": {}}

        self.module.KELIVO_GENERATOR = generate
        with mock.patch.object(
            self.module,
            "_schedule_memory_formation_shadow",
            side_effect=RuntimeError(sentinel),
        ), mock.patch.object(
            self.module.kelivo_service,
            "fail_request",
            wraps=self.module.kelivo_service.fail_request,
        ) as failed, mock.patch("builtins.print") as printed:
            response = await self.post("post-commit-scheduler-key-0001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["choices"][0]["message"]["content"],
            "exact authoritative reply",
        )
        failed.assert_not_called()
        with self.module.db() as conn:
            row = conn.execute(
                """SELECT status,error_category,response_json FROM kelivo_requests
                   WHERE idempotency_key=?""",
                ("post-commit-scheduler-key-0001",),
            ).fetchone()
            failed_rows = conn.execute(
                """SELECT count(*) FROM kelivo_requests
                   WHERE status IN ('failed','dispatch_uncertain')"""
            ).fetchone()[0]
        self.assertEqual((row["status"], row["error_category"]), ("completed", None))
        self.assertEqual(json.loads(row["response_json"]), response.json())
        self.assertEqual(failed_rows, 0)
        logs = " ".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("status=failed category=scheduler_unavailable", logs)
        self.assertNotIn(sentinel, logs)

    async def test_shadow_logging_io_failure_cannot_change_completed_response(self):
        async def generate(*_args):
            return {"text": "reply survives broken logging", "usage": {}}

        self.module.KELIVO_GENERATOR = generate
        with mock.patch.object(
            self.module,
            "_schedule_memory_formation_shadow",
            side_effect=RuntimeError("private scheduler failure"),
        ), mock.patch("builtins.print", side_effect=BrokenPipeError("private log failure")):
            response = await self.post("broken-shadow-log-key-0001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["choices"][0]["message"]["content"],
            "reply survives broken logging",
        )
        with self.module.db() as conn:
            row = conn.execute(
                """SELECT status,error_category,response_json FROM kelivo_requests
                   WHERE idempotency_key=?""",
                ("broken-shadow-log-key-0001",),
            ).fetchone()
        self.assertEqual((row["status"], row["error_category"]), ("completed", None))
        self.assertEqual(json.loads(row["response_json"]), response.json())

    async def test_shutdown_state_rejects_new_shadow_without_task_or_queue(self):
        self.module.app.state.shutting_down = True
        self.module.app.state.memory_formation_shadow_task = None
        try:
            with mock.patch.object(
                self.module, "_run_memory_formation_shadow_task",
            ) as runner, mock.patch.object(
                self.module.asyncio, "create_task",
            ) as create_task, mock.patch("builtins.print"):
                scheduled = self.module._schedule_memory_formation_shadow(
                    client_id="primary-kelivo",
                    idempotency_key="shutdown-refusal-key-0001",
                    provider_model="test-provider-model",
                    generation_callable=object(),
                )
            self.assertFalse(scheduled)
            runner.assert_not_called()
            create_task.assert_not_called()
            self.assertIsNone(self.module.app.state.memory_formation_shadow_task)
        finally:
            self.module.app.state.shutting_down = False

    async def test_automatic_idempotency_fresh_shadow_and_replay_are_exactly_once(self):
        other_temp = tempfile.TemporaryDirectory()
        self.addCleanup(other_temp.cleanup)
        module = load_app(
            other_temp.name,
            telegram=False,
            kelivo=True,
            auto_idempotency=True,
            memory=True,
            memory_auto_formation=True,
            memory_secret=MEMORY_SECRET,
            memory_candidate_persistence=True,
        )
        source = "I usually prefer tea."
        calls = []

        async def generate(*args):
            calls.append(args)
            if args[1] == EXTRACTOR_SESSION_ID:
                return {"text": extractor_output([])}
            return {"text": "automatic main reply", "usage": {}}

        module.KELIVO_GENERATOR = generate
        loaded_sources = []
        real_loader = module.kelivo_service.load_completed_canonical_formation_source

        def load_source(*args, **kwargs):
            result = real_loader(*args, **kwargs)
            loaded_sources.append((args[2], result.canonical_message_id, result.text))
            return result

        async def wait_for_idle():
            for _ in range(200):
                if getattr(module.app.state, "memory_formation_shadow_task", None) is None:
                    return
                await asyncio.sleep(0.005)
            self.fail("automatic shadow task did not finish")

        headers = {"Authorization": f"Bearer {KELIVO_KEY}"}
        with mock.patch.object(
            module.kelivo_service,
            "load_completed_canonical_formation_source",
            side_effect=load_source,
        ) as loader:
            fresh = await request(
                module,
                "POST",
                "/v1/chat/completions",
                headers=headers,
                json=self.payload(source),
            )
            self.assertEqual(fresh.status_code, 200)
            await wait_for_idle()
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                sum(call[1] == EXTRACTOR_SESSION_ID for call in calls),
                1,
            )
            loader.assert_called_once()
            self.assertEqual(len(loaded_sources), 1)
            internal_key, canonical_message_id, loaded_text = loaded_sources[0]
            self.assertTrue(internal_key.startswith("@auto:"))
            self.assertGreater(canonical_message_id, 0)
            self.assertEqual(loaded_text, source)

            with module.db() as conn:
                fresh_counts = {
                    table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in (
                        "kelivo_requests",
                        "messages",
                        "memory_items",
                        "memory_sources",
                        "memory_candidate_sources",
                        "memory_auto_formation_runs",
                    )
                }
                row = conn.execute(
                    """SELECT idempotency_key,status,error_category,provider_messages_json,
                              context_bundle_json,response_json
                       FROM kelivo_requests"""
                ).fetchone()
            self.assertEqual(fresh_counts, {
                "kelivo_requests": 1,
                "messages": 2,
                "memory_items": 0,
                "memory_sources": 0,
                "memory_candidate_sources": 0,
                "memory_auto_formation_runs": 1,
            })
            self.assertEqual(row["idempotency_key"], internal_key)
            self.assertEqual((row["status"], row["error_category"]), ("completed", None))
            persisted = (
                row["provider_messages_json"]
                + row["context_bundle_json"]
                + row["response_json"]
            )
            self.assertNotIn(EXTRACTOR_CONTRACT_VERSION, persisted)

            replay = await request(
                module,
                "POST",
                "/v1/chat/completions",
                headers=headers,
                json=self.payload(source),
            )
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json(), fresh.json())
            await asyncio.sleep(0)
            self.assertEqual(len(calls), 2)
            loader.assert_called_once()
            self.assertIsNone(
                getattr(module.app.state, "memory_formation_shadow_task", None)
            )
            with module.db() as conn:
                replay_counts = {
                    table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in (
                        "kelivo_requests",
                        "messages",
                        "memory_items",
                        "memory_sources",
                        "memory_candidate_sources",
                        "memory_auto_formation_runs",
                    )
                }
            self.assertEqual(replay_counts, fresh_counts)

    async def test_busy_runner_skips_instead_of_queueing(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def runner(**_kwargs):
            started.set()
            await release.wait()

        with mock.patch.object(
            self.module, "_run_memory_formation_shadow_task", side_effect=runner,
        ) as run, mock.patch("builtins.print") as printed:
            first = self.module._schedule_memory_formation_shadow(
                client_id="primary-kelivo",
                idempotency_key="busy-key-0001",
                provider_model="test-provider-model",
                generation_callable=object(),
            )
            await started.wait()
            second = self.module._schedule_memory_formation_shadow(
                client_id="primary-kelivo",
                idempotency_key="busy-key-0002",
                provider_model="test-provider-model",
                generation_callable=object(),
            )
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(run.await_count, 1)
            release.set()
            task = self.module.app.state.memory_formation_shadow_task
            await task
            await asyncio.sleep(0)
        logs = " ".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("status=skipped category=busy", logs)

    async def test_shutdown_cancels_and_awaits_managed_shadow_task(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def runner(**_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with mock.patch.object(self.module, "_run_memory_formation_shadow_task", side_effect=runner):
            async with self.module.lifespan(self.module.app):
                scheduled = self.module._schedule_memory_formation_shadow(
                    client_id="primary-kelivo",
                    idempotency_key="shutdown-key-0001",
                    provider_model="test-provider-model",
                    generation_callable=object(),
                )
                self.assertTrue(scheduled)
                await started.wait()
            self.assertTrue(cancelled.is_set())
            self.assertIsNone(self.module.app.state.memory_formation_shadow_task)


class MemoryCandidatePersistenceAppIntegrationTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(
            self.temp.name,
            telegram=False,
            kelivo=True,
            memory=True,
            memory_secret=MEMORY_SECRET,
            memory_auto_formation=True,
            memory_candidate_persistence=True,
        )
        self.headers = {
            "Authorization": f"Bearer {KELIVO_KEY}",
        }

    async def asyncTearDown(self):
        task = getattr(
            self.module.app.state,
            "memory_formation_shadow_task",
            None,
        )
        if task is not None and not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    @staticmethod
    def payload(text="I usually prefer tea."):
        return {
            "model": "ouou-home",
            "messages": [{"role": "user", "content": text}],
            "stream": False,
        }

    async def post(self, key, *, text="I usually prefer tea.", headers=None):
        return await request(
            self.module,
            "POST",
            "/v1/chat/completions",
            headers=(
                {**self.headers, "Idempotency-Key": key}
                if headers is None
                else headers
            ),
            json=self.payload(text),
        )

    async def wait_for_idle(self):
        for _ in range(300):
            task = getattr(
                self.module.app.state,
                "memory_formation_shadow_task",
                None,
            )
            if task is None:
                return
            await asyncio.sleep(0.005)
        self.fail("candidate persistence formation task did not finish")

    def memory_counts(self):
        with self.module.db() as conn:
            return {
                table: int(conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0])
                for table in (
                    "memory_items",
                    "memory_candidate_sources",
                    "memory_auto_formation_runs",
                    "memory_sources",
                    "memory_evidence_events",
                    "memory_action_requests",
                )
            }

    async def test_fresh_durable_signal_persists_candidate_after_response(self):
        source = "Project Atlas uses Python."
        calls = []

        async def generate(*args):
            calls.append(args)
            if args[1] == EXTRACTOR_SESSION_ID:
                return {"text": extractor_output([{
                    "signal_type": "project_fact",
                    "start": 0,
                    "end": len(source),
                }])}
            return {"text": "main reply", "usage": {"total_tokens": 3}}

        self.module.KELIVO_GENERATOR = generate
        headers = {
            **self.headers,
            "Idempotency-Key": "candidate-fresh-key-0001",
            "X-Memory-Formation-Contract-Version": "attacker-v99",
            "X-Memory-Extractor-Contract-Version": "attacker-v99",
        }
        with mock.patch.dict(
            "os.environ",
            {"MEMORY_FORMATION_CONTRACT_VERSION": "attacker-v99"},
        ), mock.patch("builtins.print") as printed:
            response = await self.post(
                "candidate-fresh-key-0001",
                text=source,
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["choices"][0]["message"]["content"],
                "main reply",
            )
            await self.wait_for_idle()
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            sum(call[1] == EXTRACTOR_SESSION_ID for call in calls),
            1,
        )
        self.assertEqual(self.memory_counts(), {
            "memory_items": 1,
            "memory_candidate_sources": 1,
            "memory_auto_formation_runs": 1,
            "memory_sources": 0,
            "memory_evidence_events": 0,
            "memory_action_requests": 0,
        })
        with self.module.db() as conn:
            item = conn.execute("SELECT * FROM memory_items").fetchone()
            run = conn.execute(
                "SELECT * FROM memory_auto_formation_runs"
            ).fetchone()
            active_count = int(conn.execute(
                "SELECT count(*) FROM memory_items WHERE status='active'"
            ).fetchone()[0])
        self.assertEqual(item["status"], "candidate")
        self.assertEqual(item["normalized_content"], source)
        self.assertEqual(active_count, 0)
        self.assertEqual(
            run["formation_contract_version"],
            FORMATION_CONTRACT_VERSION,
        )
        self.assertEqual(
            run["extractor_contract_version"],
            EXTRACTOR_CONTRACT_VERSION,
        )
        logs = " ".join(
            str(call.args[0])
            for call in printed.call_args_list
            if call.args
        )
        self.assertIn(
            "[memory-formation-shadow] status=completed proposals=1 candidates=1",
            logs,
        )
        self.assertIn(
            "[memory-candidate-persistence] status=completed "
            "created=1 existing=0 active_duplicate=0 suppressed=0 replayed=0",
            logs,
        )
        self.assertNotIn(source, logs)
        self.assertNotIn("attacker-v99", logs)

    async def test_valid_zero_proposal_output_writes_terminal_run_only(self):
        calls = []

        async def generate(*args):
            calls.append(args)
            if args[1] == EXTRACTOR_SESSION_ID:
                return {"text": extractor_output([])}
            return {"text": "main reply", "usage": {}}

        self.module.KELIVO_GENERATOR = generate
        with mock.patch("builtins.print") as printed:
            response = await self.post("candidate-zero-key-0001")
            self.assertEqual(response.status_code, 200)
            await self.wait_for_idle()
        self.assertEqual(
            sum(call[1] == EXTRACTOR_SESSION_ID for call in calls),
            1,
        )
        self.assertEqual(self.memory_counts(), {
            "memory_items": 0,
            "memory_candidate_sources": 0,
            "memory_auto_formation_runs": 1,
            "memory_sources": 0,
            "memory_evidence_events": 0,
            "memory_action_requests": 0,
        })
        logs = " ".join(
            str(call.args[0])
            for call in printed.call_args_list
            if call.args
        )
        self.assertIn(
            "status=completed created=0 existing=0 "
            "active_duplicate=0 suppressed=0 replayed=0",
            logs,
        )
        self.assertIn(
            "[memory-formation-shadow] status=completed proposals=0 candidates=0",
            logs,
        )

    async def test_rejected_and_invalid_extractor_outputs_write_no_memory(self):
        source = "Do not remember that I usually prefer coffee."
        selected = "I usually prefer coffee."
        start = source.index(selected)

        async def ineligible(*args):
            if args[1] == EXTRACTOR_SESSION_ID:
                return {"text": extractor_output([{
                    "signal_type": "durable_preference",
                    "start": start,
                    "end": start + len(selected),
                }])}
            return {"text": "main reply", "usage": {}}

        self.module.KELIVO_GENERATOR = ineligible
        response = await self.post("candidate-ineligible-key-0001", text=source)
        self.assertEqual(response.status_code, 200)
        await self.wait_for_idle()
        self.assertEqual(sum(self.memory_counts().values()), 0)

        async def invalid(*args):
            if args[1] == EXTRACTOR_SESSION_ID:
                return {"text": "PRIVATE INVALID EXTRACTOR OUTPUT"}
            return {"text": "main reply", "usage": {}}

        self.module.KELIVO_GENERATOR = invalid
        response = await self.post("candidate-invalid-key-0001")
        self.assertEqual(response.status_code, 200)
        await self.wait_for_idle()
        self.assertEqual(sum(self.memory_counts().values()), 0)

        async def unavailable(*args):
            if args[1] == EXTRACTOR_SESSION_ID:
                raise self.module.kelivo_service.GenerationError(
                    "PRIVATE EXTRACTOR PROVIDER ERROR",
                    False,
                )
            return {"text": "main reply", "usage": {}}

        self.module.KELIVO_GENERATOR = unavailable
        response = await self.post("candidate-unavailable-key-0001")
        self.assertEqual(response.status_code, 200)
        await self.wait_for_idle()
        self.assertEqual(sum(self.memory_counts().values()), 0)

    async def test_failed_and_uncertain_main_requests_never_persist(self):
        with mock.patch.object(
            self.module.MEMORY_CANDIDATE_PERSISTENCE,
            "persist",
            wraps=self.module.MEMORY_CANDIDATE_PERSISTENCE.persist,
        ) as persisted:
            for uncertain, expected_status in ((False, 502), (True, 504)):
                async def fail(*_args, uncertain=uncertain):
                    raise self.module.kelivo_service.GenerationError(
                        "synthetic_generation_failure",
                        uncertain,
                    )

                self.module.KELIVO_GENERATOR = fail
                with self.subTest(uncertain=uncertain):
                    response = await self.post(
                        f"candidate-main-failure-{int(uncertain)}-0001"
                    )
                    self.assertEqual(response.status_code, expected_status)
                    self.assertIsNone(getattr(
                        self.module.app.state,
                        "memory_formation_shadow_task",
                        None,
                    ))
            persisted.assert_not_called()
        self.assertEqual(sum(self.memory_counts().values()), 0)

    async def test_persistence_error_isolated_from_http_and_shadow_semantics(self):
        source = "Project Atlas uses Python."

        async def generate(*args):
            if args[1] == EXTRACTOR_SESSION_ID:
                return {"text": extractor_output([{
                    "signal_type": "project_fact",
                    "start": 0,
                    "end": len(source),
                }])}
            return {"text": "authoritative reply", "usage": {}}

        class StablePersistenceError(RuntimeError):
            category = "storage_unavailable"

        self.module.KELIVO_GENERATOR = generate
        with mock.patch.object(
            self.module.MEMORY_CANDIDATE_PERSISTENCE,
            "persist",
            side_effect=StablePersistenceError("PRIVATE SQLITE DETAIL"),
        ) as persisted, mock.patch("builtins.print") as printed:
            response = await self.post(
                "candidate-persistence-failure-key-0001",
                text=source,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["choices"][0]["message"]["content"],
                "authoritative reply",
            )
            await self.wait_for_idle()
        persisted.assert_called_once()
        self.assertEqual(sum(self.memory_counts().values()), 0)
        logs = " ".join(
            str(call.args[0])
            for call in printed.call_args_list
            if call.args
        )
        self.assertIn(
            "[memory-formation-shadow] status=completed proposals=1 candidates=1",
            logs,
        )
        self.assertIn(
            "[memory-candidate-persistence] "
            "status=failed category=storage_unavailable",
            logs,
        )
        self.assertNotIn("PRIVATE SQLITE DETAIL", logs)
        self.assertNotIn(source, logs)

    async def test_explicit_replay_does_not_repeat_extractor_or_persistence(self):
        calls = []

        async def generate(*args):
            calls.append(args)
            if args[1] == EXTRACTOR_SESSION_ID:
                return {"text": extractor_output([])}
            return {"text": "main reply", "usage": {}}

        self.module.KELIVO_GENERATOR = generate
        first = await self.post("candidate-replay-key-0001")
        self.assertEqual(first.status_code, 200)
        await self.wait_for_idle()
        before = self.memory_counts()
        replay = await self.post("candidate-replay-key-0001")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        await asyncio.sleep(0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            sum(call[1] == EXTRACTOR_SESSION_ID for call in calls),
            1,
        )
        self.assertEqual(self.memory_counts(), before)
        self.assertIsNone(
            getattr(self.module.app.state, "memory_formation_shadow_task", None)
        )

    async def test_source_load_failure_never_calls_persistence(self):
        async def generate(*_args):
            return {"text": "main reply", "usage": {}}

        self.module.KELIVO_GENERATOR = generate
        with mock.patch.object(
            self.module.kelivo_service,
            "load_completed_canonical_formation_source",
            side_effect=self.module.kelivo_service.KelivoError(
                503,
                "canonical_source_unavailable",
            ),
        ), mock.patch.object(
            self.module.MEMORY_CANDIDATE_PERSISTENCE,
            "persist",
            wraps=self.module.MEMORY_CANDIDATE_PERSISTENCE.persist,
        ) as persisted, mock.patch("builtins.print") as printed:
            response = await self.post("candidate-source-failure-key-0001")
            self.assertEqual(response.status_code, 200)
            await self.wait_for_idle()
        persisted.assert_not_called()
        self.assertEqual(sum(self.memory_counts().values()), 0)
        logs = " ".join(
            str(call.args[0])
            for call in printed.call_args_list
            if call.args
        )
        self.assertIn(
            "[memory-formation-shadow] status=failed category=source_unavailable",
            logs,
        )
        self.assertNotIn("[memory-candidate-persistence]", logs)

    async def test_persistence_telemetry_is_bounded_and_data_free(self):
        result = SimpleNamespace(
            created_count=99,
            existing_candidate_count=-5,
            active_duplicate_count=2,
            suppressed_count=7,
            replayed=True,
            source_text="PRIVATE SOURCE",
            memory_key="PRIVATE KEY",
        )
        with mock.patch("builtins.print") as printed:
            self.module._log_memory_candidate_persistence(
                status="completed",
                result=result,
            )
            self.module._log_memory_candidate_persistence(
                status="failed",
                category="PRIVATE RAW ERROR",
            )
        logs = tuple(
            str(call.args[0])
            for call in printed.call_args_list
            if call.args
        )
        self.assertEqual(logs, (
            "[memory-candidate-persistence] status=completed "
            "created=3 existing=0 active_duplicate=2 suppressed=3 replayed=1",
            "[memory-candidate-persistence] "
            "status=failed category=candidate_persistence_failed",
        ))
        self.assertNotIn("PRIVATE", " ".join(logs))

    async def test_enabled_missing_handle_logs_fixed_failure_without_fallback(self):
        self.module.MEMORY_CANDIDATE_PERSISTENCE = None
        self.module.MEMORY_CANDIDATE_PERSISTENCE_ERROR = (
            "memory_auto_candidate_persistence_unavailable"
        )
        proposal = AutoMemoryProposalV1(
            "durable_preference",
            0,
            len("I prefer tea."),
        )
        with mock.patch("builtins.print") as printed:
            await self.module._persist_accepted_memory_proposals(
                47,
                "I prefer tea.",
                (proposal,),
            )
        logs = tuple(
            str(call.args[0])
            for call in printed.call_args_list
            if call.args
        )
        self.assertEqual(logs, (
            "[memory-candidate-persistence] status=failed "
            "category=memory_auto_candidate_persistence_unavailable",
        ))
        self.assertEqual(sum(self.memory_counts().values()), 0)
        self.assertIsNone(self.module.MEMORY_EXPLICIT_ENTRY_SERVICES)


if __name__ == "__main__":
    unittest.main()
