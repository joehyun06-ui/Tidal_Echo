"""Telegram private-text validation, transport, and persistent workers."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping

try:
    from . import channel_store, deployment_config
except ImportError:
    import channel_store, deployment_config


MAX_TELEGRAM_ID = 2**63 - 1
TELEGRAM_IMAGE_PLACEHOLDER = "\u005b\u56fe\u7247\u005d"


def _strict_positive_id(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean id")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value and value.isascii() and value.isdecimal() and not (len(value) > 1 and value[0] == "0"):
        result = int(value, 10)
    else:
        raise ValueError("invalid id")
    if result <= 0 or result > MAX_TELEGRAM_ID:
        raise ValueError("id out of range")
    return result


def _strict_ids(value: str) -> frozenset[int]:
    if not value.strip():
        return frozenset()
    values = set()
    for item in value.split(","):
        if not item or item != item.strip() or item == "*":
            raise ValueError("integer allowlist required")
        parsed = _strict_positive_id(item)
        if parsed in values:
            raise ValueError("duplicate allowlist id")
        values.add(parsed)
    return frozenset(values)


def _safe_api_base(value: str, *, test_mode: bool, custom_allowlist: frozenset[str]) -> str:
    base = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("invalid Telegram API base")
    if test_mode:
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("invalid mock API scheme")
        return base
    if base == "https://api.telegram.org":
        return base
    if parsed.scheme != "https" or base not in custom_allowlist:
        raise ValueError("Telegram API base not allowed")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("local API base not allowed")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local or
                    address.is_reserved or address.is_unspecified or address.is_multicast):
        raise ValueError("non-public API base not allowed")
    if address is None:
        try:
            resolved = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise ValueError("unresolvable API base") from exc
        for raw in resolved:
            resolved_address = ipaddress.ip_address(raw.split("%", 1)[0])
            if (resolved_address.is_private or resolved_address.is_loopback or resolved_address.is_link_local or
                    resolved_address.is_reserved or resolved_address.is_unspecified or resolved_address.is_multicast):
                raise ValueError("non-public API base not allowed")
    return base


@dataclass(frozen=True)
class TelegramConfig:
    requested: bool
    enabled: bool
    bot_token: str
    webhook_secret: str
    audit_hmac_secret: str
    account_id: str
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    api_base: str
    max_text_length: int
    webhook_max_body_bytes: int
    worker_poll_seconds: float
    generation_max_attempts: int
    error: str = ""

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "TelegramConfig":
        env = os.environ if environ is None else environ
        requested = deployment_config.parse_strict_bool(
            env.get("TELEGRAM_ENABLED", "false"), "invalid_telegram_enabled"
        )
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        secret = env.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
        audit_secret = env.get("CHANNEL_AUDIT_HMAC_SECRET", "").strip()
        account = env.get("TELEGRAM_BOT_ACCOUNT_ID", "").strip()
        raw_base = env.get("TELEGRAM_API_BASE", "https://api.telegram.org")
        empty = (requested, False, "", "", "", "", frozenset(), frozenset(), "", 4096, 65536, 1.0, 2)
        try:
            if secret and (len(secret) > 256 or re.fullmatch(r"[A-Za-z0-9_-]+", secret) is None):
                raise ValueError("invalid webhook secret")
            users = _strict_ids(env.get("TELEGRAM_ALLOWED_USER_IDS", ""))
            chats = _strict_ids(env.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
            max_text = max(1, min(int(env.get("TELEGRAM_MAX_TEXT_LENGTH", "4096")), 4096))
            body_max = max(1024, min(int(env.get("TELEGRAM_WEBHOOK_MAX_BODY_BYTES", "65536")), 1048576))
            poll = max(0.25, deployment_config.parse_positive_finite_float(
                env.get("TELEGRAM_WORKER_POLL_SECONDS", "1"), "invalid_telegram_worker_poll"
            ))
            attempts = max(1, min(int(env.get("TELEGRAM_GENERATION_MAX_ATTEMPTS", "2")), 10))
            test_mode = deployment_config.parse_strict_bool(
                env.get("TELEGRAM_TEST_MODE", "false"), "invalid_telegram_test_mode"
            )
            custom = frozenset(x.strip().rstrip("/") for x in env.get("TELEGRAM_API_BASE_ALLOWLIST", "").split(",") if x.strip())
            api_base = _safe_api_base(raw_base, test_mode=test_mode, custom_allowlist=custom)
        except (TypeError, ValueError, deployment_config.DeploymentConfigError):
            return cls(*empty, "invalid_config")
        complete = bool(token and secret and audit_secret and account and users and chats and api_base)
        return cls(requested, requested and complete, token, secret, audit_secret, account, users, chats,
                   api_base, max_text, body_max, poll, attempts,
                   "" if complete or not requested else "incomplete_config")


class TelegramDeliveryError(Exception):
    def __init__(self, category: str, uncertain: bool = False, retry_after: int | None = None):
        super().__init__(category)
        self.category = category
        self.uncertain = uncertain
        self.retry_after = retry_after


class LoopDispatchError(Exception):
    def __init__(self, category: str, uncertain: bool = False):
        super().__init__(category)
        self.category = category
        self.uncertain = uncertain


def split_plain_text(text: str, limit: int = 4096) -> list[str]:
    if not text:
        return []
    chunks = []
    while len(text) > limit:
        newline = text.rfind("\n", 0, limit)
        cut = newline + 1 if newline >= 0 else limit
        chunks.append(text[:cut]); text = text[cut:]
    if text: chunks.append(text)
    return chunks


class TelegramClient:
    """Injectable one-part Bot API client; errors never contain URL, token, chat, or text."""

    def __init__(self, config: TelegramConfig, opener=None):
        self.config = config
        self._opener = opener or urllib.request.urlopen

    @staticmethod
    def _retry_after(payload: object) -> int | None:
        try:
            value = payload["parameters"]["retry_after"]
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        except (KeyError, TypeError):
            return None

    def send_part(self, chat_id: int, text: str) -> str:
        if not text:
            raise TelegramDeliveryError("empty_reply")
        if not self.config.enabled or chat_id not in self.config.allowed_chat_ids:
            raise TelegramDeliveryError("destination_not_allowed")
        data = json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{self.config.api_base}/bot{self.config.bot_token}/sendMessage",
                                     data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with self._opener(req, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            retry_after = None
            if exc.code == 429:
                try: retry_after = self._retry_after(json.loads(exc.read().decode("utf-8")))
                except Exception: pass
                raise TelegramDeliveryError("telegram_rate_limited", False, retry_after) from None
            if 400 <= exc.code < 500:
                raise TelegramDeliveryError("telegram_rejected", False) from None
            raise TelegramDeliveryError("telegram_server_uncertain", True) from None
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError):
            raise TelegramDeliveryError("telegram_transport_uncertain", True) from None
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
            raise TelegramDeliveryError("telegram_invalid_response", True) from None
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise TelegramDeliveryError("telegram_rejected", False, self._retry_after(payload))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise TelegramDeliveryError("telegram_invalid_response", True)
        try:
            return str(_strict_positive_id(result.get("message_id")))
        except ValueError:
            raise TelegramDeliveryError("telegram_missing_message_id", True) from None


GenerationDispatcher = Callable[[dict, dict], Awaitable[None]]


class TelegramWorker:
    def __init__(self, db_path: str, config: TelegramConfig, generation_dispatcher: GenerationDispatcher,
                 client: TelegramClient | None = None):
        self.db_path = db_path; self.config = config; self.generation_dispatcher = generation_dispatcher
        self.client = client or TelegramClient(config)

    def _canonical_message(self, message_id: int) -> dict | None:
        with channel_store.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            return ({"id": row["id"], "ts": row["ts"], "direction": row["direction"], "kind": row["kind"],
                     "text": row["text"], "meta": json.loads(row["meta"] or "{}")}) if row else None

    async def run_generation_once(self) -> bool:
        job = channel_store.claim_generation_job(self.db_path, max_attempts=self.config.generation_max_attempts)
        if not job: return False
        if not self.config.enabled:
            channel_store.start_generation_dispatch(self.db_path, job["id"])
            channel_store.finish_generation_dispatch(self.db_path, job["id"], "failed", "telegram_disabled")
            return True
        message = self._canonical_message(job["canonical_message_id"])
        if not message:
            channel_store.start_generation_dispatch(self.db_path, job["id"])
            channel_store.finish_generation_dispatch(self.db_path, job["id"], "failed", "canonical_message_missing")
            return True
        job = channel_store.start_generation_dispatch(self.db_path, job["id"])
        if not job: return True
        try:
            await self.generation_dispatcher(job, message)
        except asyncio.CancelledError:
            raise
        except LoopDispatchError as exc:
            outcome = "dispatch_uncertain" if exc.uncertain else "failed"
            channel_store.finish_generation_dispatch(self.db_path, job["id"], outcome, exc.category)
        except Exception:
            channel_store.finish_generation_dispatch(self.db_path, job["id"], "dispatch_uncertain", "unexpected_dispatch_error")
        else:
            channel_store.finish_generation_dispatch(self.db_path, job["id"], "awaiting_reply")
        return True

    def _destination_active(self, part: dict) -> bool:
        try: chat_id = _strict_positive_id(part["external_conversation_id"])
        except ValueError: return False
        if chat_id not in self.config.allowed_chat_ids: return False
        with channel_store.connect(self.db_path) as conn:
            account = conn.execute("""SELECT id FROM channel_accounts WHERE channel='telegram'
                AND external_account_id=? AND status='active'""", (self.config.account_id,)).fetchone()
            conversation = conn.execute("""SELECT external_user_id FROM channel_conversations WHERE channel='telegram'
                AND external_account_id=? AND external_conversation_id=? AND conversation_type='private' AND status='active'""",
                (self.config.account_id, str(chat_id))).fetchone()
        if not account or not conversation or part["external_account_id"] != self.config.account_id:
            return False
        try:
            user_id = _strict_positive_id(conversation["external_user_id"])
        except ValueError:
            return False
        return user_id in self.config.allowed_user_ids

    async def run_delivery_once(self) -> bool:
        part = channel_store.claim_delivery_part(self.db_path)
        if not part: return False
        if not self._destination_active(part):
            channel_store.finish_delivery_part(self.db_path, part["id"], "failed", error_category="destination_not_allowed")
            return True
        try:
            message_id = await asyncio.to_thread(self.client.send_part,
                _strict_positive_id(part["external_conversation_id"]), part["payload_text"])
        except asyncio.CancelledError:
            raise
        except TelegramDeliveryError as exc:
            status = "delivery_uncertain" if exc.uncertain else "failed"
            channel_store.finish_delivery_part(self.db_path, part["id"], status,
                                               error_category=exc.category, retry_after_seconds=exc.retry_after)
        except Exception:
            channel_store.finish_delivery_part(self.db_path, part["id"], "delivery_uncertain",
                                               error_category="unexpected_transport_error")
        else:
            channel_store.finish_delivery_part(self.db_path, part["id"], "delivered", telegram_message_id=message_id)
        return True

    async def run_forever(self) -> None:
        while True:
            try:
                worked = await self.run_generation_once()
                worked = await self.run_delivery_once() or worked
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[telegram-worker] cycle failed: {type(exc).__name__}")
                worked = False
            if not worked: await asyncio.sleep(self.config.worker_poll_seconds)


def validate_update(config: TelegramConfig, body: object) -> tuple[dict | None, str | None]:
    if not isinstance(body, dict): return None, "malformed_update"
    if "message" not in body: return None, "unsupported_update"
    message = body.get("message")
    if not isinstance(message, dict): return None, "malformed_update"
    chat = message.get("chat"); sender = message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict): return None, "malformed_update"
    if sender.get("is_bot") is True: return None, "bot_sender"
    if chat.get("type") != "private": return None, "private_chat_required"
    try:
        update_id = _strict_positive_id(body.get("update_id")); chat_id = _strict_positive_id(chat.get("id"))
        user_id = _strict_positive_id(sender.get("id")); message_id = _strict_positive_id(message.get("message_id"))
    except ValueError:
        return None, "malformed_update"
    if user_id not in config.allowed_user_ids or chat_id not in config.allowed_chat_ids:
        return None, "not_allowed"
    text = message.get("text")
    if not isinstance(text, str) or not text.strip(): return None, "text_required"
    text = text.strip()
    if text.startswith("/"): return None, "commands_not_supported"
    if len(text) > config.max_text_length: return None, "text_too_long"
    return {"update_id": str(update_id), "chat_id": str(chat_id), "user_id": str(user_id),
            "external_message_id": str(message_id), "text": text}, None
