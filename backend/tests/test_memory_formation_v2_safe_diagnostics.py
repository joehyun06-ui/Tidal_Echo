from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import memory_formation_extractor_v2 as extractor
from backend import memory_formation_v2_loopback as loopback
from backend.tests._support import NoNetworkMixin


MODEL = "[Pro按量]gpt-5.6-sol"
SOURCE = "这是 Memory V2 diagnostic smoke test，不要记忆。"


def payload(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class MemoryFormationV2ParserStageTests(unittest.TestCase):
    def assert_invalid_stage(self, raw_output: object, expected_stage: str) -> None:
        with self.assertRaises(extractor.MemoryFormationExtractorV2Error) as failure:
            extractor._parse_model_output(raw_output, 12)
        self.assertEqual(failure.exception.category, "extractor_invalid_output")
        self.assertEqual(failure.exception.stage, expected_stage)
        self.assertEqual(str(failure.exception), "extractor_invalid_output")

    def test_failure_stages_are_bounded_and_data_free(self):
        cases = (
            (None, "response_text"),
            ("```json\n{}", "json_decode"),
            (
                payload({"version": "wrong", "proposals": []}),
                "envelope",
            ),
            (
                payload({
                    "version": extractor.EXTRACTOR_CONTRACT_VERSION,
                    "proposals": [
                        {"signal_type": "project_fact", "spans": []},
                    ],
                }),
                "proposal_shape",
            ),
            (
                payload({
                    "version": extractor.EXTRACTOR_CONTRACT_VERSION,
                    "proposals": [
                        {
                            "signal_type": "project_fact",
                            "spans": [{"start": 0, "end": 99}],
                        },
                    ],
                }),
                "span_bounds",
            ),
            (
                payload({
                    "version": extractor.EXTRACTOR_CONTRACT_VERSION,
                    "proposals": [
                        {
                            "signal_type": "project_fact",
                            "spans": [
                                {"start": 0, "end": 4},
                                {"start": 3, "end": 6},
                            ],
                        },
                    ],
                }),
                "semantic_validation",
            ),
        )
        for raw_output, expected_stage in cases:
            with self.subTest(expected_stage=expected_stage):
                self.assert_invalid_stage(raw_output, expected_stage)


class MemoryFormationV2SafeDiagnosticTests(
    NoNetworkMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _legacy(self, finish_reason: object, *, model_text: str):
        legacy = SimpleNamespace(
            LOOP_CONFIG=Path(self.temp.name) / "loop.json",
            json=json,
        )

        async def run_kelivo_provider_contract(
            provider_model,
            messages,
            *,
            temperature,
            max_tokens,
        ):
            self.assertEqual(provider_model, MODEL)
            self.assertEqual(temperature, 0.0)
            self.assertEqual(max_tokens, extractor.EXTRACTOR_MAX_TOKENS)
            provider_envelope = {
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": "provider text is never logged"},
                    }
                ]
            }
            legacy.json.loads(json.dumps(provider_envelope))
            return {"outcome": "success", "text": model_text}

        legacy.run_kelivo_provider_contract = run_kelivo_provider_contract
        return legacy

    async def _run_invalid(self, finish_reason: object, *, model_text: str) -> str:
        legacy = self._legacy(finish_reason, model_text=model_text)
        stderr = io.StringIO()
        with mock.patch.object(
            loopback.deployment_config,
            "resolve_kelivo_provider_contract_defaults",
            return_value=SimpleNamespace(provider_model=MODEL),
        ):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(loopback.MemoryFormationV2LoopbackError) as failure:
                    await loopback.run_server_extraction(legacy, SOURCE)
        self.assertEqual(failure.exception.category, "extractor_invalid_output")
        return stderr.getvalue()

    async def test_finish_reason_and_parser_stage_are_logged_without_body(self):
        secret_output = "```json\n{\"secret\":\"DO_NOT_LOG_ME\"}\n```"
        log = await self._run_invalid("length", model_text=secret_output)

        self.assertIn(
            "[memory-formation-v2-extractor-diagnostic] "
            "status=failed category=extractor_invalid_output ",
            log,
        )
        self.assertIn("failure_stage=json_decode", log)
        self.assertIn("finish_reason=length", log)
        self.assertNotIn("DO_NOT_LOG_ME", log)
        self.assertNotIn(SOURCE, log)
        self.assertNotIn("provider text is never logged", log)

    async def test_unknown_finish_reason_is_collapsed_to_other(self):
        provider_value = "PRIVATE_PROVIDER_VALUE_SHOULD_NOT_LOG"
        log = await self._run_invalid(
            provider_value,
            model_text=payload({
                "version": "wrong",
                "proposals": [],
            }),
        )

        self.assertIn("failure_stage=envelope", log)
        self.assertIn("finish_reason=other", log)
        self.assertNotIn(provider_value, log)

    async def test_successful_empty_extraction_emits_no_failure_diagnostic(self):
        legacy = self._legacy(
            "stop",
            model_text=payload({
                "version": extractor.EXTRACTOR_CONTRACT_VERSION,
                "proposals": [],
            }),
        )
        stderr = io.StringIO()
        with mock.patch.object(
            loopback.deployment_config,
            "resolve_kelivo_provider_contract_defaults",
            return_value=SimpleNamespace(provider_model=MODEL),
        ):
            with contextlib.redirect_stderr(stderr):
                result = await loopback.run_server_extraction(legacy, SOURCE)

        self.assertEqual(result.proposals, ())
        self.assertNotIn("memory-formation-v2-extractor-diagnostic", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
