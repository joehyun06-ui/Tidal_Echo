"""Private compatibility bridge for the legacy OUO Home synchronous /chat backend.

The legacy VPS remains the canonical writer for its Supabase conversation. This
module only exposes server-to-server compatibility endpoints so the old Android
shell can use the same provider path as the current Render service. Credentials
never need to be exposed to browsers.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from backend import autonomous_wake_session_guard, deployment_config, kelivo_service
from backend import app as relay_app


app = relay_app.app

_BRIDGE_TOKEN = os.environ.get("LEGACY_CHAT_BRIDGE_TOKEN", "").strip()
_BRIDGE_SESSION = os.environ.get(
    "LEGACY_CHAT_BRIDGE_SESSION", "legacy-ouo-home-api"
).strip()
_WAKE_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_API_SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

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


def _check_internal_auth(request: Request) -> bool:
    expected = str(relay_app.API_LOOP_INTERNAL_TOKEN or "").strip()
    supplied = str(request.headers.get("x-internal-token") or "").strip()
    if len(expected) < 32 or not supplied:
        return False
    return hmac.compare_digest(supplied, expected)


def _existing_autonomous_message(run_id: str) -> dict | None:
    """Return an already persisted canonical wake message for this run.

    The relay schema intentionally keeps arbitrary metadata as JSON text. Read a
    bounded tail and decode in Python instead of depending on SQLite JSON1.
    """
    with relay_app.db() as conn:
        rows = conn.execute(
            """SELECT id,ts,direction,kind,text,meta
               FROM messages
               WHERE direction='out' AND kind='reply'
               ORDER BY id DESC LIMIT 2000"""
        ).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        if meta.get("autonomous_wake_run_id") != run_id:
            continue
        return {
            "id": row["id"],
            "ts": row["ts"],
            "direction": row["direction"],
            "kind": row["kind"],
            "text": row["text"],
            "meta": meta,
        }
    return None


@app.post("/internal/autonomous-wake/out")
async def autonomous_wake_out(request: Request):
    """Persist a wake message in the canonical relay exactly once per run."""
    if not _check_internal_auth(request):
        return _error(401, "unauthorized")

    raw = await request.body()
    if len(raw) > 8 * 1024:
        return _error(413, "request_body_too_large")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error(400, "malformed_json")
    if not isinstance(body, dict):
        return _error(400, "invalid_request_body")

    run_id = str(body.get("run_id") or "").strip()
    message = str(body.get("message") or "").strip()
    model = str(body.get("model") or "").strip()
    api_session = str(body.get("api_session") or "").strip()
    if _WAKE_RUN_ID_RE.fullmatch(run_id) is None:
        return _error(422, "invalid_run_id")
    if not message or len(message) > 1000:
        return _error(422, "invalid_message")
    if len(model) > 200:
        return _error(422, "invalid_model")
    if api_session and _API_SESSION_RE.fullmatch(api_session) is None:
        return _error(422, "invalid_api_session")

    existing = _existing_autonomous_message(run_id)
    if existing is not None:
        return {"ok": True, "status": "delivered", "id": existing["id"], "duplicate": True}

    try:
        if autonomous_wake_session_guard.is_active_codex_session(
            api_session,
            os.environ,
        ):
            return _error(409, "autonomous_wake_codex_session_forbidden")
    except autonomous_wake_session_guard.AutonomousWakeSessionError as error:
        return _error(503, error.category)

    meta = {
        "autonomous": True,
        "autonomous_wake_run_id": run_id,
        "source": "autonomous_wake",
    }
    if model:
        meta["model"] = model
    if api_session:
        meta["api_session"] = api_session
    msg = relay_app.save_message("out", "reply", message, meta)

    await relay_app.broadcast(relay_app.app_subs, {"type": "typing", "active": False})
    await relay_app.broadcast(relay_app.app_subs, relay_app.app_payload(msg))
    if not relay_app.app_subs:
        try:
            await relay_app.push_to_all(relay_app.notification_from_message(msg))
        except Exception:
            pass
    return {"ok": True, "status": "delivered", "id": msg["id"], "duplicate": False}


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


# Imported only by this Render entrypoint. Core modules retain their reviewed
# text-only contracts; the patch performs bounded image->text compatibility.
from backend import multimodal_patch as _multimodal_patch  # noqa: E402
from backend import kelivo_current_turn_vision as _kelivo_turn_vision  # noqa: E402
from backend import telegram_image_followup_coalesce as _telegram_image_turn  # noqa: E402

_kelivo_turn_vision.install(app)
_telegram_image_turn.install()


@app.post("/internal/legacy-chat/vision-context")
async def legacy_vision_context(request: Request):
    """Return a transient visual description for the authenticated legacy VPS.

    The caller supplies inline data:image/* URLs only. Nothing is persisted by
    this endpoint and image-derived text is never written into Memory here.
    """
    if not _check_bridge_auth(request):
        return _error(401, "unauthorized")

    raw = await request.body()
    if len(raw) > _multimodal_patch.MAX_KELIVO_MULTIMODAL_BODY:
        return _error(413, "request_body_too_large")

    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) - {"images", "prompt"}:
            return _error(400, "invalid_request_body")
        raw_images = payload.get("images")
        prompt = payload.get("prompt", "")
        if not isinstance(raw_images, list) or not 1 <= len(raw_images) <= _multimodal_patch.MAX_IMAGES:
            return _error(422, "invalid_images")
        if not isinstance(prompt, str) or len(prompt) > 8000:
            return _error(422, "invalid_prompt")

        images = []
        total = 0
        for raw_image in raw_images:
            mime, image = _multimodal_patch._decode_data_url(raw_image)
            total += len(image)
            if total > _multimodal_patch.MAX_TOTAL_IMAGE_BYTES:
                return _error(413, "images_too_large")
            images.append((mime, image))

        description = await _multimodal_patch._vision_async(
            images,
            prompt or "请读取用户这次发送的图片，并忠实描述与当前对话相关的内容。",
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        return _error(400, "malformed_json")
    except _multimodal_patch.VisionError as error:
        return _error(504 if error.uncertain else 422, error.category)
    except Exception:
        return _error(500, "vision_context_failed")

    return {"ok": True, "description": description}


@app.post("/internal/legacy-chat/vision-smoke")
async def legacy_vision_smoke(request: Request):
    if not _check_bridge_auth(request):
        return _error(401, "unauthorized")
    raw = await request.body()
    if len(raw) > 1024 * 1024:
        return _error(413, "request_body_too_large")
    try:
        payload = json.loads(raw)
        mime, image = _multimodal_patch._decode_data_url(payload.get("image"))
        description = await _multimodal_patch._vision_async(
            [(mime, image)],
            "请只确认你能读取这张测试图片，并简短描述它。",
        )
    except _multimodal_patch.VisionError as error:
        return _error(504 if error.uncertain else 502, error.category)
    except Exception:
        return _error(500, "vision_smoke_failed")
    return {"ok": True, "has_description": bool(description.strip())}
