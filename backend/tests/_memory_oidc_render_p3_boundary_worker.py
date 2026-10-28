"""Actual P3 Render entry boundary rehearsal: no TCP, IdP or memory writes.

The deployed Render service executes scripts/render_start_p3.py rather than
scripts/render_start.py. Check that the wrapper inherits the base supervisor's
OFF rules for both ordinary P3 and the independently qualified Codex branch.
"""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.deployment_config import DeploymentConfigError
from backend.tests._support import NoNetworkMixin
from scripts import render_start, render_start_p3

CONFIG = SimpleNamespace(
    deployment=SimpleNamespace(loop_port=3020),
    relay_port=10000, autonomous_wake_enabled=False,
)
PLAIN = {"CODEX_CANARY_ENTRYPOINTS_ENABLED": "false",
         "CODEX_GENERATION_ENABLED": "false",
         "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "false"}
CANARY = {"CODEX_CANARY_ENTRYPOINTS_ENABLED": "true",
          "CODEX_GENERATION_ENABLED": "true",
          "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "false"}


def commands(env=None):
    return render_start_p3.child_commands(
        CONFIG, executable="python-review",
        environ=PLAIN if env is None else env,
    )


def make_bad_base(*, remove=None, extra=None, change_host=None):
    def source(config, executable=None):
        raw = render_start.child_commands(config, executable=executable)
        process = "relay"
        if remove is not None:
            raw[process].remove(remove)
        if extra is not None:
            raw[process].append(extra)
        if change_host is not None:
            index = raw[process].index("--host")
            raw[process][index + 1] = change_host
        return raw
    return source


class RealP3SupervisorBoundaryTests(NoNetworkMixin, unittest.TestCase):
    def test_regular_p3_targets_and_unchanged_bindings(self):
        actual = commands()
        self.assertEqual(actual["api_loop"][3],
                         "examples.api_loop_provider_guard:app")
        self.assertEqual(actual["relay"][3], "backend.p3_relay_app:app")
        self.assertEqual(actual["api_loop"][6:8], ["--port", "3020"])
        self.assertEqual(actual["relay"][6:8], ["--port", "10000"])

    def test_codex_p3_explicitly_qualified_targets(self):
        actual = commands(CANARY)
        self.assertEqual(actual["api_loop"][3],
                         "examples.api_loop_codex_canary:app")
        self.assertEqual(actual["relay"][3], "backend.p3_codex_relay_app:app")

    def test_p3_all_modes_inherit_exact_proxy_disable_flags(self):
        for name, env in (("regular", PLAIN), ("codex", CANARY)):
            with self.subTest(mode=name):
                actual = commands(env)
                self.assertEqual(set(actual), {"relay", "api_loop"})
                for process, host in (("api_loop", "127.0.0.1"),
                                      ("relay", "0.0.0.0")):
                    cmd = actual[process]
                    self.assertEqual(cmd.count("--no-proxy-headers"), 1)
                    self.assertNotIn("--proxy-headers", cmd)
                    self.assertNotIn("--forwarded-allow-ips", cmd)
                    self.assertEqual(cmd[cmd.index("--host") + 1], host)
                    self.assertIn("--no-access-log", cmd)
                    self.assertEqual(cmd[cmd.index("--workers") + 1], "1")

    def test_untrusted_proxy_options_rejected_before_any_child_execution(self):
        for name, source in (
            ("missing_off", make_bad_base(remove="--no-proxy-headers")),
            ("turned_on", make_bad_base(extra="--proxy-headers")),
            ("wildcard", make_bad_base(extra="--forwarded-allow-ips")),
            ("wrong_host", make_bad_base(change_host="127.0.0.1")),
        ):
            for mode, env in (("regular", PLAIN), ("codex", CANARY)):
                with self.subTest(broken=name, mode=mode):
                    with self.assertRaisesRegex(
                        DeploymentConfigError,
                        "p3_proxy_header_boundary_unreviewed",
                    ):
                        render_start_p3._select_child_commands(
                            source, CONFIG, executable="python-review",
                            environ=env,
                        )

    def test_missing_api_loop_rejected(self):
        def missing(config, executable=None):
            commands = render_start.child_commands(config, executable=executable)
            commands.pop("api_loop")
            return commands

        with self.assertRaises(DeploymentConfigError):
            render_start_p3._select_child_commands(
                missing, CONFIG, environ=PLAIN,
            )

    def test_direct_p3_selector_rejects_oidc_even_without_preflight(self):
        for flag in (
            "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED",
            "MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED",
            "MEMORY_OIDC_FUTURE_BROWSER_LOGIN_ENABLED",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED",
            "MEMORY_PER_USER_AUTH_REVIEW_ENABLED",
            "MEMORY_BROWSER_CONSENT_REVIEW_ENABLED",
        ):
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(
                    DeploymentConfigError,
                    "unreviewed_memory_browser_identity_ingress",
                ):
                    commands({**PLAIN, flag: "true"})

    def test_p3_selector_refuses_bad_review_flag_before_base_selector(self):
        base = mock.Mock()
        with self.assertRaisesRegex(
            DeploymentConfigError, "unreviewed_memory_browser_identity_ingress",
        ):
            render_start_p3._select_child_commands(
                base, CONFIG, environ={
                    **PLAIN, "MEMORY_OIDC_FUTURE_REMOTE_APPROVED": "true",
                },
            )
        base.assert_not_called()

    def test_p3_main_rejects_oidc_before_codex_check_and_base_main(self):
        original = render_start.child_commands
        with mock.patch.dict(os.environ, {
            "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "true",
            "CODEX_CANARY_ENTRYPOINTS_ENABLED": "false",
            "CODEX_GENERATION_ENABLED": "false",
        }, clear=True), mock.patch.object(
            render_start_p3, "codex_entrypoints_enabled",
        ) as codex, mock.patch.object(render_start, "main") as base_main, \
             mock.patch.object(render_start, "safe_log") as logged:
            outcome = render_start_p3.main()
        self.assertEqual(outcome, 2)
        self.assertIs(render_start.child_commands, original)
        codex.assert_not_called()
        base_main.assert_not_called()
        logged.assert_called_once()
        self.assertEqual(logged.call_args.args[-1],
                         "unreviewed_memory_browser_identity_ingress")

    def test_p3_main_remains_disabled_for_review_flags_during_codex_mode(self):
        for flag in ("MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED",
                     "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED"):
            with self.subTest(flag=flag), mock.patch.dict(os.environ, {
                **CANARY, flag: "true",
            }, clear=True), mock.patch.object(render_start, "main") as base:
                self.assertEqual(render_start_p3.main(), 2)
                base.assert_not_called()

    def test_p3_main_regular_path_monkeypatches_and_restores_selector(self):
        original = render_start.child_commands
        seen = []

        def faux_main():
            seen.append(render_start.child_commands(
                CONFIG, executable="python-review",
            ))
            return 0

        with mock.patch.dict(os.environ, PLAIN, clear=True), \
             mock.patch.object(render_start, "main", side_effect=faux_main):
            result = render_start_p3.main()
        self.assertEqual(result, 0)
        self.assertEqual(seen[0]["relay"][3], "backend.p3_relay_app:app")
        self.assertIn("--no-proxy-headers", seen[0]["relay"])
        self.assertIs(render_start.child_commands, original)

    def test_p3_main_codex_path_restores_selector(self):
        original = render_start.child_commands
        seen = []
        def faux_main():
            seen.append(render_start.child_commands(
                CONFIG, executable="python-review",
            ))
            return 0
        with mock.patch.dict(os.environ, CANARY, clear=True), \
             mock.patch.object(render_start, "main", side_effect=faux_main):
            self.assertEqual(render_start_p3.main(), 0)
        self.assertEqual(seen[0]["relay"][3],
                         "backend.p3_codex_relay_app:app")
        self.assertIs(render_start.child_commands, original)

    def test_p3_main_restores_selector_on_base_failure(self):
        original = render_start.child_commands
        with mock.patch.dict(os.environ, PLAIN, clear=True), \
             mock.patch.object(render_start, "main", side_effect=RuntimeError(
                 "synthetic_internal_failure",
             )):
            with self.assertRaisesRegex(RuntimeError, "synthetic_internal_failure"):
                render_start_p3.main()
        self.assertIs(render_start.child_commands, original)

    def test_p3_does_not_turn_review_flags_into_identity_permissions(self):
        with mock.patch.dict(os.environ, PLAIN, clear=True):
            self.assertFalse(render_start_p3.codex_entrypoints_enabled())
        self.assertFalse(hasattr(render_start_p3, "issue_browser_session"))
        self.assertFalse(hasattr(render_start_p3, "authorize_memory_write"))

    def test_p3_launch_contract_exact_live_service_command_static(self):
        # This verifies the documented command is a P3 module entry; it
        # does not pretend to read any live Render environment or deploy.
        self.assertTrue(callable(render_start_p3.main))
        self.assertIsNot(render_start_p3.main, render_start.main)


if __name__ == "__main__":
    unittest.main()
