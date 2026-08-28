from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CodexCanaryEntrypointImportTest(unittest.TestCase):
    def _base_env(self, root: Path) -> dict[str, str]:
        env = dict(os.environ)
        brain = root / "brain_target"
        brain.write_text("desktop", encoding="utf-8")
        env.update({
            "PYTHONPATH": str(ROOT),
            "RENDER_TELEGRAM_MVP": "false",
            "RENDER_PERSISTENT_ROOT": str(root / "persistent"),
            "RELAY_SECRET": "test-relay-secret",
            "RELAY_DB": str(root / "relay.db"),
            "RELAY_UPLOAD_DIR": str(root / "uploads"),
            "RELAY_BRAIN_FILE": str(brain),
            "RELAY_BRAIN_TARGET": "",
            "LOOP_CONFIG": str(root / "api_loop.config.json"),
            "API_LOOP_INTERNAL_TOKEN": "test-internal-loop-token-1234567890",
            "CODEX_CONTROL_ENABLED": "false",
            "CODEX_GENERATION_ENABLED": "false",
            "CODEX_HOME": str(root / "persistent" / "codex-home"),
            "CODEX_WORKSPACE": str(root / "persistent" / "codex-workspace"),
            "CODEX_GENERATION_WORKSPACE": str(root / "persistent" / "codex-workspace"),
            "CODEX_GENERATION_DB": str(root / "persistent" / "codex-generation.db"),
            "TELEGRAM_ENABLED": "false",
            "KELIVO_ENABLED": "false",
            "OPERIT_SHARE_ENABLED": "false",
            "HEARTBEAT_ENABLED": "false",
            "MEMORY_CORE_ENABLED": "false",
        })
        return env

    def test_api_loop_canary_import_is_side_effect_free_while_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = self._base_env(root)
            script = r'''
from pathlib import Path
import examples.api_loop_codex_canary as module
assert module.RUNTIME.generation_enabled is False
assert module.INTEGRATION.legacy.CODEX_CONTROL.__class__.__name__ == "LegacyControlAdapter"
assert any(getattr(route, "path", "") == "/loop/ingest" for route in module.app.routes)
assert not Path(module.GENERATION_CONFIG.store_path).exists()
print("ok")
'''
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("ok", completed.stdout)

    def test_relay_canary_import_installs_only_alternate_entrypoint_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = self._base_env(root)
            env["LEGACY_CHAT_BRIDGE_TOKEN"] = "test-legacy-bridge-token-1234567890"
            script = r'''
import backend.codex_canary_relay_app as module
assert module.bridge.relay_app._CODEX_CANARY_RELAY_INSTALLED is True
assert module.app is module.bridge.app
print("ok")
'''
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("ok", completed.stdout)

    def test_render_canary_supervisor_bootstraps_repo_root_when_executed_directly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = self._base_env(root)
            env.pop("PYTHONPATH", None)
            env["CODEX_CANARY_ENTRYPOINTS_ENABLED"] = "false"
            env["PORT"] = ""
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "render_start_codex_canary.py")],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertNotIn("ModuleNotFoundError", completed.stderr)
            self.assertIn("preflight_failed", completed.stderr)


if __name__ == "__main__":
    unittest.main()
