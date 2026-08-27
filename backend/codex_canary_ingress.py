"""P2-B text-only Web canary ingress qualification.

This module only reads the canonical relay message. It does not route, enqueue, or
fallback. A message is eligible only when canonical provenance proves it is a plain
Web relay text message for the already-pinned canary session.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


class CodexCanaryIngressError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category

    def __repr__(self) -> str:
        return f"<CodexCanaryIngressError category={self.category!r}>"


def _connect_read_only(path: str | Path, timeout_seconds: float = 30.0) -> sqlite3.Connection:
    database = Path(path)
    if not database.is_absolute() or not database.is_file():
        raise CodexCanaryIngressError("codex_canary_relay_unavailable")
    try:
        uri = f"{database.as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout_seconds, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute(f"PRAGMA busy_timeout = {max(1, int(timeout_seconds * 1000))}")
        return conn
    except sqlite3.Error:
        raise CodexCanaryIngressError("codex_canary_relay_unavailable") from None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_text_only_web_message(
    relay_db: str | Path,
    *,
    canonical_message_id: int,
    api_session: str,
    expected_digest: str,
) -> str:
    if isinstance(canonical_message_id, bool) or not isinstance(canonical_message_id, int) or canonical_message_id <= 0:
        raise CodexCanaryIngressError("codex_canary_message_invalid")
    if not isinstance(api_session, str) or not api_session or len(api_session) > 160:
        raise CodexCanaryIngressError("codex_canary_session_invalid")
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise CodexCanaryIngressError("codex_canary_digest_invalid")
    try:
        with closing(_connect_read_only(relay_db)) as conn:
            row = conn.execute(
                "SELECT id,direction,kind,text,meta FROM messages WHERE id=?",
                (canonical_message_id,),
            ).fetchone()
    except CodexCanaryIngressError:
        raise
    except sqlite3.Error:
        raise CodexCanaryIngressError("codex_canary_relay_unavailable") from None
    if row is None:
        raise CodexCanaryIngressError("codex_canary_message_missing")
    if row["direction"] != "in" or row["kind"] != "user":
        raise CodexCanaryIngressError("codex_canary_surface_ineligible")
    try:
        meta = json.loads(row["meta"])
    except Exception:
        raise CodexCanaryIngressError("codex_canary_message_invalid") from None
    if not isinstance(meta, dict):
        raise CodexCanaryIngressError("codex_canary_message_invalid")
    if meta.get("channel") != "web" or meta.get("source") != "relay":
        raise CodexCanaryIngressError("codex_canary_surface_ineligible")
    if meta.get("api_session") != api_session:
        raise CodexCanaryIngressError("codex_canary_session_mismatch")
    attachments = meta.get("attachments", [])
    if attachments not in (None, []):
        raise CodexCanaryIngressError("codex_canary_attachments_unsupported")
    text = row["text"]
    if not isinstance(text, str) or not text:
        raise CodexCanaryIngressError("codex_canary_text_invalid")
    if _digest(text) != expected_digest:
        raise CodexCanaryIngressError("codex_canary_input_contract_changed")
    return text


def require_continuity_empty(status: str) -> None:
    """P2-B canary does not persist transient cross-channel continuity."""
    if status == "empty":
        return
    if status == "applied":
        raise CodexCanaryIngressError("codex_canary_continuity_unsupported")
    raise CodexCanaryIngressError("codex_canary_continuity_unavailable")
