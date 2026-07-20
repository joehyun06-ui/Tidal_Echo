from __future__ import annotations

import multiprocessing
import os
import socket
import tempfile
import unittest
from datetime import datetime, time, timezone
from pathlib import Path

from backend import channel_store, deployment_config
from backend.tests._support import NoNetworkMixin


UTC = timezone.utc


def _heartbeat_process_worker(
    database_path, settings, scheduled_iso, candidate, ready_queue, start_event, result_queue,
):
    class GuardedSocket(socket.socket):
        def connect(self, address):
            raise AssertionError("network_disabled")

        def connect_ex(self, address):
            raise AssertionError("network_disabled")

    socket.socket = GuardedSocket
    from backend import heartbeat_service

    ready_queue.put(os.getpid())
    if not start_event.wait(30):
        result_queue.put(("worker_timeout", None, None, None))
        return
    try:
        result = heartbeat_service.run_heartbeat_once(
            database_path,
            settings,
            scheduled_at=datetime.fromisoformat(scheduled_iso),
            now=datetime.fromisoformat(scheduled_iso),
            candidate_decision=candidate,
        )
        result_queue.put((
            result.outcome, result.duplicate, result.run_id, result.error_category,
        ))
    except Exception:
        result_queue.put(("worker_exception", None, None, "safe_worker_failure"))


class HeartbeatMultiprocessTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.context = multiprocessing.get_context("spawn")
        self.scheduled = datetime(2026, 7, 20, 12, tzinfo=UTC)
        self.last_contact = datetime(2026, 7, 20, 10, tzinfo=UTC)

    def _run_case(self, process_count: int, candidate: str) -> None:
        case_dir = Path(self.temp.name) / f"{process_count}-{candidate}"
        case_dir.mkdir()
        database_path = str(case_dir / "heartbeat.sqlite3")
        settings = deployment_config.HeartbeatConfig(
            enabled=True,
            interval_seconds=60,
            timezone="UTC",
            quiet_hours_start=time(1),
            quiet_hours_end=time(2),
            contact_cooldown_seconds=0,
            schedule_revision=f"mp-{process_count}-{candidate}",
        )
        channel_store.run_migrations(database_path)
        stamp = self.scheduled.isoformat()
        with channel_store.connect(database_path) as conn:
            conn.execute(
                """INSERT INTO heartbeat_state
                   (state_key,last_contact_at,consecutive_failures,status,created_at,updated_at)
                   VALUES('default',?,0,'idle',?,?)""",
                (self.last_contact.isoformat(), stamp, stamp),
            )

        ready_queue = self.context.Queue()
        result_queue = self.context.Queue()
        start_event = self.context.Event()
        processes = [
            self.context.Process(
                target=_heartbeat_process_worker,
                args=(
                    database_path, settings, self.scheduled.isoformat(), candidate,
                    ready_queue, start_event, result_queue,
                ),
            )
            for _ in range(process_count)
        ]
        try:
            for process in processes:
                process.start()
            ready = [ready_queue.get(timeout=30) for _ in processes]
            self.assertEqual(len(set(ready)), process_count)
            start_event.set()
            results = [result_queue.get(timeout=60) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10)
            ready_queue.close()
            result_queue.close()
            ready_queue.join_thread()
            result_queue.join_thread()

        self.assertTrue(all(result[0] == "completed" for result in results), results)
        self.assertEqual(sum(result[1] is False for result in results), 1)
        self.assertEqual(len({result[2] for result in results}), 1)
        with channel_store.connect(database_path) as conn:
            counts = {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "heartbeat_state", "heartbeat_runs", "journal_entries", "timeline_events",
                )
            }
            running = conn.execute(
                "SELECT count(*) FROM heartbeat_runs WHERE outcome='running'"
            ).fetchone()[0]
            state = conn.execute("SELECT * FROM heartbeat_state").fetchone()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(counts["heartbeat_state"], 1)
        self.assertEqual(counts["heartbeat_runs"], 1)
        self.assertEqual(counts["timeline_events"], 1)
        self.assertEqual(
            counts["journal_entries"], 0 if candidate == "observe" else 1,
        )
        self.assertEqual(running, 0)
        self.assertEqual(state["last_contact_at"], self.last_contact.isoformat())
        self.assertEqual(integrity, "ok")
        self.assertEqual(foreign_keys, [])

    def test_spawned_processes_deduplicate_each_candidate_shape(self):
        for process_count in (2, 4, 8):
            for candidate in ("observe", "journal_candidate", "contact_candidate"):
                with self.subTest(process_count=process_count, candidate=candidate):
                    self._run_case(process_count, candidate)


if __name__ == "__main__":
    unittest.main()
