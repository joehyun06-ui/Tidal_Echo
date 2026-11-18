"""PostgreSQL transaction API. Entry points are for trusted isolated hosts."""

import hmac
import re
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .contracts import (
    ActivationStatus, AuthorizationFence, ConsumptionClaim, Credential, LedgerError, OutcomeUnknown,
    PreparedActivation, Scope, SourceFence, canonical, require_counter, require_digest, require_id,
    require_operation_id, sha256,
)

_MIGRATION = Path(__file__).with_name("migrations") / "001_isolated_ledger.sql"
_EVIDENCE_MAX = timedelta(seconds=30)
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECOND = timedelta(microseconds=1)


def _check_connection(conn):
    loopback = conn.info.host == conn.info.hostaddr == "127.0.0.1"
    unix_socket = conn.info.host.startswith("/") and not conn.info.hostaddr
    if (not (loopback or unix_socket)
            or not re.fullmatch(r"gm02as_test_[a-z0-9_]+", conn.info.dbname)):
        raise LedgerError("isolated_postgres_required")


def install_schema(connect):
    """Run once in an empty test database over an explicit local connection."""
    with connect() as conn:
        _check_connection(conn)
        with conn.transaction():
            conn.execute(_MIGRATION.read_text(encoding="utf-8"))


def _row(conn, statement, *args):
    return conn.execute(statement, args).fetchone()


def _must(row, code):
    if row is None:
        raise LedgerError(code)
    return row


def _authorization_error(code):
    return code in {
        "principal_denied", "login_denied", "idp_evidence_unavailable",
        "conversation_denied", "g3_access_denied", "authority_changed",
        "credential_expired", "runtime_binding_changed", "receipt_already_used",
    }


