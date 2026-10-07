"""Bounded, ephemeral public reply projection; never projects reasoning or tools."""
from __future__ import annotations

import re
import time
import uuid
from collections import OrderedDict

from .codex_generation_protocol import MAX_ASSISTANT_TEXT_CHARS

PROGRESS_NOTIFICATIONS = frozenset({
    "item/started", "item/completed", "item/agentMessage/delta",
})
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


def safe_id(value: object) -> bool:
    return isinstance(value, str) and SAFE_ID.fullmatch(value) is not None


class ReplyProgress:
    """Keep snapshots separate from the small terminal-notification queue.

    Only items with an observed agentMessage lifecycle are eligible for deltas.
    Each snapshot replaces text, so a missed SSE event needs no delta replay.
    This cache is disposable; canonical history remains the durable authority.
    """
    def __init__(self, max_turns: int = 32, ttl_seconds: float = 600):
        self.epoch = uuid.uuid4().hex
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self.turns = OrderedDict()

    def prune(self):
        cutoff = time.monotonic() - self.ttl_seconds
        for key, value in tuple(self.turns.items()):
            if value["touched"] < cutoff:
                self.turns.pop(key, None)

    def receive(self, method, params):
        self.prune()
        if method not in PROGRESS_NOTIFICATIONS or not isinstance(params, dict):
            return
        thread, turn = params.get("threadId"), params.get("turnId")
        if not safe_id(thread) or not safe_id(turn):
            return
        item = params.get("item")
        if method == "item/agentMessage/delta":
            item_id, delta = params.get("itemId"), params.get("delta")
            if not safe_id(item_id) or not isinstance(delta, str):
                return
            record = self.turns.get((thread, turn))
            if not record or item_id != record["item_id"] or record["done"]:
                return
            if len(record["text"]) + len(delta) > MAX_ASSISTANT_TEXT_CHARS:
                record["done"] = True
                return
            record["text"] += delta
            record["revision"] += 1
            record["touched"] = time.monotonic()
            return
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            return
        if item.get("phase") not in (None, "final_answer", "finalAnswer"):
            return
        item_id, text = item.get("id"), item.get("text")
        if not safe_id(item_id) or not isinstance(text, str) or len(text) > MAX_ASSISTANT_TEXT_CHARS:
            return
        key = (thread, turn)
        previous = self.turns.get(key)
        # A late start must not erase deltas or an authoritative completed item.
        if previous and previous["item_id"] == item_id and method == "item/started":
            return
        self.turns[key] = {
            "item_id": item_id, "text": text,
            "done": method == "item/completed",
            "touched": time.monotonic(),
            "revision": (previous["revision"] if previous else 0) + 1,
        }
        self.turns.move_to_end(key)
        while len(self.turns) > self.max_turns:
            self.turns.popitem(last=False)

    def snapshot(self, job):
        self.prune()
        record = self.turns.get((job.get("thread_id"), job.get("turn_id")))
        if not record:
            return None
        return {
            "type": "reply_snapshot", "provider": "codex",
            "api_session": job["api_session"], "generation_id": job["generation_id"],
            "canonical_message_id": job["canonical_message_id"],
            "stream_id": job["generation_id"], "epoch": self.epoch,
            "revision": record["revision"], "text": record["text"],
            "ts": job["created_at"],
        }


def valid_snapshot(body):
    return (
        isinstance(body, dict) and body.get("provider") in {"codex", "api"}
        and all(safe_id(body.get(key)) for key in ("api_session", "generation_id", "stream_id", "epoch"))
        and body["stream_id"] == body["generation_id"]
        and isinstance(body.get("canonical_message_id"), int) and not isinstance(body["canonical_message_id"], bool)
        and 0 < body["canonical_message_id"] < 2**53
        and body["generation_id"] == f"{body['provider']}-gen-{body['canonical_message_id']}"
        and isinstance(body.get("revision"), int) and not isinstance(body["revision"], bool)
        and 0 < body["revision"] < 2**53
        and isinstance(body.get("text"), str) and len(body["text"]) <= MAX_ASSISTANT_TEXT_CHARS
        and isinstance(body.get("ts"), str) and 0 < len(body["ts"]) <= 64
    )
