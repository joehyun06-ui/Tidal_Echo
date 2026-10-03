"""Explicit browser origins shared by every relay/P3 entrypoint."""

from collections.abc import Mapping
import os
from urllib.parse import urlsplit

from .deployment_config import DeploymentConfigError, parse_strict_bool


LOCAL_DEV_ORIGINS = tuple(
    f"http://{host}:{port}"
    for host in ("localhost", "127.0.0.1")
    for port in (3000, 4173, 5173, 8080)
)


def _origins(raw: str) -> list[str]:
    result = []
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https", "capacitor"}
                and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not parsed.path and not parsed.query and not parsed.fragment
                and "*" not in value and not any(c.isspace() for c in value)
            )
            parsed.port  # Validate malformed/out-of-range ports too.
        except ValueError:
            valid = False
        if not valid:
            raise DeploymentConfigError("invalid_relay_cors_origin")
        if value not in result:
            result.append(value)
    return result


def allowed_origins(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    origins = _origins(env.get(
        "RELAY_ALLOW_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080"
    ))
    if parse_strict_bool(env.get("RELAY_CORS_DEV_ENABLED", "false"), "invalid_relay_cors_dev_enabled"):
        development = _origins(env.get("RELAY_DEV_ALLOW_ORIGINS", ",".join(LOCAL_DEV_ORIGINS)))
        origins.extend(origin for origin in development if origin not in origins)
    return origins
