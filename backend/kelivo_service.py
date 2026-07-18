"""Kelivo OpenAI-compatible validation, frozen dispatch state, and generation adapter."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import httpx

try:
    from . import channel_store
except ImportError:
    import channel_store


MAX_BODY_BYTES = 128 * 1024
MAX_MESSAGES = 100
MAX_CONTENT_CHARS = 32_000
MAX_TOTAL_CONTENT_CHARS = 96_000
MAX_MAX_TOKENS = 32_768
MAX_IDEMPOTENCY_KEY_CHARS = 128
IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
ALLOWED_ROLES = frozenset({"system", "developer", "user", "assistant"})
ALLOWED_REQUEST_KEYS = frozenset({
    "model", "messages", "tools", "temperature", "max_tokens", "stream", "stream_options",
})
PROMPT_CONTRACT_VERSION = "kelivo-provider-prompt-v1"


class KelivoError(Exception):
    def __init__(self, status_code: int, category: str, *, retry_after: int | None = None):
        super().__init__(category)
        self.status_code = status_code
        self.category = category
        self.retry_after = retry_after


class GenerationError(Exception):
    def __init__(self, category: str, uncertain: bool):
        super().__init__(category)
        self.category = category
        self.uncertain = uncertain


@dataclass(frozen=True)
class ValidatedCompletion:
    normalized_request: dict[str, Any]
    request_payload_hash: str
    messages: tuple[dict[str, str], ...]
    user_text: str
    snapshots: tuple[tuple[str, Any], ...]
    temperature: float | None
    max_tokens: int | None


@dataclass(frozen=True)
class PreparedRequest:
    action: str
    generation_id: str
    api_session: str
    provider_model: str = ""
    temperature: float = 0.7
    max_tokens: int = 2000
    messages: tuple[dict[str, str], ...] = ()
    context_bundle: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    error_category: str | None = None


GenerationCallable = Callable[
    [tuple[dict[str, str], ...], str, str, float, int, dict[str, Any]], Awaitable[dict[str, Any]]
]


def normalized_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> tuple[str, str]:
    encoded = normalized_json(value)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_request_identity_hash(
    *, virtual_model: str, provider_model: str, client_id: str, api_session: str,
    mapping_revision: int, persona_hash: str, snapshot_correlations: dict[str, Any],
    provider_messages: tuple[dict[str, str], ...] | list[dict[str, str]],
    effective_temperature: float, effective_max_tokens: int,
) -> str:
    """Build the sole deterministic identity used by prepare, lookup, and tests."""
    hash_input = {
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "model": virtual_model,
        "provider_model": provider_model,
        "client_id": client_id,
        "api_session": api_session,
        "mapping_revision": mapping_revision,
        "persona_hash": persona_hash,
        "snapshots": snapshot_correlations,
        "provider_messages": list(provider_messages),
        "effective_temperature": effective_temperature,
        "effective_max_tokens": effective_max_tokens,
    }
    return content_hash(hash_input)[1]


def _snapshot_correlations(bundle: dict[str, Any]) -> dict[str, Any]:
    snapshots = bundle.get("snapshots") if isinstance(bundle, dict) else None
    if not isinstance(snapshots, dict):
        raise KelivoError(503, "stored_contract_invalid")
    return {
        key: None if snapshots.get(key) is None else {
            "id": snapshots[key]["id"], "version": snapshots[key]["version"],
            "hash": snapshots[key]["hash"],
        }
        for key in ("system", "developer")
    }


def build_frozen_prompt_contract(
    validated: ValidatedCompletion, *, persona_text: str, persona_source: str,
    client_id: str, api_session: str, mapping_revision: int, provider_model: str,
    effective_temperature: float, effective_max_tokens: int, snapshot_rows: list[sqlite3.Row],
) -> tuple[tuple[dict[str, str], ...], dict[str, Any], str, str]:
    """Build the sole persisted contract used for hashing and provider dispatch."""
    persona_hash = text_sha256(persona_text)
    provider_messages = tuple(
        ([{"role": "system", "content": persona_text}] if persona_text else [])
        + [dict(message) for message in validated.messages]
    )
    snapshots_by_type = {
        row["snapshot_type"]: {
            "id": row["id"], "version": row["version"], "hash": row["content_hash"],
            "value": json.loads(row["normalized_json"]),
        }
        for row in snapshot_rows
    }
    bundle = {
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "persona": {"text": persona_text, "hash": persona_hash, "source": persona_source},
        "snapshots": {
            "system": snapshots_by_type.get("system"),
            "developer": snapshots_by_type.get("developer"),
        },
        "provider_model": provider_model,
        "provider_messages": list(provider_messages),
        "effective_temperature": effective_temperature,
        "effective_max_tokens": effective_max_tokens,
    }
    bundle_json, bundle_hash = content_hash(bundle)
    request_identity_hash = build_request_identity_hash(
        virtual_model=validated.normalized_request["model"], provider_model=provider_model,
        client_id=client_id, api_session=api_session, mapping_revision=mapping_revision,
        persona_hash=persona_hash, snapshot_correlations=_snapshot_correlations(bundle),
        provider_messages=provider_messages, effective_temperature=effective_temperature,
        effective_max_tokens=effective_max_tokens,
    )
    return provider_messages, json.loads(bundle_json), bundle_hash, request_identity_hash


def normalize_snapshot_text(value: str) -> str:
    """Normalize line endings only; model/canonical message strings stay byte-for-byte equivalent."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def validate_completion(payload: Any, model_alias: str) -> ValidatedCompletion:
    if not isinstance(payload, dict):
        raise KelivoError(400, "invalid_request_body")
    if set(payload) - ALLOWED_REQUEST_KEYS:
        raise KelivoError(422, "unsupported_request_field")
    if payload.get("model") != model_alias:
        raise KelivoError(404, "model_not_found")
    if payload.get("stream", False) is not False:
        raise KelivoError(400 if payload.get("stream") is True else 422,
                          "streaming_not_supported" if payload.get("stream") is True else "invalid_stream")

    stream_options = payload.get("stream_options")
    if stream_options is not None and (
        not isinstance(stream_options, dict)
        or set(stream_options) - {"include_usage"}
        or ("include_usage" in stream_options and not isinstance(stream_options["include_usage"], bool))
    ):
        raise KelivoError(422, "invalid_stream_options")

    source_messages = payload.get("messages")
    if not isinstance(source_messages, list) or not source_messages or len(source_messages) > MAX_MESSAGES:
        raise KelivoError(422, "invalid_messages")
    messages: list[dict[str, str]] = []
    system_context: list[dict[str, str]] = []
    developer_context: list[dict[str, str]] = []
    total_chars = 0
    for message in source_messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise KelivoError(422, "invalid_messages")
        role, content = message.get("role"), message.get("content")
        if role not in ALLOWED_ROLES or not isinstance(content, str):
            raise KelivoError(422, "invalid_messages")
        if len(content) > MAX_CONTENT_CHARS:
            raise KelivoError(422, "message_too_long")
        total_chars += len(content)
        if total_chars > MAX_TOTAL_CONTENT_CHARS:
            raise KelivoError(422, "messages_too_large")
        preserved = {"role": role, "content": content}
        messages.append(preserved)
        if role == "system":
            system_context.append({"role": role, "content": normalize_snapshot_text(content)})
        elif role == "developer":
            developer_context.append({"role": role, "content": normalize_snapshot_text(content)})
    if messages[-1]["role"] != "user" or not messages[-1]["content"].strip():
        raise KelivoError(422, "last_message_must_be_user")

    tools = payload.get("tools", [])
    if not isinstance(tools, list):
        raise KelivoError(422, "invalid_tools")
    if tools:
        raise KelivoError(400, "tools_not_supported")

    temperature = payload.get("temperature")
    if temperature is not None:
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
            raise KelivoError(422, "invalid_temperature")
        temperature = float(temperature)
        if temperature == 0:
            temperature = 0.0
    max_tokens = payload.get("max_tokens")
    if max_tokens is not None and (
        isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= MAX_MAX_TOKENS
    ):
        raise KelivoError(422, "invalid_max_tokens")

    normalized: dict[str, Any] = {"messages": messages, "model": model_alias, "stream": False}
    if temperature is not None:
        normalized["temperature"] = temperature
    if max_tokens is not None:
        normalized["max_tokens"] = max_tokens
    snapshots: list[tuple[str, Any]] = []
    if system_context:
        snapshots.append(("system", system_context))
    if developer_context:
        snapshots.append(("developer", developer_context))
    _, digest = content_hash(normalized)
    return ValidatedCompletion(normalized, digest, tuple(messages), messages[-1]["content"],
                               tuple(snapshots), temperature, max_tokens)


