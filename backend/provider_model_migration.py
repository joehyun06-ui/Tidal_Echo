"""Server-side model-only migration for the primary Kelivo/API provider route.

This module exists so an authenticated operator can change only the provider
model identifier without ever round-tripping the existing provider key through
the browser. The current LOOP_CONFIG remains the sole authority: URL, key, and
all unrelated config fields are preserved, the complete candidate config is
revalidated, and writes use the repository's atomic text helper.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from backend import deployment_config


CONTRACT_VERSION: Final = 1
MAX_REQUEST_BYTES: Final = 1024
MAX_MODEL_CHARS: Final = 256

INVALID_REQUEST: Final = "provider_model_migration_invalid_request"
CONFIG_UNAVAILABLE: Final = "provider_model_migration_config_unavailable"
WRITE_FAILED: Final = "provider_model_migration_write_failed"

_ALLOWED_ERRORS: Final = frozenset({
    INVALID_REQUEST,
    CONFIG_UNAVAILABLE,
    WRITE_FAILED,
})


class ProviderModelMigrationError(RuntimeError):
    """Fixed, data-free failure for the model-only migration surface."""

    __slots__ = ("category", "status_code")

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ALLOWED_ERRORS
            else CONFIG_UNAVAILABLE
        )
        self.category = safe
        self.status_code = 400 if safe == INVALID_REQUEST else 503
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except BaseException:
            return CONFIG_UNAVAILABLE

    def __repr__(self) -> str:
        return f"ProviderModelMigrationError({str(self)!r})"


def _raise(category: str) -> None:
    raise ProviderModelMigrationError(category)


def decode_model_request_body(
    raw: object,
    *,
    content_length: object = None,
    content_encoding: object = "",
) -> object:
    """Decode one bounded JSON body without accepting compression or ambiguity."""

    encoding = str(content_encoding if content_encoding is not None else "").strip().lower()
    if encoding not in {"", "identity"}:
        _raise(INVALID_REQUEST)
    if content_length not in (None, ""):
        length_text = str(content_length)
        if (
            not length_text.isascii()
            or not length_text.isdecimal()
            or int(length_text) > MAX_REQUEST_BYTES
        ):
            _raise(INVALID_REQUEST)
    if type(raw) is not bytes or not raw or len(raw) > MAX_REQUEST_BYTES:
        _raise(INVALID_REQUEST)
    if content_length not in (None, "") and int(str(content_length)) != len(raw):
        _raise(INVALID_REQUEST)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        _raise(INVALID_REQUEST)


def validate_model_request(payload: object) -> str:
    """Accept exactly ``{"model": <non-empty string>}`` and nothing else."""

    if type(payload) is not dict or set(payload) != {"model"}:
        _raise(INVALID_REQUEST)
    model = payload.get("model")
    if (
        type(model) is not str
        or not model
        or model != model.strip()
        or len(model) > MAX_MODEL_CHARS
    ):
        _raise(INVALID_REQUEST)
    try:
        model.encode("utf-8", errors="strict")
    except UnicodeError:
        _raise(INVALID_REQUEST)
    if any(ord(char) < 32 or ord(char) == 127 for char in model):
        _raise(INVALID_REQUEST)
    return model


def _load_authoritative_config(path: Path) -> tuple[str, dict]:
    try:
        if (
            not path.is_absolute()
            or not path.exists()
            or not path.is_file()
            or path.is_symlink()
        ):
            _raise(CONFIG_UNAVAILABLE)
        size = path.stat().st_size
        if size <= 0 or size > deployment_config.LOOP_CONFIG_MAX_BYTES:
            _raise(CONFIG_UNAVAILABLE)
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        deployment_config.validate_loop_config_payload(payload, render_mvp=True)
        chain = payload.get("main_chain")
        if (
            not isinstance(chain, list)
            or len(chain) != 1
            or not isinstance(chain[0], dict)
            or set(chain[0]) != {"url", "key", "model"}
        ):
            _raise(CONFIG_UNAVAILABLE)
        return raw, payload
    except ProviderModelMigrationError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        deployment_config.DeploymentConfigError,
    ):
        _raise(CONFIG_UNAVAILABLE)


def _verified_projection(
    payload: object,
    *,
    model: str,
    original_route: dict,
    expected_payload: dict,
) -> None:
    try:
        deployment_config.validate_loop_config_payload(payload, render_mvp=True)
        if not isinstance(payload, dict) or payload != expected_payload:
            _raise(WRITE_FAILED)
        chain = payload.get("main_chain")
        if not isinstance(chain, list) or len(chain) != 1 or not isinstance(chain[0], dict):
            _raise(WRITE_FAILED)
        route = chain[0]
        if (
            route.get("model") != model
            or route.get("url") != original_route.get("url")
            or route.get("key") != original_route.get("key")
        ):
            _raise(WRITE_FAILED)
    except ProviderModelMigrationError:
        raise
    except BaseException:
        _raise(WRITE_FAILED)


def migrate_primary_provider_model(
    loop_config: str | Path,
    payload: object,
) -> dict[str, object]:
    """Atomically replace only ``main_chain[0].model`` in the authoritative config.

    The existing URL and key never leave the server. On post-write verification
    failure, the original file contents are restored atomically on a best-effort
    basis before returning a fixed error category.
    """

    model = validate_model_request(payload)
    path = Path(loop_config)
    original_raw, current = _load_authoritative_config(path)
    route = dict(current["main_chain"][0])
    if route["model"] == model:
        return {
            "ok": True,
            "contract_version": CONTRACT_VERSION,
            "changed": False,
            "model": model,
        }

    candidate = dict(current)
    candidate_route = dict(route)
    candidate_route["model"] = model
    candidate["main_chain"] = [candidate_route]
    try:
        deployment_config.validate_loop_config_payload(candidate, render_mvp=True)
        encoded = json.dumps(candidate, ensure_ascii=False, indent=2) + "\n"
        if (
            len(encoded.encode("utf-8", errors="strict"))
            > deployment_config.LOOP_CONFIG_MAX_BYTES
        ):
            _raise(WRITE_FAILED)
        deployment_config.atomic_write_text(path, encoded)
        verify_raw = path.read_text(encoding="utf-8")
        verify_payload = json.loads(verify_raw)
        _verified_projection(
            verify_payload,
            model=model,
            original_route=route,
            expected_payload=candidate,
        )
    except ProviderModelMigrationError:
        try:
            deployment_config.atomic_write_text(path, original_raw)
        except BaseException:
            pass
        raise
    except BaseException:
        try:
            deployment_config.atomic_write_text(path, original_raw)
        except BaseException:
            pass
        _raise(WRITE_FAILED)

    return {
        "ok": True,
        "contract_version": CONTRACT_VERSION,
        "changed": True,
        "model": model,
    }


__all__ = (
    "CONFIG_UNAVAILABLE",
    "CONTRACT_VERSION",
    "INVALID_REQUEST",
    "MAX_MODEL_CHARS",
    "MAX_REQUEST_BYTES",
    "ProviderModelMigrationError",
    "WRITE_FAILED",
    "decode_model_request_body",
    "migrate_primary_provider_model",
    "validate_model_request",
)
