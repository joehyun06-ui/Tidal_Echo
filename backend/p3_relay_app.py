"""P3 public relay entrypoint with provider capability and safe session deletion.

The underlying relay remains the reviewed legacy Render relay. This wrapper adds
small authenticated P3 browser contracts but no generation authority. Codex-specific
relay integrations are still installed only by the alternate Codex relay entrypoint.
"""

from __future__ import annotations

import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from backend import legacy_chat_bridge_app as bridge
from backend import (
    memory_formation_v2_authority,
    memory_formation_v2_runtime_patch,
    memory_hierarchy_live_refresh_shadow,
    memory_hierarchy_summary_runtime_shadow,
    memory_retrieval_hybrid_runtime_shadow,
    p3_provider_status,
    p3_session_retire,
    web_provider_capabilities,
)


relay_app = bridge.relay_app
app = bridge.app
if not memory_formation_v2_authority.install(relay_app):
    memory_formation_v2_runtime_patch.install(relay_app)
memory_hierarchy_summary_runtime_shadow.install(relay_app)
memory_hierarchy_live_refresh_shadow.install(relay_app)
# D3B1 installs no retrieval resources. The gate defaults OFF; if enabled
# before D3B2 supplies the server-owned runner, installation fails closed.
memory_retrieval_hybrid_runtime_shadow.install(relay_app)


def _fixed_status_error() -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": p3_provider_status.ERROR_CATEGORY},
        status_code=503,
    )


def _retire_error(error: p3_session_retire.P3SessionRetireError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": error.category},
        status_code=error.status_code,
    )


def _install_capability_route() -> None:
    if getattr(relay_app, "_P3_PROVIDER_CAPABILITY_INSTALLED", False):
        return

    @app.get("/app/provider/capabilities")
    async def app_provider_capabilities(request: Request):
        relay_app.check_auth(request)
        try:
            return web_provider_capabilities.public_capabilities()
        except web_provider_capabilities.WebProviderCapabilitiesError as error:
            return JSONResponse(
                {"ok": False, "error": error.category},
                status_code=503,
            )

    relay_app._P3_PROVIDER_CAPABILITY_INSTALLED = True


def _install_provider_status_route() -> None:
    if getattr(relay_app, "_P3_PROVIDER_STATUS_INSTALLED", False):
        return

    @app.get("/app/provider/status")
    async def app_provider_status(request: Request):
        relay_app.check_auth(request)
        try:
            capabilities = web_provider_capabilities.public_capabilities()
            session_state = relay_app.loop_json("/loop/sessions")
            return p3_provider_status.project_provider_status(
                session_state,
                capabilities,
            )
        except (
            p3_provider_status.P3ProviderStatusError,
            web_provider_capabilities.WebProviderCapabilitiesError,
        ):
            return _fixed_status_error()
        except Exception:
            return _fixed_status_error()

    relay_app._P3_PROVIDER_STATUS_INSTALLED = True


def _install_session_retire_route() -> None:
    if getattr(relay_app, "_P3_SESSION_RETIRE_INSTALLED", False):
        return

    @app.post("/app/sessions/{session_id}/retire")
    async def app_sessions_retire(session_id: str, request: Request):
        relay_app.check_auth(request)
        try:
            sid = p3_session_retire.safe_session_id(session_id)
            before = relay_app.loop_json("/loop/sessions")
            p3_session_retire.require_codex_target(before, sid)
            encoded = urllib.parse.quote(sid, safe="")
            try:
                upstream = relay_app.loop_json(
                    f"/loop/provider/canary/{encoded}/retire",
                    method="POST",
                )
            except HTTPException as error:
                p3_session_retire.raise_loop_retire_error(
                    error.status_code,
                    error.detail,
                )
                raise AssertionError("unreachable")
            after = relay_app.loop_json("/loop/sessions")
            return p3_session_retire.project_retired(upstream, after, sid)
        except p3_session_retire.P3SessionRetireError as error:
            return _retire_error(error)
        except HTTPException:
            return _retire_error(
                p3_session_retire.P3SessionRetireError(
                    p3_session_retire.RETIRE_UNAVAILABLE
                )
            )
        except Exception:
            return _retire_error(
                p3_session_retire.P3SessionRetireError(
                    p3_session_retire.RETIRE_UNAVAILABLE
                )
            )

    relay_app._P3_SESSION_RETIRE_INSTALLED = True


def _install_session_delete_route() -> None:
    if getattr(relay_app, "_P3_SESSION_DELETE_INSTALLED", False):
        return

    @app.delete("/app/sessions/{session_id}")
    async def app_sessions_delete(session_id: str, request: Request):
        relay_app.check_auth(request)
        encoded = urllib.parse.quote(session_id, safe="")
        return relay_app.loop_json(
            f"/loop/sessions/{encoded}",
            method="DELETE",
        )

    relay_app._P3_SESSION_DELETE_INSTALLED = True


_install_capability_route()
_install_provider_status_route()
_install_session_retire_route()
_install_session_delete_route()
