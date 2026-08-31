"""Unwired V2-aware terminal decisions for automatic Memory candidates.

This module preserves the existing immutable decision ledger, suppression,
replay, and transaction semantics. It differs from the deployed writer only in
using the multi-span-aware integrity verifier before and after the terminal
status mutation.

No production composition imports this module yet.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Final

from backend import (
    channel_store,
    memory_candidate_decision_ledger,
    memory_candidate_integrity,
    memory_candidate_integrity_v2,
    memory_service,
    memory_store,
)


_SAFE_ERROR_CATEGORIES: Final = frozenset({
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


@dataclass(frozen=True, slots=True, repr=False)
class CandidateDecisionWriterV2:
    """V2-aware decision capability bound to an existing authorized writer."""

    _base: memory_service.CandidateDecisionWriter = field(repr=False)

    def __post_init__(self) -> None:
        if type(self._base) is not memory_service.CandidateDecisionWriter:
            raise memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError(
                "candidate_decision_configuration_invalid"
            )

    def __repr__(self) -> str:
        return "<CandidateDecisionWriterV2>"

    def readiness(self) -> tuple[bool, str]:
        # Existing readiness checks only authority/schema/profile and does not
        # inspect candidate evidence, so its semantics remain valid for V2.
        return self._base.readiness()

    def decide(
        self,
        *,
        binding: memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1,
    ) -> memory_candidate_decision_ledger.CandidateDecisionResultV1:
        return decide_memory_candidate_v2(self._base, binding=binding)


def bind_candidate_decision_writer_v2(
    base: object,
) -> CandidateDecisionWriterV2:
    if type(base) is not memory_service.CandidateDecisionWriter:
        raise memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError(
            "candidate_decision_configuration_invalid"
        )
    return CandidateDecisionWriterV2(base)


def _verifier_for(store: memory_store.MemoryStore):
    try:
        return memory_candidate_integrity_v2.AutomaticCandidateIntegrityVerifierV2(
            fingerprint_key_id=store._runtime_policy.fingerprint_key_id,
            fingerprint_hmac_secret=store._runtime_policy.fingerprint_hmac_secret,
            max_item_chars=store._runtime_policy.max_item_chars,
        )
    except memory_candidate_integrity.AutomaticCandidateIntegrityError:
        raise memory_store.MemoryStoreError(
            "candidate_decision_configuration_invalid"
        ) from None


def _translate_store_error(error: memory_store.MemoryStoreError) -> None:
    aliases = {
        "feature_disabled": "candidate_decisions_disabled",
        "memory_configuration_invalid": "candidate_decision_configuration_invalid",
    }
    category = aliases.get(error.category, error.category)
    if category not in _SAFE_ERROR_CATEGORIES:
        category = "candidate_decision_state_invalid"
    raise memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError(
        category
    ) from None


def _translate_integrity_error(
    error: memory_candidate_integrity.AutomaticCandidateIntegrityError,
) -> None:
    category = {
        "candidate_integrity_profile_mismatch": "candidate_decision_profile_mismatch",
        "candidate_integrity_schema_invalid": "candidate_decision_schema_invalid",
        "storage_unavailable": "storage_unavailable",
    }.get(error.category, "candidate_decision_state_invalid")
    raise memory_store.MemoryStoreError(category) from None


def decide_memory_candidate_v2(
    base: object,
    *,
    binding: memory_candidate_decision_ledger.CandidateDecisionLedgerBindingV1,
) -> memory_candidate_decision_ledger.CandidateDecisionResultV1:
    """Apply/replay one terminal decision after V2-aware evidence proof."""

    if type(base) is not memory_service.CandidateDecisionWriter:
        raise memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError(
            "candidate_decision_configuration_invalid"
        )
    store = base._store
    if type(store) is not memory_store.MemoryStore:
        raise memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError(
            "candidate_decision_configuration_invalid"
        )

    try:
        with channel_store.connect(store.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                store._require_candidate_decision_runtime()
                verifier = _verifier_for(store)
                try:
                    channel_store.validate_memory_candidate_decision_schema_v1_v10(
                        conn
                    )
                except (sqlite3.Error, TypeError, ValueError):
                    raise memory_store.MemoryStoreError(
                        "candidate_decision_schema_invalid"
                    ) from None
                valid_binding = memory_candidate_decision_ledger.validate_binding(
                    binding
                )
                existing = memory_candidate_decision_ledger.lookup_request(
                    conn,
                    valid_binding.request_id,
                )
                replay_ledger = (
                    memory_candidate_decision_ledger.validate_replay_binding(
                        existing,
                        valid_binding,
                    )
                    if existing is not None
                    else None
                )
                try:
                    verifier.verify_profile(conn)
                except memory_candidate_integrity.AutomaticCandidateIntegrityError as error:
                    _translate_integrity_error(error)

                if replay_ledger is not None:
                    store._verify_terminal_decision_replay(
                        conn,
                        binding=valid_binding,
                        ledger=replay_ledger,
                        verifier=verifier,
                    )
                    result = memory_candidate_decision_ledger.CandidateDecisionResultV1(
                        valid_binding,
                        replayed=True,
                    )
                    conn.execute("COMMIT")
                    return result

                row = conn.execute(
                    f"""SELECT {memory_candidate_integrity.AUTOMATIC_MEMORY_COLUMNS}
                          FROM memory_items WHERE memory_key=?""",
                    (valid_binding.candidate_key,),
                ).fetchone()
                if row is None or row["status"] != "candidate":
                    raise memory_store.MemoryStoreError("candidate_not_pending")
                try:
                    pending = verifier.verify_pending_candidate(conn, row)
                except memory_candidate_integrity.AutomaticCandidateIntegrityError as error:
                    _translate_integrity_error(error)

                if store._matching_suppression_ids(
                    conn,
                    scope_type=pending.scope_type,
                    scope_ref=pending.scope_ref,
                    kind=pending.kind,
                    fingerprint_version=pending.fingerprint_version,
                    fingerprint=pending.fingerprint,
                ):
                    raise memory_store.MemoryStoreError(
                        "candidate_decision_state_invalid"
                    )

                stamp = channel_store.now_iso()
                suppression_id = None
                if valid_binding.decision == "approve":
                    cursor = conn.execute(
                        """UPDATE memory_items
                              SET status='active',confidence=1.0,
                                  last_confirmed_at=?,updated_at=?
                            WHERE id=? AND status='candidate'""",
                        (stamp, stamp, pending.memory_id),
                    )
                else:
                    suppression_id = store._insert_candidate_rejection_suppression(
                        conn,
                        memory=pending,
                        stamp=stamp,
                    )
                    cursor = conn.execute(
                        """UPDATE memory_items
                              SET status='rejected',updated_at=?
                            WHERE id=? AND status='candidate'""",
                        (stamp, pending.memory_id),
                    )
                if cursor.rowcount != 1:
                    raise memory_store.MemoryStoreError(
                        "candidate_decision_conflict"
                    )

                store._before_candidate_decision_ledger_insert(
                    conn,
                    valid_binding,
                    pending,
                    suppression_id,
                )
                ledger = memory_candidate_decision_ledger._insert_terminal_decision(
                    conn,
                    binding=valid_binding,
                    memory_id=pending.memory_id,
                    suppression_id=suppression_id,
                    created_at=stamp,
                )
                terminal = store._verify_terminal_decision_replay(
                    conn,
                    binding=valid_binding,
                    ledger=ledger,
                    verifier=verifier,
                )
                if not store._same_verified_automatic_identity(
                    pending,
                    terminal,
                ):
                    raise memory_store.MemoryStoreError(
                        "candidate_decision_state_invalid"
                    )
                if valid_binding.decision == "approve":
                    if (
                        terminal.confidence != 1.0
                        or terminal.last_confirmed_at != stamp
                        or terminal.updated_at != stamp
                    ):
                        raise memory_store.MemoryStoreError(
                            "candidate_decision_state_invalid"
                        )
                elif (
                    terminal.confidence != 0.0
                    or terminal.last_confirmed_at != pending.last_confirmed_at
                    or terminal.updated_at != stamp
                ):
                    raise memory_store.MemoryStoreError(
                        "candidate_decision_state_invalid"
                    )

                result = memory_candidate_decision_ledger.CandidateDecisionResultV1(
                    valid_binding,
                    replayed=False,
                )
                conn.execute("COMMIT")
                return result
            except BaseException:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
    except memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError:
        raise
    except memory_store.MemoryStoreError as error:
        _translate_store_error(error)
    except sqlite3.IntegrityError:
        raise memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError(
            "candidate_decision_conflict"
        ) from None
    except (OSError, sqlite3.Error):
        raise memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError(
            "storage_unavailable"
        ) from None
    except Exception:
        raise memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError(
            "candidate_decision_state_invalid"
        ) from None
    raise AssertionError("unreachable")
