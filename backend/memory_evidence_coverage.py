"""Local, authorization-scoped aggregate coverage of read-only memory provenance.

No HTTP route or real-data CLI. Returns only fixed category counts, which are
potentially sensitive metadata and must not be published as telemetry.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Iterable

from .memory_evidence_readonly import (
    MAX_ITEM_CHARS,
    MemoryEvidenceReadonlyError,
    SESSION_PATTERN,
    _inspect_all_sources,
)

MAX_DIAGNOSTIC_ITEMS = 2000
STATUS_NAMES = (
    "verified_session_evidence",
    "missing_session_binding",
    "different_session_binding",
    "missing_qualifying_source",
    "mixed_or_unverified_sources",
    "content_over_display_limit",
    "too_many_sources",
)


def summarize_session_coverage(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    allowed_sessions: Iterable[str],
) -> dict[str, object]:
    """Never expose source IDs, content or per-memory diagnostics.

    Share exact provenance rules with the UI; qualifying only *one* of a
    memory's attached sources is insufficient for safe presentation.
    Caller authorizes the session and passes a read-only SQLite connection.
    """
    if (
        not isinstance(session_id, str)
        or SESSION_PATTERN.fullmatch(session_id) is None
        or session_id not in allowed_sessions
    ):
        raise MemoryEvidenceReadonlyError()
    try:
        rows = connection.execute(
            """SELECT id, length(normalized_content)
               FROM memory_items
               WHERE scope_type='session' AND scope_ref=?
                 AND status='active' AND sensitivity='normal'
               ORDER BY last_confirmed_at DESC, id DESC LIMIT ?""",
            (session_id, MAX_DIAGNOSTIC_ITEMS + 1),
        ).fetchall()
        if len(rows) > MAX_DIAGNOSTIC_ITEMS:
            raise MemoryEvidenceReadonlyError()
        counts: Counter[str] = Counter()
        for memory_id, content_chars in rows:
            if (
                content_chars is None
                or content_chars < 1
                or content_chars > MAX_ITEM_CHARS
            ):
                category = "content_over_display_limit"
            else:
                category, _source_ids = _inspect_all_sources(
                    connection, memory_id, session_id,
                )
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
    except (sqlite3.Error, TypeError, ValueError, OverflowError):
        raise MemoryEvidenceReadonlyError() from None
