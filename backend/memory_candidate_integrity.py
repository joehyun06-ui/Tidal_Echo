"""Neutral proof of automatic Memory candidate and terminal integrity.

The verifier owns no database path, runtime authority, store, service, or
transaction.  Callers supply an already-open SQLite connection and a row from
``memory_items``; every public verification mode fixes its own terminal state.
"""

from __future__ import annotations

import hmac
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

try:
    from . import deployment_config, memory_formation, memory_policy
except ImportError:  # support direct module execution in local tooling
    import deployment_config
    import memory_formation
    import memory_policy


CANDIDATE_REVIEW_CONTRACT_VERSION: Final = "memory-candidate-review-v1"
EVIDENCE_CONTEXT_CHARS: Final = 160
MAX_SOURCE_EXCERPT_CHARS: Final = 2320

AUTOMATIC_MEMORY_COLUMNS: Final = """id,memory_key,kind,scope_type,scope_ref,
    normalized_content,normalized_fingerprint,fingerprint_version,status,
    explicitness,confidence,sensitivity,first_observed_at,last_confirmed_at,
    superseded_by_id,created_at,updated_at"""

_PROFILE_KEY_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_CONTRACT_VERSION: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_AUTOMATIC_KINDS: Final = frozenset(
    memory_formation.SIGNAL_KIND_MAPPING.values()
)
_AUTOMATIC_SIGNALS: Final = frozenset(memory_formation.SIGNAL_KIND_MAPPING)
_ERROR_CATEGORIES: Final = frozenset({
    "candidate_integrity_invalid",
    "candidate_provenance_missing",
    "candidate_integrity_profile_mismatch",
    "candidate_integrity_schema_invalid",
    "storage_unavailable",
})


