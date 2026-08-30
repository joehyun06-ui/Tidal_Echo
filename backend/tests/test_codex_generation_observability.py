from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import codex_generation_observability as observability
from backend import codex_generation_store as store


class CodexGenerationObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "codex-generation.db"
        store.initialize(self.path)

    def test_empty_store_projects_only_empty_state(self):
        self.assertEqual(observability.latest_job_snapshot(self.path), {"state": "empty"})
        stream = io.StringIO()
        observability.log_latest_job_snapshot(self.path, stream=stream)
        self.assertEqual(stream.getvalue(), "[codex-generation] latest_job=empty\n")

    def test_latest_job_projects_bounded_lifecycle_metadata_only(self):
        store.pin_session(
            self.path,
            api_session="api-observe",
            model="test-model",
            model_provider="unresolved",
            reasoning_effort="medium",
            persona_hash="a" * 64,
        )
        store.enqueue_job(
            self.path,
            api_session="api-observe",
            canonical_message_id=7,
            input_digest="b" * 64,
            generation_id="codex-gen-7",
            client_message_id="codex-client-7",
            callback_identity="codex-callback-7",
        )
        snapshot = observability.latest_job_snapshot(self.path)
        self.assertEqual(snapshot["state"], "present")
        self.assertEqual(snapshot["status"], "queued")
        self.assertEqual(snapshot["attempt_count"], 0)
        self.assertEqual(snapshot["recovery_count"], 0)
        self.assertIs(snapshot["turn_bound"], False)
        self.assertIs(snapshot["assistant_message_bound"], False)
        self.assertIn("created_at", snapshot)
        self.assertIn("updated_at", snapshot)
        self.assertNotIn("api_session", snapshot)
        self.assertNotIn("generation_id", snapshot)
        self.assertNotIn("client_message_id", snapshot)
        self.assertNotIn("callback_identity", snapshot)
        self.assertNotIn("input_digest", snapshot)
        self.assertNotIn("thread_id", snapshot)
        self.assertNotIn("model", snapshot)

        line = observability.format_latest_job_snapshot(snapshot)
        self.assertIn("latest_job=present", line)
        self.assertIn("status=queued", line)
        self.assertNotIn("api-observe", line)
        self.assertNotIn("codex-gen-7", line)
        self.assertNotIn("test-model", line)

    def test_malformed_persisted_state_fails_closed_to_unavailable(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO codex_sessions
                   (api_session,provider,status,model,model_provider,reasoning_effort,persona_hash,
                    thread_attempt_id,thread_id,cwd,created_at,updated_at,retired_at)
                   VALUES('api-bad','codex','active','m','unresolved',NULL,?,NULL,NULL,NULL,?,?,NULL)""",
                ("c" * 64, store.now_iso(), store.now_iso()),
            )
            conn.execute(
                """INSERT INTO codex_generation_jobs
                   (generation_id,callback_identity,client_message_id,api_session,
                    canonical_message_id,input_digest,status,lease_until,attempt_count,recovery_count,
                    thread_attempt_id,thread_id,cwd,turn_id,assistant_message_id,error_category,
                    created_at,updated_at)
                   VALUES('g-bad','cb-bad','client-bad','api-bad',9,?,'queued',NULL,0,0,
                          NULL,NULL,NULL,NULL,NULL,NULL,'not-a-time','not-a-time')""",
                ("d" * 64,),
            )
        self.assertEqual(
            observability.latest_job_snapshot(self.path),
            {"state": "unavailable"},
        )

    def test_entrypoint_logs_snapshot_only_after_runtime_start(self):
        entrypoint = (
            Path(__file__).resolve().parents[2] / "examples" / "api_loop_codex_canary.py"
        ).read_text(encoding="utf-8")
        start_at = entrypoint.index("await RUNTIME.start()")
        log_at = entrypoint.index("codex_generation_observability.log_latest_job_snapshot", start_at)
        yield_at = entrypoint.index("yield", log_at)
        self.assertLess(start_at, log_at)
        self.assertLess(log_at, yield_at)
        self.assertIn("if RUNTIME.generation_enabled:", entrypoint[start_at:log_at])


if __name__ == "__main__":
    unittest.main()
