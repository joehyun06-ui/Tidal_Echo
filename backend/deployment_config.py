"""Fail-fast deployment validation shared by the relay and Render supervisor."""

from __future__ import annotations

import json
import math
import os
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
LOOP_CONFIG_MAX_BYTES = 1024 * 1024


class DeploymentConfigError(ValueError):
    """A fixed-category startup error that is safe to include in logs."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def parse_strict_bool(value: object, category: str) -> bool:
    raw = str(value if value is not None else "")
    if not raw or raw != raw.strip() or not raw.isascii():
        raise DeploymentConfigError(category)
    normalized = raw.lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise DeploymentConfigError(category)


def parse_positive_finite_float(value: object, category: str) -> float:
    raw = str(value if value is not None else "")
    if not raw or raw != raw.strip() or not raw.isascii():
        raise DeploymentConfigError(category)
    try:
        result = float(raw)
    except (TypeError, ValueError):
        raise DeploymentConfigError(category) from None
    if not math.isfinite(result) or result <= 0:
        raise DeploymentConfigError(category)
    return result


def validate_loop_timeouts(model_total: float, callback: float, margin: float, dispatch: float) -> None:
    if not all(math.isfinite(value) and value > 0 for value in (model_total, callback, margin, dispatch)):
        raise DeploymentConfigError("invalid_loop_timeout")
    if dispatch < model_total + callback + margin:
        raise DeploymentConfigError("invalid_loop_timeout_relationship")


def parse_port(value: object, category: str) -> int:
    raw = str(value if value is not None else "")
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise DeploymentConfigError(category)
    port = int(raw, 10)
    if port < 1 or port > 65535:
        raise DeploymentConfigError(category)
    return port


def path_within_root(path: str | Path, root: str | Path) -> bool:
    try:
        raw_candidate = Path(path).expanduser()
        raw_root = Path(root).expanduser()
        if not raw_candidate.is_absolute() or not raw_root.is_absolute():
            return False
        if ".." in raw_candidate.parts or ".." in raw_root.parts:
            return False
        for item in (raw_root, raw_candidate, *raw_candidate.parents):
            if item.exists() and item.is_symlink():
                return False
        candidate = raw_candidate.resolve(strict=False)
        expected = raw_root.resolve(strict=False)
        return candidate != expected and expected in candidate.parents
    except (OSError, RuntimeError, ValueError):
        return False


@dataclass(frozen=True)
class LoopTimeouts:
    model_total: float
    callback: float
    safety_margin: float
    dispatch: float


@dataclass(frozen=True)
class DeploymentConfig:
    render_telegram_mvp: bool
    persistent_root: Path
    db_path: Path
    upload_dir: Path
    brain_file: Path
    brain_target: str
    loop_config: Path
    loop_port: int
    timeouts: LoopTimeouts


def validate_loop_config_payload(payload: object, *, render_mvp: bool) -> dict:
    if not isinstance(payload, dict):
        raise DeploymentConfigError("invalid_loop_config_structure")
    if "main_chain" not in payload:
        return payload
    chain = payload["main_chain"]
    if not isinstance(chain, list):
        raise DeploymentConfigError("invalid_loop_config_structure")
    if render_mvp and len(chain) > 1:
        raise DeploymentConfigError("model_fallback_not_allowed")
    for item in chain:
        if not isinstance(item, dict) or set(item) != {"url", "key", "model"}:
            raise DeploymentConfigError("invalid_loop_model_route")
        if any(not isinstance(item[name], str) or not item[name].strip() for name in ("url", "key", "model")):
            raise DeploymentConfigError("invalid_loop_model_route")
    return payload


def validate_loop_config_update_request(payload: object, *, render_mvp: bool) -> dict:
    if not isinstance(payload, dict):
        raise DeploymentConfigError("invalid_loop_config_structure")
    if render_mvp and "history_n" in payload and (
        not isinstance(payload["history_n"], int) or isinstance(payload["history_n"], bool)
    ):
        raise DeploymentConfigError("invalid_loop_config_structure")
    if not render_mvp or "main_chain" not in payload:
        return payload
    chain = payload["main_chain"]
    if not isinstance(chain, list):
        raise DeploymentConfigError("invalid_loop_config_structure")
    if len(chain) != 1:
        raise DeploymentConfigError("model_fallback_not_allowed")
    item = chain[0]
    allowed = {"index", "model", "url", "key"}
    required = {"model", "url", "key"}
    if not isinstance(item, dict) or not required.issubset(item) or not set(item).issubset(allowed):
        raise DeploymentConfigError("invalid_loop_model_route")
    if "index" in item and (not isinstance(item["index"], int) or isinstance(item["index"], bool)):
        raise DeploymentConfigError("invalid_loop_model_route")
    if any(not isinstance(item[name], str) or not item[name].strip() for name in required):
        raise DeploymentConfigError("invalid_loop_model_route")
    return payload


def validate_loop_config_file(path: Path, *, render_mvp: bool) -> None:
    if not path.exists():
        return
    try:
        size = path.stat().st_size
        if size <= 0 or size > LOOP_CONFIG_MAX_BYTES:
            raise DeploymentConfigError("invalid_loop_config_size")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DeploymentConfigError("invalid_loop_config") from None
    validate_loop_config_payload(payload, render_mvp=render_mvp)


def load_deployment_config(
    telegram_config,
    environ: Mapping[str, str] | None = None,
) -> DeploymentConfig:
    env = os.environ if environ is None else environ
    render_mvp = parse_strict_bool(env.get("RENDER_TELEGRAM_MVP", "false"), "invalid_render_telegram_mvp")
    telegram_enabled = parse_strict_bool(env.get("TELEGRAM_ENABLED", "false"), "invalid_telegram_enabled")
    telegram_test_mode = parse_strict_bool(env.get("TELEGRAM_TEST_MODE", "false"), "invalid_telegram_test_mode")
    if telegram_enabled != bool(telegram_config.requested):
        raise DeploymentConfigError("telegram_config_invalid")

    if telegram_config.requested:
        required = (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_WEBHOOK_SECRET",
            "CHANNEL_AUDIT_HMAC_SECRET",
            "TELEGRAM_BOT_ACCOUNT_ID",
            "TELEGRAM_ALLOWED_USER_IDS",
            "TELEGRAM_ALLOWED_CHAT_IDS",
            "RELAY_SECRET",
        )
        if any(not str(env.get(name, "")).strip() for name in required):
            raise DeploymentConfigError("telegram_config_incomplete")
        if not telegram_config.enabled:
            raise DeploymentConfigError("telegram_config_invalid")
        secrets = (
            str(env.get("TELEGRAM_WEBHOOK_SECRET", "")).strip(),
            str(env.get("CHANNEL_AUDIT_HMAC_SECRET", "")).strip(),
            str(env.get("RELAY_SECRET", "")).strip(),
        )
        if len(set(secrets)) != len(secrets):
            raise DeploymentConfigError("telegram_secrets_must_be_distinct")

    try:
        timeouts = LoopTimeouts(
            model_total=parse_positive_finite_float(env.get("LOOP_MODEL_TOTAL_TIMEOUT_SECONDS", "120"), "invalid_loop_timeout"),
            callback=parse_positive_finite_float(env.get("LOOP_CALLBACK_TIMEOUT_SECONDS", "30"), "invalid_loop_timeout"),
            safety_margin=parse_positive_finite_float(env.get("LOOP_TIMEOUT_SAFETY_MARGIN_SECONDS", "15"), "invalid_loop_timeout"),
            dispatch=parse_positive_finite_float(env.get("LOOP_DISPATCH_TIMEOUT_SECONDS", "180"), "invalid_loop_timeout"),
        )
    except DeploymentConfigError:
        raise DeploymentConfigError("invalid_loop_timeout") from None
    validate_loop_timeouts(timeouts.model_total, timeouts.callback, timeouts.safety_margin, timeouts.dispatch)

    persistent_root = Path(str(env.get("RENDER_PERSISTENT_ROOT", "/var/data"))).expanduser()
    db_path = Path(str(env.get("RELAY_DB", Path(__file__).parent / "relay.db"))).expanduser()
    upload_dir = Path(str(env.get("RELAY_UPLOAD_DIR", Path(__file__).parent / "uploads"))).expanduser()
    brain_file = Path(str(env.get("RELAY_BRAIN_FILE", Path(__file__).parent / "brain_target"))).expanduser()
    loop_config = Path(str(env.get("LOOP_CONFIG", Path(__file__).parent.parent / "examples" / "api_loop.config.json"))).expanduser()
    brain_target = str(env.get("RELAY_BRAIN_TARGET", "")).strip()
    if brain_target and brain_target != "loop":
        raise DeploymentConfigError("invalid_brain_target")

    loop_port = parse_port(env.get("LOOP_PORT", "3020"), "invalid_loop_port")

    if render_mvp:
        if not telegram_config.requested or not telegram_config.enabled:
            raise DeploymentConfigError("telegram_must_be_enabled")
        if telegram_test_mode:
            raise DeploymentConfigError("telegram_test_mode_not_allowed")
        if brain_target != "loop":
            raise DeploymentConfigError("brain_target_loop_required")
        ingest_url = str(env.get("RELAY_LOOP_INGEST_URL", "")).strip()
        expected_ingest = f"http://127.0.0.1:{loop_port}/loop/ingest"
        parsed_ingest = urllib.parse.urlsplit(ingest_url)
        if (
            ingest_url != expected_ingest
            or parsed_ingest.username
            or parsed_ingest.password
            or parsed_ingest.query
            or parsed_ingest.fragment
        ):
            raise DeploymentConfigError("invalid_loop_ingest_url")
        path_names = ("RELAY_DB", "RELAY_UPLOAD_DIR", "RELAY_BRAIN_FILE", "LOOP_CONFIG")
        if any(not str(env.get(name, "")).strip() for name in path_names):
            raise DeploymentConfigError("persistent_path_missing")
        if any(not path_within_root(str(env[name]), persistent_root) for name in path_names):
            raise DeploymentConfigError("persistent_path_outside_root")
        model_names = ("LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL")
        model_values = tuple(str(env.get(name, "")) for name in model_names)
        if any(not value.strip() or value != value.strip() for value in model_values):
            raise DeploymentConfigError("primary_model_config_incomplete")
        for suffix in ("_2", "_3", "_4"):
            if any(str(env.get(f"LLM_{field}{suffix}", "")) != "" for field in ("API_BASE", "API_KEY", "MODEL")):
                raise DeploymentConfigError("model_fallback_not_allowed")
        validate_loop_config_file(loop_config, render_mvp=True)

    return DeploymentConfig(
        render_telegram_mvp=render_mvp,
        persistent_root=persistent_root.resolve(strict=False),
        db_path=db_path.resolve(strict=False),
        upload_dir=upload_dir.resolve(strict=False),
        brain_file=brain_file.resolve(strict=False),
        brain_target=brain_target,
        loop_config=loop_config.resolve(strict=False),
        loop_port=loop_port,
        timeouts=timeouts,
    )


def _probe_directory_writable(path: Path) -> None:
    fd = -1
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(prefix=".render-write-probe-", dir=str(path))
        os.close(fd)
        fd = -1
        os.unlink(temporary)
        temporary = ""
    except OSError:
        raise DeploymentConfigError("persistent_directory_not_writable") from None
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


def prepare_persistent_paths(config: DeploymentConfig) -> None:
    if not config.render_telegram_mvp:
        return
    directories = {
        config.db_path.parent,
        config.upload_dir,
        config.brain_file.parent,
        config.loop_config.parent,
    }
    try:
        config.persistent_root.mkdir(parents=True, exist_ok=True)
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            same_as_root = directory.resolve(strict=False) == config.persistent_root.resolve(strict=False)
            if not same_as_root and not path_within_root(directory, config.persistent_root):
                raise DeploymentConfigError("persistent_path_outside_root")
            _probe_directory_writable(directory)
    except DeploymentConfigError:
        raise
    except OSError:
        raise DeploymentConfigError("persistent_directory_initialization_failed") from None


def _fsync_parent(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, text: str) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(parent), text=True)
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        _fsync_parent(parent)
    except Exception:
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
        raise


def initialize_brain_target(config: DeploymentConfig) -> None:
    """Atomically initialize an explicitly configured loop target."""
    if not config.brain_target:
        return
    if config.brain_target != "loop":
        raise DeploymentConfigError("invalid_brain_target")
    atomic_write_text(config.brain_file, "loop\n")
