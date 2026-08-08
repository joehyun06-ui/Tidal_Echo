from __future__ import annotations

import asyncio
import dataclasses
import json
import unittest
from unittest import mock

from backend import memory_formation_extractor as extractor
from backend.memory_formation import SIGNAL_KIND_MAPPING


def output(proposals, *, version=extractor.EXTRACTOR_CONTRACT_VERSION, **extra):
    return json.dumps(
        {"version": version, "proposals": proposals, **extra},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class MemoryFormationExtractorTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, raw_output, *, source="abcdefghij", capture=None):
        calls = [] if capture is None else capture

        async def generation(*args):
            calls.append(args)
            return {"text": raw_output, "usage": {"total_tokens": 1}}

        result = await extractor.extract_auto_memory_proposals(
            generation,
            source,
            provider_model="provider-model",
            provider_prompt_contract_version="kelivo-provider-prompt-v1",
        )
        return result, calls

    async def assert_invalid(self, raw_output, *, source="abcdefghij"):
        with self.assertRaises(extractor.MemoryFormationExtractorError) as raised:
            await self.invoke(raw_output, source=source)
        self.assertEqual(raised.exception.category, "extractor_invalid_output")
        return raised.exception

    async def test_empty_and_one_two_three_proposals_are_accepted(self):
        cases = (
            [],
            [{"signal_type": "stable_profile", "start": 0, "end": 1}],
            [
                {"signal_type": "stable_profile", "start": 0, "end": 1},
                {"signal_type": "project_fact", "start": 1, "end": 2},
            ],
            [
                {"signal_type": "stable_profile", "start": 0, "end": 1},
                {"signal_type": "project_fact", "start": 1, "end": 2},
                {"signal_type": "task_progress", "start": 2, "end": 3},
            ],
        )
        for proposals in cases:
            with self.subTest(count=len(proposals)):
                result, calls = await self.invoke(output(proposals))
                self.assertEqual(len(result.proposals), len(proposals))
                self.assertEqual(len(calls), 1)

    async def test_more_than_three_proposals_is_rejected(self):
        proposals = [
            {"signal_type": "stable_profile", "start": index, "end": index + 1}
            for index in range(4)
        ]
        await self.assert_invalid(output(proposals))

    async def test_all_seven_signal_types_are_accepted_exactly(self):
        self.assertEqual(len(SIGNAL_KIND_MAPPING), 7)
        for signal_type in SIGNAL_KIND_MAPPING:
            with self.subTest(signal_type=signal_type):
                result, _ = await self.invoke(output([{
                    "signal_type": signal_type,
                    "start": 0,
                    "end": 1,
                }]))
                self.assertEqual(result.proposals[0].signal_type, signal_type)

    async def test_unknown_signal_is_rejected(self):
        await self.assert_invalid(output([{
            "signal_type": "unknown",
            "start": 0,
            "end": 1,
        }]))

    async def test_bool_float_string_and_null_offsets_are_rejected(self):
        for value in (True, False, 1.0, "1", None):
            for field in ("start", "end"):
                proposal = {"signal_type": "stable_profile", "start": 0, "end": 1}
                proposal[field] = value
                with self.subTest(field=field, value=value):
                    await self.assert_invalid(output([proposal]))

    async def test_negative_reversed_empty_and_out_of_range_offsets_are_rejected(self):
        for start, end in ((-1, 1), (2, 1), (1, 1), (0, 11), (12, 13)):
            with self.subTest(start=start, end=end):
                await self.assert_invalid(output([{
                    "signal_type": "stable_profile",
                    "start": start,
                    "end": end,
                }]))

    async def test_emoji_and_cjk_offsets_use_python_code_points(self):
        source = "A😀中B"
        result, _ = await self.invoke(output([{
            "signal_type": "shared_episode",
            "start": 1,
            "end": 3,
        }]), source=source)
        proposal = result.proposals[0]
        self.assertEqual(source[proposal.start:proposal.end], "😀中")
        self.assertEqual(len(source), 4)

    async def test_duplicate_json_keys_are_rejected_at_every_level(self):
        cases = (
            '{"version":"memory-formation-extractor-v1","version":"memory-formation-extractor-v1","proposals":[]}',
            '{"version":"memory-formation-extractor-v1","proposals":[{"signal_type":"stable_profile","start":0,"start":0,"end":1}]}',
        )
        for raw in cases:
            with self.subTest(raw=raw[:30]):
                await self.assert_invalid(raw)

    async def test_nan_and_infinity_are_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            raw = (
                '{"version":"memory-formation-extractor-v1","proposals":['
                '{"signal_type":"stable_profile","start":0,"end":'
                f"{value}" + "}]}"
            )
            with self.subTest(value=value):
                await self.assert_invalid(raw)

    async def test_markdown_fences_trailing_prose_and_non_json_are_rejected(self):
        valid = output([])
        for raw in (f"```json\n{valid}\n```", valid + " trailing", "not json"):
            with self.subTest(raw=raw[:20]):
                await self.assert_invalid(raw)

    async def test_extra_top_level_and_proposal_keys_are_rejected(self):
        await self.assert_invalid(output([], confidence=1))
        for key in ("content", "normalized_content", "kind", "scope", "confidence"):
            proposal = {
                "signal_type": "stable_profile",
                "start": 0,
                "end": 1,
                key: "PRIVATE-CANDIDATE",
            }
            with self.subTest(key=key):
                await self.assert_invalid(output([proposal]))

    async def test_wrong_missing_and_non_string_versions_are_rejected(self):
        for raw in (
            output([], version="wrong"),
            json.dumps({"proposals": []}),
            json.dumps({"version": None, "proposals": []}),
        ):
            await self.assert_invalid(raw)

    async def test_oversized_bytes_and_malformed_unicode_outputs_are_rejected(self):
        for raw in (
            "x" * (extractor.EXTRACTOR_RESPONSE_MAX_CHARS + 1),
            b"\xff",
            "\ud800",
        ):
            with self.subTest(kind=type(raw).__name__):
                await self.assert_invalid(raw)

    async def test_source_unicode_and_bounds_fail_closed(self):
        async def generation(*_args):
            raise AssertionError("generation must not run")

        for source in ("", "\ud800", "x" * 8001, b"bytes"):
            with self.subTest(kind=type(source).__name__), self.assertRaises(
                extractor.MemoryFormationExtractorError
            ) as raised:
                await extractor.extract_auto_memory_proposals(
                    generation,
                    source,
                    provider_model="provider-model",
                    provider_prompt_contract_version="kelivo-provider-prompt-v1",
                )
            self.assertEqual(raised.exception.category, "invalid_source_text")

    async def test_prompt_and_generation_contract_are_exact_and_isolated(self):
        source = "PRIVATE-SOURCE 我一直喜欢咖啡。"
        result, calls = await self.invoke(output([]), source=source)
        self.assertEqual(result.proposals, ())
        self.assertEqual(len(calls), 1)
        messages, session, model, temperature, max_tokens, context = calls[0]
        self.assertEqual([message["role"] for message in messages], ["developer", "user"])
        self.assertEqual(messages[1], {"role": "user", "content": source})
        self.assertNotIn(source, messages[0]["content"])
        self.assertNotIn("persona", json.dumps(messages, ensure_ascii=False).lower())
        self.assertNotIn("assistant", [message["role"] for message in messages])
        self.assertEqual(session, extractor.EXTRACTOR_SESSION_ID)
        self.assertEqual(model, "provider-model")
        self.assertEqual(temperature, 0.0)
        self.assertLessEqual(max_tokens, 256)
        self.assertEqual(max_tokens, extractor.EXTRACTOR_MAX_TOKENS)
        self.assertEqual(context, {
            "prompt_contract_version": "kelivo-provider-prompt-v1",
            "memory_formation_extractor": extractor.EXTRACTOR_CONTRACT_VERSION,
        })
        serialized = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn("message_id", serialized)
        self.assertNotIn("idempotency", serialized)

    async def test_timeout_is_bounded_and_generation_is_never_retried(self):
        calls = 0

        async def generation(*_args):
            nonlocal calls
            calls += 1
            await asyncio.sleep(1)
            return {"text": output([])}

        with mock.patch.object(extractor, "EXTRACTOR_TIMEOUT_SECONDS", 0.001):
            with self.assertRaises(extractor.MemoryFormationExtractorError) as raised:
                await extractor.extract_auto_memory_proposals(
                    generation,
                    "source",
                    provider_model="provider-model",
                    provider_prompt_contract_version="kelivo-provider-prompt-v1",
                )
        self.assertEqual(raised.exception.category, "extractor_unavailable")
        self.assertEqual(calls, 1)

    async def test_generation_failures_and_model_output_never_enter_errors(self):
        secret = "PRIVATE-MODEL-OUTPUT-AND-SOURCE"

        async def generation(*_args):
            raise RuntimeError(secret)

        with self.assertRaises(extractor.MemoryFormationExtractorError) as raised:
            await extractor.extract_auto_memory_proposals(
                generation,
                secret,
                provider_model="provider-model",
                provider_prompt_contract_version="kelivo-provider-prompt-v1",
            )
        combined = f"{raised.exception!s} {raised.exception!r}"
        self.assertNotIn(secret, combined)
        self.assertEqual(raised.exception.category, "extractor_unavailable")

        malformed = await self.assert_invalid(secret, source=secret)
        self.assertNotIn(secret, f"{malformed!s} {malformed!r}")

    async def test_result_and_proposal_repr_are_data_free(self):
        result, _ = await self.invoke(output([{
            "signal_type": "stable_profile",
            "start": 0,
            "end": 1,
        }]))
        self.assertEqual([field.name for field in dataclasses.fields(result)], ["proposals"])
        self.assertEqual(repr(result), "<AutoMemoryExtractionV1>")
        self.assertEqual(repr(result.proposals[0]), "<AutoMemoryProposalV1>")


if __name__ == "__main__":
    unittest.main()
