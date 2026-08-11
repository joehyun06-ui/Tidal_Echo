"""Read-only, profile-bound review of automatic Memory candidates.

This module deliberately owns no ``MemoryRuntime`` or write-capable store.  It
opens the relay database with SQLite ``mode=ro``, validates the frozen schema
and fingerprint profile, and re-proves every returned candidate from canonical
Phase 4A evidence.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

try:
    from . import (
        channel_store,
        deployment_config,
        memory_candidate_integrity,
        memory_formation,
        memory_policy,
    )
except ImportError:  # support direct module execution in local tooling
    import channel_store
    import deployment_config
    import memory_candidate_integrity
    import memory_formation
    import memory_policy


DEFAULT_CANDIDATE_LIMIT: Final = 20
MAX_CANDIDATE_LIMIT: Final = 50
MAX_CONTENT_PREVIEW_CHARS: Final = 240
EVIDENCE_CONTEXT_CHARS: Final = (
    memory_candidate_integrity.EVIDENCE_CONTEXT_CHARS
)
MAX_SOURCE_EXCERPT_CHARS: Final = (
    memory_candidate_integrity.MAX_SOURCE_EXCERPT_CHARS
)
CANDIDATE_REVIEW_CONTRACT_VERSION: Final = (
    memory_candidate_integrity.CANDIDATE_REVIEW_CONTRACT_VERSION
)

_AUTOMATIC_KINDS: Final = frozenset(memory_formation.SIGNAL_KIND_MAPPING.values())

_ERROR_CATEGORIES: Final = frozenset({
    "candidate_review_disabled",
    "candidate_review_configuration_invalid",
    "candidate_review_schema_invalid",
    "candidate_review_profile_mismatch",
    "candidate_review_state_invalid",
    "candidate_unreviewable",
    "candidate_not_found",
    "invalid_candidate_key",
    "invalid_candidate_kind",
    "invalid_candidate_limit",
    "invalid_candidate_cursor",
    "storage_unavailable",
})

_CANDIDATE_COLUMNS: Final = (
    memory_candidate_integrity.AUTOMATIC_MEMORY_COLUMNS
)


class MemoryCandidateReviewError(RuntimeError):
    """A stable, data-free candidate-review failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe_category = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "candidate_review_state_invalid"
        )
        self.category = safe_category
        super().__init__(safe_category)

    def __str__(self) -> str:
        try:
            category = object.__getattribute__(self, "category")
        except Exception:
            return "candidate_review_state_invalid"
        return (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "candidate_review_state_invalid"
        )

    def __repr__(self) -> str:
        return f"MemoryCandidateReviewError({str(self)!r})"


@dataclass(frozen=True, slots=True, repr=False)
class CandidateReviewSummaryV1:
    candidate_key: str = field(repr=False)
    kind: str
    content_preview: str = field(repr=False)
    created_at: str
    provenance_count: int

    def __repr__(self) -> str:
        return "<CandidateReviewSummaryV1>"


@dataclass(frozen=True, slots=True, repr=False)
class CandidateReviewEvidenceV1:
    signal_type: str
    observed_at: str
    formation_contract_version: str
    extractor_contract_version: str
    source_excerpt: str = field(repr=False)

    def __repr__(self) -> str:
        return "<CandidateReviewEvidenceV1>"


