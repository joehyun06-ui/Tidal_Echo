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
        self.relay_path = Path(self.temp.name) / "relay.db"
        store.initialize(self.path)
        with sqlite3.connect(self.relay_path) as conn:
            conn.execute(
                """CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    meta TEXT NOT NULL DEFAULT '{}')"""
            )

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

    def test_recent_ingress_receipt_correlates_exact_canonical_ids_without_text(self):
        timestamps = [
            "2026-08-31T08:31:12.000000+00:00",
            "2026-08-31T08:31:22.000000+00:00",
            "2026-08-31T08:32:07.000000+00:00",
        ]
        ids = []
        with sqlite3.connect(self.relay_path) as conn:
            for index, timestamp in enumerate(timestamps):
                cursor = conn.execute(
                    "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                    (timestamp, "in", "user", f"PRIVATE-{index}", '{"api_session":"secret-session"}'),
                )
                ids.append(int(cursor.lastrowid))
            conn.execute(
                "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                ("2026-08-31T08:32:08+00:00", "out", "reply", "PRIVATE-OUT", "{}"),
            )

        store.pin_session(
            self.path,
            api_session="api-observe",
            model="test-model",
            model_provider="unresolved",
            reasoning_effort=None,
            persona_hash="a" * 64,
        )
        store.enqueue_job(
            self.path,
            api_session="api-observe",
            canonical_message_id=ids[2],
            input_digest="b" * 64,
            generation_id="codex-gen-receipt",
            client_message_id="codex-client-receipt",
            callback_identity="codex-callback-receipt",
        )

        receipt = observability.recent_ingress_receipt(
            self.relay_path,
            self.path,
            limit=8,
        )
        self.assertEqual(receipt["state"], "present")
        self.assertEqual(
            receipt["rows"],
            [
                {"canonical_message_id": ids[0], "ts": timestamps[0], "codex_job": False},
                {"canonical_message_id": ids[1], "ts": timestamps[1], "codex_job": False},
                {
                    "canonical_message_id": ids[2],
                    "ts": timestamps[2],
                    "codex_job": True,
                    "codex_status": "queued",
                },
            ],
        )
        lines = observability.format_ingress_receipt(receipt)
        self.assertEqual(len(lines), 3)
        self.assertIn(f"canonical_message_id={ids[0]}", lines[0])
        self.assertIn("codex_job=false", lines[0])
        self.assertIn(f"canonical_message_id={ids[2]}", lines[2])
        self.assertIn("codex_job=true codex_status=queued", lines[2])
        rendered = "\n".join(lines)
        self.assertNotIn("PRIVATE", rendered)
        self.assertNotIn("secret-session", rendered)
        self.assertNotIn("api-observe", rendered)
        self.assertNotIn("codex-gen-receipt", rendered)

    def test_recent_codex_continuity_proves_same_session_and_thread_without_ids(self):
        session_id = "api-continuity-secret"
        thread_id = "thread-continuity-secret"
        attempt_id = "attempt-continuity-secret"
        cwd = "/tmp/codex-continuity-secret"
        store.pin_session(
            self.path,
            api_session=session_id,
            model="test-model",
            model_provider="unresolved",
            reasoning_effort=None,
            persona_hash="c" * 64,
        )
        store.enqueue_job(
            self.path,
            api_session=session_id,
            canonical_message_id=101,
            input_digest="d" * 64,
            generation_id="codex-gen-cont-1",
            client_message_id="codex-client-cont-1",
            callback_identity="codex-callback-cont-1",
        )
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """UPDATE codex_sessions
                   SET thread_attempt_id=?,thread_id=?,cwd=?
                   WHERE api_session=?""",
                (attempt_id, thread_id, cwd, session_id),
            )
            conn.execute(
                """UPDATE codex_generation_jobs
                   SET thread_attempt_id=?,thread_id=?,cwd=?,status='completed',assistant_message_id=1001
                   WHERE canonical_message_id=101""",
                (attempt_id, thread_id, cwd),
            )
        for index, canonical_id in enumerate((102, 103), start=2):
            store.enqueue_job(
                self.path,
                api_session=session_id,
                canonical_message_id=canonical_id,
                input_digest=("e" if index == 2 else "f") * 64,
                generation_id=f"codex-gen-cont-{index}",
                client_message_id=f"codex-client-cont-{index}",
                callback_identity=f"codex-callback-cont-{index}",
            )
            with sqlite3.connect(self.path) as conn:
                conn.execute(
                    """UPDATE codex_generation_jobs
                       SET status='completed',assistant_message_id=?
                       WHERE canonical_message_id=?""",
                    (1000 + index, canonical_id),
                )

        receipt = observability.recent_codex_continuity_receipt(self.path, limit=3)
        self.assertEqual(receipt["state"], "present")
        self.assertEqual(receipt["job_count"], 3)
        self.assertEqual(receipt["canonical_message_ids"], [101, 102, 103])
        self.assertIs(receipt["same_session"], True)
        self.assertIs(receipt["all_thread_bound"], True)
        self.assertIs(receipt["same_thread"], True)
        self.assertIs(receipt["all_thread_attempt_bound"], True)
        self.assertIs(receipt["same_thread_attempt"], True)
        self.assertIs(receipt["session_active"], True)
        self.assertIs(receipt["current_thread_matches"], True)

        line = observability.format_codex_continuity_receipt(receipt)
        self.assertIn("canonical_message_ids=101,102,103", line)
        self.assertIn("same_session=true", line)
        self.assertIn("same_thread=true", line)
        self.assertIn("same_thread_attempt=true", line)
        self.assertIn("session_active=true", line)
        self.assertIn("current_thread_matches=true", line)
        self.assertNotIn(session_id, line)
        self.assertNotIn(thread_id, line)
        self.assertNotIn(attempt_id, line)
        self.assertNotIn(cwd, line)

    def test_recent_codex_continuity_is_bounded_and_requires_multiple_jobs(self):
        self.assertEqual(
            observability.recent_codex_continuity_receipt(self.path, limit=1),
            {"state": "unavailable"},
        )
        store.pin_session(
            self.path,
            api_session="api-single",
            model="test-model",
            model_provider="unresolved",
            reasoning_effort=None,
            persona_hash="1" * 64,
        )
        store.enqueue_job(
            self.path,
            api_session="api-single",
            canonical_message_id=201,
            input_digest="2" * 64,
            generation_id="codex-gen-single",
            client_message_id="codex-client-single",
            callback_identity="codex-callback-single",
        )
        self.assertEqual(
            observability.recent_codex_continuity_receipt(self.path, limit=3),
            {"state": "insufficient", "job_count": 1},
        )

    def test_recent_ingress_receipt_is_bounded_and_fails_closed(self):
        self.assertEqual(
            observability.recent_ingress_receipt(self.relay_path, self.path, limit=0),
            {"state": "unavailable", "rows": []},
        )
        self.assertEqual(
            observability.recent_ingress_receipt(
                Path(self.temp.name) / "missing.db",
                self.path,
                limit=8,
            ),
            {"state": "unavailable", "rows": []},
        )

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

    def test_entrypoint_logs_receipts_only_after_runtime_start(self):
        entrypoint = (
            Path(__file__).resolve().parents[2] / "examples" / "api_loop_codex_canary.py"
        ).read_text(encoding="utf-8")
        start_at = entrypoint.index("await RUNTIME.start()")
        latest_at = entrypoint.index("codex_generation_observability.log_latest_job_snapshot", start_at)
        receipt_at = entrypoint.index("codex_generation_observability.log_recent_ingress_receipt", latest_at)
        yield_at = entrypoint.index("yield", receipt_at)
        self.assertLess(start_at, latest_at)
        self.assertLess(latest_at, receipt_at)
        self.assertLess(receipt_at, yield_at)
        self.assertIn("if RUNTIME.generation_enabled:", entrypoint[start_at:latest_at])
        self.assertIn("legacy.RELAY_DB", entrypoint[receipt_at:yield_at])

    def test_startup_receipt_appends_bounded_continuity_line(self):
        stream = io.StringIO()
        observability.log_recent_ingress_receipt(
            self.relay_path,
            self.path,
            stream=stream,
        )
        lines = stream.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(lines[-1].startswith("[codex-continuity]"))


if __name__ == "__main__":
    unittest.main()
