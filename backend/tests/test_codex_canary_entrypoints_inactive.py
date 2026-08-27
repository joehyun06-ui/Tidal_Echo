from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CodexCanaryEntrypointsInactiveTest(unittest.TestCase):
    def test_supervisor_still_uses_legacy_production_entrypoints(self):
        source = (ROOT / "scripts" / "render_start.py").read_text(encoding="utf-8")
        self.assertIn('"examples.api_loop:app"', source)
        self.assertIn('"backend.legacy_chat_bridge_app:app"', source)
        self.assertNotIn("examples.api_loop_codex_canary:app", source)
        self.assertNotIn("backend.codex_canary_relay_app:app", source)

    def test_render_blueprint_start_command_and_codex_gates_are_unchanged(self):
        source = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.assertRegex(source, r"(?m)^\s*startCommand:\s*python scripts/render_start\.py\s*$")
        self.assertNotIn("CODEX_GENERATION_ENABLED", source)
        control = re.search(
            r"(?ms)^\s*- key:\s*CODEX_CONTROL_ENABLED\s*$\n\s*value:\s*([^\n#]+)",
            source,
        )
        self.assertIsNotNone(control)
        self.assertEqual(control.group(1).strip().strip('"\''), "false")


if __name__ == "__main__":
    unittest.main()
