from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import memory_formation_v2_loopback as loopback
from backend.tests._support import NoNetworkMixin


MODEL = "[Pro按量]gpt-5.6-sol"
NEIGHBOR = "[Pro按量]gpt-5.6-sol-preview"
SOURCE = "这是 Memory V2 路由 smoke test，不要记忆。"


def extractor_output() -> str:
    return json.dumps(
        {
            "version": "memory-formation-extractor-v2",
            "proposals": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class MemoryFormationV2Gpt56ReasoningTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _legacy(self, model: str, seen: list[dict], *, fail: bool = False):
        def chat_body(
            route,
            messages,
            *,
            stream,
            temperature=None,
            max_tokens=None,
        ):
            body = {
                "model": route["model"],
                "messages": list(messages),
                "max_completion_tokens": max_tokens,
                "stream": stream,
            }
            if temperature is not None:
                body["temperature_input"] = temperature
            return body

        legacy = SimpleNamespace(
            LOOP_CONFIG=Path(self.temp.name) / "loop.json",
            _chat_completion_body=chat_body,
        )

        async def run_kelivo_provider_contract(
            provider_model,
            messages,
            *,
            temperature,
            max_tokens,
        ):
            body = legacy._chat_completion_body(
                {"model": provider_model},
                messages,
                stream=False,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            seen.append(body)
            if fail:
                raise RuntimeError("provider failed")
            return {
                "outcome": "success",
                "text": extractor_output(),
            }

        legacy.run_kelivo_provider_contract = run_kelivo_provider_contract
        return legacy

    async def test_exact_pro_metered_model_gets_reasoning_none_only_in_v2_call(self):
        seen: list[dict] = []
        legacy = self._legacy(MODEL, seen)

        with mock.patch.object(
            loopback.deployment_config,
            "resolve_kelivo_provider_contract_defaults",
            return_value=SimpleNamespace(provider_model=MODEL),
        ):
            extraction = await loopback.run_server_extraction(legacy, SOURCE)

        self.assertEqual(extraction.proposals, ())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["model"], MODEL)
        self.assertEqual(
            seen[0]["max_completion_tokens"],
            loopback.memory_formation_extractor_v2.EXTRACTOR_MAX_TOKENS,
        )
        self.assertEqual(seen[0]["temperature_input"], 0.0)
        self.assertEqual(seen[0]["reasoning_effort"], "none")

        ordinary = legacy._chat_completion_body(
            {"model": MODEL},
            [{"role": "user", "content": "ordinary kelivo"}],
            stream=False,
            temperature=0.0,
            max_tokens=256,
        )
        self.assertNotIn("reasoning_effort", ordinary)

    async def test_neighboring_model_is_not_modified(self):
        seen: list[dict] = []
        legacy = self._legacy(NEIGHBOR, seen)

        with mock.patch.object(
            loopback.deployment_config,
            "resolve_kelivo_provider_contract_defaults",
            return_value=SimpleNamespace(provider_model=NEIGHBOR),
        ):
            extraction = await loopback.run_server_extraction(legacy, SOURCE)

        self.assertEqual(extraction.proposals, ())
        self.assertEqual(len(seen), 1)
        self.assertNotIn("reasoning_effort", seen[0])

    async def test_reasoning_context_resets_after_provider_failure(self):
        seen: list[dict] = []
        legacy = self._legacy(MODEL, seen, fail=True)

        with mock.patch.object(
            loopback.deployment_config,
            "resolve_kelivo_provider_contract_defaults",
            return_value=SimpleNamespace(provider_model=MODEL),
        ):
            with self.assertRaises(loopback.MemoryFormationV2LoopbackError) as failure:
                await loopback.run_server_extraction(legacy, SOURCE)

        self.assertEqual(failure.exception.category, "extractor_unavailable")
        self.assertEqual(seen[0]["reasoning_effort"], "none")

        ordinary = legacy._chat_completion_body(
            {"model": MODEL},
            [{"role": "user", "content": "ordinary kelivo"}],
            stream=False,
            temperature=0.0,
            max_tokens=256,
        )
        self.assertNotIn("reasoning_effort", ordinary)


if __name__ == "__main__":
    unittest.main()
