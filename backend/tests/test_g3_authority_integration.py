"""Test-only canonical session with the real read-only HTTP G3 adapter."""

from contextlib import closing
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from uuid import uuid4
from unittest.mock import patch

from backend.g3_authority_client import G3AuthorityTestClient, TEST_ACCESS
from backend.g3_projection_contract import AccessScope, G3SourceRef, ProjectionError
from backend.session_lifecycle import LifecycleDeltaPage, LifecycleError, SessionSnapshot, SourceCursor, WindowReceipt, _json
from backend.session_window_api_consumption import (
    ApiWindowConsumptionObservation, ApiWindowConsumptionStore, install_schema as install_consumption_schema,
)
from backend.session_window_api_input import FORMAT, prepare_api_window_input
from backend.session_window_attempt_activation import activate_recorded_attempt
from backend.session_window_attempt_dispatch import attempt_packet_dispatch_once
from backend.session_window_attempts import WindowAttemptJournal, install_schema as install_attempt_schema
from backend.tests.test_session_lifecycle import CID
from backend.tests.test_session_lifecycle_storage import FileHarness

ATTEMPT = "g3_test_attempt"
ACTIVATION = "g3_test_activation"
CLOSEOUT = "g3_test_closeout"
PREPARE = "g3_test_prepare"


class _CanonicalTestSession(FileHarness):
    def __init__(self, path, client, event_id, *, seed):
        super().__init__(path, seed=seed)
        self.client = client
        self.event_id = event_id
        self.engine.evidence = client

    def snapshot(self, cid, access):
        if cid != CID or access != TEST_ACCESS:
            raise LifecycleError("lifecycle_access_denied")
        session = self.session_authority.row_for_session(cid)
        runtime = self.get("runtime")
        scan = self.client.begin_scan(after_cursor=0, access=access)
        visible = self.client.list_events(after_cursor=0, access=access)
        if self.client.validate_scan(scan, access=access) is not True:
            raise LifecycleError("lifecycle_authority_changed")
        recent = tuple(event.ref.event_id for event in visible if event.ref.event_id == self.event_id)
        message_head = self.conn.execute(
            "SELECT MAX(id) FROM messages WHERE json_extract(meta,'$.api_session')=?", (cid,),
        ).fetchone()[0]
        snapshot = SessionSnapshot(
            cid, session["provider"], cid, runtime["window_id"], runtime["model_run_id"], runtime["epoch"],
            "pending_stamp", SourceCursor(scan.source_generation, runtime["observed_source"]),
            runtime["observed_message"], SourceCursor(scan.source_generation, scan.upper_cursor), message_head,
            (), (), (), recent,
        )
        return replace(snapshot, stamp=sha256(_json([asdict(snapshot), scan.authorization_epoch])).hexdigest())

    def read_delta(self, snapshot, *, source_after, message_after, access):
        scan = self.client.begin_scan(after_cursor=source_after, access=access)
        source = self.client.read_page(scan, after_cursor=source_after, limit=100, access=access)
        if scan.upper_cursor != snapshot.source_head.position:
            raise LifecycleError("lifecycle_authority_changed")
        messages = tuple(row[0] for row in self.conn.execute(
            "SELECT id FROM messages WHERE id>? AND id<=? AND json_extract(meta,'$.api_session')=? "
            "ORDER BY id LIMIT 100", (message_after, snapshot.message_head, snapshot.version_id),
        ))
        upper_message = messages[-1] if messages else snapshot.message_head
        return LifecycleDeltaPage(snapshot.stamp, source_after, source.next_cursor, message_after,
                                  upper_message, source.events, messages,
                                  source.done and upper_message == snapshot.message_head)


def _require(condition, error):
    if not condition:
        raise AssertionError(error)


def _deny(fn, expected, error_type):
    try:
        fn()
    except error_type as error:
        _require(error.category == expected, f"expected {expected}, got {error.category}")
        return error.category
    raise AssertionError(f"expected {expected}")


def _activate(engine, journal, digest):
    return activate_recorded_attempt(
        engine, journal, CID, ACTIVATION, attempt_request_id=ATTEMPT,
        packet_sha256=digest, access=TEST_ACCESS, enabled=True,
    )


def _wire(harness, *, observer):
    with closing(harness.connect_canonical()) as conn:
        install_attempt_schema(conn)
        install_consumption_schema(conn)
    journal = WindowAttemptJournal(harness.repo, enabled=True)
    runtime = ApiWindowConsumptionStore(harness.engine, journal, observe_consumption=observer, enabled=True)
    harness.engine.runtime = runtime
    return journal, runtime


