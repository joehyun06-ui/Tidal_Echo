"""Unwired V2-aware composition for automatic Memory candidate review.

The public review contract and adapters remain V1-shaped. This module creates
an exact existing ``MemoryCandidateReviewReader`` and replaces only its private
proof engine with ``AutomaticCandidateIntegrityVerifierV2`` before the existing
service/adapters are bound. No write authority is introduced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from backend import (
    deployment_config,
    memory_candidate_integrity_v2,
    memory_candidate_review,
    memory_candidate_review_adapters,
)


@dataclass(frozen=True, slots=True, repr=False)
class MemoryCandidateReviewCapabilitiesV2:
    service: memory_candidate_review.MemoryCandidateReviewService = field(
        repr=False
    )
    operator_cli: memory_candidate_review_adapters.MemoryCandidateReviewAdapter = field(
        repr=False
    )
    mcp: memory_candidate_review_adapters.MemoryCandidateReviewAdapter = field(
        repr=False
    )

    def __repr__(self) -> str:
        return "<MemoryCandidateReviewCapabilitiesV2>"


def _raise(category: str) -> None:
    raise memory_candidate_review.MemoryCandidateReviewError(category)


def compose_candidate_review_capabilities_v2(
    deployment: deployment_config.DeploymentConfig,
) -> MemoryCandidateReviewCapabilitiesV2:
    """Compose read-only review with V2-aware evidence reconstruction."""

    if type(deployment) is not deployment_config.DeploymentConfig:
        _raise("candidate_review_configuration_invalid")
    config = deployment.memory
    if not config.enabled:
        _raise("candidate_review_configuration_invalid")
    if not config.candidate_review_enabled:
        _raise("candidate_review_disabled")
    if not config.configuration_valid:
        _raise("candidate_review_configuration_invalid")
    try:
        database = Path(deployment.db_path)
        if not database.is_file():
            _raise("storage_unavailable")
    except memory_candidate_review.MemoryCandidateReviewError:
        raise
    except (OSError, TypeError, ValueError):
        _raise("storage_unavailable")

    try:
        # Keep the exact existing reader/service types so all reviewed adapter
        # identity checks remain unchanged. Only the internal proof engine differs.
        reader = memory_candidate_review.MemoryCandidateReviewReader(
            database,
            fingerprint_key_id=config.fingerprint_key_id,
            fingerprint_hmac_secret=config.fingerprint_hmac_secret,
            max_item_chars=config.max_item_chars,
        )
        reader._verifier = (
            memory_candidate_integrity_v2.AutomaticCandidateIntegrityVerifierV2(
                fingerprint_key_id=config.fingerprint_key_id,
                fingerprint_hmac_secret=config.fingerprint_hmac_secret,
                max_item_chars=config.max_item_chars,
            )
        )
        service = memory_candidate_review.MemoryCandidateReviewService(
            reader,
            enabled=config.candidate_review_enabled,
            configuration_valid=config.configuration_valid,
            error_category=config.error_category,
        )
        ready, category = service.readiness()
        if not ready:
            _raise(category or "candidate_review_state_invalid")
        return MemoryCandidateReviewCapabilitiesV2(
            service=service,
            operator_cli=memory_candidate_review_adapters.bind_operator_cli(service),
            mcp=memory_candidate_review_adapters.bind_mcp(service),
        )
    except memory_candidate_review.MemoryCandidateReviewError:
        raise
    except Exception:
        _raise("candidate_review_state_invalid")
