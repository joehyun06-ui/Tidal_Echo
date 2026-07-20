import json
from concurrent.futures import ThreadPoolExecutor
import tempfile
import time
import unittest
from pathlib import Path

from backend import channel_store, kelivo_service


class KelivoContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "context.sqlite3")
        channel_store.run_migrations(self.path)

    def test_hash_is_stable_across_key_order_and_preserves_strings(self):
        first = {"b": 2, "a": "line  \r\nnext\r"}
        second = {"a": "line  \r\nnext\r", "b": 2}
        self.assertEqual(kelivo_service.content_hash(first), kelivo_service.content_hash(second))
        validated = kelivo_service.validate_completion({
            "model": "ouou-home", "messages": [{"role": "user", "content": " line  \r\nnext \r"}]
        }, "ouou-home")
        self.assertEqual(validated.user_text, " line  \r\nnext \r")

    def test_temperature_zero_is_canonical_and_nonfinite_values_are_rejected(self):
        base = {"model": "ouou-home", "messages": [{"role": "user", "content": "x"}]}
        integer = kelivo_service.validate_completion({**base, "temperature": 0}, "ouou-home")
        negative = kelivo_service.validate_completion({**base, "temperature": -0.0}, "ouou-home")
        self.assertEqual(integer.temperature, 0.0)
        self.assertEqual(integer.request_payload_hash, negative.request_payload_hash)
        for value in (float("nan"), float("inf"), float("-inf"), -0.01, 2.01):
            with self.subTest(value=value), self.assertRaisesRegex(
                kelivo_service.KelivoError, "invalid_temperature"
            ):
                kelivo_service.validate_completion({**base, "temperature": value}, "ouou-home")

    def test_stream_options_canonicalize_out_of_payload_and_identity_hashes(self):
        base = {"model": "ouou-home", "messages": [{"role": "user", "content": "same"}]}
        payloads = (
            base,
            {**base, "stream_options": None},
            {**base, "stream_options": {}},
            {**base, "stream_options": {"include_usage": True}},
            {**base, "stream_options": {"include_usage": False}},
        )
        validated = [kelivo_service.validate_completion(payload, "ouou-home") for payload in payloads]
        self.assertEqual(len({item.request_payload_hash for item in validated}), 1)
        self.assertTrue(all("stream_options" not in item.normalized_request for item in validated))
        self.assertTrue(all(not item.snapshots for item in validated))
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('a','s',1,1,?,?)""", (stamp, stamp),
            )
        for index, item in enumerate(validated):
            kelivo_service.prepare_request(
                self.path, "a", f"stream-options-hash-{index:04d}", item,
                persona_text="persona", provider_model="provider-a",
                effective_temperature=0.0, effective_max_tokens=77,
            )
        with channel_store.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT request_identity_hash,provider_messages_json,context_bundle_json "
                "FROM kelivo_requests ORDER BY id"
            ).fetchall()
            snapshots = conn.execute("SELECT count(*) FROM companion_context_snapshots").fetchone()[0]
        self.assertEqual(len({row["request_identity_hash"] for row in rows}), 1)
        self.assertTrue(all("stream_options" not in row["provider_messages_json"] for row in rows))
        self.assertTrue(all("stream_options" not in row["context_bundle_json"] for row in rows))
        self.assertEqual(snapshots, 0)

    def test_identical_snapshot_is_deduplicated(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            one = kelivo_service.store_snapshot(conn, "session-1", "system", [{"content": "persona"}])
            two = kelivo_service.store_snapshot(conn, "session-1", "system", [{"content": "persona"}])
            conn.execute("COMMIT")
            count = conn.execute("SELECT count(*) FROM companion_context_snapshots").fetchone()[0]
        self.assertEqual(one["id"], two["id"])
        self.assertEqual(count, 1)

    def test_new_snapshot_deactivates_old_version(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = kelivo_service.store_snapshot(conn, "session-1", "system", [{"content": "old"}])
            new = kelivo_service.store_snapshot(conn, "session-1", "system", [{"content": "new"}])
            conn.execute("COMMIT")
            rows = conn.execute(
                "SELECT id,active,version FROM companion_context_snapshots ORDER BY version"
            ).fetchall()
        self.assertEqual([(row["active"], row["version"]) for row in rows], [(0, 1), (1, 2)])
        self.assertNotEqual(old["id"], new["id"])

    def test_active_context_is_shared_structured_interface(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            kelivo_service.store_snapshot(conn, "session-1", "tools", [{"type": "function"}])
            conn.execute("COMMIT")
        self.assertEqual(kelivo_service.active_context(self.path, "session-1"),
                         {"tools": [{"type": "function"}]})

    def test_validation_separates_known_context_without_guessing_categories(self):
        result = kelivo_service.validate_completion({
            "model": "ouou-home",
            "messages": [
                {"role": "system", "content": "persona and world book"},
                {"role": "developer", "content": "developer rule"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "current turn"},
            ],
        }, "ouou-home")
        self.assertEqual(result.user_text, "current turn")
        self.assertEqual([item[0] for item in result.snapshots], ["system", "developer"])
        self.assertNotIn("memory", dict(result.snapshots))
        json.loads(kelivo_service.normalized_json(result.normalized_request))

    def test_prepare_freezes_mapping_boundary_context_and_prompt(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('client-a','session-a',1,3,?,?)""", (stamp, stamp),
            )
            conn.execute(
                "INSERT INTO messages VALUES(7,?,'in','user','old','{\"api_session\":\"session-a\"}')", (stamp,),
            )
        validated = kelivo_service.validate_completion({
            "model": "ouou-home", "messages": [
                {"role": "system", "content": "  exact persona  "},
                {"role": "assistant", "content": "old"},
                {"role": "user", "content": "  exact user  "},
            ],
        }, "ouou-home")
        prepared = kelivo_service.prepare_request(
            self.path, "client-a", "freeze-key-0001", validated, rate_limit=10,
        )
        with channel_store.connect(self.path) as conn:
            kelivo_service.store_snapshot(conn, "session-a", "system", [{"content": "changed"}])
            row = conn.execute("SELECT * FROM kelivo_requests").fetchone()
        self.assertEqual(prepared.messages[-1]["content"], "  exact user  ")
        self.assertEqual(row["mapping_revision"], 3)
        self.assertEqual(row["history_before_id"], 7)
        self.assertEqual(json.loads(row["context_bundle_json"])["snapshots"]["system"]["value"][0]["content"],
                         "  exact persona  ")
        self.assertEqual(row["context_bundle_hash"], kelivo_service.content_hash(
            json.loads(row["context_bundle_json"]))[1])

    def test_new_request_does_not_inherit_old_active_snapshot(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('a','shared',1,1,?,?)""", (stamp, stamp),
            )
        first = kelivo_service.validate_completion({
            "model": "ouou-home", "messages": [
                {"role": "system", "content": "old system"},
                {"role": "user", "content": "one"},
            ],
        }, "ouou-home")
        second = kelivo_service.validate_completion({
            "model": "ouou-home", "messages": [{"role": "user", "content": "two"}],
        }, "ouou-home")
        kelivo_service.prepare_request(self.path, "a", "snapshot-key-0001", first)
        prepared = kelivo_service.prepare_request(self.path, "a", "snapshot-key-0002", second)
        self.assertIsNone(prepared.context_bundle["snapshots"]["system"])
        self.assertEqual(list(prepared.messages), [{"role": "user", "content": "two"}])
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM companion_context_snapshots WHERE active=1"
            ).fetchone()[0], 1)

    def test_request_identity_hash_covers_exact_frozen_provider_contract(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('a','s',1,2,?,?)""", (stamp, stamp),
            )
        validated = kelivo_service.validate_completion({
            "model": "ouou-home", "messages": [{"role": "user", "content": " hello "}],
            "temperature": 0.3, "max_tokens": 99,
        }, "ouou-home")
        prepared = kelivo_service.prepare_request(
            self.path, "a", "hash-fields-key-0001", validated,
            persona_text=" exact persona ", persona_source="test",
            provider_model="provider-exact", effective_temperature=0.3,
            effective_max_tokens=99,
        )
        with channel_store.connect(self.path) as conn:
            row = conn.execute("SELECT * FROM kelivo_requests").fetchone()
        bundle = json.loads(row["context_bundle_json"])
        expected = kelivo_service.build_request_identity_hash(
            virtual_model="ouou-home", provider_model="provider-exact", client_id="a",
            api_session="s", mapping_revision=2,
            persona_hash=kelivo_service.text_sha256(" exact persona "),
            snapshot_correlations={"system": None, "developer": None},
            provider_messages=prepared.messages, effective_temperature=0.3,
            effective_max_tokens=99,
        )
        self.assertEqual(row["request_identity_hash"], expected)
        self.assertNotIn(row["generation_id"], row["request_identity_hash"])
        self.assertEqual(bundle["provider_messages"][-1]["content"], " hello ")

    def test_identity_is_deterministic_and_generation_is_only_correlation(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('a','s',1,1,?,?)""", (stamp, stamp),
            )
        validated = kelivo_service.validate_completion({
            "model": "ouou-home", "messages": [{"role": "user", "content": " exact "}],
        }, "ouou-home")
        for key in ("deterministic-key-0001", "deterministic-key-0002"):
            kelivo_service.prepare_request(
                self.path, "a", key, validated, persona_text="persona",
                provider_model="provider-a", effective_temperature=0.0,
                effective_max_tokens=77,
            )
        with channel_store.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT generation_id,request_identity_hash FROM kelivo_requests ORDER BY id"
            ).fetchall()
        self.assertNotEqual(rows[0]["generation_id"], rows[1]["generation_id"])
        self.assertEqual(rows[0]["request_identity_hash"], rows[1]["request_identity_hash"])

        variants = (
            ("provider-b", 0.0, 77, validated),
            ("provider-a", 0.1, 77, validated),
            ("provider-a", 0.0, 78, validated),
            ("provider-a", 0.0, 77, kelivo_service.validate_completion({
                "model": "ouou-home", "messages": [{"role": "user", "content": " changed "}],
            }, "ouou-home")),
        )
        hashes = {rows[0]["request_identity_hash"]}
        for number, (model, temperature, max_tokens, request) in enumerate(variants, 3):
            kelivo_service.prepare_request(
                self.path, "a", f"deterministic-key-{number:04d}", request,
                persona_text="persona", provider_model=model,
                effective_temperature=temperature, effective_max_tokens=max_tokens,
            )
        with channel_store.connect(self.path) as conn:
            hashes.update(row[0] for row in conn.execute(
                "SELECT request_identity_hash FROM kelivo_requests WHERE id > 2"
            ))
        self.assertEqual(len(hashes), 5)

    def test_automatic_fingerprint_reuses_full_frozen_identity_contract(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('a','s',1,3,?,?)""", (stamp, stamp),
            )
        validated = kelivo_service.validate_completion({
            "model": "ouou-home",
            "messages": [
                {"role": "system", "content": "snapshot"},
                {"role": "user", "content": " exact user whitespace "},
            ],
        }, "ouou-home")
        base = kelivo_service.freeze_automatic_request(
            self.path, "a", validated, persona_text="persona-a", provider_model="provider-a",
            effective_temperature=0.2, effective_max_tokens=88,
        )
        same = kelivo_service.freeze_automatic_request(
            self.path, "a", validated, persona_text="persona-a", provider_model="provider-a",
            effective_temperature=0.2, effective_max_tokens=88,
        )
        self.assertEqual(base.request_identity_hash, same.request_identity_hash)
        self.assertEqual(base.provider_messages[-1]["content"], " exact user whitespace ")
        variants = (
            ("persona-b", "provider-a", 0.2, 88),
            ("persona-a", "provider-b", 0.2, 88),
            ("persona-a", "provider-a", 0.3, 88),
            ("persona-a", "provider-a", 0.2, 89),
        )
        hashes = {base.request_identity_hash}
        for persona, model, temperature, max_tokens in variants:
            contract = kelivo_service.freeze_automatic_request(
                self.path, "a", validated, persona_text=persona, provider_model=model,
                effective_temperature=temperature, effective_max_tokens=max_tokens,
            )
            hashes.add(contract.request_identity_hash)
        self.assertEqual(len(hashes), 5)
        changed_snapshot = kelivo_service.validate_completion({
            "model": "ouou-home",
            "messages": [
                {"role": "system", "content": "different snapshot"},
                {"role": "user", "content": " exact user whitespace "},
            ],
        }, "ouou-home")
        changed = kelivo_service.freeze_automatic_request(
            self.path, "a", changed_snapshot, persona_text="persona-a", provider_model="provider-a",
            effective_temperature=0.2, effective_max_tokens=88,
        )
        self.assertNotEqual(changed.request_identity_hash, base.request_identity_hash)

    def test_automatic_restart_recovery_blocks_prepared_and_uncertain_without_new_generation(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('a','s',1,1,?,?)""", (stamp, stamp),
            )
        requests = [kelivo_service.validate_completion({
            "model": "ouou-home", "messages": [{"role": "user", "content": text}],
        }, "ouou-home") for text in ("prepared", "dispatching")]
        prepared_rows = []
        for validated in requests:
            contract = kelivo_service.freeze_automatic_request(self.path, "a", validated)
            prepared_rows.append((contract, kelivo_service.prepare_automatic_request(
                self.path, "a", contract.request_identity_hash, validated, contract,
                replay_seconds=600,
            )))
        second = prepared_rows[1][1]
        kelivo_service.begin_dispatch(self.path, "a", second.idempotency_key, stale_seconds=300)
        self.assertEqual(kelivo_service.recover_dispatching_requests(self.path), 1)
        blocked = [kelivo_service.lookup_automatic_request(
            self.path, "a", contract.request_identity_hash,
        ) for contract, _prepared in prepared_rows]
        self.assertTrue(all(item is not None and item.action == "blocked" for item in blocked))
        self.assertEqual(blocked[0].error_category, "relay_restarted_before_dispatch")
        self.assertEqual(blocked[1].error_category, "relay_restarted")
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM kelivo_requests").fetchone()[0], 2)

    def test_restart_recovery_distinguishes_prepared_and_dispatched(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('a','s',1,1,?,?)""", (stamp, stamp),
            )
        validated = kelivo_service.validate_completion({
            "model": "ouou-home", "messages": [{"role": "user", "content": "x"}],
        }, "ouou-home")
        for key in ("restart-prepared-0001", "restart-dispatch-0001"):
            kelivo_service.prepare_request(self.path, "a", key, validated)
        kelivo_service.begin_dispatch(self.path, "a", "restart-dispatch-0001", stale_seconds=300)
        self.assertEqual(kelivo_service.recover_dispatching_requests(self.path), 1)
        with channel_store.connect(self.path) as conn:
            states = {row["idempotency_key"]: (row["status"], row["error_category"])
                      for row in conn.execute("SELECT * FROM kelivo_requests")}
        self.assertEqual(states["restart-prepared-0001"],
                         ("failed", "relay_restarted_before_dispatch"))
        self.assertEqual(states["restart-dispatch-0001"],
                         ("dispatch_uncertain", "relay_restarted"))

    def test_idempotency_is_scoped_to_client(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = channel_store.now_iso()
            for client in ("a", "b"):
                conn.execute(
                    """INSERT INTO kelivo_clients
                       (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                       VALUES(?,?,1,1,?,?)""", (client, f"session-{client}", stamp, stamp),
                )
        validated = kelivo_service.validate_completion({
            "model": "ouou-home", "messages": [{"role": "user", "content": "same"}]
        }, "ouou-home")
        kelivo_service.prepare_request(self.path, "a", "shared-key-0001", validated)
        kelivo_service.prepare_request(self.path, "b", "shared-key-0001", validated)
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM kelivo_requests").fetchone()[0], 2)

    def test_mapping_remap_is_explicit_and_telegram_target_is_verified(self):
        with channel_store.connect(self.path) as conn:
            target = channel_store.get_or_create_conversation(
                conn, "telegram", "bot-a", "chat-a", "private", "user-a"
            )["api_session"]
        kelivo_service.initialize_client_mapping(
            self.path, "client-a", target, require_telegram_session=True,
            allowed_account_ids=frozenset({"bot-a"}), allowed_chat_ids=frozenset({"chat-a"}),
            allowed_user_ids=frozenset({"user-a"}),
        )
        with self.assertRaisesRegex(kelivo_service.KelivoError, "remap_not_allowed"):
            kelivo_service.initialize_client_mapping(self.path, "client-a", "other-session")
        with self.assertRaisesRegex(kelivo_service.KelivoError, "target_invalid"):
            kelivo_service.initialize_client_mapping(
                self.path, "client-b", "missing", require_telegram_session=True,
                allowed_account_ids=frozenset({"bot-a"}), allowed_chat_ids=frozenset({"chat-a"}),
                allowed_user_ids=frozenset({"user-a"}),
            )
        kelivo_service.initialize_client_mapping(
            self.path, "client-a", "other-session", allow_session_remap=True,
        )
        with channel_store.connect(self.path) as conn:
            mapping = conn.execute(
                "SELECT api_session,mapping_revision FROM kelivo_clients WHERE client_id='client-a'"
            ).fetchone()
            event = conn.execute(
                "SELECT event_type FROM channel_audit_events WHERE event_type='kelivo_session_remap'"
            ).fetchone()
        self.assertEqual((mapping["api_session"], mapping["mapping_revision"]), ("other-session", 2))
        self.assertIsNotNone(event)

    def test_stale_dispatch_reaper_only_marks_uncertain(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = "2000-01-01T00:00:00+00:00"
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('a','s',1,1,?,?)""", (stamp, stamp),
            )
        validated = kelivo_service.validate_completion({
            "model": "ouou-home", "messages": [{"role": "user", "content": "x"}]
        }, "ouou-home")
        kelivo_service.prepare_request(self.path, "a", "reaper-key-0001", validated)
        kelivo_service.begin_dispatch(self.path, "a", "reaper-key-0001", stale_seconds=30)
        with channel_store.connect(self.path) as conn:
            conn.execute("UPDATE kelivo_requests SET dispatch_expires_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(kelivo_service.recover_dispatching_requests(
            self.path, stale_seconds=30, category="dispatch_expired"
        ), 1)
        with channel_store.connect(self.path) as conn:
            row = conn.execute("SELECT status,error_category FROM kelivo_requests").fetchone()
        self.assertEqual((row["status"], row["error_category"]),
                         ("dispatch_uncertain", "dispatch_expired"))

    def test_completion_waiting_on_sqlite_lock_beats_future_deadline_reaper(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('a','s',1,1,?,?)""", (stamp, stamp),
            )
        validated = kelivo_service.validate_completion({
            "model": "ouou-home", "messages": [{"role": "user", "content": "x"}],
        }, "ouou-home")
        kelivo_service.prepare_request(self.path, "a", "locked-complete-key-0001", validated)
        kelivo_service.begin_dispatch(self.path, "a", "locked-complete-key-0001", stale_seconds=300)
        blocker = channel_store.connect(self.path)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                completion = pool.submit(
                    kelivo_service.complete_request, self.path, "a", "locked-complete-key-0001",
                    "ouou-home", {"text": "done", "usage": {}},
                )
                reaper = pool.submit(
                    kelivo_service.recover_dispatching_requests, self.path,
                    stale_seconds=300, category="dispatch_expired",
                )
                time.sleep(0.05)
                blocker.execute("COMMIT")
                self.assertEqual(completion.result()["choices"][0]["message"]["content"], "done")
                self.assertEqual(reaper.result(), 0)
        finally:
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")
            blocker.close()
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT status FROM kelivo_requests").fetchone()[0], "completed")

    def test_concurrent_personas_keep_distinct_frozen_bundles(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('a','shared',1,1,?,?)""", (stamp, stamp),
            )
        def prepare(number: int):
            validated = kelivo_service.validate_completion({
                "model": "ouou-home", "messages": [
                    {"role": "system", "content": f"persona-{number}"},
                    {"role": "user", "content": f"question-{number}"},
                ]
            }, "ouou-home")
            return kelivo_service.prepare_request(
                self.path, "a", f"concurrent-key-{number:04d}", validated, rate_limit=10,
                persona_text=f"server-persona-{number}", persona_source="test",
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(prepare, (1, 2)))
        values = {
            result.context_bundle["snapshots"]["system"]["value"][0]["content"] for result in results
        }
        self.assertEqual(values, {"persona-1", "persona-2"})
        self.assertEqual({result.messages[0]["content"] for result in results},
                         {"server-persona-1", "server-persona-2"})

    def test_fixed_window_concurrency_never_over_admits(self):
        with channel_store.connect(self.path) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,ts TEXT,direction TEXT,kind TEXT,text TEXT,meta TEXT)")
            stamp = channel_store.now_iso()
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('a','s',1,1,?,?)""", (stamp, stamp),
            )
        validated = kelivo_service.validate_completion({
            "model": "ouou-home", "messages": [{"role": "user", "content": "x"}],
        }, "ouou-home")
        def attempt(number):
            try:
                kelivo_service.prepare_request(
                    self.path, "a", f"rate-race-key-{number:04d}", validated, rate_limit=3,
                )
                return "ok"
            except kelivo_service.KelivoError as exc:
                return exc.category
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(8)))
        self.assertEqual(outcomes.count("ok"), 3)
        self.assertEqual(outcomes.count("rate_limit_exceeded"), 5)
        with channel_store.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT request_count FROM kelivo_rate_limits").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
