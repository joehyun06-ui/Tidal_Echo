"""V2-aware proof for automatic Memory candidates with multi-span evidence.

The deployed V1 verifier proves every candidate source row independently. Atomic
Formation V2 stores several immutable source rows for one candidate proposal, so
those rows must be grouped and rebuilt together before the stored Memory content
can be trusted.

This verifier subclasses the existing verifier and overrides only evidence
reconstruction. Item/profile/fingerprint/status semantics remain owned by the
reviewed V1 verifier. It accepts mixed V1/V2 provenance on the same pending
memory while requiring every evidence bundle to independently prove the exact
same stored content and fingerprint.
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from typing import Final

from backend import (
    memory_candidate_integrity,
    memory_formation,
    memory_formation_extractor_v2,
    memory_formation_v2,
    memory_policy,
)


FORMATION_CONTRACT_VERSION: Final = memory_formation_v2.FORMATION_CONTRACT_VERSION
EXTRACTOR_CONTRACT_VERSION: Final = (
    memory_formation_extractor_v2.EXTRACTOR_CONTRACT_VERSION
)


class AutomaticCandidateIntegrityVerifierV2(
    memory_candidate_integrity.AutomaticCandidateIntegrityVerifier
):
    """Existing candidate proof plus grouped Atomic Formation V2 evidence."""

    def __repr__(self) -> str:
        return "<AutomaticCandidateIntegrityVerifierV2>"

    @staticmethod
    def _validated_message(
        conn: sqlite3.Connection,
        canonical_message_id: int,
    ) -> str:
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
        ):
            memory_candidate_integrity._raise(
                "candidate_integrity_invalid"
            )
        return message["text"]

    def _verify_rebuilt_candidate(
        self,
        proof: object,
        *,
        canonical_message_id: int,
        signal_type: str,
        kind: str,
        content: str,
        fingerprint: bytes,
    ) -> None:
        if (
            type(proof)
            not in (
                memory_formation.AutoMemoryCandidateV1,
                memory_formation_v2.AutoMemoryCandidateV2,
            )
            or proof.source_message_id != canonical_message_id
            or proof.signal_type != signal_type
            or proof.kind != kind
            or proof.scope_type != memory_formation.SCOPE_TYPE
            or proof.scope_ref != memory_formation.SCOPE_REF
            or proof.sensitivity != memory_formation.SENSITIVITY
            or proof.normalized_content != content
        ):
            memory_candidate_integrity._raise(
                "candidate_integrity_invalid"
            )
        proof_fingerprint = memory_policy.fingerprint_content(
            self._fingerprint_hmac_secret,
            scope_type=proof.scope_type,
            scope_ref=proof.scope_ref,
            kind=proof.kind,
            normalized_content=proof.normalized_content,
        )
        if not memory_policy.secure_digest_equal(
            fingerprint,
            proof_fingerprint,
        ):
            memory_candidate_integrity._raise(
                "candidate_integrity_invalid"
            )

    @staticmethod
    def _evidence_from_row(
        row: sqlite3.Row,
        *,
        source_text: str,
    ) -> memory_candidate_integrity.VerifiedAutomaticEvidenceV1:
        start = row["span_start"]
        end = row["span_end"]
        excerpt = source_text[
            max(0, start - memory_candidate_integrity.EVIDENCE_CONTEXT_CHARS):
            min(
                len(source_text),
                end + memory_candidate_integrity.EVIDENCE_CONTEXT_CHARS,
            )
        ]
        if len(excerpt) > memory_candidate_integrity.MAX_SOURCE_EXCERPT_CHARS:
            memory_candidate_integrity._raise(
                "candidate_integrity_invalid"
            )
        return memory_candidate_integrity.VerifiedAutomaticEvidenceV1(
            source_id=row["id"],
            canonical_message_id=row["canonical_message_id"],
            signal_type=row["signal_type"],
            span_start=start,
            span_end=end,
            formation_contract_version=row["formation_contract_version"],
            extractor_contract_version=row["extractor_contract_version"],
            observed_at=row["created_at"],
            source_excerpt=excerpt,
        )

    def _verify_evidence(
        self,
        conn: sqlite3.Connection,
        *,
        memory_id: int,
        kind: str,
        content: str,
        fingerprint: bytes,
    ) -> tuple[memory_candidate_integrity.VerifiedAutomaticEvidenceV1, ...]:
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
                memory_candidate_integrity._raise(
                    "candidate_provenance_missing"
                )

            # Preserve the exact reviewed V1 path when no V2 evidence exists.
            if not any(
                row["formation_contract_version"] == FORMATION_CONTRACT_VERSION
                for row in rows
            ):
                return super()._verify_evidence(
                    conn,
                    memory_id=memory_id,
                    kind=kind,
                    content=content,
                    fingerprint=fingerprint,
                )

            messages: dict[int, str] = {}
            groups: OrderedDict[tuple[object, ...], list[sqlite3.Row]] = OrderedDict()

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
                    or signal_type
                    not in memory_candidate_integrity._AUTOMATIC_SIGNALS
                    or type(start) is not int
                    or type(end) is not int
                    or not 0 <= start < end
                    or type(formation_version) is not str
                    or memory_candidate_integrity._CONTRACT_VERSION.fullmatch(
                        formation_version
                    )
                    is None
                    or type(extractor_version) is not str
                    or memory_candidate_integrity._CONTRACT_VERSION.fullmatch(
                        extractor_version
                    )
                    is None
                    or memory_candidate_integrity._timestamp(observed_at) is None
                ):
                    memory_candidate_integrity._raise(
                        "candidate_integrity_invalid"
                    )

                source_text = messages.get(canonical_message_id)
                if source_text is None:
                    source_text = self._validated_message(
                        conn,
                        canonical_message_id,
                    )
                    messages[canonical_message_id] = source_text
                if (
                    end > len(source_text)
                    or end - start
                    > memory_formation.TOTAL_CANDIDATE_MAX_CHARS
                ):
                    memory_candidate_integrity._raise(
                        "candidate_integrity_invalid"
                    )

                if formation_version == FORMATION_CONTRACT_VERSION:
                    if extractor_version != EXTRACTOR_CONTRACT_VERSION:
                        memory_candidate_integrity._raise(
                            "candidate_integrity_invalid"
                        )
                    key = (
                        "v2",
                        canonical_message_id,
                        signal_type,
                        formation_version,
                        extractor_version,
                        observed_at,
                    )
                else:
                    # V1 historically accepted any syntactically valid contract
                    # labels and proved each source row independently. Keep that
                    # behavior byte-for-byte in the mixed path.
                    key = ("v1-row", source_id)
                groups.setdefault(key, []).append(row)

            for key, group_rows in groups.items():
                first = group_rows[0]
                canonical_message_id = first["canonical_message_id"]
                signal_type = first["signal_type"]
                source_text = messages[canonical_message_id]
                if key[0] == "v2":
                    spans = tuple(
                        memory_formation_v2.AutoMemorySourceSpanV2(
                            row["span_start"],
                            row["span_end"],
                        )
                        for row in group_rows
                    )
                    proposal = memory_formation_v2.AutoMemoryProposalV2(
                        signal_type,
                        spans,
                    )
                    try:
                        rebuilt = (
                            memory_formation_v2.build_auto_memory_candidates_v2(
                                canonical_message_id,
                                source_text,
                                (proposal,),
                                max_item_chars=self._max_item_chars,
                            )
                        )
                    except memory_formation_v2.MemoryFormationV2Error:
                        memory_candidate_integrity._raise(
                            "candidate_integrity_invalid"
                        )
                else:
                    row = first
                    proposal = memory_formation.AutoMemoryProposalV1(
                        signal_type=signal_type,
                        start=row["span_start"],
                        end=row["span_end"],
                    )
                    try:
                        rebuilt = memory_formation.build_auto_memory_candidates(
                            canonical_message_id,
                            source_text,
                            (proposal,),
                            max_item_chars=self._max_item_chars,
                        )
                    except memory_formation.MemoryFormationError:
                        memory_candidate_integrity._raise(
                            "candidate_integrity_invalid"
                        )
                if len(rebuilt) != 1:
                    memory_candidate_integrity._raise(
                        "candidate_integrity_invalid"
                    )
                self._verify_rebuilt_candidate(
                    rebuilt[0],
                    canonical_message_id=canonical_message_id,
                    signal_type=signal_type,
                    kind=kind,
                    content=content,
                    fingerprint=fingerprint,
                )

            evidence: list[
                memory_candidate_integrity.VerifiedAutomaticEvidenceV1
            ] = []
            for row in rows:
                evidence.append(self._evidence_from_row(
                    row,
                    source_text=messages[row["canonical_message_id"]],
                ))
            return tuple(evidence)
        except memory_candidate_integrity.AutomaticCandidateIntegrityError:
            raise
        except (OSError, sqlite3.Error):
            memory_candidate_integrity._raise("storage_unavailable")
        except (IndexError, KeyError, TypeError, ValueError):
            memory_candidate_integrity._raise(
                "candidate_integrity_invalid"
            )
        raise AssertionError("unreachable")
