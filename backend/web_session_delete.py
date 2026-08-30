"""Safe Web-session index deletion for P3.

Deletion is intentionally narrower than session creation/editing:
- only durable API-authority Web sessions may be removed;
- Codex-authority rows are retained so historical provider identity remains fail-closed;
- deleting a session removes only the session index row, never relay messages;
- if the deleted row was active, the newest remaining API session becomes active;
- if no ordinary API session remains, the active pointer is cleared to the legacy surface.
"""

from __future__ import annotations

from typing import Any

from . import web_session_provider_authority


DELETE_FORBIDDEN = "web_session_delete_forbidden"


def delete_api_session(
    authority: web_session_provider_authority.WebSessionProviderAuthority,
    session_id: str,
) -> dict[str, Any]:
    """Remove one API Web-session index row without deleting canonical messages."""
    target = authority.row_for_session(session_id)
    if target is None:
        raise web_session_provider_authority.WebSessionProviderAuthorityError(
            "web_session_not_found"
        )
    if target["provider"] != web_session_provider_authority.API_PROVIDER:
        raise web_session_provider_authority.WebSessionProviderAuthorityError(
            DELETE_FORBIDDEN
        )

    rows = authority.session_rows()
    remaining = [row for row in rows if row["id"] != target["id"]]
    remaining_ids = {row["id"] for row in remaining}
    current_active = authority.active_session_id()

    if current_active == target["id"] or current_active not in remaining_ids:
        api_rows = [
            row
            for row in remaining
            if row["provider"] == web_session_provider_authority.API_PROVIDER
        ]
        next_active = api_rows[-1]["id"] if api_rows else ""
    else:
        next_active = current_active

    cfg = authority.legacy.load_config()
    cfg["sessions"] = remaining
    cfg["active_session"] = next_active
    authority.legacy.save_config(cfg)

    public = authority.sessions_public()
    return {
        **public,
        "deleted": {
            "id": target["id"],
            "provider": web_session_provider_authority.API_PROVIDER,
            "messages_deleted": False,
        },
    }