class Ledger:
    def __init__(self, connect, recovery, signing_key):
        if type(signing_key) is not bytes or len(signing_key) < 32:
            raise LedgerError("isolated_signing_key_required")
        self._connect = connect
        self._recovery = recovery
        self._key = signing_key

    @contextmanager
    def _transaction(self, *, recovering=False):
        # The isolated controller holds this lease through commit. Restoring
        # the database cannot restore or bypass the separately held lease.
        with self._recovery.lease(frozen=recovering) as incarnation, self._connect() as conn:
            _check_connection(conn)
            conn.row_factory = dict_row
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                conn.execute("SET LOCAL synchronous_commit = on")
                conn.execute("SET LOCAL search_path = gm02as, pg_catalog")
                conn.execute("SET LOCAL lock_timeout = '5s'")
                conn.execute("SET LOCAL idle_in_transaction_session_timeout = '10s'")
                mode = "FOR UPDATE" if recovering else "FOR SHARE"
                meta = _must(_row(conn, f"""
                    SELECT authority_incarnation FROM authority_metadata
                    WHERE singleton AND schema_version = 1 {mode}
                """), "authority_metadata_unavailable")
                stored = meta["authority_incarnation"]
                if (not recovering and stored != incarnation) or stored > incarnation:
                    raise LedgerError("recovery_required")
                yield conn, incarnation, stored

    @staticmethod
    def _principal(conn, principal_id):
        return _must(_row(conn, """
            SELECT * FROM principal_heads WHERE principal_id = %s FOR UPDATE
        """, principal_id), "principal_denied")

    @staticmethod
    def _login(conn, login_id, principal_id):
        return _must(_row(conn, """
            SELECT * FROM login_sessions WHERE login_id = %s AND principal_id = %s FOR UPDATE
        """, login_id, principal_id), "login_denied")

    @staticmethod
    def _conversation(conn, conversation_id):
        return _must(_row(conn, """
            SELECT * FROM conversation_heads WHERE conversation_id = %s FOR UPDATE
        """, conversation_id), "conversation_denied")

    @staticmethod
    def _dataset(conn, dataset_id):
        return _must(_row(conn, """
            SELECT * FROM g3_dataset_heads WHERE dataset_id = %s FOR UPDATE
        """, dataset_id), "g3_access_denied")

    @staticmethod
    def _source(conn, dataset_id, source_id):
        return _row(conn, """
            SELECT * FROM g3_source_heads WHERE dataset_id = %s AND source_id = %s FOR UPDATE
        """, dataset_id, source_id)

    def _lock_scope(self, conn, scope):
        # Every writer takes the relevant prefix/subsequence of this order.
        principal = self._principal(conn, scope.principal_id)
        login = self._login(conn, scope.login_id, scope.principal_id)
        conversation = self._conversation(conn, scope.conversation_id)
        dataset = self._dataset(conn, scope.dataset_id)
        sources = tuple(self._source(conn, scope.dataset_id, source) for source in scope.source_ids)
        return principal, login, conversation, dataset, sources

    def _snapshot(self, conn, incarnation, scope, locked):
        principal, login, conversation, dataset, source_rows = locked
        if not principal["active"]:
            raise LedgerError("principal_denied")
        # Identity-link mutations are serialized by the referenced principal head.
        identity = _row(conn, """
            SELECT * FROM identity_links WHERE issuer = %s AND subject = %s
        """, login["issuer"], login["subject"])
        if (not login["active"] or not identity or not identity["active"]
                or identity["principal_id"] != scope.principal_id
                or identity["revision"] != login["identity_revision"]
                or login["principal_epoch"] != principal["epoch"]):
            raise LedgerError("login_denied")

        version = _row(conn, """
            SELECT * FROM session_versions WHERE conversation_id = %s AND version_id = %s
        """, scope.conversation_id, conversation["selected_version"])
        grant = _row(conn, """
            SELECT * FROM conversation_grants WHERE conversation_id = %s AND principal_id = %s
        """, scope.conversation_id, scope.principal_id)
        is_owner = conversation["owner_id"] == scope.principal_id
        if (not conversation["active"] or not version or not version["active"]
                or version["owner_id"] != conversation["owner_id"] or not grant or not grant["active"]
                or (grant["role"] == "owner") != is_owner):
            raise LedgerError("conversation_denied")

        rows = conn.execute("""
            SELECT source_id, epoch FROM g3_grants
            WHERE dataset_id = %s AND principal_id = %s AND active
                AND (source_id IS NULL OR source_id = ANY(%s))
        """, (scope.dataset_id, scope.principal_id, list(scope.source_ids))).fetchall()
        g3_epochs = {row["source_id"]: row["epoch"] for row in rows}
        if not dataset["active"] or None not in g3_epochs:
            raise LedgerError("g3_access_denied")
        sources = []
        for source_id, row in zip(scope.source_ids, source_rows):
            if (not row or not row["active"] or row["generation_revision"] != dataset["generation_revision"]
                    or row["cursor_position"] > dataset["cursor_upper"] or source_id not in g3_epochs):
                raise LedgerError("g3_access_denied")
            sources.append(SourceFence(
                source_id, row["event_id"], row["revision"], row["cursor_position"],
                row["content_sha256"], g3_epochs[source_id],
            ))
        # Read wall clock after taking every relevant lock, including request/receipt
        # locks at final activation. An expired observation never passes on retry.
        now = _row(conn, "SELECT clock_timestamp() AS now")["now"]
        if login["evidence_state"] != "active" or not login["observed_at"] <= now < login["evidence_until"]:
            raise LedgerError("idp_evidence_unavailable")
        fence = AuthorizationFence(
            scope, incarnation, principal["epoch"], identity["revision"], login["epoch"],
            conversation["authorization_revision"], version["version_id"], version["revision"],
            grant["epoch"], version["provider"], dataset["generation"], dataset["generation_revision"],
            dataset["authorization_epoch"],
            dataset["cursor_upper"], g3_epochs[None], tuple(sources),
        )
        return fence, (login["evidence_until"] - _UNIX_EPOCH) // _MICROSECOND, (now - _UNIX_EPOCH) // _MICROSECOND

    def _seal(self, purpose, value):
        return hmac.digest(self._key, canonical([purpose, value]), "sha256").hex()

    def _verify_credential(self, credential):
        if type(credential) is not Credential or type(credential.fence) is not AuthorizationFence:
            raise LedgerError("invalid_credential")
        require_digest(credential.seal)
        expected = self._seal("g3-read-v1", [asdict(credential.fence), credential.valid_until_us])
        if not hmac.compare_digest(expected, credential.seal):
            raise LedgerError("invalid_credential")
        return credential.fence

    def _verify_package(self, package):
        if type(package) is not PreparedActivation or type(package.claim) is not ConsumptionClaim:
            raise LedgerError("invalid_package")
        fence = self._verify_credential(package.credential)
        require_digest(package.seal)
        expected = self._seal("prepared-activation-v1", [asdict(package.credential), asdict(package.claim)])
        if not hmac.compare_digest(expected, package.seal):
            raise LedgerError("invalid_package")
        return fence

    @staticmethod
    def _require_incarnation(fence, incarnation):
        if fence.incarnation != incarnation:
            raise LedgerError("stale_incarnation")

    def _current(self, conn, incarnation, credential, locked, *, for_history=False):
        fence = credential.fence
        self._require_incarnation(fence, incarnation)
        current, evidence_until, now = self._snapshot(conn, incarnation, fence.scope, locked)
        if current != fence:
            raise LedgerError("authority_changed")
        if not for_history and now >= credential.valid_until_us:
            raise LedgerError("credential_expired")
        return _UNIX_EPOCH + min(evidence_until, credential.valid_until_us) * _MICROSECOND

    def issue_credential(self, scope):
        if type(scope) is not Scope:
            raise LedgerError("invalid_scope")
        with self._transaction() as (conn, incarnation, _):
            locked = self._lock_scope(conn, scope)
            fence, deadline, _ = self._snapshot(conn, incarnation, scope, locked)
            return Credential(fence, deadline, self._seal("g3-read-v1", [asdict(fence), deadline]))

    def validate_scan(self, credential):
        """Return the authorized committed cursor upper bound and scan scope."""
        fence = self._verify_credential(credential)
        with self._transaction() as (conn, incarnation, _):
            self._require_incarnation(fence, incarnation)
            self._current(conn, incarnation, credential, self._lock_scope(conn, fence.scope))
            return fence

    @staticmethod
    def _require_runtime(conversation, claim):
        if (conversation["active_window_id"] != claim.from_window_id
                or conversation["model_run_id"] == claim.model_run_id
                or conversation["context_epoch"] + 1 != claim.context_epoch):
            raise LedgerError("runtime_binding_changed")

    def seal_package(self, credential, claim):
        fence = self._verify_credential(credential)
        if type(claim) is not ConsumptionClaim:
            raise LedgerError("invalid_consumption_binding")
        with self._transaction() as (conn, incarnation, _):
            self._require_incarnation(fence, incarnation)
            locked = self._lock_scope(conn, fence.scope)
            self._current(conn, incarnation, credential, locked)
            self._require_runtime(locked[2], claim)
            return PreparedActivation(
                credential, claim, self._seal("prepared-activation-v1", [asdict(credential), asdict(claim)]),
            )

    def record_consumption(self, package, observation):
        """Record the trusted host's v2 consumed-input proof for this exact package."""
        fence = self._verify_package(package)
        claim = package.claim
        if not claim.matches(fence, observation):
            raise LedgerError("consumption_mismatch")
        sealed = asdict(package)
        with self._transaction() as (conn, incarnation, _):
            self._require_incarnation(fence, incarnation)
            locked = self._lock_scope(conn, fence.scope)
            self._current(conn, incarnation, package.credential, locked)
            existing = _row(conn, """
                SELECT 1 FROM consumption_bindings WHERE receipt_id = %s FOR UPDATE
            """, package.receipt_id)
            if existing:
                self._receipt(conn, package, conflict_code="consumption_conflict")
                return package.receipt_id
            self._require_runtime(locked[2], claim)
            conn.execute("""
                INSERT INTO consumption_bindings VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (package.receipt_id, incarnation, fence.scope.conversation_id, fence.version_id,
                  claim.attempt_request_id, claim.packet_sha256, claim.input_sha256, Jsonb(sealed)))
            self._receipt(conn, package, conflict_code="consumption_conflict")
            return package.receipt_id

    @staticmethod
    def _receipt(conn, package, *, conflict_code="consumption_missing"):
        row = _row(conn, """
            SELECT sealed_binding FROM consumption_bindings WHERE receipt_id = %s FOR UPDATE
        """, package.receipt_id)
        # Normalize tuples to JSON lists before comparing with JSONB.
        if not row or canonical(row["sealed_binding"]) != canonical(asdict(package)):
            raise LedgerError(conflict_code)

    def request_digest(self, request_id, package):
        fence = self._verify_package(package)
        require_operation_id(request_id, fence.incarnation)
        return sha256(["activate-v1", request_id, asdict(package)])

    def _request_inputs(self, scope, request_id, request_sha256, package):
        fence = self._verify_package(package)
        require_digest(request_sha256)
        if scope != fence.scope:
            raise LedgerError("request_scope_denied")
        if not hmac.compare_digest(self.request_digest(request_id, package), request_sha256):
            raise LedgerError("request_digest_conflict")
        return fence

    @staticmethod
    def _request_row(conn, request_id, digest, fence):
        row = _row(conn, """
            SELECT * FROM activation_requests WHERE activation_request_id = %s FOR UPDATE
        """, request_id)
        if row:
            if (row["principal_id"] != fence.scope.principal_id
                    or row["conversation_id"] != fence.scope.conversation_id):
                raise LedgerError("request_scope_denied")
            if not hmac.compare_digest(row["request_sha256"], digest):
                raise LedgerError("request_digest_conflict")
        return row

    def _status(self, conn, incarnation, request_id, package, locked):
        decision = _row(conn, """
            SELECT * FROM activation_decisions WHERE activation_request_id = %s
        """, request_id)
        if not decision:
            return ActivationStatus(request_id, "pending")
        effective = False
        if decision["outcome"] == "activated" and locked[2]["active_request_id"] == request_id:
            try:
                self._current(conn, incarnation, package.credential, locked, for_history=True)
                effective = True
            except LedgerError as exc:
                if not _authorization_error(exc.code):
                    raise
        return ActivationStatus(
            request_id, decision["outcome"], decision["reason"], effective, decision["projection_revision"],
        )

    def register_activation(self, scope, request_id, request_sha256, package):
        """Persist one pending command. Replays never replace its canonical input."""
        fence = self._request_inputs(scope, request_id, request_sha256, package)
        try:
            with self._transaction() as (conn, incarnation, _):
                self._require_incarnation(fence, incarnation)
                locked = self._lock_scope(conn, scope)
                # The deferred receipt FK allows the request slot to be taken
                # first. ON CONFLICT also serializes claims from different scopes.
                inserted = conn.execute("""
                    INSERT INTO activation_requests
                        (activation_request_id,authority_incarnation,request_sha256,principal_id,
                         conversation_id,receipt_id)
                    VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                """, (request_id, incarnation, request_sha256, scope.principal_id, scope.conversation_id,
                      package.receipt_id)).rowcount
                self._request_row(conn, request_id, request_sha256, fence)
                self._receipt(conn, package)
                if inserted:
                    self._current(conn, incarnation, package.credential, locked)
                    self._require_runtime(locked[2], package.claim)
                return self._status(conn, incarnation, request_id, package, locked)
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            raise OutcomeUnknown(request_id) from exc

    def query_activation(self, principal_id, request_id, *, request_sha256=None):
        """Read the original ID under the host-authenticated principal. Never dispatch or finish it."""
        require_id(principal_id)
        if request_sha256 is not None:
            require_digest(request_sha256)
        try:
            with self._transaction() as (conn, incarnation, _):
                require_operation_id(request_id, incarnation)
                # These immutable rows are only a routing read. Locks are
                # acquired below in the same order as the mutating command.
                row = _row(conn, """
                    SELECT a.request_sha256, b.sealed_binding FROM activation_requests a
                    JOIN consumption_bindings b ON b.receipt_id = a.receipt_id
                    WHERE a.activation_request_id = %s AND a.principal_id = %s
                """, request_id, principal_id)
                if row is None:
                    return ActivationStatus(request_id, "absent")
                if request_sha256 is not None and not hmac.compare_digest(row["request_sha256"], request_sha256):
                    raise LedgerError("request_digest_conflict")
                package = PreparedActivation.from_payload(row["sealed_binding"])
                scope = package.credential.fence.scope
                fence = self._request_inputs(scope, request_id, row["request_sha256"], package)
                if scope.principal_id != principal_id:
                    raise LedgerError("request_scope_denied")
                locked = self._lock_scope(conn, scope)
                self._request_row(conn, request_id, row["request_sha256"], fence)
                self._receipt(conn, package)
                return self._status(conn, incarnation, request_id, package, locked)
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            raise OutcomeUnknown(request_id) from exc

    def activate(self, scope, request_id, request_sha256, package):
        fence = self._request_inputs(scope, request_id, request_sha256, package)
        try:
            with self._transaction() as (conn, incarnation, _):
                self._require_incarnation(fence, incarnation)
                locked = self._lock_scope(conn, scope)
                _must(self._request_row(conn, request_id, request_sha256, fence), "activation_not_registered")
                self._receipt(conn, package)
                status = self._status(conn, incarnation, request_id, package, locked)
                if status.state != "pending":
                    return status
                return self._decide(conn, incarnation, request_id, package, locked)
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            # Includes disconnect while committing. Never retry this transaction here.
            raise OutcomeUnknown(request_id) from exc
        except psycopg.DatabaseError as exc:
            if exc.sqlstate == "P7201":
                raise LedgerError("idp_evidence_unavailable") from exc
            raise

    def _decide(self, conn, incarnation, request_id, package, locked):
        claim, scope = package.claim, package.credential.fence.scope
        state, reason, revision, deadline = "activated", None, None, None
        try:
            if _row(conn, """
                SELECT 1 FROM activation_decisions WHERE receipt_id = %s AND outcome = 'activated'
            """, package.receipt_id):
                raise LedgerError("receipt_already_used")
            self._require_runtime(locked[2], claim)
            if _row(conn, """
                SELECT 1 FROM activation_decisions WHERE authority_incarnation = %s
                    AND conversation_id = %s AND outcome = 'activated'
                    AND (%s IN (from_window_id,target_window_id) OR %s IN (from_model_run_id,model_run_id))
            """, incarnation, scope.conversation_id, claim.target_window_id, claim.model_run_id):
                raise LedgerError("runtime_binding_changed")
            deadline = self._current(conn, incarnation, package.credential, locked)
        except LedgerError as exc:
            if not _authorization_error(exc.code):
                raise
            state, reason = "denied", exc.code
        if state == "activated":
            row = _row(conn, """
                UPDATE conversation_heads SET active_window_id = %s, model_run_id = %s, context_epoch = %s,
                    active_request_id = %s, projection_revision = projection_revision + 1
                WHERE conversation_id = %s RETURNING projection_revision
            """, claim.target_window_id, claim.model_run_id, claim.context_epoch, request_id, scope.conversation_id)
            revision = row["projection_revision"]
        conn.execute("""
            INSERT INTO activation_decisions
                (activation_request_id,receipt_id,outcome,reason,projection_revision,authority_incarnation,
                 conversation_id,from_window_id,from_model_run_id,target_window_id,model_run_id,evidence_deadline)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (request_id, package.receipt_id, state, reason, revision, incarnation, scope.conversation_id,
              claim.from_window_id, locked[2]["model_run_id"], claim.target_window_id, claim.model_run_id,
              deadline))
        return ActivationStatus(request_id, state, reason, state == "activated", revision)

    @staticmethod
    def _command(conn, incarnation, command_id, payload, apply):
        require_operation_id(command_id, incarnation)
        digest = sha256(payload)
        inserted = conn.execute("""
            INSERT INTO authority_commands (command_id,authority_incarnation,command_sha256)
            VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
        """, (command_id, incarnation, digest)).rowcount
        row = _row(conn, """
            SELECT * FROM authority_commands WHERE command_id = %s FOR UPDATE
        """, command_id)
        if not hmac.compare_digest(row["command_sha256"], digest):
            raise LedgerError("command_digest_conflict")
        if not inserted:
            return row["result"]
        result = apply()
        conn.execute("UPDATE authority_commands SET result = %s WHERE command_id = %s",
                     (Jsonb(result), command_id))
        return result

    @staticmethod
    def _audit(conn, incarnation, command_id, scope, keys, epoch):
        conn.execute("""
            INSERT INTO revocation_audit (authority_incarnation,command_id,scope,subject_keys,new_epoch)
            VALUES (%s,%s,%s,%s,%s)
        """, (incarnation, command_id, scope, Jsonb(keys), epoch))

    def observe_idp(self, command_id, principal_id, login_id, state, observed_at):
        """Apply one trusted IdP fixture observation. Revoked login IDs are terminal."""
        require_id(principal_id)
        require_id(login_id)
        if state not in ("active", "unknown", "revoked"):
            raise LedgerError("invalid_idp_observation")
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise LedgerError("invalid_idp_observation")
        observed_at = observed_at.astimezone(timezone.utc)

        with self._transaction() as (conn, incarnation, _):
            self._principal(conn, principal_id)
            login = self._login(conn, login_id, principal_id)

            def apply():
                if observed_at > _row(conn, "SELECT clock_timestamp() AS now")["now"]:
                    raise LedgerError("invalid_idp_observation")
                if not login["active"]:
                    if state != "revoked":
                        raise LedgerError("login_denied")
                    return {"epoch": login["epoch"], "state": login["evidence_state"]}
                if state == "active" and observed_at <= login["observed_at"]:
                    raise LedgerError("stale_idp_observation")
                if state == "unknown" and observed_at < login["observed_at"]:
                    raise LedgerError("stale_idp_observation")
                revoked = state == "revoked"
                stamp = max(observed_at, login["observed_at"])
                deadline = stamp + _EVIDENCE_MAX if state == "active" else stamp
                row = _row(conn, """
                    UPDATE login_sessions SET observed_at = %s, evidence_until = %s, evidence_state = %s,
                        active = %s, epoch = epoch + %s WHERE login_id = %s RETURNING epoch
                """, stamp, deadline, state, not revoked, int(revoked), login_id)
                if revoked:
                    self._audit(conn, incarnation, command_id, "login", [principal_id, login_id], row["epoch"])
                return {"epoch": row["epoch"], "state": state}

            return self._command(
                conn, incarnation, command_id, ["idp-observation", principal_id, login_id, state,
                                                 observed_at.isoformat()], apply,
            )

    def set_principal_access(self, command_id, principal_id, enabled):
        require_id(principal_id)
        if type(enabled) is not bool:
            raise LedgerError("invalid_grant")
        with self._transaction() as (conn, incarnation, _):
            principal = self._principal(conn, principal_id)

            def apply():
                epoch = principal["epoch"]
                if principal["active"] != enabled:
                    epoch += 1
                    conn.execute("UPDATE principal_heads SET active = %s, epoch = %s WHERE principal_id = %s",
                                 (enabled, epoch, principal_id))
                    if not enabled:
                        self._audit(conn, incarnation, command_id, "principal", [principal_id], epoch)
                return {"epoch": epoch, "enabled": enabled}

            return self._command(conn, incarnation, command_id, ["principal", principal_id, enabled], apply)

    def set_conversation_grant(self, command_id, principal_id, conversation_id, enabled):
        require_id(principal_id)
        require_id(conversation_id)
        if type(enabled) is not bool:
            raise LedgerError("invalid_grant")
        with self._transaction() as (conn, incarnation, _):
            self._principal(conn, principal_id)
            conversation = self._conversation(conn, conversation_id)

            def apply():
                role = "owner" if conversation["owner_id"] == principal_id else "delegate"
                changed = _row(conn, """
                    INSERT INTO conversation_grants VALUES (%s,%s,%s,1,%s)
                    ON CONFLICT (conversation_id,principal_id) DO UPDATE
                    SET epoch = conversation_grants.epoch + 1, active = EXCLUDED.active, role = EXCLUDED.role
                    WHERE (conversation_grants.active, conversation_grants.role)
                        IS DISTINCT FROM (EXCLUDED.active, EXCLUDED.role)
                    RETURNING epoch
                """, conversation_id, principal_id, role, enabled)
                if changed:
                    conn.execute("""
                        UPDATE conversation_heads SET authorization_revision = authorization_revision + 1,
                            projection_revision = projection_revision + 1 WHERE conversation_id = %s
                    """, (conversation_id,))
                    if not enabled:
                        self._audit(conn, incarnation, command_id, "conversation",
                                    [principal_id, conversation_id], changed["epoch"])
                row = changed or _row(conn, """
                    SELECT epoch FROM conversation_grants WHERE conversation_id = %s AND principal_id = %s
                """, conversation_id, principal_id)
                return {"epoch": row["epoch"], "enabled": enabled}

            return self._command(conn, incarnation, command_id,
                                 ["conversation-grant", principal_id, conversation_id, enabled], apply)

    def select_version(self, command_id, conversation_id, expected_revision, version_id):
        require_id(conversation_id)
        require_id(version_id)
        require_counter(expected_revision, 1)
        with self._transaction() as (conn, incarnation, _):
            conversation = self._conversation(conn, conversation_id)

            def apply():
                version = _row(conn, """
                    SELECT * FROM session_versions WHERE conversation_id = %s AND version_id = %s
                """, conversation_id, version_id)
                if not conversation["active"] or not version or not version["active"]:
                    raise LedgerError("conversation_denied")
                if (conversation["authorization_revision"] != expected_revision
                        or version["owner_id"] != conversation["owner_id"]):
                    raise LedgerError("version_conflict")
                row = _row(conn, """
                    UPDATE conversation_heads SET selected_version = %s,
                        authorization_revision = authorization_revision + 1,
                        projection_revision = projection_revision + 1, active_request_id = NULL
                    WHERE conversation_id = %s RETURNING authorization_revision
                """, version_id, conversation_id)
                return {"authorization_revision": row["authorization_revision"], "version_id": version_id}

            return self._command(conn, incarnation, command_id,
                                 ["select-version", conversation_id, expected_revision, version_id], apply)

    def set_g3_grant(self, command_id, principal_id, dataset_id, source_id, enabled):
        require_id(principal_id)
        require_id(dataset_id)
        if source_id is not None:
            require_id(source_id)
        if type(enabled) is not bool:
            raise LedgerError("invalid_grant")
        with self._transaction() as (conn, incarnation, _):
            self._principal(conn, principal_id)
            dataset = self._dataset(conn, dataset_id)
            if source_id is not None:
                _must(self._source(conn, dataset_id, source_id), "g3_access_denied")

            def apply():
                changed = _row(conn, """
                    INSERT INTO g3_grants VALUES (%s,%s,%s,1,%s)
                    ON CONFLICT (dataset_id,source_id,principal_id) DO UPDATE
                    SET epoch = g3_grants.epoch + 1, active = EXCLUDED.active
                    WHERE g3_grants.active IS DISTINCT FROM EXCLUDED.active RETURNING epoch
                """, dataset_id, source_id, principal_id, enabled)
                authorization_epoch = dataset["authorization_epoch"]
                if changed:
                    authorization_epoch += 1
                    conn.execute("UPDATE g3_dataset_heads SET authorization_epoch = %s WHERE dataset_id = %s",
                                 (authorization_epoch, dataset_id))
                    if not enabled:
                        self._audit(conn, incarnation, command_id, "g3",
                                    [principal_id, dataset_id, source_id], authorization_epoch)
                row = changed or _row(conn, """
                    SELECT epoch FROM g3_grants
                    WHERE dataset_id = %s AND source_id IS NOT DISTINCT FROM %s AND principal_id = %s
                """, dataset_id, source_id, principal_id)
                return {"epoch": row["epoch"], "enabled": enabled, "authorization_epoch": authorization_epoch}

            return self._command(conn, incarnation, command_id,
                                 ["g3-grant", principal_id, dataset_id, source_id, enabled], apply)

    def publish_revision(self, command_id, dataset_id, source_id, *, generation, generation_revision,
                         expected_revision, event_id, content_sha256):
        """Make an already durable fixture body's revision and cursor visible together."""
        for value in (dataset_id, source_id, generation, event_id):
            require_id(value)
        require_digest(content_sha256)
        require_counter(generation_revision, 1)
        require_counter(expected_revision)
        with self._transaction() as (conn, incarnation, _):
            dataset = self._dataset(conn, dataset_id)
            source = self._source(conn, dataset_id, source_id)

            def apply():
                if (not dataset["active"] or dataset["generation"] != generation
                        or dataset["generation_revision"] != generation_revision
                        or (source["revision"] if source else 0) != expected_revision):
                    raise LedgerError("publish_conflict")
                cursor = dataset["cursor_upper"] + 1
                revision = expected_revision + 1
                conn.execute("""
                    INSERT INTO g3_source_heads
                        (dataset_id,source_id,event_id,generation_revision,revision,cursor_position,content_sha256)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (dataset_id,source_id) DO UPDATE
                    SET event_id = EXCLUDED.event_id, generation_revision = EXCLUDED.generation_revision,
                        revision = EXCLUDED.revision, cursor_position = EXCLUDED.cursor_position,
                        content_sha256 = EXCLUDED.content_sha256, active = true
                """, (dataset_id, source_id, event_id, dataset["generation_revision"], revision, cursor, content_sha256))
                conn.execute("UPDATE g3_dataset_heads SET cursor_upper = %s WHERE dataset_id = %s",
                             (cursor, dataset_id))
                return {"revision": revision, "cursor": cursor}

            return self._command(conn, incarnation, command_id,
                                 ["publish-revision", dataset_id, source_id, generation, generation_revision,
                                  expected_revision, event_id, content_sha256], apply)

    def publish_generation(self, command_id, dataset_id, expected_generation, generation_revision, new_generation):
        for value in (dataset_id, expected_generation, new_generation):
            require_id(value)
        require_counter(generation_revision, 1)
        with self._transaction() as (conn, incarnation, _):
            dataset = self._dataset(conn, dataset_id)

            def apply():
                if (not dataset["active"] or dataset["generation"] != expected_generation
                        or dataset["generation_revision"] != generation_revision
                        or new_generation == expected_generation):
                    raise LedgerError("publish_conflict")
                row = _row(conn, """
                    UPDATE g3_dataset_heads SET generation = %s, generation_revision = generation_revision + 1,
                        cursor_upper = 0,
                        authorization_epoch = authorization_epoch + 1
                    WHERE dataset_id = %s RETURNING authorization_epoch, generation_revision
                """, new_generation, dataset_id)
                return {"authorization_epoch": row["authorization_epoch"], "generation": new_generation,
                        "generation_revision": row["generation_revision"]}

            return self._command(conn, incarnation, command_id,
                                 ["publish-generation", dataset_id, expected_generation, generation_revision,
                                  new_generation], apply)

    def projection(self, conversation_id):
        """Emit a detached display snapshot. Its contents never authorize a ledger operation."""
        require_id(conversation_id)
        with self._transaction() as (conn, incarnation, _):
            row = self._conversation(conn, conversation_id)
            return {
                "incarnation": incarnation, "conversation_id": conversation_id,
                "projection_revision": row["projection_revision"], "version_id": row["selected_version"],
                "window_id": row["active_window_id"], "model_run_id": row["model_run_id"],
                "context_epoch": row["context_epoch"],
            }

    def reconcile_restored_state(self):
        """Isolated recovery policy. Retire all restored effective facts while frozen."""
        with self._transaction(recovering=True) as (conn, incarnation, stored):
            if stored == incarnation:
                return False
            # The exclusive metadata lock fences ordinary writers. Restored data
            # does not establish that any old identity, session or ACL is current.
            conn.execute("""
                UPDATE principal_heads SET active = false, epoch = epoch + 1;
                UPDATE identity_links SET active = false, revision = revision + 1;
                UPDATE login_sessions SET active = false, evidence_state = 'revoked', epoch = epoch + 1;
                UPDATE conversation_heads SET active_request_id = NULL, active = false,
                    authorization_revision = authorization_revision + 1, projection_revision = projection_revision + 1;
                UPDATE session_versions SET active = false, revision = revision + 1;
                UPDATE conversation_grants SET active = false, epoch = epoch + 1;
                UPDATE g3_dataset_heads SET active = false, authorization_epoch = authorization_epoch + 1;
                UPDATE g3_source_heads SET active = false;
                UPDATE g3_grants SET active = false, epoch = epoch + 1
            """)
            conn.execute("UPDATE authority_metadata SET authority_incarnation = %s WHERE singleton", (incarnation,))
            return True
