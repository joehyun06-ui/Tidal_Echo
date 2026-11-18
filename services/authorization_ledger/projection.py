"""Apply detached PostgreSQL snapshots to a synthetic Relay SQLite projection."""

import sqlite3
from contextlib import closing

from .contracts import LedgerError, canonical, require_counter, require_id

_FIELDS = {
    "incarnation", "conversation_id", "projection_revision", "version_id", "window_id",
    "model_run_id", "context_epoch",
}


class RelayProjection:
    def __init__(self, path):
        self.path = path

    def install(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS projection_generation (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    incarnation INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_projection (
                    conversation_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload BLOB NOT NULL
                );
            """)

    def bind_incarnation(self, incarnation):
        """Called by the trusted projection worker during recovery, before delivery."""
        require_counter(incarnation, 1)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT incarnation FROM projection_generation WHERE singleton=1").fetchone()
            if row and incarnation < row[0]:
                raise LedgerError("stale_incarnation")
            if row and incarnation == row[0]:
                return False
            conn.execute("DELETE FROM conversation_projection")
            conn.execute("INSERT OR REPLACE INTO projection_generation VALUES (1,?)", (incarnation,))
            return True

    def apply(self, snapshot):
        if type(snapshot) is not dict or set(snapshot) != _FIELDS:
            raise LedgerError("invalid_projection")
        incarnation, revision, cid = (snapshot[key] for key in
                                      ("incarnation", "projection_revision", "conversation_id"))
        require_counter(incarnation, 1)
        require_counter(revision, 1)
        require_counter(snapshot["context_epoch"])
        for key in ("conversation_id", "version_id", "window_id", "model_run_id"):
            require_id(snapshot[key])
        payload = canonical(snapshot)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            expected = conn.execute("SELECT incarnation FROM projection_generation WHERE singleton=1").fetchone()
            if expected is None or incarnation != expected[0]:
                raise LedgerError("stale_incarnation")
            row = conn.execute(
                "SELECT revision,payload FROM conversation_projection WHERE conversation_id=?", (cid,),
            ).fetchone()
            if row and revision <= row[0]:
                if revision == row[0] and payload != row[1]:
                    raise LedgerError("projection_conflict")
                return False
            conn.execute("INSERT OR REPLACE INTO conversation_projection VALUES (?,?,?)", (cid, revision, payload))
            return True
