"""Disposable canonical sessions wired through the existing GM-02AS write path."""

from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
import json
import subprocess
import sys

from backend import channel_store, deployment_config
from backend.g3_projection_contract import AccessScope
from backend.session_lifecycle import LifecycleDeltaPage, SessionLifecycle, SessionSnapshot, SourceCursor, WindowReceipt, _json
from backend.session_lifecycle_storage import JsonSessionReader, SQLiteLifecycleRepository, _selected, install_schema
from backend.session_window_activation_status import inspect_recorded_activation
from backend.session_window_api_consumption import ApiWindowConsumptionObservation, ApiWindowConsumptionStore
from backend.session_window_api_consumption import install_schema as install_consumption_schema
from backend.session_window_attempts import WindowAttemptJournal, install_schema as install_attempt_schema
from backend.session_window_api_input import FORMAT, prepare_api_window_input
from backend.session_window_attempt_activation import activate_recorded_attempt
from backend.session_window_attempt_dispatch import attempt_packet_dispatch_once
from .authority import Denied

ATTEMPT = "formal_attempt"
ACTIVATION = "formal_activation"


def provision_case(path, cid):
    with closing(channel_store.connect(str(path))) as conn:
        conn.execute(channel_store.RELAY_TABLE_DDL["messages"])
        conn.executescript("""
            CREATE TABLE formal_runtime(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            CREATE TABLE web_message_versions(version_id TEXT PRIMARY KEY,root_id TEXT NOT NULL);
            CREATE TABLE web_version_selections(root_id TEXT PRIMARY KEY,version_id TEXT NOT NULL);
        """)
        conn.execute("INSERT INTO messages(ts,kind,direction,text,meta) VALUES (?,?,?,?,?)",
                     ("2026-11-18T00:00:00Z", "user", "in", "Dedicated session test message.",
                      json.dumps({"api_session": cid})))
        conn.execute("INSERT INTO formal_runtime VALUES(1,?)", (json.dumps({
            "window_id": "window_0", "model_run_id": "run_0", "epoch": 0,
            "observed_source": 0, "observed_message": 0,
        }),))
        install_schema(conn)
        install_attempt_schema(conn)
        install_consumption_schema(conn)
    deployment_config.atomic_write_text(path.with_suffix(".json"), json.dumps({
        "api_session": cid, "sessions": [{"id": cid, "title": "Formal auth test",
        "provider": "api", "since_id": 0, "created_at": "2026-11-18T00:00:00Z"}],
    }))


