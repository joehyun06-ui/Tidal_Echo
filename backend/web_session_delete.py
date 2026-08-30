"""Conversation hard deletion for P3 Web sessions.

User-visible chat content is removed while preserving database integrity:
- unreferenced canonical message rows are physically deleted;
- rows still referenced by Memory/Kelivo foreign keys are irreversibly redacted in
  place (empty text, no attachment metadata, no original session id);
- orphaned files under the configured relay upload directory are removed;
- deleted Web session ids receive tiny id/provider/deleted_at tombstones so stale
  tabs cannot silently route through API again;
- the legacy untagged surface can be purged too and then disappears from the UI;
- Codex sessions are deletable only after retirement and with no nonterminal job;
- deleting the last ordinary API session creates a fresh empty API session.

Memory rows themselves are intentionally not deleted here. A Memory item may retain
its own derived text even when the source chat message has been purged.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import urllib.parse
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import codex_generation_store, web_session_provider_authority


LEGACY_SESSION_ID = "__legacy__"
DELETE_FORBIDDEN = "web_session_delete_forbidden"
DELETE_REQUIRES_RETIREMENT = "web_session_delete_requires_retirement"
DELETE_JOB_ACTIVE = "web_session_delete_job_active"
DELETE_STORAGE_UNAVAILABLE = "web_session_delete_storage_unavailable"
DELETE_CODEX_AUTHORITY_UNAVAILABLE = "web_session_delete_codex_authority_unavailable"
DELETED_KIND = "deleted"
_DELETED_API_SESSION = "__deleted__"
_CHUNK = 400
_SAFE_UPLOAD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def _raise(category: str):
    raise web_session_provider_authority.WebSessionProviderAuthorityError(category)


def _chunks(values: list[int], size: int = _CHUNK) -> Iterable[list[int]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _connect_relay(path: str | os.PathLike[str]) -> sqlite3.Connection:
    try:
        database = Path(path)
    except (TypeError, ValueError):
        _raise(DELETE_STORAGE_UNAVAILABLE)
    try:
        if not database.is_absolute() or not database.is_file():
            _raise(DELETE_STORAGE_UNAVAILABLE)
        conn = sqlite3.connect(str(database), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn
    except web_session_provider_authority.WebSessionProviderAuthorityError:
        raise
    except (OSError, sqlite3.Error):
        _raise(DELETE_STORAGE_UNAVAILABLE)


def _message_scope(meta_raw: object) -> str:
    if not isinstance(meta_raw, str):
        return ""
    try:
        meta = json.loads(meta_raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("api_session") or "").strip()


def _attachment_names(meta_raw: object) -> set[str]:
    if not isinstance(meta_raw, str):
        return set()
    try:
        meta = json.loads(meta_raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(meta, dict):
        return set()
    attachments = meta.get("attachments")
    if not isinstance(attachments, list):
        return set()
    names: set[str] = set()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        url = attachment.get("url")
        if not isinstance(url, str) or not url:
            continue
        try:
            path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        except (TypeError, ValueError):
            continue
        if "/uploads/" not in path:
            continue
        name = Path(path).name
        if _SAFE_UPLOAD_NAME.fullmatch(name):
            names.add(name)
    return names


def _target_messages(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    try:
        rows = conn.execute(
            "SELECT id,kind,meta FROM messages WHERE kind != ? ORDER BY id",
            (DELETED_KIND,),
        ).fetchall()
    except sqlite3.Error:
        _raise(DELETE_STORAGE_UNAVAILABLE)
    target: list[sqlite3.Row] = []
    for row in rows:
        scope = _message_scope(row["meta"])
        if session_id == LEGACY_SESSION_ID:
            if not scope:
                target.append(row)
        elif scope == session_id:
            target.append(row)
    return target


def _quote_identifier(value: object) -> str:
    name = str(value or "")
    if not name or "\x00" in name:
        _raise(DELETE_STORAGE_UNAVAILABLE)
    return '"' + name.replace('"', '""') + '"'


def _referenced_message_ids(conn: sqlite3.Connection, message_ids: list[int]) -> set[int]:
    if not message_ids:
        return set()
    referenced: set[int] = set()
    try:
        tables = [
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        reference_columns: list[tuple[str, str]] = []
        for table in tables:
            pragma = f"PRAGMA foreign_key_list({_quote_identifier(table)})"
            for foreign_key in conn.execute(pragma):
                if (
                    str(foreign_key["table"]) == "messages"
                    and str(foreign_key["to"]) == "id"
                ):
                    reference_columns.append((table, str(foreign_key["from"])))
        for chunk in _chunks(message_ids):
            placeholders = ",".join("?" for _ in chunk)
            for table, column in reference_columns:
                query = (
                    f"SELECT DISTINCT {_quote_identifier(column)} AS message_id "
                    f"FROM {_quote_identifier(table)} "
                    f"WHERE {_quote_identifier(column)} IN ({placeholders})"
                )
                for row in conn.execute(query, chunk):
                    value = row["message_id"]
                    if isinstance(value, int) and not isinstance(value, bool):
                        referenced.add(value)
    except web_session_provider_authority.WebSessionProviderAuthorityError:
        raise
    except sqlite3.Error:
        _raise(DELETE_STORAGE_UNAVAILABLE)
    return referenced


def _delete_ids(conn: sqlite3.Connection, message_ids: list[int]) -> None:
    for chunk in _chunks(message_ids):
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", chunk)


def _redact_ids(conn: sqlite3.Connection, message_ids: list[int]) -> None:
    redacted_meta = json.dumps(
        {"deleted": True, "api_session": _DELETED_API_SESSION},
        separators=(",", ":"),
        sort_keys=True,
    )
    for chunk in _chunks(message_ids):
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"UPDATE messages SET kind=?,text='',meta=? WHERE id IN ({placeholders})",
            [DELETED_KIND, redacted_meta, *chunk],
        )


def _remaining_attachment_names(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute(
            "SELECT meta FROM messages WHERE kind != ?",
            (DELETED_KIND,),
        ).fetchall()
    except sqlite3.Error:
        _raise(DELETE_STORAGE_UNAVAILABLE)
    names: set[str] = set()
    for row in rows:
        names.update(_attachment_names(row["meta"]))
    return names


def _cleanup_uploads(
    upload_dir: str | os.PathLike[str] | None,
    candidates: set[str],
    still_referenced: set[str],
) -> tuple[int, int, int]:
    orphans = sorted(candidates - still_referenced)
    retained = len(candidates) - len(orphans)
    if not orphans:
        return 0, retained, 0
    if upload_dir is None:
        return 0, retained, len(orphans)
    try:
        root = Path(upload_dir)
        info = root.stat()
        if not root.is_absolute() or not stat.S_ISDIR(info.st_mode):
            return 0, retained, len(orphans)
    except (OSError, TypeError, ValueError):
        return 0, retained, len(orphans)

    removed = 0
    failed = 0
    for name in orphans:
        path = root / name
        try:
            if path.parent != root or path.is_symlink():
                failed += 1
                continue
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            failed += 1
    return removed, retained, failed


def purge_messages(
    relay_db: str | os.PathLike[str],
    *,
    session_id: str,
    upload_dir: str | os.PathLike[str] | None = None,
) -> dict[str, int | bool]:
    """Erase one session's chat content without violating foreign-key integrity."""
    conn = _connect_relay(relay_db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = _target_messages(conn, session_id)
        ids = [int(row["id"]) for row in rows]
        candidates: set[str] = set()
        for row in rows:
            candidates.update(_attachment_names(row["meta"]))
        referenced = _referenced_message_ids(conn, ids)
        physically_deleted = [message_id for message_id in ids if message_id not in referenced]
        redacted = [message_id for message_id in ids if message_id in referenced]
        _delete_ids(conn, physically_deleted)
        _redact_ids(conn, redacted)
        remaining_attachments = _remaining_attachment_names(conn)
        conn.execute("COMMIT")
    except web_session_provider_authority.WebSessionProviderAuthorityError:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _raise(DELETE_STORAGE_UNAVAILABLE)
    finally:
        conn.close()

    attachments_deleted, attachments_retained, attachment_cleanup_failed = (
        _cleanup_uploads(upload_dir, candidates, remaining_attachments)
    )
    return {
        "content_deleted": True,
        "messages_purged": len(ids),
        "messages_deleted": len(physically_deleted),
        "messages_redacted": len(redacted),
        "attachments_deleted": attachments_deleted,
        "attachments_retained": attachments_retained,
        "attachment_cleanup_failed": attachment_cleanup_failed,
        "memory_deleted": False,
    }


def legacy_available(relay_db: str | os.PathLike[str]) -> bool:
    conn = _connect_relay(relay_db)
    try:
        return bool(_target_messages(conn, LEGACY_SESSION_ID))
    finally:
        conn.close()


def _codex_store_path(path: str | os.PathLike[str] | None) -> Path:
    if path is None:
        _raise(DELETE_CODEX_AUTHORITY_UNAVAILABLE)
    try:
        database = Path(path)
        if not database.is_absolute() or not database.is_file():
            _raise(DELETE_CODEX_AUTHORITY_UNAVAILABLE)
    except web_session_provider_authority.WebSessionProviderAuthorityError:
        raise
    except (TypeError, ValueError, OSError):
        _raise(DELETE_CODEX_AUTHORITY_UNAVAILABLE)
    return database


def assert_codex_deletable(
    store_path: str | os.PathLike[str] | None,
    session_id: str,
) -> None:
    database = _codex_store_path(store_path)
    try:
        session = codex_generation_store.get_session(database, session_id)
        if session is None or session.get("provider") != "codex":
            _raise(DELETE_CODEX_AUTHORITY_UNAVAILABLE)
        if session.get("status") != "retired":
            _raise(DELETE_REQUIRES_RETIREMENT)
        with closing(codex_generation_store.connect(database)) as conn:
            statuses = sorted(codex_generation_store.ACTIVE_JOB_STATUSES)
            placeholders = ",".join("?" for _ in statuses)
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM codex_generation_jobs "
                f"WHERE api_session=? AND status IN ({placeholders})",
                [session_id, *statuses],
            ).fetchone()
        if row is None or int(row["n"]) != 0:
            _raise(DELETE_JOB_ACTIVE)
    except web_session_provider_authority.WebSessionProviderAuthorityError:
        raise
    except (OSError, sqlite3.Error, codex_generation_store.CodexGenerationStoreError):
        _raise(DELETE_CODEX_AUTHORITY_UNAVAILABLE)


