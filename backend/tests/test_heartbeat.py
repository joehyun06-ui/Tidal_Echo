from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timezone
from pathlib import Path
from unittest import mock

from backend import channel_store, deployment_config, heartbeat_service
from backend.tests._support import NoNetworkMixin


UTC = timezone.utc


def config(
    *, enabled: bool = True, timezone_name: str = "UTC", quiet_start: time = time(22),
    quiet_end: time = time(8), cooldown: int = 300, interval: int = 60,
) -> deployment_config.HeartbeatConfig:
    return deployment_config.HeartbeatConfig(
        enabled, interval, timezone_name, quiet_start, quiet_end, cooldown,
    )


class HeartbeatConfigurationTests(NoNetworkMixin, unittest.TestCase):
    def test_default_is_disabled(self):
        loaded = deployment_config.load_heartbeat_config({})
        self.assertFalse(loaded.enabled)
        self.assertEqual(loaded.timezone, "UTC")
        self.assertEqual(loaded.interval_seconds, 300)
        self.assertEqual(loaded.contact_cooldown_seconds, 21600)

    def test_invalid_configuration_is_rejected_even_while_disabled(self):
        cases = (
            ("HEARTBEAT_ENABLED", "treu", "invalid_heartbeat_enabled"),
            ("HEARTBEAT_INTERVAL_SECONDS", "29", "invalid_heartbeat_interval"),
            ("HEARTBEAT_INTERVAL_SECONDS", "300.0", "invalid_heartbeat_interval"),
            ("HEARTBEAT_TIMEZONE", "Mars/Olympus", "invalid_heartbeat_timezone"),
            ("HEARTBEAT_TIMEZONE", " UTC", "invalid_heartbeat_timezone"),
            ("HEARTBEAT_QUIET_HOURS_START", "9:00", "invalid_heartbeat_quiet_hours_start"),
            ("HEARTBEAT_QUIET_HOURS_END", "24:00", "invalid_heartbeat_quiet_hours_end"),
            ("HEARTBEAT_CONTACT_COOLDOWN_SECONDS", "-1", "invalid_heartbeat_contact"),
        )
        for name, value, category in cases:
            with self.subTest(name=name, value=value), self.assertRaisesRegex(
                deployment_config.DeploymentConfigError, category
            ):
                deployment_config.load_heartbeat_config({name: value})
        with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "relationship"):
            deployment_config.load_heartbeat_config({
                "HEARTBEAT_QUIET_HOURS_START": "08:00",
                "HEARTBEAT_QUIET_HOURS_END": "08:00",
            })


class HeartbeatDecisionTests(NoNetworkMixin, unittest.TestCase):
    def test_disabled_decision_wins(self):
        decision = heartbeat_service.decide_heartbeat(
            config(enabled=False), datetime(2026, 7, 19, 12, tzinfo=UTC), None, "contact_candidate"
        )
        self.assertEqual(decision, "disabled")

    def test_ordinary_quiet_hours(self):
        settings = config(quiet_start=time(9), quiet_end=time(17))
        self.assertEqual(heartbeat_service.decide_heartbeat(
            settings, datetime(2026, 7, 19, 10, tzinfo=UTC), None
        ), "quiet_hours")
        self.assertEqual(heartbeat_service.decide_heartbeat(
            settings, datetime(2026, 7, 19, 17, tzinfo=UTC), None
        ), "observe")

    def test_cross_midnight_quiet_hours(self):
        settings = config(quiet_start=time(22), quiet_end=time(8))
        for hour in (23, 7):
            with self.subTest(hour=hour):
                self.assertEqual(heartbeat_service.decide_heartbeat(
                    settings, datetime(2026, 7, 19, hour, tzinfo=UTC), None
                ), "quiet_hours")
        self.assertEqual(heartbeat_service.decide_heartbeat(
            settings, datetime(2026, 7, 19, 12, tzinfo=UTC), None
        ), "observe")

    def test_iana_timezone_conversion(self):
        settings = config(timezone_name="America/New_York", quiet_start=time(22), quiet_end=time(8))
        self.assertEqual(heartbeat_service.decide_heartbeat(
            settings, datetime(2026, 7, 19, 3, tzinfo=UTC), None
        ), "quiet_hours")

    def test_cooldown_active_and_boundary_expired(self):
        settings = config(quiet_start=time(1), quiet_end=time(2), cooldown=300)
        contact = datetime(2026, 7, 19, 12, tzinfo=UTC)
        self.assertEqual(heartbeat_service.decide_heartbeat(
            settings, datetime(2026, 7, 19, 12, 4, 59, tzinfo=UTC), contact, "contact_candidate"
        ), "cooldown")
        self.assertEqual(heartbeat_service.decide_heartbeat(
            settings, datetime(2026, 7, 19, 12, 5, tzinfo=UTC), contact, "contact_candidate"
        ), "contact_candidate")


class HeartbeatServiceTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "heartbeat.sqlite3")
        self.settings = config(quiet_start=time(1), quiet_end=time(2), interval=60)
        self.noon = datetime(2026, 7, 19, 12, tzinfo=UTC)

    def state_with_contact(self, contact: datetime) -> None:
        channel_store.run_migrations(self.path)
        stamp = self.noon.isoformat()
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO heartbeat_state
                   (state_key,last_contact_at,consecutive_failures,status,created_at,updated_at)
                   VALUES('default',?,0,'idle',?,?)""",
                (contact.isoformat(), stamp, stamp),
            )

    def test_persisted_cooldown_survives_service_restart_boundary(self):
        self.state_with_contact(self.noon)
        result = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon.replace(minute=1),
            now=self.noon.replace(minute=1), candidate_decision="contact_candidate",
        )
        self.assertEqual(result.decision, "cooldown")
        with channel_store.connect(self.path) as conn:
            state = conn.execute("SELECT * FROM heartbeat_state").fetchone()
        self.assertEqual(state["last_contact_at"], self.noon.isoformat())

    def test_path_lock_registry_is_released_after_tick(self):
        heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon, now=self.noon,
        )
        self.assertEqual(heartbeat_service._PATH_LOCKS, {})

    def test_duplicate_ticks_share_one_run_and_one_candidate(self):
        first = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon.replace(second=1), now=self.noon,
            candidate_decision="journal_candidate",
        )
        second = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon.replace(second=59), now=self.noon,
            candidate_decision="journal_candidate",
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.run_id, second.run_id)
        with channel_store.connect(self.path) as conn:
            counts = tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                           for table in ("heartbeat_runs", "journal_entries", "timeline_events"))
        self.assertEqual(counts, (1, 1, 1))

    def test_concurrent_tick_is_serialized_and_deduplicated(self):
        def execute(_index):
            return heartbeat_service.run_heartbeat_once(
                self.path, self.settings, scheduled_at=self.noon, now=self.noon,
                candidate_decision="contact_candidate",
            )
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(execute, range(8)))
        self.assertEqual(sum(not item.duplicate for item in results), 1)
        self.assertEqual(len({item.run_id for item in results}), 1)
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM heartbeat_runs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM journal_entries").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM timeline_events").fetchone()[0], 1)

    def test_failure_does_not_advance_contact_and_is_recoverable(self):
        prior = self.noon.replace(hour=10)
        self.state_with_contact(prior)
        with mock.patch.object(heartbeat_service, "_record_key", side_effect=RuntimeError("injected")):
            failed = heartbeat_service.run_heartbeat_once(
                self.path, self.settings, scheduled_at=self.noon, now=self.noon,
                candidate_decision="contact_candidate",
            )
        self.assertEqual(failed.outcome, "failed")
        with channel_store.connect(self.path) as conn:
            state = conn.execute("SELECT * FROM heartbeat_state").fetchone()
            self.assertEqual(state["last_contact_at"], prior.isoformat())
            self.assertEqual(state["consecutive_failures"], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM journal_entries").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM timeline_events").fetchone()[0], 0)
        recovered = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon, now=self.noon.replace(second=1),
            candidate_decision="contact_candidate",
        )
        self.assertEqual(recovered.outcome, "completed")
        self.assertTrue(recovered.recovered)
        with channel_store.connect(self.path) as conn:
            state = conn.execute("SELECT * FROM heartbeat_state").fetchone()
            run = conn.execute("SELECT * FROM heartbeat_runs").fetchone()
        self.assertEqual(state["last_contact_at"], prior.isoformat())
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertEqual(run["attempt_count"], 2)

    def test_journal_and_timeline_candidates_are_fixed_local_records(self):
        journal = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon, now=self.noon,
            candidate_decision="journal_candidate",
        )
        contact = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon.replace(minute=1),
            now=self.noon.replace(minute=1), candidate_decision="contact_candidate",
        )
        self.assertIsNotNone(journal.journal_entry_id)
        self.assertIsNotNone(contact.journal_entry_id)
        with channel_store.connect(self.path) as conn:
            journals = conn.execute("SELECT entry_type,content,source FROM journal_entries ORDER BY id").fetchall()
            timeline = conn.execute("SELECT event_type,summary,source FROM timeline_events ORDER BY id").fetchall()
            outbox = conn.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0]
            jobs = conn.execute("SELECT count(*) FROM generation_jobs").fetchone()[0]
        self.assertEqual([tuple(row) for row in journals], [
            ("journal_candidate", "heartbeat observed", "heartbeat"),
            ("contact_candidate", "contact candidate deferred", "heartbeat"),
        ])
        self.assertEqual([tuple(row) for row in timeline], [
            ("journal_candidate", "heartbeat observed", "heartbeat"),
            ("contact_candidate", "contact candidate deferred", "heartbeat"),
        ])
        self.assertEqual((outbox, jobs), (0, 0))

    def test_persisted_times_are_utc_and_contact_is_never_advanced(self):
        result = heartbeat_service.run_heartbeat_once(
            self.path, self.settings,
            scheduled_at=datetime(2026, 7, 19, 8, tzinfo=timezone.utc),
            now=datetime(2026, 7, 19, 8, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(result.outcome, "completed")
        with channel_store.connect(self.path) as conn:
            state = conn.execute("SELECT * FROM heartbeat_state").fetchone()
            run = conn.execute("SELECT * FROM heartbeat_runs").fetchone()
        for value in (state["last_tick_at"], state["last_success_at"], run["scheduled_at"],
                      run["started_at"], run["completed_at"]):
            parsed = datetime.fromisoformat(value)
            self.assertEqual(parsed.utcoffset().total_seconds(), 0)
        self.assertIsNone(state["last_contact_at"])


if __name__ == "__main__":
    unittest.main()
