"""Durable identity/session grants and the scoped read-only G3 capability."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest, digest
from secrets import token_urlsafe
import fcntl
import sqlite3
import time
from uuid import uuid4

from backend.g3_projection_contract import AccessScope, G3Event, G3SourceRef, ProjectionError
from backend.g3_projection_feed import G3FeedPage, G3Scan, _binding
from backend.session_lifecycle import _json


class Denied(ValueError):
    def __init__(self, status=403, category="formal_access_denied"):
        self.status, self.category = status, category
        super().__init__(category)


def _hash(value):
    return sha256(value.encode()).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS identities(
    issuer TEXT NOT NULL, subject TEXT NOT NULL, principal TEXT NOT NULL UNIQUE, auditor INTEGER NOT NULL,
    PRIMARY KEY(issuer,subject));
CREATE TABLE IF NOT EXISTS logins(
    login_hash TEXT PRIMARY KEY, principal TEXT NOT NULL, expires INTEGER NOT NULL,
    login_epoch TEXT NOT NULL UNIQUE, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS canonical_grants(
    cid TEXT PRIMARY KEY, principal TEXT NOT NULL, version TEXT NOT NULL,
    dataset TEXT NOT NULL, source_id TEXT NOT NULL, target TEXT NOT NULL, model_run TEXT NOT NULL,
    scope_epoch INTEGER NOT NULL DEFAULT 1, credential_epoch INTEGER NOT NULL DEFAULT 1,
    scope_active INTEGER NOT NULL DEFAULT 1, credential_active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS audit_grants(principal TEXT NOT NULL, cid TEXT NOT NULL, PRIMARY KEY(principal,cid));
CREATE TABLE IF NOT EXISTS source_history(
    cursor INTEGER PRIMARY KEY, dataset TEXT NOT NULL, source_id TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL, committed_at TEXT NOT NULL, body BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS formal_metadata(generation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS formal_history(
    cid TEXT PRIMARY KEY, authority_alias TEXT NOT NULL, scope TEXT NOT NULL,
    packet_sha256 TEXT NOT NULL, attempt_id TEXT NOT NULL, activation_id TEXT NOT NULL);
"""


@dataclass(frozen=True)
class Grant:
    cid: str
    principal: str
    version: str
    dataset: str
    source_id: str
    target: str
    model_run: str
    scope_epoch: int
    credential_epoch: int
    scope_active: int
    credential_active: int


