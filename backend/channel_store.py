"""Persistent Telegram mappings, jobs, completion identities, and outboxes."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str) -> sqlite3.Connection:
    try:
        timeout = float(os.environ.get("SQLITE_BUSY_TIMEOUT_SECONDS", "30"))
    except (TypeError, ValueError):
        timeout = 30.0
    timeout = timeout if timeout > 0 else 30.0
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {max(1, int(timeout * 1000))}")
    return conn


def _migration_001(conn: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE channel_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(channel, external_account_id))""",
        """CREATE TABLE channel_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, external_conversation_id TEXT NOT NULL,
            conversation_type TEXT NOT NULL, api_session TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(channel, external_account_id, external_conversation_id))""",
        """CREATE TABLE inbound_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, update_id TEXT NOT NULL, event_type TEXT NOT NULL,
            status TEXT NOT NULL, error_category TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(channel, external_account_id, update_id))""",
        """CREATE TABLE external_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, external_conversation_id TEXT NOT NULL,
            external_message_id TEXT NOT NULL, direction TEXT NOT NULL,
            canonical_message_id INTEGER, generation_id TEXT, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(channel, external_account_id, external_conversation_id, external_message_id))""",
        """CREATE TABLE generation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, inbound_message_id INTEGER NOT NULL UNIQUE,
            canonical_message_id INTEGER NOT NULL, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, external_conversation_id TEXT NOT NULL,
            api_session TEXT NOT NULL, stream_id TEXT, generation_id TEXT UNIQUE,
            reply_to TEXT, reply_message_id INTEGER, status TEXT NOT NULL, lease_until TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0, error_category TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(inbound_message_id) REFERENCES external_messages(id))""",
        """CREATE TABLE delivery_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, generation_job_id INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, external_conversation_id TEXT NOT NULL,
            payload_text TEXT NOT NULL, status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
            external_message_id TEXT, error_category TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(generation_job_id) REFERENCES generation_jobs(id))""",
        """CREATE TABLE channel_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, channel TEXT NOT NULL,
            external_id_hash TEXT, request_job_id TEXT, status TEXT NOT NULL, error_category TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE channel_rate_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, external_user_id TEXT NOT NULL,
            window_started_at TEXT NOT NULL, event_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(channel, external_account_id, external_user_id))""",
        "CREATE INDEX idx_generation_jobs_status_lease ON generation_jobs(status, lease_until, id)",
        "CREATE INDEX idx_delivery_attempts_status ON delivery_attempts(status, id)",
    )
    for statement in statements:
        conn.execute(statement)


