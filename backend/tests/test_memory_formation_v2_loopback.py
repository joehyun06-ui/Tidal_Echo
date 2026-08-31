from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from backend import memory_formation_v2_loopback as loopback
from backend.tests._support import NoNetworkMixin


SOURCE = (
    "Project Atlas uses PostgreSQL 16. filler. "
    "The project runs on port 5432."
)
FIRST = "Project Atlas uses PostgreSQL 16."
SECOND = "The project runs on port 5432."


def span(part: str) -> tuple[int, int]:
    start = SOURCE.index(part)
    return start, start + len(part)


def provider_output() -> str:
    return json.dumps(
        {
            "version": "memory-formation-extractor-v2",
            "proposals": [{
                "signal_type": "project_fact",
                "spans": [
                    {"start": span(FIRST)[0], "end": span(FIRST)[1]},
                    {"start": span(SECOND)[0], "end": span(SECOND)[1]},
                ],
            }],
        },
        separators=(",", ":"),
    )


class MemoryFormationV2LoopbackTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_server_owns_provider_contract_and_returns_ranges_only(self):
        calls = []

        class Legacy:
            LOOP_CONFIG = Path(self.temp.name) / "loop.json"

            @staticmethod
            async def run_kelivo_provider_contract(
                provider_model, messages, *, temperature, max_tokens
            ):
                calls.append((provider_model, messages, temperature, max_tokens))
                return {"outcome": "success", "text": provider_output()}

        with mock.patch.object(
            loopback.deployment_config,
            "resolve_kelivo_provider_contract_defaults",
            return_value=SimpleNamespace(provider_model="fixed-provider-model"),
        ):
            extraction = await loopback.run_server_extraction(Legacy, SOURCE)

        self.assertEqual(len(calls), 1)
        provider_model, messages, temperature, max_tokens = calls[0]
        self.assertEqual(provider_model, "fixed-provider-model")
        self.assertEqual(temperature, 0.0)
        self.assertEqual(max_tokens, 256)
        self.assertEqual(messages[-1], {"role": "user", "content": SOURCE})
        self.assertEqual(len(extraction.proposals), 1)
        self.assertEqual(extraction.proposals[0].signal_type, "project_fact")
        self.assertEqual(
            tuple((item.start, item.end) for item in extraction.proposals[0].spans),
            (span(FIRST), span(SECOND)),
        )
        payload = loopback._serialize_extraction(extraction)
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("Project Atlas", rendered)
        self.assertNotIn("5432", rendered)
        self.assertEqual(set(payload), {"ok", "version", "proposals"})

    async def test_client_is_loopback_only_and_revalidates_structured_response(self):
        seen = []

        async def handler(request: httpx.Request):
            seen.append(request)
            body = json.loads(request.content)
            self.assertEqual(body, {"source_text": SOURCE})
            self.assertEqual(
                request.headers.get("x-api-loop-internal-token"),
                "x" * 32,
            )
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "version": "memory-formation-extractor-v2",
                    "proposals": [{
                        "signal_type": "project_fact",
                        "spans": [
                            {"start": span(FIRST)[0], "end": span(FIRST)[1]},
                            {"start": span(SECOND)[0], "end": span(SECOND)[1]},
                        ],
                    }],
                },
            )

        extraction = await loopback.extract_v2_via_loopback(
            ingest_url="http://127.0.0.1:3020/loop/ingest",
            internal_token="x" * 32,
            source_text=SOURCE,
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.path, loopback.ENDPOINT)
        self.assertEqual(len(extraction.proposals[0].spans), 2)

        with self.assertRaises(loopback.MemoryFormationV2LoopbackError) as remote:
            await loopback.extract_v2_via_loopback(
                ingest_url="https://example.com/loop/ingest",
                internal_token="x" * 32,
                source_text=SOURCE,
                transport=httpx.MockTransport(handler),
            )
        self.assertEqual(remote.exception.category, "loopback_unavailable")

    async def test_timeout_and_malformed_response_are_fixed_categories(self):
        async def timeout_handler(_request: httpx.Request):
            return httpx.Response(
                504,
                json={"ok": False, "error": "extractor_timeout"},
            )

        with self.assertRaises(loopback.MemoryFormationV2LoopbackError) as timeout:
            await loopback.extract_v2_via_loopback(
                ingest_url="http://127.0.0.1:3020/loop/ingest",
                internal_token="x" * 32,
                source_text=SOURCE,
                transport=httpx.MockTransport(timeout_handler),
            )
        self.assertEqual(timeout.exception.category, "extractor_timeout")

        async def malformed_handler(_request: httpx.Request):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "version": "memory-formation-extractor-v2",
                    "proposals": [{
                        "signal_type": "project_fact",
                        "spans": [{"start": -1, "end": 2}],
                    }],
                },
            )

        with self.assertRaises(loopback.MemoryFormationV2LoopbackError) as malformed:
            await loopback.extract_v2_via_loopback(
                ingest_url="http://127.0.0.1:3020/loop/ingest",
                internal_token="x" * 32,
                source_text=SOURCE,
                transport=httpx.MockTransport(malformed_handler),
            )
        self.assertEqual(malformed.exception.category, "loopback_invalid_response")


if __name__ == "__main__":
    unittest.main()
