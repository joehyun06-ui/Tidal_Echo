"""Default-off persistence for API window consumption observed by a trusted host."""

from contextlib import closing
from dataclasses import dataclass
import inspect
import sqlite3

from .g3_projection import _valid_id
from .g3_projection_contract import ProjectionError
from .g3_projection_feed import _binding
from .session_lifecycle import LifecycleError, SessionLifecycle, SourceCursor, WindowReceipt, _session_id
from .session_lifecycle_storage import _owned, _require_current_message_cursor, _source_schema
from .session_window_api_input import _materialize_api_window_input
from .session_window_attempts import WindowAttemptJournal, _checksum, _hex, _row, _schema as _attempt_schema


TABLE = "session_window_api_consumption_v2"
DDL = f"""CREATE TABLE {TABLE} (
    packet_sha256 TEXT PRIMARY KEY NOT NULL,
    input_sha256 TEXT NOT NULL,
    access_binding TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    attempt_request_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    from_window_id TEXT NOT NULL,
    target_window_id TEXT NOT NULL,
    model_run_id TEXT NOT NULL,
    context_epoch INTEGER NOT NULL CHECK(context_epoch>0),
    source_generation TEXT NOT NULL,
    source_position INTEGER NOT NULL CHECK(source_position>=0),
    message_cursor INTEGER NOT NULL CHECK(message_cursor>=0),
    record_sha256 TEXT NOT NULL,
    UNIQUE(conversation_id,attempt_request_id),
    FOREIGN KEY(conversation_id,attempt_request_id)
        REFERENCES session_window_attempts(conversation_id,request_id))"""


@dataclass(frozen=True)
class ApiWindowConsumptionObservation:
    """A trusted host's observation of the context actually loaded by the target run."""

    receipt: WindowReceipt
    input_sha256: str


def _schema(conn, *, allow_missing=False):
    rows = [
        tuple(row) for row in conn.execute(
            "SELECT type,sql FROM main.sqlite_master WHERE tbl_name=? OR name=?", (TABLE, TABLE),
        )
        if not (row[0] == "index" and row[1] is None)
    ]
    if not rows and allow_missing:
        return False
    if rows != [("table", DDL)] or conn.execute(f"PRAGMA main.foreign_key_check({TABLE})").fetchone():
        raise LifecycleError("lifecycle_consumption_schema_invalid")
    return True


def install_schema(conn):
    """Install the digest-bound schema explicitly and preserve any v1 rows."""
    if conn.in_transaction or conn.isolation_level is not None:
        raise LifecycleError("lifecycle_connection_invalid")
    try:
        conn.execute("BEGIN IMMEDIATE")
        _source_schema(conn)
        _owned(conn)
        _attempt_schema(conn)
        created = not _schema(conn, allow_missing=True)
        if created:
            conn.execute(DDL)
        _schema(conn)
        conn.commit()
        return created
    except BaseException as exc:
        conn.rollback()
        if isinstance(exc, (sqlite3.Error, OSError)):
            raise LifecycleError("lifecycle_storage_unavailable") from None
        raise


def _values(observation, attempt_id, binding):
    if type(observation) is not ApiWindowConsumptionObservation or not _hex(observation.input_sha256):
        raise LifecycleError("lifecycle_consumption_invalid")
    proof = observation.receipt
    if (type(proof) is not WindowReceipt or proof.provider != "api" or proof.status != "confirmed"
            or not _hex(proof.packet_sha256) or not _hex(binding)
            or not _valid_id(attempt_id) or not _session_id(proof.conversation_id)
            or not _session_id(proof.version_id) or type(proof.source_cursor) is not SourceCursor
            or not all(_valid_id(value) for value in (
                proof.from_window_id, proof.target_window_id, proof.model_run_id, proof.source_cursor.generation,
            ))
            or proof.target_window_id == proof.from_window_id
            or type(proof.context_epoch) is not int or not 1 <= proof.context_epoch < 2**63
            or type(proof.source_cursor.position) is not int or not 0 <= proof.source_cursor.position < 2**63
            or type(proof.message_cursor) is not int or not 0 <= proof.message_cursor < 2**63):
        raise LifecycleError("lifecycle_consumption_invalid")
    return (
        proof.packet_sha256, observation.input_sha256, binding, proof.conversation_id, attempt_id, proof.version_id,
        proof.from_window_id, proof.target_window_id, proof.model_run_id, proof.context_epoch,
        proof.source_cursor.generation, proof.source_cursor.position, proof.message_cursor,
    )


