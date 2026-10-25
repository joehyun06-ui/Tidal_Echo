"""Read-only, session-bound evidence snapshot for the experimental PWA panel.

This is NOT semantic search and does not infer an answer from a user's question.
Only active, normal-sensitivity session memories with corroborating real, user-role
canonical evidence from the *same* session are displayed. Never write to DB.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from typing import Iterable

SESSION_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z", re.ASCII)
MAX_ITEMS = 10
MAX_TOTAL_CHARS = 8000
MAX_ITEM_CHARS = 4000
MAX_SOURCES = 20


class MemoryEvidenceReadonlyError(RuntimeError):
    def __init__(self):
        super().__init__("memory_evidence_unavailable")


def _date(value: object) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise MemoryEvidenceReadonlyError()
    candidate = value[:10]
    try:
        parsed = dt.date.fromisoformat(candidate)
    except ValueError:
        raise MemoryEvidenceReadonlyError() from None
    if parsed.isoformat() != candidate:
        raise MemoryEvidenceReadonlyError()
    return candidate


def _inspect_all_sources(
    connection: sqlite3.Connection, memory_id: int, session_id: str,
) -> tuple[str, list[str]]:
    """Read every linked source, including invalid ones; never cherry-pick.

    Return a fixed diagnostic category and opaque IDs only for fully verified
    records. Reading a single qualifying source is NOT sufficient when another
    source is foreign, unbound or invalid.
    """
    rows = connection.execute(
        """SELECT s.id, s.evidence_role, e.reality_scope,
                  e.subject_scope, e.evidence_type, msg.direction, msg.kind,
                  CASE WHEN json_valid(msg.meta)
                         AND json_type(msg.meta,'$.api_session')='text'
                       THEN json_extract(msg.meta,'$.api_session')
                       ELSE NULL END AS bound_session
           FROM memory_sources AS s
           LEFT JOIN memory_evidence_events AS e
             ON e.id=s.evidence_event_id
            AND e.canonical_message_id=s.canonical_message_id
            AND e.evidence_type=s.evidence_type
           LEFT JOIN messages AS msg ON msg.id=s.canonical_message_id
           WHERE s.memory_id=?
           ORDER BY s.id LIMIT ?""",
        (memory_id, MAX_SOURCES + 1),
    ).fetchall()
    if not rows:
        return "missing_qualifying_source", []
    if len(rows) > MAX_SOURCES:
        return "too_many_sources", []
    outcomes = set()
    for source in rows:
        (source_id, role, reality, subject, evidence, direction, kind, bound) = source
        if (
            role != "user" or reality != "real"
            or subject not in ("user", "project")
            or evidence not in (
                "explicit_user_memory", "confirmed_user_fact",
                "confirmed_project_decision", "explicit_user_correction",
            )
            or direction != "in" or kind != "user"
        ):
            outcomes.add("unqualified")
        elif bound == session_id:
            outcomes.add("verified")
        elif bound is None or bound == "":
            outcomes.add("unbound")
        else:
            outcomes.add("foreign")
    if outcomes == {"verified"}:
        return "verified_session_evidence", [
            f"memory-source-{int(row[0])}" for row in rows
        ]
    if len(outcomes) > 1:
        return "mixed_or_unverified_sources", []
    if "foreign" in outcomes:
        return "different_session_binding", []
    if "unbound" in outcomes:
        return "missing_session_binding", []
    return "missing_qualifying_source", []


def build_snapshot(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    allowed_sessions: Iterable[str],
) -> dict:
    """Caller must authenticate, check feature gates, and open SQLite read-only.

    The internal loop checks its own bearer token and passes its canonical list
    of sessions. This function does not authorize the caller on its own.
    """
    if (
        not isinstance(session_id, str)
        or SESSION_PATTERN.fullmatch(session_id) is None
        or session_id not in allowed_sessions
    ):
        raise MemoryEvidenceReadonlyError()
    try:
        items = connection.execute(
            """SELECT id, memory_key, normalized_content, last_confirmed_at
               FROM memory_items
               WHERE scope_type='session' AND scope_ref=?
                 AND status='active' AND sensitivity='normal'
               ORDER BY last_confirmed_at DESC, id DESC LIMIT ?""",
            (session_id, MAX_ITEMS),
        ).fetchall()
        views: list[dict] = []
        total_chars = 0
        for memory_id, memory_key, content, last_confirmed_at in items:
            if not isinstance(memory_key, str) or not isinstance(content, str):
                raise MemoryEvidenceReadonlyError()
            if not content or len(content) > MAX_ITEM_CHARS:
                continue
            if total_chars + len(content) > MAX_TOTAL_CHARS:
                break
            # Every attached source must corroborate this same session.
            source_verdict, source_ids = _inspect_all_sources(
                connection, memory_id, session_id,
            )
            if source_verdict != "verified_session_evidence":
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,96}", memory_key):
                raise MemoryEvidenceReadonlyError()
            views.append({
                "case": memory_key,
                "retrieval_status": "found",
                "answer": content,
                "as_of": _date(last_confirmed_at),
                "source_ids": source_ids,
            })
            total_chars += len(content)
        return {
            "schema": "guitting-memory-retrieval-ui-v1",
            "mode": "server_readonly",
            "ready_for_live_frontend": True,
            "api_session": session_id,
            "views": views,
        }
    except (sqlite3.Error, TypeError, ValueError, OverflowError):
        raise MemoryEvidenceReadonlyError() from None
