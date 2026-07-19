import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import channel_store


class KelivoMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "kelivo.sqlite3")
        self.corrupt_count = 0

    def corrupted_schema(self, transform_table=lambda name, sql: sql,
                         transform_index=lambda name, sql: sql):
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as source:
            tables = source.execute(
                """SELECT name,sql FROM sqlite_master WHERE type='table' AND name IN
                   ('kelivo_clients','kelivo_requests','companion_context_snapshots','kelivo_rate_limits')
                   ORDER BY CASE name WHEN 'kelivo_clients' THEN 1 WHEN 'kelivo_requests' THEN 2 ELSE 3 END"""
            ).fetchall()
            indexes = source.execute(
                """SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL AND name IN
                   ('idx_kelivo_requests_status','idx_kelivo_rate_limits_window',
                    'idx_context_snapshots_lookup','idx_context_snapshots_one_active',
                    'idx_kelivo_requests_automatic')"""
            ).fetchall()
        self.corrupt_count += 1
        target_path = str(Path(self.temp.name) / f"corrupt-{self.corrupt_count}.sqlite3")
        with channel_store.connect(target_path) as target:
            target.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY)")
            for row in tables:
                sql = transform_table(row["name"], row["sql"])
                if sql:
                    target.execute(sql)
            for row in indexes:
                sql = transform_index(row["name"], row["sql"])
                if sql:
                    target.execute(sql)
        return target_path

    def test_latest_upgrades_empty_database_and_is_repeatable(self):
        channel_store.run_migrations(self.path)
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(versions, [1, 2, 3, 4, 5])
        self.assertTrue({"kelivo_clients", "kelivo_requests", "companion_context_snapshots",
                         "kelivo_rate_limits"}.issubset(tables))

    def test_applied_marker_does_not_hide_structural_corruption(self):
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            conn.execute("DROP INDEX idx_context_snapshots_one_active")
        with self.assertRaisesRegex(sqlite3.DatabaseError, "snapshot"):
            channel_store.run_migrations(self.path)

    def test_validator_rejects_missing_and_wrong_ordinary_indexes(self):
        missing = self.corrupted_schema(
            transform_index=lambda name, sql: None if name == "idx_kelivo_requests_status" else sql
        )
        with channel_store.connect(missing) as conn, self.assertRaisesRegex(sqlite3.DatabaseError, "invalid kelivo index set"):
            channel_store.validate_kelivo_schema(conn)
        wrong = self.corrupted_schema(
            transform_index=lambda name, sql: sql.replace(
                "status,dispatch_expires_at,id", "dispatch_expires_at,status,id"
            ) if name == "idx_kelivo_requests_status" else sql
        )
        with channel_store.connect(wrong) as conn, self.assertRaisesRegex(sqlite3.DatabaseError, "kelivo index"):
            channel_store.validate_kelivo_schema(conn)

    def test_validator_rejects_unique_partial_check_fk_table_and_column_corruption(self):
        cases = {
            "generation": (
                lambda name, sql: sql.replace("generation_id TEXT NOT NULL UNIQUE", "generation_id TEXT NOT NULL")
                if name == "kelivo_requests" else sql, lambda name, sql: sql,
            ),
            "partial": (
                lambda name, sql: sql,
                lambda name, sql: None if name == "idx_context_snapshots_one_active" else sql,
            ),
            "check": (
                lambda name, sql: sql.replace("CHECK(history_before_id >= 0)", "")
                if name == "kelivo_requests" else sql, lambda name, sql: sql,
            ),
            "foreign key": (
                lambda name, sql: sql.replace(
                    "FOREIGN KEY(assistant_message_id) REFERENCES messages(id)",
                    "FOREIGN KEY(assistant_message_id) REFERENCES kelivo_clients(client_id)",
                ) if name == "kelivo_requests" else sql, lambda name, sql: sql,
            ),
            "table": (
                lambda name, sql: None if name == "kelivo_rate_limits" else sql,
                lambda name, sql: None if name == "idx_kelivo_rate_limits_window" else sql,
            ),
            "columns": (
                lambda name, sql: sql.replace("persona_source TEXT", "persona_origin TEXT")
                if name == "kelivo_requests" else sql, lambda name, sql: sql,
            ),
        }
        for label, (table_transform, index_transform) in cases.items():
            with self.subTest(label=label):
                path = self.corrupted_schema(table_transform, index_transform)
                with channel_store.connect(path) as conn, self.assertRaises(sqlite3.DatabaseError):
                    channel_store.validate_kelivo_schema(conn)

    def test_validator_rejects_broadened_checks_partial_indexes_uniqueness_and_fks(self):
        table_cases = {
            "client key ordinary": lambda name, sql: sql.replace(
                "UNIQUE(client_id,idempotency_key)", "CHECK(length(client_id)>=0)"
            ) if name == "kelivo_requests" else sql,
            "generation ordinary": lambda name, sql: sql.replace(
                "generation_id TEXT NOT NULL UNIQUE", "generation_id TEXT NOT NULL"
            ) if name == "kelivo_requests" else sql,
            "check or true": lambda name, sql: sql.replace(
                "CHECK(history_before_id >= 0)", "CHECK(history_before_id >= 0 OR 1=1)"
            ) if name == "kelivo_requests" else sql,
            "extra status": lambda name, sql: sql.replace(
                "'failed','completed'))", "'failed','completed','anything'))"
            ) if name == "kelivo_requests" else sql,
            "fk target column": lambda name, sql: sql.replace(
                "REFERENCES messages(id)", "REFERENCES messages(missing)", 1
            ) if name == "kelivo_requests" else sql,
            "fk delete": lambda name, sql: sql.replace(
                "REFERENCES messages(id)", "REFERENCES messages(id) ON DELETE CASCADE", 1
            ) if name == "kelivo_requests" else sql,
            "fk update": lambda name, sql: sql.replace(
                "REFERENCES messages(id)", "REFERENCES messages(id) ON UPDATE CASCADE", 1
            ) if name == "kelivo_requests" else sql,
        }
        for label, transform in table_cases.items():
            with self.subTest(label=label):
                path = self.corrupted_schema(transform_table=transform)
                with channel_store.connect(path) as conn, self.assertRaises(sqlite3.DatabaseError):
                    channel_store.validate_kelivo_schema(conn)

        partials = ("active=0 OR active=1", "active IN (0,1)", "active=1 OR 1=1")
        for expression in partials:
            with self.subTest(partial=expression):
                path = self.corrupted_schema(transform_index=lambda name, sql: sql.replace(
                    "active=1", expression
                ) if name == "idx_context_snapshots_one_active" else sql)
                with channel_store.connect(path) as conn, self.assertRaises(sqlite3.DatabaseError):
                    channel_store.validate_kelivo_schema(conn)

        unique_ordinary = self.corrupted_schema(transform_index=lambda name, sql: sql.replace(
            "CREATE INDEX", "CREATE UNIQUE INDEX"
        ) if name == "idx_kelivo_requests_status" else sql)
        with channel_store.connect(unique_ordinary) as conn, self.assertRaises(sqlite3.DatabaseError):
            channel_store.validate_kelivo_schema(conn)

    def test_validator_accepts_only_lexically_equivalent_case_whitespace_and_comments(self):
        path = self.corrupted_schema(
            transform_table=lambda _name, sql: "-- canonical comment\n" + sql.replace("CREATE TABLE", "create table"),
            transform_index=lambda _name, sql: "/* canonical */\n" + sql.replace("CREATE", "create"),
        )
        with channel_store.connect(path) as conn:
            channel_store.validate_kelivo_schema(conn)

    def test_applied_v3_marker_with_missing_table_or_column_fails_startup_validation(self):
        for label, transform_table, transform_index in (
            ("table", lambda name, sql: None if name == "kelivo_rate_limits" else sql,
             lambda name, sql: None if name == "idx_kelivo_rate_limits_window" else sql),
            ("column", lambda name, sql: sql.replace("persona_source TEXT", "persona_origin TEXT")
             if name == "kelivo_requests" else sql, lambda name, sql: sql),
        ):
            with self.subTest(label=label):
                path = self.corrupted_schema(transform_table, transform_index)
                with channel_store.connect(path) as conn:
                    conn.execute("""CREATE TABLE schema_migrations(
                        version INTEGER PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL,
                        created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
                    stamp = channel_store.now_iso()
                    conn.execute("INSERT INTO schema_migrations VALUES(3,?,?,?,?)",
                                 ("kelivo_nonstream_foundation", "applied", stamp, stamp))
                with self.assertRaises(sqlite3.DatabaseError):
                    channel_store.run_migrations(path, channel_store.MIGRATIONS[2:])

    def test_actual_v3_apply_exception_rolls_back_schema_marker_and_preserves_v2(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:2])
        with channel_store.connect(self.path) as conn:
            stamp = channel_store.now_iso()
            conn.execute(
                "INSERT INTO channel_accounts(channel,external_account_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("telegram", "rollback-bot", "active", stamp, stamp),
            )
        def broken_v3(conn):
            channel_store._migration_003(conn)
            raise RuntimeError("injected v3 failure")
        migrations = (*channel_store.MIGRATIONS[:2], (3, "kelivo_nonstream_foundation", broken_v3))
        with self.assertRaisesRegex(RuntimeError, "injected"):
            channel_store.run_migrations(self.path, migrations)
        with channel_store.connect(self.path) as conn:
            kelivo_tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'kelivo_%'"
            ).fetchall()
            marker = conn.execute("SELECT status FROM schema_migrations WHERE version=3").fetchone()
            bot = conn.execute("SELECT external_account_id FROM channel_accounts").fetchone()[0]
        self.assertEqual(kelivo_tables, [])
        self.assertIsNone(marker)
        self.assertEqual(bot, "rollback-bot")

    def test_v3_request_data_is_preserved_and_marked_explicit_by_v4(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:3])
        with channel_store.connect(self.path) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)""")
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('client','session',1,1,?,?)""", (stamp, stamp),
            )
            conn.execute(
                """INSERT INTO kelivo_requests
                   (idempotency_key,request_payload_hash,request_identity_hash,client_id,api_session,
                    mapping_revision,history_before_id,context_bundle_json,context_bundle_hash,
                    provider_messages_json,prompt_contract_version,persona_hash,persona_source,
                    provider_model,effective_temperature,effective_max_tokens,status,generation_id,
                    created_at,updated_at)
                   VALUES('explicit-key-0001','payload','identity','client','session',1,0,
                    '{}','bundle','[]','contract','persona','test','provider',0.7,100,
                    'prepared','generation-v3',?,?)""", (stamp, stamp),
            )
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            row = conn.execute("SELECT * FROM kelivo_requests").fetchone()
            versions = [item[0] for item in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )]
        self.assertEqual(versions, [1, 2, 3, 4, 5])
        self.assertEqual((row["idempotency_key"], row["generation_id"], row["idempotency_mode"]),
                         ("explicit-key-0001", "generation-v3", "explicit"))
        self.assertIsNone(row["automatic_fingerprint"])
        self.assertIsNone(row["automatic_replay_until"])

    def test_actual_v4_apply_exception_rolls_back_schema_marker_and_preserves_v3(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:3])
        with channel_store.connect(self.path) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)""")
            stamp = channel_store.now_iso()
            conn.execute(
                "INSERT INTO channel_accounts(channel,external_account_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("telegram", "rollback-v4-bot", "active", stamp, stamp),
            )
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('client','session',1,1,?,?)""", (stamp, stamp),
            )
            conn.execute(
                """INSERT INTO kelivo_requests
                   (idempotency_key,request_payload_hash,request_identity_hash,client_id,api_session,
                    mapping_revision,history_before_id,context_bundle_json,context_bundle_hash,
                    provider_messages_json,prompt_contract_version,persona_hash,persona_source,
                    provider_model,effective_temperature,effective_max_tokens,status,generation_id,
                    created_at,updated_at)
                   VALUES('rollback-key-0001','payload','identity','client','session',1,0,
                    '{}','bundle','[]','contract','persona','test','provider',0.7,100,
                    'prepared','rollback-generation',?,?)""", (stamp, stamp),
            )
        def broken_v4(conn):
            channel_store._migration_004(conn)
            raise RuntimeError("injected v4 failure")
        migrations = (*channel_store.MIGRATIONS[:3], (4, "kelivo_automatic_idempotency", broken_v4))
        with self.assertRaisesRegex(RuntimeError, "injected v4"):
            channel_store.run_migrations(self.path, migrations)
        with channel_store.connect(self.path) as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_xinfo(kelivo_requests)")}
            marker = conn.execute("SELECT status FROM schema_migrations WHERE version=4").fetchone()
            bot = conn.execute("SELECT external_account_id FROM channel_accounts").fetchone()[0]
            request = conn.execute("SELECT idempotency_key,generation_id FROM kelivo_requests").fetchone()
            channel_store.validate_kelivo_schema(conn, version=3)
        self.assertNotIn("idempotency_mode", columns)
        self.assertIsNone(marker)
        self.assertEqual(bot, "rollback-v4-bot")
        self.assertEqual(tuple(request), ("rollback-key-0001", "rollback-generation"))

    def test_v4_validator_rejects_automatic_constraint_and_index_corruption(self):
        broken_check = self.corrupted_schema(transform_table=lambda name, sql: sql.replace(
            "idempotency_mode='automatic' AND automatic_fingerprint IS NOT NULL",
            "idempotency_mode='automatic'",
        ) if name == "kelivo_requests" else sql)
        with channel_store.connect(broken_check) as conn, self.assertRaises(sqlite3.DatabaseError):
            channel_store.validate_kelivo_schema(conn)
        broken_index = self.corrupted_schema(transform_index=lambda name, sql: sql.replace(
            "client_id,automatic_fingerprint,created_at,status",
            "client_id,automatic_fingerprint,status,created_at",
        ) if name == "idx_kelivo_requests_automatic" else sql)
        with channel_store.connect(broken_index) as conn, self.assertRaises(sqlite3.DatabaseError):
            channel_store.validate_kelivo_schema(conn)

    def test_v2_data_is_preserved(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:2])
        with channel_store.connect(self.path) as conn:
            stamp = channel_store.now_iso()
            conn.execute(
                "INSERT INTO channel_accounts(channel,external_account_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("telegram", "existing-bot", "active", stamp, stamp),
            )
            before_indexes = {row["name"]: row["sql"] for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name NOT LIKE 'kelivo_%'"
            )}
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            row = conn.execute("SELECT external_account_id FROM channel_accounts").fetchone()
            after_indexes = {item["name"]: item["sql"] for item in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index'"
            ) if item["name"] in before_indexes}
        self.assertEqual(row[0], "existing-bot")
        self.assertEqual(after_indexes, before_indexes)

    def test_full_relational_v2_dataset_is_preserved_by_v3(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:2])
        with channel_store.connect(self.path) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, direction TEXT NOT NULL,
                kind TEXT NOT NULL, text TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}')""")
        queued = channel_store.enqueue_telegram_update(
            self.path, account_id="bot-v2", update_id="update-v2", chat_id="chat-v2",
            user_id="user-v2", external_message_id="message-v2", text="hello v2",
            rate_limit=10, rate_window_seconds=60,
        )
        job = channel_store.claim_generation_job(self.path)
        dispatched = channel_store.start_generation_dispatch(self.path, job["id"])
        channel_store.finish_generation_dispatch(self.path, job["id"], "awaiting_reply")
        completed = channel_store.complete_telegram_generation(self.path, meta={
            "generation_id": dispatched["generation_id"], "stream_id": dispatched["stream_id"],
            "api_session": queued["api_session"], "reply_to": str(queued["canonical_message_id"]),
            "channel": "telegram", "channel_account": "bot-v2", "channel_conversation": "chat-v2",
        }, text="reply v2")
        self.assertIsNotNone(completed)
        core_tables = (
            "channel_accounts", "channel_conversations", "inbound_events", "external_messages",
            "generation_jobs", "messages", "delivery_attempts", "delivery_parts",
            "telegram_completions", "channel_rate_limits", "channel_audit_events",
        )
        with channel_store.connect(self.path) as conn:
            before = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in core_tables}
            identities = {
                "session": conn.execute("SELECT api_session FROM channel_conversations").fetchone()[0],
                "generation": conn.execute("SELECT generation_id FROM generation_jobs").fetchone()[0],
                "message": conn.execute("SELECT canonical_message_id FROM external_messages WHERE direction='in'").fetchone()[0],
            }
        channel_store.run_migrations(self.path)
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            after = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in core_tables}
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("SELECT api_session FROM channel_conversations").fetchone()[0],
                             identities["session"])
            self.assertEqual(conn.execute("SELECT generation_id FROM generation_jobs").fetchone()[0],
                             identities["generation"])
        self.assertEqual(before, after)

    def test_corrupt_partial_migration_state_fails_safe(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:2])
        with channel_store.connect(self.path) as conn:
            stamp = channel_store.now_iso()
            conn.execute(
                "INSERT INTO schema_migrations(version,name,status,created_at,updated_at) VALUES(3,?,?,?,?)",
                ("kelivo_nonstream_foundation", "applying", stamp, stamp),
            )
        with self.assertRaises(sqlite3.DatabaseError):
            channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT status FROM schema_migrations WHERE version=3").fetchone()[0], "applying")
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='kelivo_clients'"
            ).fetchone())

    def test_v5_uses_explicit_heartbeat_table_names(self):
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"heartbeat_state", "heartbeat_runs", "journal_entries", "timeline_events"} <= tables)
        self.assertFalse({"heartbeat", "journal", "timeline"} & tables)


if __name__ == "__main__":
    unittest.main()