def validate_idempotency_key(value: str | None) -> str:
    if value is None or IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
        raise KelivoError(400, "invalid_idempotency_key")
    return value


def _verify_telegram_target(
    conn: sqlite3.Connection, api_session: str, allowed_account_ids: frozenset[str],
    allowed_chat_ids: frozenset[str], allowed_user_ids: frozenset[str],
) -> None:
    row = conn.execute(
        """SELECT c.external_account_id,c.external_conversation_id,c.external_user_id,
                  c.status,a.status AS account_status
           FROM channel_conversations c JOIN channel_accounts a
             ON a.channel=c.channel AND a.external_account_id=c.external_account_id
           WHERE c.api_session=? AND c.channel='telegram'""", (api_session,),
    ).fetchone()
    if not row or row["status"] != "active" or row["account_status"] != "active":
        raise KelivoError(503, "kelivo_session_target_invalid")
    if row["external_account_id"] not in allowed_account_ids or row["external_conversation_id"] not in allowed_chat_ids:
        raise KelivoError(503, "kelivo_session_target_not_allowed")
    if not row["external_user_id"] or row["external_user_id"] not in allowed_user_ids:
        raise KelivoError(503, "kelivo_session_target_not_allowed")


def initialize_client_mapping(
    path: str, client_id: str, api_session: str, *, allow_session_remap: bool = False,
    require_telegram_session: bool = False, allowed_account_ids: frozenset[str] = frozenset(),
    allowed_chat_ids: frozenset[str] = frozenset(),
    allowed_user_ids: frozenset[str] = frozenset(),
) -> None:
    stamp = channel_store.now_iso()
    with channel_store.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if require_telegram_session:
            _verify_telegram_target(
                conn, api_session, allowed_account_ids, allowed_chat_ids, allowed_user_ids
            )
        row = conn.execute(
            "SELECT api_session,enabled,mapping_revision FROM kelivo_clients WHERE client_id=?", (client_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES(?,?,1,1,?,?)""", (client_id, api_session, stamp, stamp),
            )
        elif row["api_session"] != api_session or not row["enabled"]:
            if not allow_session_remap:
                raise KelivoError(503, "kelivo_session_remap_not_allowed")
            conn.execute(
                """UPDATE kelivo_clients SET api_session=?,enabled=1,mapping_revision=mapping_revision+1,
                   updated_at=? WHERE client_id=?""", (api_session, stamp, client_id),
            )
            channel_store.audit(conn, "kelivo_session_remap", "kelivo", client_id, api_session,
                                status="applied")
        conn.execute("COMMIT")


def client_mapping_ready(path: str, client_id: str, api_session: str) -> bool:
    try:
        with channel_store.connect(path) as conn:
            row = conn.execute(
                "SELECT api_session,enabled FROM kelivo_clients WHERE client_id=?", (client_id,)
            ).fetchone()
        return bool(row and row["enabled"] == 1 and row["api_session"] == api_session)
    except (sqlite3.Error, OSError):
        return False


def store_snapshot(conn: sqlite3.Connection, api_session: str, snapshot_type: str, value: Any) -> sqlite3.Row:
    encoded, digest = content_hash(value)
    existing = conn.execute(
        """SELECT * FROM companion_context_snapshots
           WHERE api_session=? AND snapshot_type=? AND content_hash=?""",
        (api_session, snapshot_type, digest),
    ).fetchone()
    if existing:
        if existing["active"] != 1:
            conn.execute(
                "UPDATE companion_context_snapshots SET active=0 WHERE api_session=? AND snapshot_type=? AND active=1",
                (api_session, snapshot_type),
            )
            conn.execute("UPDATE companion_context_snapshots SET active=1 WHERE id=?", (existing["id"],))
        return conn.execute("SELECT * FROM companion_context_snapshots WHERE id=?", (existing["id"],)).fetchone()
    version = conn.execute(
        """SELECT COALESCE(MAX(version),0)+1 AS next_version FROM companion_context_snapshots
           WHERE api_session=? AND snapshot_type=?""", (api_session, snapshot_type),
    ).fetchone()["next_version"]
    conn.execute(
        "UPDATE companion_context_snapshots SET active=0 WHERE api_session=? AND snapshot_type=? AND active=1",
        (api_session, snapshot_type),
    )
    cursor = conn.execute(
        """INSERT INTO companion_context_snapshots
           (api_session,snapshot_type,normalized_json,content_hash,active,version,created_at)
           VALUES(?,?,?,?,1,?,?)""", (api_session, snapshot_type, encoded, digest, version, channel_store.now_iso()),
    )
    return conn.execute("SELECT * FROM companion_context_snapshots WHERE id=?", (cursor.lastrowid,)).fetchone()


def active_context(path: str, api_session: str) -> dict[str, Any]:
    with channel_store.connect(path) as conn:
        rows = conn.execute(
            """SELECT snapshot_type,normalized_json FROM companion_context_snapshots
               WHERE api_session=? AND active=1""", (api_session,),
        ).fetchall()
    return {row["snapshot_type"]: json.loads(row["normalized_json"]) for row in rows}


def lookup_request(
    path: str, client_id: str, idempotency_key: str, validated: ValidatedCompletion,
    persona_text: str, provider_model: str, effective_temperature: float, effective_max_tokens: int,
) -> PreparedRequest | None:
    with channel_store.connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM kelivo_requests WHERE client_id=? AND idempotency_key=?",
            (client_id, idempotency_key),
        ).fetchone()
        mapping = conn.execute(
            "SELECT api_session,mapping_revision FROM kelivo_clients WHERE client_id=? AND enabled=1",
            (client_id,),
        ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(row["request_payload_hash"], validated.request_payload_hash):
        raise KelivoError(409, "idempotency_conflict")
    if row["status"] == "completed" and row["response_json"]:
        return PreparedRequest("replay", row["generation_id"], row["api_session"],
                               response=json.loads(row["response_json"]))
    if mapping is None:
        raise KelivoError(409, "idempotency_conflict")
    bundle = json.loads(row["context_bundle_json"])
    provider_messages = tuple(
        ([{"role": "system", "content": persona_text}] if persona_text else [])
        + [dict(message) for message in validated.messages]
    )
    expected_identity = build_request_identity_hash(
        virtual_model=validated.normalized_request["model"], provider_model=provider_model,
        client_id=client_id, api_session=mapping["api_session"],
        mapping_revision=int(mapping["mapping_revision"]), persona_hash=text_sha256(persona_text),
        snapshot_correlations=_snapshot_correlations(bundle), provider_messages=provider_messages,
        effective_temperature=effective_temperature, effective_max_tokens=effective_max_tokens,
    )
    if not hmac.compare_digest(row["request_identity_hash"], expected_identity):
        raise KelivoError(409, "idempotency_conflict")
    category = "idempotency_in_progress" if row["status"] in {"prepared", "dispatching"} else (
        row["error_category"] or row["status"]
    )
    return PreparedRequest("blocked", row["generation_id"], row["api_session"], error_category=category)


def _consume_rate_limit(
    conn: sqlite3.Connection, client_id: str, limit: int, window_seconds: int, now_epoch: int,
) -> None:
    window_start = now_epoch - (now_epoch % window_seconds)
    row = conn.execute(
        "SELECT request_count FROM kelivo_rate_limits WHERE client_id=? AND window_started_at=?",
        (client_id, window_start),
    ).fetchone()
    retry_after = max(1, window_start + window_seconds - now_epoch)
    if row and int(row["request_count"]) >= limit:
        raise KelivoError(429, "rate_limit_exceeded", retry_after=retry_after)
    stamp = channel_store.now_iso()
    if row:
        conn.execute(
            """UPDATE kelivo_rate_limits SET request_count=request_count+1,updated_at=?
               WHERE client_id=? AND window_started_at=?""", (stamp, client_id, window_start),
        )
    else:
        conn.execute(
            """INSERT INTO kelivo_rate_limits
               (client_id,window_started_at,request_count,created_at,updated_at) VALUES(?,?,1,?,?)""",
            (client_id, window_start, stamp, stamp),
        )
    conn.execute("DELETE FROM kelivo_rate_limits WHERE window_started_at < ?", (window_start - window_seconds * 2,))


def prepare_request(
    path: str, client_id: str, idempotency_key: str, validated: ValidatedCompletion,
    *, persona_text: str = "", persona_source: str = "default",
    provider_model: str = "test-provider", effective_temperature: float = 0.7,
    effective_max_tokens: int = 2000,
    rate_limit: int = 10, rate_window_seconds: int = 60,
) -> PreparedRequest:
    stamp = channel_store.now_iso()
    with channel_store.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        mapping = conn.execute(
            """SELECT api_session,mapping_revision FROM kelivo_clients
               WHERE client_id=? AND enabled=1""", (client_id,),
        ).fetchone()
        if not mapping:
            raise KelivoError(503, "client_mapping_unavailable")
        existing = conn.execute(
            "SELECT * FROM kelivo_requests WHERE client_id=? AND idempotency_key=?",
            (client_id, idempotency_key),
        ).fetchone()
        if existing:
            conn.execute("COMMIT")
            if not hmac.compare_digest(existing["request_payload_hash"], validated.request_payload_hash):
                raise KelivoError(409, "idempotency_conflict")
            if existing["status"] == "completed" and existing["response_json"]:
                return PreparedRequest("replay", existing["generation_id"], existing["api_session"],
                                       response=json.loads(existing["response_json"]))
            bundle = json.loads(existing["context_bundle_json"])
            provider_messages = tuple(
                ([{"role": "system", "content": persona_text}] if persona_text else [])
                + [dict(message) for message in validated.messages]
            )
            expected_identity = build_request_identity_hash(
                virtual_model=validated.normalized_request["model"], provider_model=provider_model,
                client_id=client_id, api_session=mapping["api_session"],
                mapping_revision=int(mapping["mapping_revision"]), persona_hash=text_sha256(persona_text),
                snapshot_correlations=_snapshot_correlations(bundle), provider_messages=provider_messages,
                effective_temperature=effective_temperature, effective_max_tokens=effective_max_tokens,
            )
            if not hmac.compare_digest(existing["request_identity_hash"], expected_identity):
                raise KelivoError(409, "idempotency_conflict")
            category = "idempotency_in_progress" if existing["status"] in {"prepared", "dispatching"} else (
                existing["error_category"] or existing["status"]
            )
            return PreparedRequest("blocked", existing["generation_id"], existing["api_session"],
                                   error_category=category)
        _consume_rate_limit(conn, client_id, rate_limit, rate_window_seconds, int(time.time()))
        api_session = mapping["api_session"]
        snapshot_rows = [store_snapshot(conn, api_session, snapshot_type, value)
                         for snapshot_type, value in validated.snapshots]
        boundary = int(conn.execute(
            """SELECT COALESCE(MAX(id),0) FROM messages
               WHERE json_valid(meta) AND json_extract(meta,'$.api_session')=?""", (api_session,),
        ).fetchone()[0])
        generation_id = "chatcmpl-" + secrets.token_urlsafe(18)
        provider_messages, bundle, bundle_hash, request_identity_hash = build_frozen_prompt_contract(
            validated, persona_text=persona_text, persona_source=persona_source,
            client_id=client_id, api_session=api_session,
            mapping_revision=int(mapping["mapping_revision"]), provider_model=provider_model,
            effective_temperature=effective_temperature, effective_max_tokens=effective_max_tokens,
            snapshot_rows=snapshot_rows,
        )
        bundle_json = normalized_json(bundle)
        prompt_json = normalized_json(list(provider_messages))
        conn.execute(
            """INSERT INTO kelivo_requests
               (idempotency_key,request_payload_hash,request_identity_hash,client_id,api_session,mapping_revision,
                history_before_id,context_bundle_json,context_bundle_hash,provider_messages_json,
                prompt_contract_version,persona_hash,persona_source,provider_model,
                effective_temperature,effective_max_tokens,status,
                generation_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'prepared',?,?,?)""",
            (idempotency_key, validated.request_payload_hash, request_identity_hash, client_id, api_session,
             mapping["mapping_revision"], boundary, bundle_json, bundle_hash, prompt_json,
             PROMPT_CONTRACT_VERSION, text_sha256(persona_text), persona_source,
             provider_model, effective_temperature, effective_max_tokens,
             generation_id, stamp, stamp),
        )
        conn.execute("COMMIT")
        return PreparedRequest(
            "prepared", generation_id, api_session, provider_model, effective_temperature,
            effective_max_tokens, provider_messages, bundle,
        )


def begin_dispatch(
    path: str, client_id: str, idempotency_key: str, *, stale_seconds: float,
) -> PreparedRequest:
    stamp = datetime.now(timezone.utc)
    expires = (stamp + timedelta(seconds=stale_seconds)).isoformat()
    with channel_store.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            """UPDATE kelivo_requests SET status='dispatching',dispatch_expires_at=?,updated_at=?
               WHERE client_id=? AND idempotency_key=? AND status='prepared'""",
            (expires, stamp.isoformat(), client_id, idempotency_key),
        ).rowcount
        row = conn.execute(
            "SELECT * FROM kelivo_requests WHERE client_id=? AND idempotency_key=?",
            (client_id, idempotency_key),
        ).fetchone()
        conn.execute("COMMIT")
    if row is None:
        raise KelivoError(503, "request_state_missing")
    if changed != 1:
        if row["status"] == "completed" and row["response_json"]:
            return PreparedRequest("replay", row["generation_id"], row["api_session"],
                                   response=json.loads(row["response_json"]))
        category = "idempotency_in_progress" if row["status"] in {"prepared", "dispatching"} else (
            row["error_category"] or row["status"]
        )
        return PreparedRequest("blocked", row["generation_id"], row["api_session"], error_category=category)
    return PreparedRequest(
        "dispatch", row["generation_id"], row["api_session"], row["provider_model"],
        float(row["effective_temperature"]), int(row["effective_max_tokens"]),
        tuple(json.loads(row["provider_messages_json"])), json.loads(row["context_bundle_json"]),
    )


def fail_request(path: str, client_id: str, idempotency_key: str, category: str, uncertain: bool) -> None:
    with channel_store.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE kelivo_requests SET status=CASE WHEN status='prepared' THEN 'failed' ELSE ? END,
               error_category=?,updated_at=?
               WHERE client_id=? AND idempotency_key=? AND status IN ('prepared','dispatching')""",
            ("dispatch_uncertain" if uncertain else "failed", category, channel_store.now_iso(),
             client_id, idempotency_key),
        )
        conn.execute("COMMIT")


def recover_dispatching_requests(
    path: str, *, stale_seconds: int = 0, category: str = "relay_restarted",
) -> int:
    now = datetime.now(timezone.utc)
    with channel_store.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if stale_seconds <= 0:
            conn.execute(
                """UPDATE kelivo_requests SET status='failed',error_category='relay_restarted_before_dispatch',
                   updated_at=? WHERE status='prepared'""", (now.isoformat(),),
            )
            changed = conn.execute(
                """UPDATE kelivo_requests SET status='dispatch_uncertain',error_category=?,updated_at=?
                   WHERE status='dispatching'""", (category, now.isoformat()),
            ).rowcount
        else:
            changed = conn.execute(
                """UPDATE kelivo_requests SET status='dispatch_uncertain',error_category=?,updated_at=?
                   WHERE status='dispatching' AND dispatch_expires_at IS NOT NULL
                     AND dispatch_expires_at<=?""",
                (category, now.isoformat(), now.isoformat()),
            ).rowcount
        conn.execute("COMMIT")
        return changed


def _safe_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        key: item if isinstance((item := value.get(key, 0)), int) and not isinstance(item, bool) and item >= 0 else 0
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def complete_request(
    path: str, client_id: str, idempotency_key: str, model_alias: str, result: dict[str, Any],
) -> dict[str, Any]:
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise GenerationError("empty_model_response", True)
    with channel_store.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        request = conn.execute(
            "SELECT * FROM kelivo_requests WHERE client_id=? AND idempotency_key=?",
            (client_id, idempotency_key),
        ).fetchone()
        if not request:
            raise KelivoError(503, "request_state_missing")
        if request["status"] == "completed" and request["response_json"]:
            conn.execute("COMMIT")
            return json.loads(request["response_json"])
        if request["status"] != "dispatching":
            raise KelivoError(409, "request_not_dispatchable")
        prompt_messages = json.loads(request["provider_messages_json"])
        user_text = prompt_messages[-1]["content"]
        stamp = channel_store.now_iso()
        meta = normalized_json({"api_session": request["api_session"], "channel": "kelivo",
                                "generation_id": request["generation_id"]})
        user = conn.execute(
            "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
            (stamp, "in", "user", user_text, meta),
        )
        assistant = conn.execute(
            "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
            (stamp, "out", "reply", text, meta),
        )
        response = {
            "id": request["generation_id"], "object": "chat.completion", "created": int(time.time()),
            "model": model_alias,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": _safe_usage(result.get("usage")),
        }
        response_json = normalized_json(response)
        changed = conn.execute(
            """UPDATE kelivo_requests SET status='completed',user_message_id=?,assistant_message_id=?,
               response_json=?,error_category=NULL,updated_at=? WHERE id=? AND status='dispatching'""",
            (user.lastrowid, assistant.lastrowid, response_json, stamp, request["id"]),
        ).rowcount
        if changed != 1:
            raise sqlite3.IntegrityError("Kelivo completion state changed")
        conn.execute("COMMIT")
        return response


class AdmissionLease:
    def __init__(self, global_sem: asyncio.Semaphore, client_sem: asyncio.Semaphore):
        self.global_sem, self.client_sem, self.released = global_sem, client_sem, False

    def release(self) -> None:
        if not self.released:
            self.client_sem.release()
            self.global_sem.release()
            self.released = True


@dataclass
class _KeyLockEntry:
    lock: asyncio.Lock
    references: int = 0


class IdempotencyLockRegistry:
    """Short-lived keyed locks with cancellation-safe reference cleanup."""

    def __init__(self):
        self._entries: dict[tuple[str, str], _KeyLockEntry] = {}
        self._registry_lock = asyncio.Lock()

    @asynccontextmanager
    async def hold(self, client_id: str, idempotency_key: str):
        key = (client_id, idempotency_key)
        async with self._registry_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _KeyLockEntry(asyncio.Lock())
                self._entries[key] = entry
            entry.references += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._registry_lock:
                entry.references -= 1
                if entry.references == 0 and not entry.lock.locked():
                    self._entries.pop(key, None)

    @property
    def entry_count(self) -> int:
        return len(self._entries)


class KelivoAdmissionController:
    def __init__(self, global_limit: int, client_limit: int, queue_timeout_seconds: float):
        self._global = asyncio.Semaphore(global_limit)
        self._client_limit = client_limit
        self._queue_timeout = queue_timeout_seconds
        self._clients: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, client_id: str) -> AdmissionLease:
        deadline = asyncio.get_running_loop().time() + self._queue_timeout
        async with self._lock:
            client_sem = self._clients.setdefault(client_id, asyncio.Semaphore(self._client_limit))
        try:
            await asyncio.wait_for(client_sem.acquire(), self._queue_timeout)
        except TimeoutError:
            raise KelivoError(429, "concurrency_limit_exceeded", retry_after=max(1, int(self._queue_timeout))) from None
        remaining = deadline - asyncio.get_running_loop().time()
        try:
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(self._global.acquire(), remaining)
        except TimeoutError:
            client_sem.release()
            raise KelivoError(429, "concurrency_limit_exceeded", retry_after=max(1, int(self._queue_timeout))) from None
        except asyncio.CancelledError:
            client_sem.release()
            raise
        return AdmissionLease(self._global, client_sem)