class AutomaticCandidateIntegrityError(RuntimeError):
    """Stable, closed, data-free automatic-candidate proof failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe_category = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "candidate_integrity_invalid"
        )
        self.category = safe_category
        super().__init__(safe_category)

    def __str__(self) -> str:
        try:
            category = object.__getattribute__(self, "category")
        except Exception:
            return "candidate_integrity_invalid"
        return (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "candidate_integrity_invalid"
        )

    def __repr__(self) -> str:
        return f"AutomaticCandidateIntegrityError({str(self)!r})"


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedAutomaticEvidenceV1:
    source_id: int = field(repr=False)
    canonical_message_id: int = field(repr=False)
    signal_type: str
    span_start: int = field(repr=False)
    span_end: int = field(repr=False)
    formation_contract_version: str
    extractor_contract_version: str
    observed_at: str
    source_excerpt: str = field(repr=False)

    def __repr__(self) -> str:
        return "<VerifiedAutomaticEvidenceV1>"


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedAutomaticMemoryV1:
    memory_id: int = field(repr=False)
    candidate_key: str = field(repr=False)
    kind: str
    content: str = field(repr=False)
    fingerprint: bytes = field(repr=False)
    fingerprint_version: int
    scope_type: str
    scope_ref: str = field(repr=False)
    sensitivity: str
    explicitness: str
    confidence: float
    first_observed_at: str
    last_confirmed_at: str
    created_at: str
    updated_at: str
    evidence: tuple[VerifiedAutomaticEvidenceV1, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "<VerifiedAutomaticMemoryV1>"


def _raise(category: str) -> None:
    raise AutomaticCandidateIntegrityError(category)


def _timestamp(value: object) -> datetime | None:
    if type(value) is not str or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed


class AutomaticCandidateIntegrityVerifier:
    """Pinned, read-only proof shared by review and terminal decisions."""

    __slots__ = (
        "_fingerprint_key_id",
        "_fingerprint_key_check",
        "_fingerprint_hmac_secret",
        "_max_item_chars",
        "_policy",
    )

    def __init__(
        self,
        *,
        fingerprint_key_id: str,
        fingerprint_hmac_secret: str,
        max_item_chars: int,
    ):
        try:
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
        except Exception:
            _raise("candidate_integrity_invalid")
        self._fingerprint_key_id = fingerprint_key_id
        self._fingerprint_key_check = key_check
        self._fingerprint_hmac_secret = fingerprint_hmac_secret
        self._max_item_chars = max_item_chars
        self._policy = policy

    def __repr__(self) -> str:
        return "<AutomaticCandidateIntegrityVerifier>"

    def verify_profile(self, conn: sqlite3.Connection) -> None:
        """Prove one exact existing fingerprint profile without bootstrap."""

        if not isinstance(conn, sqlite3.Connection):
            _raise("candidate_integrity_profile_mismatch")
        try:
            rows = conn.execute(
                """SELECT singleton,key_id,key_check,normalization_version,
                          fingerprint_version
                   FROM memory_fingerprint_profile ORDER BY singleton"""
            ).fetchall()
            if len(rows) != 1:
                _raise("candidate_integrity_profile_mismatch")
            row = rows[0]
            if (
                type(row["singleton"]) is not int
                or row["singleton"] != 1
                or type(row["key_id"]) is not str
                or not hmac.compare_digest(
                    row["key_id"], self._fingerprint_key_id
                )
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
                _raise("candidate_integrity_profile_mismatch")
        except AutomaticCandidateIntegrityError:
            raise
        except (OSError, sqlite3.Error, IndexError, KeyError, TypeError, ValueError):
            _raise("candidate_integrity_profile_mismatch")

    def verify_pending_candidate(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> VerifiedAutomaticMemoryV1:
        return self._verify_fixed(conn, row, mode="pending")

    def verify_approved_memory(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> VerifiedAutomaticMemoryV1:
        return self._verify_fixed(conn, row, mode="approved")

    def verify_rejected_memory(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> VerifiedAutomaticMemoryV1:
        return self._verify_fixed(conn, row, mode="rejected")

    def _verify_fixed(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        mode: str,
    ) -> VerifiedAutomaticMemoryV1:
        fixed = {
            "pending": ("candidate", 0.0),
            "approved": ("active", 1.0),
            "rejected": ("rejected", 0.0),
        }.get(mode)
        if fixed is None or not isinstance(conn, sqlite3.Connection):
            _raise("candidate_integrity_invalid")
        expected_status, expected_confidence = fixed
        try:
            memory_id = row["id"]
            candidate_key = row["memory_key"]
            kind = row["kind"]
            scope_type = row["scope_type"]
            scope_ref = row["scope_ref"]
            content = row["normalized_content"]
            fingerprint = row["normalized_fingerprint"]
            fingerprint_version = row["fingerprint_version"]
            status = row["status"]
            explicitness = row["explicitness"]
            confidence = row["confidence"]
            sensitivity = row["sensitivity"]
            first_observed_at = row["first_observed_at"]
            last_confirmed_at = row["last_confirmed_at"]
            superseded_by_id = row["superseded_by_id"]
            created_at = row["created_at"]
            updated_at = row["updated_at"]
        except (IndexError, KeyError, TypeError):
            _raise("candidate_integrity_invalid")

        first_timestamp = _timestamp(first_observed_at)
        last_timestamp = _timestamp(last_confirmed_at)
        created_timestamp = _timestamp(created_at)
        updated_timestamp = _timestamp(updated_at)
        if (
            type(memory_id) is not int
            or memory_id <= 0
            or type(candidate_key) is not str
            or memory_policy.MEMORY_KEY_PATTERN.fullmatch(candidate_key) is None
            or type(kind) is not str
            or kind not in _AUTOMATIC_KINDS
            or scope_type != memory_formation.SCOPE_TYPE
            or scope_ref != memory_formation.SCOPE_REF
            or type(content) is not str
            or not content
            or type(fingerprint) is not bytes
            or len(fingerprint) != memory_policy.HMAC_DIGEST_BYTES
            or type(fingerprint_version) is not int
            or fingerprint_version != memory_policy.FINGERPRINT_VERSION
            or status != expected_status
            or explicitness != "inferred"
            or type(confidence) is not float
            or confidence != expected_confidence
            or sensitivity != memory_formation.SENSITIVITY
            or superseded_by_id is not None
            or first_timestamp is None
            or last_timestamp is None
            or created_timestamp is None
            or updated_timestamp is None
        ):
            _raise("candidate_integrity_invalid")

        try:
            canonical = self._policy.validate_content(
                content,
                memory_formation.SENSITIVITY,
            )
            expected_fingerprint = memory_policy.fingerprint_content(
                self._fingerprint_hmac_secret,
                scope_type=memory_formation.SCOPE_TYPE,
                scope_ref=memory_formation.SCOPE_REF,
                kind=kind,
                normalized_content=content,
            )
        except memory_policy.MemoryPolicyError:
            _raise("candidate_integrity_invalid")
        if (
            canonical != content
            or not memory_policy.secure_digest_equal(
                fingerprint, expected_fingerprint
            )
        ):
            _raise("candidate_integrity_invalid")

        evidence = self._verify_evidence(
            conn,
            memory_id=memory_id,
            kind=kind,
            content=content,
            fingerprint=fingerprint,
        )
        return VerifiedAutomaticMemoryV1(
            memory_id=memory_id,
            candidate_key=candidate_key,
            kind=kind,
            content=content,
            fingerprint=fingerprint,
            fingerprint_version=fingerprint_version,
            scope_type=scope_type,
            scope_ref=scope_ref,
            sensitivity=sensitivity,
            explicitness=explicitness,
            confidence=confidence,
            first_observed_at=first_observed_at,
            last_confirmed_at=last_confirmed_at,
            created_at=created_at,
            updated_at=updated_at,
            evidence=evidence,
        )

    def _verify_evidence(
        self,
        conn: sqlite3.Connection,
        *,
        memory_id: int,
        kind: str,
        content: str,
        fingerprint: bytes,
    ) -> tuple[VerifiedAutomaticEvidenceV1, ...]:
        try:
            rows = conn.execute(
                """SELECT id,canonical_message_id,signal_type,span_start,span_end,
                          formation_contract_version,extractor_contract_version,
                          created_at
                   FROM memory_candidate_sources
                   WHERE memory_id=? ORDER BY created_at ASC,id ASC""",
                (memory_id,),
            ).fetchall()
            if not rows:
                _raise("candidate_provenance_missing")
            evidence: list[VerifiedAutomaticEvidenceV1] = []
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
                    or _timestamp(observed_at) is None
                ):
                    _raise("candidate_integrity_invalid")

                message = conn.execute(
                    "SELECT id,direction,kind,text FROM messages WHERE id=?",
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
                    _raise("candidate_integrity_invalid")
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
                    _raise("candidate_integrity_invalid")
                if len(rebuilt) != 1:
                    _raise("candidate_integrity_invalid")
                proof = rebuilt[0]
                if (
                    type(proof) is not memory_formation.AutoMemoryCandidateV1
                    or proof.source_message_id != canonical_message_id
                    or proof.signal_type != signal_type
                    or proof.kind != kind
                    or proof.scope_type != memory_formation.SCOPE_TYPE
                    or proof.scope_ref != memory_formation.SCOPE_REF
                    or proof.sensitivity != memory_formation.SENSITIVITY
                    or proof.normalized_content != content
                ):
                    _raise("candidate_integrity_invalid")
                proof_fingerprint = memory_policy.fingerprint_content(
                    self._fingerprint_hmac_secret,
                    scope_type=proof.scope_type,
                    scope_ref=proof.scope_ref,
                    kind=proof.kind,
                    normalized_content=proof.normalized_content,
                )
                if not memory_policy.secure_digest_equal(
                    fingerprint, proof_fingerprint
                ):
                    _raise("candidate_integrity_invalid")
                excerpt = source_text[
                    max(0, start - EVIDENCE_CONTEXT_CHARS):
                    min(len(source_text), end + EVIDENCE_CONTEXT_CHARS)
                ]
                if len(excerpt) > MAX_SOURCE_EXCERPT_CHARS:
                    _raise("candidate_integrity_invalid")
                evidence.append(VerifiedAutomaticEvidenceV1(
                    source_id=source_id,
                    canonical_message_id=canonical_message_id,
                    signal_type=signal_type,
                    span_start=start,
                    span_end=end,
                    formation_contract_version=formation_version,
                    extractor_contract_version=extractor_version,
                    observed_at=observed_at,
                    source_excerpt=excerpt,
                ))
            return tuple(evidence)
        except AutomaticCandidateIntegrityError:
            raise
        except (OSError, sqlite3.Error):
            _raise("storage_unavailable")
        except (IndexError, KeyError, TypeError, ValueError):
            _raise("candidate_integrity_invalid")
