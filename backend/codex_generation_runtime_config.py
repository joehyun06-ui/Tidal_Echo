"""P2-B runtime configuration for the shared Codex generation process.

No module import creates directories or opens the generation database. Callers must
explicitly prepare paths only after the generation gate is enabled.
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import deployment_config
from .codex_app_server_shared_transport import CodexSharedTransportConfig
from .codex_generation_protocol import CodexGenerationConfig, CodexGenerationError


@dataclass(frozen=True)
class CodexGenerationRuntimeConfig:
    generation: CodexGenerationConfig
    store_path: Path
    poll_interval_seconds: float
    persistent_root: Path

    @property
    def enabled(self) -> bool:
        return self.generation.enabled


def load_generation_runtime_config(
    environ: Mapping[str, str] | None = None,
    *,
    persistent_root: Path = Path("/var/data"),
    relay_db: Path | None = None,
) -> CodexGenerationRuntimeConfig:
    env = os.environ if environ is None else environ
    generation = CodexGenerationConfig.from_environ(
        env, persistent_root=persistent_root
    )
    raw_store = str(
        env.get("CODEX_GENERATION_DB", persistent_root / "codex-generation.db")
    )
    store_path = Path(raw_store)
    if not store_path.is_absolute() or ".." in store_path.parts:
        raise CodexGenerationError("invalid_codex_generation_store_path")
    if relay_db is not None:
        try:
            if store_path.resolve(strict=False) == relay_db.resolve(strict=False):
                raise CodexGenerationError("codex_generation_store_must_be_separate")
        except CodexGenerationError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise CodexGenerationError("invalid_codex_generation_store_path") from None
    if not deployment_config.path_within_root(store_path, persistent_root):
        raise CodexGenerationError("invalid_codex_generation_store_path")
    if not deployment_config.path_within_root(generation.workspace_root, persistent_root):
        raise CodexGenerationError("invalid_codex_generation_workspace")
    raw_poll = str(env.get("CODEX_GENERATION_POLL_SECONDS", "0.25"))
    try:
        poll = float(raw_poll)
    except (TypeError, ValueError):
        raise CodexGenerationError("invalid_codex_generation_poll_seconds") from None
    if (
        not raw_poll
        or raw_poll != raw_poll.strip()
        or not raw_poll.isascii()
        or not math.isfinite(poll)
        or poll < 0.05
        or poll > 5.0
    ):
        raise CodexGenerationError("invalid_codex_generation_poll_seconds")
    return CodexGenerationRuntimeConfig(
        generation=generation,
        store_path=store_path,
        poll_interval_seconds=poll,
        persistent_root=persistent_root,
    )


def compose_shared_transport_config(
    control: deployment_config.CodexControlConfig,
    generation: CodexGenerationRuntimeConfig,
) -> CodexSharedTransportConfig:
    return CodexSharedTransportConfig(
        enabled=bool(control.enabled or generation.enabled),
        codex_home=control.codex_home,
        workspace=control.workspace,
        request_timeout_seconds=control.request_timeout_seconds,
    )


def _probe_writable(directory: Path) -> None:
    fd = -1
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(prefix=".codex-generation-write-probe-", dir=str(directory))
        os.write(fd, b"ok")
        os.fsync(fd)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def prepare_generation_paths(
    config: CodexGenerationRuntimeConfig,
    control: deployment_config.CodexControlConfig,
) -> None:
    """Prepare shared-process and generation paths only when generation is enabled."""
    if not config.enabled:
        return
    directories = {
        config.persistent_root,
        control.codex_home,
        control.workspace,
        config.generation.workspace_root,
        config.store_path.parent,
    }
    try:
        config.persistent_root.mkdir(parents=True, exist_ok=True)
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            same_root = directory.resolve(strict=False) == config.persistent_root.resolve(strict=False)
            if not same_root and not deployment_config.path_within_root(
                directory, config.persistent_root
            ):
                raise CodexGenerationError("invalid_codex_generation_persistent_path")
            _probe_writable(directory)
    except CodexGenerationError:
        raise
    except OSError:
        raise CodexGenerationError("codex_generation_persistent_path_unavailable") from None
