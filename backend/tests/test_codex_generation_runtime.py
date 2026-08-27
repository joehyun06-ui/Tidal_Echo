from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend import deployment_config
from backend.codex_generation_protocol import CodexGenerationConfig, CodexProcessActivityGate
from backend.codex_generation_runtime import CodexGenerationRuntime
from backend.codex_generation_runtime_config import CodexGenerationRuntimeConfig


class FakeFoundation:
    def __init__(self, workspace: Path):
        self.activity_gate = CodexProcessActivityGate()
        self.generation = SimpleNamespace(config=SimpleNamespace(workspace_root=workspace))
        self.control = object()
        self.closed = False

    async def close(self):
        self.closed = True


class CodexGenerationRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def control(self, root: Path, enabled=False):
        return deployment_config.CodexControlConfig(
            enabled=enabled,
            codex_home=root / "codex-home",
            workspace=root / "codex-workspace",
            request_timeout_seconds=10,
        )

    def config(self, root: Path, enabled: bool):
        return CodexGenerationRuntimeConfig(
            generation=CodexGenerationConfig(enabled, root / "generation-workspace"),
            store_path=root / "codex-generation.db",
            poll_interval_seconds=0.05,
            persistent_root=root,
        )

    async def test_disabled_runtime_creates_no_store_or_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "persistent"
            foundation = FakeFoundation(root / "generation-workspace")
            runtime = CodexGenerationRuntime(
                control_config=self.control(root),
                generation_config=self.config(root, False),
                relay_db=root / "relay.db",
                persona_loader=lambda: "persona",
                completion_callback=lambda *_args: 1,
                _foundation=foundation,
            )
            await runtime.start()
            self.assertFalse(root.exists())
            self.assertTrue(runtime.worker_healthy())
            await runtime.close()
            self.assertTrue(foundation.closed)

    async def test_enabled_runtime_initializes_separate_store_and_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "persistent"
            relay = root / "relay.db"
            foundation = FakeFoundation(root / "generation-workspace")
            runtime = CodexGenerationRuntime(
                control_config=self.control(root),
                generation_config=self.config(root, True),
                relay_db=relay,
                persona_loader=lambda: "persona",
                completion_callback=lambda *_args: 1,
                _foundation=foundation,
            )
            await runtime.start()
            await asyncio.sleep(0)
            self.assertTrue((root / "codex-generation.db").is_file())
            self.assertFalse(relay.exists())
            self.assertTrue(runtime.worker_healthy())
            await runtime.close()
            self.assertTrue(foundation.closed)

    async def test_worker_failure_is_observable_not_silently_healthy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "persistent"
            foundation = FakeFoundation(root / "generation-workspace")
            runtime = CodexGenerationRuntime(
                control_config=self.control(root),
                generation_config=self.config(root, True),
                relay_db=root / "relay.db",
                persona_loader=lambda: "persona",
                completion_callback=lambda *_args: 1,
                _foundation=foundation,
            )

            async def fail_once():
                raise RuntimeError("worker failed")

            runtime.worker.run_once = fail_once
            await runtime.start()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(runtime.worker_healthy())
            self.assertIsInstance(runtime.worker_exception(), RuntimeError)
            await runtime.close()


if __name__ == "__main__":
    unittest.main()
