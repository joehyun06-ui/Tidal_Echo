"""P3 public relay entrypoint with provider capability and safe session deletion.

The underlying relay remains the reviewed legacy Render relay. This wrapper adds
small authenticated P3 browser contracts but no generation authority. Codex-specific
relay integrations are still installed only by the alternate Codex relay entrypoint.
"""

from __future__ import annotations

import urllib.parse

from fastapi import Request
from fastapi.responses import JSONResponse

from backend import legacy_chat_bridge_app as bridge
from backend import p3_provider_status, web_provider_capabilities


relay_app = bridge.relay_app
app = bridge.app


def _fixed_status_error() -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": p3_provider_status.ERROR_CATEGORY},
        status_code=503,
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
            # Localhost loop failures and malformed upstream state are deliberately
            # collapsed to one data-free public category.
            return _fixed_status_error()

    relay_app._P3_PROVIDER_STATUS_INSTALLED = True


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
_install_session_delete_route()
