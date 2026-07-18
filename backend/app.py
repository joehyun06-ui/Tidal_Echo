#!/usr/bin/env python3
"""
companion relay backend — a private 1:1 message channel between a person and
their AI companion (an AI running locally as a Claude Code "channel" plugin).

Two ends, one shared secret:
  - AI side   (local CC channel plugin):  POST /channel/out  ·  SSE GET /channel/in
  - Human side (phone PWA):               POST /app/send     ·  SSE GET /app/stream  ·  GET /app/history

No framework magic: messages land in sqlite and fan out to SSE subscribers via
one asyncio.Queue per connection. A single shared Bearer secret guards every
endpoint (single user). The secret may travel in the Authorization header *or*
as a ?token= query param — because the browser's native EventSource cannot set
custom headers.

Everything personal — names, secrets, domain, paths — comes from environment
variables (see .env.example). Nothing identifying is hard-coded.
"""

import asyncio
import mimetypes
import hmac
import json
import os
import re
import secrets
import subprocess
import sqlite3
import urllib.error
import urllib.request
import urllib.parse
import signal
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

try:
    from . import channel_store, deployment_config, kelivo_service
    from .telegram_integration import LoopDispatchError, TelegramConfig, TelegramWorker, validate_update
except ImportError:  # support `python backend/app.py`
    import channel_store, deployment_config, kelivo_service
    from telegram_integration import LoopDispatchError, TelegramConfig, TelegramWorker, validate_update

try:
    from pywebpush import webpush, WebPushException
except Exception:  # a missing lib must not stop the relay from starting
    webpush = None
    class WebPushException(Exception):
        pass


# --- identity (parameterized — set these to your own names) ----------------
AI_NAME = os.environ.get("RELAY_AI_NAME", "AI")          # AI companion's display name (push title, narration)
HUMAN_NAME = os.environ.get("RELAY_HUMAN_NAME", "对方")   # how the AI is told about you in voice/call narration

# --- core config / secrets (all from env) ----------------------------------
SECRET = os.environ.get("RELAY_SECRET", "")
DB_PATH = os.environ.get("RELAY_DB", str(Path(__file__).parent / "relay.db"))
PORT = deployment_config.parse_port(os.environ.get("RELAY_PORT", "3011"), "invalid_relay_port")
UPLOAD_DIR = Path(os.environ.get("RELAY_UPLOAD_DIR", str(Path(__file__).parent / "uploads")))
PUBLIC_PREFIX = os.environ.get("RELAY_PUBLIC_PREFIX", "/relay").rstrip("/")
APP_PATH = os.environ.get("RELAY_APP_PATH", "/")  # where a push-notification tap opens the PWA
ALLOW_ORIGINS = [o.strip() for o in os.environ.get(
    "RELAY_ALLOW_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080"
).split(",") if o.strip()]
MAX_UPLOAD_BYTES = int(os.environ.get("RELAY_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
VOICE_MAX_BYTES = int(os.environ.get("RELAY_VOICE_MAX_BYTES", str(8 * 1024 * 1024)))
VOICE_TRANSCRIBE_CMD = os.environ.get("RELAY_VOICE_TRANSCRIBE_CMD", "")

# --- MiniMax TTS (optional — leave keys blank to disable spoken replies) ----
MINIMAX_API_BASE = os.environ.get("MINIMAX_API_BASE", "https://api.minimaxi.com")
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_GROUP_ID = os.environ.get("MINIMAX_GROUP_ID", "")
MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL", "speech-02-hd")
MINIMAX_VOICE_ZH = os.environ.get("MINIMAX_VOICE_ZH", "")
MINIMAX_TTS_TIMEOUT = float(os.environ.get("MINIMAX_TTS_TIMEOUT", "30"))

# --- Web Push (VAPID, optional) — push unread replies to the PWA lock screen
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_PEM = os.environ.get("VAPID_PRIVATE_PEM", "")   # PEM file path OR inline PEM text
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")
PUSH_PREVIEW_CHARS = int(os.environ.get("RELAY_PUSH_PREVIEW_CHARS", "120"))

# --- presence tuning (seconds) ---------------------------------------------
PRESENCE_ONLINE_SEC = int(os.environ.get("RELAY_PRESENCE_ONLINE_SEC", "180"))
PRESENCE_RECENT_SEC = int(os.environ.get("RELAY_PRESENCE_RECENT_SEC", "1800"))

# --- Optional server-side API loop -----------------------------------------
# "desktop" keeps the original Claude Code channel path. "loop" forwards new
# human messages to a local HTTP loop, which replies through /channel/out.
BRAIN_FILE = Path(os.environ.get("RELAY_BRAIN_FILE", str(Path(__file__).parent / "brain_target")))
LOOP_INGEST_URL = os.environ.get("RELAY_LOOP_INGEST_URL", "http://127.0.0.1:3020/loop/ingest")
STREAM_DRAFT_TTL = int(os.environ.get("RELAY_STREAM_DRAFT_TTL", "600"))
API_LOOP_EXPECTED_NONCE = os.environ.get("API_LOOP_EXPECTED_NONCE", "")
API_LOOP_INTERNAL_TOKEN = os.environ.get("API_LOOP_INTERNAL_TOKEN", "")
try:
    LOOP_TARGET_REQUESTED = (
        os.environ.get("RELAY_BRAIN_TARGET", "").strip() == "loop"
        or BRAIN_FILE.read_text(encoding="utf-8").strip() == "loop"
    )
except OSError:
    LOOP_TARGET_REQUESTED = False
try:
    TELEGRAM = TelegramConfig.from_env()
    DEPLOYMENT = deployment_config.load_deployment_config(TELEGRAM)
    if DEPLOYMENT.render_telegram_mvp and not API_LOOP_EXPECTED_NONCE:
        raise deployment_config.DeploymentConfigError("api_loop_expected_nonce_missing")
    if (LOOP_TARGET_REQUESTED or DEPLOYMENT.render_telegram_mvp or DEPLOYMENT.kelivo.enabled) and len(API_LOOP_INTERNAL_TOKEN) < 32:
        raise deployment_config.DeploymentConfigError("api_loop_internal_token_missing")
    deployment_config.prepare_persistent_paths(DEPLOYMENT)
    deployment_config.initialize_brain_target(DEPLOYMENT)
except deployment_config.DeploymentConfigError as exc:
    raise SystemExit(f"invalid deployment configuration: {exc.category}") from None
except OSError:
    raise SystemExit("invalid deployment configuration: brain_target_initialization_failed") from None
LOOP_MODEL_TOTAL_TIMEOUT_SECONDS = DEPLOYMENT.timeouts.model_total
LOOP_CALLBACK_TIMEOUT_SECONDS = DEPLOYMENT.timeouts.callback
LOOP_TIMEOUT_SAFETY_MARGIN_SECONDS = DEPLOYMENT.timeouts.safety_margin
LOOP_DISPATCH_TIMEOUT_SECONDS = DEPLOYMENT.timeouts.dispatch
validate_loop_timeouts = deployment_config.validate_loop_timeouts
KELIVO_PERSONA, KELIVO_PERSONA_SOURCE = deployment_config.load_server_persona()

if not SECRET:
    raise SystemExit("RELAY_SECRET is required (set it in the systemd EnvironmentFile)")


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH, timeout=DEPLOYMENT.sqlite_busy_timeout_seconds,
        factory=channel_store.ClosingConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {max(1, int(DEPLOYMENT.sqlite_busy_timeout_seconds * 1000))}")
    return conn


def init_db() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT NOT NULL,
                direction TEXT NOT NULL,   -- 'in' (human -> AI) | 'out' (AI -> human)
                kind      TEXT NOT NULL,   -- 'user' | 'reply' | 'thinking' | 'voice' | 'call' | ...
                text      TEXT NOT NULL,
                meta      TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint TEXT PRIMARY KEY,
                p256dh   TEXT NOT NULL,
                auth     TEXT NOT NULL,
                ua       TEXT,
                created  TEXT NOT NULL,
                last_ok  TEXT
            )
            """
        )
        conn.commit()
    channel_store.run_migrations(DB_PATH)
    channel_store.recover_inflight_generations(DB_PATH)
    channel_store.recover_inflight_deliveries(DB_PATH)
    # A process restart makes every prior in-flight dispatch uncertain; never
    # assume a provider call did not happen merely because this worker died.
    kelivo_service.recover_dispatching_requests(DB_PATH, stale_seconds=0)
    if DEPLOYMENT.kelivo.enabled:
        kelivo_service.initialize_client_mapping(
            DB_PATH, DEPLOYMENT.kelivo.client_id, DEPLOYMENT.kelivo.api_session,
            allow_session_remap=DEPLOYMENT.kelivo.allow_session_remap,
            require_telegram_session=DEPLOYMENT.kelivo.require_telegram_session,
            allowed_account_ids=frozenset({TELEGRAM.account_id}),
            allowed_chat_ids=frozenset(str(value) for value in TELEGRAM.allowed_chat_ids),
            allowed_user_ids=frozenset(str(value) for value in TELEGRAM.allowed_user_ids),
        )


def save_message(direction: str, kind: str, text: str, meta: dict) -> dict:
    ts = meta.get("ts") or now_iso()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
            (ts, direction, kind, text, json.dumps(meta, ensure_ascii=False)),
        )
        conn.commit()
        mid = cur.lastrowid
    return {"id": mid, "ts": ts, "direction": direction, "kind": kind, "text": text, "meta": meta}


def set_reaction(message_id, who, emoji):
    # Set/clear one party's reaction on an existing message.
    # Returns the message's reactions dict, or None if the target doesn't exist.
    with db() as conn:
        row = conn.execute("SELECT meta FROM messages WHERE id = ?", (message_id,)).fetchone()
        if not row:
            return None
        meta = json.loads(row["meta"] or "{}")
        reactions = meta.get("reactions") or {}
        if emoji:
            reactions[who] = emoji
        else:
            reactions.pop(who, None)
        if reactions:
            meta["reactions"] = reactions
        else:
            meta.pop("reactions", None)
        conn.execute(
            "UPDATE messages SET meta = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), message_id),
        )
        conn.commit()
    return reactions


def history(since: int, limit: int) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?",
            (since, limit),
        ).fetchall()
    return rows_to_messages(rows)


def history_for_session(session_id: str, since: int, limit: int) -> list:
    session_id = (session_id or "").strip()
    if not session_id:
        return history(since, limit)
    with db() as conn:
        if session_id == "__legacy__":
            rows = conn.execute(
                "SELECT * FROM messages "
                "WHERE id > ? AND (json_extract(meta, '$.api_session') IS NULL OR json_extract(meta, '$.api_session') = '') "
                "ORDER BY id ASC LIMIT ?",
                (since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages "
                "WHERE id > ? AND json_extract(meta, '$.api_session') = ? "
                "ORDER BY id ASC LIMIT ?",
                (since, session_id, limit),
            ).fetchall()
    return rows_to_messages(rows)


def inbound_history(since: int, limit: int) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE id > ? AND direction = 'in' ORDER BY id ASC LIMIT ?",
            (since, limit),
        ).fetchall()
    return rows_to_messages(rows)


def rows_to_messages(rows) -> list:
    return [
        {
            "id": r["id"], "ts": r["ts"], "direction": r["direction"],
            "kind": r["kind"], "text": r["text"], "meta": json.loads(r["meta"] or "{}"),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# web push — subscription storage + send
# ---------------------------------------------------------------------------

def save_subscription(endpoint: str, p256dh: str, auth: str, ua: str = "") -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth, ua, created, last_ok)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, ua=excluded.ua
            """,
            (endpoint, p256dh, auth, ua, now_iso(), None),
        )
        conn.commit()


