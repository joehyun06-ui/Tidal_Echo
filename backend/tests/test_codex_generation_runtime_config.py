from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend import deployment_config
from backend.codex_generation_protocol import CodexGenerationError
from backend.codex_generation_runtime_config import (
    compose_shared_transport_config,
    load_generation_runtime_config,
    prepare_generation_paths,
)


class CodexGenerationRuntimeConfigTest(unittest.TestCase):
    def control(self, root: Path, *, enabled=False):
        return deployment_config.CodexControlConfig(
            enabled=enabled,
            codex_home=root / "codex-home",
            workspace=root / "codex-workspace",
            request_timeout_seconds=10,
        )

    def test_default_off_does_not_create_persistent_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "persistent"
            cfg = load_generation_runtime_config({}, persistent_root=root)
            self.assertFalse(cfg.enabled)
            prepare_generation_paths(cfg, self.control(root))
            self.assertFalse(root.exists())

    def test_enabled_generation_prepares_home_workspace_and_store_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "persistent"
            cfg = load_generation_runtime_config(
                {"CODEX_GENERATION_ENABLED": "true"}, persistent_root=root
            )
            control = self.control(root, enabled=False)
            prepare_generation_paths(cfg, control)
            self.assertTrue(control.codex_home.is_dir())
            self.assertTrue(control.workspace.is_dir())
            self.assertTrue(cfg.generation.workspace_root.is_dir())
            self.assertTrue(cfg.store_path.parent.is_dir())
            self.assertFalse(cfg.store_path.exists())

    def test_store_must_remain_separate_from_relay_db(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            relay = root / "relay.db"
            with self.assertRaisesRegex(
                CodexGenerationError, "codex_generation_store_must_be_separate"
            ):
                load_generation_runtime_config(
                    {"CODEX_GENERATION_DB": str(relay)},
                    persistent_root=root,
                    relay_db=relay,
                )

    def test_paths_outside_persistent_root_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "persistent"
            with self.assertRaisesRegex(CodexGenerationError, "store_path"):
                load_generation_runtime_config(
                    {"CODEX_GENERATION_DB": "/tmp/outside-codex.db"},
                    persistent_root=root,
                )
            with self.assertRaisesRegex(CodexGenerationError, "workspace"):
                load_generation_runtime_config(
                    {"CODEX_GENERATION_WORKSPACE": "/tmp/outside-workspace"},
                    persistent_root=root,
                )

    def test_shared_process_gate_is_control_or_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            off = load_generation_runtime_config({}, persistent_root=root)
            gen_on = load_generation_runtime_config(
                {"CODEX_GENERATION_ENABLED": "true"}, persistent_root=root
            )
            self.assertFalse(compose_shared_transport_config(self.control(root), off).enabled)
            self.assertTrue(compose_shared_transport_config(self.control(root, enabled=True), off).enabled)
            self.assertTrue(compose_shared_transport_config(self.control(root), gen_on).enabled)

    def test_poll_interval_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(
                load_generation_runtime_config({}, persistent_root=root).poll_interval_seconds,
                0.25,
            )
            for value in ("0", "0.01", "6", "nan", " 0.25"):
                with self.subTest(value=value), self.assertRaisesRegex(
                    CodexGenerationError, "poll_seconds"
                ):
                    load_generation_runtime_config(
                        {"CODEX_GENERATION_POLL_SECONDS": value},
                        persistent_root=root,
                    )


if __name__ == "__main__":
    unittest.main()
