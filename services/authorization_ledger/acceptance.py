"""Run with python -m unittest services.authorization_ledger.acceptance -v."""

import hashlib
import itertools
import os
import sqlite3
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from secrets import token_bytes
from tempfile import TemporaryDirectory

import psycopg

from backend.session_lifecycle import SourceCursor, WindowReceipt
from backend.session_window_api_consumption import ApiWindowConsumptionObservation

from .acceptance_support import RecoveryController, SandboxPostgres, commit_cut
from .contracts import ConsumptionClaim, LedgerError, OutcomeUnknown, Scope, new_operation_id
from .ledger import Ledger, install_schema
from .projection import RelayProjection


_BODY = b"isolated synthetic G3 revision"
_DIGEST = hashlib.sha256(_BODY).hexdigest()
_PRINCIPAL = "fixture_user"
_OTHER = "fixture_delegate"
_CID = "fixture_conversation"
_VERSION = "fixture_version"
_LOGIN = "fixture_login"
_DATASET = "fixture_dataset"
_SOURCE = "fixture_source"
_SECOND = "fixture_source_two"
_ISSUER = "https://fixture.invalid/auth"
_GENERATION = "fixture_generation"
_SCOPE = Scope(_PRINCIPAL, _LOGIN, _CID, _DATASET, (_SOURCE,))


class PausedLedger(Ledger):
    def __init__(self, *args):
        super().__init__(*args)
        self.ready = threading.Event()
        self.release = threading.Event()

    @contextmanager
    def _transaction(self, **kwargs):
        with super()._transaction(**kwargs) as transaction:
            yield transaction
            self.ready.set()
            if not self.release.wait(5):
                raise AssertionError("commit was never released")


class LedgerAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bins = os.environ.get("GM02AS_TEST_PG_BIN")
        if not bins:
            raise RuntimeError("Set GM02AS_TEST_PG_BIN to a local PostgreSQL 17+ bin directory")
        root = TemporaryDirectory(prefix="gm02as-ledger-")
        cls.addClassCleanup(root.cleanup)
        cls.postgres = SandboxPostgres(bins, Path(root.name))
        cls.addClassCleanup(cls.postgres.stop)
        cls.postgres.start()
        cls.cases = itertools.count(1)
        with cls.postgres.connect("postgres") as conn:
            print("Isolated PostgreSQL:", conn.execute("SHOW server_version").fetchone()[0], flush=True)

    def setUp(self):
        self.case = next(self.cases)
        self.database = f"gm02as_test_case_{self.case}"
        self.postgres.create_database(self.database)
        self.controller = RecoveryController(self.postgres.root / f"outside-pitr-{self.case}")
        self.controller.initialize()
        self.key = token_bytes(32)
        self.ledger = self.make_ledger()
        install_schema(self.connect)
        self.seed()

    def connect(self):
        return self.postgres.connect(self.database)

    def make_ledger(self, *, application="ledger-acceptance", proxy_port=None, cls=Ledger, recovery=None):
        return cls(
            lambda: self.postgres.connect(self.database, application=application, proxy_port=proxy_port),
            recovery or self.controller,
            self.key,
        )

    def seed(self):
        with self.connect() as conn, conn.transaction():
            conn.execute("SET LOCAL search_path = gm02as, pg_catalog")
            for principal, login in ((_PRINCIPAL, _LOGIN), (_OTHER, "delegate_login")):
                conn.execute("INSERT INTO principal_heads (principal_id) VALUES (%s)", (principal,))
                conn.execute("INSERT INTO identity_links (issuer, subject, principal_id) VALUES (%s,%s,%s)",
                             (_ISSUER, principal, principal))
                conn.execute("""
                    INSERT INTO login_sessions
                      (login_id, principal_id, issuer, subject, principal_epoch, identity_revision,
                       evidence_state, observed_at, evidence_until)
                    SELECT %s,%s,%s,%s,1,1,'active',observation,observation + interval '30 seconds'
                    FROM (SELECT clock_timestamp() AS observation) AS fixture
                """, (login, principal, _ISSUER, principal))
            conn.execute("""
                INSERT INTO conversation_heads
                  (conversation_id,owner_id,selected_version,active_window_id,model_run_id,context_epoch)
                VALUES (%s,%s,%s,'window_zero','run_zero',0)
            """, (_CID, _PRINCIPAL, _VERSION))
            for version in (_VERSION, "other_version"):
                conn.execute("""
                    INSERT INTO session_versions (conversation_id,version_id,owner_id,provider)
                    VALUES (%s,%s,%s,'api')
                """, (_CID, version, _PRINCIPAL))
            conn.execute("INSERT INTO conversation_grants VALUES (%s,%s,'owner',1,true)", (_CID, _PRINCIPAL))
            conn.execute("INSERT INTO conversation_grants VALUES (%s,%s,'delegate',1,true)", (_CID, _OTHER))
            conn.execute("""
                INSERT INTO g3_dataset_heads (dataset_id,generation,cursor_upper) VALUES (%s,%s,2)
            """, (_DATASET, _GENERATION))
            for position, source in enumerate((_SOURCE, _SECOND), 1):
                conn.execute("""
                    INSERT INTO g3_source_heads
                      (dataset_id,source_id,event_id,generation_revision,revision,cursor_position,content_sha256)
                    VALUES (%s,%s,%s,1,1,%s,%s)
                """, (_DATASET, source, f"event_{position}", position, _DIGEST))
            for principal in (_PRINCIPAL, _OTHER):
                for source in (None, _SOURCE, _SECOND):
                    conn.execute("INSERT INTO g3_grants VALUES (%s,%s,%s,1,true)",
                                 (_DATASET, source, principal))

    def now(self):
        with self.connect() as conn:
            return conn.execute("SELECT clock_timestamp()").fetchone()[0]

    def read(self, statement, *args):
        with self.connect() as conn, conn.transaction():
            conn.execute("SET LOCAL search_path = gm02as, pg_catalog")
            return conn.execute(statement, args).fetchone()

    def assert_code(self, code, command, *args, **kwargs):
        with self.assertRaises(LedgerError) as captured:
            command(*args, **kwargs)
        self.assertEqual(captured.exception.code, code)

    @staticmethod
    def observation(credential, claim):
        fence = credential.fence
        return ApiWindowConsumptionObservation(
            WindowReceipt(fence.scope.conversation_id, fence.provider, fence.version_id, claim.from_window_id,
                          claim.target_window_id, claim.model_run_id, claim.context_epoch, claim.packet_sha256,
                          SourceCursor(fence.generation, fence.cursor_upper), claim.message_cursor, "confirmed"),
            claim.input_sha256,
        )

    def prepare(self, *, scope=_SCOPE, attempt="attempt_one", target="window_one", model_run="run_one"):
        credential = self.ledger.issue_credential(scope)
        current = self.ledger.projection(scope.conversation_id)
        claim = ConsumptionClaim(attempt, "a" * 64, "b" * 64, current["window_id"], target, model_run,
                                 current["context_epoch"] + 1, 7)
        package = self.ledger.seal_package(credential, claim)
        self.assertEqual(self.ledger.record_consumption(package, self.observation(credential, claim)),
                         package.receipt_id)
        return package

    def register(self, package=None):
        package = package or self.prepare()
        request_id = new_operation_id(package.credential.fence.incarnation)
        digest = self.ledger.request_digest(request_id, package)
        args = (package.credential.fence.scope, request_id, digest, package)
        self.assertEqual(self.ledger.register_activation(*args).state, "pending")
        return args

    def revoke(self, ledger, *, command_id=None):
        return ledger.set_g3_grant(command_id or new_operation_id(1), _PRINCIPAL, _DATASET, _SOURCE, False)

    def assert_blocked(self, application):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.connect() as observer:
                row = observer.execute("""
                    SELECT pid FROM pg_stat_activity
                    WHERE application_name=%s AND cardinality(pg_blocking_pids(pid)) > 0
                """, (application,)).fetchone()
            if row:
                return
            time.sleep(0.015)
        self.fail("second PostgreSQL transaction was never observed waiting on a lock")

    def race(self, first, second):
        winner = self.make_ledger(application="winner", cls=PausedLedger)
        follower = self.make_ledger(application="follower")
        with ThreadPoolExecutor(2) as pool:
            one = pool.submit(first, winner)
            try:
                if not winner.ready.wait(3):
                    one.result(timeout=1)
                    self.fail("first transaction did not reach COMMIT")
                two = pool.submit(second, follower)
                self.assert_blocked("follower")
            finally:
                winner.release.set()
            return one.result(), two.result()

    def test_private_cluster_and_transaction_settings(self):
        with self.ledger._transaction() as (conn, incarnation, stored):
            self.assertEqual(incarnation, stored)
            self.assertEqual(conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"],
                             "read committed")
            self.assertEqual(conn.execute("SHOW synchronous_commit").fetchone()["synchronous_commit"], "on")
            self.assertEqual(conn.execute("SHOW fsync").fetchone()["fsync"], "on")
            self.assertEqual(conn.execute("SHOW listen_addresses").fetchone()["listen_addresses"], "127.0.0.1")
        self.assert_code("isolated_postgres_required", install_schema, lambda: self.postgres.connect("postgres"))
        with self.assertRaises(psycopg.errors.DuplicateSchema):
            install_schema(self.connect)
        self.assertEqual(self.read("SELECT schema_version FROM authority_metadata"), (1,))

    def test_revoke_commits_before_activation(self):
        request = self.register()
        _, status = self.race(self.revoke, lambda ledger: ledger.activate(*request))
        self.assertEqual((status.state, status.reason, status.effective_now), ("denied", "g3_access_denied", False))
        self.assertEqual(self.read("SELECT active_request_id,context_epoch FROM conversation_heads"), (None, 0))
        self.assertEqual(self.read("SELECT count(*) FROM revocation_audit"), (1,))

    def test_activation_commits_first_and_later_revoke_changes_only_effectiveness(self):
        request = self.register()
        status, _ = self.race(lambda ledger: ledger.activate(*request), self.revoke)
        self.assertTrue(status.effective_now)
        later = self.ledger.query_activation(_PRINCIPAL, request[1])
        self.assertEqual((later.state, later.effective_now, later.projection_revision),
                         ("activated", False, status.projection_revision))
        self.assertEqual(self.read("SELECT count(*) FROM activation_decisions"), (1,))

    def test_conversation_regrant_advances_epoch_and_rejects_artifact_replay(self):
        old = self.register()
        old_package = old[3]
        off_id = new_operation_id(1)
        off = self.ledger.set_conversation_grant(off_id, _PRINCIPAL, _CID, False)
        self.assertEqual(self.ledger.set_conversation_grant(off_id, _PRINCIPAL, _CID, False), off)
        on = self.ledger.set_conversation_grant(new_operation_id(1), _PRINCIPAL, _CID, True)
        self.assertGreater(on["epoch"], off["epoch"])
        self.assert_code("command_digest_conflict", self.ledger.set_conversation_grant,
                         off_id, _PRINCIPAL, _CID, True)
        self.assert_code("authority_changed", self.ledger.validate_scan, old_package.credential)
        self.assert_code("authority_changed", self.ledger.seal_package, old_package.credential, old_package.claim)
        self.assert_code("authority_changed", self.ledger.record_consumption,
                         old_package, self.observation(old_package.credential, old_package.claim))
        self.assertEqual(self.ledger.activate(*old).reason, "authority_changed")
        fresh = self.register(self.prepare(attempt="attempt_fresh"))
        self.assertTrue(self.ledger.activate(*fresh).effective_now)
        self.assertEqual(self.read("SELECT count(*) FROM revocation_audit"), (1,))

    def test_g3_regrant_advances_epoch_and_rejects_artifact_replay(self):
        old = self.register()
        off = self.revoke(self.ledger)
        on = self.ledger.set_g3_grant(new_operation_id(1), _PRINCIPAL, _DATASET, _SOURCE, True)
        self.assertGreater(on["epoch"], off["epoch"])
        self.assertGreater(on["authorization_epoch"], old[3].credential.fence.authorization_epoch)
        self.assert_code("authority_changed", self.ledger.validate_scan, old[3].credential)
        self.assertEqual(self.ledger.activate(*old).reason, "authority_changed")
        new = self.register(self.prepare(attempt="attempt_fresh"))
        self.assertEqual(self.ledger.activate(*new).state, "activated")

    def test_database_commit_ack_lost_then_query_by_original_request_id(self):
        request = self.register()
        with commit_cut(self.postgres, after_commit=True) as proxy_port:
            severed = self.make_ledger(proxy_port=proxy_port)
            with self.assertRaises(OutcomeUnknown) as captured:
                severed.activate(*request)
            self.assertEqual(captured.exception.request_id, request[1])
        restarted = self.make_ledger()
        self.assertEqual(restarted.query_activation(_PRINCIPAL, new_operation_id(1)).state, "absent")
        status = restarted.query_activation(_PRINCIPAL, request[1])
        self.assertEqual(status.state, "activated")
        self.assertTrue(status.effective_now)
        self.assertEqual(self.read("SELECT count(*) FROM activation_decisions"), (1,))

    def test_disconnect_before_commit_leaves_pending_and_allows_same_id_retry(self):
        request = self.register()
        with commit_cut(self.postgres, after_commit=False) as proxy_port:
            with self.assertRaises(OutcomeUnknown):
                self.make_ledger(proxy_port=proxy_port).activate(*request)
        self.assertEqual(self.ledger.query_activation(_PRINCIPAL, request[1]).state, "pending")
        self.assertEqual(self.read("SELECT active_request_id,context_epoch FROM conversation_heads"), (None, 0))
        self.assertEqual(self.read("SELECT count(*) FROM activation_decisions"), (0,))
        self.assertTrue(self.ledger.activate(*request).effective_now)

    def test_query_waits_for_inflight_commit_and_never_applies(self):
        request = self.register()
        winner, query = self.race(lambda ledger: ledger.activate(*request),
                                  lambda ledger: ledger.query_activation(_PRINCIPAL, request[1]))
        self.assertEqual(winner, query)
        self.assertEqual(self.read("SELECT count(*) FROM activation_decisions"), (1,))

    def test_same_request_concurrent_redelivery_commits_one_decision(self):
        request = self.register()
        self.assertEqual(self.ledger.register_activation(*request).state, "pending")
        one, two = self.race(lambda ledger: ledger.activate(*request), lambda ledger: ledger.activate(*request))
        self.assertEqual(one, two)
        self.assertEqual(self.ledger.register_activation(*request), one)
        self.assertEqual(self.read("SELECT count(*) FROM activation_requests"), (1,))
        self.assertEqual(self.read("SELECT count(*) FROM activation_decisions"), (1,))

    def test_new_request_cannot_reuse_one_consumption_receipt(self):
        first = self.register()
        second = self.register(first[3])
        self.assertTrue(self.ledger.activate(*first).effective_now)
        self.assertEqual(self.ledger.activate(*second).reason, "receipt_already_used")
        self.assertEqual(self.read("SELECT count(*) FROM activation_decisions WHERE outcome='activated'"), (1,))

    def test_digest_and_sealed_payload_tampering_are_rejected(self):
        scope, request_id, digest, package = self.register()
        self.assert_code("request_digest_conflict", self.ledger.activate, scope, request_id, "c" * 64, package)
        self.assert_code("request_digest_conflict", self.ledger.query_activation,
                         _PRINCIPAL, request_id, request_sha256="c" * 64)
        forged = replace(package, claim=replace(package.claim, input_sha256="c" * 64))
        self.assert_code("invalid_package", self.ledger.activate, scope, request_id, digest, forged)
        self.assert_code("invalid_credential", self.ledger.validate_scan,
                         replace(package.credential, fence=replace(package.credential.fence, cursor_upper=99)))
        self.assertEqual(self.read("SELECT count(*) FROM activation_decisions"), (0,))

    def test_same_id_different_valid_package_conflicts_and_is_global(self):
        request = self.register()
        another = self.prepare(attempt="another_attempt", target="another_target")
        other_digest = self.ledger.request_digest(request[1], another)
        self.assert_code("request_digest_conflict", self.ledger.register_activation,
                         _SCOPE, request[1], other_digest, another)
        other_scope = replace(_SCOPE, principal_id=_OTHER, login_id="delegate_login")
        delegated = self.prepare(scope=other_scope, attempt="delegate_attempt", target="delegate_target")
        self.assert_code("request_scope_denied", self.ledger.register_activation, other_scope, request[1],
                         self.ledger.request_digest(request[1], delegated), delegated)
        self.assertEqual(self.ledger.query_activation(_OTHER, request[1]).state, "absent")
        self.assertEqual(self.read("SELECT count(*) FROM activation_requests"), (1,))

    def test_verified_subject_login_binding_and_dataset_plus_source_acl(self):
        self.assert_code("login_denied", self.ledger.issue_credential, replace(_SCOPE, login_id="delegate_login"))
        source_set = replace(_SCOPE, source_ids=(_SOURCE, _SECOND))
        self.assertEqual(len(self.ledger.issue_credential(source_set).fence.sources), 2)
        self.ledger.set_g3_grant(new_operation_id(1), _PRINCIPAL, _DATASET, _SECOND, False)
        self.assert_code("g3_access_denied", self.ledger.issue_credential, source_set)
        self.assertEqual(self.ledger.issue_credential(_SCOPE).fence.version_id, _VERSION)
        self.ledger.set_g3_grant(new_operation_id(1), _PRINCIPAL, _DATASET, None, False)
        self.assert_code("g3_access_denied", self.ledger.issue_credential, _SCOPE)

    def test_consumption_proof_must_match_actual_input_and_cursors(self):
        package = self.prepare()
        proof = self.observation(package.credential, package.claim)
        for wrong in (
            replace(proof, input_sha256="c" * 64),
            replace(proof, receipt=replace(proof.receipt, status="unknown")),
            replace(proof, receipt=replace(proof.receipt, model_run_id="incorrect_run")),
            replace(proof, receipt=replace(proof.receipt, message_cursor=8)),
            replace(proof, receipt=replace(proof.receipt, source_cursor=SourceCursor(_GENERATION, 1))),
        ):
            self.assert_code("consumption_mismatch", self.ledger.record_consumption, package, wrong)
        self.assertEqual(self.ledger.record_consumption(package, proof), package.receipt_id)
        self.assertEqual(self.read("SELECT count(*) FROM consumption_bindings"), (1,))

    def test_version_switch_wins_and_switching_back_does_not_revive_old_binding(self):
        request = self.register()
        command_id = new_operation_id(1)

        def select_version(ledger):
            return ledger.select_version(command_id, _CID, 1, "other_version")

        change, denied = self.race(select_version, lambda ledger: ledger.activate(*request))
        self.assertEqual(change["authorization_revision"], 2)
        self.assertEqual(denied.reason, "authority_changed")
        self.assertEqual(select_version(self.ledger), change)
        self.ledger.select_version(new_operation_id(1), _CID, 2, _VERSION)
        self.assert_code("authority_changed", self.ledger.validate_scan, request[3].credential)

    def test_activation_first_then_version_switch_makes_history_ineffective(self):
        request = self.register()
        decision, switch = self.race(
            lambda ledger: ledger.activate(*request),
            lambda ledger: ledger.select_version(new_operation_id(1), _CID, 1, "other_version"),
        )
        self.assertEqual((decision.state, switch["version_id"]), ("activated", "other_version"))
        self.assertFalse(self.ledger.query_activation(_PRINCIPAL, request[1]).effective_now)

    def test_expired_or_unknown_idp_observations_fail_closed(self):
        request = self.register()
        self.ledger.observe_idp(new_operation_id(1), _PRINCIPAL, _LOGIN, "unknown", self.now())
        self.assertEqual(self.ledger.activate(*request).reason, "idp_evidence_unavailable")
        self.assert_code("idp_evidence_unavailable", self.ledger.issue_credential, _SCOPE)
        # Simulate an isolated IdP observation aging beyond its deadline.
        with self.connect() as conn:
            conn.execute("""
                UPDATE gm02as.login_sessions SET evidence_state='active',
                  observed_at=clock_timestamp()-interval '65 seconds',
                  evidence_until=clock_timestamp()-interval '36 seconds' WHERE login_id=%s
            """, (_LOGIN,))
        self.assert_code("idp_evidence_unavailable", self.ledger.issue_credential, _SCOPE)
        self.ledger.observe_idp(new_operation_id(1), _PRINCIPAL, _LOGIN, "active", self.now())
        self.assertEqual(self.ledger.activate(*request).state, "denied")
        self.assertEqual(self.ledger.issue_credential(_SCOPE).fence.scope, _SCOPE)

    def test_revoked_login_cannot_be_resurrected_by_late_idp_observation(self):
        request = self.register()
        command_id, stamp = new_operation_id(1), self.now()
        result = self.ledger.observe_idp(command_id, _PRINCIPAL, _LOGIN, "revoked", stamp)
        self.assertEqual(self.ledger.observe_idp(command_id, _PRINCIPAL, _LOGIN, "revoked", stamp), result)
        self.assert_code("login_denied", self.ledger.observe_idp,
                         new_operation_id(1), _PRINCIPAL, _LOGIN, "active", self.now())
        self.assertEqual(self.ledger.activate(*request).reason, "login_denied")
        self.assertEqual(self.read("SELECT count(*) FROM revocation_audit"), (1,))

    def test_principal_regrant_requires_a_new_login(self):
        request = self.register()
        self.ledger.set_principal_access(new_operation_id(1), _PRINCIPAL, False)
        self.ledger.set_principal_access(new_operation_id(1), _PRINCIPAL, True)
        self.assert_code("login_denied", self.ledger.issue_credential, _SCOPE)
        self.assertEqual(self.ledger.activate(*request).reason, "login_denied")

    def test_g3_publish_disconnect_keeps_revision_and_cursor_in_same_transaction(self):
        old = self.ledger.issue_credential(_SCOPE)
        command_id = new_operation_id(1)

        def publish(ledger):
            return ledger.publish_revision(command_id, _DATASET, _SOURCE, generation=_GENERATION,
                                           generation_revision=1, expected_revision=1, event_id="revision_two", content_sha256=_DIGEST)

        with commit_cut(self.postgres, after_commit=False) as proxy_port:
            with self.assertRaises(psycopg.OperationalError):
                publish(self.make_ledger(proxy_port=proxy_port))
        self.assertEqual(self.ledger.validate_scan(old).cursor_upper, 2)
        self.assertEqual(self.read("SELECT revision FROM g3_source_heads WHERE source_id=%s", _SOURCE), (1,))
        self.assertEqual(publish(self.ledger), {"cursor": 3, "revision": 2})
        self.assertEqual(publish(self.make_ledger()), {"cursor": 3, "revision": 2})
        self.assert_code("authority_changed", self.ledger.validate_scan, old)
        self.assertEqual(self.ledger.issue_credential(_SCOPE).fence.cursor_upper, 3)

    def test_generation_aba_does_not_publish_old_source_even_if_cursor_advances(self):
        old = self.ledger.issue_credential(_SCOPE)
        self.ledger.publish_generation(new_operation_id(1), _DATASET, _GENERATION, 1, "second_generation")
        self.ledger.publish_generation(new_operation_id(1), _DATASET, "second_generation", 2, _GENERATION)
        self.ledger.publish_revision(new_operation_id(1), _DATASET, _SECOND, generation=_GENERATION,
                                     generation_revision=3, expected_revision=1, event_id="updated_second", content_sha256=_DIGEST)
        self.assert_code("g3_access_denied", self.ledger.issue_credential, _SCOPE)
        self.assert_code("publish_conflict", self.ledger.publish_revision, new_operation_id(1), _DATASET, _SOURCE,
                         generation=_GENERATION, generation_revision=1, expected_revision=1,
                         event_id="late_old_publish", content_sha256=_DIGEST)
        self.assert_code("publish_conflict", self.ledger.publish_generation,
                         new_operation_id(1), _DATASET, _GENERATION, 1, "late_generation_change")
        self.ledger.publish_revision(new_operation_id(1), _DATASET, _SOURCE, generation=_GENERATION,
                                     generation_revision=3, expected_revision=1, event_id="updated_first", content_sha256=_DIGEST)
        self.assert_code("authority_changed", self.ledger.validate_scan, old)
        self.assertEqual(self.ledger.issue_credential(_SCOPE).fence.generation_revision, 3)

    def test_projection_reordering_restart_and_tampering_cannot_authorize(self):
        path = self.controller.root / "synthetic_relay.sqlite3"
        projection = RelayProjection(path)
        projection.install()
        projection.bind_incarnation(1)
        old = self.ledger.projection(_CID)
        request = self.register()
        self.ledger.activate(*request)
        newest = self.ledger.projection(_CID)
        self.assertTrue(projection.apply(newest))
        restarted = RelayProjection(path)
        restarted.install()
        self.assertFalse(restarted.apply(old))
        self.assertFalse(restarted.apply(newest))
        self.assert_code("projection_conflict", restarted.apply, dict(newest, window_id="forged"))
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("SELECT revision FROM conversation_projection").fetchone(),
                             (newest["projection_revision"],))
        self.revoke(self.ledger)
        self.assertFalse(self.ledger.query_activation(_PRINCIPAL, request[1]).effective_now)
        restarted.bind_incarnation(2)
        self.assert_code("stale_incarnation", restarted.apply, newest)

    def test_restored_postgres_image_cannot_revive_old_incarnation_or_grants(self):
        committed = self.register()
        self.ledger.activate(*committed)
        pending = self.register(self.prepare(attempt="pending_attempt", target="window_two", model_run="run_two"))
        snapshot = f"gm02as_test_snapshot_{self.case}"
        self.postgres.create_database(snapshot, template=self.database)
        self.revoke(self.ledger)
        lost_request = new_operation_id(1)
        self.assertEqual(self.read("SELECT count(*) FROM revocation_audit"), (1,))
        self.controller.freeze()
        self.database = snapshot
        restored = self.make_ledger(recovery=RecoveryController(self.controller.root))
        # The backing PostgreSQL database really contains the pre-revoke image.
        self.assertEqual(self.read("SELECT count(*) FROM revocation_audit"), (0,))
        self.assertEqual(self.read("SELECT active FROM g3_grants WHERE source_id=%s AND principal_id=%s",
                                   _SOURCE, _PRINCIPAL), (True,))
        self.assert_code("recovery_frozen", restored.activate, *pending)
        self.controller.thaw(2)
        self.assert_code("recovery_required", restored.issue_credential, _SCOPE)
        self.controller.freeze()
        self.assertTrue(restored.reconcile_restored_state())
        self.assertFalse(restored.reconcile_restored_state())
        self.controller.thaw(3)
        for command, args in (
            (restored.validate_scan, (pending[3].credential,)),
            (restored.seal_package, (pending[3].credential, pending[3].claim)),
            (restored.record_consumption, (pending[3], self.observation(pending[3].credential, pending[3].claim))),
            (restored.activate, pending),
            (restored.register_activation, pending),
            (restored.query_activation, (_PRINCIPAL, pending[1])),
            (restored.query_activation, (_PRINCIPAL, committed[1])),
            (restored.query_activation, (_PRINCIPAL, lost_request)),
        ):
            self.assert_code("stale_incarnation", command, *args)
        self.assert_code("principal_denied", restored.issue_credential, _SCOPE)
        self.assertEqual(self.read("SELECT count(*) FROM activation_decisions WHERE outcome='activated'"), (1,))

    def test_missing_recovery_fence_fails_closed(self):
        self.controller.state.unlink()
        self.assert_code("recovery_unavailable", self.ledger.issue_credential, _SCOPE)

    def test_persisted_history_cannot_be_rewritten_by_regular_dml(self):
        request = self.register()
        self.ledger.activate(*request)
        self.revoke(self.ledger)
        for table in ("activation_requests", "activation_decisions", "consumption_bindings", "revocation_audit"):
            with self.connect() as conn, self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                conn.execute(f"DELETE FROM gm02as.{table}")

    def test_restart_does_not_allow_old_target_window_or_model_run_to_be_reused(self):
        self.ledger.activate(*self.register())
        second = self.prepare(attempt="attempt_two", target="window_two", model_run="run_two")
        self.assertTrue(self.make_ledger().activate(*self.register(second)).effective_now)
        reuse = self.prepare(attempt="attempt_three", target="window_zero", model_run="run_three")
        self.assertEqual(self.ledger.activate(*self.register(reuse)).reason, "runtime_binding_changed")
        reuse_run = self.prepare(attempt="attempt_four", target="window_four", model_run="run_one")
        self.assertEqual(self.ledger.activate(*self.register(reuse_run)).reason, "runtime_binding_changed")

    def test_deadline_between_decision_and_commit_rolls_back_all_changes(self):
        with self.connect() as conn:
            deadline = conn.execute("""
                UPDATE gm02as.login_sessions SET evidence_until = clock_timestamp() + interval '1.5 seconds'
                WHERE login_id = %s RETURNING evidence_until
            """, (_LOGIN,)).fetchone()[0]
        request = self.register()
        writer = self.make_ledger(cls=PausedLedger)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(writer.activate, *request)
            try:
                if not writer.ready.wait(2):
                    future.result(timeout=1)
                    self.fail("activation did not reach pre-commit pause")
                self.assertFalse(future.done())
                while self.now() <= deadline:
                    time.sleep(0.025)
            finally:
                writer.release.set()
            self.assert_code("idp_evidence_unavailable", future.result)
        self.assertEqual(self.ledger.query_activation(_PRINCIPAL, request[1]).state, "pending")
        self.assertEqual(self.read("SELECT active_request_id,context_epoch FROM conversation_heads"), (None, 0))
        self.assertEqual(self.read("SELECT count(*) FROM activation_decisions"), (0,))
        self.assertEqual(self.ledger.activate(*request).reason, "idp_evidence_unavailable")
