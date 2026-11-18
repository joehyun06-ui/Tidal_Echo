"""API lifecycle receipt integration on a disposable canonical database."""

from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend import session_window_api_consumption
from backend.session_lifecycle import LifecycleError, WindowReceipt, _json
from backend.session_window_activation_status import inspect_recorded_activation
from backend.session_window_api_consumption import (
    ApiWindowConsumptionObservation, ApiWindowConsumptionStore, DDL, TABLE, install_schema,
)
from backend.session_window_api_input import FORMAT, prepare_api_window_input
from backend.session_window_attempt_activation import activate_recorded_attempt
from backend.session_window_attempt_dispatch import attempt_packet_dispatch_once
from backend.session_window_attempts import WindowAttemptJournal, _checksum, install_schema as install_attempt_schema
from backend.tests.test_session_lifecycle import CID, CTX
from backend.tests.test_session_lifecycle_storage import FileHarness, Proxy


def reopen_and_activate(path, digest):
    harness = FileHarness(path, seed=False)
    with closing(harness.conn):
        journal = WindowAttemptJournal(harness.repo, enabled=True)
        observer = Mock(side_effect=AssertionError("activation must read the persisted receipt"))
        runtime = ApiWindowConsumptionStore(harness.engine, journal, observe_consumption=observer, enabled=True)
        harness.engine.runtime = runtime
        observation = runtime.read_observation(digest, access=CTX)
        reference = dict(attempt_request_id="attempt_1", packet_sha256=digest, access=CTX, enabled=True)
        activated = activate_recorded_attempt(harness.engine, journal, CID, "activate_1", **reference)
        status = inspect_recorded_activation(journal, CID, "activate_1", **reference)
        state = harness.record()
        observer.assert_not_called()
        print(json.dumps({
            "conversation": state.conversation_id, "window": state.window_id,
            "run_id": observation.receipt.model_run_id, "input_sha256": observation.input_sha256,
            "confirmed": activated.window_confirmed,
            "reply_completed": activated.reply_completed, "history": status["state"],
            "current_confirmed": status["current_window_confirmed"], "retry_authorized": status["retry_authorized"],
        }))


class ApiWindowConsumptionTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(tmp) / "canonical.sqlite"
        self.harness = h = FileHarness(self.path)
        self.addCleanup(h.conn.close)
        with closing(h.connect_canonical()) as conn:
            install_attempt_schema(conn)
            self.assertTrue(install_schema(conn))
            self.assertFalse(install_schema(conn))
        self.journal = WindowAttemptJournal(h.repo, enabled=True)
        self.observe = Mock(return_value=None)
        self.store = ApiWindowConsumptionStore(h.engine, self.journal, observe_consumption=self.observe, enabled=True)
        h.engine.runtime = self.store
        h.engine.closeout(CID, "close_1", window_id="window_0", context_epoch=0, access=CTX)
        self.mid = h.conn.execute(
            "INSERT INTO messages(ts,kind,direction,text,meta) VALUES (?, 'user', 'in', ?, ?)",
            ("2026-11-18T00:00:00Z", "synthetic canonical text", json.dumps({"api_session": CID})),
        ).lastrowid
        h.engine.reentry(CID, "prepare_1", closeout_id="close_1", target_window_id="window_1", access=CTX)
        self.state = h.record()
        self.reference = {"attempt_request_id": "attempt_1", "packet_sha256": self.state.packet_sha256,
                          "access": CTX, "enabled": True}

    def record_consumption(self):
        return self.store.record(CID, "attempt_1", access=CTX)

    def receipt_count(self):
        return self.harness.conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]

    def dispatch_once(self, callback):
        return attempt_packet_dispatch_once(
            self.harness.engine, self.journal, CID, "attempt_1", packet_sha256=self.state.packet_sha256,
            expected_revision=self.state.revision, access=CTX, dispatch=callback, enabled=True,
        )

    def prepared_target(self):
        expected = None
        prepared = prepare_api_window_input(self.harness.engine, CID, access=CTX, enabled=True)

        def dispatch(attempt, packet):
            nonlocal expected
            receipt = WindowReceipt(
                packet.conversation_id, packet.provider, packet.version_id, packet.from_window_id,
                packet.target_window_id, "run_api_1", packet.context_epoch, attempt.packet_sha256,
                packet.source_cursor, packet.message_cursor, "confirmed",
            )
            synthetic_loaded_message = json.loads(json.dumps(prepared.as_message()))
            loaded_digest = hashlib.sha256(_json([
                FORMAT, attempt.packet_sha256, synthetic_loaded_message["role"], synthetic_loaded_message["content"],
            ])).hexdigest()
            expected = ApiWindowConsumptionObservation(receipt, loaded_digest)

        self.dispatch_once(dispatch)
        runtime = self.harness.get("runtime")
        proof = expected.receipt
        runtime.update(window_id=proof.target_window_id, model_run_id=proof.model_run_id,
                       epoch=proof.context_epoch, observed_source=proof.source_cursor.position,
                       observed_message=proof.message_cursor)
        self.harness.put("runtime", runtime)
        return expected

    def assert_category(self, action, category):
        with self.assertRaises(LifecycleError) as caught:
            action()
        self.assertEqual(caught.exception.category, category)

    def activate(self):
        return activate_recorded_attempt(self.harness.engine, self.journal, CID, "activate_1", **self.reference)

    def edit_original_text(self):
        self.harness.conn.execute("UPDATE messages SET text=? WHERE id=?", ("edited synthetic text", self.mid))

    def test_default_off_ignores_runtime_dependencies(self):
        store = ApiWindowConsumptionStore(object(), object(), observe_consumption=object())
        self.assertIsNone(store.read_receipt(object(), access=None))
        self.assert_category(lambda: store.record(None, None, access=None), "lifecycle_disabled")

    def test_prepared_attempt_and_successful_dispatch_cannot_issue_receipt(self):
        def dispatch(_observation, _packet):
            return {"status": "completed"}

        result = self.dispatch_once(dispatch)
        self.assertFalse(result.window_confirmed)
        self.assert_category(self.record_consumption, "lifecycle_consumption_unconfirmed")
        self.observe.assert_not_called()
        self.assertIsNone(self.store.read_receipt(self.state.packet_sha256, access=CTX))
        self.assertEqual(self.harness.record(), self.state)

    def test_missing_or_mismatched_host_proof_preserves_prepared_state(self):
        observation = self.prepared_target()
        expected = observation.receipt
        self.assert_category(self.record_consumption, "lifecycle_consumption_unconfirmed")
        self.observe.return_value = expected
        self.assert_category(self.record_consumption, "lifecycle_consumption_invalid")
        for proof in (replace(expected, message_cursor=expected.message_cursor + 1),
                      replace(expected, source_cursor=replace(expected.source_cursor, generation="wrong")),
                      replace(expected, model_run_id="other_run"), replace(expected, packet_sha256="0" * 64),
                      replace(expected, version_id="other_version")):
            with self.subTest(proof=proof):
                self.observe.return_value = replace(observation, receipt=proof)
                self.assert_category(self.record_consumption, "lifecycle_consumption_mismatch")
        self.assertEqual(self.receipt_count(), 0)
        self.assertEqual(self.harness.record(), self.state)

    def test_runtime_snapshot_must_have_consumed_both_cursors(self):
        expected = self.prepared_target()
        self.observe.return_value = expected
        for field in ("observed_source", "observed_message"):
            with self.subTest(field=field):
                original = self.harness.get("runtime")
                self.harness.put("runtime", {**original, field: original[field] - 1})
                self.assert_category(self.record_consumption, "lifecycle_consumption_unconfirmed")
                self.harness.put("runtime", original)
        self.observe.assert_not_called()
        self.assertEqual(self.receipt_count(), 0)

    def test_receipt_is_idempotent_and_activates_on_reopen(self):
        expected = self.prepared_target()
        self.observe.return_value = expected
        self.assertEqual(self.record_consumption(), expected.receipt)
        self.assertEqual(self.record_consumption(), expected.receipt)
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.harness.record(), self.state)
        denied = replace(CTX, allowed_scopes=frozenset())
        self.assertIsNone(self.store.read_receipt(self.state.packet_sha256, access=denied))

        self.assertEqual(self.store.read_observation(self.state.packet_sha256, access=CTX), expected)
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import sys; from backend.tests.test_session_window_api_consumption import reopen_and_activate; "
             "reopen_and_activate(sys.argv[1], sys.argv[2])", str(self.path), self.state.packet_sha256],
            capture_output=True, text=True, timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "conversation": CID, "window": expected.receipt.target_window_id, "run_id": expected.receipt.model_run_id,
            "input_sha256": expected.input_sha256,
            "confirmed": True, "reply_completed": False, "history": "historical_confirmed",
            "current_confirmed": False, "retry_authorized": False,
        })
        self.edit_original_text()
        replay = self.activate()
        self.assertTrue(replay.replayed)
        self.assertEqual(self.receipt_count(), 1)

    def test_source_revoke_during_observation_prevents_commit(self):
        expected = self.prepared_target()

        def changed(_digest, *, access):
            self.harness.g3.deny(access.principal_id, "private_A")
            return expected

        self.observe.side_effect = changed
        self.assert_category(self.record_consumption, "lifecycle_authority_changed")
        self.assertEqual(self.receipt_count(), 0)

    def test_write_failure_and_lost_ack_observe_same_receipt(self):
        expected = self.prepared_target()
        self.observe.return_value = expected

        def fail():
            raise OSError("synthetic")

        for checkpoint, count in (("before", 0), ("after", 1)):
            with self.subTest(checkpoint=checkpoint):
                with patch.object(self.harness, "connection_wrapper", lambda conn: Proxy(conn, **{checkpoint: fail})):
                    self.assert_category(self.record_consumption, "lifecycle_storage_unavailable")
                self.assertEqual(self.receipt_count(), count)
        self.assertEqual(self.record_consumption(), expected.receipt)
        self.assertEqual(self.receipt_count(), 1)

    def test_corrupt_persisted_receipt_cannot_activate(self):
        self.observe.return_value = self.prepared_target()
        self.record_consumption()
        self.harness.conn.execute(f"UPDATE {TABLE} SET message_cursor=message_cursor+1")
        self.assert_category(self.activate, "lifecycle_consumption_corrupt")
        self.assertEqual(self.harness.record(), self.state)

    def test_host_digest_must_match_prepared_context(self):
        expected = self.prepared_target()
        self.observe.return_value = replace(expected, input_sha256="0" * 64)
        self.assert_category(self.record_consumption, "lifecycle_consumption_input_mismatch")
        self.assertEqual(self.receipt_count(), 0)

    def test_same_packet_with_text_edit_before_record_is_rejected(self):
        self.observe.return_value = self.prepared_target()
        self.edit_original_text()
        self.assert_category(self.record_consumption, "lifecycle_consumption_input_mismatch")
        self.assertEqual(self.receipt_count(), 0)

    def test_same_packet_with_text_edit_after_record_cannot_activate(self):
        observation = self.prepared_target()
        self.observe.return_value = observation
        self.record_consumption()
        self.edit_original_text()
        self.assert_category(self.activate, "lifecycle_consumption_input_mismatch")
        self.assertEqual(self.store.read_observation(self.state.packet_sha256, access=CTX), observation)
        state = inspect_recorded_activation(self.journal, CID, "activate_1", **self.reference)
        self.assertEqual(state["state"], "unconfirmed")
        self.assertEqual(self.harness.record(), self.state)

    def test_activation_rebuild_occurs_under_the_canonical_write_lock(self):
        self.observe.return_value = self.prepared_target()
        self.record_consumption()
        rebuild = session_window_api_consumption._materialize_api_window_input

        def check_lock(tx, packet, digest):
            self.assertTrue(tx.conn.in_transaction)
            with closing(sqlite3.connect(self.path, timeout=0, isolation_level=None)) as writer:
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    writer.execute("UPDATE messages SET text=? WHERE id=?", ("concurrent edit", self.mid))
                self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            return rebuild(tx, packet, digest)

        with patch.object(session_window_api_consumption, "_materialize_api_window_input", side_effect=check_lock) as checked:
            self.assertTrue(self.activate().window_confirmed)
        checked.assert_called_once()

    def test_legacy_receipt_without_input_digest_remains_historical(self):
        observation = self.prepared_target()
        proof = observation.receipt
        previous_table = "session_window_api_consumption_v1"
        conn = self.harness.conn
        conn.execute(DDL.replace(TABLE, previous_table).replace("    input_sha256 TEXT NOT NULL,\n", ""))
        values = (
            proof.packet_sha256, self.state.access_binding, CID, "attempt_1", proof.version_id, proof.from_window_id,
            proof.target_window_id, proof.model_run_id, proof.context_epoch, proof.source_cursor.generation,
            proof.source_cursor.position, proof.message_cursor,
        )
        conn.execute(f"INSERT INTO {previous_table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (*values, _checksum(values)))
        self.assertIsNone(self.store.read_receipt(proof.packet_sha256, access=CTX))
        self.assert_category(self.activate, "lifecycle_confirmation_unknown")
        self.assertEqual(conn.execute(f"SELECT count(*) FROM {previous_table}").fetchone()[0], 1)
