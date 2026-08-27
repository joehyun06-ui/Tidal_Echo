from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import deployment_config
from scripts import render_start
from scripts import render_start_codex_canary as canary_start


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


class CodexCanaryActivationFoundationTest(unittest.TestCase):
    def test_canary_entrypoint_gate_defaults_off(self):
        self.assertFalse(canary_start.canary_entrypoints_enabled({}))

    def test_canary_entrypoint_and_generation_flags_are_strict(self):
        for env, category in (
            ({canary_start.CANARY_FLAG: " true "}, "invalid_codex_canary_entrypoints_enabled"),
            ({canary_start.GENERATION_FLAG: "yes please"}, "invalid_codex_generation_enabled"),
        ):
            with self.subTest(env=env), self.assertRaisesRegex(
                deployment_config.DeploymentConfigError,
                category,
            ):
                canary_start.canary_entrypoints_enabled(env)

    def test_generation_cannot_claim_enabled_while_legacy_entrypoints_are_selected(self):
        with self.assertRaisesRegex(
            deployment_config.DeploymentConfigError,
            "codex_generation_requires_canary_entrypoints",
        ):
            canary_start.canary_entrypoints_enabled({
                canary_start.CANARY_FLAG: "false",
                canary_start.GENERATION_FLAG: "true",
            })

    def test_canary_entrypoints_may_be_staged_while_generation_stays_off(self):
        self.assertTrue(canary_start.canary_entrypoints_enabled({
            canary_start.CANARY_FLAG: "true",
            canary_start.GENERATION_FLAG: "false",
        }))

    def test_default_commands_are_byte_for_byte_legacy_selection(self):
        config = supervisor_config()
        expected = render_start.child_commands(config, executable="python-test")
        actual = canary_start.child_commands(
            config,
            executable="python-test",
            environ={},
        )
        self.assertEqual(actual, expected)
        self.assertIn(canary_start.LEGACY_API_LOOP, actual["api_loop"])
        self.assertIn(canary_start.LEGACY_RELAY, actual["relay"])

    def test_enabled_gate_swaps_only_api_loop_and_relay_targets(self):
        config = supervisor_config(autonomous_wake_enabled=True)
        legacy = render_start.child_commands(config, executable="python-test")
        actual = canary_start.child_commands(
            config,
            executable="python-test",
            environ={
                canary_start.CANARY_FLAG: "true",
                canary_start.GENERATION_FLAG: "false",
            },
        )
        self.assertIn(canary_start.CANARY_API_LOOP, actual["api_loop"])
        self.assertNotIn(canary_start.LEGACY_API_LOOP, actual["api_loop"])
        self.assertIn(canary_start.CANARY_RELAY, actual["relay"])
        self.assertNotIn(canary_start.LEGACY_RELAY, actual["relay"])
        self.assertEqual(actual["autonomous_wake"], legacy["autonomous_wake"])

        normalized = {name: list(command) for name, command in actual.items()}
        normalized["api_loop"] = [
            canary_start.LEGACY_API_LOOP if item == canary_start.CANARY_API_LOOP else item
            for item in normalized["api_loop"]
        ]
        normalized["relay"] = [
            canary_start.LEGACY_RELAY if item == canary_start.CANARY_RELAY else item
            for item in normalized["relay"]
        ]
        self.assertEqual(normalized, legacy)

    def test_wrapper_main_restores_original_supervisor_selector(self):
        original = render_start.child_commands
        observed = {}

        def fake_main():
            observed["selector"] = render_start.child_commands
            return 17

        with mock.patch.dict(os.environ, {
            canary_start.CANARY_FLAG: "false",
            canary_start.GENERATION_FLAG: "false",
        }, clear=False), mock.patch.object(render_start, "main", side_effect=fake_main):
            self.assertEqual(canary_start.main(), 17)
        self.assertIsNot(observed["selector"], original)
        self.assertIs(render_start.child_commands, original)

    def test_current_render_blueprint_still_uses_legacy_supervisor_and_has_no_activation_flags(self):
        payload = json.loads((ROOT / "render.yaml").read_text(encoding="utf-8"))
        service = payload["services"][0]
        self.assertEqual(service["startCommand"], "python scripts/render_start.py")
        env = {row["key"]: row for row in service["envVars"]}
        self.assertNotIn(canary_start.CANARY_FLAG, env)
        self.assertNotIn(canary_start.GENERATION_FLAG, env)
        self.assertEqual(env["CODEX_CONTROL_ENABLED"]["value"], "false")


if __name__ == "__main__":
    unittest.main()
