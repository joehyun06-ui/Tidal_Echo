"""Dedicated test authority. The only dataset contains generated test events."""

from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from backend.g3_projection_contract import AccessScope, G3Event, G3SourceRef, ProjectionError
from backend.g3_projection_feed import G3Scan, _binding

DATASET = "gm02as-g3-test"
PRINCIPAL = "gm02as-g3-reader"
ACCESS = AccessScope(PRINCIPAL, frozenset({DATASET}))
DISK = Path("/var/data/g3-authority-test")
ID = re.compile(r"test-[0-9a-f]{32}\Z")


def require_token(supplied, key):
    expected = os.environ.get(key, "")
    if (len(expected) < 32 or not supplied or len(supplied) > 256
            or not compare_digest(supplied.encode(), expected.encode())):
        raise ProjectionError("g3_test_access_denied")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _event(row):
    ref = G3SourceRef(row["event_id"], row["revision"], row["source_id"], row["source_sha256"], row["scope"])
    return G3Event(ref, row["cursor"], row["committed_at"], row["status"], "short_term", "event")


class AuthorityStore:
    def __init__(self, directory):
        self.path = directory / "authority.sqlite3"

    @classmethod
    def deployed(cls):
        if (os.environ.get("G3_AUTHORITY_TEST") != "1"
                or os.environ.get("RENDER_SERVICE_NAME") != "g3-authority-test"
                or os.environ.get("RENDER_EXTERNAL_URL")):
            raise RuntimeError("g3_test_service_required")
        mounts = Path("/proc/self/mountinfo").read_text().splitlines()
        if not any(line.split()[4] == str(DISK) for line in mounts):
            raise RuntimeError("g3_test_dedicated_disk_required")
        return cls(DISK)

    def initialize(self):
        schema = Path(__file__).with_name("migrations").joinpath("001.sql").read_text()
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=5)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(schema)
            conn.execute("INSERT OR IGNORE INTO authority VALUES (1, ?, 0)", (uuid4().hex,))

    @contextmanager
    def _transaction(self, *, write=False):
        mode = "rw" if write else "ro"
        with closing(sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            with conn:
                yield conn

    def seed_test_event(self, event_id, *, token):
        require_token(token, "G3_TEST_ADMIN_TOKEN")
        if not ID.fullmatch(event_id):
            raise ProjectionError("g3_test_input_invalid")
        body = b"GM-02AS dedicated authority acceptance event. Test content only."
        with self._transaction(write=True) as conn:
            previous = conn.execute("SELECT * FROM revisions WHERE event_id=? AND revision=1", (event_id,)).fetchone()
            if previous is not None:
                return _event(previous)
            cursor = conn.execute(
                """INSERT INTO revisions(event_id,revision,source_id,source_sha256,scope,committed_at,status,body)
                   VALUES (?,1,?,?,?,?,'active',?)""",
                (event_id, uuid4().hex, sha256(body).hexdigest(), DATASET, _now(), body),
            ).lastrowid
            conn.execute("INSERT INTO grants VALUES (?,?,?,0)", (event_id, PRINCIPAL, DATASET))
            conn.execute("UPDATE authority SET authorization_epoch=authorization_epoch+1")
            return _event(conn.execute("SELECT * FROM revisions WHERE cursor=?", (cursor,)).fetchone())

    def revoke_test_event(self, event_id, *, token):
        require_token(token, "G3_TEST_ADMIN_TOKEN")
        if not ID.fullmatch(event_id):
            raise ProjectionError("g3_test_input_invalid")
        with self._transaction(write=True) as conn:
            head = conn.execute(
                "SELECT * FROM revisions WHERE event_id=? ORDER BY revision DESC LIMIT 1", (event_id,),
            ).fetchone()
            if head is None:
                raise ProjectionError("g3_test_reference_denied")
            if head["status"] == "revoked":
                return _event(head)
            cursor = conn.execute(
                """INSERT INTO revisions(event_id,revision,source_id,source_sha256,scope,committed_at,status,body)
                   VALUES (?,?,?,?,?,?,'revoked',NULL)""",
                (event_id, head["revision"] + 1, head["source_id"], head["source_sha256"], DATASET, _now()),
            ).lastrowid
            conn.execute(
                "UPDATE grants SET revoked=1 WHERE event_id=? AND principal=? AND scope=?",
                (event_id, PRINCIPAL, DATASET),
            )
            conn.execute("UPDATE authority SET authorization_epoch=authorization_epoch+1")
            return _event(conn.execute("SELECT * FROM revisions WHERE cursor=?", (cursor,)).fetchone())

    @staticmethod
    def _scan(conn, after):
        generation, epoch = conn.execute("SELECT generation,authorization_epoch FROM authority").fetchone()
        upper = conn.execute("SELECT COALESCE(MAX(cursor),0) FROM revisions").fetchone()[0]
        if type(after) is not int or not 0 <= after <= upper:
            raise ProjectionError("g3_test_cursor_invalid")
        binding = _binding(ACCESS)
        identity = sha256(json.dumps([generation, epoch, after, upper, binding]).encode()).hexdigest()
        return G3Scan(identity, generation, str(upper), str(epoch), binding, after, upper)

    @staticmethod
    def _current_event(conn, event_id):
        row = conn.execute(
            """SELECT r.* FROM revisions r JOIN grants g USING (event_id,scope)
               WHERE r.event_id=? AND g.principal=? AND g.scope=? AND g.revoked=0
               ORDER BY r.revision DESC LIMIT 1""",
            (event_id, PRINCIPAL, DATASET),
        ).fetchone()
        if row is None or row["status"] != "active":
            raise ProjectionError("g3_test_reference_denied")
        return row

    def read(self, operation, arguments):
        with self._transaction() as conn:
            if operation == "scan":
                return asdict(self._scan(conn, arguments["after_cursor"]))
            if operation in ("page", "validate"):
                scan = G3Scan(**arguments["scan"])
                current = self._scan(conn, scan.after_cursor)
                if operation == "validate":
                    return scan == current
                after, limit = arguments["after_cursor"], arguments["limit"]
                if (scan != current or type(after) is not int or not scan.after_cursor <= after <= scan.upper_cursor
                        or type(limit) is not int or not 1 <= limit <= 200):
                    raise ProjectionError("g3_test_scan_changed")
                # Page in authoritative cursor order before filtering revoked revisions.
                rows = conn.execute(
                    """SELECT r.*, g.revoked FROM revisions r JOIN grants g USING (event_id,scope)
                       WHERE g.principal=? AND g.scope=? AND r.cursor>? AND r.cursor<=?
                       ORDER BY r.cursor LIMIT ?""",
                    (PRINCIPAL, DATASET, after, scan.upper_cursor, limit),
                ).fetchall()
                next_cursor = rows[-1]["cursor"] if rows else scan.upper_cursor
                return {"scan_id": scan.scan_id, "after_cursor": after, "next_cursor": next_cursor,
                        "done": next_cursor == scan.upper_cursor, "events": [
                            asdict(_event(row)) for row in rows if not row["revoked"]
                        ]}
            if operation == "revocations":
                after = arguments["after_cursor"]
                self._scan(conn, after)
                rows = conn.execute(
                    """SELECT r.event_id, r.revision, r.cursor FROM revisions r JOIN grants g USING (event_id,scope)
                       WHERE g.principal=? AND g.scope=? AND r.status='revoked' AND cursor>?
                       ORDER BY cursor LIMIT 1001""", (PRINCIPAL, DATASET, after),
                ).fetchall()
                if len(rows) > 1000:
                    raise ProjectionError("g3_test_read_budget_exceeded")
                return [dict(row) for row in rows]
            if operation == "source":
                ref = G3SourceRef(**arguments["ref"])
                row = self._current_event(conn, ref.event_id)
                if _event(row).ref != ref or sha256(row["body"]).hexdigest() != ref.source_sha256:
                    raise ProjectionError("g3_test_reference_denied")
                return row["body"].decode("utf-8")
            row = self._current_event(conn, arguments["event_id"])
            revision = arguments["revision"]
            if revision is not None and (type(revision) is not int or revision != row["revision"]):
                raise ProjectionError("g3_test_reference_denied")
            return asdict(_event(row))
