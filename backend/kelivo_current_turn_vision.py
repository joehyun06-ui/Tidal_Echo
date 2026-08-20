"""Kelivo current-turn image compatibility wrapper.

Kelivo may serialize an attachment-bearing user message and the user's text as
separate messages in one request. The older multimodal middleware only promoted
images from the final message, so those images could be treated as history and
removed before generation. This outer middleware collects images from every user
message since the most recent assistant message, reduces them to a transient
visual description, and appends that description to the final user message.

Diagnostics deliberately record shape/counts only. No text, file paths, URLs,
image bytes, credentials, or hashes are logged.
"""

from __future__ import annotations

import json
from typing import Any

from backend import multimodal_patch as mm


class KelivoCurrentTurnVisionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/v1/chat/completions"
            or not mm._authorized_kelivo(scope)
        ):
            await self.app(scope, receive, send)
            return

        raw = bytearray()
        while True:
            event = await receive()
            if event.get("type") != "http.request":
                continue
            raw.extend(event.get("body") or b"")
            if len(raw) > mm.MAX_KELIVO_MULTIMODAL_BODY:
                await mm._send_json(send, 413, {
                    "error": {
                        "message": "request_body_too_large",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "request_body_too_large",
                    }
                })
                return
            if not event.get("more_body"):
                break
        body = bytes(raw)

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            await mm._replay_downstream(self.app, scope, send, body)
            return
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list) or not messages:
            await mm._replay_downstream(self.app, scope, send, body)
            return

        last_assistant = -1
        list_contents = 0
        image_parts = 0
        image_message_indexes: list[int] = []
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant":
                last_assistant = index
            content = message.get("content")
            if not isinstance(content, list):
                continue
            list_contents += 1
            count = sum(
                1 for part in content
                if isinstance(part, dict) and part.get("type") == "image_url"
            )
            if count:
                image_parts += count
                image_message_indexes.append(index)

        # Safe, data-free telemetry used only while closing this compatibility bug.
        if list_contents or image_parts:
            relative = [
                "current" if index > last_assistant else "history"
                for index in image_message_indexes
            ]
            print(
                "[kelivo-mm-shape] "
                f"messages={len(messages)} list_content={list_contents} "
                f"image_parts={image_parts} image_scope={','.join(relative) or '-'}",
                flush=True,
            )

        if not image_parts:
            await mm._replay_downstream(self.app, scope, send, body)
            return

        if not isinstance(messages[-1], dict) or messages[-1].get("role") != "user":
            await mm._replay_downstream(self.app, scope, send, body)
            return

        selected_images: list[tuple[str, bytes]] = []
        selected_indexes: set[int] = set()
        total = 0
        transformed: list[dict[str, Any]] = []
        prompt_parts: list[str] = []

        try:
            for index, message in enumerate(messages):
                if not isinstance(message, dict) or set(message) != {"role", "content"}:
                    raise mm.VisionError("invalid_messages", False)
                role = message.get("role")
                if role not in {"system", "developer", "user", "assistant"}:
                    raise mm.VisionError("invalid_messages", False)
                text, images, multimodal = mm._multimodal_parts(message.get("content"))

                for _mime, data in images:
                    total += len(data)
                    if total > mm.MAX_TOTAL_IMAGE_BYTES:
                        raise mm.VisionError("images_too_large", False)

                current_user_image = bool(images) and role == "user" and index > last_assistant
                if current_user_image:
                    if len(selected_images) + len(images) > mm.MAX_IMAGES:
                        raise mm.VisionError("too_many_images", False)
                    selected_images.extend(images)
                    selected_indexes.add(index)
                    if text.strip():
                        prompt_parts.append(text.strip())
                    transformed.append({
                        "role": role,
                        "content": (text + "\n[本轮附图已转为临时视觉上下文]").strip(),
                    })
                    continue

                if images:
                    transformed.append({
                        "role": role,
                        "content": (text + "\n[历史图片已省略]").strip(),
                    })
                    continue

                transformed.append({
                    "role": role,
                    "content": text if multimodal else message.get("content"),
                })

            final_text = str(transformed[-1].get("content") or "").strip()
            if final_text:
                prompt_parts.append(final_text)

            if not selected_images:
                await mm._replay_downstream(self.app, scope, send, body)
                return

            description = await mm._vision_async(
                selected_images,
                "\n".join(prompt_parts)[-8000:],
            )
            # The compatibility text intentionally omits image hashes/paths; those
            # are not needed for model grounding or canonical conversation state.
            final_user_text = final_text or "[用户发送了图片]"
            transformed[-1]["content"] = (
                final_user_text
                + "\n\n"
                + mm.VISUAL_CONTEXT_PREFIX
                + "\n"
                + description.strip()
            )
            payload["messages"] = transformed
            rewritten = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            print(
                "[kelivo-mm-shape] transformed=true "
                f"selected_images={len(selected_images)} selected_messages={len(selected_indexes)}",
                flush=True,
            )
        except mm.VisionError as exc:
            status = 504 if exc.uncertain else (
                413 if "large" in exc.category or "many" in exc.category else 422
            )
            print(
                f"[kelivo-mm-shape] transformed=false category={exc.category}",
                flush=True,
            )
            await mm._send_json(send, status, {
                "error": {
                    "message": exc.category,
                    "type": "invalid_request_error" if status < 500 else "server_error",
                    "param": None,
                    "code": exc.category,
                }
            })
            return

        await mm._replay_downstream(self.app, scope, send, rewritten)


def install(app) -> None:
    app.add_middleware(KelivoCurrentTurnVisionMiddleware)
