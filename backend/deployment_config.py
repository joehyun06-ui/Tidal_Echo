"""Fail-fast deployment validation shared by the relay and Render supervisor."""

from __future__ import annotations

import json
import math
import os
import tempfile
import urllib.parse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
LOOP_CONFIG_MAX_BYTES = 1024 * 1024
DEFAULT_PERSONA = (
    "You are the user's private AI companion in a one-to-one chat. "
    "Reply naturally, warmly, and concisely unless the user asks for detail."
)


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


def parse_bounded_int(value: object, minimum: int, maximum: int, category: str) -> int:
    raw = str(value if value is not None else "")
    if not raw or raw != raw.strip() or not raw.isascii() or not raw.isdecimal():
        raise DeploymentConfigError(category)
    result = int(raw, 10)
    if result < minimum or result > maximum:
        raise DeploymentConfigError(category)
    return result


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
class KelivoProviderDefaults:
    provider_model: str
    temperature: float
    max_tokens: int


@dataclass(frozen=True)
class KelivoConfig:
    enabled: bool
    api_key: str
    client_id: str
    api_session: str
    model_alias: str
    global_concurrency: int
    client_concurrency: int
    rate_limit_per_minute: int
    queue_timeout_seconds: float
    stale_dispatch_seconds: int
    completion_commit_margin_seconds: float
    internal_response_max_bytes: int
    allow_session_remap: bool
    require_telegram_session: bool
    auto_idempotency_enabled: bool
    auto_idempotency_replay_seconds: int


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
    sqlite_busy_timeout_seconds: float
    kelivo: KelivoConfig