def _read(conn, digest, binding):
    _schema(conn)
    # Bound stored text before passing any fields through the application.
    row = conn.execute(
        f"""SELECT CASE WHEN length(input_sha256)=64 THEN input_sha256 END,
            CASE WHEN length(conversation_id)<=160 THEN conversation_id END,
            CASE WHEN length(attempt_request_id)<=160 THEN attempt_request_id END,
            CASE WHEN length(version_id)<=160 THEN version_id END,
            CASE WHEN length(from_window_id)<=160 THEN from_window_id END,
            CASE WHEN length(target_window_id)<=160 THEN target_window_id END,
            CASE WHEN length(model_run_id)<=160 THEN model_run_id END,context_epoch,
            CASE WHEN length(source_generation)<=160 THEN source_generation END,source_position,message_cursor,
            CASE WHEN length(record_sha256)=64 THEN record_sha256 END
            FROM main.{TABLE} WHERE packet_sha256=? AND access_binding=?""",
        (digest, binding),
    ).fetchone()
    if row is None:
        return None
    input_sha256, cid, attempt_id, version, source, target, run, epoch, generation, position, message, checksum = row
    proof = WindowReceipt(cid, "api", version, source, target, run, epoch, digest,
                          SourceCursor(generation, position), message, "confirmed")
    observation = ApiWindowConsumptionObservation(proof, input_sha256)
    if not _hex(input_sha256) or _checksum(_values(observation, attempt_id, binding)) != checksum:
        raise LifecycleError("lifecycle_consumption_corrupt")
    attempt = _row(conn, cid, request_id=attempt_id)
    if attempt is None or (attempt[3], attempt[5]) != (digest, binding):
        raise LifecycleError("lifecycle_consumption_corrupt")
    return observation