def codex_delete_allowed(
    store_path: str | os.PathLike[str] | None,
    session_id: str,
) -> bool:
    try:
        assert_codex_deletable(store_path, session_id)
        return True
    except web_session_provider_authority.WebSessionProviderAuthorityError:
        return False


def public_session_state(
    authority: web_session_provider_authority.WebSessionProviderAuthority,
    *,
    relay_db: str | os.PathLike[str],
    codex_store: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    public = authority.sessions_public()
    sessions: list[dict[str, Any]] = []
    for item in public["sessions"]:
        row = dict(item)
        if row["provider"] == web_session_provider_authority.API_PROVIDER:
            row["delete_allowed"] = True
        else:
            row["delete_allowed"] = codex_delete_allowed(codex_store, row["id"])
        sessions.append(row)
    try:
        legacy_present = legacy_available(relay_db)
        legacy_delete_allowed = legacy_present
    except web_session_provider_authority.WebSessionProviderAuthorityError:
        # Never hide history merely because the storage probe is unavailable.
        legacy_present = True
        legacy_delete_allowed = False
    return {
        "active_session": public["active_session"],
        "sessions": sessions,
        "legacy_available": legacy_present,
        "legacy_delete_allowed": legacy_delete_allowed,
    }


def _append_tombstone(
    authority: web_session_provider_authority.WebSessionProviderAuthority,
    *,
    session_id: str,
    provider: str,
    cfg: dict[str, Any],
) -> None:
    tombstones = authority.tombstones()
    existing = next((row for row in tombstones if row["id"] == session_id), None)
    if existing is not None:
        if existing["provider"] != provider:
            _raise("web_session_provider_authority_conflict")
        cfg[web_session_provider_authority.DELETED_SESSIONS_KEY] = tombstones
        return
    tombstones.append(
        {
            "id": session_id,
            "provider": provider,
            "deleted_at": authority.legacy.now_iso(),
        }
    )
    cfg[web_session_provider_authority.DELETED_SESSIONS_KEY] = tombstones


def delete_conversation(
    authority: web_session_provider_authority.WebSessionProviderAuthority,
    session_id: str,
    *,
    relay_db: str | os.PathLike[str],
    upload_dir: str | os.PathLike[str] | None,
    codex_store: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """Purge one visible conversation and remove its UI session authority row."""
    session_id = str(session_id or "").strip()
    if session_id == LEGACY_SESSION_ID:
        purge = purge_messages(
            relay_db,
            session_id=LEGACY_SESSION_ID,
            upload_dir=upload_dir,
        )
        rows = authority.session_rows()
        created = None
        api_rows = [
            row for row in rows
            if row["provider"] == web_session_provider_authority.API_PROVIDER
        ]
        if not api_rows:
            created = authority.new_row(title="新的对话", provider="api")
            rows.append(created)
            api_rows.append(created)
        active = authority.active_session_id()
        ids = {row["id"] for row in rows}
        if active not in ids:
            active = api_rows[-1]["id"]
        cfg = authority.legacy.load_config()
        cfg["sessions"] = rows
        cfg["active_session"] = active
        authority.legacy.save_config(cfg)
        public = public_session_state(
            authority,
            relay_db=relay_db,
            codex_store=codex_store,
        )
        result: dict[str, Any] = {
            **public,
            "deleted": {
                "id": LEGACY_SESSION_ID,
                "scope": "legacy",
                "provider": None,
                **purge,
            },
        }
        if created is not None:
            result["created"] = created
        return result

    tombstone = authority.tombstone_for_session(session_id)
    if tombstone is not None:
        return {
            **public_session_state(
                authority,
                relay_db=relay_db,
                codex_store=codex_store,
            ),
            "deleted": {
                "id": session_id,
                "scope": "session",
                "provider": tombstone["provider"],
                "content_deleted": True,
                "messages_purged": 0,
                "messages_deleted": 0,
                "messages_redacted": 0,
                "attachments_deleted": 0,
                "attachments_retained": 0,
                "attachment_cleanup_failed": 0,
                "memory_deleted": False,
                "duplicate": True,
            },
        }

    target = authority.row_for_session(session_id)
    if target is None:
        _raise("web_session_not_found")
    provider = str(target["provider"])
    if provider == web_session_provider_authority.CODEX_PROVIDER:
        assert_codex_deletable(codex_store, session_id)
    elif provider != web_session_provider_authority.API_PROVIDER:
        _raise(DELETE_FORBIDDEN)

    purge = purge_messages(
        relay_db,
        session_id=session_id,
        upload_dir=upload_dir,
    )

    rows = [row for row in authority.session_rows() if row["id"] != session_id]
    current_active = authority.active_session_id()
    api_rows = [
        row for row in rows
        if row["provider"] == web_session_provider_authority.API_PROVIDER
    ]
    created = None
    if not api_rows:
        created = authority.new_row(title="新的对话", provider="api")
        rows.append(created)
        api_rows.append(created)
    remaining_ids = {row["id"] for row in rows}
    if current_active == session_id or current_active not in remaining_ids:
        next_active = api_rows[-1]["id"]
    else:
        next_active = current_active

    cfg = authority.legacy.load_config()
    cfg["sessions"] = rows
    cfg["active_session"] = next_active
    _append_tombstone(
        authority,
        session_id=session_id,
        provider=provider,
        cfg=cfg,
    )
    authority.legacy.save_config(cfg)

    public = public_session_state(
        authority,
        relay_db=relay_db,
        codex_store=codex_store,
    )
    result = {
        **public,
        "deleted": {
            "id": session_id,
            "scope": "session",
            "provider": provider,
            **purge,
            "duplicate": False,
        },
    }
    if created is not None:
        result["created"] = created
    return result
