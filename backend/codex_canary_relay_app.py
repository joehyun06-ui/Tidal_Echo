"""Alternate relay entrypoint for the explicit Codex Web canary.

P3 keeps the provider-capability projection present in both API-only and Codex
entrypoint modes. Codex-specific relay/admin integrations remain installed only by
this alternate entrypoint.
"""

from __future__ import annotations

from backend import codex_canary_admin_proxy
from backend import codex_canary_recovery_admin
from backend import codex_canary_relay_integration
from backend import p3_relay_app as bridge


codex_canary_relay_integration.install(bridge.relay_app)
codex_canary_admin_proxy.install(bridge.relay_app)
codex_canary_recovery_admin.install(bridge.relay_app)
app = bridge.app
