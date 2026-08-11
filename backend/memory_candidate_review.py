"""Read-only, profile-bound review of automatic Memory candidates.

This module deliberately owns no ``MemoryRuntime`` or write-capable store.  It
opens the relay database with SQLite ``mode=ro``, validates the frozen schema
and fingerprint profile, and re-proves every returned candidate from canonical
Phase 4A evidence.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

try:
    from . import channel_store, deployment_config, memory_formation, memory_policy
except ImportError:  # support direct module execution in local tooling
    import channel_store
    import deployment_config
    import memory_formation
    import memory_policy


DEFAULT_CANDIDATE_LIMIT: Final = 20
MAX_CANDIDATE_LIMIT: Final = 50
MAX_CONTENT_PREVIEW_CHARS: Final = 240
EVIDENCE_CONTEXT_CHARS: Final = 160
MAX_SOURCE_EXCERPT_CHARS: Final = 2320

_PROFILE_KEY_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_CONTRACT_VERSION: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_AUTOMATIC_KINDS: Final = frozenset(memory_formation.SIGNAL_KIND_MAPPING.values())
_AUTOMATIC_SIGNALS: Final = frozenset(memory_formation.SIGNAL_KIND_MAPPING)

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

_CANDIDATE_COLUMNS: Final = """id,memory_key,kind,scope_type,scope_ref,
    normalized_content,normalized_fingerprint,fingerprint_version,status,
    explicitness,confidence,sensitivity,created_at"""


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


@dataclass(frozen=True, slots=True, repr=False)
class _ValidatedCandidate:
    memory_id: int
    candidate_key: str
    kind: str
    content: str
    fingerprint: bytes
    created_at: str


def _raise(category: str) -> None:
    raise MemoryCandidateReviewError(category)


def _is_valid_timestamp(value: object) -> bool:
    if type(value) is not str or not value or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    except (TypeError, ValueError, OverflowError):
        return False


def _preview(content: str) -> str:
    if len(content) <= MAX_CONTENT_PREVIEW_CHARS:
        return content
    return content[: MAX_CONTENT_PREVIEW_CHARS - 1] + "…"


class MemoryCandidateReviewReader:
    """Isolated read-only reader with pinned fingerprint expectations."""

    __slots__ = (
        "_database_path",
        "_fingerprint_key_id",
        "_fingerprint_key_check",
        "_fingerprint_hmac_secret",
        "_max_item_chars",
        "_policy",
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
                or _PROFILE_KEY_ID.fullmatch(fingerprint_key_id) is None
                or type(fingerprint_hmac_secret) is not str
                or not deployment_config.memory_fingerprint_secret_is_strong(
                    fingerprint_hmac_secret
                )
                or type(max_item_chars) is not int
                or not 64 <= max_item_chars <= 4096
            ):
                raise ValueError
            key_check = memory_policy.fingerprint_profile_check(
                fingerprint_hmac_secret
            )
            policy = memory_policy.MemoryPolicy(
                max_item_chars=max_item_chars,
                sensitive_storage_enabled=False,
            )
        except (MemoryCandidateReviewError,):
            raise
        except Exception:
            _raise("candidate_review_configuration_invalid")
        self._database_path = str(database)
        self._fingerprint_key_id = fingerprint_key_id
        self._fingerprint_key_check = key_check
        self._fingerprint_hmac_secret = fingerprint_hmac_secret
        self._max_item_chars = max_item_chars
        self._policy = policy

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
            rows = conn.execute(
                """SELECT singleton,key_id,key_check,normalization_version,
                          fingerprint_version
                   FROM memory_fingerprint_profile ORDER BY singleton"""
            ).fetchall()
            if len(rows) != 1:
                _raise("candidate_review_profile_mismatch")
            row = rows[0]
            if (
                type(row["singleton"]) is not int
                or row["singleton"] != 1
                or type(row["key_id"]) is not str
                or row["key_id"] != self._fingerprint_key_id
                or type(row["key_check"]) is not bytes
                or not memory_policy.secure_digest_equal(
                    row["key_check"], self._fingerprint_key_check
                )
                or type(row["normalization_version"]) is not int
                or row["normalization_version"]
                != memory_policy.NORMALIZATION_VERSION
                or type(row["fingerprint_version"]) is not int
                or row["fingerprint_version"]
                != memory_policy.FINGERPRINT_VERSION
            ):
                _raise("candidate_review_profile_mismatch")
        except MemoryCandidateReviewError:
            raise
        except (OSError, sqlite3.Error, KeyError, TypeError, ValueError):
            _raise("candidate_review_profile_mismatch")

    def _prepare_connection(self, conn: sqlite3.Connection) -> None:
        self._validate_schema(conn)
        self._validate_profile(conn)

    def _validate_candidate_row(self, row: sqlite3.Row) -> _ValidatedCandidate:
        try:
            memory_id = row["id"]
            candidate_key = row["memory_key"]
            kind = row["kind"]
            content = row["normalized_content"]
            fingerprint = row["normalized_fingerprint"]
            confidence = row["confidence"]
            created_at = row["created_at"]
            if (
                type(memory_id) is not int
                or memory_id <= 0
                or type(candidate_key) is not str
                or (
                    memory_policy.MEMORY_KEY_PATTERN.fullmatch(candidate_key)
                    is None
                )
                or type(kind) is not str
                or kind not in _AUTOMATIC_KINDS
                or row["status"] != "candidate"
                or row["explicitness"] != "inferred"
                or type(confidence) is not float
                or confidence != 0.0
                or row["scope_type"] != memory_formation.SCOPE_TYPE
                or row["scope_ref"] != memory_formation.SCOPE_REF
                or row["sensitivity"] != memory_formation.SENSITIVITY
                or type(content) is not str
                or not content
                or type(row["fingerprint_version"]) is not int
                or row["fingerprint_version"] != memory_policy.FINGERPRINT_VERSION
                or type(fingerprint) is not bytes
                or len(fingerprint) != memory_policy.HMAC_DIGEST_BYTES
                or not _is_valid_timestamp(created_at)
            ):
                _raise("candidate_review_state_invalid")
            canonical = self._policy.validate_content(
                content,
                memory_formation.SENSITIVITY,
            )
            if canonical != content:
                _raise("candidate_review_state_invalid")
            expected = memory_policy.fingerprint_content(
                self._fingerprint_hmac_secret,
                scope_type=memory_formation.SCOPE_TYPE,
                scope_ref=memory_formation.SCOPE_REF,
                kind=kind,
                normalized_content=content,
            )
            if not memory_policy.secure_digest_equal(fingerprint, expected):
                _raise("candidate_review_state_invalid")
        except MemoryCandidateReviewError:
            raise
        except (memory_policy.MemoryPolicyError, KeyError, TypeError, ValueError):
            _raise("candidate_review_state_invalid")
        return _ValidatedCandidate(
            memory_id=memory_id,
            candidate_key=candidate_key,
            kind=kind,
            content=content,
            fingerprint=fingerprint,
            created_at=created_at,
        )

    def _validate_evidence(
        self,
        conn: sqlite3.Connection,
        candidate: _ValidatedCandidate,
        *,
        missing_category: str,
    ) -> tuple[CandidateReviewEvidenceV1, ...]:
        try:
            rows = conn.execute(
                """SELECT id,canonical_message_id,signal_type,span_start,span_end,
                          formation_contract_version,extractor_contract_version,
                          created_at
                   FROM memory_candidate_sources
                   WHERE memory_id=? ORDER BY created_at ASC,id ASC""",
                (candidate.memory_id,),
            ).fetchall()
            if not rows:
                _raise(missing_category)
            evidence: list[CandidateReviewEvidenceV1] = []
            for row in rows:
                source_id = row["id"]
                canonical_message_id = row["canonical_message_id"]
                signal_type = row["signal_type"]
                start = row["span_start"]
                end = row["span_end"]
                formation_version = row["formation_contract_version"]
                extractor_version = row["extractor_contract_version"]
                observed_at = row["created_at"]
                if (
                    type(source_id) is not int
                    or source_id <= 0
                    or type(canonical_message_id) is not int
                    or canonical_message_id <= 0
                    or type(signal_type) is not str
                    or signal_type not in _AUTOMATIC_SIGNALS
                    or type(start) is not int
                    or type(end) is not int
                    or not 0 <= start < end
                    or type(formation_version) is not str
                    or _CONTRACT_VERSION.fullmatch(formation_version) is None
                    or type(extractor_version) is not str
                    or _CONTRACT_VERSION.fullmatch(extractor_version) is None
                    or not _is_valid_timestamp(observed_at)
                ):
                    _raise("candidate_review_state_invalid")

                message = conn.execute(
                    """SELECT id,direction,kind,text FROM messages WHERE id=?""",
                    (canonical_message_id,),
                ).fetchone()
                if (
                    message is None
                    or type(message["id"]) is not int
                    or message["id"] != canonical_message_id
                    or message["direction"] != "in"
                    or message["kind"] != "user"
                    or type(message["text"]) is not str
                    or end > len(message["text"])
                    or end - start > memory_formation.TOTAL_CANDIDATE_MAX_CHARS
                ):
                    _raise("candidate_review_state_invalid")
                source_text = message["text"]
                proposal = memory_formation.AutoMemoryProposalV1(
                    signal_type=signal_type,
                    start=start,
                    end=end,
                )
                try:
                    rebuilt = memory_formation.build_auto_memory_candidates(
                        canonical_message_id,
                        source_text,
                        (proposal,),
                        max_item_chars=self._max_item_chars,
                    )
                except memory_formation.MemoryFormationError:
                    _raise("candidate_review_state_invalid")
                if len(rebuilt) != 1:
                    _raise("candidate_review_state_invalid")
                proof = rebuilt[0]
                if (
                    type(proof) is not memory_formation.AutoMemoryCandidateV1
                    or proof.source_message_id != canonical_message_id
                    or proof.signal_type != signal_type
                    or proof.kind != candidate.kind
                    or proof.scope_type != memory_formation.SCOPE_TYPE
                    or proof.scope_ref != memory_formation.SCOPE_REF
                    or proof.sensitivity != memory_formation.SENSITIVITY
                    or proof.normalized_content != candidate.content
                ):
                    _raise("candidate_review_state_invalid")
                proof_fingerprint = memory_policy.fingerprint_content(
                    self._fingerprint_hmac_secret,
                    scope_type=proof.scope_type,
                    scope_ref=proof.scope_ref,
                    kind=proof.kind,
                    normalized_content=proof.normalized_content,
                )
                if not memory_policy.secure_digest_equal(
                    candidate.fingerprint, proof_fingerprint
                ):
                    _raise("candidate_review_state_invalid")
                excerpt = source_text[
                    max(0, start - EVIDENCE_CONTEXT_CHARS):
                    min(len(source_text), end + EVIDENCE_CONTEXT_CHARS)
                ]
                if len(excerpt) > MAX_SOURCE_EXCERPT_CHARS:
                    _raise("candidate_review_state_invalid")
                evidence.append(CandidateReviewEvidenceV1(
                    signal_type=signal_type,
                    observed_at=observed_at,
                    formation_contract_version=formation_version,
                    extractor_contract_version=extractor_version,
                    source_excerpt=excerpt,
                ))
            return tuple(evidence)
        except MemoryCandidateReviewError:
            raise
        except (OSError, sqlite3.Error):
            _raise("storage_unavailable")
        except (KeyError, TypeError, ValueError):
            _raise("candidate_review_state_invalid")

    def _resolve_cursor(
        self,
        conn: sqlite3.Connection,
        candidate_key: str,
        kind: str | None,
    ) -> tuple[str, int]:
        try:
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
            candidate = self._validate_candidate_row(row)
            self._validate_evidence(
                conn,
                candidate,
                missing_category="candidate_review_state_invalid",
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
                candidate = self._validate_candidate_row(row)
                evidence = self._validate_evidence(
                    conn,
                    candidate,
                    missing_category="candidate_review_state_invalid",
                )
                result.append(CandidateReviewSummaryV1(
                    candidate_key=candidate.candidate_key,
                    kind=candidate.kind,
                    content_preview=_preview(candidate.content),
                    created_at=candidate.created_at,
                    provenance_count=len(evidence),
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
            candidate = self._validate_candidate_row(row)
            evidence = self._validate_evidence(
                conn,
                candidate,
                missing_category="candidate_unreviewable",
            )
            return CandidateReviewDetailV1(
                candidate_key=candidate.candidate_key,
                kind=candidate.kind,
                content=candidate.content,
                scope_type=memory_formation.SCOPE_TYPE,
                scope_ref=memory_formation.SCOPE_REF,
                sensitivity=memory_formation.SENSITIVITY,
                explicitness="inferred",
                confidence=0.0,
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
