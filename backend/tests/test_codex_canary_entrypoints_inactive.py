from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CodexCanaryEntrypointsInactiveTest(unittest.TestCase):
    def test_supervisor_uses_p3_production_relay_while_codex_canary_is_inactive(self):
        source = (ROOT / "scripts" / "render_start.py").read_text(encoding="utf-8")
        self.assertIn('"examples.api_loop:app"', source)
        self.assertIn('"backend.p3_relay_app:app"', source)
        self.assertNotIn('"backend.legacy_chat_bridge_app:app"', source)
        self.assertNotIn("examples.api_loop_codex_canary:app", source)
        self.assertNotIn("backend.codex_canary_relay_app:app", source)

    def test_render_blueprint_start_command_and_codex_gates_are_unchanged(self):
        payload = json.loads((ROOT / "render.yaml").read_text(encoding="utf-8"))
        service = payload["services"][0]
        self.assertEqual(service["startCommand"], "python scripts/render_start.py")
        env = {row["key"]: row for row in service["envVars"]}
        self.assertNotIn("CODEX_GENERATION_ENABLED", env)
        self.assertEqual(env["CODEX_CONTROL_ENABLED"]["value"], "false")


if __name__ == "__main__":
    unittest.main()
