"""Read-only preparation of API window context from a validated reentry packet."""

from dataclasses import dataclass, field
import hashlib
import json

from .session_lifecycle import LifecycleError, SessionLifecycle, SourceCursor, _json
from .session_lifecycle_storage import SQLiteLifecycleRepository, _require_current_message_cursor


FORMAT = "guiting.api_reentry_input.v1"
MAX_CONTEXT_BYTES = 65536


@dataclass(frozen=True)
class ApiWindowInput:
    conversation_id: str
    version_id: str
    target_window_id: str
    context_epoch: int
    packet_sha256: str
    input_sha256: str
    source_cursor: SourceCursor
    message_cursor: int
    content: str = field(repr=False)

    def as_message(self):
        """Use a user-role message for historical data and derived context."""
        return {"role": "user", "content": self.content}


def _canonical_messages(conn, packet, max_bytes):
    ids = packet.canonical_message_ids
    if not ids:
        return []
    params = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT id,kind,direction,
                CASE WHEN typeof(text)='text' AND length(CAST(text AS BLOB))<=? THEN text END,
                json_type(meta,'$.attachments'),json_array_length(meta,'$.attachments')
            FROM main.messages WHERE id IN ({params}) AND json_extract(meta,'$.api_session')=?
            ORDER BY id""",
        (max_bytes, *ids, packet.version_id),
    )
    messages = []
    text_bytes = 0
    for mid, kind, direction, text, attachments, attachment_count in rows:
        if (kind, direction) not in (("user", "in"), ("voice", "in"), ("reply", "out")):
            raise LifecycleError("lifecycle_input_message_unavailable")
        if (attachments, attachment_count) not in ((None, None), ("array", 0)):
            raise LifecycleError("lifecycle_input_media_unsupported")
        if text is None:
            raise LifecycleError("lifecycle_input_budget_exceeded")
        if not text.strip():
            raise LifecycleError("lifecycle_input_message_unavailable")
        text_bytes += len(text.encode("utf-8"))
        if text_bytes > max_bytes:
            raise LifecycleError("lifecycle_input_budget_exceeded")
        messages.append({"id": mid, "role": "assistant" if direction == "out" else "user", "text": text})
    if tuple(message["id"] for message in messages) != ids:
        raise LifecycleError("lifecycle_input_message_unavailable")
    return messages


def _materialize_api_window_input(tx, packet, digest, *, max_bytes=MAX_CONTEXT_BYTES):
    if packet.provider != "api":
        raise LifecycleError("lifecycle_provider_unsupported")
    _require_current_message_cursor(tx.conn, packet)
    messages = _canonical_messages(tx.conn, packet, max_bytes)
    excerpts = []
    for card in packet.anchors + packet.recent:
        ref = card.source_refs[0]
        excerpts.append({"event_id": ref.event_id, "revision": ref.revision,
                         "layer": card.layer, "excerpt": card.projection_text})
    content = json.dumps({
        "format": FORMAT, "authority": packet.authority, "pending": packet.pending,
        "active_state": dict(packet.active_state), "g3_excerpts": excerpts,
        "summary_candidates": packet.summary_candidates, "canonical_messages": messages,
    }, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    if len(content.encode("utf-8")) > max_bytes:
        raise LifecycleError("lifecycle_input_budget_exceeded")
    input_sha256 = hashlib.sha256(_json([FORMAT, digest, "user", content])).hexdigest()
    return ApiWindowInput(
        packet.conversation_id, packet.version_id, packet.target_window_id, packet.context_epoch,
        digest, input_sha256, packet.source_cursor, packet.message_cursor, content,
    )


def prepare_api_window_input(engine, conversation_id, *, access, max_bytes=32768, enabled=False):
    """Stage a text-only input under the existing canonical/session and G3 checks."""
    if enabled is not True:
        raise LifecycleError("lifecycle_disabled")
    if (type(engine) is not SessionLifecycle or type(engine.repository) is not SQLiteLifecycleRepository
            or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_CONTEXT_BYTES):
        raise LifecycleError("lifecycle_input_binding_invalid")
    if not engine.enabled:
        raise LifecycleError("lifecycle_disabled")

    def materialize(tx, packet, digest):
        return _materialize_api_window_input(tx, packet, digest, max_bytes=max_bytes)

    return engine._read_packet(conversation_id, access=access, materialize=materialize)