class LoopGenerationClient:
    """Authenticated, loopback-only adapter to the existing api_loop model implementation."""

    def __init__(self, ingest_url: str, timeout_seconds: float, internal_token: str,
                 response_max_bytes: int = 1024 * 1024, transport: httpx.AsyncBaseTransport | None = None):
        parsed = urllib.parse.urlsplit(ingest_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or parsed.query or parsed.fragment:
            raise ValueError("loop URL must be local")
        if not internal_token:
            raise ValueError("internal loop token required")
        self.url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/loop/chat", "", ""))
        self.timeout_seconds = timeout_seconds
        self.internal_token = internal_token
        self.response_max_bytes = response_max_bytes
        self.transport = transport

    async def generate(
        self, messages: tuple[dict[str, str], ...], api_session: str, provider_model: str,
        temperature: float, max_tokens: int, context: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, trust_env=False, transport=self.transport,
            ) as client:
                async with client.stream(
                    "POST", self.url,
                    headers={"X-API-Loop-Internal-Token": self.internal_token},
                    json={
                        "provider_messages": list(messages), "session_id": api_session,
                        "provider_model": provider_model,
                        "prompt_contract_version": context.get("prompt_contract_version"),
                        "use_default_persona": False, "single_route": True,
                        "temperature": temperature, "max_tokens": max_tokens,
                    },
                ) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > self.response_max_bytes:
                            raise GenerationError("generation_response_too_large", True)
        except GenerationError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, OSError):
            raise GenerationError("model_transport_uncertain", True) from None
        try:
            payload = json.loads(bytes(data))
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            raise GenerationError("invalid_generation_response", True) from None
        if response.status_code >= 400 or not isinstance(payload, dict) or payload.get("ok") is not True:
            uncertain = response.status_code >= 500 and bool(payload.get("dispatch_uncertain"))
            raise GenerationError(str(payload.get("error") or "model_failed"), uncertain) from None
        api = payload.get("api") if isinstance(payload.get("api"), dict) else {}
        return {"text": payload.get("reply"), "usage": api.get("usage") or {}}
