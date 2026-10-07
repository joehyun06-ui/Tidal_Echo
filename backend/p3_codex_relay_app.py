"""Production P3 relay entrypoint for mixed API/Codex Web sessions.

This entrypoint keeps the reviewed Codex Web queued-ack/completion integration while
intentionally omitting qualification-era canary admin and recovery routes. Provider
capabilities, authoritative status, ordinary session lifecycle, Telegram/Kelivo,
and other P3 relay behavior remain owned by ``backend.p3_relay_app``.
"""

from __future__ import annotations

from backend import codex_canary_relay_integration
from backend.codex_generation_routes import install_relay as install_generation_routes
from backend import p3_relay_app as bridge
from backend import api_web_generation_relay


codex_canary_relay_integration.install(bridge.relay_app)
install_generation_routes(bridge.relay_app)
api_web_generation_relay.install(bridge.relay_app)
app = bridge.app
