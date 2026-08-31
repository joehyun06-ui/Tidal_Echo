"""Production P3 relay entrypoint for mixed API/Codex Web sessions.

This entrypoint keeps the reviewed Codex Web queued-ack/completion integration while
intentionally omitting qualification-era canary admin and recovery routes. Provider
capabilities, authoritative status, ordinary session lifecycle, Telegram/Kelivo,
and other P3 relay behavior remain owned by ``backend.p3_relay_app``.
"""

from __future__ import annotations

from backend import codex_canary_relay_integration
from backend import p3_relay_app as bridge


codex_canary_relay_integration.install(bridge.relay_app)
app = bridge.app