class CanonicalSession:
    def __init__(self, path, grant=None, reader=None, *, test_control=False):
        self.path = path
        self.grant, self.client = grant, reader
        self.writable = grant is not None or test_control
        config = JsonSessionReader(path.with_suffix(".json"), historical_provider=lambda _: None)
        self.repo = SQLiteLifecycleRepository(self.connect, config, enabled=True)
        self.engine = SessionLifecycle(self.repo, self, reader, None, enabled=True,
                                       supported_providers=frozenset({"api"}))
        self.conn = self.connect()

    def connect(self):
        if self.writable and self.path.is_file():
            return channel_store.connect(str(self.path))
        return channel_store.connect_read_only(self.path, timeout_seconds=5)

    def close(self):
        self.conn.close()

    def get(self, key):
        if key != "runtime":
            raise ValueError("unknown runtime field")
        return json.loads(self.conn.execute("SELECT payload FROM formal_runtime WHERE id=1").fetchone()[0])

    def put(self, key, value):
        if key != "runtime":
            raise ValueError("unknown runtime field")
        self.conn.execute("UPDATE formal_runtime SET payload=? WHERE id=1", (json.dumps(value),))

    @contextmanager
    def fence(self, cid, *, access):
        # The gateway owns the shared authority fence across this entire call.
        yield self.snapshot(cid, access)

    def validate(self, snapshot, *, access):
        return self.snapshot(snapshot.conversation_id, access) == snapshot

    def runtime(self, observer):
        journal = WindowAttemptJournal(self.repo, enabled=True)
        store = ApiWindowConsumptionStore(self.engine, journal, observe_consumption=observer, enabled=True)
        self.engine.runtime = store
        return journal, store

    def resolve_version(self, cid):
        with self.repo.observation(cid) as tx:
            return _selected(tx.conn, tx.view)

    def snapshot(self, cid, access):
        if self.grant is None or cid != self.grant.cid or access != self.client.access:
            raise Denied()
        version = self.resolve_version(cid)
        if version != self.grant.version:
            raise Denied()
        runtime = self.get("runtime")
        if runtime["epoch"] and (runtime["window_id"], runtime["model_run_id"]) != (self.grant.target, self.grant.model_run):
            raise Denied()
        scan = self.client.begin_scan(after_cursor=0, access=access)
        recent = tuple(event.ref.event_id for event in self.client.list_events(after_cursor=0, access=access))
        if not self.client.validate_scan(scan, access=access):
            raise Denied()
        message_head = self.conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM messages WHERE json_extract(meta,'$.api_session')=?", (version,),
        ).fetchone()[0]
        snapshot = SessionSnapshot(
            cid, "api", version, runtime["window_id"], runtime["model_run_id"], runtime["epoch"], "pending_stamp",
            SourceCursor(scan.source_generation, runtime["observed_source"]), runtime["observed_message"],
            SourceCursor(scan.source_generation, scan.upper_cursor), message_head, (), (), (), recent,
        )
        return replace(snapshot, stamp=sha256(_json([asdict(snapshot), scan.authorization_epoch])).hexdigest())

    def read_delta(self, snapshot, *, source_after, message_after, access):
        scan = self.client.begin_scan(after_cursor=source_after, access=access)
        page = self.client.read_page(scan, after_cursor=source_after, limit=100, access=access)
        if scan.upper_cursor != snapshot.source_head.position:
            raise Denied()
        ids = tuple(row[0] for row in self.conn.execute(
            "SELECT id FROM messages WHERE id>? AND id<=? AND json_extract(meta,'$.api_session')=? "
            "ORDER BY id LIMIT 100", (message_after, snapshot.message_head, snapshot.version_id),
        ))
        head = ids[-1] if ids else snapshot.message_head
        return LifecycleDeltaPage(snapshot.stamp, source_after, page.next_cursor, message_after, head,
                                  page.events, ids, page.done and head == snapshot.message_head)

    def prepare(self):
        grant = self.grant
        cid, access, engine = grant.cid, self.client.access, self.engine
        if self.conn.execute("SELECT 1 FROM session_lifecycle_records").fetchone():
            raise Denied(409, "formal_attempt_exists")
        observed = None
        journal, store = self.runtime(lambda *_a, **_kw: observed)
        engine.closeout(cid, "formal_close", window_id="window_0", context_epoch=0, access=access)
        engine.reentry(cid, "formal_prepare", closeout_id="formal_close", target_window_id=grant.target, access=access)
        prepared = prepare_api_window_input(engine, cid, access=access, enabled=True)
        authorization = engine.packet(cid, access=access).source_authorization_epoch

        def consume(attempt, packet):
            nonlocal observed
            if packet.target_window_id != grant.target:
                raise Denied()
            # The isolated target receives the actual canonical input bytes and
            # computes its own hash. It makes no external model request.
            reply = subprocess.run(
                [sys.executable, "-c",
                 "import sys,json,hashlib; from backend.session_lifecycle import _json; "
                 "value=json.load(sys.stdin); print(hashlib.sha256(_json(value)).hexdigest())"],
                input=json.dumps([FORMAT, attempt.packet_sha256, prepared.as_message()["role"],
                                  prepared.as_message()["content"]]),
                text=True, stdout=subprocess.PIPE, timeout=5, check=True,
            )
            proof = WindowReceipt(cid, "api", grant.version, packet.from_window_id, grant.target, grant.model_run,
                                  packet.context_epoch, attempt.packet_sha256, packet.source_cursor,
                                  packet.message_cursor, "confirmed")
            observed = ApiWindowConsumptionObservation(proof, reply.stdout.strip())

        with self.repo.observation(cid) as tx:
            revision = tx.load().revision
        attempt_packet_dispatch_once(engine, journal, cid, ATTEMPT, packet_sha256=prepared.packet_sha256,
                                     expected_revision=revision, access=access, dispatch=consume, enabled=True)
        proof = observed.receipt
        run = self.get("runtime")
        run.update(window_id=proof.target_window_id, model_run_id=proof.model_run_id, epoch=proof.context_epoch,
                   observed_source=proof.source_cursor.position, observed_message=proof.message_cursor)
        self.put("runtime", run)
        store.record(cid, ATTEMPT, access=access)
        _check = store.read_observation(prepared.packet_sha256, access=access)
        if _check != observed:
            raise RuntimeError("formal_consumption_roundtrip_failed")
        return {"packet_sha256": prepared.packet_sha256, "input_sha256": observed.input_sha256,
                "conversation_id": cid, "version_id": grant.version, "target_window_id": grant.target,
                "model_run_id": grant.model_run, "context_epoch": proof.context_epoch,
                "attempt_id": ATTEMPT, "activation_id": ACTIVATION,
                "authorization_sha256": authorization}

    def activate(self, request_id, packet_sha256):
        journal, _store = self.runtime(lambda *_a, **_kw: None)
        receipt = activate_recorded_attempt(
            self.engine, journal, self.grant.cid, request_id, attempt_request_id=ATTEMPT,
            packet_sha256=packet_sha256, access=self.client.access, enabled=True,
        )
        return {"window_confirmed": receipt.window_confirmed, "replayed": receipt.replayed}

    def history(self, row):
        journal, store = self.runtime(lambda *_a, **_kw: None)
        access = AccessScope(row["authority_alias"], frozenset({row["scope"]}))
        status = inspect_recorded_activation(journal, row["cid"], row["activation_id"],
                                             attempt_request_id=row["attempt_id"], packet_sha256=row["packet_sha256"],
                                             access=access, enabled=True)
        receipt = store.read_observation(row["packet_sha256"], access=access)
        if receipt is None:
            raise Denied()
        return {"packet_sha256": receipt.receipt.packet_sha256, "input_sha256": receipt.input_sha256,
                "current_window_confirmed": status["current_window_confirmed"],
                "current_authorization": False, "retry_authorized": status["retry_authorized"],
                "activation_at_apply": status["window_confirmed_at_apply"], "state": status["state"]}
