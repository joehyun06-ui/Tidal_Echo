"""Pure binding and replay semantics for the candidate decision ledger.

This Slice 1 module exposes no approve/reject service, opens no database, owns
no transaction, and has no Memory runtime or write authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final

try:
    from . import memory_candidate_review, memory_policy
except ImportError:  # support direct module execution in local tooling
    import memory_candidate_review
    import memory_policy


CANDIDATE_DECISION_CONTRACT_VERSION: Final = (
    "memory-candidate-decision-v1"
)
CANDIDATE_DECISION_REQUEST_ID_PATTERN: Final = (
    memory_policy.OPAQUE_MEMORY_ID_PATTERN
)

_ORIGINS: Final = frozenset({"operator_cli", "mcp"})
_DECISIONS: Final = frozenset({"approve", "reject"})
_ERROR_CATEGORIES: Final = frozenset({
    "candidate_decisions_disabled",
    "candidate_decision_configuration_invalid",
    "candidate_decision_schema_invalid",
    "candidate_decision_profile_mismatch",
    "candidate_decision_state_invalid",
    "candidate_not_pending",
    "invalid_candidate_decision_request",
    "invalid_candidate_key",
    "candidate_decision_request_conflict",
    "candidate_decision_conflict",
    "runtime_authority_invalid",
    "storage_unavailable",
})


class MemoryCandidateDecisionLedgerError(RuntimeError):
    """A stable, closed, data-free decision-ledger failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe_category = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "candidate_decision_state_invalid"
        )
        self.category = safe_category
        super().__init__(safe_category)

    def __str__(self) -> str:
        try:
            category = object.__getattribute__(self, "category")
        except Exception:
            return "candidate_decision_state_invalid"
        return (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "candidate_decision_state_invalid"
        )

    def __repr__(self) -> str:
        return f"MemoryCandidateDecisionLedgerError({str(self)!r})"


@dataclass(frozen=True, slots=True, repr=False)
class CandidateDecisionLedgerBindingV1:
    request_id: str = field(repr=False)
    origin: str
    decision: str
    candidate_key: str = field(repr=False)
    review_contract_version: str = field(
        default=memory_candidate_review.CANDIDATE_REVIEW_CONTRACT_VERSION,
        init=False,
    )
    decision_contract_version: str = field(
        default=CANDIDATE_DECISION_CONTRACT_VERSION,
        init=False,
    )

    def __repr__(self) -> str:
        return "<CandidateDecisionLedgerBindingV1>"


@dataclass(frozen=True, slots=True, repr=False)
class CandidateDecisionLedgerRowV1:
    request_id: str = field(repr=False)
    memory_id: int = field(repr=False)
    origin: str
    decision: str
    request_binding_digest: bytes = field(repr=False)
    suppression_id: int | None = field(repr=False)
    review_contract_version: str
    decision_contract_version: str
    created_at: str

    def __repr__(self) -> str:
        return "<CandidateDecisionLedgerRowV1>"


def issue_candidate_decision_request_id() -> str:
    """Issue one opaque server-generated ledger identity."""
    request_id = secrets.token_urlsafe(24)
    if CANDIDATE_DECISION_REQUEST_ID_PATTERN.fullmatch(request_id) is None:
        raise MemoryCandidateDecisionLedgerError(
            "candidate_decision_state_invalid"
        )
    return request_id


def validate_binding(
    binding: object,
) -> CandidateDecisionLedgerBindingV1:
    """Validate the fixed v1 semantic decision projection."""
    if type(binding) is not CandidateDecisionLedgerBindingV1:
        raise MemoryCandidateDecisionLedgerError(
            "invalid_candidate_decision_request"
        )
    if (
        type(binding.request_id) is not str
        or CANDIDATE_DECISION_REQUEST_ID_PATTERN.fullmatch(
            binding.request_id
        )
        is None
        or type(binding.origin) is not str
        or binding.origin not in _ORIGINS
        or type(binding.decision) is not str
        or binding.decision not in _DECISIONS
        or binding.review_contract_version
        != memory_candidate_review.CANDIDATE_REVIEW_CONTRACT_VERSION
        or binding.decision_contract_version
        != CANDIDATE_DECISION_CONTRACT_VERSION
    ):
        raise MemoryCandidateDecisionLedgerError(
            "invalid_candidate_decision_request"
        )
    if (
        type(binding.candidate_key) is not str
        or memory_policy.MEMORY_KEY_PATTERN.fullmatch(binding.candidate_key)
        is None
    ):
        raise MemoryCandidateDecisionLedgerError("invalid_candidate_key")
    return binding


