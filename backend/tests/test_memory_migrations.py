from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend import channel_store


class MemoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "memory.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")

    def test_empty_database_upgrades_to_v10_and_is_repeatable(self):
        channel_store.run_migrations(self.path)
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            versions = [
                row[0] for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            channel_store.validate_memory_schema(conn)
        self.assertEqual(versions, list(range(1, 11)))
        self.assertTrue(
            {
                "memory_items",
                "memory_fingerprint_profile",
                "memory_evidence_events",
                "memory_sources",
                "memory_suppressions",
                "memory_action_requests",
                "memory_candidate_sources",
                "memory_auto_formation_runs",
                "memory_candidate_decisions",
            }.issubset(tables)
        )

    def test_every_synthetic_prior_version_upgrades_to_v10(self):
        for version in range(1, 10):
            with self.subTest(version=version):
                path = str(Path(self.temp.name) / f"v{version}.sqlite3")
                with channel_store.connect(path) as conn:
                    conn.execute("""CREATE TABLE messages(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                        text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
                channel_store.run_migrations(path, channel_store.MIGRATIONS[:version])
                with channel_store.connect(path) as conn:
                    stamp = channel_store.now_iso()
                    conn.execute(
                        """INSERT INTO channel_accounts
                           (channel,external_account_id,status,created_at,updated_at)
                           VALUES('telegram',?,'active',?,?)""",
                        (f"synthetic-v{version}", stamp, stamp),
                    )
                channel_store.run_migrations(path)
                with channel_store.connect(path) as conn:
                    marker = conn.execute(
                        "SELECT status FROM schema_migrations WHERE version=9"
                    ).fetchone()
                    preserved = conn.execute(
                        "SELECT count(*) FROM channel_accounts"
                    ).fetchone()[0]
                    channel_store.validate_memory_schema(conn)
                self.assertEqual(marker[0], "applied")
                self.assertEqual(preserved, 1)

    def test_concurrent_optional_v9_v10_migrations_apply_exactly_once(self):
        channel_store.run_migrations(
            self.path, channel_store.MIGRATIONS[:8],
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(
                lambda _index: channel_store.run_migrations(self.path),
                range(16),
            ))
        with channel_store.connect(self.path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM schema_migrations WHERE version=9"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM schema_migrations WHERE version=10"
                ).fetchone()[0],
                1,
            )
            channel_store.validate_memory_schema(conn)
            channel_store.validate_memory_action_schema(conn)
            channel_store.validate_memory_candidate_persistence_schema(conn)
            channel_store.validate_memory_candidate_decision_schema_v1_v10(
                conn
            )

    def test_existing_v8_upgrades_additively_without_rebuilding_memory_schema(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:8])
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO memory_items
                   (memory_key,kind,scope_type,scope_ref,normalized_content,
                    normalized_fingerprint,fingerprint_version,status,explicitness,
                    confidence,sensitivity,first_observed_at,last_confirmed_at,
                    superseded_by_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,1,'candidate','inferred',0.0,'normal',
                          ?,?,NULL,?,?)""",
                (
                    "V" * 32,
                    "project",
                    "global_user",
                    "",
                    "synthetic preserved candidate",
                    b"v" * 32,
                    stamp,
                    stamp,
                    stamp,
                    stamp,
                ),
            )
            before = {
                (row["type"], row["name"]): row["sql"]
                for row in conn.execute(
                    """SELECT type,name,sql FROM sqlite_master
                       WHERE (name LIKE 'memory_%' OR name LIKE 'idx_memory_%')
                         AND name NOT LIKE 'sqlite_autoindex_%'"""
                )
            }

        channel_store.run_migrations(self.path)

        with channel_store.connect(self.path) as conn:
            after_old = {
                key: conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                    key,
                ).fetchone()[0]
                for key in before
            }
            marker = conn.execute(
                "SELECT name,status FROM schema_migrations WHERE version=9"
            ).fetchone()
            preserved = conn.execute(
                "SELECT status,explicitness,confidence FROM memory_items"
            ).fetchone()
            channel_store.validate_memory_candidate_persistence_schema(conn)
        self.assertEqual(after_old, before)
        self.assertEqual(
            tuple(marker),
            ("automatic_memory_candidate_persistence_foundation", "applied"),
        )
        self.assertEqual(tuple(preserved), ("candidate", "inferred", 0.0))

    def test_v9_exact_columns_indexes_and_foreign_keys(self):
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            source_columns = tuple(
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_xinfo(memory_candidate_sources)"
                )
            )
            run_columns = tuple(
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_xinfo(memory_auto_formation_runs)"
                )
            )
            source_indexes = {
                row["name"]: (
                    bool(row["unique"]),
                    row["origin"],
                    bool(row["partial"]),
                    channel_store._index_columns(conn, row["name"]),
                )
                for row in conn.execute(
                    "PRAGMA index_list(memory_candidate_sources)"
                )
            }
            run_indexes = conn.execute(
                "PRAGMA index_list(memory_auto_formation_runs)"
            ).fetchall()
            source_fks = {
                (
                    row["from"], row["table"], row["to"],
                    row["on_delete"],
                )
                for row in conn.execute(
                    "PRAGMA foreign_key_list(memory_candidate_sources)"
                )
            }
            run_fks = {
                (
                    row["from"], row["table"], row["to"],
                    row["on_delete"],
                )
                for row in conn.execute(
                    "PRAGMA foreign_key_list(memory_auto_formation_runs)"
                )
            }
            triggers = {
                row["name"]
                for row in conn.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='trigger'
                         AND tbl_name IN (
                             'memory_candidate_sources',
                             'memory_auto_formation_runs'
                         )"""
                )
            }
        self.assertEqual(source_columns, (
            "id",
            "memory_id",
            "canonical_message_id",
            "signal_type",
            "span_start",
            "span_end",
            "formation_contract_version",
            "extractor_contract_version",
            "created_at",
        ))
        self.assertEqual(run_columns, (
            "canonical_message_id",
            "proposal_digest",
            "proposal_count",
            "candidate_count",
            "created_count",
            "existing_candidate_count",
            "active_duplicate_count",
            "suppressed_count",
            "formation_contract_version",
            "extractor_contract_version",
            "created_at",
        ))
        self.assertEqual(source_indexes, {
            "sqlite_autoindex_memory_candidate_sources_1": (
                True,
                "u",
                False,
                (
                    "memory_id",
                    "canonical_message_id",
                    "signal_type",
                    "span_start",
                    "span_end",
                    "formation_contract_version",
                    "extractor_contract_version",
                ),
            ),
            "idx_memory_candidate_sources_memory": (
                False, "c", False, ("memory_id", "id"),
            ),
            "idx_memory_candidate_sources_canonical": (
                False, "c", False, ("canonical_message_id", "id"),
            ),
        })
        self.assertEqual(run_indexes, [])
        self.assertEqual(source_fks, {
            ("memory_id", "memory_items", "id", "RESTRICT"),
            ("canonical_message_id", "messages", "id", "RESTRICT"),
        })
        self.assertEqual(run_fks, {
            ("canonical_message_id", "messages", "id", "RESTRICT"),
        })
        self.assertEqual(triggers, {
            "memory_candidate_sources_immutable_update",
            "memory_candidate_sources_immutable_delete",
            "memory_auto_formation_runs_immutable_update",
            "memory_auto_formation_runs_immutable_delete",
        })

    def test_v9_checks_unique_and_restrict_constraints_fail_closed(self):
        channel_store.run_migrations(self.path)
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            message_ids = []
            for index in range(8):
                message_ids.append(int(conn.execute(
                    """INSERT INTO messages(ts,direction,kind,text,meta)
                       VALUES(?,'in','user',?,'{}')""",
                    (stamp, f"synthetic-{index}"),
                ).lastrowid))
            memory_id = int(conn.execute(
                """INSERT INTO memory_items
                   (memory_key,kind,scope_type,scope_ref,normalized_content,
                    normalized_fingerprint,fingerprint_version,status,explicitness,
                    confidence,sensitivity,first_observed_at,last_confirmed_at,
                    superseded_by_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,1,'candidate','inferred',0.0,'normal',
                          ?,?,NULL,?,?)""",
                (
                    "C" * 32,
                    "project",
                    "global_user",
                    "",
                    "synthetic candidate",
                    b"c" * 32,
                    stamp,
                    stamp,
                    stamp,
                    stamp,
                ),
            ).lastrowid)
            source_values = (
                memory_id,
                message_ids[0],
                "project_fact",
                0,
                9,
                "memory-formation-v1",
                "memory-formation-extractor-v1",
                stamp,
            )
            conn.execute(
                """INSERT INTO memory_candidate_sources
                   (memory_id,canonical_message_id,signal_type,span_start,span_end,
                    formation_contract_version,extractor_contract_version,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                source_values,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO memory_candidate_sources
                       (memory_id,canonical_message_id,signal_type,span_start,span_end,
                        formation_contract_version,extractor_contract_version,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    source_values,
                )
            invalid_sources = (
                ("project_fact", -1, 9, "memory-formation-v1"),
                ("project_fact", 0.5, 9, "memory-formation-v1"),
                ("project_fact", 9, 9, "memory-formation-v1"),
                ("unknown", 0, 9, "memory-formation-v1"),
                ("project_fact", 0, 9, "unsafe/version"),
            )
            for offset, (signal, start, end, formation_version) in enumerate(
                invalid_sources,
                start=1,
            ):
                with self.subTest(signal=signal, start=start, end=end), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    conn.execute(
                        """INSERT INTO memory_candidate_sources
                           (memory_id,canonical_message_id,signal_type,span_start,span_end,
                            formation_contract_version,extractor_contract_version,created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            memory_id,
                            message_ids[offset],
                            signal,
                            start,
                            end,
                            formation_version,
                            "memory-formation-extractor-v1",
                            stamp,
                        ),
                    )

            valid_run = (
                message_ids[0],
                "a" * 64,
                2,
                1,
                1,
                0,
                0,
                0,
                "memory-formation-v1",
                "memory-formation-extractor-v1",
                stamp,
            )
            conn.execute(
                """INSERT INTO memory_auto_formation_runs
                   (canonical_message_id,proposal_digest,proposal_count,candidate_count,
                    created_count,existing_candidate_count,active_duplicate_count,
                    suppressed_count,formation_contract_version,
                    extractor_contract_version,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                valid_run,
            )
            immutable_operations = (
                (
                    "candidate-source-update",
                    """UPDATE memory_candidate_sources
                       SET span_end=8 WHERE memory_id=?""",
                    (memory_id,),
                    "memory_candidate_source_immutable",
                ),
                (
                    "candidate-source-delete",
                    "DELETE FROM memory_candidate_sources WHERE memory_id=?",
                    (memory_id,),
                    "memory_candidate_source_immutable",
                ),
                (
                    "formation-run-update",
                    """UPDATE memory_auto_formation_runs
                       SET proposal_count=3 WHERE canonical_message_id=?""",
                    (message_ids[0],),
                    "memory_auto_formation_run_immutable",
                ),
                (
                    "formation-run-delete",
                    """DELETE FROM memory_auto_formation_runs
                       WHERE canonical_message_id=?""",
                    (message_ids[0],),
                    "memory_auto_formation_run_immutable",
                ),
            )
            for name, statement, parameters, category in immutable_operations:
                with self.subTest(operation=name), self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    f"^{category}$",
                ):
                    conn.execute(statement, parameters)
            invalid_runs = (
                ("A" * 64, 0, 0, 0, 0, 0, 0),
                ("b" * 64, 4, 0, 0, 0, 0, 0),
                ("c" * 64, 0, 1, 1, 0, 0, 0),
                ("d" * 64, 2, 2, 1, 0, 0, 0),
                ("e" * 64, 1, 1, 0.5, 0, 0, 0.5),
            )
            for offset, values in enumerate(invalid_runs, start=1):
                with self.subTest(run=offset), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    conn.execute(
                        """INSERT INTO memory_auto_formation_runs
                           (canonical_message_id,proposal_digest,proposal_count,
                            candidate_count,created_count,existing_candidate_count,
                            active_duplicate_count,suppressed_count,
                            formation_contract_version,extractor_contract_version,
                            created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            message_ids[offset],
                            *values,
                            "memory-formation-v1",
                            "memory-formation-extractor-v1",
                            stamp,
                        ),
                    )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM memory_items WHERE id=?",
                    (memory_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM messages WHERE id=?",
                    (message_ids[0],),
                )

    def test_failed_v9_rolls_back_both_tables_indexes_and_marker(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:8])

        def broken(conn):
            channel_store._migration_009(conn)
            raise RuntimeError("injected-v9")

        migrations = (
            *channel_store.MIGRATIONS[:8],
            (
                9,
                "automatic_memory_candidate_persistence_foundation",
                broken,
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "^injected-v9$"):
            channel_store.run_migrations(self.path, migrations)
        with channel_store.connect(self.path) as conn:
            objects = conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE name LIKE 'memory_candidate_%'
                      OR name LIKE 'memory_auto_formation_%'
                      OR name LIKE 'idx_memory_candidate_%'"""
            ).fetchall()
            marker = conn.execute(
                "SELECT status FROM schema_migrations WHERE version=9"
            ).fetchone()
            v8 = conn.execute(
                "SELECT status FROM schema_migrations WHERE version=8"
            ).fetchone()
        self.assertEqual(objects, [])
        self.assertIsNone(marker)
        self.assertEqual(v8[0], "applied")

    def test_v9_validator_rejects_check_index_fk_and_object_tampering(self):
        corruptions = (
            (
                "index",
                "DROP INDEX idx_memory_candidate_sources_memory",
            ),
            (
                "check",
                """PRAGMA writable_schema=ON;
                   UPDATE sqlite_master
                   SET sql=replace(sql,'span_start>=0','span_start>=-1')
                   WHERE type='table' AND name='memory_candidate_sources';
                   PRAGMA writable_schema=OFF""",
            ),
            (
                "foreign-key",
                """PRAGMA writable_schema=ON;
                   UPDATE sqlite_master
                   SET sql=replace(sql,'ON DELETE RESTRICT','ON DELETE CASCADE')
                   WHERE type='table' AND name='memory_auto_formation_runs';
                   PRAGMA writable_schema=OFF""",
            ),
            (
                "missing-trigger",
                "DROP TRIGGER memory_candidate_sources_immutable_update",
            ),
            (
                "tampered-trigger",
                """DROP TRIGGER memory_auto_formation_runs_immutable_delete;
                   CREATE TRIGGER memory_auto_formation_runs_immutable_delete
                   BEFORE DELETE ON memory_auto_formation_runs
                   BEGIN
                     SELECT RAISE(ABORT,'wrong_category');
                   END""",
            ),
            (
                "extra-trigger",
                """CREATE TRIGGER unreviewed_candidate_trigger
                   AFTER INSERT ON memory_candidate_sources
                   BEGIN SELECT 1; END""",
            ),
        )
        for name, script in corruptions:
            with self.subTest(name=name):
                path = str(Path(self.temp.name) / f"tamper-{name}.sqlite3")
                with channel_store.connect(path) as conn:
                    conn.execute("""CREATE TABLE messages(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                        text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
                channel_store.run_migrations(path)
                with channel_store.connect(path) as conn:
                    conn.executescript(script)
                with channel_store.connect(path) as conn, self.assertRaises(
                    sqlite3.DatabaseError
                ):
                    channel_store.validate_memory_candidate_persistence_schema(
                        conn
                    )

    def test_v6_relational_rows_are_preserved(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:6])
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO channel_accounts
                   (channel,external_account_id,status,created_at,updated_at)
                   VALUES('telegram','synthetic-account','active',?,?)""",
                (stamp, stamp),
            )
            before = {
                row["name"]: row["sql"] for row in conn.execute(
                    """SELECT name,sql FROM sqlite_master
                       WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"""
                )
            }
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM channel_accounts").fetchone()[0], 1
            )
            after_old = {
                row["name"]: row["sql"] for row in conn.execute(
                    """SELECT name,sql FROM sqlite_master
                       WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'
                         AND name NOT LIKE 'memory_%' AND name NOT LIKE 'idx_memory_%'"""
                )
            }
        self.assertEqual(after_old, before)

    def test_failed_v7_rolls_back_tables_indexes_and_marker(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:6])

        def broken(conn):
            channel_store._migration_007(conn)
            raise RuntimeError("injected")

        migrations = (*channel_store.MIGRATIONS[:6], (7, "explicit_memory_core_foundation", broken))
        with self.assertRaisesRegex(RuntimeError, "injected"):
            channel_store.run_migrations(self.path, migrations)
        with channel_store.connect(self.path) as conn:
            objects = conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE name LIKE 'memory_%' OR name LIKE 'idx_memory_%'"""
            ).fetchall()
            marker = conn.execute(
                "SELECT status FROM schema_migrations WHERE version=7"
            ).fetchone()
        self.assertEqual(objects, [])
        self.assertIsNone(marker)

    def test_validator_rejects_structural_corruption(self):
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            conn.execute("DROP INDEX idx_memory_items_live_fingerprint")
        with channel_store.connect(self.path) as conn, self.assertRaisesRegex(
            sqlite3.DatabaseError, "memory index"
        ):
            channel_store.validate_memory_schema(conn)
        with self.assertRaises(sqlite3.DatabaseError):
            channel_store.run_migrations(self.path)

    def test_validator_rejects_missing_or_modified_immutability_trigger(self):
        for index, statement in enumerate((
            "DROP TRIGGER memory_evidence_events_immutable_update",
            """DROP TRIGGER memory_evidence_events_immutable_delete;
               CREATE TRIGGER memory_evidence_events_immutable_delete
               BEFORE DELETE ON memory_evidence_events
               BEGIN SELECT RAISE(ABORT,'wrong_category'); END""",
        )):
            with self.subTest(index=index):
                path = str(Path(self.temp.name) / f"trigger-{index}.sqlite3")
                with channel_store.connect(path) as conn:
                    conn.execute("""CREATE TABLE messages(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                        text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
                channel_store.run_migrations(path)
                with channel_store.connect(path) as conn:
                    conn.executescript(statement)
                with channel_store.connect(path) as conn, self.assertRaisesRegex(
                    sqlite3.DatabaseError, "memory trigger",
                ):
                    channel_store.validate_memory_schema(conn)

    def test_checks_partial_unique_and_restrict_fks_are_enforced(self):
        channel_store.run_migrations(self.path)
        stamp = channel_store.now_iso()
        digest = b"a" * 32
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,'in','user','synthetic','{"channel":"web","source":"relay"}')""",
                (stamp,),
            )
            conn.execute(
                """INSERT INTO memory_items
                   (memory_key,kind,scope_type,scope_ref,normalized_content,
                    normalized_fingerprint,fingerprint_version,status,explicitness,
                    confidence,sensitivity,first_observed_at,last_confirmed_at,
                    superseded_by_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,1,'active','explicit',1.0,'normal',?,?,NULL,?,?)""",
                ("A" * 32, "project", "global_user", "", "synthetic", digest, stamp, stamp, stamp, stamp),
            )
            memory_id = conn.execute(
                "SELECT id FROM memory_items WHERE memory_key=?", ("A" * 32,)
            ).fetchone()[0]
            message_id = conn.execute("SELECT id FROM messages").fetchone()[0]
            evidence_cursor = conn.execute(
                """INSERT INTO memory_evidence_events
                   (canonical_message_id,action_id,action_type,action_binding_version,
                    evidence_type,reality_scope,subject_scope,
                    created_by_component,created_at)
                   VALUES(?,?,'remember_explicit_user',1,
                          'explicit_user_memory','real','user','memory_admin',?)""",
                (message_id, "A" * 32, stamp),
            )
            conn.execute(
                """INSERT INTO memory_sources
                   (memory_id,evidence_event_id,canonical_message_id,channel,source,
                    evidence_role,evidence_type,created_at)
                   VALUES(?,?,?,'web','relay','user','explicit_user_memory',?)""",
                (memory_id, int(evidence_cursor.lastrowid), message_id, stamp),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO memory_items
                       (memory_key,kind,scope_type,scope_ref,normalized_content,
                        normalized_fingerprint,fingerprint_version,status,explicitness,
                        confidence,sensitivity,first_observed_at,last_confirmed_at,
                        superseded_by_id,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,1,'candidate','explicit',1.0,'normal',?,?,NULL,?,?)""",
                    ("B" * 32, "project", "global_user", "", "duplicate", digest, stamp, stamp, stamp, stamp),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE memory_items SET status='forgotten' WHERE id=?", (memory_id,)
                )

    def test_v10_database_remains_compatible_with_old_migration_paths(self):
        channel_store.run_migrations(self.path)
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:6])
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:7])
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 10
            )
            channel_store.validate_kelivo_schema(conn)
            channel_store.validate_heartbeat_schema(conn)
            channel_store.validate_heartbeat_hardening_schema(conn)
            channel_store.validate_memory_schema(conn)
            channel_store.validate_memory_action_schema(conn)
            channel_store.validate_memory_candidate_persistence_schema(conn)
            channel_store.validate_memory_candidate_decision_schema_v1_v10(
                conn
            )

    def test_v1_through_v6_migration_identity_is_unchanged(self):
        self.assertEqual(
            tuple((version, name) for version, name, _apply in channel_store.MIGRATIONS[:6]),
            (
                (1, "telegram_private_text_mvp"),
                (2, "telegram_reliability"),
                (3, "kelivo_nonstream_foundation"),
                (4, "kelivo_automatic_idempotency"),
                (5, "dylan_heartbeat_foundation"),
                (6, "dylan_heartbeat_hardening"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