def _activation_count(harness):
    return harness.conn.execute(
        "SELECT count(*) FROM session_lifecycle_receipts WHERE conversation_id=? "
        "AND json_extract(payload,'$.data.phase_at_apply')='active'", (CID,),
    ).fetchone()[0]


def prepare_case(client, path, event_id, *, positive_control=False):
    scan = client.begin_scan(after_cursor=0, access=TEST_ACCESS)
    event = client.read_event(event_id, revision=1, access=TEST_ACCESS)
    source = client.open_source(event.ref, access=TEST_ACCESS)
    _require(sha256(source).hexdigest() == event.ref.source_sha256, "source bytes differ from authority digest")
    harness = _CanonicalTestSession(path, client, event_id, seed=True)
    with closing(harness.conn):
        runtime_state = harness.get("runtime")
        harness.put("runtime", {**runtime_state, "observed_source": 0})
        engine = harness.engine
        observed = None
        journal, runtime = _wire(harness, observer=lambda *_a, **_kw: observed)
        engine.closeout(CID, CLOSEOUT, window_id="window_0", context_epoch=0, access=TEST_ACCESS)
        engine.reentry(CID, PREPARE, closeout_id=CLOSEOUT, target_window_id="window_1", access=TEST_ACCESS)
        packet = engine.packet(CID, access=TEST_ACCESS)
        prepared = prepare_api_window_input(engine, CID, access=TEST_ACCESS, enabled=True)
        _require(tuple(card.source_refs[0] for card in packet.recent) == (event.ref,), "packet did not use the authority ref")
        _require(packet.source_authorization_epoch == scan.authorization_epoch, "packet is missing the authority epoch")
        _require(source.decode() in json.loads(prepared.content)["g3_excerpts"][0]["excerpt"], "input did not use the authority bytes")

        def load_in_test_window(attempt, handoff):
            nonlocal observed
            proof = WindowReceipt(
                handoff.conversation_id, handoff.provider, handoff.version_id, handoff.from_window_id,
                handoff.target_window_id, "g3_test_run", handoff.context_epoch, attempt.packet_sha256,
                handoff.source_cursor, handoff.message_cursor, "confirmed",
            )
            loaded = json.loads(json.dumps(prepared.as_message()))
            input_sha256 = sha256(_json([FORMAT, attempt.packet_sha256, loaded["role"], loaded["content"]])).hexdigest()
            observed = ApiWindowConsumptionObservation(proof, input_sha256)

        attempt_packet_dispatch_once(
            engine, journal, CID, ATTEMPT, packet_sha256=prepared.packet_sha256,
            expected_revision=harness.record().revision, access=TEST_ACCESS, dispatch=load_in_test_window, enabled=True,
        )
        proof = observed.receipt
        harness.put("runtime", {
            **runtime_state, "window_id": proof.target_window_id, "model_run_id": proof.model_run_id,
            "epoch": proof.context_epoch, "observed_source": proof.source_cursor.position,
            "observed_message": proof.message_cursor,
        })
        runtime.record(CID, ATTEMPT, access=TEST_ACCESS)
        _require(runtime.read_observation(proof.packet_sha256, access=TEST_ACCESS) == observed,
                 "v2 consumption observation did not round-trip")
        _require(_activation_count(harness) == 0, "unexpected activation before revocation")
        if positive_control:
            _require(_activate(engine, journal, proof.packet_sha256).window_confirmed, "unrevoked control could not activate")
        return {
            "ref": asdict(event.ref), "packet_sha256": proof.packet_sha256, "input_sha256": observed.input_sha256,
            "generation": scan.source_generation, "cursor": scan.upper_cursor,
            "authorization_epoch": scan.authorization_epoch, "attempt_id": ATTEMPT, "conversation_id": CID,
            "version_id": proof.version_id, "target_window_id": proof.target_window_id,
            "context_epoch": proof.context_epoch, "model_run_id": proof.model_run_id,
            "message_cursor": proof.message_cursor,
        }