class Authority:
    def __init__(self, root, reader_key):
        self.path = root / "formal_authority.sqlite3"
        self.lock = root / "authorization.lock"
        self.reader_key = reader_key

    @contextmanager
    def fence(self, *, mutate=False):
        # Every test control-plane writer uses this file. Hold the read lock
        # over lifecycle commit, not merely the authorization network/read call.
        with self.lock.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if mutate else fcntl.LOCK_SH)
            yield

    @contextmanager
    def connect(self, *, write=False):
        suffix = "?mode=rwc" if write else "?mode=ro"
        with closing(sqlite3.connect(self.path.as_uri() + suffix, uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn

    def provision(self, issuer, accounts, cases, events):
        with self.fence(mutate=True), self.connect(write=True) as conn:
            conn.executescript(SCHEMA)
            if conn.execute("SELECT 1 FROM formal_metadata").fetchone() is not None:
                raise Denied()
            conn.execute("INSERT INTO formal_metadata VALUES (?)", (uuid4().hex,))
            conn.executemany("INSERT INTO identities VALUES (?,?,?,?)", (
                (issuer, subject, principal, principal == "audit") for principal, subject in accounts.items()
            ))
            for cid, principal, dataset, source in cases:
                conn.execute(
                    "INSERT INTO canonical_grants(cid,principal,version,dataset,source_id,target,model_run) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (cid, principal, cid, dataset, source, "target_" + cid, "run_" + cid),
                )
                conn.execute("INSERT INTO audit_grants VALUES ('audit',?)", (cid,))
            committed = datetime.now(timezone.utc).isoformat()
            for dataset, source, event in events:
                conn.execute(
                    "INSERT INTO source_history(dataset,source_id,event_id,revision,committed_at,body) VALUES(?,?,?,?,?,?)",
                    (dataset, source, event, 1, committed, b"GM-02AS formally authenticated dedicated test event."),
                )

    def login(self, verified):
        with self.fence(mutate=True), self.connect(write=True) as conn:
            row = conn.execute("SELECT principal FROM identities WHERE issuer=? AND subject=?",
                               (verified.issuer, verified.subject)).fetchone()
            if row is None:
                raise Denied()
            raw = token_urlsafe(32)
            conn.execute("INSERT INTO logins(login_hash,principal,expires,login_epoch) VALUES (?,?,?,?)",
                         (_hash(raw), row["principal"], int(time.time()) + 900, uuid4().hex))
            return raw

    @staticmethod
    def identity(conn, raw_token):
        if not raw_token or len(raw_token) != 43:
            raise Denied(401, "formal_login_required")
        row = conn.execute(
            "SELECT l.*,i.issuer,i.subject,i.auditor FROM logins l JOIN identities i USING(principal) "
            "WHERE login_hash=? AND revoked=0 AND expires>?", (_hash(raw_token), int(time.time())),
        ).fetchone()
        if row is None:
            raise Denied(401, "formal_login_required")
        return row

    @staticmethod
    def grant(conn, cid, principal):
        row = conn.execute("SELECT * FROM canonical_grants WHERE cid=? AND principal=?", (cid, principal)).fetchone()
        if row is None or not row["scope_active"] or not row["credential_active"]:
            raise Denied()
        return Grant(**dict(row))

    def issue_reader(self, conn, raw_login, cid):
        identity = self.identity(conn, raw_login)
        grant = self.grant(conn, cid, identity["principal"])
        tag = self._tag(identity, grant)
        return grant, ScopedG3Reader(self, identity["login_hash"], grant.cid, tag,
                                     AccessScope("formal_" + _hash("alias:" + tag), frozenset({grant.dataset})))

    def _tag(self, login, grant):
        # A non-exported capability and version binding, with a stable
        # principal alias for the lifecycle repository and v2 receipt.
        return digest(self.reader_key, _json([
            login["issuer"], login["subject"], login["login_epoch"], grant.cid, grant.version,
            grant.dataset, grant.source_id, grant.target, grant.model_run,
            grant.scope_epoch, grant.credential_epoch, "g3:read",
        ]), "sha256").hex()

    def reader_current(self, conn, login_hash, cid, tag):
        row = conn.execute(
            "SELECT l.*,i.issuer,i.subject FROM logins l JOIN identities i USING(principal) "
            "WHERE login_hash=? AND revoked=0 AND expires>?", (login_hash, int(time.time())),
        ).fetchone()
        if row is None:
            raise Denied()
        grant = self.grant(conn, cid, row["principal"])
        if not compare_digest(self._tag(row, grant), tag):
            raise Denied()
        return grant

    def revoke(self, cid, kind, *, raw_login=None):
        with self.fence(mutate=True), self.connect(write=True) as conn:
            if kind == "scope":
                changed = conn.execute(
                    "UPDATE canonical_grants SET scope_active=0,scope_epoch=scope_epoch+1 WHERE cid=? AND scope_active=1",
                    (cid,),
                )
            elif kind == "credential":
                changed = conn.execute(
                    "UPDATE canonical_grants SET credential_active=0,credential_epoch=credential_epoch+1 "
                    "WHERE cid=? AND credential_active=1", (cid,),
                )
            elif kind == "login":
                changed = conn.execute("UPDATE logins SET revoked=1 WHERE login_hash=? AND revoked=0",
                                       (_hash(raw_login),))
            else:
                raise Denied()
            if changed.rowcount != 1:
                raise Denied()


class ScopedG3Reader:
    """G3 refuses access using its own persisted grant and credential check."""

    require_authorization_epoch = True

    def __init__(self, authority, login, cid, credential, access):
        self.authority, self.login, self.cid = authority, login, cid
        self._credential, self.access = credential, access

    @contextmanager
    def _read(self, access):
        if access != self.access:
            raise ProjectionError("g3_formal_scope_denied")
        try:
            with self.authority.connect() as conn:
                conn.execute("BEGIN")
                grant = self.authority.reader_current(conn, self.login, self.cid, self._credential)
                yield conn, grant
        except Denied:
            raise ProjectionError("g3_formal_scope_denied") from None

    @staticmethod
    def _event(row):
        ref = G3SourceRef(row["event_id"], row["revision"], row["source_id"],
                          sha256(row["body"]).hexdigest(), row["dataset"])
        return G3Event(ref, row["cursor"], row["committed_at"], "active", "short_term", "event")

    def _scan(self, conn, after):
        generation = conn.execute("SELECT generation FROM formal_metadata").fetchone()[0]
        upper = conn.execute("SELECT COALESCE(MAX(cursor),0) FROM source_history").fetchone()[0]
        if type(after) is not int or not 0 <= after <= upper:
            raise ProjectionError("g3_formal_scope_denied")
        binding = _binding(self.access)
        epoch = sha256(_json([self.access.principal_id, generation])).hexdigest()
        scan_id = sha256(_json([generation, upper, after, epoch])).hexdigest()
        return G3Scan(scan_id, generation, str(upper), epoch, binding, after, upper)

    def begin_scan(self, *, after_cursor, access):
        with self._read(access) as (conn, _grant):
            return self._scan(conn, after_cursor)

    def validate_scan(self, scan, *, access):
        with self._read(access) as (conn, _grant):
            return scan == self._scan(conn, scan.after_cursor)

    def read_page(self, scan, *, after_cursor, limit, access):
        with self._read(access) as (conn, grant):
            if (scan != self._scan(conn, scan.after_cursor)
                    or type(after_cursor) is not int or not scan.after_cursor <= after_cursor <= scan.upper_cursor
                    or type(limit) is not int or not 1 <= limit <= 200):
                raise ProjectionError("g3_formal_scope_denied")
            rows = conn.execute("SELECT * FROM source_history WHERE cursor>? AND cursor<=? ORDER BY cursor LIMIT ?",
                                (after_cursor, scan.upper_cursor, limit)).fetchall()
            next_cursor = rows[-1]["cursor"] if rows else scan.upper_cursor
            events = tuple(self._event(row) for row in rows
                           if (row["dataset"], row["source_id"]) == (grant.dataset, grant.source_id))
            return G3FeedPage(scan.scan_id, after_cursor, next_cursor, events, next_cursor == scan.upper_cursor)

    def list_events(self, *, after_cursor, access):
        with self._read(access) as (conn, grant):
            scan = self._scan(conn, after_cursor)
            rows = conn.execute(
                "SELECT * FROM source_history WHERE cursor>? AND cursor<=? AND dataset=? AND source_id=? ORDER BY cursor",
                (after_cursor, scan.upper_cursor, grant.dataset, grant.source_id),
            )
            return tuple(self._event(row) for row in rows)

    @staticmethod
    def _row(conn, grant, event_id):
        row = conn.execute("SELECT * FROM source_history WHERE dataset=? AND source_id=? AND event_id=?",
                           (grant.dataset, grant.source_id, event_id)).fetchone()
        if row is None:
            raise ProjectionError("g3_formal_scope_denied")
        return row

    def read_event(self, event_id, *, revision=None, access):
        with self._read(access) as (conn, grant):
            event = self._event(self._row(conn, grant, event_id))
            if revision is not None and (type(revision) is not int or revision != event.ref.revision):
                raise ProjectionError("g3_formal_scope_denied")
            return event

    def open_source(self, ref, *, access):
        if type(ref) is not G3SourceRef:
            raise ProjectionError("g3_formal_scope_denied")
        with self._read(access) as (conn, grant):
            row = self._row(conn, grant, ref.event_id)
            if self._event(row).ref != ref:
                raise ProjectionError("g3_formal_scope_denied")
            return row["body"]

    def read_revocations(self, *, after_cursor, access):
        # Grant tombstones are applied above even for requests using old scans.
        with self._read(access) as (conn, _grant):
            self._scan(conn, after_cursor)
            return ()