def load_server_persona(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Load the server persona once and return exact text plus a non-sensitive source marker."""
    env = os.environ if environ is None else environ
    direct = str(env.get("PERSONA", "")).strip()
    if direct:
        return direct, "environment"
    persona_file = str(env.get("PERSONA_FILE", "")).strip()
    if persona_file:
        try:
            loaded = Path(persona_file).read_text(encoding="utf-8").strip()
        except OSError:
            loaded = ""
        if loaded:
            return loaded, "file"
    return DEFAULT_PERSONA, "default"


def resolve_kelivo_provider_contract_defaults(
    environ: Mapping[str, str] | None = None, loop_config: str | Path | None = None,
) -> KelivoProviderDefaults:
    """Resolve the one primary model and concrete generation defaults used by Kelivo."""
    env = os.environ if environ is None else environ
    model = str(env.get("LLM_MODEL", ""))
    config_path = Path(loop_config) if loop_config is not None else Path(
        str(env.get("LOOP_CONFIG", Path(__file__).parent.parent / "examples" / "api_loop.config.json"))
    )
    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            validate_loop_config_payload(payload, render_mvp=False)
        except (OSError, UnicodeError, json.JSONDecodeError, DeploymentConfigError):
            raise DeploymentConfigError("invalid_kelivo_provider_contract") from None
        chain = payload.get("main_chain")
        if isinstance(chain, list) and chain:
            model = chain[0]["model"]
    if not model or model != model.strip():
        raise DeploymentConfigError("kelivo_provider_model_missing")

    raw_temperature = str(env.get("LLM_TEMPERATURE", "0.7"))
    try:
        temperature = float(raw_temperature)
    except (TypeError, ValueError):
        raise DeploymentConfigError("invalid_kelivo_default_temperature") from None
    if (
        not raw_temperature or raw_temperature != raw_temperature.strip()
        or not raw_temperature.isascii() or not math.isfinite(temperature)
        or temperature < 0 or temperature > 2
    ):
        raise DeploymentConfigError("invalid_kelivo_default_temperature")
    if temperature == 0:
        temperature = 0.0
    max_tokens = parse_bounded_int(
        env.get("LLM_MAX_TOKENS", "2000"), 1, 32768, "invalid_kelivo_default_max_tokens"
    )
    return KelivoProviderDefaults(model, temperature, max_tokens)


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
    kelivo_enabled = parse_strict_bool(env.get("KELIVO_ENABLED", "false"), "invalid_kelivo_enabled")
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

    kelivo_key = str(env.get("KELIVO_API_KEY", "")).strip()
    kelivo_client_id = str(env.get("KELIVO_CLIENT_ID", "primary-kelivo")).strip()
    kelivo_api_session = str(env.get("KELIVO_API_SESSION", "")).strip()
    kelivo_model_alias = str(env.get("KELIVO_MODEL_ALIAS", "ouou-home")).strip()
    kelivo_allow_remap = parse_strict_bool(
        env.get("KELIVO_ALLOW_SESSION_REMAP", "false"), "invalid_kelivo_allow_session_remap"
    )
    kelivo_require_telegram = parse_strict_bool(
        env.get("KELIVO_REQUIRE_TELEGRAM_SESSION", "false"), "invalid_kelivo_require_telegram_session"
    )
    if kelivo_enabled:
        kelivo_auto_idempotency = parse_strict_bool(
            env.get("KELIVO_AUTO_IDEMPOTENCY_ENABLED", "false"),
            "invalid_kelivo_auto_idempotency_enabled",
        )
        kelivo_auto_replay_seconds = parse_bounded_int(
            env.get("KELIVO_AUTO_IDEMPOTENCY_REPLAY_SECONDS", "300"), 60, 3600,
            "invalid_kelivo_auto_idempotency_replay_seconds",
        )
    else:
        # Disabled Kelivo deployments ignore compatibility-only configuration.
        kelivo_auto_idempotency = False
        kelivo_auto_replay_seconds = 300
    kelivo_global_concurrency = parse_bounded_int(
        env.get("KELIVO_GLOBAL_CONCURRENCY", "2"), 1, 32, "invalid_kelivo_global_concurrency"
    )
    kelivo_client_concurrency = parse_bounded_int(
        env.get("KELIVO_CLIENT_CONCURRENCY", "1"), 1, 16, "invalid_kelivo_client_concurrency"
    )
    if kelivo_enabled and kelivo_client_concurrency > kelivo_global_concurrency:
        raise DeploymentConfigError("invalid_kelivo_concurrency_relationship")
    kelivo_rate_limit = parse_bounded_int(
        env.get("KELIVO_RATE_LIMIT_PER_MINUTE", "10"), 1, 600, "invalid_kelivo_rate_limit"
    )
    kelivo_queue_timeout = parse_positive_finite_float(
        env.get("KELIVO_QUEUE_TIMEOUT_SECONDS", "2"), "invalid_kelivo_queue_timeout"
    )
    if kelivo_queue_timeout > 60:
        raise DeploymentConfigError("invalid_kelivo_queue_timeout")
    kelivo_stale_dispatch = parse_bounded_int(
        env.get("KELIVO_DISPATCH_STALE_SECONDS", "300"), 30, 86400, "invalid_kelivo_stale_dispatch"
    )
    sqlite_busy_timeout = parse_positive_finite_float(
        env.get("SQLITE_BUSY_TIMEOUT_SECONDS", "30"), "invalid_sqlite_busy_timeout"
    )
    if sqlite_busy_timeout > 300:
        raise DeploymentConfigError("invalid_sqlite_busy_timeout")
    kelivo_completion_margin = parse_positive_finite_float(
        env.get("KELIVO_COMPLETION_COMMIT_MARGIN_SECONDS", "15"),
        "invalid_kelivo_completion_commit_margin",
    )
    if kelivo_completion_margin > 300:
        raise DeploymentConfigError("invalid_kelivo_completion_commit_margin")
    kelivo_internal_response_max = parse_bounded_int(
        env.get("KELIVO_INTERNAL_RESPONSE_MAX_BYTES", "1048576"), 4096, 8 * 1024 * 1024,
        "invalid_kelivo_internal_response_max_bytes",
    )
    safe_identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
    if kelivo_enabled:
        if len(kelivo_key) < 32 or len(kelivo_key) > 512:
            raise DeploymentConfigError("kelivo_api_key_missing")
        if any(safe_identifier.fullmatch(item) is None for item in
               (kelivo_client_id, kelivo_api_session, kelivo_model_alias)):
            raise DeploymentConfigError("kelivo_identity_invalid")
        protected = {
            str(env.get("RELAY_SECRET", "")).strip(),
            str(env.get("TELEGRAM_WEBHOOK_SECRET", "")).strip(),
            str(env.get("CHANNEL_AUDIT_HMAC_SECRET", "")).strip(),
            str(env.get("LLM_API_KEY", "")).strip(),
        }
        protected.discard("")
        if kelivo_key in protected:
            raise DeploymentConfigError("kelivo_api_key_must_be_distinct")

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
    if kelivo_enabled and kelivo_stale_dispatch <= (
        timeouts.model_total + kelivo_queue_timeout + sqlite_busy_timeout + kelivo_completion_margin
    ):
        raise DeploymentConfigError("invalid_kelivo_stale_dispatch_relationship")
    if kelivo_enabled and kelivo_auto_idempotency and kelivo_auto_replay_seconds <= kelivo_stale_dispatch:
        raise DeploymentConfigError("invalid_kelivo_auto_idempotency_replay_window")

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

    if kelivo_enabled:
        resolve_kelivo_provider_contract_defaults(env, loop_config)

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
        sqlite_busy_timeout_seconds=sqlite_busy_timeout,
        kelivo=KelivoConfig(
            enabled=kelivo_enabled,
            api_key=kelivo_key,
            client_id=kelivo_client_id,
            api_session=kelivo_api_session,
            model_alias=kelivo_model_alias,
            global_concurrency=kelivo_global_concurrency,
            client_concurrency=kelivo_client_concurrency,
            rate_limit_per_minute=kelivo_rate_limit,
            queue_timeout_seconds=kelivo_queue_timeout,
            stale_dispatch_seconds=kelivo_stale_dispatch,
            completion_commit_margin_seconds=kelivo_completion_margin,
            internal_response_max_bytes=kelivo_internal_response_max,
            allow_session_remap=kelivo_allow_remap,
            require_telegram_session=kelivo_require_telegram,
            auto_idempotency_enabled=kelivo_auto_idempotency,
            auto_idempotency_replay_seconds=kelivo_auto_replay_seconds,
        ),
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
