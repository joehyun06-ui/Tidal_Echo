from __future__ import annotations

import asyncio
import importlib
import json
import os
import tempfile
import unittest
from unittest import mock

from backend.tests._support import NoNetworkMixin, load_app


MEMORY_SECRET = "Synthetic-V2-Authority-HMAC-Key-2026!Z9q7"
AUTHORITY_GATE = "MEMORY_FORMATION_V2_AUTHORITY_ENABLED"


class MemoryFormationV2AuthorityTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def load(self, *, gate: str, full_lifecycle: bool = True):
        os.environ[AUTHORITY_GATE] = gate
        module = load_app(
            self.temp.name,
            telegram=False,
            kelivo=True,
            memory=True,
            memory_auto_formation=True,
            memory_natural_ingress_formation=True,
            memory_candidate_persistence=True,
            memory_candidate_review=full_lifecycle,
            memory_candidate_decisions=full_lifecycle,
            memory_secret=MEMORY_SECRET,
        )
        return module

    def reload_v2(self):
        names = (
            "backend.memory_formation_v2",
            "backend.memory_formation_extractor_v2",
            "backend.memory_candidate_persistence_v2",
            "backend.memory_candidate_integrity_v2",
            "backend.memory_candidate_review_v2",
            "backend.memory_candidate_decision_v2",
            "backend.memory_candidate_decision_adapters_v2",
            "backend.memory_formation_v2_loopback",
            "backend.memory_formation_v2_runtime_patch",
            "backend.memory_formation_v2_authority",
        )
        loaded = {}
        for name in names:
            loaded[name] = importlib.reload(importlib.import_module(name))
        return loaded["backend.memory_formation_v2_authority"]

    def seed_profile(self, module):
        source = "Project Seed uses Python."
        row = module.save_message(
            "in",
            "user",
            source,
            {"channel": "web", "source": "relay"},
        )
        proposal = module.memory_formation.AutoMemoryProposalV1(
            "project_fact", 0, len(source)
        )
        result = module.MEMORY_CANDIDATE_PERSISTENCE.persist(
            canonical_message_id=row["id"],
            source_text=source,
            proposals=(proposal,),
            formation_contract_version="memory-formation-v1",
            extractor_contract_version="memory-formation-extractor-v1",
        )
        self.assertEqual(result.outcome, "completed")

    def prepare_authority(self):
        module = self.load(gate="true", full_lifecycle=True)
        # Review intentionally fails closed until an authorized write has
        # initialized the fingerprint profile. Seed that existing production
        # invariant, then reinstall the V2 compositions and re-run startup.
        self.seed_profile(module)
        authority = self.reload_v2()
        self.assertTrue(authority.install(module))
        module.init_db()
        self.assertEqual(module.MEMORY_CANDIDATE_REVIEW_ERROR, "")
        self.assertIsNone(module.MEMORY_CANDIDATE_DECISION_ERROR)
        return module, authority

    def table_counts(self, module):
        tables = (
            "memory_items",
            "memory_candidate_sources",
            "memory_auto_formation_runs",
            "memory_candidate_decisions",
            "memory_suppressions",
        )
        with module.db() as conn:
            return {
                table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                for table in tables
            }

    async def test_gate_defaults_off_without_touching_existing_tasks(self):
        module = self.load(gate="false", full_lifecycle=False)
        authority = self.reload_v2()
        kelivo_before = module._run_memory_formation_shadow_task
        natural_before = module._run_natural_ingress_memory_formation_shadow_task
        self.assertFalse(authority.install(module))
        self.assertIs(module._run_memory_formation_shadow_task, kelivo_before)
        self.assertIs(
            module._run_natural_ingress_memory_formation_shadow_task,
            natural_before,
        )

    async def test_gate_is_strict_and_requires_full_candidate_lifecycle(self):
        module = self.load(gate="invalid", full_lifecycle=False)
        authority = self.reload_v2()
        with self.assertRaises(module.deployment_config.DeploymentConfigError) as invalid:
            authority.install(module)
        self.assertEqual(
            invalid.exception.category,
            "invalid_memory_formation_v2_authority_enabled",
        )

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        os.environ[AUTHORITY_GATE] = "true"
        module = load_app(
            other.name,
            telegram=False,
            kelivo=True,
            memory=True,
            memory_auto_formation=True,
            memory_natural_ingress_formation=True,
            memory_candidate_persistence=True,
            memory_candidate_review=False,
            memory_candidate_decisions=False,
            memory_secret=MEMORY_SECRET,
        )
        authority = self.reload_v2()
        with self.assertRaises(module.deployment_config.DeploymentConfigError) as relation:
            authority.install(module)
        self.assertEqual(
            relation.exception.category,
            "memory_formation_v2_authority_requires_candidate_lifecycle",
        )

    async def test_web_authority_persists_multispan_and_uses_v2_review_decision(self):
        module, authority = self.prepare_authority()
        formation_v2 = importlib.import_module("backend.memory_formation_v2")
        extractor_v2 = importlib.import_module("backend.memory_formation_extractor_v2")
        adapters_v2 = importlib.import_module("backend.memory_candidate_decision_adapters_v2")

        source = (
            "Project Atlas uses Python. filler. "
            "The project runs on Render."
        )
        first = "Project Atlas uses Python."
        second = "The project runs on Render."
        s1 = source.index(first)
        s2 = source.index(second)
        proposal = formation_v2.AutoMemoryProposalV2(
            "project_fact",
            (
                formation_v2.AutoMemorySourceSpanV2(s1, s1 + len(first)),
                formation_v2.AutoMemorySourceSpanV2(s2, s2 + len(second)),
            ),
        )
        extraction = extractor_v2.AutoMemoryExtractionV2((proposal,))
        message = module.save_message(
            "in",
            "user",
            source,
            {"channel": "web", "source": "relay"},
        )
        before = self.table_counts(module)
        v1_calls = []

        async def forbidden_v1(*args, **kwargs):
            v1_calls.append(1)
            raise AssertionError("V1 extractor must not run for Web authority")

        async def fake_loopback(**kwargs):
            self.assertEqual(kwargs["source_text"], source)
            return extraction

        with mock.patch.object(
            authority.memory_formation_v2_loopback,
            "extract_v2_via_loopback",
            new=fake_loopback,
        ), mock.patch("builtins.print") as printed:
            await module._run_natural_ingress_memory_formation_shadow_task(
                canonical_message_id=message["id"],
                channel="web",
                source="relay",
                generation_callable=forbidden_v1,
            )

        self.assertEqual(v1_calls, [])
        after = self.table_counts(module)
        self.assertEqual(after["memory_items"], before["memory_items"] + 1)
        self.assertEqual(
            after["memory_candidate_sources"],
            before["memory_candidate_sources"] + 2,
        )
        self.assertEqual(
            after["memory_auto_formation_runs"],
            before["memory_auto_formation_runs"] + 1,
        )
        with module.db() as conn:
            run = conn.execute(
                """SELECT formation_contract_version,extractor_contract_version,
                          proposal_count,candidate_count
                     FROM memory_auto_formation_runs
                    WHERE canonical_message_id=?""",
                (message["id"],),
            ).fetchone()
            item = conn.execute(
                """SELECT memory_key,status,normalized_content
                     FROM memory_items ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            sources = conn.execute(
                """SELECT span_start,span_end,formation_contract_version,
                          extractor_contract_version
                     FROM memory_candidate_sources
                    WHERE memory_id=(SELECT id FROM memory_items
                                      WHERE memory_key=?)
                    ORDER BY span_start""",
                (item["memory_key"],),
            ).fetchall()
            module.channel_store.validate_memory_candidate_persistence_schema(conn)
            module.channel_store.validate_memory_candidate_decision_schema_v1_v10(conn)
        self.assertEqual(
            tuple(run),
            ("memory-formation-v2", "memory-formation-extractor-v2", 1, 1),
        )
        self.assertEqual(item["status"], "candidate")
        self.assertIn("Project Atlas uses Python.", item["normalized_content"])
        self.assertIn("The project runs on Render.", item["normalized_content"])
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(
            row["formation_contract_version"] == "memory-formation-v2"
            and row["extractor_contract_version"] == "memory-formation-extractor-v2"
            for row in sources
        ))

        detail = module.MEMORY_CANDIDATE_REVIEW_OPERATOR.get_candidate(
            item["memory_key"]
        )
        self.assertEqual(detail.provenance_count, 2)
        request = adapters_v2.ApproveCandidateRequestV1(
            request_id="00000000000000000000000000000071",
            candidate_key=item["memory_key"],
        )
        approved = module.MEMORY_CANDIDATE_DECISION_OPERATOR.approve_candidate(request)
        self.assertEqual(approved.result_category, "approved")
        self.assertEqual(approved.resulting_status, "active")
        self.assertFalse(approved.replayed)
        snapshot = self.table_counts(module)
        replay = module.MEMORY_CANDIDATE_DECISION_OPERATOR.approve_candidate(request)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.table_counts(module), snapshot)

        logs = " ".join(
            str(call.args[0]) for call in printed.call_args_list if call.args
        )
        self.assertIn(
            "[memory-formation-v2-authority] status=completed proposals=1 "
            "candidates=1 created=1",
            logs,
        )
        self.assertNotIn("Project Atlas", logs)
        self.assertNotIn("Render", logs)

    async def test_authority_failure_never_falls_back_to_v1(self):
        module, authority = self.prepare_authority()
        source = "Project Atlas uses Python."
        message = module.save_message(
            "in", "user", source, {"channel": "web", "source": "relay"}
        )
        before = self.table_counts(module)
        v1_calls = []

        async def forbidden_v1(*args, **kwargs):
            v1_calls.append(1)
            raise AssertionError("no V1 fallback")

        async def fail_loopback(**kwargs):
            raise authority.memory_formation_v2_loopback.MemoryFormationV2LoopbackError(
                "extractor_timeout"
            )

        with mock.patch.object(
            authority.memory_formation_v2_loopback,
            "extract_v2_via_loopback",
            new=fail_loopback,
        ):
            await module._run_natural_ingress_memory_formation_shadow_task(
                canonical_message_id=message["id"],
                channel="web",
                source="relay",
                generation_callable=forbidden_v1,
            )
        self.assertEqual(v1_calls, [])
        self.assertEqual(self.table_counts(module), before)

    async def test_telegram_remains_v1_until_its_main_forward_barrier_exists(self):
        module, authority = self.prepare_authority()
        source = "Project Atlas uses Python."
        message = module.save_message(
            "in",
            "user",
            source,
            {"channel": "telegram", "source": "telegram"},
        )
        calls = []

        async def v1_generate(messages, session, model, temperature, max_tokens, context):
            calls.append(session)
            if session != "memory-formation-extractor-v1":
                raise AssertionError("Telegram partition must remain V1")
            return {
                "text": json.dumps({
                    "version": "memory-formation-extractor-v1",
                    "proposals": [{
                        "signal_type": "project_fact",
                        "start": 0,
                        "end": len(source),
                    }],
                }, separators=(",", ":"))
            }

        async def forbidden_loopback(**kwargs):
            raise AssertionError("Telegram must not use V2 loopback yet")

        with mock.patch.object(
            authority.memory_formation_v2_loopback,
            "extract_v2_via_loopback",
            new=forbidden_loopback,
        ):
            await module._run_natural_ingress_memory_formation_shadow_task(
                canonical_message_id=message["id"],
                channel="telegram",
                source="telegram",
                generation_callable=v1_generate,
            )
        self.assertEqual(calls, ["memory-formation-extractor-v1"])
        with module.db() as conn:
            run = conn.execute(
                """SELECT formation_contract_version,extractor_contract_version
                     FROM memory_auto_formation_runs
                    WHERE canonical_message_id=?""",
                (message["id"],),
            ).fetchone()
        self.assertEqual(
            tuple(run),
            ("memory-formation-v1", "memory-formation-extractor-v1"),
        )


if __name__ == "__main__":
    unittest.main()
