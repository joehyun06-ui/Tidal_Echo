from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    channel_store,
    memory_candidate_review,
    memory_candidate_review_adapters,
    memory_policy,
)
from backend.tests._support import NoNetworkMixin


TEST_SECRET = "Synthetic-Candidate-HMAC-Key-2026-Alpha!Z9q7"
KEY_ID = "candidate-review-adapter-key"


class MemoryCandidateReviewAdapterTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "adapter.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute(channel_store.RELAY_TABLE_DDL["messages"])
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO memory_fingerprint_profile
                   (singleton,key_id,key_check,normalization_version,
                    fingerprint_version,created_at,updated_at)
                   VALUES(1,?,?,?,?,?,?)""",
                (
                    KEY_ID,
                    memory_policy.fingerprint_profile_check(TEST_SECRET),
                    memory_policy.NORMALIZATION_VERSION,
                    memory_policy.FINGERPRINT_VERSION,
                    stamp,
                    stamp,
                ),
            )
        reader = memory_candidate_review.MemoryCandidateReviewReader(
            self.path,
            fingerprint_key_id=KEY_ID,
            fingerprint_hmac_secret=TEST_SECRET,
            max_item_chars=1000,
        )
        self.service = memory_candidate_review.MemoryCandidateReviewService(
            reader,
            enabled=True,
            configuration_valid=True,
            error_category="",
        )

    def test_closed_binders_require_exact_service_and_fixed_origins(self):
        operator = memory_candidate_review_adapters.bind_operator_cli(
            self.service
        )
        mcp = memory_candidate_review_adapters.bind_mcp(self.service)
        self.assertIs(
            operator._service,
            self.service,
        )
        self.assertIs(mcp._service, self.service)
        self.assertEqual(operator._origin, "operator_cli")
        self.assertEqual(mcp._origin, "mcp")
        for binder in (
            memory_candidate_review_adapters.bind_operator_cli,
            memory_candidate_review_adapters.bind_mcp,
        ):
            with self.subTest(binder=binder.__name__), self.assertRaises(
                memory_candidate_review.MemoryCandidateReviewError
            ) as ctx:
                binder(object())
            self.assertEqual(
                ctx.exception.category,
                "candidate_review_configuration_invalid",
            )
        self.assertFalse(hasattr(memory_candidate_review_adapters, "bind"))
        self.assertFalse(hasattr(memory_candidate_review_adapters, "bind_web"))
        self.assertFalse(
            hasattr(memory_candidate_review_adapters, "bind_telegram")
        )
        self.assertFalse(hasattr(memory_candidate_review_adapters, "bind_operit"))
        self.assertNotIn(
            "origin",
            inspect.signature(
                memory_candidate_review_adapters.MemoryCandidateReviewAdapter
            ).parameters,
        )
        with self.assertRaises(
            memory_candidate_review.MemoryCandidateReviewError
        ):
            memory_candidate_review_adapters.MemoryCandidateReviewAdapter(
                self.service,
                _binding=object(),
            )

    def test_public_callable_surface_is_exact_and_repr_is_data_free(self):
        public = {
            name
            for name, value in inspect.getmembers(
                memory_candidate_review_adapters.MemoryCandidateReviewAdapter
            )
            if not name.startswith("_") and callable(value)
        }
        self.assertEqual(public, {"list_candidates", "get_candidate"})
        adapter = memory_candidate_review_adapters.bind_operator_cli(
            self.service
        )
        self.assertEqual(repr(adapter), "<MemoryCandidateReviewAdapter>")
        self.assertFalse(hasattr(adapter, "service"))
        for forbidden in (
            "remember", "correct", "forget", "approve", "accept",
            "confirm", "reject", "dismiss", "promote", "activate",
            "suppress", "update", "delete",
        ):
            self.assertFalse(hasattr(adapter, forbidden))

    def test_list_and_detail_pass_through_slice1_immutable_models(self):
        adapter = memory_candidate_review_adapters.bind_mcp(self.service)
        summary = memory_candidate_review.CandidateReviewSummaryV1(
            candidate_key="A" * 32,
            kind="project",
            content_preview="Project Atlas uses Python.",
            created_at="2026-01-01T00:00:00+00:00",
            provenance_count=1,
        )
        evidence = memory_candidate_review.CandidateReviewEvidenceV1(
            signal_type="project_fact",
            observed_at="2026-01-01T00:00:00+00:00",
            formation_contract_version="memory-formation-v1",
            extractor_contract_version="memory-formation-extractor-v1",
            source_excerpt="Project Atlas uses Python.",
        )
        detail = memory_candidate_review.CandidateReviewDetailV1(
            candidate_key="A" * 32,
            kind="project",
            content="Project Atlas uses Python.",
            scope_type="global_user",
            scope_ref="",
            sensitivity="normal",
            explicitness="inferred",
            confidence=0.0,
            created_at="2026-01-01T00:00:00+00:00",
            provenance_count=1,
            evidence=(evidence,),
        )
        with (
            mock.patch.object(
                memory_candidate_review.MemoryCandidateReviewService,
                "list_candidates",
                return_value=(summary,),
            ) as listed,
            mock.patch.object(
                memory_candidate_review.MemoryCandidateReviewService,
                "get_candidate",
                return_value=detail,
            ) as fetched,
        ):
            result = adapter.list_candidates(
                limit=7,
                after_candidate_key="B" * 32,
                kind="project",
            )
            fetched_result = adapter.get_candidate("A" * 32)
        self.assertIs(result[0], summary)
        self.assertIs(fetched_result, detail)
        listed.assert_called_once_with(
            limit=7,
            after_candidate_key="B" * 32,
            kind="project",
        )
        fetched.assert_called_once_with("A" * 32)

    def test_exception_boundary_is_closed_and_data_free(self):
        adapter = memory_candidate_review_adapters.bind_operator_cli(
            self.service
        )
        marker = "candidate plaintext and database path"
        with mock.patch.object(
            memory_candidate_review.MemoryCandidateReviewService,
            "list_candidates",
            side_effect=RuntimeError(marker),
        ), self.assertRaises(
            memory_candidate_review.MemoryCandidateReviewError
        ) as ctx:
            adapter.list_candidates()
        self.assertEqual(
            ctx.exception.category,
            "candidate_review_state_invalid",
        )
        self.assertNotIn(marker, str(ctx.exception))
        self.assertNotIn(marker, repr(ctx.exception))

        expected = memory_candidate_review.MemoryCandidateReviewError(
            "invalid_candidate_key"
        )
        with mock.patch.object(
            memory_candidate_review.MemoryCandidateReviewService,
            "get_candidate",
            side_effect=expected,
        ), self.assertRaises(
            memory_candidate_review.MemoryCandidateReviewError
        ) as propagated:
            adapter.get_candidate("bad/key")
        self.assertIs(propagated.exception, expected)

    def test_adapter_module_has_no_db_runtime_write_or_telemetry_code(self):
        source = inspect.getsource(memory_candidate_review_adapters)
        for forbidden in (
            "sqlite", "memory_runtime", "memory_store", "MemoryStore",
            "PrivilegedMemoryActions", "memory_explicit_actions",
            "memory_action_ledger", "FastAPI", "Telegram", "Operit",
            "INSERT ", "UPDATE ", "DELETE ", "print(", "logging",
            "__getattr__",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