def binding_digest(binding: object) -> bytes:
    """Return the raw SHA-256 digest of the exact canonical v1 projection."""
    valid = validate_binding(binding)
    projection = {
        "candidate_key": valid.candidate_key,
        "decision": valid.decision,
        "decision_contract_version": valid.decision_contract_version,
        "origin": valid.origin,
        "review_contract_version": valid.review_contract_version,
    }
    encoded = json.dumps(
        projection,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).digest()


def _valid_terminal_timestamp(value: object) -> bool:
    if type(value) is not str or not 25 <= len(value) <= 40:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(None)


def _row_from_sqlite(row: sqlite3.Row) -> CandidateDecisionLedgerRowV1:
    try:
        request_id = row["request_id"]
        memory_id = row["memory_id"]
        origin = row["origin"]
        decision = row["decision"]
        digest = row["request_binding_digest"]
        suppression_id = row["suppression_id"]
        review_version = row["review_contract_version"]
        decision_version = row["decision_contract_version"]
        created_at = row["created_at"]
    except (IndexError, KeyError, TypeError):
        raise MemoryCandidateDecisionLedgerError(
            "candidate_decision_state_invalid"
        ) from None
    if (
        type(request_id) is not str
        or CANDIDATE_DECISION_REQUEST_ID_PATTERN.fullmatch(request_id) is None
        or type(memory_id) is not int
        or memory_id <= 0
        or type(origin) is not str
        or origin not in _ORIGINS
        or type(decision) is not str
        or decision not in _DECISIONS
        or type(digest) is not bytes
        or len(digest) != hashlib.sha256().digest_size
        or (
            suppression_id is not None
            and (type(suppression_id) is not int or suppression_id <= 0)
        )
        or (decision == "approve" and suppression_id is not None)
        or (decision == "reject" and suppression_id is None)
        or review_version
        != memory_candidate_review.CANDIDATE_REVIEW_CONTRACT_VERSION
        or decision_version != CANDIDATE_DECISION_CONTRACT_VERSION
        or not _valid_terminal_timestamp(created_at)
    ):
        raise MemoryCandidateDecisionLedgerError(
            "candidate_decision_state_invalid"
        )
    return CandidateDecisionLedgerRowV1(
        request_id=request_id,
        memory_id=memory_id,
        origin=origin,
        decision=decision,
        request_binding_digest=digest,
        suppression_id=suppression_id,
        review_contract_version=review_version,
        decision_contract_version=decision_version,
        created_at=created_at,
    )


def lookup_request(
    conn: sqlite3.Connection,
    request_id: object,
) -> CandidateDecisionLedgerRowV1 | None:
    """Look up an existing terminal request on a caller-owned connection."""
    if (
        not isinstance(conn, sqlite3.Connection)
        or type(request_id) is not str
        or CANDIDATE_DECISION_REQUEST_ID_PATTERN.fullmatch(request_id) is None
    ):
        raise MemoryCandidateDecisionLedgerError(
            "invalid_candidate_decision_request"
        )
    try:
        row = conn.execute(
            """SELECT request_id,memory_id,origin,decision,
                      request_binding_digest,suppression_id,
                      review_contract_version,decision_contract_version,
                      created_at
                 FROM memory_candidate_decisions WHERE request_id=?""",
            (request_id,),
        ).fetchone()
    except sqlite3.Error:
        raise MemoryCandidateDecisionLedgerError(
            "candidate_decision_schema_invalid"
        ) from None
    return None if row is None else _row_from_sqlite(row)


def validate_replay_binding(
    row: object,
    binding: object,
) -> CandidateDecisionLedgerRowV1:
    """Recognize an exact replay or reject reuse of the request identity."""
    valid = validate_binding(binding)
    if type(row) is not CandidateDecisionLedgerRowV1:
        raise MemoryCandidateDecisionLedgerError(
            "candidate_decision_state_invalid"
        )
    digest = binding_digest(valid)
    if (
        row.request_id != valid.request_id
        or row.origin != valid.origin
        or row.decision != valid.decision
        or row.review_contract_version != valid.review_contract_version
        or row.decision_contract_version != valid.decision_contract_version
        or not hmac.compare_digest(row.request_binding_digest, digest)
    ):
        raise MemoryCandidateDecisionLedgerError(
            "candidate_decision_request_conflict"
        )
    return row
