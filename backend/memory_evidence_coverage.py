"""Private read-only provenance coverage diagnostics for the session evidence UI.

No HTTP route, CLI on real data, API key, mutation, memory text, memory keys,
message IDs or per-record results. Intended only for isolated fixtures and
future explicitly authorized local operator auditing.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Iterable

from .memory_evidence_readonly import (
    MAX_ITEM_CHARS,
    MAX_SOURCES,
    MemoryEvidenceReadonlyError,
    SESSION_PATTERN,
)

MAX_DIAGNOSTIC_ITEMS = 2000
STATUS_NAMES = (
    "verified_session_evidence",
    "missing_session_binding",
    "different_session_binding",
    "missing_qualifying_source",
    "content_over_display_limit",
    "too_many_sources",
)


def summarize_session_coverage(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    allowed_sessions: Iterable[str],
) -> dict[str, object]:
    """Aggregate current active, normal-sensitivity, session-scoped coverage.

    The caller MUST already be authorized and must supply a read-only SQLite
    connection. This deliberately excludes other scopes, disabled memories,
    and sensitive records from even aggregate counts.
    """
    if (
        not isinstance(session_id, str)
        or SESSION_PATTERN.fullmatch(session_id) is None
        or session_id not in allowed_sessions
    ):
        raise MemoryEvidenceReadonlyError()

    try:
        # Never SELECT normalized_content, memory_key, msg.text or msg.meta.
        # JSON extraction happens in SQL and only fixed category flags return.
        rows = connection.execute(
            """WITH candidates AS (
                 SELECT id, length(normalized_content) AS content_chars
                 FROM memory_items
                 WHERE scope_type='session' AND scope_ref=?
                   AND status='active' AND sensitivity='normal'
                 ORDER BY last_confirmed_at DESC, id DESC
                 LIMIT ?
               ),
               eligible_sources AS (
                 SELECT s.memory_id,
                   CASE WHEN json_valid(msg.meta)
                              AND json_type(msg.meta,'$.api_session')='text'
                        THEN json_extract(msg.meta,'$.api_session')
                        ELSE NULL END AS bound_session
                 FROM memory_sources AS s
                 JOIN memory_evidence_events AS e
                   ON e.id=s.evidence_event_id
                  AND e.canonical_message_id=s.canonical_message_id
                  AND e.evidence_type=s.evidence_type
                 JOIN messages AS msg ON msg.id=s.canonical_message_id
                 WHERE s.memory_id IN (SELECT id FROM candidates)
                   AND s.evidence_role='user'
                   AND e.reality_scope='real'
                   AND e.subject_scope IN ('user','project')
                   AND e.evidence_type IN (
                     'explicit_user_memory','confirmed_user_fact',
                     'confirmed_project_decision','explicit_user_correction')
               )
               SELECT c.content_chars,
                      COUNT(e.memory_id) AS source_count,
                      MAX(CASE WHEN e.bound_session=?
                          THEN 1 ELSE 0 END) AS has_match,
                      MAX(CASE WHEN e.memory_id IS NOT NULL AND
                         (e.bound_session IS NULL OR e.bound_session='')
                          THEN 1 ELSE 0 END) AS has_missing,
                      MAX(CASE WHEN e.bound_session IS NOT NULL
                         AND e.bound_session<>'' AND e.bound_session<>?
                          THEN 1 ELSE 0 END) AS has_other
               FROM candidates c
               LEFT JOIN eligible_sources e ON e.memory_id=c.id
               GROUP BY c.id
               ORDER BY c.id""",
            (session_id, MAX_DIAGNOSTIC_ITEMS + 1, session_id, session_id),
        ).fetchall()
        if len(rows) > MAX_DIAGNOSTIC_ITEMS:
            raise MemoryEvidenceReadonlyError()

        counts: Counter[str] = Counter()
        for content_chars, source_count, has_match, has_missing, has_other in rows:
            if content_chars is None or content_chars < 1 or content_chars > MAX_ITEM_CHARS:
                category = "content_over_display_limit"
            elif source_count > MAX_SOURCES:
                category = "too_many_sources"
            elif has_match:
                category = "verified_session_evidence"
            elif has_missing:
                category = "missing_session_binding"
            elif has_other:
                category = "different_session_binding"
            else:
                category = "missing_qualifying_source"
            counts[category] += 1

        return {
            "schema": "guitting-memory-source-coverage-local-v1",
            "scope": "active_normal_session_only",
            "total_eligible_scope_items": len(rows),
            "counts": {name: counts[name] for name in STATUS_NAMES},
            "raw_content_or_identifiers_returned": False,
            "counts_may_be_sensitive_metadata": True,
            "writes": 0,
        }
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        raise MemoryEvidenceReadonlyError() from None