@dataclass(frozen=True, slots=True, repr=False)
class CandidateReviewDetailV1:
    candidate_key: str = field(repr=False)
    kind: str
    content: str = field(repr=False)
    scope_type: str
    scope_ref: str = field(repr=False)
    sensitivity: str
    explicitness: str
    confidence: float
    created_at: str
    provenance_count: int
    evidence: tuple[CandidateReviewEvidenceV1, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "<CandidateReviewDetailV1>"


def _raise(category: str) -> None:
    raise MemoryCandidateReviewError(category)


def _preview(content: str) -> str:
    if len(content) <= MAX_CONTENT_PREVIEW_CHARS:
        return content
    return content[: MAX_CONTENT_PREVIEW_CHARS - 1] + "…"


class MemoryCandidateReviewReader:
    """Isolated read-only reader with pinned fingerprint expectations."""

    __slots__ = (
        "_database_path",
        "_verifier",
    )

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        fingerprint_key_id: str,
        fingerprint_hmac_secret: str,
        max_item_chars: int,
    ):
        try:
            database = Path(database_path)
            if not database.is_absolute():
                raise ValueError
            if (
                type(fingerprint_key_id) is not str
                or type(fingerprint_hmac_secret) is not str
                or not deployment_config.memory_fingerprint_secret_is_strong(
                    fingerprint_hmac_secret
                )
                or type(max_item_chars) is not int
                or not 64 <= max_item_chars <= 4096
            ):
                raise ValueError
            verifier = (
                memory_candidate_integrity.AutomaticCandidateIntegrityVerifier(
                    fingerprint_key_id=fingerprint_key_id,
                    fingerprint_hmac_secret=fingerprint_hmac_secret,
                    max_item_chars=max_item_chars,
                )
            )
            memory_policy.MemoryPolicy(
                max_item_chars=max_item_chars,
                sensitive_storage_enabled=False,
            )
        except (MemoryCandidateReviewError,):
            raise
        except Exception:
            _raise("candidate_review_configuration_invalid")
        self._database_path = str(database)
        self._verifier = verifier

    def __repr__(self) -> str:
        return "<MemoryCandidateReviewReader>"

    def _connect_read_only(self) -> sqlite3.Connection:
        """Private test seam; returned connections are SQLite read-only."""

        try:
            return channel_store.connect_read_only(
                self._database_path,
                timeout_seconds=30.0,
            )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            _raise("storage_unavailable")

    def _validate_schema(self, conn: sqlite3.Connection) -> None:
        try:
            channel_store.validate_memory_candidate_persistence_schema(conn)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            _raise("candidate_review_schema_invalid")

    def _validate_profile(self, conn: sqlite3.Connection) -> None:
        try:
            self._verifier.verify_profile(conn)
        except memory_candidate_integrity.AutomaticCandidateIntegrityError:
            _raise("candidate_review_profile_mismatch")

    def _prepare_connection(self, conn: sqlite3.Connection) -> None:
        self._validate_schema(conn)
        self._validate_profile(conn)

    @staticmethod
    def _map_verified_evidence(
        evidence: tuple[
            memory_candidate_integrity.VerifiedAutomaticEvidenceV1, ...
        ],
    ) -> tuple[CandidateReviewEvidenceV1, ...]:
        return tuple(
            CandidateReviewEvidenceV1(
                signal_type=item.signal_type,
                observed_at=item.observed_at,
                formation_contract_version=item.formation_contract_version,
                extractor_contract_version=item.extractor_contract_version,
                source_excerpt=item.source_excerpt,
            )
            for item in evidence
        )

    def _verify_candidate(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        missing_category: str,
    ) -> memory_candidate_integrity.VerifiedAutomaticMemoryV1:
        try:
            return self._verifier.verify_pending_candidate(conn, row)
        except memory_candidate_integrity.AutomaticCandidateIntegrityError as error:
            if error.category == "candidate_provenance_missing":
                _raise(missing_category)
            if error.category == "candidate_integrity_profile_mismatch":
                _raise("candidate_review_profile_mismatch")
            if error.category == "candidate_integrity_schema_invalid":
                _raise("candidate_review_schema_invalid")
            if error.category == "storage_unavailable":
                _raise("storage_unavailable")
            _raise("candidate_review_state_invalid")

    def _resolve_cursor(
        self,
        conn: sqlite3.Connection,
        candidate_key: str,
        kind: str | None,
    ) -> tuple[str, int]:
        try:
            if memory_policy.MEMORY_KEY_PATTERN.fullmatch(candidate_key) is None:
                _raise("invalid_candidate_cursor")
            row = conn.execute(
                f"""SELECT {_CANDIDATE_COLUMNS} FROM memory_items
                   WHERE memory_key=?""",
                (candidate_key,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "candidate"
                or (kind is not None and row["kind"] != kind)
            ):
                _raise("invalid_candidate_cursor")
            candidate = self._verify_candidate(
                conn,
                missing_category="candidate_review_state_invalid",
                row=row,
            )
            return candidate.created_at, candidate.memory_id
        except MemoryCandidateReviewError:
            raise
        except (OSError, sqlite3.Error):
            _raise("storage_unavailable")
        except (KeyError, TypeError, ValueError):
            _raise("candidate_review_state_invalid")

    def _list_candidates(
        self,
        *,
        limit: int,
        after_candidate_key: str | None,
        kind: str | None,
    ) -> tuple[CandidateReviewSummaryV1, ...]:
        with self._connect_read_only() as conn:
            self._prepare_connection(conn)
            clauses = ["status='candidate'"]
            parameters: list[object] = []
            if kind is not None:
                clauses.append("kind=?")
                parameters.append(kind)
            if after_candidate_key is not None:
                cursor_created_at, cursor_id = self._resolve_cursor(
                    conn, after_candidate_key, kind
                )
                clauses.append("(created_at<? OR (created_at=? AND id<?))")
                parameters.extend((cursor_created_at, cursor_created_at, cursor_id))
            parameters.append(limit)
            sql = (
                f"SELECT {_CANDIDATE_COLUMNS} FROM memory_items WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC,id DESC LIMIT ?"
            )
            try:
                rows = conn.execute(sql, tuple(parameters)).fetchall()
            except (OSError, sqlite3.Error, TypeError, ValueError):
                _raise("storage_unavailable")
            result: list[CandidateReviewSummaryV1] = []
            for row in rows:
                candidate = self._verify_candidate(
                    conn,
                    row,
                    missing_category="candidate_review_state_invalid",
                )
                result.append(CandidateReviewSummaryV1(
                    candidate_key=candidate.candidate_key,
                    kind=candidate.kind,
                    content_preview=_preview(candidate.content),
                    created_at=candidate.created_at,
                    provenance_count=len(candidate.evidence),
                ))
            return tuple(result)

    def _get_candidate(self, candidate_key: str) -> CandidateReviewDetailV1:
        with self._connect_read_only() as conn:
            self._prepare_connection(conn)
            try:
                row = conn.execute(
                    f"""SELECT {_CANDIDATE_COLUMNS} FROM memory_items
                        WHERE memory_key=? AND status='candidate'""",
                    (candidate_key,),
                ).fetchone()
            except (OSError, sqlite3.Error, TypeError, ValueError):
                _raise("storage_unavailable")
            if row is None:
                _raise("candidate_not_found")
            candidate = self._verify_candidate(
                conn,
                row,
                missing_category="candidate_unreviewable",
            )
            evidence = self._map_verified_evidence(candidate.evidence)
            return CandidateReviewDetailV1(
                candidate_key=candidate.candidate_key,
                kind=candidate.kind,
                content=candidate.content,
                scope_type=candidate.scope_type,
                scope_ref=candidate.scope_ref,
                sensitivity=candidate.sensitivity,
                explicitness=candidate.explicitness,
                confidence=candidate.confidence,
                created_at=candidate.created_at,
                provenance_count=len(evidence),
                evidence=evidence,
            )

    def _readiness(self) -> tuple[bool, str]:
        """Probe only schema/profile state without reading candidate data."""

        with self._connect_read_only() as conn:
            self._prepare_connection(conn)
        return True, ""


class MemoryCandidateReviewService:
    """Capability gate for the isolated read-only review reader."""

    __slots__ = (
        "_reader",
        "_enabled",
        "_configuration_valid",
        "_constructor_valid",
        "_error_category",
    )

    def __init__(
        self,
        reader: MemoryCandidateReviewReader,
        *,
        enabled: bool,
        configuration_valid: bool,
        error_category: str,
    ):
        self._reader = reader
        self._enabled = enabled
        self._configuration_valid = configuration_valid
        self._error_category = error_category
        self._constructor_valid = (
            type(reader) is MemoryCandidateReviewReader
            and type(enabled) is bool
            and type(configuration_valid) is bool
            and type(error_category) is str
        )

    def __repr__(self) -> str:
        return "<MemoryCandidateReviewService>"

    def _require_enabled(self) -> None:
        if type(self._enabled) is bool and not self._enabled:
            _raise("candidate_review_disabled")
        if not self._constructor_valid:
            _raise("candidate_review_configuration_invalid")
        if not self._configuration_valid:
            _raise("candidate_review_configuration_invalid")

    def readiness(self) -> tuple[bool, str]:
        """Return a bounded health result without inspecting candidate rows."""

        try:
            self._require_enabled()
            return self._reader._readiness()
        except MemoryCandidateReviewError as error:
            return False, error.category
        except Exception:
            return False, "candidate_review_state_invalid"

    def list_candidates(
        self,
        *,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
        after_candidate_key: str | None = None,
        kind: str | None = None,
    ) -> tuple[CandidateReviewSummaryV1, ...]:
        self._require_enabled()
        if type(limit) is not int or not 1 <= limit <= MAX_CANDIDATE_LIMIT:
            _raise("invalid_candidate_limit")
        if kind is not None and (type(kind) is not str or kind not in _AUTOMATIC_KINDS):
            _raise("invalid_candidate_kind")
        if after_candidate_key is not None and (
            type(after_candidate_key) is not str
            or (
                memory_policy.MEMORY_KEY_PATTERN.fullmatch(after_candidate_key)
                is None
            )
        ):
            _raise("invalid_candidate_cursor")
        return self._reader._list_candidates(
            limit=limit,
            after_candidate_key=after_candidate_key,
            kind=kind,
        )

    def get_candidate(self, candidate_key: str) -> CandidateReviewDetailV1:
        self._require_enabled()
        if (
            type(candidate_key) is not str
            or memory_policy.MEMORY_KEY_PATTERN.fullmatch(candidate_key) is None
        ):
            _raise("invalid_candidate_key")
        return self._reader._get_candidate(candidate_key)
