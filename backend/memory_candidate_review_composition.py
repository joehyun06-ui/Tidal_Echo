"""Independent, read-only composition root for candidate review."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

try:
    from . import (
        deployment_config,
        memory_candidate_review,
        memory_candidate_review_adapters,
    )
except ImportError:  # support direct module execution in local tooling
    import deployment_config
    import memory_candidate_review
    import memory_candidate_review_adapters


@dataclass(frozen=True, slots=True, repr=False)
class MemoryCandidateReviewCapabilitiesV1:
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
        return "<MemoryCandidateReviewCapabilitiesV1>"


def _raise(category: str) -> None:
    raise memory_candidate_review.MemoryCandidateReviewError(category)


def compose_candidate_review_capabilities(
    deployment: deployment_config.DeploymentConfig,
) -> MemoryCandidateReviewCapabilitiesV1:
    """Compose review capabilities without creating or mutating storage."""

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
        reader = memory_candidate_review.MemoryCandidateReviewReader(
            database,
            fingerprint_key_id=config.fingerprint_key_id,
            fingerprint_hmac_secret=config.fingerprint_hmac_secret,
            max_item_chars=config.max_item_chars,
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
        operator_cli = memory_candidate_review_adapters.bind_operator_cli(service)
        mcp = memory_candidate_review_adapters.bind_mcp(service)
        return MemoryCandidateReviewCapabilitiesV1(
            service=service,
            operator_cli=operator_cli,
            mcp=mcp,
        )
    except memory_candidate_review.MemoryCandidateReviewError:
        raise
    except Exception:
        _raise("candidate_review_state_invalid")


def _load_deployment(
    telegram_config: object,
    environ: Mapping[str, str] | None,
) -> deployment_config.DeploymentConfig:
    try:
        return deployment_config.load_deployment_config(
            telegram_config,
            environ,
        )
    except deployment_config.DeploymentConfigError:
        _raise("candidate_review_configuration_invalid")
    except Exception:
        _raise("candidate_review_configuration_invalid")


def compose_operator_candidate_review_from_environment(
    telegram_config: object,
    environ: Mapping[str, str] | None = None,
) -> memory_candidate_review_adapters.MemoryCandidateReviewAdapter:
    deployment = _load_deployment(telegram_config, environ)
    return compose_candidate_review_capabilities(deployment).operator_cli


def compose_mcp_candidate_review_from_environment(
    telegram_config: object,
    environ: Mapping[str, str] | None = None,
) -> memory_candidate_review_adapters.MemoryCandidateReviewAdapter:
    deployment = _load_deployment(telegram_config, environ)
    return compose_candidate_review_capabilities(deployment).mcp
