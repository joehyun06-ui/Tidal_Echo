"""Alternate relay entrypoint for the explicit Codex Web canary.

Current Render startup still imports `backend.legacy_chat_bridge_app:app`; therefore
merging this module alone does not change production behavior. A later explicit
activation change may point the supervisor at this entrypoint.
"""

from __future__ import annotations

from backend import codex_canary_admin_proxy
from backend import codex_canary_relay_integration
from backend import legacy_chat_bridge_app as bridge


codex_canary_relay_integration.install(bridge.relay_app)
codex_canary_admin_proxy.install(bridge.relay_app)
app = bridge.app
