"""Private compatibility bridge for the legacy OUO Home synchronous /chat backend.

The legacy VPS remains the canonical writer for its Supabase conversation. This
module only exposes a server-to-server OpenAI-compatible generation endpoint so
that the old Android shell can use the same healthy api_loop provider path as the
current Render service. The bridge never persists chat content in the Render
relay database and never exposes its credential to browsers.
"""

from __future__ import annotations

import hmac
import json
import os
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from backend import deployment_config, kelivo_service
from backend import app as relay_app


app = relay_app.app

_BRIDGE_TOKEN = os.environ.get("LEGACY_CHAT_BRIDGE_TOKEN", "").strip()
_BRIDGE_SESSION = os.environ.get(
    "LEGACY_CHAT_BRIDGE_SESSION", "legacy-ouo-home-api"
).strip()

if (
    len(_BRIDGE_TOKEN) < 32
    or len(_BRIDGE_TOKEN) > 256
    or any(char.isspace() for char in _BRIDGE_TOKEN)
):
    raise RuntimeError("invalid legacy chat bridge token")
if (
    not _BRIDGE_SESSION
    or len(_BRIDGE_SESSION) > 128
    or any(ord(char) < 33 or ord(char) > 126 for char in _BRIDGE_SESSION)
):
    raise RuntimeError("invalid legacy chat bridge session")


def _error(status_code: int, category: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": category,
                "type": "authentication_error" if status_code == 401 else "server_error",
                "param": None,
                "code": category,
            }
        },
        status_code=status_code,
    )


def _check_bridge_auth(request: Request) -> bool:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return False
    supplied = authorization[7:].strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied, _BRIDGE_TOKEN)


@app.post("/internal/legacy-chat/v1/chat/completions")
async def legacy_chat_completion(request: Request):
    if not _check_bridge_auth(request):
        return _error(401, "unauthorized")

    content_length = request.headers.get("content-length", "")
    if content_length:
        if not content_length.isascii() or not content_length.isdecimal():
            return _error(400, "invalid_content_length")
        if int(content_length) > kelivo_service.MAX_BODY_BYTES:
            return _error(413, "request_body_too_large")

    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > kelivo_service.MAX_BODY_BYTES:
            return _error(413, "request_body_too_large")

    try:
        payload = json.loads(bytes(raw))
        validated = kelivo_service.validate_completion(
            payload,
            relay_app.DEPLOYMENT.kelivo.model_alias,
        )
        provider_defaults = deployment_config.resolve_kelivo_provider_contract_defaults(
            os.environ,
            relay_app.DEPLOYMENT.loop_config,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        return _error(400, "malformed_json")
    except kelivo_service.KelivoError as error:
        return _error(error.status_code, error.category)
    except deployment_config.DeploymentConfigError:
        return _error(503, "provider_contract_unavailable")

    generator = relay_app.KELIVO_GENERATOR
    if generator is None:
        return _error(503, "generation_service_unavailable")

    temperature = (
        validated.temperature
        if validated.temperature is not None
        else provider_defaults.temperature
    )
    max_tokens = (
        validated.max_tokens
        if validated.max_tokens is not None
        else provider_defaults.max_tokens
    )

    try:
        result = await generator(
            validated.messages,
            _BRIDGE_SESSION,
            provider_defaults.provider_model,
            temperature,
            max_tokens,
            {"prompt_contract_version": kelivo_service.PROMPT_CONTRACT_VERSION},
        )
    except kelivo_service.GenerationError as error:
        return _error(504 if error.uncertain else 502, error.category)
    except Exception:
        return _error(504, "unexpected_generation_error")

    reply = result.get("text") if isinstance(result, dict) else None
    if not isinstance(reply, str) or not reply.strip():
        return _error(502, "empty_model_response")

    usage = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(usage, dict):
        usage = {}

    return {
        "id": "legacy-chat-bridge",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": relay_app.DEPLOYMENT.kelivo.model_alias,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }
