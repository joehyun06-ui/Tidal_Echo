from __future__ import annotations

import dataclasses
import hashlib
import importlib
import inspect
import json
import re
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import (
    channel_store,
    deployment_config,
    memory_formation,
    memory_policy,
    memory_runtime,
    memory_service,
    memory_store,
)
from backend.tests._support import NoNetworkMixin


TEST_SECRET = "Synthetic-Candidate-HMAC-Key-2026-Alpha!Z9q7"
OTHER_SECRET = "Other-Synthetic-HMAC-Key-2026-Beta!Q8w6"
FORMATION_VERSION = "memory-formation-v1"
EXTRACTOR_VERSION = "memory-formation-extractor-v1"


def candidate_config(
    *,
    enabled: bool = True,
    writes: bool = False,
    persistence: bool = True,
    secret: str = TEST_SECRET,
) -> deployment_config.MemoryConfig:
    return deployment_config.MemoryConfig(
        enabled=enabled,
        context_injection_enabled=False,
        smart_retrieval_enabled=False,
        explicit_writes_enabled=writes,
        sensitive_storage_enabled=False,
        max_item_chars=1000,
        forget_retention_policy="tombstone_without_content",
        fingerprint_key_id="candidate-persistence-test-key",
        fingerprint_hmac_secret=secret,
        configuration_valid=True,
        error_category="",
        auto_formation_enabled=True,
        auto_candidate_persistence_enabled=persistence,
    )


def bootstrap(path: str, config: deployment_config.MemoryConfig):
    global channel_store, memory_formation, memory_policy
    global memory_runtime, memory_service, memory_store
    memory_runtime = importlib.import_module("backend.memory_runtime")
    memory_runtime = importlib.reload(memory_runtime)
    channel_store = importlib.import_module("backend.channel_store")
    memory_formation = importlib.import_module("backend.memory_formation")
    memory_policy = importlib.import_module("backend.memory_policy")
    memory_store = importlib.import_module("backend.memory_store")
    memory_service = importlib.import_module("backend.memory_service")
    deployment = dataclasses.replace(
        deployment_config.load_deployment_config(
            SimpleNamespace(requested=False, enabled=False),
            {
                "TELEGRAM_ENABLED": "false",
                "RELAY_DB": path,
            },
        ),
        memory=config,
    )
    with mock.patch.object(
        deployment_config,
        "load_deployment_config",
        return_value=deployment,
    ):
        return memory_runtime.bootstrap_memory_runtime_from_environment(object())


class MemoryCandidatePersistenceTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "candidate.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """CREATE TABLE messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                    text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')"""
            )
        channel_store.run_migrations(self.path)
        self.runtime = bootstrap(self.path, candidate_config())
        self.persistence = self.runtime.candidate_persistence
        self.store = self.persistence._store

    def message(
        self,
        text: str,
        *,
        direction: str = "in",
        kind: str = "user",
    ) -> int:
        with channel_store.connect(self.path) as conn:
            cursor = conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,?,?,?,?)""",
                (
                    channel_store.now_iso(),
                    direction,
                    kind,
                    text,
                    json.dumps(
                        {"channel": "web", "source": "relay"},
                        separators=(",", ":"),
                    ),
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def proposal(
        text: str,
        signal_type: str = "durable_preference",
        *,
        source: str | None = None,
    ) -> memory_formation.AutoMemoryProposalV1:
        source = text if source is None else source
        start = source.index(text)
        return memory_formation.AutoMemoryProposalV1(
            signal_type,
            start,
            start + len(text),
        )

    def persist(
        self,
        source_text: str,
        proposals=None,
        *,
        message_id: int | None = None,
        formation_version: str = FORMATION_VERSION,
        extractor_version: str = EXTRACTOR_VERSION,
        persistence=None,
    ):
        message_id = message_id or self.message(source_text)
        if proposals is None:
            proposals = (self.proposal(source_text),)
        return (persistence or self.persistence).persist(
            canonical_message_id=message_id,
            source_text=source_text,
            proposals=proposals,
            formation_contract_version=formation_version,
            extractor_contract_version=extractor_version,
        )

    def counts(self) -> dict[str, int]:
        tables = (
            "memory_items",
            "memory_fingerprint_profile",
            "memory_sources",
            "memory_suppressions",
            "memory_evidence_events",
            "memory_action_requests",
            "memory_candidate_sources",
            "memory_auto_formation_runs",
        )
        with channel_store.connect(self.path) as conn:
            return {
                table: int(conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0])
                for table in tables
            }

    def fingerprint(self, *, kind: str, content: str) -> bytes:
        return memory_policy.fingerprint_content(
            TEST_SECRET,
            scope_type="global_user",
            scope_ref="",
            kind=kind,
            normalized_content=content,
        )

    def initialize_profile(self) -> None:
        self.persist(
            "Canonical user message with no proposals.",
            (),
        )

    def insert_live(
        self,
        *,
        kind: str,
        content: str,
        status: str,
        fingerprint: bytes | None = None,
        key_seed: str = "A",
    ) -> int:
        stamp = channel_store.now_iso()
        fingerprint = fingerprint or self.fingerprint(kind=kind, content=content)
        with channel_store.connect(self.path) as conn:
            cursor = conn.execute(
                """INSERT INTO memory_items
                   (memory_key,kind,scope_type,scope_ref,normalized_content,
                    normalized_fingerprint,fingerprint_version,status,
                    explicitness,confidence,sensitivity,first_observed_at,
                    last_confirmed_at,superseded_by_id,created_at,updated_at)
                   VALUES(?,?, 'global_user','',?,?,?, ?,?,?, 'normal',?,?,NULL,?,?)""",
                (
                    key_seed * 32,
                    kind,
                    content,
                    fingerprint,
                    memory_policy.FINGERPRINT_VERSION,
                    status,
                    "explicit" if status == "active" else "inferred",
                    1.0 if status == "active" else 0.0,
                    stamp,
                    stamp,
                    stamp,
                    stamp,
                ),
            )
            return int(cursor.lastrowid)

    def insert_suppression(self, *, kind: str, content: str) -> int:
        with channel_store.connect(self.path) as conn:
            cursor = conn.execute(
                """INSERT INTO memory_suppressions
                   (scope_type,scope_ref,kind,normalized_fingerprint,
                    fingerprint_version,reason_category,created_at)
                   VALUES('global_user','',?,?,?,'user_reject',?)""",
                (
                    kind,
                    self.fingerprint(kind=kind, content=content),
                    memory_policy.FINGERPRINT_VERSION,
                    channel_store.now_iso(),
                ),
            )
            return int(cursor.lastrowid)

    def assert_service_error(self, category: str, callable_, *args, **kwargs):
        with self.assertRaises(memory_service.MemoryServiceError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.category, category)
        self.assertEqual(str(raised.exception), category)
        return raised.exception

    def test_automatic_authority_is_independent_from_explicit_actions(self):
        text = "Project Atlas uses Python."
        message_id = self.message(text)
        with mock.patch.object(
            memory_runtime,
            "issue_action_envelope",
            wraps=memory_runtime.issue_action_envelope,
        ) as issue:
            result = self.persist(
                text,
                (self.proposal(text, "project_fact"),),
                message_id=message_id,
            )
        self.assertEqual(result.created_count, 1)
        issue.assert_not_called()
        self.assertFalse(
            any("candidate" in action for action in memory_runtime.ACTION_TYPES)
        )
        self.assert_service_error(
            "explicit_writes_disabled",
            self.runtime.privileged_actions.remember_explicit_user_message,
            kind="project",
            scope_type="global_user",
            scope_ref="",
            content=text,
            sensitivity="normal",
            canonical_message_id=self.message("Explicit attempt."),
        )

    def test_disabled_candidate_flag_is_data_free_and_zero_write(self):
        disabled_runtime = bootstrap(
            self.path,
            candidate_config(writes=True, persistence=False),
        )
        before = self.counts()
        text = "I usually prefer window seats."
        message_id = self.message(text)
        self.assert_service_error(
            "auto_candidate_persistence_disabled",
            disabled_runtime.candidate_persistence.persist,
            canonical_message_id=message_id,
            source_text=text,
            proposals=(self.proposal(text),),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        self.assertEqual(self.counts(), before)

    def test_disabled_memory_core_blocks_candidate_persistence_with_zero_write(self):
        disabled_runtime = bootstrap(
            self.path,
            candidate_config(enabled=False, persistence=True),
        )
        before = self.counts()
        text = "Project Atlas uses Python."
        self.assert_service_error(
            "feature_disabled",
            disabled_runtime.candidate_persistence.persist,
            canonical_message_id=self.message(text),
            source_text=text,
            proposals=(self.proposal(text, "project_fact"),),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        self.assertEqual(self.counts(), before)

    def test_runtime_profile_version_mismatch_fails_before_writes(self):
        before = self.counts()
        text = "I usually prefer window seats."
        message_id = self.message(text)
        with mock.patch.object(
            memory_policy,
            "NORMALIZATION_VERSION",
            memory_policy.NORMALIZATION_VERSION + 1,
        ):
            self.assert_service_error(
                "memory_configuration_invalid",
                self.persistence.persist,
                canonical_message_id=message_id,
                source_text=text,
                proposals=(self.proposal(text),),
                formation_contract_version=FORMATION_VERSION,
                extractor_contract_version=EXTRACTOR_VERSION,
            )
        self.assertEqual(self.counts(), before)

    def test_api_shape_accepts_only_source_proposals_and_contracts(self):
        parameters = tuple(
            inspect.signature(type(self.persistence).persist).parameters
        )
        self.assertEqual(
            parameters,
            (
                "self",
                "canonical_message_id",
                "source_text",
                "proposals",
                "formation_contract_version",
                "extractor_contract_version",
            ),
        )
        forbidden = {
            "candidate",
            "content",
            "normalized_content",
            "kind",
            "scope",
            "sensitivity",
            "confidence",
            "fingerprint",
            "memory_key",
        }
        self.assertTrue(forbidden.isdisjoint(parameters))
        text = "I usually prefer window seats."
        with self.assertRaises(TypeError):
            self.persistence.persist(
                canonical_message_id=self.message(text),
                source_text=text,
                proposals=(self.proposal(text),),
                formation_contract_version=FORMATION_VERSION,
                extractor_contract_version=EXTRACTOR_VERSION,
                content="forged candidate plaintext",
            )
        message_id = self.message(text)
        candidate = memory_formation.build_auto_memory_candidates(
            message_id,
            text,
            (self.proposal(text),),
        )[0]
        before = self.counts()
        self.assert_service_error(
            "invalid_proposal",
            self.persistence.persist,
            canonical_message_id=message_id,
            source_text=text,
            proposals=(candidate,),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        self.assertEqual(self.counts(), before)

    def test_canonical_source_must_exist_be_user_and_match_exact_text(self):
        text = "I usually prefer window seats."
        proposal = (self.proposal(text),)
        before = self.counts()
        valid_id = self.message(text)
        cases = (
            (valid_id, "I usually prefer aisle seats.", proposal),
            (999999, text, proposal),
            (
                self.message(text, direction="out", kind="reply"),
                text,
                proposal,
            ),
            (
                self.message(text, direction="in", kind="voice"),
                text,
                proposal,
            ),
        )
        for message_id, source_text, proposals in cases:
            with self.subTest(message_id=message_id, source_text=source_text):
                self.assert_service_error(
                    "invalid_canonical_source",
                    self.persistence.persist,
                    canonical_message_id=message_id,
                    source_text=source_text,
                    proposals=proposals,
                    formation_contract_version=FORMATION_VERSION,
                    extractor_contract_version=EXTRACTOR_VERSION,
                )
                self.assertEqual(self.counts(), before)

    def test_duck_typed_proposal_fails_through_phase4a_with_zero_writes(self):
        class DuckProposal:
            signal_type = "durable_preference"
            start = 0
            end = 31

        text = "I usually prefer window seats."
        before = self.counts()
        self.assert_service_error(
            "invalid_proposal",
            self.persistence.persist,
            canonical_message_id=self.message(text),
            source_text=text,
            proposals=(DuckProposal(),),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        self.assertEqual(self.counts(), before)

    def test_builder_candidate_to_proposal_mismatch_rolls_back(self):
        text = "Project Atlas uses Python."
        message_id = self.message(text)
        proposal = self.proposal(text, "project_fact")
        valid = memory_formation.build_auto_memory_candidates(
            message_id,
            text,
            (proposal,),
        )[0]
        forged = dataclasses.replace(valid, signal_type="stable_profile")
        before = self.counts()
        with mock.patch.object(
            memory_formation,
            "build_auto_memory_candidates",
            return_value=(forged,),
        ):
            self.assert_service_error(
                "candidate_state_conflict",
                self.persistence.persist,
                canonical_message_id=message_id,
                source_text=text,
                proposals=(proposal,),
                formation_contract_version=FORMATION_VERSION,
                extractor_contract_version=EXTRACTOR_VERSION,
            )
        self.assertEqual(self.counts(), before)

    def test_new_candidate_row_and_provenance_have_exact_fixed_shape(self):
        text = "Project Atlas uses Python."
        message_id = self.message(text)
        result = self.persist(
            text,
            (self.proposal(text, "project_fact"),),
            message_id=message_id,
        )
        self.assertEqual(
            dataclasses.asdict(result),
            {
                "outcome": "completed",
                "proposal_count": 1,
                "candidate_count": 1,
                "created_count": 1,
                "existing_candidate_count": 0,
                "active_duplicate_count": 0,
                "suppressed_count": 0,
                "replayed": False,
            },
        )
        self.assertEqual(repr(result), "<AutoCandidatePersistenceResult>")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.created_count = 2
        with channel_store.connect(self.path) as conn:
            row = conn.execute("SELECT * FROM memory_items").fetchone()
            source = conn.execute(
                "SELECT * FROM memory_candidate_sources"
            ).fetchone()
            run = conn.execute(
                "SELECT * FROM memory_auto_formation_runs"
            ).fetchone()
            active_count = conn.execute(
                "SELECT count(*) FROM memory_items WHERE status='active'"
            ).fetchone()[0]
            explicit_counts = tuple(conn.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0] for table in (
                "memory_sources",
                "memory_evidence_events",
                "memory_action_requests",
            ))
        self.assertEqual(row["status"], "candidate")
        self.assertEqual(row["explicitness"], "inferred")
        self.assertEqual(row["confidence"], 0.0)
        self.assertEqual(row["scope_type"], "global_user")
        self.assertEqual(row["scope_ref"], "")
        self.assertEqual(row["sensitivity"], "normal")
        self.assertEqual(row["kind"], "project")
        self.assertEqual(row["normalized_content"], text)
        self.assertRegex(row["memory_key"], r"\A[A-Za-z0-9_-]{32,96}\Z")
        self.assertEqual(active_count, 0)
        self.assertEqual(explicit_counts, (0, 0, 0))
        self.assertEqual(source["memory_id"], row["id"])
        self.assertEqual(source["canonical_message_id"], message_id)
        self.assertEqual(source["signal_type"], "project_fact")
        self.assertEqual((source["span_start"], source["span_end"]), (0, len(text)))
        self.assertEqual(source["formation_contract_version"], FORMATION_VERSION)
        self.assertEqual(source["extractor_contract_version"], EXTRACTOR_VERSION)
        self.assertRegex(run["proposal_digest"], r"\A[0-9a-f]{64}\Z")
        self.assertNotIn(text, run["proposal_digest"])
        self.assertTrue(self.store.validate_schema())

    def test_zero_and_three_candidate_batches_have_exact_counts_and_order(self):
        zero = self.persist("No proposals in this canonical message.", ())
        self.assertEqual((zero.proposal_count, zero.candidate_count), (0, 0))
        self.assertEqual(zero.created_count, 0)
        first = "I usually prefer window seats."
        second = "I work as a product designer."
        third = "Project Atlas uses Python."
        source_text = f"{first} {second} {third}"
        proposals = (
            self.proposal(third, "project_fact", source=source_text),
            self.proposal(first, "durable_preference", source=source_text),
            self.proposal(second, "stable_profile", source=source_text),
        )
        message_id = self.message(source_text)
        result = self.persist(
            source_text,
            proposals,
            message_id=message_id,
        )
        self.assertEqual(
            (
                result.proposal_count,
                result.candidate_count,
                result.created_count,
            ),
            (3, 3, 3),
        )
        with channel_store.connect(self.path) as conn:
            provenance = conn.execute(
                """SELECT signal_type,span_start,span_end
                   FROM memory_candidate_sources
                   WHERE canonical_message_id=? ORDER BY span_start""",
                (message_id,),
            ).fetchall()
        self.assertEqual(
            tuple(row["signal_type"] for row in provenance),
            ("durable_preference", "stable_profile", "project_fact"),
        )

    def test_suppression_wins_without_candidate_or_provenance(self):
        self.initialize_profile()
        text = "Project Atlas uses Python."
        suppression_id = self.insert_suppression(kind="project", content=text)
        with channel_store.connect(self.path) as conn:
            before = tuple(conn.execute(
                "SELECT * FROM memory_suppressions WHERE id=?",
                (suppression_id,),
            ).fetchone())
        result = self.persist(
            text,
            (self.proposal(text, "project_fact"),),
        )
        self.assertEqual(result.suppressed_count, 1)
        self.assertEqual(result.created_count, 0)
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM memory_items"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM memory_candidate_sources"
            ).fetchone()[0], 0)
            after = tuple(conn.execute(
                "SELECT * FROM memory_suppressions WHERE id=?",
                (suppression_id,),
            ).fetchone())
        self.assertEqual(after, before)

    def test_active_duplicate_is_unchanged_and_receives_no_provenance(self):
        self.initialize_profile()
        text = "I work as a product designer."
        memory_id = self.insert_live(
            kind="user_profile",
            content=text,
            status="active",
        )
        with channel_store.connect(self.path) as conn:
            before = tuple(conn.execute(
                "SELECT * FROM memory_items WHERE id=?", (memory_id,)
            ).fetchone())
        result = self.persist(
            text,
            (self.proposal(text, "stable_profile"),),
        )
        self.assertEqual(result.active_duplicate_count, 1)
        self.assertEqual(result.created_count, 0)
        with channel_store.connect(self.path) as conn:
            after = tuple(conn.execute(
                "SELECT * FROM memory_items WHERE id=?", (memory_id,)
            ).fetchone())
            provenance_count = conn.execute(
                "SELECT count(*) FROM memory_candidate_sources WHERE memory_id=?",
                (memory_id,),
            ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(provenance_count, 0)

    def test_existing_candidate_is_unchanged_and_gets_one_new_provenance(self):
        text = "I usually prefer window seats."
        first = self.persist(text)
        self.assertEqual(first.created_count, 1)
        with channel_store.connect(self.path) as conn:
            row = conn.execute("SELECT * FROM memory_items").fetchone()
            memory_id = int(row["id"])
            before = tuple(row)
        second = self.persist(text)
        self.assertEqual(second.existing_candidate_count, 1)
        self.assertEqual(second.created_count, 0)
        with channel_store.connect(self.path) as conn:
            after = tuple(conn.execute(
                "SELECT * FROM memory_items WHERE id=?", (memory_id,)
            ).fetchone())
            sources = conn.execute(
                """SELECT canonical_message_id FROM memory_candidate_sources
                   WHERE memory_id=? ORDER BY id""",
                (memory_id,),
            ).fetchall()
        self.assertEqual(after, before)
        self.assertEqual(len(sources), 2)
        self.assertNotEqual(sources[0][0], sources[1][0])

    def test_mixed_batch_uses_fixed_suppression_active_candidate_order(self):
        preference = "I usually prefer window seats."
        profile = "I work as a product designer."
        project = "Project Atlas uses Python."
        self.persist(preference)
        self.insert_live(
            kind="user_profile",
            content=profile,
            status="active",
            key_seed="B",
        )
        self.insert_suppression(kind="project", content=project)
        source_text = f"{preference} {profile} {project}"
        result = self.persist(
            source_text,
            (
                self.proposal(project, "project_fact", source=source_text),
                self.proposal(profile, "stable_profile", source=source_text),
                self.proposal(
                    preference,
                    "durable_preference",
                    source=source_text,
                ),
            ),
        )
        self.assertEqual(
            (
                result.created_count,
                result.existing_candidate_count,
                result.active_duplicate_count,
                result.suppressed_count,
            ),
            (0, 1, 1, 1),
        )

    def test_same_digest_and_versions_replay_without_new_rows(self):
        text = "Project Atlas uses Python."
        message_id = self.message(text)
        proposals = (self.proposal(text, "project_fact"),)
        first = self.persist(text, proposals, message_id=message_id)
        before = self.counts()
        second = self.persist(text, proposals, message_id=message_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(
            first,
            dataclasses.replace(second, replayed=False),
        )
        self.assertEqual(self.counts(), before)

    def test_digest_and_contract_replay_conflicts_are_zero_change(self):
        first = "I usually prefer window seats."
        second = "I work as a product designer."
        source_text = f"{first} {second}"
        message_id = self.message(source_text)
        first_proposal = self.proposal(
            first, "durable_preference", source=source_text
        )
        second_proposal = self.proposal(
            second, "stable_profile", source=source_text
        )
        self.persist(source_text, (first_proposal,), message_id=message_id)
        before = self.counts()
        self.assert_service_error(
            "formation_replay_conflict",
            self.persistence.persist,
            canonical_message_id=message_id,
            source_text=source_text,
            proposals=(second_proposal,),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        self.assertEqual(self.counts(), before)
        self.assert_service_error(
            "formation_replay_conflict",
            self.persistence.persist,
            canonical_message_id=message_id,
            source_text=source_text,
            proposals=(first_proposal,),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version="memory-formation-extractor-v2",
        )
        self.assertEqual(self.counts(), before)
        self.assert_service_error(
            "formation_replay_conflict",
            self.persistence.persist,
            canonical_message_id=message_id,
            source_text=source_text,
            proposals=(first_proposal,),
            formation_contract_version="memory-formation-v2",
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        self.assertEqual(self.counts(), before)

    def test_proposal_digest_is_canonical_and_excludes_plaintext_and_ids(self):
        first = "I usually prefer window seats."
        second = "Project Atlas uses Python."
        source_text = f"{first} {second}"
        proposals = (
            self.proposal(second, "project_fact", source=source_text),
            self.proposal(first, "durable_preference", source=source_text),
        )
        message_id = self.message(source_text)
        self.persist(source_text, proposals, message_id=message_id)
        other_message_id = self.message(source_text)
        self.persist(source_text, proposals, message_id=other_message_id)
        sorted_proposals = sorted(
            proposals,
            key=lambda item: (item.start, item.end, item.signal_type),
        )
        payload = [
            {
                "end": item.end,
                "signal_type": item.signal_type,
                "start": item.start,
            }
            for item in sorted_proposals
        ]
        expected = hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")).hexdigest()
        with channel_store.connect(self.path) as conn:
            digests = tuple(row[0] for row in conn.execute(
                """SELECT proposal_digest FROM memory_auto_formation_runs
                   WHERE canonical_message_id IN (?,?)
                   ORDER BY canonical_message_id""",
                (message_id, other_message_id),
            ).fetchall())
        self.assertEqual(digests, (expected, expected))
        self.assertNotIn(first, expected)
        self.assertNotIn(second, expected)

    def test_concurrent_same_request_has_one_run_and_deterministic_replay(self):
        text = "Project Atlas uses Python."
        message_id = self.message(text)
        proposals = (self.proposal(text, "project_fact"),)

        def execute():
            return self.persistence.persist(
                canonical_message_id=message_id,
                source_text=text,
                proposals=proposals,
                formation_contract_version=FORMATION_VERSION,
                extractor_contract_version=EXTRACTOR_VERSION,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _value: execute(), range(2)))
        self.assertEqual(sorted(result.replayed for result in results), [False, True])
        self.assertEqual(
            dataclasses.replace(results[0], replayed=False),
            dataclasses.replace(results[1], replayed=False),
        )
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM memory_auto_formation_runs"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM memory_items WHERE status='candidate'"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM memory_candidate_sources"
            ).fetchone()[0], 1)

    def test_failure_after_candidate_sources_rolls_back_whole_batch(self):
        first = "I usually prefer window seats."
        second = "Project Atlas uses Python."
        source_text = f"{first} {second}"
        proposals = (
            self.proposal(first, "durable_preference", source=source_text),
            self.proposal(second, "project_fact", source=source_text),
        )
        observed_inside: list[tuple[int, int]] = []

        def fail(conn):
            observed_inside.append((
                int(conn.execute(
                    "SELECT count(*) FROM memory_items"
                ).fetchone()[0]),
                int(conn.execute(
                    "SELECT count(*) FROM memory_candidate_sources"
                ).fetchone()[0]),
            ))
            raise sqlite3.OperationalError("private rollback marker")

        before = self.counts()
        with mock.patch.object(
            self.store,
            "_before_auto_formation_run_insert",
            side_effect=fail,
        ):
            error = self.assert_service_error(
                "storage_unavailable",
                self.persist,
                source_text,
                proposals,
            )
        self.assertEqual(observed_inside, [(2, 2)])
        self.assertEqual(self.counts(), before)
        self.assertNotIn("private rollback marker", repr(error))

    def test_fingerprint_content_mismatch_rolls_back_as_impossible_state(self):
        self.initialize_profile()
        expected = "Project Atlas uses Python."
        self.insert_live(
            kind="project",
            content="Different stored project content.",
            status="active",
            fingerprint=self.fingerprint(kind="project", content=expected),
        )
        before = self.counts()
        self.assert_service_error(
            "candidate_state_conflict",
            self.persist,
            expected,
            (self.proposal(expected, "project_fact"),),
        )
        self.assertEqual(self.counts(), before)

    def test_v9_run_only_state_blocks_profile_reinitialization(self):
        self.persist("Canonical zero-proposal run.", ())
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM memory_items"
            ).fetchone()[0], 0)
            conn.execute("DELETE FROM memory_fingerprint_profile")
        other = bootstrap(self.path, candidate_config(secret=OTHER_SECRET))
        before = self.counts()
        self.assert_service_error(
            "memory_fingerprint_profile_mismatch",
            other.candidate_persistence.persist,
            canonical_message_id=self.message("Another canonical run."),
            source_text="Another canonical run.",
            proposals=(),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        self.assertEqual(self.counts(), before)

    def test_candidate_source_state_is_profile_bound(self):
        self.persist("Project Atlas uses Python.", (
            self.proposal("Project Atlas uses Python.", "project_fact"),
        ))
        self.assertIn("memory_candidate_sources", memory_store._PROFILE_STATE_TABLES)
        self.assertIn("memory_auto_formation_runs", memory_store._PROFILE_STATE_TABLES)
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM memory_candidate_sources"
            ).fetchone()[0], 1)
            conn.execute("DELETE FROM memory_fingerprint_profile")
        other = bootstrap(self.path, candidate_config(secret=OTHER_SECRET))
        before = self.counts()
        text = "I work as a product designer."
        self.assert_service_error(
            "memory_fingerprint_profile_mismatch",
            other.candidate_persistence.persist,
            canonical_message_id=self.message(text),
            source_text=text,
            proposals=(self.proposal(text, "stable_profile"),),
            formation_contract_version=FORMATION_VERSION,
            extractor_contract_version=EXTRACTOR_VERSION,
        )
        self.assertEqual(self.counts(), before)

    def test_profile_mismatch_happens_before_candidate_write(self):
        self.persist("Canonical zero-proposal run.", ())
        with channel_store.connect(self.path) as conn:
            conn.execute(
                "UPDATE memory_fingerprint_profile SET key_id='wrong-profile'"
            )
        before = self.counts()
        text = "Project Atlas uses Python."
        self.assert_service_error(
            "memory_fingerprint_profile_mismatch",
            self.persist,
            text,
            (self.proposal(text, "project_fact"),),
        )
        self.assertEqual(self.counts(), before)

    def test_active_retrieval_still_excludes_persisted_candidate(self):
        marker = "Project Atlas uses Python."
        self.persist(marker, (self.proposal(marker, "project_fact"),))
        result = self.runtime.read_service.get_active_memories(
            scope_type="global_user",
            scope_ref="",
        )
        self.assertEqual(result, [])
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(marker, serialized)
        with channel_store.connect(self.path) as conn:
            key = conn.execute(
                "SELECT memory_key FROM memory_items WHERE status='candidate'"
            ).fetchone()[0]
        self.assertNotIn(key, serialized)


if __name__ == "__main__":
    unittest.main()
