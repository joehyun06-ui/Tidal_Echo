from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import render_start


class RenderHybridEntrypointTests(unittest.TestCase):
    def test_public_relay_uses_p3_wrapper_with_hybrid_status_contract(self):
        config = SimpleNamespace(
            deployment=SimpleNamespace(loop_port=3020),
            relay_port=10000,
            autonomous_wake_enabled=False,
        )
        command = render_start.child_commands(config, executable="python")["relay"]
        self.assertIn("backend.p3_relay_app:app", command)
        self.assertNotIn("backend.legacy_chat_bridge_app:app", command)

        p3_source = (Path(__file__).parents[1] / "p3_relay_app.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('@app.get("/app/memory/hybrid-shadow/status")', p3_source)
        self.assertIn("relay_app.check_auth(request)", p3_source)
        self.assertIn("status_payload_v1(relay_app)", p3_source)


if __name__ == "__main__":
    unittest.main()
