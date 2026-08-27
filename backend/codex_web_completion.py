"""P2-B exactly-once canonical Web completion primitive for Codex callbacks.

No schema migration is required: BEGIN IMMEDIATE serializes writers and the stable
callback identity is stored inside the existing messages.meta JSON. This helper is
not wired into /channel/out until the P2-B integration slice.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path


MAX_TEXT_CHARS = 64_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class CodexWebCompletionError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category

    def __repr__(self) -> str:
        return f"<CodexWebCompletionError category={self.category!r}>"


def _safe_id(value: object, category: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise CodexWebCompletionError(category)
    return value


def _safe_reply_to(value: object) -> str:
    raw = str(value or "")
    if not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
        raise CodexWebCompletionError("codex_web_reply_to_invalid")
    return raw


def _connect(path: str | Path, timeout_seconds: float) -> sqlite3.Connection:
    database = Path(path)
    if not database.is_absolute() or ".." in database.parts:
        raise CodexWebCompletionError("codex_web_completion_store_invalid")
    conn = sqlite3.connect(str(database), timeout=timeout_seconds, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {max(1, int(timeout_seconds * 1000))}")
    return conn


def _message_dict(row: sqlite3.Row) -> dict:
    try:
        meta = json.loads(row["meta"])
    except Exception:
        raise CodexWebCompletionError("codex_web_completion_corrupt") from None
    if not isinstance(meta, dict):
        raise CodexWebCompletionError("codex_web_completion_corrupt")
    return {
        "id": int(row["id"]),
        "ts": str(row["ts"]),
        "direction": str(row["direction"]),
        "kind": str(row["kind"]),
        "text": str(row["text"]),
        "meta": meta,
    }


def _validate_source_message(conn: sqlite3.Connection, reply_to: str, api_session: str) -> None:
    row = conn.execute(
        "SELECT id,direction,kind,text,meta FROM messages WHERE id=?",
        (int(reply_to),),
    ).fetchone()
    if row is None:
        raise CodexWebCompletionError("codex_web_source_message_missing")
    if row["direction"] != "in" or row["kind"] != "user":
        raise CodexWebCompletionError("codex_web_source_message_mismatch")
    try:
        meta = json.loads(row["meta"])
    except Exception:
        raise CodexWebCompletionError("codex_web_source_message_mismatch") from None
    if not isinstance(meta, dict) or (
        meta.get("channel") != "web"
        or meta.get("source") != "relay"
        or meta.get("api_session") != api_session
    ):
        raise CodexWebCompletionError("codex_web_source_message_mismatch")


def complete_codex_web_generation(
    path: str | Path,
    *,
    callback_identity: str,
    generation_id: str,
    client_message_id: str,
    api_session: str,
    reply_to: str | int,
    text: str,
    ts: str,
    usage: dict[str, int] | None = None,
    timeout_seconds: float = 30.0,
) -> dict:
    callback_identity = _safe_id(callback_identity, "codex_web_callback_identity_invalid")
    generation_id = _safe_id(generation_id, "codex_web_generation_id_invalid")
    client_message_id = _safe_id(client_message_id, "codex_web_client_message_id_invalid")
    api_session = _safe_id(api_session, "codex_web_session_invalid")
    reply_to = _safe_reply_to(reply_to)
    if not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS:
        raise CodexWebCompletionError("codex_web_completion_text_invalid")
    if not isinstance(ts, str) or not ts or len(ts) > 64:
        raise CodexWebCompletionError("codex_web_completion_timestamp_invalid")
    if usage is not None:
        if not isinstance(usage, dict) or any(
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in usage.items()
        ):
            raise CodexWebCompletionError("codex_web_completion_usage_invalid")
        usage = {str(key): int(value) for key, value in usage.items()}
    meta = {
        "channel": "web",
        "source": "codex_generation",
        "provider": "codex",
        "api_session": api_session,
        "reply_to": reply_to,
        "generation_id": generation_id,
        "client_message_id": client_message_id,
        "codex_callback_identity": callback_identity,
    }
    if usage:
        meta["usage"] = usage
    encoded = json.dumps(meta, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    try:
        with closing(_connect(path, timeout_seconds)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _validate_source_message(conn, reply_to, api_session)
            rows = conn.execute(
                """SELECT id,ts,direction,kind,text,meta FROM messages
                   WHERE direction='out' AND kind='reply'
                     AND json_extract(meta,'$.codex_callback_identity')=?
                   ORDER BY id LIMIT 2""",
                (callback_identity,),
            ).fetchall()
            if len(rows) > 1:
                conn.execute("ROLLBACK")
                raise CodexWebCompletionError("codex_web_completion_corrupt")
            if rows:
                existing = _message_dict(rows[0])
                existing_meta = existing["meta"]
                expected = {
                    "channel": "web",
                    "source": "codex_generation",
                    "provider": "codex",
                    "api_session": api_session,
                    "reply_to": reply_to,
                    "generation_id": generation_id,
                    "client_message_id": client_message_id,
                    "codex_callback_identity": callback_identity,
                }
                if (
                    existing["text"] != text
                    or any(existing_meta.get(key) != value for key, value in expected.items())
                ):
                    conn.execute("ROLLBACK")
                    raise CodexWebCompletionError("codex_web_completion_conflict")
                conn.execute("COMMIT")
                return {"message": existing, "duplicate": True}
            cur = conn.execute(
                "INSERT INTO messages (ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                (ts, "out", "reply", text, encoded),
            )
            row = conn.execute(
                "SELECT id,ts,direction,kind,text,meta FROM messages WHERE id=?",
                (cur.lastrowid,),
            ).fetchone()
            conn.execute("COMMIT")
    except CodexWebCompletionError:
        raise
    except sqlite3.Error:
        raise CodexWebCompletionError("codex_web_completion_unavailable") from None
    return {"message": _message_dict(row), "duplicate": False}
