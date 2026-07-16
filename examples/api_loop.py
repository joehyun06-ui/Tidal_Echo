#!/usr/bin/env python3
"""
api_loop.py — optional server-side OpenAI-compatible loop for Companion Channel.

Run this beside backend/app.py when you want the VPS to answer directly via an
LLM API instead of routing every message to the Claude Code channel plugin.

Relay flow:
  PWA POST /relay/app/send
    -> relay stores the human message
    -> when /relay/app/brain == "loop", relay POSTs here: /loop/ingest
    -> this loop builds persona + same-session history + current message
    -> model answer is POSTed back to relay /channel/out

All private values live in env/.env. This file contains no domain, key, or
personal identity.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


def load_dotenv(path: Path) -> None:
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

LOOP_PORT = int(os.environ.get("LOOP_PORT", "3020"))
LOOP_CONFIG = Path(os.environ.get("LOOP_CONFIG", str(HERE / "api_loop.config.json")))
RELAY_DB = os.environ.get("RELAY_DB", str(HERE.parent / "backend" / "relay.db"))
RELAY_URL = os.environ.get("RELAY_URL", "http://127.0.0.1:3011").rstrip("/")
RELAY_SECRET = os.environ.get("RELAY_SECRET", "")
PERSONA_FILE = os.environ.get("PERSONA_FILE", "")
PERSONA = os.environ.get("PERSONA", "").strip()
HISTORY_N = int(os.environ.get("HISTORY_N", "24"))
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2000"))
TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.7"))
STREAM_OUTPUT = os.environ.get("LOOP_STREAM", "1").lower() not in {"0", "false", "no"}
LOOP_MODEL_TOTAL_TIMEOUT_SECONDS = float(os.environ.get("LOOP_MODEL_TOTAL_TIMEOUT_SECONDS", "120"))
LOOP_CALLBACK_TIMEOUT_SECONDS = float(os.environ.get("LOOP_CALLBACK_TIMEOUT_SECONDS", "30"))
LOOP_TIMEOUT_SAFETY_MARGIN_SECONDS = float(os.environ.get("LOOP_TIMEOUT_SAFETY_MARGIN_SECONDS", "15"))
LOOP_DISPATCH_TIMEOUT_SECONDS = float(os.environ.get("LOOP_DISPATCH_TIMEOUT_SECONDS", "180"))
if min(LOOP_MODEL_TOTAL_TIMEOUT_SECONDS, LOOP_CALLBACK_TIMEOUT_SECONDS,
       LOOP_TIMEOUT_SAFETY_MARGIN_SECONDS, LOOP_DISPATCH_TIMEOUT_SECONDS) <= 0:
    raise SystemExit("loop timeouts must be positive")
if LOOP_DISPATCH_TIMEOUT_SECONDS < (LOOP_MODEL_TOTAL_TIMEOUT_SECONDS + LOOP_CALLBACK_TIMEOUT_SECONDS + LOOP_TIMEOUT_SAFETY_MARGIN_SECONDS):
    raise SystemExit("LOOP_DISPATCH_TIMEOUT_SECONDS must cover model total, callback, and safety margin")
SAFE_FALLBACK_ERROR_CODES = {"model_not_found", "model_not_supported", "unsupported_model"}

if not PERSONA and PERSONA_FILE:
    try:
        PERSONA = Path(PERSONA_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        PERSONA = ""
if not PERSONA:
    PERSONA = (
        "You are the user's private AI companion in a one-to-one chat. "
        "Reply naturally, warmly, and concisely unless the user asks for detail."
    )


def env_routes() -> list[dict[str, str]]:
    routes: list[dict[str, str]] = []
    for suffix in ("", "_2", "_3", "_4"):
        base = os.environ.get(f"LLM_API_BASE{suffix}", "").rstrip("/")
        key = os.environ.get(f"LLM_API_KEY{suffix}", "")
        model = os.environ.get(f"LLM_MODEL{suffix}", "")
        if base and key and model:
            routes.append({"url": base, "key": key, "model": model})
    return routes


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def mask_key(key: str) -> str:
    key = str(key or "")
    if not key:
        return ""
    if len(key) <= 10:
        return "***"
    return key[:6] + "***" + key[-4:]


def load_config() -> dict[str, Any]:
    try:
        data = json.loads(LOOP_CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_config(cfg: dict[str, Any]) -> None:
    LOOP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOOP_CONFIG.with_suffix(LOOP_CONFIG.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LOOP_CONFIG)


def main_chain() -> list[dict[str, str]]:
    cfg = load_config()
    configured = cfg.get("main_chain")
    if isinstance(configured, list):
        rows = [r for r in configured if isinstance(r, dict) and r.get("url") and r.get("key") and r.get("model")]
        if rows:
            return rows
    return env_routes()


def history_n() -> int:
    try:
        return max(0, min(int(load_config().get("history_n", HISTORY_N)), 200))
    except Exception:
        return HISTORY_N


def session_rows() -> list[dict[str, Any]]:
    rows = load_config().get("sessions")
    if not isinstance(rows, list):
        return []
    out = []
    for item in rows:
        if isinstance(item, dict) and item.get("id"):
            out.append({
                "id": str(item.get("id")),
                "title": str(item.get("title") or "New chat"),
                "since_id": int(item.get("since_id") or 0),
                "created_at": item.get("created_at") or "",
                "pinned": bool(item.get("pinned", False)),
            })
    return out


def active_session_id() -> str:
    cfg = load_config()
    active = str(cfg.get("active_session") or "").strip()
    ids = {s["id"] for s in session_rows()}
    if active in ids:
        return active
    rows = session_rows()
    return rows[-1]["id"] if rows else ""


def save_sessions(rows: list[dict[str, Any]], active: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    cfg["sessions"] = rows
    if active is not None:
        cfg["active_session"] = active
    save_config(cfg)
    return sessions_public()


def sessions_public() -> dict[str, Any]:
    return {"active_session": active_session_id(), "sessions": session_rows()}


def create_session(title: str = "New chat", since_id: int = 0, activate: bool = True) -> dict[str, Any]:
    rows = session_rows()
    sid = "api-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    row = {"id": sid, "title": title or "New chat", "since_id": int(since_id or 0), "created_at": now_iso()}
    rows.append(row)
    save_sessions(rows, sid if activate else None)
    return row


def patch_session(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    rows = session_rows()
    found = False
    for item in rows:
        if item["id"] != session_id:
            continue
        found = True
        if "title" in body:
            item["title"] = str(body.get("title") or item["title"]).strip() or item["title"]
        if "pinned" in body:
            item["pinned"] = bool(body.get("pinned"))
    if not found:
        raise HTTPException(status_code=404, detail="session not found")
    active = session_id if body.get("active") else None
    return save_sessions(rows, active)


def relay_rows(before_id: int | None, session_id: str, limit: int) -> list[dict[str, Any]]:
    path = Path(RELAY_DB)
    if not path.exists():
        return []
    params: list[Any] = []
    where = ["kind IN ('user','voice','reply')"]
    if before_id:
        where.append("id < ?")
        params.append(int(before_id))
    if session_id:
        where.append("json_extract(meta, '$.api_session') = ?")
        params.append(session_id)
    else:
        where.append("(json_extract(meta, '$.api_session') IS NULL OR json_extract(meta, '$.api_session') = '')")
    sql = (
        "SELECT id, direction, kind, text, meta FROM messages "
        f"WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?"
    )
    params.append(max(0, limit))
    with closing(sqlite3.connect(str(path))) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in reversed(rows)]


def build_messages(text: str, *, before_id: int | None = None, session_id: str = "", use_context: bool = True) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": PERSONA}]
    if use_context:
        for row in relay_rows(before_id, session_id, history_n()):
            content = str(row.get("text") or "").strip()
            if not content:
                continue
            role = "assistant" if row.get("direction") == "out" else "user"
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": text})
    return messages


def public_config() -> dict[str, Any]:
    return {
        "history_n": history_n(),
        "active_session": active_session_id(),
        "sessions": session_rows(),
        "main_chain": [
            {"index": i, "model": r.get("model", ""), "url": r.get("url", ""), "key_masked": mask_key(r.get("key", ""))}
            for i, r in enumerate(main_chain())
        ],
    }


def update_config(body: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    if "history_n" in body:
        cfg["history_n"] = max(0, min(int(body.get("history_n") or 0), 200))
    if isinstance(body.get("main_chain"), list):
        old = main_chain()
        new_chain = []
        for pos, item in enumerate(body["main_chain"]):
            if not isinstance(item, dict):
                continue
            old_idx = int(item.get("index", pos) or 0)
            prev = old[old_idx] if 0 <= old_idx < len(old) else {}
            entry = {
                "model": str(item.get("model") or prev.get("model") or "").strip(),
                "url": str(item.get("url") or prev.get("url") or "").strip().rstrip("/"),
                "key": str(item.get("key") or prev.get("key") or ""),
            }
            if not (entry["model"] and entry["url"] and entry["key"]):
                raise HTTPException(status_code=400, detail=f"row {pos + 1}: model/url/key required")
            new_chain.append(entry)
        if new_chain:
            cfg["main_chain"] = new_chain
    save_config(cfg)
    return public_config()


async def relay_out(payload: dict[str, Any]) -> tuple[bool, Any, bool]:
    if not RELAY_SECRET:
        return False, {"error": "relay_secret_missing"}, False
    try:
        async with asyncio.timeout(LOOP_CALLBACK_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(timeout=LOOP_CALLBACK_TIMEOUT_SECONDS, trust_env=False) as client:
                resp = await client.post(
                    f"{RELAY_URL}/channel/out",
                    headers={"Authorization": f"Bearer {RELAY_SECRET}", "Content-Type": "application/json"},
                    json=payload,
                )
    except (TimeoutError, httpx.TimeoutException, httpx.NetworkError):
        return False, {"error": "callback_uncertain"}, True
    try:
        body: Any = resp.json()
    except Exception:
        return False, {"error": "invalid_callback_response"}, True
    ok = resp.status_code < 300 and isinstance(body, dict) and body.get("ok") is not False and "id" in body
    return ok, body, False


class ModelRouteError(Exception):
    def __init__(self, category: str, outcome: str):
        super().__init__(category)
        self.category = category
        self.outcome = outcome


def _safe_log(category: str) -> None:
    print(f"[api-loop] model_dispatch={category}", file=sys.stderr, flush=True)


async def _check_provider_response(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    if resp.status_code == 404:
        try:
            await resp.aread()
            payload = resp.json()
            code = str((payload.get("error") or {}).get("code") or "").strip().lower()
        except Exception:
            code = ""
        if code in SAFE_FALLBACK_ERROR_CODES:
            raise ModelRouteError("model_unsupported", "safe_to_fallback")
    if resp.status_code in {408, 429} or resp.status_code >= 500:
        raise ModelRouteError("provider_response_uncertain", "dispatch_uncertain")
    raise ModelRouteError("provider_explicit_rejection", "explicit_failed")


async def stream_chat(route: dict[str, str], messages: list[dict[str, str]], sink) -> dict[str, Any]:
    body = {
        "model": route["model"],
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": True,
    }
    text_parts: list[str] = []
    usage: dict[str, Any] = {}
    model_work_started = False
    done_received = False
    try:
        async with httpx.AsyncClient(timeout=LOOP_MODEL_TOTAL_TIMEOUT_SECONDS, trust_env=False) as client:
            async with client.stream(
                "POST", route["url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {route['key']}", "Content-Type": "application/json"}, json=body,
            ) as resp:
                await _check_provider_response(resp)
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done_received = True
                        break
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        raise ModelRouteError("invalid_stream_response", "dispatch_uncertain") from None
                    if not isinstance(ev, dict) or not isinstance(ev.get("choices"), list):
                        raise ModelRouteError("invalid_stream_response", "dispatch_uncertain")
                    if isinstance(ev.get("usage"), dict):
                        usage = ev["usage"]
                    delta = (((ev.get("choices") or [{}])[0]).get("delta") or {})
                    chunk = delta.get("content") or ""
                    if chunk:
                        model_work_started = True
                        text_parts.append(chunk)
                        await sink(chunk)
    except asyncio.CancelledError:
        raise
    except ModelRouteError:
        raise
    except Exception:
        category = "model_stream_interrupted" if model_work_started else "model_transport_uncertain"
        raise ModelRouteError(category, "dispatch_uncertain") from None
    if not done_received or not text_parts:
        raise ModelRouteError("incomplete_stream_response", "dispatch_uncertain")
    return {"text": "".join(text_parts).strip(), "usage": usage}


async def complete_chat(route: dict[str, str], messages: list[dict[str, str]]) -> dict[str, Any]:
    body = {
        "model": route["model"],
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=LOOP_MODEL_TOTAL_TIMEOUT_SECONDS, trust_env=False) as client:
            resp = await client.post(
                route["url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {route['key']}", "Content-Type": "application/json"}, json=body,
            )
        await _check_provider_response(resp)
        data = resp.json()
    except asyncio.CancelledError:
        raise
    except ModelRouteError:
        raise
    except Exception:
        raise ModelRouteError("model_transport_uncertain", "dispatch_uncertain") from None
    msg = ((data.get("choices") or [{}])[0]).get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        raise ModelRouteError("empty_model_response", "dispatch_uncertain")
    return {"text": text, "usage": data.get("usage") or {}}


async def run_model(messages: list[dict[str, str]], *, stream_id: str = "", session_id: str = "", emit_stream: bool = False) -> dict[str, Any]:
    tried: list[str] = []

    async def execute() -> dict[str, Any]:
        for route in main_chain():
            tried.append(str(route.get("model") or ""))
            try:
                if emit_stream and STREAM_OUTPUT:
                    async def sink(chunk: str) -> None:
                        await relay_out({"type": "reply_delta", "stream_id": stream_id, "text": chunk,
                                         "done": False, "api_session": session_id})
                    out = await stream_chat(route, messages, sink)
                else:
                    out = await complete_chat(route, messages)
            except asyncio.CancelledError:
                raise
            except ModelRouteError as exc:
                if exc.outcome == "safe_to_fallback":
                    continue
                _safe_log(exc.category)
                return {"outcome": exc.outcome, "error": exc.category, "tried": tried}
            except Exception:
                _safe_log("model_unexpected_uncertain")
                return {"outcome": "dispatch_uncertain", "error": "model_unexpected_uncertain", "tried": tried}
            out.update({"outcome": "success", "model": route.get("model"), "tried": tried[:-1]})
            return out
        return {"outcome": "explicit_failed", "error": "no_supported_model", "tried": tried}

    try:
        async with asyncio.timeout(LOOP_MODEL_TOTAL_TIMEOUT_SECONDS):
            return await execute()
    except TimeoutError:
        _safe_log("model_timeout")
        return {"outcome": "dispatch_uncertain", "error": "model_timeout", "tried": tried}


async def handle_ingest(
    text: str,
    msg_id: int | None,
    session_id: str,
    *,
    dry: bool = False,
    stream_id: str = "",
    generation_id: str = "",
    reply_to: str = "",
    channel: str = "",
    channel_account: str = "",
    channel_conversation: str = "",
) -> dict[str, Any]:
    stream_id = stream_id or ("api-" + uuid.uuid4().hex[:16])
    messages = build_messages(text, before_id=msg_id, session_id=session_id, use_context=True)
    out = await run_model(messages, stream_id=stream_id, session_id=session_id, emit_stream=not dry)
    if out.get("outcome") != "success":
        uncertain = out.get("outcome") == "dispatch_uncertain"
        return {"ok": False, "callback_delivered": False, "dispatch_uncertain": uncertain,
                "generation_id": generation_id, "stream_id": stream_id,
                "api_session": session_id, "error": out.get("error") or "model_failed"}
    reply = (out.get("text") or "").strip()
    meta = {
        "runtime": "api_loop",
        "model": out.get("model"),
        "fallback_from": out.get("tried") or [],
        "usage": out.get("usage") or {},
        "session": session_id,
    }
    if dry:
        return {"ok": True, "reply": reply, "api": meta}
    if STREAM_OUTPUT:
        ok, body, uncertain = await relay_out({
            "type": "reply_delta",
            "stream_id": stream_id,
            "generation_id": generation_id,
            "reply_to": reply_to,
            "done": True,
            "final_text": reply,
            "api": meta,
            "api_session": session_id,
            "channel": channel,
            "channel_account": channel_account,
            "channel_conversation": channel_conversation,
        })
    else:
        ok, body, uncertain = await relay_out({
            "type": "reply", "text": reply, "api": meta, "api_session": session_id,
            "stream_id": stream_id, "generation_id": generation_id, "reply_to": reply_to,
            "channel": channel, "channel_account": channel_account,
            "channel_conversation": channel_conversation,
        })
    return {"ok": ok, "callback_delivered": ok, "dispatch_uncertain": uncertain,
            "generation_id": generation_id, "stream_id": stream_id, "api_session": session_id,
            "relay": body, "api": meta}


app = FastAPI(title="companion-api-loop")


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "models": [r.get("model") for r in main_chain()],
        "history_n": history_n(),
        "relay_db": RELAY_DB,
        "relay_secret_loaded": bool(RELAY_SECRET),
    }


@app.get("/loop/config")
async def loop_config():
    return public_config()


@app.post("/loop/config")
async def loop_config_update(request: Request):
    return update_config(await request.json())


@app.get("/loop/sessions")
async def loop_sessions():
    return sessions_public()


@app.post("/loop/sessions")
async def loop_sessions_create(request: Request):
    body = await request.json()
    row = create_session(
        title=str(body.get("title") or "New chat"),
        since_id=int(body.get("since_id") or 0),
        activate=bool(body.get("activate", True)),
    )
    return {**sessions_public(), "created": row}


@app.patch("/loop/sessions/{session_id}")
async def loop_sessions_patch(session_id: str, request: Request):
    return patch_session(session_id, await request.json())


@app.post("/loop/chat")
async def loop_chat(request: Request):
    body = await request.json()
    text = str(body.get("text") or body.get("message") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    session_id = str(body.get("session_id") or body.get("api_session") or active_session_id() or "").strip()
    messages = build_messages(text, before_id=None, session_id=session_id, use_context=bool(body.get("use_context", True)))
    out = await run_model(messages, emit_stream=False)
    if out.get("outcome") != "success":
        return JSONResponse({"ok": False, "dispatch_uncertain": out.get("outcome") == "dispatch_uncertain",
                             "error": out.get("error")}, status_code=504 if out.get("outcome") == "dispatch_uncertain" else 502)
    return {"ok": True, "reply": out.get("text") or "", "api": out}


@app.post("/loop/ingest")
async def loop_ingest(request: Request):
    body = await request.json()
    text = str(body.get("text") or body.get("message") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    msg_id = body.get("id")
    try:
        before_id = int(msg_id) if msg_id is not None else None
    except Exception:
        before_id = None
    session_id = str(body.get("session_id") or body.get("api_session") or active_session_id() or "").strip()
    dry = bool(body.get("dry"))
    result = await handle_ingest(
        text,
        before_id,
        session_id,
        dry=dry,
        stream_id=str(body.get("stream_id") or "").strip(),
        generation_id=str(body.get("generation_id") or "").strip(),
        reply_to=str(body.get("reply_to") or "").strip(),
        channel=str(body.get("channel") or "").strip(),
        channel_account=str(body.get("channel_account") or "").strip(),
        channel_conversation=str(body.get("channel_conversation") or "").strip(),
    )
    if result.get("ok") is not True:
        return JSONResponse(result, status_code=504 if result.get("dispatch_uncertain") else 502)
    return result


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=LOOP_PORT)
