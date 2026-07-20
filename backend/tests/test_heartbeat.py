from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock

from backend import channel_store, deployment_config, heartbeat_service
from backend.tests._support import NoNetworkMixin


UTC = timezone.utc


def config(
    *, enabled: bool = True, timezone_name: str = "UTC", quiet_start: time = time(22),
    quiet_end: time = time(8), cooldown: int = 300, interval: int = 60,
    schedule_revision: str = "test-r1",
) -> deployment_config.HeartbeatConfig:
    return deployment_config.HeartbeatConfig(
        enabled, interval, timezone_name, quiet_start, quiet_end, cooldown, schedule_revision,
    )


class FaultConnection:
    def __init__(self, connection, should_fail):
        self.connection = connection
        self.should_fail = should_fail

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.connection.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        if self.should_fail(" ".join(sql.split())):
            error = sqlite3.OperationalError("injected sqlite failure")
            error.sqlite_errorname = "SQLITE_FULL"
            raise error
        return self.connection.execute(sql, parameters)


class HeartbeatConfigurationTests(NoNetworkMixin, unittest.TestCase):
    def test_default_is_disabled(self):
        loaded = deployment_config.load_heartbeat_config({})
        self.assertFalse(loaded.enabled)
        self.assertEqual(loaded.timezone, "UTC")
        self.assertEqual(loaded.interval_seconds, 300)
        self.assertEqual(loaded.contact_cooldown_seconds, 21600)
        self.assertEqual(loaded.schedule_revision, "default")

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
            ("HEARTBEAT_SCHEDULE_REVISION", "bad revision", "invalid_heartbeat_schedule"),
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

    def test_invalid_direct_inputs_fail_before_migration_or_database_write(self):
        invalid_configs = (
            replace(self.settings, enabled=1),
            replace(self.settings, interval_seconds=0),
            replace(self.settings, timezone="Invalid/Zone"),
            replace(self.settings, quiet_hours_start=time(1, 0, 1)),
            replace(self.settings, quiet_hours_end=self.settings.quiet_hours_start),
            replace(self.settings, contact_cooldown_seconds=-1),
            replace(self.settings, schedule_revision="bad revision"),
        )
        for invalid in invalid_configs:
            with self.subTest(config=invalid), mock.patch.object(
                channel_store, "run_migrations"
            ) as migrations, self.assertRaisesRegex(ValueError, "invalid_heartbeat_config"):
                heartbeat_service.run_heartbeat_once(
                    self.path, invalid, scheduled_at=self.noon, now=self.noon,
                )
            migrations.assert_not_called()
        for candidate in (None, 1, {}, "not_allowed"):
            with self.subTest(candidate=type(candidate).__name__), mock.patch.object(
                channel_store, "run_migrations"
            ) as migrations, self.assertRaisesRegex(
                ValueError, "invalid_heartbeat_candidate_decision"
            ):
                heartbeat_service.run_heartbeat_once(
                    self.path, self.settings, scheduled_at=self.noon, now=self.noon,
                    candidate_decision=candidate,
                )
            migrations.assert_not_called()
        self.assertFalse(Path(self.path).exists())

    def test_caller_metadata_is_rejected_and_persisted_metadata_is_fixed(self):
        with self.assertRaises(TypeError):
            heartbeat_service.run_heartbeat_once(
                self.path, self.settings, scheduled_at=self.noon, now=self.noon,
                metadata={"untrusted": "sensitive-placeholder"},
            )
        self.assertFalse(Path(self.path).exists())
        result = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon, now=self.noon,
            candidate_decision="observe",
        )
        self.assertEqual(result.outcome, "completed")
        with channel_store.connect(self.path) as conn:
            metadata = json.loads(conn.execute(
                "SELECT metadata_json FROM heartbeat_runs"
            ).fetchone()[0])
        self.assertEqual(metadata, {"foundation_version": 2})

    def test_all_six_candidate_values_are_strictly_allowed(self):
        for index, candidate in enumerate(sorted(heartbeat_service.DECISIONS)):
            with self.subTest(candidate=candidate):
                result = heartbeat_service.run_heartbeat_once(
                    self.path, self.settings,
                    scheduled_at=self.noon.replace(minute=index),
                    now=self.noon.replace(minute=index), candidate_decision=candidate,
                )
                self.assertEqual(result.decision, candidate)

    def test_relative_dotdot_and_absolute_paths_share_one_database(self):
        root = Path(self.temp.name) / "paths"
        nested = root / "nested"
        nested.mkdir(parents=True)
        database = root / "canonical.sqlite3"
        original_cwd = Path.cwd()
        try:
            os.chdir(nested)
            relative = Path("..") / "." / "canonical.sqlite3"
            first = heartbeat_service.run_heartbeat_once(
                relative, self.settings, scheduled_at=self.noon, now=self.noon,
            )
        finally:
            os.chdir(original_cwd)
        second = heartbeat_service.run_heartbeat_once(
            database.resolve(), self.settings, scheduled_at=self.noon, now=self.noon,
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.run_id, second.run_id)

    def test_canonical_path_survives_working_directory_change_after_entry(self):
        start = Path(self.temp.name) / "start"
        other = Path(self.temp.name) / "other"
        start.mkdir()
        other.mkdir()
        original_cwd = Path.cwd()
        original_migrations = channel_store.run_migrations

        def migrations_then_change_cwd(path):
            os.chdir(other)
            return original_migrations(path)

        try:
            os.chdir(start)
            with mock.patch.object(
                channel_store, "run_migrations", side_effect=migrations_then_change_cwd
            ):
                result = heartbeat_service.run_heartbeat_once(
                    "heartbeat.sqlite3", self.settings,
                    scheduled_at=self.noon, now=self.noon,
                )
        finally:
            os.chdir(original_cwd)
        self.assertEqual(result.outcome, "completed")
        self.assertTrue((start / "heartbeat.sqlite3").exists())
        self.assertFalse((other / "heartbeat.sqlite3").exists())

    def test_symlinked_directory_resolves_to_same_database_when_supported(self):
        actual = Path(self.temp.name) / "actual"
        alias = Path(self.temp.name) / "alias"
        actual.mkdir()
        try:
            os.symlink(actual, alias, target_is_directory=True)
        except OSError:
            self.assertFalse(alias.exists())
            return
        first = heartbeat_service.run_heartbeat_once(
            actual / "heartbeat.sqlite3", self.settings,
            scheduled_at=self.noon, now=self.noon,
        )
        second = heartbeat_service.run_heartbeat_once(
            alias / "heartbeat.sqlite3", self.settings,
            scheduled_at=self.noon, now=self.noon,
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)

    def test_bucket_boundaries_are_stable(self):
        before = self.noon - timedelta(microseconds=1)
        first = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=before, now=before,
        )
        boundary = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon, now=self.noon,
        )
        after = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon + timedelta(microseconds=1),
            now=self.noon + timedelta(microseconds=1),
        )
        self.assertNotEqual(first.run_id, boundary.run_id)
        self.assertEqual(boundary.run_id, after.run_id)
        self.assertTrue(after.duplicate)

    def test_schedule_revision_and_input_fingerprint_conflicts(self):
        first = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon, now=self.noon,
            candidate_decision="observe",
        )
        changed_candidate = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon, now=self.noon,
            candidate_decision="contact_candidate",
        )
        changed_enabled = heartbeat_service.run_heartbeat_once(
            self.path, replace(self.settings, enabled=False),
            scheduled_at=self.noon, now=self.noon, candidate_decision="observe",
        )
        changed_interval = heartbeat_service.run_heartbeat_once(
            self.path, replace(self.settings, interval_seconds=300),
            scheduled_at=self.noon, now=self.noon, candidate_decision="observe",
        )
        new_revision_same_tick = heartbeat_service.run_heartbeat_once(
            self.path, replace(self.settings, schedule_revision="test-r2"),
            scheduled_at=self.noon, now=self.noon, candidate_decision="observe",
        )
        self.assertEqual(first.outcome, "completed")
        self.assertEqual(changed_candidate.error_category, "input_fingerprint_conflict")
        self.assertEqual(changed_enabled.error_category, "input_fingerprint_conflict")
        self.assertEqual(changed_interval.error_category, "schedule_revision_conflict")
        self.assertEqual(new_revision_same_tick.error_category, "logical_tick_conflict")
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM heartbeat_runs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM timeline_events").fetchone()[0], 1)

    def test_forward_jump_and_clock_rollback_are_structured(self):
        first = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon, now=self.noon,
        )
        forward = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon + timedelta(hours=2),
            now=self.noon + timedelta(hours=2),
        )
        completed_replay = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon, now=self.noon + timedelta(hours=2),
        )
        stale = heartbeat_service.run_heartbeat_once(
            self.path, self.settings, scheduled_at=self.noon - timedelta(minutes=1),
            now=self.noon + timedelta(hours=2),
        )
        self.assertEqual(first.outcome, "completed")
        self.assertEqual(forward.outcome, "completed")
        self.assertTrue(completed_replay.duplicate)
        self.assertEqual(stale.error_category, "stale_clock")
        with channel_store.connect(self.path) as conn:
            state = conn.execute("SELECT last_tick_at FROM heartbeat_state").fetchone()[0]
            count = conn.execute("SELECT count(*) FROM heartbeat_runs").fetchone()[0]
        self.assertEqual(state, forward.scheduled_at)
        self.assertEqual(count, 2)

    def _run_with_sql_fault(self, should_fail):
        channel_store.run_migrations(self.path)
        sentinel = self.noon - timedelta(hours=1)
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO heartbeat_state
                   (state_key,last_tick_at,last_contact_at,consecutive_failures,status,
                    created_at,updated_at)
                   VALUES('default',?,?,0,'observe',?,?)""",
                (sentinel.isoformat(), sentinel.isoformat(), sentinel.isoformat(), sentinel.isoformat()),
            )
        real_connect = channel_store.connect
        call_count = 0

        def faulting_connect(path):
            nonlocal call_count
            call_count += 1
            connection = real_connect(path)
            return FaultConnection(connection, should_fail) if call_count == 2 else connection

        with mock.patch.object(channel_store, "connect", side_effect=faulting_connect):
            result = heartbeat_service.run_heartbeat_once(
                self.path, self.settings, scheduled_at=self.noon, now=self.noon,
                candidate_decision="contact_candidate",
            )
        return result, sentinel

    def test_insert_and_update_failures_preserve_transaction_invariants(self):
        cases = (
            ("insert", lambda sql: sql.startswith("INSERT INTO timeline_events")),
            ("update", lambda sql: sql.startswith("UPDATE heartbeat_state SET last_tick_at")),
        )
        for label, predicate in cases:
            with self.subTest(label=label):
                target = str(Path(self.temp.name) / f"fault-{label}.sqlite3")
                original_path, self.path = self.path, target
                try:
                    result, sentinel = self._run_with_sql_fault(predicate)
                finally:
                    self.path = original_path
                self.assertEqual(result.outcome, "failed")
                self.assertEqual(result.error_category, "heartbeat_execution_failed")
                self.assertEqual(result.database_error_category, "sqlite_full")
                with channel_store.connect(target) as conn:
                    state = conn.execute("SELECT * FROM heartbeat_state").fetchone()
                    run = conn.execute("SELECT * FROM heartbeat_runs").fetchone()
                    self.assertEqual(conn.execute("SELECT count(*) FROM timeline_events").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT count(*) FROM journal_entries").fetchone()[0], 0)
                self.assertEqual(state["last_tick_at"], sentinel.isoformat())
                self.assertEqual(state["last_contact_at"], sentinel.isoformat())
                self.assertEqual(run["outcome"], "failed")
                self.assertEqual(run["error_category"], "sqlite_full")

    def test_commit_failure_after_savepoint_release_is_not_masked(self):
        release_seen = False
        failed = False

        def fail_commit(sql):
            nonlocal release_seen, failed
            if sql == "RELEASE SAVEPOINT heartbeat_execution":
                release_seen = True
            if sql == "COMMIT" and release_seen and not failed:
                failed = True
                return True
            return False

        result, sentinel = self._run_with_sql_fault(fail_commit)
        self.assertTrue(release_seen)
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.error_category, "commit_failed")
        self.assertEqual(result.database_error_category, "sqlite_full")
        with channel_store.connect(self.path) as conn:
            state = conn.execute("SELECT * FROM heartbeat_state").fetchone()
            self.assertEqual(conn.execute("SELECT count(*) FROM heartbeat_runs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM timeline_events").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM journal_entries").fetchone()[0], 0)
        self.assertEqual(state["last_tick_at"], sentinel.isoformat())
        self.assertEqual(state["last_contact_at"], sentinel.isoformat())

    def test_commit_and_rollback_failure_is_classified_uncertain_without_implicit_commit(self):
        release_seen = False

        def fail_commit_and_rollback(sql):
            nonlocal release_seen
            if sql == "RELEASE SAVEPOINT heartbeat_execution":
                release_seen = True
            return release_seen and sql in {"COMMIT", "ROLLBACK"}

        result, sentinel = self._run_with_sql_fault(fail_commit_and_rollback)
        self.assertEqual(result.outcome, "uncertain")
        self.assertEqual(result.error_category, "commit_uncertain")
        self.assertEqual(result.database_error_category, "sqlite_full")
        with channel_store.connect(self.path) as conn:
            state = conn.execute("SELECT * FROM heartbeat_state").fetchone()
            self.assertEqual(conn.execute("SELECT count(*) FROM heartbeat_runs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM timeline_events").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM journal_entries").fetchone()[0], 0)
        self.assertEqual(state["last_tick_at"], sentinel.isoformat())
        self.assertEqual(state["last_contact_at"], sentinel.isoformat())


if __name__ == "__main__":
    unittest.main()