def verify_revoked(client, path, original):
    ref = G3SourceRef(**original["ref"])
    scan = client.begin_scan(after_cursor=original["cursor"], access=TEST_ACCESS)
    _require(scan.source_generation == original["generation"], "authority generation changed")
    _require(int(scan.authorization_epoch) > int(original["authorization_epoch"]), "authorization epoch did not advance")
    delta = client.read_revocations(after_cursor=original["cursor"], access=TEST_ACCESS)
    matching = [item for item in delta if item.event_id == ref.event_id and item.revision == ref.revision + 1]
    _require(len(matching) == 1 and original["cursor"] < matching[0].cursor <= scan.upper_cursor,
             "durable revocation missing from feed")
    evidence = {
        "original_revision": _deny(lambda: client.read_event(ref.event_id, revision=ref.revision, access=TEST_ACCESS),
                                   "g3_test_reference_denied", ProjectionError),
        "original_source": _deny(lambda: client.open_source(ref, access=TEST_ACCESS),
                                 "g3_test_reference_denied", ProjectionError),
        "generation": scan.source_generation, "cursor": scan.upper_cursor,
        "authorization_epoch": scan.authorization_epoch, "revocation": asdict(matching[0]),
    }
    harness = _CanonicalTestSession(path, client, ref.event_id, seed=False)
    with closing(harness.conn):
        journal, runtime = _wire(harness, observer=lambda *_a, **_kw: None)
        digest = original["packet_sha256"]
        _require(runtime.read_observation(digest, access=TEST_ACCESS).input_sha256 == original["input_sha256"],
                 "historical v2 evidence changed")
        evidence["old_packet"] = _deny(lambda: harness.engine.packet(CID, access=TEST_ACCESS),
                                       "lifecycle_delta_required", LifecycleError)
        evidence["old_activation"] = _deny(lambda: _activate(harness.engine, journal, digest),
                                           "lifecycle_delta_required", LifecycleError)
        evidence["activation_receipts"] = _activation_count(harness)
        _require(evidence["activation_receipts"] == 0 and harness.record().phase == "prepared",
                 "revoked activation wrote state or receipt")
    return evidence


class G3AuthorityIntegrationTests(unittest.TestCase):
    def test_read_to_revocation_to_reopened_authority(self):
        from services.g3_authority.api import make_server
        from services.g3_authority.store import AuthorityStore

        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "G3_TEST_ADMIN_TOKEN": "admin-" + uuid4().hex,
            "G3_TEST_READER_TOKEN": "reader-" + uuid4().hex,
        }):
            root = Path(directory)
            writer = os.environ["G3_TEST_ADMIN_TOKEN"]
            reader = os.environ["G3_TEST_READER_TOKEN"]
            event = "test-" + uuid4().hex
            store = AuthorityStore(root)
            store.initialize()

            def serve():
                server = make_server(AuthorityStore(root), "127.0.0.1", 0)
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                url = f"http://127.0.0.1:{server.server_port}"
                return server, thread, G3AuthorityTestClient(url, reader), url

            _deny(lambda: store.seed_test_event(event, token=reader), "g3_test_access_denied", ProjectionError)
            store.seed_test_event(event, token=writer)
            server, thread, client, url = serve()
            try:
                other_dataset = AccessScope(TEST_ACCESS.principal_id, frozenset({"outside-test-dataset"}))
                _deny(lambda: client.begin_scan(after_cursor=0, access=other_dataset),
                      "g3_test_access_denied", ProjectionError)
                _deny(lambda: G3AuthorityTestClient(url, writer).begin_scan(after_cursor=0, access=TEST_ACCESS),
                      "g3_test_reference_denied", ProjectionError)
                scan = client.begin_scan(after_cursor=0, access=TEST_ACCESS)
                prepare_case(client, root / "control.sqlite3", event, positive_control=True)
                path = root / "canonical.sqlite3"
                original = prepare_case(client, path, event)
                _deny(lambda: store.revoke_test_event(event, token=reader), "g3_test_access_denied", ProjectionError)
                revoked = store.revoke_test_event(event, token=writer)
                self.assertEqual(store.revoke_test_event(event, token=writer), revoked)
                self.assertFalse(client.validate_scan(scan, access=TEST_ACCESS))
                self.assertEqual(client.list_events(after_cursor=0, access=TEST_ACCESS), ())
                first = verify_revoked(client, path, original)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()
            server, thread, client, _ = serve()
            try:
                self.assertEqual(verify_revoked(client, path, original), first)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


def main():
    import sys

    action, url, path, event_id = sys.argv[1:]
    client = G3AuthorityTestClient(url, os.environ["G3_TEST_READER_TOKEN"])
    _require("G3_TEST_ADMIN_TOKEN" not in os.environ, "reader process received the admin credential")
    canonical = Path(path)
    if action == "prepare":
        result = prepare_case(client, canonical, event_id)
    elif action == "verify":
        original = json.loads((canonical.parent / "prepared.json").read_text())
        _require(original["ref"]["event_id"] == event_id, "test event differs from original")
        result = verify_revoked(client, canonical, original)
    else:
        raise ValueError("unknown acceptance phase")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