def delete_subscription(endpoint: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        conn.commit()


def list_subscriptions() -> list:
    with db() as conn:
        rows = conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()
    return [{"endpoint": r["endpoint"], "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}} for r in rows]


def mark_subscription_ok(endpoint: str) -> None:
    with db() as conn:
        conn.execute("UPDATE push_subscriptions SET last_ok = ? WHERE endpoint = ?", (now_iso(), endpoint))
        conn.commit()


def _send_one_push(sub: dict, data: str):
    """Blocking single send (run in a thread). Returns (endpoint, status): 0=ok, 404/410=dead, else=transient."""
    if webpush is None:
        return sub["endpoint"], -1
    try:
        webpush(
            subscription_info=sub,
            data=data,
            vapid_private_key=VAPID_PRIVATE_PEM,
            vapid_claims={"sub": VAPID_SUBJECT},
            timeout=10,
        )
        return sub["endpoint"], 0
    except WebPushException as exc:
        code = getattr(getattr(exc, "response", None), "status_code", 0) or 0
        return sub["endpoint"], code
    except Exception:
        return sub["endpoint"], -1


async def push_to_all(payload: dict) -> dict:
    """Best-effort fan-out to all subscriptions; never raises. 404/410 prunes dead subs."""
    if webpush is None or not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_PEM:
        return {"sent": 0, "dead": 0, "skipped": "not_configured"}
    subs = list_subscriptions()
    if not subs:
        return {"sent": 0, "dead": 0}
    data = json.dumps(payload, ensure_ascii=False)
    results = await asyncio.gather(*[asyncio.to_thread(_send_one_push, s, data) for s in subs])
    sent = dead = 0
    for endpoint, status in results:
        if status == 0:
            sent += 1
            mark_subscription_ok(endpoint)
        elif status in (404, 410):
            delete_subscription(endpoint)
            dead += 1
    return {"sent": sent, "dead": dead}


_PUSH_TAG_RE = re.compile(r"<[^>]+>")


def notification_from_message(msg: dict) -> dict:
    raw = (msg.get("text") or "").strip()
    body = _PUSH_TAG_RE.sub("", raw)
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) > PUSH_PREVIEW_CHARS:
        body = body[:PUSH_PREVIEW_CHARS].rstrip() + "…"
    if not body:
        body = f"{AI_NAME}给你发来一条消息"
    return {"title": AI_NAME, "body": body, "url": APP_PATH, "id": msg.get("id"), "ts": msg.get("ts")}


# ---------------------------------------------------------------------------
# pub/sub — one asyncio.Queue per connected SSE client
# ---------------------------------------------------------------------------

plugin_subs: set[asyncio.Queue] = set()  # AI side    (GET /channel/in)
app_subs: set[asyncio.Queue] = set()     # human side (GET /app/stream)
stream_drafts: dict[tuple[str, str], dict] = {}


async def broadcast(subs: set, payload: dict) -> None:
    for q in list(subs):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            subs.discard(q)  # slow/dead consumer — drop it


def app_payload(msg: dict) -> dict:
    """Shape the PWA renders: from = 'human' | 'ai', plus kind for styling."""
    return {
        "id": msg["id"], "ts": msg["ts"],
        "from": "human" if msg["direction"] == "in" else "ai",
        "kind": msg["kind"], "text": msg["text"], "meta": msg["meta"],
    }


def plugin_payload(msg: dict) -> dict:
    meta = msg.get("meta") or {}
    return {
        "id": msg["id"],
        "content": msg["text"],
        "user": meta.get("user") or "human",
        "ts": msg["ts"],
        "attachments": meta.get("attachments") or [],
    }


def brain_target() -> str:
    try:
        target = BRAIN_FILE.read_text(encoding="utf-8").strip()
        return target if target in ("desktop", "loop") else "desktop"
    except FileNotFoundError:
        return "desktop"
    except Exception:
        return "desktop"


def _forward_to_loop_sync(msg: dict, routing: dict | None = None) -> dict:
    meta = msg.get("meta") or {}
    payload = {
        "id": msg.get("id"),
        "text": msg.get("text", ""),
        "session_id": meta.get("api_session") or "",
    }
    if routing:
        payload.update(routing)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        LOOP_INGEST_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "X-API-Loop-Internal-Token": API_LOOP_INTERNAL_TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=LOOP_DISPATCH_TIMEOUT_SECONDS) as response:
            response_bytes = response.read(DEPLOYMENT.kelivo.internal_response_max_bytes + 1)
            if len(response_bytes) > DEPLOYMENT.kelivo.internal_response_max_bytes:
                raise LoopDispatchError("loop_response_too_large", True)
            raw = response_bytes.decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_bytes = exc.read(DEPLOYMENT.kelivo.internal_response_max_bytes + 1)
        if len(error_bytes) > DEPLOYMENT.kelivo.internal_response_max_bytes:
            raise LoopDispatchError("loop_response_too_large", True) from None
        try:
            payload = json.loads(error_bytes.decode("utf-8"))
        except Exception:
            payload = {}
        uncertain = bool(payload.get("dispatch_uncertain")) if isinstance(payload, dict) else False
        raise LoopDispatchError("loop_dispatch_uncertain" if uncertain else "loop_explicit_failed", uncertain) from None
    except (TimeoutError, urllib.error.URLError, ConnectionError, OSError):
        raise LoopDispatchError("loop_dispatch_timeout", True) from None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise LoopDispatchError("loop_invalid_ack", True) from None
    if not isinstance(payload, dict):
        raise LoopDispatchError("loop_invalid_ack", True)
    if payload.get("ok") is False:
        raise LoopDispatchError("loop_dispatch_uncertain" if payload.get("dispatch_uncertain") else "loop_explicit_failed",
                                bool(payload.get("dispatch_uncertain")))
    required_ack = ("callback_delivered", "generation_id", "stream_id", "api_session")
    if payload.get("ok") is not True or any(key not in payload for key in required_ack):
        raise LoopDispatchError("correlation_missing", True)
    if payload.get("callback_delivered") is not True:
        raise LoopDispatchError("loop_callback_failed", False)
    if routing:
        expected = {
            "generation_id": routing.get("generation_id"), "stream_id": routing.get("stream_id"),
            "api_session": routing.get("api_session"),
        }
        if any(str(payload.get(key) or "") != str(value or "") for key, value in expected.items()):
            raise LoopDispatchError("loop_ack_correlation_mismatch", False)
    return payload


async def forward_to_loop(msg: dict) -> None:
    try:
        await asyncio.to_thread(_forward_to_loop_sync, msg)
    except Exception as exc:
        print(f"[loop] forward failed: {type(exc).__name__}: {exc}")


async def dispatch_telegram_generation(job: dict, msg: dict) -> None:
    """Dispatch one claimed Telegram job only to loop with explicit correlation IDs."""
    if brain_target() != "loop":
        raise LoopDispatchError("loop_required", False)
    await asyncio.to_thread(_forward_to_loop_sync, msg, {
        "api_session": job["api_session"],
        "stream_id": job["stream_id"],
        "generation_id": job["generation_id"],
        "reply_to": job["reply_to"],
        "channel": "telegram",
        "channel_account": job["external_account_id"],
        "channel_conversation": job["external_conversation_id"],
    })


async def dispatch_human_message(
    text: str,
    *,
    attachments: list | None = None,
    api_session: str = "",
    existing_message: dict | None = None,
    route: bool = True,
    extra_meta: dict | None = None,
) -> dict:
    """Canonical human-message path shared by Web and channel adapters."""
    attachments = attachments or []
    if existing_message is None:
        meta = {"user": "human", "attachments": attachments}
        if api_session:
            meta["api_session"] = api_session
        if extra_meta:
            meta.update(extra_meta)
        msg = save_message("in", "user", text, meta)
    else:
        msg = existing_message
    if route:
        if brain_target() == "loop":
            asyncio.create_task(forward_to_loop(msg))
        else:
            await broadcast(plugin_subs, plugin_payload(msg))
    await broadcast(app_subs, app_payload(msg))
    await broadcast(app_subs, {"type": "typing", "active": True})
    return msg


def telegram_completion_for(msg: dict) -> dict | None:
    """Atomically complete a strongly correlated Telegram reply, if present."""
    if msg.get("kind") != "reply":
        return None
    meta = msg.get("meta") or {}
    result = channel_store.complete_telegram_generation(DB_PATH, meta=meta, text=msg.get("text") or "", kind="reply")
    if result:
        return result
    complete_keys = ("channel_account", "channel_conversation", "api_session", "reply_to")
    if meta.get("channel") == "telegram" and all(meta.get(key) for key in complete_keys) and (meta.get("generation_id") or meta.get("stream_id")):
        return None  # the transactional validator already wrote one fixed-category audit event
    if any(meta.get(key) for key in ("generation_id", "reply_to", "channel_account", "channel_conversation")) or str(meta.get("stream_id") or "").startswith("tg-stream-"):
        channel_store.mark_uncorrelated(DB_PATH, meta)
    return None


def prune_stream_drafts() -> None:
    now = datetime.now(timezone.utc).timestamp()
    stale = [k for k, v in stream_drafts.items() if now - float(v.get("updated_at") or 0) > STREAM_DRAFT_TTL]
    for k in stale:
        stream_drafts.pop(k, None)


async def handle_stream_delta(kind: str, body: dict) -> dict:
    base_kind = kind[:-6] if kind.endswith("_delta") else kind
    if base_kind not in ("thinking", "reply"):
        raise HTTPException(status_code=400, detail="unknown stream kind")
    stream_id = str(body.get("stream_id") or "").strip()
    if not stream_id:
        raise HTTPException(status_code=400, detail="stream_id required")

    done = bool(body.get("done"))
    chunk = str(body.get("text") or "")
    meta = {k: v for k, v in body.items() if k not in ("type", "text", "done", "final_text")}
    meta["stream_id"] = stream_id
    key = (stream_id, base_kind)
    prune_stream_drafts()

    now_ts = datetime.now(timezone.utc).timestamp()
    draft = stream_drafts.get(key)
    if not draft:
        draft = {"text": "", "meta": meta, "ts": now_iso(), "updated_at": now_ts}
        stream_drafts[key] = draft
    draft["text"] += chunk
    if done and isinstance(body.get("final_text"), str):
        draft["text"] = body.get("final_text") or ""
    draft["meta"].update(meta)
    draft["updated_at"] = now_ts

    if not done:
        await broadcast(app_subs, {
            "type": kind,
            "stream_id": stream_id,
            "text": chunk,
            "done": False,
            "ts": draft["ts"],
            "api_session": draft["meta"].get("api_session") or "",
        })
        return {"ok": True, "stream_id": stream_id, "draft": True}

    text = draft.get("text") or ""
    stream_drafts.pop(key, None)
    if not text:
        if (draft.get("meta") or {}).get("channel") == "telegram":
            raise HTTPException(status_code=422, detail="empty Telegram reply")
        return {"ok": True, "stream_id": stream_id, "saved": False}
    final_meta = dict(draft.get("meta") or {})
    completion = telegram_completion_for({"kind": base_kind, "text": text, "meta": final_meta})
    if completion:
        msg = completion["message"]
        if completion.get("duplicate"):
            return {"id": msg["id"], "stream_id": stream_id, "saved": True, "duplicate": True}
    else:
        if final_meta.get("channel") == "telegram":
            raise HTTPException(status_code=409, detail="telegram correlation rejected")
        msg = save_message("out", base_kind, text, final_meta)
    await broadcast(app_subs, {"type": "typing", "active": False})
    await broadcast(app_subs, app_payload(msg))
    if base_kind == "reply" and not app_subs:
        try:
            await push_to_all(notification_from_message(msg))
        except Exception:
            pass
    return {"id": msg["id"], "stream_id": stream_id, "saved": True}


def loop_base_url() -> str:
    parsed = urllib.parse.urlparse(LOOP_INGEST_URL)
    if not parsed.scheme or not parsed.netloc:
        return "http://127.0.0.1:3020"
    return f"{parsed.scheme}://{parsed.netloc}"


def loop_json(path: str, method: str = "GET", body=None):
    data = None
    headers = {"Content-Type": "application/json", "X-API-Loop-Internal-Token": API_LOOP_INTERNAL_TOKEN}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(loop_base_url() + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            response_bytes = resp.read(DEPLOYMENT.kelivo.internal_response_max_bytes + 1)
            if len(response_bytes) > DEPLOYMENT.kelivo.internal_response_max_bytes:
                raise HTTPException(status_code=502, detail="loop response too large")
            raw = response_bytes.decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read(501).decode("utf-8", "replace")[:500]
        raise HTTPException(status_code=exc.code, detail=detail)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"loop proxy error: {exc}")


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def clean_filename(name: str) -> str:
    name = Path(name or "file").name
    name = SAFE_NAME_RE.sub("_", name).strip("._") or "file"
    return name[:80]


def ext_for(name: str, mime: str) -> str:
    ext = Path(name).suffix.lower()
    if ext and re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
        return ext
    guessed = mimetypes.guess_extension((mime or "").split(";", 1)[0].strip())
    return guessed or ".bin"


def save_upload_bytes(data: bytes, name: str, mime: str, prefix: str = "att") -> dict:
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")
    safe = clean_filename(name)
    ext = ext_for(safe, mime)
    stored = f"{prefix}-{secrets.token_urlsafe(10)}{ext}"
    path = UPLOAD_DIR / stored
    path.write_bytes(data)
    kind = "image" if (mime or "").startswith("image/") else ("audio" if (mime or "").startswith("audio/") else "file")
    return {
        "url": f"{PUBLIC_PREFIX}/uploads/{stored}" if PUBLIC_PREFIX else f"/uploads/{stored}",
        "name": safe,
        "size": len(data),
        "mime": mime or "application/octet-stream",
        "kind": kind,
    }


def transcribe_with_command(audio_path: Path, mime: str) -> str:
    """Optional local ASR hook. The command receives <audio_path> <mime> and prints a transcript."""
    if not VOICE_TRANSCRIBE_CMD:
        return ""
    try:
        proc = subprocess.run(
            [VOICE_TRANSCRIBE_CMD, str(audio_path), mime or "application/octet-stream"],
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def minimax_tts_mp3(text: str) -> bytes:
    if not MINIMAX_API_KEY or not MINIMAX_VOICE_ZH:
        raise HTTPException(status_code=503, detail="minimax tts not configured")
    clean = (text or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="empty text")
    clean = clean[:900]
    url = f"{MINIMAX_API_BASE.rstrip('/')}/v1/t2a_v2"
    if MINIMAX_GROUP_ID:
        url += f"?GroupId={MINIMAX_GROUP_ID}"
    payload = {
        "model": MINIMAX_MODEL,
        "text": clean,
        "stream": False,
        "voice_setting": {
            "voice_id": MINIMAX_VOICE_ZH,
            "speed": 1.0,
            "vol": 1.0,
            "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {MINIMAX_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=MINIMAX_TTS_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"minimax tts failed: {exc}")
    audio_hex = (data.get("data") or {}).get("audio")
    if not audio_hex:
        raise HTTPException(status_code=502, detail="minimax tts returned no audio")
    try:
        return bytes.fromhex(audio_hex)
    except ValueError:
        raise HTTPException(status_code=502, detail="bad minimax audio payload")


def sse_data(payload: dict) -> str:
    lines: list[str] = []
    event_id = payload.get("id")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


def sse_ping() -> str:
    payload = {"type": "ping", "ts": datetime.now(timezone.utc).isoformat()}
    return "event: ping\n" + sse_data(payload)


async def sse_stream(subs: set, request: Request, initial: list[dict] | None = None):
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    subs.add(q)
    try:
        yield "retry: 3000\n: connected\n\n"
        for payload in initial or []:
            yield sse_data(payload)
        while True:
            if await request.is_disconnected():
                break
            try:
                payload = await asyncio.wait_for(q.get(), timeout=15)
                yield sse_data(payload)
            except asyncio.TimeoutError:
                yield sse_ping()  # keep the connection alive and let clients watchdog it
    finally:
        subs.discard(q)


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",  # tell nginx not to buffer the stream
    "Connection": "keep-alive",
}


# ---------------------------------------------------------------------------
# auth — one shared Bearer secret on every endpoint (single user)
# ---------------------------------------------------------------------------

def check_auth(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.query_params.get("token")
    if not token or not hmac.compare_digest(token, SECRET):
        raise HTTPException(status_code=401, detail="unauthorized")


def check_kelivo_auth(request: Request) -> None:
    if not DEPLOYMENT.kelivo.enabled:
        raise HTTPException(status_code=404, detail="not found")
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token or not hmac.compare_digest(token, DEPLOYMENT.kelivo.api_key):
        raise HTTPException(status_code=401, detail="unauthorized")


def kelivo_error(status_code: int, category: str, *, retry_after: int | None = None) -> JSONResponse:
    error_type = "authentication_error" if status_code == 401 else (
        "rate_limit_error" if status_code == 429 else "invalid_request_error"
    )
    return JSONResponse(
        {"error": {"message": category, "type": error_type, "param": None, "code": category}},
        status_code=status_code,
        headers={"Retry-After": str(retry_after)} if retry_after is not None else None,
    )


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError("non-finite JSON number")


def _validate_json_complexity(value) -> None:
    nodes = 0
    string_chars = 0
    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 4096 or depth > 32:
            raise kelivo_service.KelivoError(400, "json_too_complex")
        if isinstance(current, str):
            string_chars += len(current)
            if string_chars > 100_000:
                raise kelivo_service.KelivoError(413, "json_strings_too_large")
        elif isinstance(current, dict):
            if len(current) > 128:
                raise kelivo_service.KelivoError(400, "json_object_too_large")
            stack.extend((item, depth + 1) for pair in current.items() for item in pair)
        elif isinstance(current, list):
            if len(current) > 256:
                raise kelivo_service.KelivoError(400, "json_array_too_large")
            stack.extend((item, depth + 1) for item in current)


KELIVO_GENERATOR = (
    kelivo_service.LoopGenerationClient(
        LOOP_INGEST_URL, LOOP_MODEL_TOTAL_TIMEOUT_SECONDS, API_LOOP_INTERNAL_TOKEN,
        DEPLOYMENT.kelivo.internal_response_max_bytes,
    ).generate
    if DEPLOYMENT.kelivo.enabled else None
)
KELIVO_ADMISSION = kelivo_service.KelivoAdmissionController(
    DEPLOYMENT.kelivo.global_concurrency,
    DEPLOYMENT.kelivo.client_concurrency,
    DEPLOYMENT.kelivo.queue_timeout_seconds,
)
KELIVO_KEY_LOCKS = kelivo_service.IdempotencyLockRegistry()


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

def _request_worker_restart() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def _telegram_worker_done(task: asyncio.Task) -> None:
    if not DEPLOYMENT.render_telegram_mvp or getattr(app.state, "shutting_down", False):
        return
    if not task.cancelled():
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
    if getattr(app.state, "worker_restart_requested", False):
        return
    app.state.worker_restart_requested = True
    print("[telegram-worker] terminal task exit; requesting process restart", flush=True)
    _request_worker_restart()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    worker_task = None
    app.state.shutting_down = False
    app.state.worker_restart_requested = False
    app.state.telegram_worker_task = None
    if TELEGRAM.enabled:
        worker = TelegramWorker(DB_PATH, TELEGRAM, dispatch_telegram_generation)
        app.state.telegram_worker = worker
        worker_task = asyncio.create_task(worker.run_forever())
        worker_task.add_done_callback(_telegram_worker_done)
        app.state.telegram_worker_task = worker_task
    try:
        yield
    finally:
        app.state.shutting_down = True
        if worker_task:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
        app.state.telegram_worker_task = None


app = FastAPI(
    lifespan=lifespan,
    docs_url=None if DEPLOYMENT.render_telegram_mvp else "/docs",
    redoc_url=None if DEPLOYMENT.render_telegram_mvp else "/redoc",
    openapi_url=None if DEPLOYMENT.render_telegram_mvp else "/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


def _database_ready() -> bool:
    try:
        uri = Path(DB_PATH).resolve(strict=False).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            required_tables = {
                "messages", "schema_migrations", "generation_jobs", "delivery_attempts",
                "telegram_completions", "delivery_parts", "external_messages",
                "channel_accounts", "channel_conversations", "inbound_events",
            }
            if DEPLOYMENT.kelivo.enabled:
                required_tables.update({
                    "kelivo_clients", "kelivo_requests", "companion_context_snapshots", "kelivo_rate_limits"
                })
            found = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN (%s)"
                    % ",".join("?" for _ in required_tables), tuple(required_tables)
                )
            }
            if found != required_tables:
                return False
            latest = channel_store.MIGRATIONS[-1][0]
            row = conn.execute(
                "SELECT status FROM schema_migrations WHERE version=?", (latest,)
            ).fetchone()
            if row is None or row[0] != "applied":
                return False
            if DEPLOYMENT.kelivo.enabled:
                channel_store.validate_kelivo_schema(conn)
            return True
    except (OSError, sqlite3.Error, ValueError):
        return False


async def _api_loop_ready() -> bool:
    if DEPLOYMENT.render_telegram_mvp and not API_LOOP_EXPECTED_NONCE:
        return False
    try:
        async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{DEPLOYMENT.loop_port}/healthz")
        if response.status_code < 200 or response.status_code >= 300:
            return False
        payload = response.json()
        expected = API_LOOP_EXPECTED_NONCE
        nonce_matches = (
            isinstance(payload, dict)
            and isinstance(payload.get("instance_nonce"), str)
            and (not DEPLOYMENT.render_telegram_mvp or hmac.compare_digest(payload["instance_nonce"], expected))
        )
        return bool(payload.get("ok") is True and payload.get("service") == "api_loop" and nonce_matches)
    except Exception:
        return False


def _persistent_paths_ready() -> bool:
    if not DEPLOYMENT.render_telegram_mvp:
        return True
    paths = (DEPLOYMENT.db_path, DEPLOYMENT.upload_dir, DEPLOYMENT.brain_file, DEPLOYMENT.loop_config)
    if not all(deployment_config.path_within_root(path, DEPLOYMENT.persistent_root) for path in paths):
        return False
    return (
        DEPLOYMENT.db_path.is_file()
        and DEPLOYMENT.upload_dir.is_dir()
        and DEPLOYMENT.brain_file.is_file()
        and DEPLOYMENT.loop_config.parent.is_dir()
        and DEPLOYMENT.db_path.parent.is_dir()
    )


@app.get("/readyz")
async def readyz():
    worker_task = getattr(app.state, "telegram_worker_task", None)
    checks = {
        "database": await asyncio.to_thread(_database_ready),
        "persistent_path": await asyncio.to_thread(_persistent_paths_ready),
        "brain_target": brain_target() == "loop",
        "telegram_config": TELEGRAM.requested and TELEGRAM.enabled,
        "telegram_worker": worker_task is not None and not worker_task.done(),
        "api_loop": await _api_loop_ready(),
    }
    if DEPLOYMENT.kelivo.enabled:
        checks["kelivo"] = await asyncio.to_thread(
            kelivo_service.client_mapping_ready,
            DB_PATH,
            DEPLOYMENT.kelivo.client_id,
            DEPLOYMENT.kelivo.api_session,
        )
    ready = all(checks.values())
    payload = {"ready": ready, "checks": checks, "status": "ready" if ready else "not_ready"}
    return JSONResponse(payload, status_code=200 if ready else 503)


# ---- Kelivo OpenAI-compatible API ----------------------------------------

@app.get("/v1/models")
async def kelivo_models(request: Request):
    try:
        check_kelivo_auth(request)
    except HTTPException as exc:
        return kelivo_error(exc.status_code, "unauthorized" if exc.status_code == 401 else "not_found")
    return {
        "object": "list",
        "data": [{
            "id": DEPLOYMENT.kelivo.model_alias,
            "object": "model",
            "created": 0,
            "owned_by": "ouou-home",
        }],
    }


@app.post("/v1/chat/completions")
async def kelivo_chat_completions(request: Request):
    try:
        check_kelivo_auth(request)
    except HTTPException as exc:
        return kelivo_error(exc.status_code, "unauthorized" if exc.status_code == 401 else "not_found")
    encoding = request.headers.get("content-encoding", "").strip().lower()
    if encoding not in {"", "identity"}:
        return kelivo_error(415, "content_encoding_not_supported")
    content_length = request.headers.get("content-length")
    try:
        if content_length and (
            not content_length.isascii() or not content_length.isdecimal()
        ):
            return kelivo_error(400, "invalid_content_length")
        if content_length and int(content_length) > kelivo_service.MAX_BODY_BYTES:
            return kelivo_error(413, "request_body_too_large")
    except ValueError:
        return kelivo_error(400, "invalid_content_length")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > kelivo_service.MAX_BODY_BYTES:
            return kelivo_error(413, "request_body_too_large")
    try:
        payload = json.loads(
            bytes(raw), object_pairs_hook=_reject_duplicate_json_keys, parse_constant=_reject_json_constant
        )
        _validate_json_complexity(payload)
        validated = kelivo_service.validate_completion(payload, DEPLOYMENT.kelivo.model_alias)
        raw_idempotency_key = request.headers.get("idempotency-key")
        idempotency_mode = "explicit"
        if raw_idempotency_key is None and DEPLOYMENT.kelivo.auto_idempotency_enabled:
            idempotency_mode = "automatic"
            idempotency_key = ""
        else:
            # Present-but-empty/invalid values never fall through to compatibility mode.
            idempotency_key = kelivo_service.validate_idempotency_key(raw_idempotency_key)
        provider_defaults = await asyncio.to_thread(
            deployment_config.resolve_kelivo_provider_contract_defaults,
            os.environ, DEPLOYMENT.loop_config,
        )
        effective_temperature = (
            validated.temperature if validated.temperature is not None else provider_defaults.temperature
        )
        effective_max_tokens = (
            validated.max_tokens if validated.max_tokens is not None else provider_defaults.max_tokens
        )
        await asyncio.to_thread(
            kelivo_service.recover_dispatching_requests, DB_PATH,
            stale_seconds=DEPLOYMENT.kelivo.stale_dispatch_seconds,
            category="dispatch_expired",
        )
        automatic_contract = None
        if idempotency_mode == "automatic":
            automatic_contract = await asyncio.to_thread(
                kelivo_service.freeze_automatic_request,
                DB_PATH, DEPLOYMENT.kelivo.client_id, validated,
                persona_text=KELIVO_PERSONA, persona_source=KELIVO_PERSONA_SOURCE,
                provider_model=provider_defaults.provider_model,
                effective_temperature=effective_temperature, effective_max_tokens=effective_max_tokens,
            )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        return kelivo_error(400, "malformed_json")
    except MemoryError:
        return kelivo_error(413, "request_body_too_large")
    except kelivo_service.KelivoError as exc:
        return kelivo_error(exc.status_code, exc.category, retry_after=exc.retry_after)
    except deployment_config.DeploymentConfigError:
        return kelivo_error(503, "provider_contract_unavailable")
    except (sqlite3.Error, OSError):
        return kelivo_error(503, "temporarily_unavailable")
    try:
        lock_key = (
            automatic_contract.request_identity_hash
            if automatic_contract is not None else idempotency_key
        )
        async with KELIVO_KEY_LOCKS.hold(DEPLOYMENT.kelivo.client_id, lock_key):
            if automatic_contract is not None:
                prepared = await asyncio.to_thread(
                    kelivo_service.lookup_automatic_request,
                    DB_PATH, DEPLOYMENT.kelivo.client_id, automatic_contract.request_identity_hash,
                )
            else:
                prepared = await asyncio.to_thread(
                    kelivo_service.lookup_request, DB_PATH, DEPLOYMENT.kelivo.client_id,
                    idempotency_key, validated, KELIVO_PERSONA,
                    provider_defaults.provider_model, effective_temperature, effective_max_tokens,
                )
            if prepared is None:
                if automatic_contract is not None:
                    prepare_task = asyncio.create_task(asyncio.to_thread(
                        kelivo_service.prepare_automatic_request,
                        DB_PATH, DEPLOYMENT.kelivo.client_id,
                        automatic_contract.request_identity_hash, validated, automatic_contract,
                        persona_source=KELIVO_PERSONA_SOURCE,
                        provider_model=provider_defaults.provider_model,
                        effective_temperature=effective_temperature,
                        effective_max_tokens=effective_max_tokens,
                        replay_seconds=DEPLOYMENT.kelivo.auto_idempotency_replay_seconds,
                        rate_limit=DEPLOYMENT.kelivo.rate_limit_per_minute, rate_window_seconds=60,
                    ))
                else:
                    prepare_task = asyncio.create_task(asyncio.to_thread(
                        kelivo_service.prepare_request, DB_PATH, DEPLOYMENT.kelivo.client_id,
                        idempotency_key, validated, persona_text=KELIVO_PERSONA,
                        persona_source=KELIVO_PERSONA_SOURCE,
                        provider_model=provider_defaults.provider_model,
                        effective_temperature=effective_temperature,
                        effective_max_tokens=effective_max_tokens,
                        rate_limit=DEPLOYMENT.kelivo.rate_limit_per_minute, rate_window_seconds=60,
                    ))
                try:
                    prepared = await asyncio.shield(prepare_task)
                except asyncio.CancelledError:
                    prepared = await asyncio.shield(prepare_task)
                    if prepared.idempotency_key:
                        idempotency_key = prepared.idempotency_key
                    if prepared.action == "prepared":
                        await asyncio.to_thread(
                            kelivo_service.fail_request, DB_PATH, DEPLOYMENT.kelivo.client_id,
                            idempotency_key, "request_cancelled_before_dispatch", False,
                        )
                    raise
            if prepared.idempotency_key:
                idempotency_key = prepared.idempotency_key
    except kelivo_service.KelivoError as exc:
        return kelivo_error(exc.status_code, exc.category, retry_after=exc.retry_after)
    except (sqlite3.Error, OSError):
        return kelivo_error(503, "temporarily_unavailable")
    if prepared.action == "replay":
        return JSONResponse(prepared.response)
    if prepared.action == "blocked":
        return kelivo_error(409, prepared.error_category or "request_not_dispatchable")
    lease = None
    try:
        lease = await KELIVO_ADMISSION.acquire(DEPLOYMENT.kelivo.client_id)
        prepared = await asyncio.to_thread(
            kelivo_service.begin_dispatch, DB_PATH, DEPLOYMENT.kelivo.client_id,
            idempotency_key, stale_seconds=DEPLOYMENT.kelivo.stale_dispatch_seconds,
        )
    except kelivo_service.KelivoError as exc:
        if lease:
            lease.release()
        try:
            await asyncio.to_thread(
                kelivo_service.fail_request, DB_PATH, DEPLOYMENT.kelivo.client_id,
                idempotency_key, exc.category, False,
            )
        except Exception:
            pass
        return kelivo_error(exc.status_code, exc.category, retry_after=exc.retry_after)
    except (sqlite3.Error, OSError):
        if lease:
            lease.release()
        try:
            await asyncio.to_thread(
                kelivo_service.fail_request, DB_PATH, DEPLOYMENT.kelivo.client_id,
                idempotency_key, "dispatch_state_unavailable", False,
            )
        except Exception:
            pass
        return kelivo_error(503, "temporarily_unavailable")
    except asyncio.CancelledError:
        if lease:
            lease.release()
        try:
            await asyncio.shield(asyncio.to_thread(
                kelivo_service.fail_request, DB_PATH, DEPLOYMENT.kelivo.client_id,
                idempotency_key, "request_cancelled_before_dispatch", False,
            ))
        except Exception:
            pass
        raise
    if prepared.action == "replay":
        lease.release()
        return JSONResponse(prepared.response)
    if prepared.action == "blocked":
        lease.release()
        return kelivo_error(409, prepared.error_category or "request_not_dispatchable")
    dispatch_started = False
    try:
        if KELIVO_GENERATOR is None:
            raise kelivo_service.GenerationError("generation_service_unavailable", False)
        dispatch_started = True
        result = await KELIVO_GENERATOR(
            prepared.messages,
            prepared.api_session,
            prepared.provider_model,
            prepared.temperature,
            prepared.max_tokens,
            prepared.context_bundle or {},
        )
        completion_task = asyncio.create_task(asyncio.to_thread(
            kelivo_service.complete_request, DB_PATH, DEPLOYMENT.kelivo.client_id,
            idempotency_key, DEPLOYMENT.kelivo.model_alias, result,
        ))
        try:
            response = await asyncio.shield(completion_task)
        except asyncio.CancelledError:
            await asyncio.shield(completion_task)
            raise
    except kelivo_service.GenerationError as exc:
        try:
            await asyncio.to_thread(
                kelivo_service.fail_request, DB_PATH, DEPLOYMENT.kelivo.client_id,
                idempotency_key, exc.category, exc.uncertain
            )
        except Exception:
            pass
        return kelivo_error(504 if exc.uncertain else 502, exc.category)
    except kelivo_service.KelivoError as exc:
        return kelivo_error(exc.status_code, exc.category, retry_after=exc.retry_after)
    except asyncio.CancelledError:
        if dispatch_started:
            try:
                await asyncio.shield(asyncio.to_thread(
                    kelivo_service.fail_request, DB_PATH, DEPLOYMENT.kelivo.client_id,
                    idempotency_key, "client_cancelled_after_dispatch", True,
                ))
            except Exception:
                pass
        raise
    except Exception:
        try:
            await asyncio.to_thread(
                kelivo_service.fail_request, DB_PATH, DEPLOYMENT.kelivo.client_id, idempotency_key,
                "unexpected_generation_error", True,
            )
        except Exception:
            pass
        return kelivo_error(504, "unexpected_generation_error")
    finally:
        if lease:
            lease.release()
    return JSONResponse(response)


# ---- AI side ---------------------------------------------------------------

@app.get("/channel/in")
async def channel_in(request: Request, since: int = 0, limit: int = 100):
    """SSE stream the plugin holds open. The human's messages get pushed down here."""
    check_auth(request)
    backlog = [plugin_payload(m) for m in inbound_history(since, min(limit, 500))]
    return StreamingResponse(sse_stream(plugin_subs, request, backlog), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/channel/out")
async def channel_out(request: Request):
    """The AI's reply/react. Persist + fan out to the PWA."""
    check_auth(request)
    body = await request.json()
    kind = body.get("type", "reply")
    if kind in ("thinking_delta", "reply_delta"):
        return await handle_stream_delta(kind, body)
    if kind == "react":
        # An emoji reaction attached to an existing message's meta.reactions; no new
        # message is created. An empty emoji clears that reaction.
        try:
            target_id = int(body.get("id"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="react: numeric id required")
        emoji = (body.get("emoji") or "").strip()
        reactions = set_reaction(target_id, "ai", emoji)
        if reactions is None:
            raise HTTPException(status_code=404, detail="react: message not found")
        await broadcast(app_subs, {"type": "reaction", "id": target_id, "reactions": reactions, "by": "ai"})
        # A react is also the AI "acting" once — clear the typing indicator so the
        # header doesn't stay stuck typing when no reply follows.
        await broadcast(app_subs, {"type": "typing", "active": False})
        return {"id": target_id, "reactions": reactions}
    text = body.get("text", "")
    meta = {k: v for k, v in body.items() if k not in ("type", "text")}
    completion = telegram_completion_for({"kind": kind, "text": text, "meta": meta})
    if completion:
        msg = completion["message"]
        if completion.get("duplicate"):
            return {"id": msg["id"], "duplicate": True}
    else:
        if meta.get("channel") == "telegram":
            raise HTTPException(status_code=409, detail="telegram correlation rejected")
        msg = save_message("out", kind, text, meta)
    # the AI replied — clear the typing state
    await broadcast(app_subs, {"type": "typing", "active": False})
    await broadcast(app_subs, app_payload(msg))
    # Unread push: only when no PWA tab is holding the stream (app_subs empty);
    # only push real replies, not 'thinking' chatter.
    if kind == "reply" and not app_subs:
        try:
            await push_to_all(notification_from_message(msg))
        except Exception:
            pass  # a push failure must never affect persistence/fan-out
    return {"id": msg["id"]}


# ---- human side ------------------------------------------------------------

@app.post("/app/send")
async def app_send(request: Request):
    """Human types in the PWA. Persist, push to the AI (plugin), echo to other PWA tabs."""
    check_auth(request)
    body = await request.json()
    text = (body.get("text") or "").strip()
    attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else []
    api_session = str(body.get("api_session") or body.get("session_id") or "").strip()
    if not text and not attachments:
        raise HTTPException(status_code=400, detail="empty text")
    msg = await dispatch_human_message(text, attachments=attachments, api_session=api_session)
    return {"id": msg["id"]}


@app.post("/integrations/telegram/webhook")
async def telegram_webhook(request: Request):
    if not TELEGRAM.enabled:
        raise HTTPException(status_code=404, detail="integration disabled")
    received = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not received or not hmac.compare_digest(received, TELEGRAM.webhook_secret):
        raise HTTPException(status_code=401, detail="invalid webhook authentication")
    content_length = request.headers.get("content-length", "")
    try:
        if content_length and int(content_length) > TELEGRAM.webhook_max_body_bytes:
            return Response(content='{"ok":false,"error":"body_too_large"}', status_code=413, media_type="application/json")
    except ValueError:
        return Response(content='{"ok":false,"error":"malformed_request"}', status_code=400, media_type="application/json")
    raw_buffer = bytearray()
    async for chunk in request.stream():
        raw_buffer.extend(chunk)
        if len(raw_buffer) > TELEGRAM.webhook_max_body_bytes:
            return Response(content='{"ok":false,"error":"body_too_large"}', status_code=413, media_type="application/json")
    raw = bytes(raw_buffer)
    try:
        body = json.loads(raw)
    except Exception:
        return Response(content='{"ok":false,"error":"malformed_json"}', status_code=400, media_type="application/json")
    update, error = validate_update(TELEGRAM, body)
    if error:
        public_reason = {
            "unsupported_update": "unsupported_update", "not_allowed": "not_allowed",
            "private_chat_required": "not_private", "commands_not_supported": "command",
            "text_required": "empty_text", "text_too_long": "text_too_long",
            "bot_sender": "bot_sender", "malformed_update": "malformed_update",
        }.get(error, "unsupported_update")
        return {"ok": True, "ignored": True, "reason": public_reason}
    try:
        result = channel_store.enqueue_telegram_update(
            DB_PATH, account_id=TELEGRAM.account_id, update_id=update["update_id"],
            chat_id=update["chat_id"], user_id=update["user_id"],
            external_message_id=update["external_message_id"], text=update["text"],
            rate_limit=20, rate_window_seconds=60,
        )
    except (sqlite3.Error, OSError):
        return Response(content='{"ok":false,"error":"temporarily_unavailable"}', status_code=503, media_type="application/json")
    if result.get("rejected") == "rate_limited":
        return {"ok": True, "ignored": True, "reason": "rate_limited"}
    if result.get("duplicate"):
        response = {"ok": True, "duplicate": True}
        if result.get("ignored"):
            response.update({"ignored": True, "reason": result.get("reason") or "duplicate"})
        return response
    try:
        await dispatch_human_message(update["text"], api_session=result["api_session"],
                                     existing_message=result["message"], route=False)
    except Exception:
        print("[telegram] sse_broadcast_failed")
    return {"ok": True, "queued": True}


@app.post("/app/upload")
async def app_upload(request: Request, name: str = "file"):
    check_auth(request)
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    mime = request.headers.get("content-type", "application/octet-stream")
    return save_upload_bytes(data, name, mime, "att")


@app.get("/uploads/{name}")
async def uploads(request: Request, name: str):
    check_auth(request)
    safe = clean_filename(name)
    path = UPLOAD_DIR / safe
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)


@app.post("/app/voice")
async def app_voice(request: Request):
    """Voice input from the PWA. Prefer the browser transcript; fall back to an audio attachment."""
    check_auth(request)
    ctype = request.headers.get("content-type", "")

    if ctype.startswith("application/json"):
        body = await request.json()
        transcript = (body.get("text") or body.get("transcript") or "").strip()
        if not transcript:
            raise HTTPException(status_code=400, detail="empty transcript")
        if not transcript.startswith("🎤"):
            transcript = "🎤 " + transcript
        meta = {"user": "human", "voice": True, "source": body.get("source") or "browser_speech"}
        msg = save_message("in", "voice", transcript, meta)
        await broadcast(plugin_subs, plugin_payload(msg))
        await broadcast(app_subs, app_payload(msg))
        await broadcast(app_subs, {"type": "typing", "active": True})
        return {"id": msg["id"], "text": transcript}

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    if len(data) > VOICE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="voice too large")

    mime = ctype or "audio/webm"
    upload = save_upload_bytes(data, request.query_params.get("name", "voice.webm"), mime, "voice")
    stored = Path(upload["url"]).name
    local_audio = UPLOAD_DIR / stored
    transcript = transcribe_with_command(local_audio, mime)
    text = ("🎤 " + transcript) if transcript else f"🎤 [语音] {HUMAN_NAME}发来一段语音；当前 relay 未配置 ASR，音频已作为附件送达。"
    meta = {
        "user": "human",
        "voice": True,
        "source": "media_recorder",
        "attachments": [upload],
        "transcribed": bool(transcript),
    }
    msg = save_message("in", "voice", text, meta)
    await broadcast(plugin_subs, plugin_payload(msg))
    await broadcast(app_subs, app_payload(msg))
    await broadcast(app_subs, {"type": "typing", "active": True})
    return {"id": msg["id"], "text": transcript, "attachment": upload}


@app.post("/app/call")
async def app_call(request: Request):
    """Call lifecycle events from the PWA so the AI knows this is voice, not typing."""
    check_auth(request)
    body = await request.json()
    action = (body.get("action") or "").strip().lower()
    call_id = (body.get("call_id") or "").strip()
    if action not in {"start", "end"}:
        raise HTTPException(status_code=400, detail="invalid call action")
    if action == "start":
        text = f"📞 [call_start] {HUMAN_NAME}开启了语音通话。接下来带 🎤 的消息来自语音。请用适合朗读的短句回复。"
    else:
        text = f"📞 [call_end] {HUMAN_NAME}结束了语音通话。"
    msg = save_message("in", "call", text, {"user": "human", "call": action, "call_id": call_id})
    if action == "end":
        await broadcast(plugin_subs, plugin_payload(msg))
    if action == "start":
        await broadcast(app_subs, {"type": "typing", "active": True})
    return {"id": msg["id"]}


@app.post("/app/tts")
async def app_tts(request: Request):
    """Generate MiniMax speech for an AI reply. The frontend falls back if unavailable."""
    check_auth(request)
    body = await request.json()
    audio = minimax_tts_mp3(body.get("text") or "")
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# presence — the PWA POSTs /app/ping every ~60s; read /app/status to decide
# whether the human is around. In-memory only: a relay restart clears last_seen
# (state degrades to 'unknown') until the next ping.
# ---------------------------------------------------------------------------

_last_seen_ts = None


def _presence_state(now):
    if _last_seen_ts is None:
        return "unknown", None
    age = (now - _last_seen_ts).total_seconds()
    if age < PRESENCE_ONLINE_SEC:
        return "online", age
    if age < PRESENCE_RECENT_SEC:
        return "recent", age
    return "away", age


def latest_message():
    """Newest real conversational message (excludes 'thinking' stream)."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE kind != 'thinking' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return rows_to_messages([row])[0]


@app.post("/app/ping")
async def app_ping(request: Request):
    """PWA foreground heartbeat."""
    check_auth(request)
    global _last_seen_ts
    _last_seen_ts = datetime.now(timezone.utc)
    return {"ok": True}


@app.get("/app/status")
async def app_status(request: Request):
    """Presence state + the time/direction of the most recent message. Metadata only, no message text."""
    check_auth(request)
    now = datetime.now(timezone.utc)
    state, seen_age = _presence_state(now)
    last_msg = latest_message()
    last_msg_ts = last_msg["ts"] if last_msg else None
    last_msg_dir = last_msg["direction"] if last_msg else None
    last_msg_age = None
    if last_msg_ts:
        try:
            mt = datetime.fromisoformat(last_msg_ts)
            if mt.tzinfo is None:
                mt = mt.replace(tzinfo=timezone.utc)
            last_msg_age = (now - mt).total_seconds()
        except Exception:
            last_msg_age = None
    return {
        "now": now.isoformat(),
        "last_seen": _last_seen_ts.isoformat() if _last_seen_ts else None,
        "seen_age_sec": seen_age,
        "online": state == "online",
        "state": state,
        "last_msg_ts": last_msg_ts,
        "last_msg_dir": last_msg_dir,
        "last_msg_age_sec": last_msg_age,
    }


@app.get("/app/history")
async def app_history(request: Request, since: int = 0, limit: int = 200, session_id: str = ""):
    check_auth(request)
    rows = history_for_session(session_id, since, min(limit, 500)) if session_id else history(since, min(limit, 500))
    return {"messages": [app_payload(m) for m in rows]}


@app.get("/app/stream")
async def app_stream(request: Request):
    """SSE stream the PWA holds open while foregrounded. The AI's messages arrive here."""
    check_auth(request)
    return StreamingResponse(sse_stream(app_subs, request), media_type="text/event-stream", headers=SSE_HEADERS)


# ---- web push subscription management --------------------------------------

@app.get("/app/vapid_public")
async def app_vapid_public(request: Request):
    """Public key the PWA needs to subscribe (not a secret — safe to expose)."""
    check_auth(request)
    return {"key": VAPID_PUBLIC_KEY}


@app.post("/app/subscribe")
async def app_subscribe(request: Request):
    """PWA turns on lock-screen notifications: store the subscription."""
    check_auth(request)
    body = await request.json()
    endpoint = (body.get("endpoint") or "").strip()
    keys = body.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="endpoint + keys.p256dh + keys.auth required")
    ua = request.headers.get("user-agent", "")[:200]
    save_subscription(endpoint, p256dh, auth, ua)
    return {"ok": True, "count": len(list_subscriptions())}


@app.post("/app/unsubscribe")
async def app_unsubscribe(request: Request):
    """PWA turns off lock-screen notifications: drop the subscription."""
    check_auth(request)
    body = await request.json()
    endpoint = (body.get("endpoint") or "").strip()
    if endpoint:
        delete_subscription(endpoint)
    return {"ok": True}


@app.post("/app/push_test")
async def app_push_test(request: Request):
    """Self-test: push one test notification to every subscription."""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = (body.get("text") if isinstance(body, dict) else None) or f"测试通知 · {AI_NAME}在这儿"
    res = await push_to_all({"title": AI_NAME, "body": text, "url": APP_PATH, "id": 0})
    return {"ok": True, **res}


# ---- optional API loop control --------------------------------------------

@app.get("/app/brain")
async def get_brain(request: Request):
    check_auth(request)
    return {"target": brain_target()}


@app.post("/app/brain")
async def set_brain(request: Request):
    check_auth(request)
    body = await request.json()
    raw_target = body.get("target") if isinstance(body, dict) else None
    if DEPLOYMENT.render_telegram_mvp:
        if not isinstance(raw_target, str) or raw_target != "loop":
            raise HTTPException(status_code=400, detail="brain_target_loop_required")
        target = raw_target
    else:
        target = str(raw_target or "").strip()
    if target not in ("desktop", "loop"):
        raise HTTPException(status_code=400, detail="target must be 'desktop' or 'loop'")
    deployment_config.atomic_write_text(BRAIN_FILE, target + "\n")
    return {"target": target}


@app.get("/app/loop_config")
async def get_loop_config(request: Request):
    check_auth(request)
    return loop_json("/loop/config")


@app.post("/app/loop_config")
async def set_loop_config(request: Request):
    check_auth(request)
    body = await request.json()
    try:
        deployment_config.validate_loop_config_update_request(
            body, render_mvp=DEPLOYMENT.render_telegram_mvp
        )
    except deployment_config.DeploymentConfigError as exc:
        raise HTTPException(status_code=400, detail=exc.category) from None
    return loop_json("/loop/config", method="POST", body=body)


@app.get("/app/sessions")
async def app_sessions(request: Request):
    check_auth(request)
    return loop_json("/loop/sessions")


@app.post("/app/sessions")
async def app_sessions_create(request: Request):
    check_auth(request)
    body = await request.json()
    if "since_id" not in body:
        try:
            with db() as conn:
                row = conn.execute("SELECT MAX(id) AS id FROM messages").fetchone()
                body["since_id"] = int(row["id"] or 0)
        except Exception:
            body["since_id"] = 0
    return loop_json("/loop/sessions", method="POST", body=body)


@app.patch("/app/sessions/{session_id}")
async def app_sessions_patch(session_id: str, request: Request):
    check_auth(request)
    return loop_json(f"/loop/sessions/{urllib.parse.quote(session_id)}", method="PATCH", body=await request.json())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, access_log=False)
