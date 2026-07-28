"""Side-effect-free operator Memory preflight and reviewed composition root."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

try:
    from . import (
        channel_store,
        deployment_config,
        memory_explicit_actions,
        memory_runtime,
        memory_store,
    )
except ImportError:  # support direct module execution in local tooling
    import channel_store
    import deployment_config
    import memory_explicit_actions
    import memory_runtime
    import memory_store


_CATEGORY = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


def _category(value: object, fallback: str) -> str:
    if type(value) is str and _CATEGORY.fullmatch(value) is not None:
        return value
    return fallback


class MemoryOperatorCompositionError(RuntimeError):
    """A fixed, data-free operator composition failure."""

    def __init__(self, category: str):
        safe_category = _category(
            category,
            "memory_operator_composition_failed",
        )
        super().__init__(safe_category)
        self.category = safe_category

    def __repr__(self) -> str:
        return "<MemoryOperatorCompositionError>"


@dataclass(frozen=True, slots=True, repr=False)
class MemoryOperatorPreflightV1:
    ready: bool
    category: str

    def __post_init__(self) -> None:
        if type(self.ready) is not bool:
            raise TypeError("invalid_memory_operator_preflight")
        if (
            type(self.category) is not str
            or _CATEGORY.fullmatch(self.category) is None
        ):
            raise ValueError("invalid_memory_operator_preflight")

    def __repr__(self) -> str:
        return "<MemoryOperatorPreflightV1>"


def _result(ready: bool, category: str) -> MemoryOperatorPreflightV1:
    return MemoryOperatorPreflightV1(
        ready=ready,
        category=_category(
            category,
            "memory_operator_preflight_failed",
        ),
    )


def _preflight_operator_memory(
    deployment: deployment_config.DeploymentConfig,
) -> MemoryOperatorPreflightV1:
    if type(deployment) is not deployment_config.DeploymentConfig:
        return _result(False, "deployment_config_invalid")
    config = deployment.memory
    if not config.enabled:
        return _result(False, "memory_core_disabled")
    if not config.explicit_writes_enabled:
        return _result(False, "memory_explicit_writes_disabled")
    if not config.explicit_entry_enabled:
        return _result(False, "memory_explicit_entry_disabled")
    if not config.configuration_valid:
        return _result(
            False,
            config.error_category or "memory_configuration_invalid",
        )
    if not config.entry_configuration_valid:
        return _result(
            False,
            config.entry_error_category
            or "memory_explicit_entry_configuration_invalid",
        )
    database = Path(deployment.db_path)
    try:
        if not database.is_file():
            return _result(False, "memory_storage_missing")
    except OSError:
        return _result(False, "memory_storage_unavailable")
    try:
        with channel_store.connect_read_only(
            database,
            timeout_seconds=deployment.sqlite_busy_timeout_seconds,
        ) as conn:
            channel_store.validate_memory_operator_schema_v1_v8(conn)
            expected_profile = (
                memory_runtime.memory_fingerprint_profile_from_config(config)
            )
            memory_store.validate_memory_fingerprint_profile(
                conn,
                expected_profile=expected_profile,
            )
    except memory_runtime.MemoryRuntimeError as error:
        return _result(
            False,
            _category(error.category, "memory_configuration_invalid"),
        )
    except memory_store.MemoryStoreError:
        return _result(False, "memory_fingerprint_profile_mismatch")
    except (FileNotFoundError, OSError):
        return _result(False, "memory_storage_unavailable")
    except (sqlite3.Error, TypeError, ValueError):
        return _result(False, "memory_operator_schema_invalid")
    return _result(True, "ready")


def _load_deployment(
    telegram_config: object,
    environ: Mapping[str, str] | None,
) -> tuple[
    deployment_config.DeploymentConfig | None,
    MemoryOperatorPreflightV1 | None,
]:
    try:
        deployment = deployment_config.load_deployment_config(
            telegram_config,
            environ,
        )
    except deployment_config.DeploymentConfigError as error:
        return None, _result(
            False,
            _category(error.category, "deployment_configuration_invalid"),
        )
    except (OSError, TypeError, ValueError):
        return None, _result(False, "deployment_configuration_invalid")
    return deployment, None


def preflight_operator_memory_from_environment(
    telegram_config: object,
    environ: Mapping[str, str] | None = None,
) -> MemoryOperatorPreflightV1:
    """Load one frozen environment snapshot and perform a read-only preflight."""
    deployment, error = _load_deployment(telegram_config, environ)
    if error is not None:
        return error
    assert deployment is not None
    return _preflight_operator_memory(deployment)


def compose_operator_memory_service_from_environment(
    telegram_config: object,
    environ: Mapping[str, str] | None = None,
) -> memory_explicit_actions.ExplicitMemoryActionService:
    """Preflight before authority, then bind only the operator CLI service."""
    deployment, error = _load_deployment(telegram_config, environ)
    if error is not None:
        raise MemoryOperatorCompositionError(error.category)
    assert deployment is not None
    preflight = _preflight_operator_memory(deployment)
    if not preflight.ready:
        raise MemoryOperatorCompositionError(preflight.category)
    try:
        runtime = memory_runtime.bootstrap_memory_runtime(deployment)
        backend = memory_explicit_actions.create_entry_backend(
            runtime.privileged_actions
        )
        return memory_explicit_actions.bind_operator_cli(backend)
    except (
        memory_explicit_actions.ExplicitMemoryActionError,
        memory_runtime.MemoryRuntimeError,
    ) as error:
        raise MemoryOperatorCompositionError(
            _category(
                error.category,
                "memory_operator_composition_failed",
            )
        ) from None
