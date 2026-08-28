from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend import codex_generation_store as store


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CodexGenerationStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "codex-generation.db"
        store.initialize(self.path)

    def pin(self, session: str = "api-canary") -> dict:
        return store.pin_session(
            self.path,
            api_session=session,
            model="gpt-5.6-sol",
            model_provider="openai",
            reasoning_effort="high",
            persona_hash=digest("persona"),
        )

    def enqueue(self, *, session: str = "api-canary", message_id: int = 41) -> dict:
        return store.enqueue_job(
            self.path,
            api_session=session,
            canonical_message_id=message_id,
            input_digest=digest(f"message-{message_id}"),
            generation_id=f"codex-gen-{message_id}",
            client_message_id=f"codex-client-{message_id}",
            callback_identity=f"codex-callback-{message_id}",
        )

    def test_schema_is_standalone_and_exact(self):
        with closing(store.connect(self.path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertEqual(
                tables,
                {"codex_generation_schema", "codex_sessions", "codex_generation_jobs"},
            )
            self.assertEqual(
                conn.execute("SELECT version FROM codex_generation_schema").fetchone()[0],
                1,
            )

    def test_pin_is_idempotent_but_contract_changes_fail_closed(self):
        first = self.pin()
        second = self.pin()
        self.assertEqual(first["api_session"], second["api_session"])
        with self.assertRaisesRegex(store.CodexGenerationStoreError, "session_conflict"):
            store.pin_session(
                self.path,
                api_session="api-canary",
                model="other-model",
                model_provider="openai",
                reasoning_effort="high",
                persona_hash=digest("persona"),
            )

    def test_enqueue_requires_explicit_active_pin_and_stores_no_plaintext(self):
        with self.assertRaisesRegex(store.CodexGenerationStoreError, "session_not_pinned"):
            self.enqueue()
        self.pin()
        job = self.enqueue()
        self.assertEqual(job["status"], "queued")
        with closing(store.connect(self.path)) as conn:
            schema = " ".join(
                row["sql"] or ""
                for row in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name IN ('codex_sessions','codex_generation_jobs')"
                )
            )
        self.assertNotIn("payload_text", schema)
        self.assertNotIn("response_text", schema)

    def test_enqueue_is_idempotent_only_for_same_canonical_binding(self):
        self.pin()
        first = self.enqueue()
        second = self.enqueue()
        self.assertEqual(first["id"], second["id"])
        with self.assertRaisesRegex(store.CodexGenerationStoreError, "job_conflict"):
            store.enqueue_job(
                self.path,
                api_session="api-canary",
                canonical_message_id=41,
                input_digest=digest("different"),
                generation_id="codex-gen-other",
                client_message_id="codex-client-other",
                callback_identity="codex-callback-other",
            )

    def test_claim_retries_only_safe_pre_dispatch_states(self):
        self.pin()
        job = self.enqueue()
        claimed = store.claim_next_job(self.path, lease_seconds=1)
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["status"], "processing")
        self.assertEqual(claimed["attempt_count"], 1)

        with closing(store.connect(self.path)) as conn:
            conn.execute(
                "UPDATE codex_generation_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                (job["id"],),
            )
        reclaimed = store.claim_next_job(self.path, lease_seconds=1)
        self.assertEqual(reclaimed["id"], job["id"])
        self.assertEqual(reclaimed["attempt_count"], 2)

    def test_thread_dispatch_state_is_never_claimed_as_safe_retry(self):
        self.pin()
        job = self.enqueue()
        job = store.claim_next_job(self.path)
        job = store.begin_thread_dispatch(
            self.path,
            job_id=job["id"],
            thread_attempt_id="attempt-1",
            cwd=str(Path(self.temp.name) / "workspace" / "attempt-1"),
        )
        with closing(store.connect(self.path)) as conn:
            conn.execute(
                "UPDATE codex_generation_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                (job["id"],),
            )
        self.assertIsNone(store.claim_next_job(self.path))
        recovery = store.claim_recovery_job(self.path, lease_seconds=1)
        self.assertEqual(recovery["id"], job["id"])
        self.assertEqual(recovery["status"], "thread_dispatching")

    def test_thread_result_binds_session_and_job_atomically(self):
        self.pin()
        job = self.enqueue()
        job = store.claim_next_job(self.path)
        cwd = str(Path(self.temp.name) / "workspace" / "attempt-1")
        store.begin_thread_dispatch(
            self.path, job_id=job["id"], thread_attempt_id="attempt-1", cwd=cwd
        )
        bound = store.bind_session_thread(
            self.path,
            job_id=job["id"],
            thread_attempt_id="attempt-1",
            thread_id="thr-1",
            cwd=cwd,
        )
        session = store.get_session(self.path, "api-canary")
        self.assertEqual(bound["status"], "processing")
        self.assertEqual(bound["thread_id"], "thr-1")
        self.assertEqual(session["thread_id"], "thr-1")
        self.assertEqual(session["thread_attempt_id"], "attempt-1")
        self.assertEqual(session["cwd"], cwd)

    def test_empty_thread_attempt_can_be_abandoned_without_touching_session(self):
        self.pin()
        job = store.claim_next_job(self.path) if self.enqueue() else None
        cwd = str(Path(self.temp.name) / "workspace" / "attempt-1")
        store.begin_thread_dispatch(
            self.path, job_id=job["id"], thread_attempt_id="attempt-1", cwd=cwd
        )
        requeued = store.abandon_thread_attempt_and_requeue(self.path, job_id=job["id"])
        self.assertEqual(requeued["status"], "queued")
        self.assertIsNone(requeued["thread_attempt_id"])
        self.assertIsNone(store.get_session(self.path, "api-canary")["thread_id"])

    def test_turn_dispatch_requires_durable_session_thread(self):
        self.pin()
        job = self.enqueue()
        job = store.claim_next_job(self.path)
        with self.assertRaisesRegex(store.CodexGenerationStoreError, "thread_missing"):
            store.begin_turn_dispatch(self.path, job_id=job["id"])

        cwd = str(Path(self.temp.name) / "workspace" / "attempt-1")
        store.begin_thread_dispatch(
            self.path, job_id=job["id"], thread_attempt_id="attempt-1", cwd=cwd
        )
        store.bind_session_thread(
            self.path,
            job_id=job["id"],
            thread_attempt_id="attempt-1",
            thread_id="thr-1",
            cwd=cwd,
        )
        dispatching = store.begin_turn_dispatch(self.path, job_id=job["id"])
        self.assertEqual(dispatching["status"], "turn_dispatching")
        self.assertEqual(dispatching["thread_id"], "thr-1")

    def test_turn_dispatch_uncertain_requires_recovery_not_safe_claim(self):
        self.pin()
        first = self.enqueue()
        first = store.claim_next_job(self.path)
        cwd = str(Path(self.temp.name) / "workspace" / "attempt-1")
        store.begin_thread_dispatch(
            self.path, job_id=first["id"], thread_attempt_id="attempt-1", cwd=cwd
        )
        store.bind_session_thread(
            self.path,
            job_id=first["id"],
            thread_attempt_id="attempt-1",
            thread_id="thr-1",
            cwd=cwd,
        )
        store.begin_turn_dispatch(self.path, job_id=first["id"])
        uncertain = store.mark_dispatch_uncertain(self.path, job_id=first["id"])
        self.assertEqual(uncertain["status"], "dispatch_uncertain")
        self.assertIsNone(store.claim_next_job(self.path))
        recovered = store.claim_recovery_job(self.path)
        self.assertEqual(recovered["id"], first["id"])

    def test_verified_absent_turn_can_return_to_pre_dispatch_processing(self):
        self.pin()
        job = self.enqueue()
        job = store.claim_next_job(self.path)
        cwd = str(Path(self.temp.name) / "workspace" / "attempt-1")
        store.begin_thread_dispatch(
            self.path, job_id=job["id"], thread_attempt_id="attempt-1", cwd=cwd
        )
        store.bind_session_thread(
            self.path,
            job_id=job["id"],
            thread_attempt_id="attempt-1",
            thread_id="thr-1",
            cwd=cwd,
        )
        store.begin_turn_dispatch(self.path, job_id=job["id"])
        store.mark_dispatch_uncertain(self.path, job_id=job["id"])
        job = store.requeue_after_verified_turn_absent(self.path, job_id=job["id"])
        self.assertEqual(job["status"], "processing")
        self.assertEqual(job["thread_id"], "thr-1")

    def test_reconciled_completed_turn_moves_to_callback_pending(self):
        self.pin()
        job = self.enqueue()
        job = store.claim_next_job(self.path)
        cwd = str(Path(self.temp.name) / "workspace" / "attempt-1")
        store.begin_thread_dispatch(
            self.path, job_id=job["id"], thread_attempt_id="attempt-1", cwd=cwd
        )
        store.bind_session_thread(
            self.path,
            job_id=job["id"],
            thread_attempt_id="attempt-1",
            thread_id="thr-1",
            cwd=cwd,
        )
        store.begin_turn_dispatch(self.path, job_id=job["id"])
        store.mark_dispatch_uncertain(self.path, job_id=job["id"])
        job = store.record_reconciled_turn(
            self.path, job_id=job["id"], turn_id="turn-1", status="completed"
        )
        self.assertEqual(job["status"], "callback_pending")
        self.assertEqual(job["turn_id"], "turn-1")

    def test_completion_is_idempotent_for_same_relay_message_only(self):
        self.pin()
        job = self.enqueue()
        with closing(store.connect(self.path)) as conn:
            conn.execute(
                "UPDATE codex_generation_jobs SET status='callback_pending',thread_id='thr-1',turn_id='turn-1' WHERE id=?",
                (job["id"],),
            )
        first = store.mark_completed(self.path, job_id=job["id"], assistant_message_id=77)
        second = store.mark_completed(self.path, job_id=job["id"], assistant_message_id=77)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["assistant_message_id"], 77)
        with self.assertRaisesRegex(store.CodexGenerationStoreError, "completion_conflict"):
            store.mark_completed(self.path, job_id=job["id"], assistant_message_id=78)

    def test_retire_is_single_way_and_blocked_by_nonterminal_job(self):
        self.pin()
        job = self.enqueue()
        with self.assertRaisesRegex(store.CodexGenerationStoreError, "session_busy"):
            store.retire_session(self.path, api_session="api-canary")
        store.mark_failed(self.path, job_id=job["id"], category="canary_closed")
        retired = store.retire_session(self.path, api_session="api-canary")
        self.assertEqual(retired["status"], "retired")
        with self.assertRaisesRegex(store.CodexGenerationStoreError, "session_conflict"):
            self.pin()


if __name__ == "__main__":
    unittest.main()
