from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import deployment_config
from scripts import render_start
from scripts import render_start_p3


ROOT = Path(__file__).resolve().parents[2]


def supervisor_config(*, autonomous_wake_enabled: bool = False):
    return render_start.SupervisorConfig(
        deployment=SimpleNamespace(loop_port=3020),
        relay_port=10000,
        loop_ready_timeout=15.0,
        shutdown_grace=10.0,
        instance_nonce="nonce",
        internal_token="token",
        autonomous_wake_enabled=autonomous_wake_enabled,
    )


class P3ProviderGuardSupervisorTests(unittest.TestCase):
    def test_default_selection_wraps_api_loop_and_preserves_p3_relay(self):
        config = supervisor_config(autonomous_wake_enabled=True)
        base = render_start.child_commands(config, executable="python-test")
        actual = render_start_p3.child_commands(
            config,
            executable="python-test",
            environ={},
        )
        self.assertIn(render_start_p3.GUARD_API_LOOP, actual["api_loop"])
        self.assertNotIn(render_start_p3.BASE_API_LOOP, actual["api_loop"])
        self.assertIn(render_start_p3.P3_RELAY, actual["relay"])
        self.assertEqual(actual["relay"], base["relay"])
        self.assertEqual(actual["autonomous_wake"], base["autonomous_wake"])
        normalized = {name: list(command) for name, command in actual.items()}
        normalized["api_loop"] = [
            render_start_p3.BASE_API_LOOP
            if item == render_start_p3.GUARD_API_LOOP else item
            for item in normalized["api_loop"]
        ]
        self.assertEqual(normalized, base)

    def test_flags_are_strict_and_generation_requires_codex_entrypoints(self):
        for env, category in (
            ({render_start_p3.CANARY_FLAG: " true "}, "invalid_codex_canary_entrypoints_enabled"),
            ({render_start_p3.GENERATION_FLAG: "yes please"}, "invalid_codex_generation_enabled"),
        ):
            with self.subTest(env=env), self.assertRaisesRegex(
                deployment_config.DeploymentConfigError,
                category,
            ):
                render_start_p3.codex_entrypoints_enabled(env)
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            "codex_generation_requires_canary_entrypoints",
        ):
            render_start_p3.codex_entrypoints_enabled({
                render_start_p3.CANARY_FLAG: "false",
                render_start_p3.GENERATION_FLAG: "true",
            })

    def test_enabled_codex_gate_swaps_p3_wrappers_only(self):
        config = supervisor_config(autonomous_wake_enabled=True)
        guarded = render_start_p3.child_commands(
            config,
            executable="python-test",
            environ={},
        )
        actual = render_start_p3.child_commands(
            config,
            executable="python-test",
            environ={
                render_start_p3.CANARY_FLAG: "true",
                render_start_p3.GENERATION_FLAG: "false",
            },
        )
        self.assertIn(render_start_p3.CODEX_API_LOOP, actual["api_loop"])
        self.assertNotIn(render_start_p3.GUARD_API_LOOP, actual["api_loop"])
        self.assertIn(render_start_p3.CODEX_RELAY, actual["relay"])
        self.assertNotIn(render_start_p3.P3_RELAY, actual["relay"])
        self.assertEqual(actual["autonomous_wake"], guarded["autonomous_wake"])

    def test_main_selector_is_scoped_and_restores_base_supervisor(self):
        original = render_start.child_commands
        observed = {}

        def fake_main():
            observed["selector"] = render_start.child_commands
            observed["commands"] = render_start.child_commands(
                supervisor_config(),
                executable="python-test",
            )
            return 23

        with mock.patch.dict(os.environ, {
            render_start_p3.CANARY_FLAG: "false",
            render_start_p3.GENERATION_FLAG: "false",
        }, clear=False), mock.patch.object(render_start, "main", side_effect=fake_main):
            self.assertEqual(render_start_p3.main(), 23)

        self.assertIsNot(observed["selector"], original)
        self.assertIn(render_start_p3.GUARD_API_LOOP, observed["commands"]["api_loop"])
        self.assertIn(render_start_p3.P3_RELAY, observed["commands"]["relay"])
        self.assertIs(render_start.child_commands, original)

    def test_direct_execution_bootstraps_repo_root_before_preflight(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.update({
            render_start_p3.CANARY_FLAG: "false",
            render_start_p3.GENERATION_FLAG: "false",
        })
        with tempfile.TemporaryDirectory() as cwd:
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "render_start_p3.py")],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("ModuleNotFoundError", combined)
        self.assertIn("preflight_failed", combined)


if __name__ == "__main__":
    unittest.main()
