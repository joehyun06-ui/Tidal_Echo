from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import render_start_p3


ROOT = Path(__file__).resolve().parents[2]


class P3ProductionCodexRelayTests(unittest.TestCase):
    def _base_env(self, root: Path) -> dict[str, str]:
        # This subprocess validates the P3 production relay shape, not the
        # caller's currently deployed optional Memory feature graph. Keep the
        # fixture hermetic so newly enabled MEMORY_* gates in CI/production do
        # not combine with the synthetic MEMORY_CORE_ENABLED=false setting.
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("MEMORY_")
        }
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
            "LEGACY_CHAT_BRIDGE_TOKEN": "test-legacy-bridge-token-1234567890",
        })
        return env

    def test_subprocess_fixture_drops_host_memory_feature_gates(self):
        with tempfile.TemporaryDirectory() as temp:
            with unittest.mock.patch.dict(
                os.environ,
                {
                    "MEMORY_FORMATION_V2_AUTHORITY_ENABLED": "true",
                    "MEMORY_FUTURE_OPTIONAL_GATE": "true",
                },
            ):
                env = self._base_env(Path(temp))
        self.assertEqual(env["MEMORY_CORE_ENABLED"], "false")
        self.assertNotIn("MEMORY_FORMATION_V2_AUTHORITY_ENABLED", env)
        self.assertNotIn("MEMORY_FUTURE_OPTIONAL_GATE", env)

    def test_supervisor_selects_production_not_qualification_relay(self):
        self.assertEqual(
            render_start_p3.CODEX_RELAY,
            "backend.p3_codex_relay_app:app",
        )
        self.assertNotEqual(
            render_start_p3.CODEX_RELAY,
            "backend.codex_canary_relay_app:app",
        )

    def test_production_entrypoint_keeps_codex_transport_without_canary_admin_routes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = self._base_env(root)
            script = r'''
import sys
import backend.p3_codex_relay_app as module

relay = module.bridge.relay_app
assert module.app is module.bridge.app
assert relay._CODEX_CANARY_RELAY_INSTALLED is True
assert getattr(relay, "_P3_PROVIDER_CAPABILITY_INSTALLED", False) is True
assert getattr(relay, "_P3_PROVIDER_STATUS_INSTALLED", False) is True
assert getattr(relay, "_P3_SESSION_RETIRE_INSTALLED", False) is True
assert getattr(relay, "_P3_SESSION_DELETE_INSTALLED", False) is True
assert getattr(relay, "_CODEX_CANARY_ADMIN_PROXY_INSTALLED", False) is False
assert getattr(relay, "_CODEX_CANARY_RECOVERY_ADMIN_INSTALLED", False) is False
assert "backend.codex_canary_admin_proxy" not in sys.modules
assert "backend.codex_canary_recovery_admin" not in sys.modules

paths = {getattr(route, "path", "") for route in module.app.routes}
for required in (
    "/app/provider/capabilities",
    "/app/provider/status",
    "/app/sessions/{session_id}/retire",
    "/app/sessions/{session_id}",
):
    assert required in paths, required
for forbidden in (
    "/provider/canary/create",
    "/provider/canary/{session_id}/status",
    "/provider/canary/{session_id}/diagnostic",
    "/provider/canary/{session_id}/retire",
    "/provider/canary/{session_id}/recover-existing",
):
    assert forbidden not in paths, forbidden
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

    def test_production_entrypoint_source_does_not_import_admin_modules(self):
        source = (ROOT / "backend" / "p3_codex_relay_app.py").read_text(encoding="utf-8")
        self.assertIn("codex_canary_relay_integration", source)
        self.assertNotIn("codex_canary_admin_proxy", source)
        self.assertNotIn("codex_canary_recovery_admin", source)


if __name__ == "__main__":
    unittest.main()