class ApiWindowConsumptionStore:
    """Persist host-observed consumption, then serve the existing runtime read protocol.

    The host supplies a read-only observer of the target run's loaded input and
    both consumed cursors under the same authority fence as the runtime snapshot.
    """

    def __init__(self, engine, journal, *, observe_consumption=None, enabled=False):
        self.enabled = enabled is True
        if self.enabled and (
            type(engine) is not SessionLifecycle or type(journal) is not WindowAttemptJournal
            or not engine.enabled or not journal.enabled or engine.repository is not journal.repository
            or not callable(observe_consumption)
            or inspect.iscoroutinefunction(observe_consumption) or inspect.isasyncgenfunction(observe_consumption)
            or inspect.isgeneratorfunction(observe_consumption)
        ):
            raise LifecycleError("lifecycle_consumption_binding_invalid")
        self.engine = engine
        self.journal = journal
        self.observe_consumption = observe_consumption

    def record(self, conversation_id, attempt_request_id, *, access):
        """Record a trusted positive observation while the original attempt is prepared."""
        if not self.enabled:
            raise LifecycleError("lifecycle_disabled")
        engine = self.engine
        try:
            binding = self.journal._check(conversation_id, attempt_request_id, access)
            with engine.authority.fence(conversation_id, access=access) as snapshot:
                engine._snapshot(snapshot, conversation_id)
                with engine.repository.transaction(conversation_id) as tx:
                    state = self.journal._bound(tx, binding)
                    engine._bound(state, snapshot, binding)
                    engine._current(state, snapshot)
                    attempt = _row(tx.conn, conversation_id, request_id=attempt_request_id)
                    if attempt is None:
                        raise LifecycleError("lifecycle_attempt_missing")
                    if (state.closeout_id, state.packet_sha256, state.revision, binding) != (
                        attempt[1], attempt[3], attempt[4], attempt[5],
                    ):
                        raise LifecycleError("lifecycle_attempt_state_changed")
                    _require_current_message_cursor(tx.conn, state)
                    scan = engine._start_source_scan(state, snapshot, access)
                    packet, digest = engine._packet(state, snapshot, access)
                    if digest != state.packet_sha256:
                        raise LifecycleError("lifecycle_packet_changed")

                    expected = WindowReceipt(
                        conversation_id, "api", state.version_id, state.window_id, state.target_window_id,
                        snapshot.model_run_id, state.context_epoch + 1, digest,
                        state.source_cursor, state.message_cursor, "confirmed",
                    )
                    if ((snapshot.window_id, snapshot.context_epoch, snapshot.observed_source_cursor,
                         snapshot.observed_message_cursor) != (expected.target_window_id, expected.context_epoch,
                         expected.source_cursor, expected.message_cursor)
                            or snapshot.model_run_id in state.used_model_runs):
                        raise LifecycleError("lifecycle_consumption_unconfirmed")
                    observed = self.observe_consumption(digest, access=access)
                    if inspect.iscoroutine(observed) or inspect.isgenerator(observed):
                        observed.close()
                        raise LifecycleError("lifecycle_consumption_invalid")
                    if observed is None:
                        raise LifecycleError("lifecycle_consumption_unconfirmed")
                    values = _values(observed, attempt_request_id, binding)
                    if observed.receipt != expected:
                        raise LifecycleError("lifecycle_consumption_mismatch")
                    prepared = _materialize_api_window_input(tx, packet, digest)
                    if observed.input_sha256 != prepared.input_sha256:
                        raise LifecycleError("lifecycle_consumption_input_mismatch")

                    prior = _read(tx.conn, digest, binding)
                    if prior is None:
                        tx.conn.execute(
                            f"INSERT INTO main.{TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (*values, _checksum(values)),
                        )
                    elif prior != observed:
                        raise LifecycleError("lifecycle_consumption_conflict")
                    if engine.authority.validate(snapshot, access=access) is not True:
                        raise LifecycleError("lifecycle_authority_changed")
                    engine._check_source_scan(scan, access)
                return observed.receipt
        except ProjectionError:
            raise LifecycleError("lifecycle_evidence_unavailable") from None
        except OSError:
            raise LifecycleError("lifecycle_storage_unavailable") from None

    def read_receipt(self, packet_sha256, *, access):
        observation = self.read_observation(packet_sha256, access=access)
        return observation.receipt if observation is not None else None

    def read_observation(self, packet_sha256, *, access):
        """Read durable historical evidence by packet digest and scope."""
        if not self.enabled:
            return None
        if not _hex(packet_sha256):
            raise LifecycleError("lifecycle_consumption_invalid")
        try:
            binding = _binding(access)
            with closing(self.engine.repository._connect()) as conn:
                if conn.in_transaction or conn.isolation_level is not None:
                    raise LifecycleError("lifecycle_connection_invalid")
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                return _read(conn, packet_sha256, binding)
        except ProjectionError:
            raise LifecycleError("lifecycle_access_denied") from None
        except (sqlite3.Error, OSError):
            raise LifecycleError("lifecycle_storage_unavailable") from None

    def read_receipt_in_transaction(self, tx, packet, digest, *, access):
        """Recheck loaded bytes in the canonical activation transaction."""
        if not self.enabled:
            return None
        if tx.view.conversation_id != packet.conversation_id or tx.view.provider != "api":
            raise LifecycleError("lifecycle_consumption_binding_invalid")
        observation = _read(tx.conn, digest, _binding(access))
        if observation is None:
            return None
        prepared = _materialize_api_window_input(tx, packet, digest)
        if observation.input_sha256 != prepared.input_sha256:
            raise LifecycleError("lifecycle_consumption_input_mismatch")
        return observation.receipt