def _migration_002(conn: sqlite3.Connection) -> None:
    # v1 is immutable. New state-machine columns and tables live in v2.
    conn.execute("ALTER TABLE generation_jobs ADD COLUMN dispatch_started_at TEXT")
    conn.execute("ALTER TABLE generation_jobs ADD COLUMN awaiting_reply_since TEXT")
    conn.execute("ALTER TABLE delivery_attempts ADD COLUMN retry_after_seconds INTEGER")
    conn.execute("ALTER TABLE channel_conversations ADD COLUMN external_user_id TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS telegram_completions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        completion_identity TEXT NOT NULL UNIQUE,
        generation_job_id INTEGER NOT NULL UNIQUE,
        canonical_message_id INTEGER NOT NULL UNIQUE,
        delivery_id INTEGER NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        FOREIGN KEY(generation_job_id) REFERENCES generation_jobs(id),
        FOREIGN KEY(delivery_id) REFERENCES delivery_attempts(id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS delivery_parts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        delivery_id INTEGER NOT NULL,
        part_index INTEGER NOT NULL,
        total_parts INTEGER NOT NULL,
        text_hash TEXT NOT NULL,
        text_length INTEGER NOT NULL,
        payload_text TEXT NOT NULL,
        status TEXT NOT NULL,
        telegram_message_id TEXT,
        error_category TEXT,
        retry_after_seconds INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(delivery_id, part_index),
        FOREIGN KEY(delivery_id) REFERENCES delivery_attempts(id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_parts_status ON delivery_parts(delivery_id,status,part_index)")


KELIVO_TABLE_DDL: dict[str, str] = {
    "kelivo_clients": """CREATE TABLE kelivo_clients (
            client_id TEXT PRIMARY KEY,
            api_session TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            mapping_revision INTEGER NOT NULL DEFAULT 1 CHECK(mapping_revision > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""",
    "kelivo_requests": """CREATE TABLE kelivo_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL,
            request_payload_hash TEXT NOT NULL,
            request_identity_hash TEXT NOT NULL,
            client_id TEXT NOT NULL,
            api_session TEXT NOT NULL,
            mapping_revision INTEGER NOT NULL CHECK(mapping_revision > 0),
            history_before_id INTEGER NOT NULL CHECK(history_before_id >= 0),
            context_bundle_json TEXT NOT NULL,
            context_bundle_hash TEXT NOT NULL,
            provider_messages_json TEXT NOT NULL,
            prompt_contract_version TEXT NOT NULL,
            persona_hash TEXT NOT NULL,
            persona_source TEXT NOT NULL,
            provider_model TEXT NOT NULL,
            effective_temperature REAL NOT NULL CHECK(effective_temperature >= 0 AND effective_temperature <= 2),
            effective_max_tokens INTEGER NOT NULL CHECK(effective_max_tokens >= 1 AND effective_max_tokens <= 32768),
            status TEXT NOT NULL CHECK(status IN
                ('prepared','dispatching','dispatch_uncertain','failed','completed')),
            dispatch_expires_at TEXT,
            generation_id TEXT NOT NULL UNIQUE,
            user_message_id INTEGER,
            assistant_message_id INTEGER,
            response_json TEXT,
            error_category TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(client_id,idempotency_key),
            FOREIGN KEY(client_id) REFERENCES kelivo_clients(client_id),
            FOREIGN KEY(user_message_id) REFERENCES messages(id),
            FOREIGN KEY(assistant_message_id) REFERENCES messages(id))""",
    "companion_context_snapshots": """CREATE TABLE companion_context_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_session TEXT NOT NULL,
            snapshot_type TEXT NOT NULL,
            normalized_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            version INTEGER NOT NULL CHECK(version > 0),
            created_at TEXT NOT NULL,
            UNIQUE(api_session, snapshot_type, content_hash),
            UNIQUE(api_session, snapshot_type, version))""",
    "kelivo_rate_limits": """CREATE TABLE kelivo_rate_limits (
            client_id TEXT NOT NULL,
            window_started_at INTEGER NOT NULL CHECK(window_started_at >= 0),
            request_count INTEGER NOT NULL CHECK(request_count > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(client_id,window_started_at),
            FOREIGN KEY(client_id) REFERENCES kelivo_clients(client_id))""",
}

KELIVO_INDEX_DDL: dict[str, str] = {
    "idx_kelivo_requests_status":
        "CREATE INDEX idx_kelivo_requests_status ON kelivo_requests(status,dispatch_expires_at,id)",
    "idx_kelivo_rate_limits_window":
        "CREATE INDEX idx_kelivo_rate_limits_window ON kelivo_rate_limits(window_started_at,client_id)",
    "idx_context_snapshots_lookup":
        "CREATE INDEX idx_context_snapshots_lookup ON companion_context_snapshots(api_session,snapshot_type,active,version)",
    "idx_context_snapshots_one_active":
        "CREATE UNIQUE INDEX idx_context_snapshots_one_active ON companion_context_snapshots(api_session,snapshot_type) WHERE active=1",
}


def _migration_003(conn: sqlite3.Connection) -> None:
    """Kelivo client mapping, idempotent requests, and normalized context."""
    for statement in (*KELIVO_TABLE_DDL.values(), *KELIVO_INDEX_DDL.values()):
        conn.execute(statement)


def _index_columns(conn: sqlite3.Connection, index_name: str) -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA index_xinfo({index_name})").fetchall()
    return tuple(row["name"] for row in rows if row["key"] == 1 and row["cid"] >= 0)


def _sql_fingerprint(sql: str) -> tuple[str, ...]:
    """Tokenize the limited migration DDL, ignoring whitespace/comments but preserving boolean structure."""
    tokens: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            end = sql.find("\n", index + 2)
            index = len(sql) if end < 0 else end + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise sqlite3.DatabaseError("invalid kelivo schema SQL")
            index = end + 2
            continue
        if char == "'":
            end = index + 1
            while end < len(sql):
                if sql[end] == "'":
                    if end + 1 < len(sql) and sql[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            if end > len(sql) or sql[end - 1] != "'":
                raise sqlite3.DatabaseError("invalid kelivo schema SQL")
            tokens.append(sql[index:end])
            index = end
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            tokens.append(sql[index:end].lower())
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < len(sql) and (sql[end].isdigit() or sql[end] == "."):
                end += 1
            tokens.append(sql[index:end])
            index = end
            continue
        operator = sql[index:index + 2]
        if operator in {">=", "<=", "!=", "<>", "=="}:
            tokens.append(operator)
            index += 2
            continue
        if char in "(),=><+-*/":
            tokens.append(char)
            index += 1
            continue
        # Current migration deliberately uses no quoted identifiers or expressions.
        raise sqlite3.DatabaseError("invalid kelivo schema SQL")
    return tuple(tokens)


def _validate_index_xinfo(conn: sqlite3.Connection, name: str, columns: tuple[str, ...]) -> None:
    rows = conn.execute(f"PRAGMA index_xinfo({name})").fetchall()
    key_rows = [row for row in rows if row["key"] == 1]
    auxiliary = [row for row in rows if row["key"] == 0]
    if tuple(row["name"] for row in key_rows) != columns:
        raise sqlite3.DatabaseError(f"invalid kelivo index columns: {name}")
    if any(row["cid"] < 0 or row["desc"] != 0 or str(row["coll"]).upper() != "BINARY" for row in key_rows):
        raise sqlite3.DatabaseError(f"invalid kelivo index expression: {name}")
    if len(auxiliary) != 1 or auxiliary[0]["cid"] != -1:
        raise sqlite3.DatabaseError(f"invalid kelivo index auxiliary shape: {name}")


def validate_kelivo_schema(conn: sqlite3.Connection) -> None:
    """Reject an applied v3 marker unless its complete structural fingerprint matches."""
    expected_columns = {
        "kelivo_clients": {
            "client_id": ("TEXT", 0, None, 1), "api_session": ("TEXT", 1, None, 0),
            "enabled": ("INTEGER", 1, "1", 0), "mapping_revision": ("INTEGER", 1, "1", 0),
            "created_at": ("TEXT", 1, None, 0), "updated_at": ("TEXT", 1, None, 0),
        },
        "kelivo_requests": {
            "id": ("INTEGER", 0, None, 1), "idempotency_key": ("TEXT", 1, None, 0),
            "request_payload_hash": ("TEXT", 1, None, 0), "request_identity_hash": ("TEXT", 1, None, 0),
            "client_id": ("TEXT", 1, None, 0), "api_session": ("TEXT", 1, None, 0),
            "mapping_revision": ("INTEGER", 1, None, 0), "history_before_id": ("INTEGER", 1, None, 0),
            "context_bundle_json": ("TEXT", 1, None, 0), "context_bundle_hash": ("TEXT", 1, None, 0),
            "provider_messages_json": ("TEXT", 1, None, 0),
            "prompt_contract_version": ("TEXT", 1, None, 0), "persona_hash": ("TEXT", 1, None, 0),
            "persona_source": ("TEXT", 1, None, 0), "provider_model": ("TEXT", 1, None, 0),
            "effective_temperature": ("REAL", 1, None, 0),
            "effective_max_tokens": ("INTEGER", 1, None, 0), "status": ("TEXT", 1, None, 0),
            "dispatch_expires_at": ("TEXT", 0, None, 0), "generation_id": ("TEXT", 1, None, 0),
            "user_message_id": ("INTEGER", 0, None, 0), "assistant_message_id": ("INTEGER", 0, None, 0),
            "response_json": ("TEXT", 0, None, 0), "error_category": ("TEXT", 0, None, 0),
            "created_at": ("TEXT", 1, None, 0), "updated_at": ("TEXT", 1, None, 0),
        },
        "companion_context_snapshots": {
            "id": ("INTEGER", 0, None, 1), "api_session": ("TEXT", 1, None, 0),
            "snapshot_type": ("TEXT", 1, None, 0), "normalized_json": ("TEXT", 1, None, 0),
            "content_hash": ("TEXT", 1, None, 0), "active": ("INTEGER", 1, "1", 0),
            "version": ("INTEGER", 1, None, 0), "created_at": ("TEXT", 1, None, 0),
        },
        "kelivo_rate_limits": {
            "client_id": ("TEXT", 1, None, 1), "window_started_at": ("INTEGER", 1, None, 2),
            "request_count": ("INTEGER", 1, None, 0), "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
    }
    for table, expected in expected_columns.items():
        xinfo_rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
        if any(int(row["hidden"]) != 0 for row in xinfo_rows):
            raise sqlite3.DatabaseError(f"invalid hidden kelivo column: {table}")
        actual = {
            row["name"]: (str(row["type"]).upper(), int(row["notnull"]), row["dflt_value"], int(row["pk"]))
            for row in xinfo_rows
        }
        if actual != expected:
            raise sqlite3.DatabaseError(f"invalid kelivo schema: {table} columns")

    expected_indexes = {
        "kelivo_clients": {
            "sqlite_autoindex_kelivo_clients_1": (True, "pk", False, ("client_id",)),
        },
        "kelivo_requests": {
            "sqlite_autoindex_kelivo_requests_1": (True, "u", False, ("generation_id",)),
            "sqlite_autoindex_kelivo_requests_2": (True, "u", False, ("client_id", "idempotency_key")),
            "idx_kelivo_requests_status": (False, "c", False, ("status", "dispatch_expires_at", "id")),
        },
        "companion_context_snapshots": {
            "sqlite_autoindex_companion_context_snapshots_1":
                (True, "u", False, ("api_session", "snapshot_type", "content_hash")),
            "sqlite_autoindex_companion_context_snapshots_2":
                (True, "u", False, ("api_session", "snapshot_type", "version")),
            "idx_context_snapshots_lookup":
                (False, "c", False, ("api_session", "snapshot_type", "active", "version")),
            "idx_context_snapshots_one_active":
                (True, "c", True, ("api_session", "snapshot_type")),
        },
        "kelivo_rate_limits": {
            "sqlite_autoindex_kelivo_rate_limits_1":
                (True, "pk", False, ("client_id", "window_started_at")),
            "idx_kelivo_rate_limits_window":
                (False, "c", False, ("window_started_at", "client_id")),
        },
    }
    for table, expected in expected_indexes.items():
        actual_rows = {row["name"]: row for row in conn.execute(f"PRAGMA index_list({table})")}
        if set(actual_rows) != set(expected):
            raise sqlite3.DatabaseError(f"invalid kelivo index set: {table}")
        for name, (unique, origin, partial, columns) in expected.items():
            row = actual_rows[name]
            if (bool(row["unique"]), row["origin"], bool(row["partial"])) != (unique, origin, partial):
                raise sqlite3.DatabaseError(f"invalid kelivo index attributes: {name}")
            _validate_index_xinfo(conn, name, columns)
    expected_fks = {
        "kelivo_requests": {
            ("client_id", "kelivo_clients", "client_id", "NO ACTION", "NO ACTION", "NONE"),
            ("user_message_id", "messages", "id", "NO ACTION", "NO ACTION", "NONE"),
            ("assistant_message_id", "messages", "id", "NO ACTION", "NO ACTION", "NONE"),
        },
        "kelivo_rate_limits": {
            ("client_id", "kelivo_clients", "client_id", "NO ACTION", "NO ACTION", "NONE"),
        },
        "kelivo_clients": set(), "companion_context_snapshots": set(),
    }
    for table, expected in expected_fks.items():
        actual = {
            (row["from"], row["table"], row["to"], row["on_update"], row["on_delete"], row["match"])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }
        if actual != expected:
            raise sqlite3.DatabaseError(f"invalid kelivo foreign key: {table}")
    for table, expected_sql in KELIVO_TABLE_DDL.items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row is None or _sql_fingerprint(str(row["sql"])) != _sql_fingerprint(expected_sql):
            raise sqlite3.DatabaseError(f"invalid kelivo table fingerprint: {table}")
    for name, expected_sql in KELIVO_INDEX_DDL.items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        if row is None or _sql_fingerprint(str(row["sql"])) != _sql_fingerprint(expected_sql):
            raise sqlite3.DatabaseError(f"invalid kelivo index fingerprint: {name}")


MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "telegram_private_text_mvp", _migration_001),
    (2, "telegram_reliability", _migration_002),
    (3, "kelivo_nonstream_foundation", _migration_003),
)


def run_migrations(path: str, migrations: Iterable[tuple[int, str, Callable[[sqlite3.Connection], None]]] = MIGRATIONS) -> None:
    """Apply versions under a SQLite write lock; concurrent starters re-read state."""
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            for version, name, apply in migrations:
                row = conn.execute("SELECT name,status FROM schema_migrations WHERE version=?", (version,)).fetchone()
                if row:
                    if row["status"] == "applied" and row["name"] == name:
                        if version == 3 and name == "kelivo_nonstream_foundation":
                            validate_kelivo_schema(conn)
                        continue
                    raise sqlite3.DatabaseError("invalid migration state")
                apply(conn)
                stamp = now_iso()
                conn.execute(
                    "INSERT INTO schema_migrations(version,name,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (version, name, "applied", stamp, stamp),
                )
                if version == 3 and name == "kelivo_nonstream_foundation":
                    validate_kelivo_schema(conn)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def audit_id(secret: str, channel: str, account_id: str, external_id: str) -> str:
    value = f"{channel}\x1f{account_id}\x1f{external_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), value, hashlib.sha256).hexdigest()[:24]


def audit(conn: sqlite3.Connection, event_type: str, channel: str, account_id: str = "",
          external_id: str = "", request_job_id: str = "", status: str = "recorded",
          error_category: str | None = None) -> None:
    secret = os.environ.get("CHANNEL_AUDIT_HMAC_SECRET", "")
    external_hash = audit_id(secret, channel, account_id, external_id) if secret and external_id else None
    stamp = now_iso()
    conn.execute(
        """INSERT INTO channel_audit_events
           (event_type,channel,external_id_hash,request_job_id,status,error_category,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (event_type, channel, external_hash, request_job_id, status, error_category, stamp, stamp),
    )


def get_or_create_conversation(conn: sqlite3.Connection, channel: str, account_id: str,
                               conversation_id: str, conversation_type: str = "private",
                               external_user_id: str = "") -> sqlite3.Row:
    stamp = now_iso()
    conn.execute("""INSERT OR IGNORE INTO channel_accounts
        (channel,external_account_id,status,created_at,updated_at) VALUES(?,?,?,?,?)""",
        (channel, account_id, "active", stamp, stamp))
    row = conn.execute("""SELECT * FROM channel_conversations WHERE channel=?
        AND external_account_id=? AND external_conversation_id=?""",
        (channel, account_id, conversation_id)).fetchone()
    if row:
        if external_user_id and row["external_user_id"] != external_user_id:
            conn.execute("UPDATE channel_conversations SET external_user_id=?,updated_at=? WHERE id=?",
                         (external_user_id, stamp, row["id"]))
            row = conn.execute("SELECT * FROM channel_conversations WHERE id=?", (row["id"],)).fetchone()
        return row
    api_session = "api-tg-" + secrets.token_urlsafe(24)
    conn.execute("""INSERT INTO channel_conversations
        (channel,external_account_id,external_conversation_id,conversation_type,api_session,status,created_at,updated_at,external_user_id)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (channel, account_id, conversation_id, conversation_type, api_session, "active", stamp, stamp, external_user_id))
    return conn.execute("SELECT * FROM channel_conversations WHERE api_session=?", (api_session,)).fetchone()


def rate_limit_allowed(conn: sqlite3.Connection, channel: str, account_id: str, user_id: str,
                       limit: int, window_seconds: int) -> bool:
    now = datetime.now(timezone.utc); stamp = now.isoformat()
    row = conn.execute("""SELECT * FROM channel_rate_limits WHERE channel=?
        AND external_account_id=? AND external_user_id=?""", (channel, account_id, user_id)).fetchone()
    if not row:
        conn.execute("""INSERT INTO channel_rate_limits
            (channel,external_account_id,external_user_id,window_started_at,event_count,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)""", (channel, account_id, user_id, stamp, 1, "active", stamp, stamp))
        return True
    try:
        started = datetime.fromisoformat(row["window_started_at"])
    except ValueError:
        started = now - timedelta(seconds=window_seconds + 1)
    if now - started >= timedelta(seconds=window_seconds):
        conn.execute("UPDATE channel_rate_limits SET window_started_at=?,event_count=1,updated_at=? WHERE id=?",
                     (stamp, stamp, row["id"])); return True
    if int(row["event_count"]) >= limit:
        return False
    conn.execute("UPDATE channel_rate_limits SET event_count=event_count+1,updated_at=? WHERE id=?", (stamp, row["id"]))
    return True


def enqueue_telegram_update(path: str, *, account_id: str, update_id: str, chat_id: str,
                            user_id: str, external_message_id: str, text: str,
                            rate_limit: int, rate_window_seconds: int) -> dict:
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute("""SELECT id,status,error_category FROM inbound_events
                WHERE channel='telegram' AND external_account_id=? AND update_id=?""",
                (account_id, update_id)).fetchone()
            if existing:
                conn.execute("COMMIT")
                return {"duplicate": True, "ignored": existing["status"] == "rejected",
                        "reason": existing["error_category"] or "duplicate", "event_id": existing["id"]}
            existing_message = conn.execute("""SELECT id FROM external_messages WHERE channel='telegram'
                AND external_account_id=? AND external_conversation_id=? AND external_message_id=?""",
                (account_id, chat_id, external_message_id)).fetchone()
            if existing_message:
                conn.execute("COMMIT"); return {"duplicate": True}
            stamp = now_iso()
            cur = conn.execute("""INSERT INTO inbound_events
                (channel,external_account_id,update_id,event_type,status,created_at,updated_at)
                VALUES('telegram',?,?,'message','accepted',?,?)""", (account_id, update_id, stamp, stamp))
            event_id = cur.lastrowid
            if not rate_limit_allowed(conn, "telegram", account_id, user_id, rate_limit, rate_window_seconds):
                conn.execute("UPDATE inbound_events SET status='rejected',error_category='rate_limited',updated_at=? WHERE id=?",
                             (stamp, event_id))
                audit(conn, "webhook_rejected", "telegram", account_id, user_id, str(event_id), "rejected", "rate_limited")
                conn.execute("COMMIT")
                return {"duplicate": False, "rejected": "rate_limited", "event_id": event_id}
            conversation = get_or_create_conversation(conn, "telegram", account_id, chat_id, "private", user_id)
            meta = {"user": "human", "attachments": [], "api_session": conversation["api_session"],
                    "channel": "telegram", "external_event_id": str(event_id)}
            cur = conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                               (stamp, "in", "user", text, json.dumps(meta, ensure_ascii=False)))
            canonical_id = cur.lastrowid
            cur = conn.execute("""INSERT INTO external_messages
                (channel,external_account_id,external_conversation_id,external_message_id,direction,canonical_message_id,status,created_at,updated_at)
                VALUES('telegram',?,?,?,?,?,'received',?,?)""",
                (account_id, chat_id, external_message_id, "in", canonical_id, stamp, stamp))
            inbound_id = cur.lastrowid
            cur = conn.execute("""INSERT INTO generation_jobs
                (inbound_message_id,canonical_message_id,channel,external_account_id,external_conversation_id,api_session,status,created_at,updated_at)
                VALUES(?,?,'telegram',?,?,?,'queued',?,?)""",
                (inbound_id, canonical_id, account_id, chat_id, conversation["api_session"], stamp, stamp))
            job_id = cur.lastrowid
            audit(conn, "webhook_accepted", "telegram", account_id, user_id, str(job_id), "queued")
            conn.execute("COMMIT")
            return {"duplicate": False, "event_id": event_id, "canonical_message_id": canonical_id,
                    "job_id": job_id, "api_session": conversation["api_session"],
                    "message": {"id": canonical_id, "ts": stamp, "direction": "in", "kind": "user",
                                "text": text, "meta": meta}}
        except Exception:
            if conn.in_transaction: conn.execute("ROLLBACK")
            raise


def claim_generation_job(path: str, lease_seconds: int = 180, max_attempts: int = 2) -> dict | None:
    now = datetime.now(timezone.utc); stamp = now.isoformat(); lease = (now + timedelta(seconds=lease_seconds)).isoformat()
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Exhausted safe-to-retry work becomes terminal before selection.
        conn.execute("""UPDATE generation_jobs SET status='failed',error_category='max_attempts',lease_until=NULL,updated_at=?
            WHERE status IN ('queued','processing') AND attempt_count>=?""", (stamp, max_attempts))
        row = conn.execute("""SELECT * FROM generation_jobs WHERE attempt_count<? AND
            (status='queued' OR (status='processing' AND dispatch_started_at IS NULL AND lease_until < ?))
            ORDER BY id LIMIT 1""", (max_attempts, stamp)).fetchone()
        if not row:
            conn.execute("COMMIT"); return None
        generation_id = row["generation_id"] or ("tg-gen-" + secrets.token_urlsafe(16))
        stream_id = row["stream_id"] or ("tg-stream-" + secrets.token_urlsafe(16))
        updated = conn.execute("""UPDATE generation_jobs SET status='processing',lease_until=?,attempt_count=attempt_count+1,
            generation_id=?,stream_id=?,reply_to=?,error_category=NULL,updated_at=? WHERE id=? AND attempt_count<? AND
            (status='queued' OR (status='processing' AND dispatch_started_at IS NULL AND lease_until < ?))""",
            (lease, generation_id, stream_id, str(row["canonical_message_id"]), stamp, row["id"], max_attempts, stamp))
        conn.execute("COMMIT")
        if updated.rowcount != 1: return None
    with connect(path) as conn:
        claimed = conn.execute("SELECT * FROM generation_jobs WHERE id=?", (row["id"],)).fetchone()
        return dict(claimed) if claimed else None


def start_generation_dispatch(path: str, job_id: int) -> dict | None:
    stamp = now_iso()
    with connect(path) as conn:
        result = conn.execute("""UPDATE generation_jobs SET status='dispatching',dispatch_started_at=?,lease_until=NULL,updated_at=?
            WHERE id=? AND status='processing' AND generation_id IS NOT NULL AND stream_id IS NOT NULL""",
            (stamp, stamp, job_id))
        if result.rowcount != 1: return None
        row = conn.execute("SELECT * FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row)


def finish_generation_dispatch(path: str, job_id: int, outcome: str, error_category: str | None = None) -> None:
    if outcome not in {"awaiting_reply", "failed", "dispatch_uncertain"}:
        raise ValueError("invalid dispatch outcome")
    stamp = now_iso()
    with connect(path) as conn:
        conn.execute("""UPDATE generation_jobs SET status=?,lease_until=NULL,error_category=?,
            awaiting_reply_since=CASE WHEN ?='awaiting_reply' THEN ? ELSE awaiting_reply_since END,updated_at=?
            WHERE id=? AND status='dispatching'""", (outcome, error_category, outcome, stamp, stamp, job_id))


def recover_inflight_generations(path: str) -> int:
    with connect(path) as conn:
        result = conn.execute("""UPDATE generation_jobs SET status='dispatch_uncertain',error_category='worker_restarted',
            lease_until=NULL,updated_at=? WHERE status='dispatching'""", (now_iso(),))
        return result.rowcount


def _completion_identity(job: sqlite3.Row | dict) -> str:
    correlation = job["generation_id"] or job["stream_id"]
    return f"telegram\x1f{job['external_account_id']}\x1f{correlation}\x1f{job['reply_to']}"


def _split_text(text: str, limit: int = 4096) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut + 1 if cut >= 0 else limit
        parts.append(text[:cut]); text = text[cut:]
    if text: parts.append(text)
    return parts


def complete_telegram_generation(path: str, *, meta: dict, text: str, kind: str = "reply") -> dict | None:
    """Atomically deduplicate, validate correlation, save reply, complete job, and create outbox."""
    generation_id = str(meta.get("generation_id") or "").strip()
    stream_id = str(meta.get("stream_id") or "").strip()
    session = str(meta.get("api_session") or meta.get("session_id") or "").strip()
    reply_to = str(meta.get("reply_to") or "").strip()
    account = str(meta.get("channel_account") or meta.get("account_id") or "").strip()
    conversation = str(meta.get("channel_conversation") or meta.get("conversation_id") or "").strip()
    channel = str(meta.get("channel") or "").strip()
    if (kind != "reply" or not text or channel != "telegram" or not account or not conversation or
            not session or not reply_to or not (generation_id or stream_id)):
        return None
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            where = ["channel='telegram'", "api_session=?", "reply_to=?",
                     "status IN ('processing','dispatching','dispatch_uncertain','awaiting_reply','completed')"]
            params: list[object] = [session, reply_to]
            if generation_id: where.append("generation_id=?"); params.append(generation_id)
            if stream_id: where.append("stream_id=?"); params.append(stream_id)
            if account: where.append("external_account_id=?"); params.append(account)
            if conversation: where.append("external_conversation_id=?"); params.append(conversation)
            rows = conn.execute("SELECT * FROM generation_jobs WHERE " + " AND ".join(where), params).fetchall()
            if len(rows) != 1:
                audit(conn, "reply_uncorrelated", "telegram", account, session, generation_id or stream_id,
                      "ignored", "correlation_missing")
                conn.execute("COMMIT"); return None
            job = rows[0]
            inbound = conn.execute("""SELECT * FROM external_messages WHERE id=? AND channel='telegram'
                AND external_account_id=? AND external_conversation_id=? AND direction='in'
                AND canonical_message_id=?""", (job["inbound_message_id"], job["external_account_id"],
                job["external_conversation_id"], job["canonical_message_id"])).fetchone()
            conv = conn.execute("""SELECT * FROM channel_conversations WHERE channel='telegram'
                AND external_account_id=? AND external_conversation_id=? AND api_session=?
                AND conversation_type='private' AND status='active'""", (job["external_account_id"],
                job["external_conversation_id"], session)).fetchone()
            account_row = conn.execute("""SELECT id FROM channel_accounts WHERE channel='telegram'
                AND external_account_id=? AND status='active'""", (job["external_account_id"],)).fetchone()
            if not inbound or not conv or not account_row or reply_to != str(job["canonical_message_id"]):
                audit(conn, "reply_uncorrelated", "telegram", job["external_account_id"], session,
                      generation_id or stream_id, "ignored", "correlation_mismatch")
                conn.execute("COMMIT"); return None
            identity = _completion_identity(job)
            existing = conn.execute("""SELECT c.*,m.ts,m.direction,m.kind,m.text,m.meta FROM telegram_completions c
                JOIN messages m ON m.id=c.canonical_message_id WHERE c.completion_identity=?""", (identity,)).fetchone()
            if existing:
                conn.execute("COMMIT")
                return {"duplicate": True, "message": {"id": existing["canonical_message_id"], "ts": existing["ts"],
                        "direction": existing["direction"], "kind": existing["kind"], "text": existing["text"],
                        "meta": json.loads(existing["meta"] or "{}")}, "delivery_id": existing["delivery_id"]}
            if job["status"] == "failed":
                conn.execute("COMMIT"); return None
            stamp = str(meta.get("ts") or now_iso())
            stored_meta = dict(meta)
            stored_meta.update({"channel": "telegram", "channel_account": job["external_account_id"],
                                "channel_conversation": job["external_conversation_id"]})
            cur = conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                               (stamp, "out", "reply", text, json.dumps(stored_meta, ensure_ascii=False)))
            message_id = cur.lastrowid
            updated = conn.execute("""UPDATE generation_jobs SET status='completed',reply_message_id=?,lease_until=NULL,
                error_category=NULL,updated_at=? WHERE id=? AND status IN
                ('processing','dispatching','dispatch_uncertain','awaiting_reply')""", (message_id, stamp, job["id"]))
            if updated.rowcount != 1:
                raise sqlite3.IntegrityError("generation completion state changed")
            key = "telegram:completion:" + hashlib.sha256(identity.encode()).hexdigest()
            cur = conn.execute("""INSERT INTO delivery_attempts
                (generation_job_id,idempotency_key,channel,external_account_id,external_conversation_id,payload_text,status,created_at,updated_at)
                VALUES(?,?,'telegram',?,?,?,'pending',?,?)""", (job["id"], key, job["external_account_id"],
                job["external_conversation_id"], text, stamp, stamp))
            delivery_id = cur.lastrowid
            parts = _split_text(text)
            if not parts: raise sqlite3.IntegrityError("empty reply")
            for index, part in enumerate(parts):
                conn.execute("""INSERT INTO delivery_parts
                    (delivery_id,part_index,total_parts,text_hash,text_length,payload_text,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,'pending',?,?)""", (delivery_id, index, len(parts),
                    hashlib.sha256(part.encode()).hexdigest(), len(part), part, stamp, stamp))
            conn.execute("""INSERT INTO telegram_completions
                (completion_identity,generation_job_id,canonical_message_id,delivery_id,created_at)
                VALUES(?,?,?,?,?)""", (identity, job["id"], message_id, delivery_id, stamp))
            audit(conn, "generation_completed", "telegram", job["external_account_id"],
                  job["external_conversation_id"], str(job["id"]), "completed")
            conn.execute("COMMIT")
            return {"duplicate": False, "message": {"id": message_id, "ts": stamp, "direction": "out",
                    "kind": "reply", "text": text, "meta": stored_meta}, "delivery_id": delivery_id}
        except Exception:
            if conn.in_transaction: conn.execute("ROLLBACK")
            raise


def mark_uncorrelated(path: str, meta: dict) -> None:
    with connect(path) as conn:
        audit(conn, "reply_uncorrelated", "telegram", str(meta.get("channel_account") or ""),
              str(meta.get("api_session") or ""), str(meta.get("generation_id") or meta.get("stream_id") or ""),
              "ignored", "correlation_missing")


def claim_delivery_part(path: str) -> dict | None:
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""SELECT p.*,d.external_account_id,d.external_conversation_id,d.generation_job_id,
            d.status AS delivery_status FROM delivery_parts p JOIN delivery_attempts d ON d.id=p.delivery_id
            WHERE d.status='pending' AND p.status='pending' AND NOT EXISTS
              (SELECT 1 FROM delivery_parts prior WHERE prior.delivery_id=p.delivery_id
               AND prior.part_index<p.part_index AND prior.status!='delivered')
            ORDER BY d.id,p.part_index LIMIT 1""").fetchone()
        if not row:
            conn.execute("COMMIT"); return None
        stamp = now_iso()
        conn.execute("UPDATE delivery_attempts SET status='sending',attempt_count=attempt_count+1,updated_at=? WHERE id=?",
                     (stamp, row["delivery_id"]))
        updated = conn.execute("UPDATE delivery_parts SET status='sending',updated_at=? WHERE id=? AND status='pending'",
                               (stamp, row["id"]))
        conn.execute("COMMIT")
        return dict(row) if updated.rowcount == 1 else None


def finish_delivery_part(path: str, part_id: int, status: str, telegram_message_id: str | None = None,
                         error_category: str | None = None, retry_after_seconds: int | None = None) -> None:
    stamp = now_iso()
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        part = conn.execute("SELECT * FROM delivery_parts WHERE id=?", (part_id,)).fetchone()
        if not part or part["status"] != "sending": conn.execute("COMMIT"); return
        conn.execute("""UPDATE delivery_parts SET status=?,telegram_message_id=?,error_category=?,retry_after_seconds=?,updated_at=?
            WHERE id=?""", (status, telegram_message_id, error_category, retry_after_seconds, stamp, part_id))
        delivery = conn.execute("SELECT * FROM delivery_attempts WHERE id=?", (part["delivery_id"],)).fetchone()
        job = conn.execute("SELECT * FROM generation_jobs WHERE id=?", (delivery["generation_job_id"],)).fetchone()
        if telegram_message_id:
            conn.execute("""INSERT OR IGNORE INTO external_messages
                (channel,external_account_id,external_conversation_id,external_message_id,direction,canonical_message_id,generation_id,status,created_at,updated_at)
                VALUES('telegram',?,?,?,'out',?,?,?, ?,?)""", (delivery["external_account_id"],
                delivery["external_conversation_id"], telegram_message_id, job["reply_message_id"],
                job["generation_id"], "delivered", stamp, stamp))
        remaining = conn.execute("SELECT count(*) FROM delivery_parts WHERE delivery_id=? AND status!='delivered'",
                                 (delivery["id"],)).fetchone()[0]
        if status == "delivered" and remaining == 0:
            ids = [r[0] for r in conn.execute("SELECT telegram_message_id FROM delivery_parts WHERE delivery_id=? ORDER BY part_index",
                                              (delivery["id"],))]
            conn.execute("""UPDATE delivery_attempts SET status='delivered',external_message_id=?,error_category=NULL,
                retry_after_seconds=NULL,updated_at=? WHERE id=?""", (",".join(ids), stamp, delivery["id"]))
        elif status != "delivered":
            conn.execute("""UPDATE delivery_attempts SET status=?,error_category=?,retry_after_seconds=?,updated_at=? WHERE id=?""",
                         (status, error_category, retry_after_seconds, stamp, delivery["id"]))
        else:
            conn.execute("UPDATE delivery_attempts SET status='pending',updated_at=? WHERE id=?", (stamp, delivery["id"]))
        conn.execute("COMMIT")


# Compatibility helpers retained for callers/tests while delivery is now part-based.
def claim_delivery(path: str) -> dict | None:
    return claim_delivery_part(path)


def recover_inflight_deliveries(path: str) -> int:
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        parts = conn.execute("""UPDATE delivery_parts SET status='delivery_uncertain',error_category='worker_restarted',updated_at=?
            WHERE status='sending'""", (now_iso(),)).rowcount
        conn.execute("""UPDATE delivery_attempts SET status='delivery_uncertain',error_category='worker_restarted',updated_at=?
            WHERE status='sending'""", (now_iso(),))
        conn.execute("COMMIT"); return parts
