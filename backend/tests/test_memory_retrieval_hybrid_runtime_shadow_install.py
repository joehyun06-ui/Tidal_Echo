from __future__ import annotations

import contextlib
import os
import types
import unittest
from unittest import mock

from backend import memory_context_integration
from backend import memory_retrieval_hybrid_runtime_shadow as runtime_shadow


class HybridShadowInstallAtomicityTests(unittest.TestCase):
    def relay(self):
        @contextlib.asynccontextmanager
        async def lifespan(_application):
            yield

        original = lambda *_args, **_kwargs: memory_context_integration.TransientMemoryDispatch((), False)
        context_module = types.SimpleNamespace(
            prepare_transient_memory_dispatch=original,
        )
        memory = types.SimpleNamespace(
            enabled=True,
            configuration_valid=True,
            context_injection_enabled=True,
            smart_retrieval_enabled=True,
        )
        relay = types.SimpleNamespace(
            DEPLOYMENT=types.SimpleNamespace(memory=memory),
            memory_context_integration=context_module,
            app=types.SimpleNamespace(
                router=types.SimpleNamespace(lifespan_context=lifespan)
            ),
        )
        return relay, original, lifespan

    def test_failed_enabled_install_leaves_no_half_installed_markers_or_patches(self):
        relay, original, lifespan = self.relay()
        with mock.patch.dict(
            os.environ,
            {runtime_shadow.ENV_GATE: "true"},
            clear=False,
        ):
            with self.assertRaises(
                runtime_shadow.MemoryHybridRetrievalRuntimeShadowError
            ) as raised:
                runtime_shadow.install(relay)
        self.assertEqual(
            raised.exception.category,
            "memory_hybrid_retrieval_shadow_runner_missing",
        )
        self.assertFalse(hasattr(relay, runtime_shadow.INSTALL_MARKER))
        self.assertFalse(hasattr(relay, runtime_shadow.ENABLED_MARKER))
        self.assertFalse(hasattr(relay, runtime_shadow.ORIGINAL_PREPARE_MARKER))
        self.assertIs(
            relay.memory_context_integration.prepare_transient_memory_dispatch,
            original,
        )
        self.assertIs(relay.app.router.lifespan_context, lifespan)


if __name__ == "__main__":
    unittest.main()
