"""Parallel Phase 4D-A persistence for Atomic Memory Formation V2.

This module deliberately does not modify or replace the deployed V1 persistence
path. It binds to an already-authorized ``AutomaticCandidatePersistence``
instance, reuses the existing immutable ``memory_candidate_sources`` row-per-span
schema, and writes one V2 candidate with one or more exact source rows in a
single SQLite transaction.

There is no application/runtime wiring in this module. A later reviewed change
must explicitly compose it before any production path can call it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Final

from backend import (
    channel_store,
    memory_formation_v2,
    memory_formation_extractor_v2,
    memory_policy,
    memory_service,
    memory_store,
)


FORMATION_CONTRACT_VERSION: Final = memory_formation_v2.FORMATION_CONTRACT_VERSION
EXTRACTOR_CONTRACT_VERSION: Final = (
    memory_formation_extractor_v2.EXTRACTOR_CONTRACT_VERSION
)

_ERROR_CATEGORIES: Final = frozenset({
    "auto_candidate_persistence_disabled",
    "candidate_budget_exceeded",
    "candidate_persistence_conflict",
    "candidate_persistence_failed",
    "candidate_policy_rejected",
    "candidate_state_conflict",
    "duplicate_proposal",
    "duplicate_span",
    "empty_spans",
    "feature_disabled",
    "formation_replay_conflict",
    "ineligible_proposal",
    "invalid_canonical_source",
    "invalid_max_item_chars",
    "invalid_proposal",
    "invalid_proposals",
    "invalid_signal_type",
    "invalid_source_message_id",
    "invalid_source_text",
    "invalid_span",
    "invalid_spans",
    "memory_configuration_invalid",
    "memory_fingerprint_profile_mismatch",
    "memory_formation_v2_error",
    "memory_schema_invalid",
    "overlapping_proposals",
    "overlapping_spans",
    "runtime_authority_invalid",
    "source_text_too_long",
    "storage_unavailable",
    "too_many_proposals",
    "too_many_spans",
    "too_many_total_spans",
})


class MemoryCandidatePersistenceV2Error(RuntimeError):
    """Stable, data-free V2 persistence failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe_category = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "candidate_persistence_failed"
        )
        self.category = safe_category
        super().__init__(safe_category)

    def __str__(self) -> str:
        try:
            category = object.__getattribute__(self, "category")
        except Exception:
            return "candidate_persistence_failed"
        return (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "candidate_persistence_failed"
        )

    def __repr__(self) -> str:
        return f"MemoryCandidatePersistenceV2Error({str(self)!r})"


@dataclass(frozen=True, slots=True, repr=False)
class AutomaticCandidatePersistenceV2:
    """Unwired V2 persistence capability bound to the existing runtime authority."""

    _base: memory_service.AutomaticCandidatePersistence = field(repr=False)

    def __post_init__(self) -> None:
        if type(self._base) is not memory_service.AutomaticCandidatePersistence:
            raise MemoryCandidatePersistenceV2Error(
                "candidate_persistence_failed"
            )

    def __repr__(self) -> str:
        return "<AutomaticCandidatePersistenceV2>"

    def persist(
        self,
        *,
        canonical_message_id: object,
        source_text: object,
        proposals: object,
    ) -> memory_store.AutoCandidatePersistenceResult:
        return persist_auto_memory_candidates_v2(
            self._base,
            canonical_message_id=canonical_message_id,
            source_text=source_text,
            proposals=proposals,
        )


def bind_candidate_persistence_v2(
    base: object,
) -> AutomaticCandidatePersistenceV2:
    if type(base) is not memory_service.AutomaticCandidatePersistence:
        raise MemoryCandidatePersistenceV2Error("candidate_persistence_failed")
    return AutomaticCandidatePersistenceV2(base)


def _raise(category: str) -> None:
    raise MemoryCandidatePersistenceV2Error(category)


def _proposal_digest(
    proposals: tuple[memory_formation_v2.AutoMemoryProposalV2, ...],
) -> str:
    payload = [
        {
            "signal_type": proposal.signal_type,
            "spans": [
                {"start": span.start, "end": span.end}
                for span in proposal.spans
            ],
        }
        for proposal in proposals
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_batch_identity(
    proposals: tuple[memory_formation_v2.AutoMemoryProposalV2, ...],
    candidates: tuple[memory_formation_v2.AutoMemoryCandidateV2, ...],
    *,
    canonical_message_id: int,
) -> None:
    if len(proposals) != len(candidates):
        _raise("candidate_state_conflict")
    identities: set[tuple[str, str, str, str]] = set()
    for proposal, candidate in zip(proposals, candidates):
        if (
            type(proposal) is not memory_formation_v2.AutoMemoryProposalV2
            or type(candidate) is not memory_formation_v2.AutoMemoryCandidateV2
            or candidate.source_message_id != canonical_message_id
            or candidate.signal_type != proposal.signal_type
            or candidate.source_spans != proposal.spans
            or candidate.kind
            != memory_formation_v2.SIGNAL_KIND_MAPPING.get(proposal.signal_type)
            or candidate.subject
            != memory_formation_v2.SUBJECT_BY_SIGNAL.get(proposal.signal_type)
            or candidate.scope_type != "global_user"
            or candidate.scope_ref != ""
            or candidate.sensitivity != "normal"
            or type(candidate.normalized_content) is not str
            or not candidate.normalized_content
        ):
            _raise("candidate_state_conflict")
        identity = (
            candidate.kind,
            candidate.scope_type,
            candidate.scope_ref,
            candidate.normalized_content,
        )
        # The existing row-per-span schema has no proposal-group column. Reject
        # two same-memory proposals from one canonical message so a V2 evidence
        # bundle is always unambiguous when read back and re-proved.
        if identity in identities:
            _raise("candidate_state_conflict")
        identities.add(identity)


def _insert_source_rows(
    conn: sqlite3.Connection,
    *,
    memory_id: int,
    canonical_message_id: int,
    proposal: memory_formation_v2.AutoMemoryProposalV2,
    stamp: str,
) -> None:
    for span in proposal.spans:
        conn.execute(
            """INSERT INTO memory_candidate_sources
               (memory_id,canonical_message_id,signal_type,span_start,span_end,
                formation_contract_version,extractor_contract_version,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                memory_id,
                canonical_message_id,
                proposal.signal_type,
                span.start,
                span.end,
                FORMATION_CONTRACT_VERSION,
                EXTRACTOR_CONTRACT_VERSION,
                stamp,
            ),
        )


def _result_from_run(
    row: sqlite3.Row,
    *,
    replayed: bool,
) -> memory_store.AutoCandidatePersistenceResult:
    try:
        return memory_store.MemoryStore._candidate_result_from_run(
            row,
            replayed=replayed,
        )
    except memory_store.MemoryStoreError as error:
        _raise(error.category)
    raise AssertionError("unreachable")


def persist_auto_memory_candidates_v2(
    base: object,
    *,
    canonical_message_id: object,
    source_text: object,
    proposals: object,
) -> memory_store.AutoCandidatePersistenceResult:
    """Persist one V2 candidate batch without changing the V1 production path."""

    if type(base) is not memory_service.AutomaticCandidatePersistence:
        _raise("candidate_persistence_failed")
    store = base._store
    if type(store) is not memory_store.MemoryStore:
        _raise("candidate_persistence_failed")

    try:
        base._require_enabled()
        store._require_candidate_persistence_runtime()
        validated_source = memory_formation_v2._validate_source(
            canonical_message_id,
            source_text,
        )
        validated_proposals = memory_formation_v2.validate_auto_memory_proposals(
            proposals,
            source_length=len(validated_source),
        )
        candidates = memory_formation_v2.build_auto_memory_candidates_v2(
            canonical_message_id,
            validated_source,
            validated_proposals,
            max_item_chars=store._runtime_policy.max_item_chars,
        )
        _validate_batch_identity(
            validated_proposals,
            candidates,
            canonical_message_id=canonical_message_id,
        )
        proposal_digest = _proposal_digest(validated_proposals)
        proposal_count = len(validated_proposals)
        candidate_count = len(candidates)

        with channel_store.connect(store.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                store._require_candidate_persistence_runtime()
                try:
                    channel_store.validate_memory_candidate_persistence_schema(conn)
                except (sqlite3.Error, ValueError):
                    raise MemoryStoreV2BridgeError("memory_schema_invalid") from None
                store._validate_canonical_candidate_source(
                    conn,
                    canonical_message_id=canonical_message_id,
                    source_text=validated_source,
                )
                store._validate_or_initialize_profile(conn, initialize=True)

                existing_run = conn.execute(
                    """SELECT * FROM memory_auto_formation_runs
                       WHERE canonical_message_id=?""",
                    (canonical_message_id,),
                ).fetchone()
                if existing_run is not None:
                    if (
                        existing_run["proposal_digest"] != proposal_digest
                        or existing_run["formation_contract_version"]
                        != FORMATION_CONTRACT_VERSION
                        or existing_run["extractor_contract_version"]
                        != EXTRACTOR_CONTRACT_VERSION
                    ):
                        raise MemoryStoreV2BridgeError(
                            "formation_replay_conflict"
                        )
                    result = _result_from_run(existing_run, replayed=True)
                    if (
                        result.proposal_count != proposal_count
                        or result.candidate_count != candidate_count
                    ):
                        raise MemoryStoreV2BridgeError(
                            "candidate_state_conflict"
                        )
                    conn.execute("COMMIT")
                    return result

                stamp = channel_store.now_iso()
                created_count = 0
                existing_candidate_count = 0
                active_duplicate_count = 0
                suppressed_count = 0

                for proposal, candidate in zip(
                    validated_proposals,
                    candidates,
                ):
                    fingerprint = memory_policy.fingerprint_content(
                        store._runtime_policy.fingerprint_hmac_secret,
                        scope_type=candidate.scope_type,
                        scope_ref=candidate.scope_ref,
                        kind=candidate.kind,
                        normalized_content=candidate.normalized_content,
                    )
                    suppression_ids = store._matching_suppression_ids(
                        conn,
                        scope_type=candidate.scope_type,
                        scope_ref=candidate.scope_ref,
                        kind=candidate.kind,
                        fingerprint_version=(
                            store._runtime_policy.fingerprint_version
                        ),
                        fingerprint=fingerprint,
                    )
                    if suppression_ids:
                        suppressed_count += 1
                        continue

                    existing = store._find_live_by_fingerprint(
                        conn,
                        scope_type=candidate.scope_type,
                        scope_ref=candidate.scope_ref,
                        kind=candidate.kind,
                        fingerprint_version=(
                            store._runtime_policy.fingerprint_version
                        ),
                        fingerprint=fingerprint,
                    )
                    if existing is not None:
                        if (
                            existing["normalized_content"]
                            != candidate.normalized_content
                        ):
                            raise MemoryStoreV2BridgeError(
                                "candidate_state_conflict"
                            )
                        if existing["status"] == "active":
                            active_duplicate_count += 1
                            continue
                        if existing["status"] != "candidate":
                            raise MemoryStoreV2BridgeError(
                                "candidate_state_conflict"
                            )
                        existing_candidate_count += 1
                        _insert_source_rows(
                            conn,
                            memory_id=int(existing["id"]),
                            canonical_message_id=canonical_message_id,
                            proposal=proposal,
                            stamp=stamp,
                        )
                        continue

                    memory_key = secrets.token_urlsafe(24)
                    cursor = conn.execute(
                        """INSERT INTO memory_items
                           (memory_key,kind,scope_type,scope_ref,
                            normalized_content,normalized_fingerprint,
                            fingerprint_version,status,explicitness,confidence,
                            sensitivity,first_observed_at,last_confirmed_at,
                            superseded_by_id,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,'candidate','inferred',0.0,
                                  ?,?,?,NULL,?,?)""",
                        (
                            memory_key,
                            candidate.kind,
                            candidate.scope_type,
                            candidate.scope_ref,
                            candidate.normalized_content,
                            fingerprint,
                            store._runtime_policy.fingerprint_version,
                            candidate.sensitivity,
                            stamp,
                            stamp,
                            stamp,
                            stamp,
                        ),
                    )
                    memory_id = int(cursor.lastrowid)
                    persisted = conn.execute(
                        """SELECT status,explicitness,confidence,scope_type,
                                  scope_ref,sensitivity,kind,normalized_content,
                                  normalized_fingerprint,fingerprint_version
                           FROM memory_items WHERE id=?""",
                        (memory_id,),
                    ).fetchone()
                    if (
                        persisted is None
                        or persisted["status"] != "candidate"
                        or persisted["explicitness"] != "inferred"
                        or persisted["confidence"] != 0.0
                        or persisted["scope_type"] != candidate.scope_type
                        or persisted["scope_ref"] != candidate.scope_ref
                        or persisted["sensitivity"] != candidate.sensitivity
                        or persisted["kind"] != candidate.kind
                        or persisted["normalized_content"]
                        != candidate.normalized_content
                        or persisted["fingerprint_version"]
                        != store._runtime_policy.fingerprint_version
                        or not memory_policy.secure_digest_equal(
                            persisted["normalized_fingerprint"],
                            fingerprint,
                        )
                    ):
                        raise MemoryStoreV2BridgeError(
                            "candidate_state_conflict"
                        )
                    _insert_source_rows(
                        conn,
                        memory_id=memory_id,
                        canonical_message_id=canonical_message_id,
                        proposal=proposal,
                        stamp=stamp,
                    )
                    created_count += 1

                store._before_auto_formation_run_insert(conn)
                conn.execute(
                    """INSERT INTO memory_auto_formation_runs
                       (canonical_message_id,proposal_digest,proposal_count,
                        candidate_count,created_count,existing_candidate_count,
                        active_duplicate_count,suppressed_count,
                        formation_contract_version,extractor_contract_version,
                        created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        canonical_message_id,
                        proposal_digest,
                        proposal_count,
                        candidate_count,
                        created_count,
                        existing_candidate_count,
                        active_duplicate_count,
                        suppressed_count,
                        FORMATION_CONTRACT_VERSION,
                        EXTRACTOR_CONTRACT_VERSION,
                        stamp,
                    ),
                )
                run = conn.execute(
                    """SELECT * FROM memory_auto_formation_runs
                       WHERE canonical_message_id=?""",
                    (canonical_message_id,),
                ).fetchone()
                if run is None:
                    raise MemoryStoreV2BridgeError(
                        "candidate_state_conflict"
                    )
                result = _result_from_run(run, replayed=False)
                conn.execute("COMMIT")
                return result
            except BaseException:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
    except MemoryCandidatePersistenceV2Error:
        raise
    except memory_formation_v2.MemoryFormationV2Error as error:
        _raise(error.category)
    except memory_service.MemoryServiceError as error:
        _raise(getattr(error, "category", "candidate_persistence_failed"))
    except memory_store.MemoryStoreError as error:
        _raise(getattr(error, "category", "candidate_persistence_failed"))
    except MemoryStoreV2BridgeError as error:
        _raise(error.category)
    except sqlite3.IntegrityError:
        _raise("candidate_persistence_conflict")
    except (OSError, sqlite3.Error):
        _raise("storage_unavailable")
    except Exception:
        _raise("candidate_persistence_failed")


class MemoryStoreV2BridgeError(RuntimeError):
    """Internal-only control-flow error; always translated before crossing API."""

    __slots__ = ("category",)

    def __init__(self, category: str):
        self.category = category
        super().__init__(category)
