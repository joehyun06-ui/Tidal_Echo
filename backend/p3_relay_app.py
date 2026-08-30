"""P3 public relay entrypoint with provider capability projection.

The underlying relay remains the reviewed legacy Render relay.  This wrapper adds
one authenticated, read-only browser capability route and no generation authority.
Codex-specific relay integrations are still installed only by the alternate Codex
relay entrypoint.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from backend import legacy_chat_bridge_app as bridge
from backend import web_provider_capabilities


relay_app = bridge.relay_app
app = bridge.app


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


_install_capability_route()
