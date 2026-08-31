from __future__ import annotations

import asyncio
import dataclasses
import json
import unittest
from unittest import mock

from backend import memory_formation_extractor_v2 as extractor


def output(proposals, *, version=extractor.EXTRACTOR_CONTRACT_VERSION, **extra):
    return json.dumps(
        {"version": version, "proposals": proposals, **extra},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class MemoryFormationExtractorV2Tests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, raw_output, *, source="abcdefghij", capture=None):
        calls = [] if capture is None else capture

        async def generation(*args):
            calls.append(args)
            return {"text": raw_output, "usage": {"total_tokens": 1}}

        result = await extractor.extract_auto_memory_proposals_v2(
            generation,
            source,
            provider_model="provider-model",
            provider_prompt_contract_version="kelivo-provider-prompt-v1",
        )
        return result, calls

    async def assert_invalid(self, raw_output, *, source="abcdefghij"):
        with self.assertRaises(extractor.MemoryFormationExtractorV2Error) as raised:
            await self.invoke(raw_output, source=source)
        self.assertEqual(raised.exception.category, "extractor_invalid_output")
        return raised.exception

    async def test_one_atomic_proposal_may_bind_multiple_source_spans(self):
        source = "A😀中B--C123--D"
        raw = output([{
            "signal_type": "project_fact",
            "spans": [
                {"start": 1, "end": 4},
                {"start": 6, "end": 10},
            ],
        }])
        result, calls = await self.invoke(raw, source=source)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result.proposals), 1)
        item = result.proposals[0]
        self.assertEqual(item.signal_type, "project_fact")
        self.assertEqual(
            [source[span.start:span.end] for span in item.spans],
            ["😀中B", "C123"],
        )

    async def test_empty_and_bounded_proposals_are_accepted(self):
        cases = (
            [],
            [{
                "signal_type": "stable_profile",
                "spans": [{"start": 0, "end": 1}],
            }],
            [
                {
                    "signal_type": "stable_profile",
                    "spans": [{"start": 0, "end": 1}, {"start": 2, "end": 3}],
                },
                {
                    "signal_type": "project_fact",
                    "spans": [{"start": 4, "end": 5}],
                },
                {
                    "signal_type": "task_progress",
                    "spans": [{"start": 6, "end": 7}],
                },
            ],
        )
        for proposals in cases:
            with self.subTest(count=len(proposals)):
                result, _ = await self.invoke(output(proposals))
                self.assertEqual(len(result.proposals), len(proposals))

    async def test_model_cannot_supply_subject_candidate_text_or_other_server_fields(self):
        forbidden = (
            ("subject", "assistant"),
            ("actor", "assistant"),
            ("content", "PRIVATE"),
            ("normalized_content", "PRIVATE"),
            ("kind", "assistant_experience"),
            ("scope", "global_user"),
            ("confidence", 1),
            ("summary", "PRIVATE"),
            ("entities", ["PRIVATE"]),
        )
        for key, value in forbidden:
            proposal = {
                "signal_type": "stable_profile",
                "spans": [{"start": 0, "end": 1}],
                key: value,
            }
            with self.subTest(key=key):
                await self.assert_invalid(output([proposal]))

    async def test_span_objects_accept_only_exact_start_end_keys(self):
        for key, value in (
            ("text", "PRIVATE"),
            ("subject", "user"),
            ("confidence", 1),
        ):
            span = {"start": 0, "end": 1, key: value}
            with self.subTest(key=key):
                await self.assert_invalid(output([{
                    "signal_type": "stable_profile",
                    "spans": [span],
                }]))

    async def test_unknown_signals_and_invalid_offsets_are_rejected(self):
        await self.assert_invalid(output([{
            "signal_type": "unknown",
            "spans": [{"start": 0, "end": 1}],
        }]))
        for value in (True, False, 1.0, "1", None):
            for field in ("start", "end"):
                span = {"start": 0, "end": 1}
                span[field] = value
                with self.subTest(field=field, value=value):
                    await self.assert_invalid(output([{
                        "signal_type": "stable_profile",
                        "spans": [span],
                    }]))
        for start, end in ((-1, 1), (2, 1), (1, 1), (0, 11)):
            with self.subTest(start=start, end=end):
                await self.assert_invalid(output([{
                    "signal_type": "stable_profile",
                    "spans": [{"start": start, "end": end}],
                }]))

    async def test_empty_too_many_duplicate_and_overlapping_spans_are_rejected(self):
        await self.assert_invalid(output([{
            "signal_type": "stable_profile",
            "spans": [],
        }]))
        await self.assert_invalid(output([{
            "signal_type": "stable_profile",
            "spans": [
                {"start": 0, "end": 1},
                {"start": 2, "end": 3},
                {"start": 4, "end": 5},
                {"start": 6, "end": 7},
                {"start": 8, "end": 9},
            ],
        }]))
        await self.assert_invalid(output([{
            "signal_type": "stable_profile",
            "spans": [
                {"start": 0, "end": 2},
                {"start": 0, "end": 2},
            ],
        }]))
        await self.assert_invalid(output([{
            "signal_type": "stable_profile",
            "spans": [
                {"start": 0, "end": 4},
                {"start": 3, "end": 5},
            ],
        }]))

    async def test_cross_proposal_overlap_and_total_span_budget_are_rejected(self):
        await self.assert_invalid(output([
            {
                "signal_type": "stable_profile",
                "spans": [{"start": 0, "end": 4}],
            },
            {
                "signal_type": "project_fact",
                "spans": [{"start": 2, "end": 5}],
            },
        ]))
        nine = [
            {"start": index, "end": index + 1}
            for index in range(9)
        ]
        await self.assert_invalid(
            output([
                {"signal_type": "stable_profile", "spans": nine[:3]},
                {"signal_type": "project_fact", "spans": nine[3:6]},
                {"signal_type": "task_progress", "spans": nine[6:9]},
            ]),
            source="abcdefghijkl",
        )

    async def test_duplicate_json_keys_extra_top_level_and_wrong_version_are_rejected(self):
        duplicate = (
            '{"version":"memory-formation-extractor-v2",'
            '"proposals":[{"signal_type":"stable_profile","spans":'
            '[{"start":0,"start":0,"end":1}]}]}'
        )
        await self.assert_invalid(duplicate)
        await self.assert_invalid(output([], extra="no"))
        await self.assert_invalid(output([], version="wrong"))
        await self.assert_invalid(json.dumps({"proposals": []}))

    async def test_prompt_contract_exposes_only_source_and_range_authority(self):
        source = "PRIVATE-SOURCE Project Atlas uses PostgreSQL 16."
        result, calls = await self.invoke(output([]), source=source)
        self.assertEqual(result.proposals, ())
        self.assertEqual(len(calls), 1)
        messages, session, model, temperature, max_tokens, context = calls[0]
        self.assertEqual([message["role"] for message in messages], ["developer", "user"])
        self.assertEqual(messages[1], {"role": "user", "content": source})
        self.assertNotIn(source, messages[0]["content"])
        self.assertEqual(session, extractor.EXTRACTOR_SESSION_ID)
        self.assertEqual(model, "provider-model")
        self.assertEqual(temperature, 0.0)
        self.assertEqual(max_tokens, extractor.EXTRACTOR_MAX_TOKENS)
        self.assertEqual(
            context,
            {
                "prompt_contract_version": "kelivo-provider-prompt-v1",
                "memory_formation_extractor": "memory-formation-extractor-v2",
                "memory_formation_contract": "memory-formation-v2",
            },
        )
        instruction = messages[0]["content"].lower()
        self.assertIn("subject attribution is server-derived", instruction)
        self.assertIn("commit/deploy ids", instruction)
        self.assertIn("numbers", instruction)

    async def test_generation_is_single_attempt_bounded_and_errors_are_data_free(self):
        calls = 0

        async def slow(*_args):
            nonlocal calls
            calls += 1
            await asyncio.sleep(1)
            return {"text": output([])}

        with mock.patch.object(extractor, "EXTRACTOR_TIMEOUT_SECONDS", 0.001):
            with self.assertRaises(extractor.MemoryFormationExtractorV2Error) as raised:
                await extractor.extract_auto_memory_proposals_v2(
                    slow,
                    "source",
                    provider_model="provider-model",
                    provider_prompt_contract_version="kelivo-provider-prompt-v1",
                )
        self.assertEqual(raised.exception.category, "extractor_timeout")
        self.assertEqual(calls, 1)

        secret = "PRIVATE-MODEL-FAILURE"

        async def failing(*_args):
            raise RuntimeError(secret)

        with self.assertRaises(extractor.MemoryFormationExtractorV2Error) as raised:
            await extractor.extract_auto_memory_proposals_v2(
                failing,
                secret,
                provider_model="provider-model",
                provider_prompt_contract_version="kelivo-provider-prompt-v1",
            )
        self.assertEqual(raised.exception.category, "extractor_unavailable")
        self.assertNotIn(secret, f"{raised.exception!s} {raised.exception!r}")

    async def test_contract_objects_are_frozen_slotted_and_data_free(self):
        result, _ = await self.invoke(output([{
            "signal_type": "stable_profile",
            "spans": [{"start": 0, "end": 1}],
        }]))
        item = result.proposals[0]
        span = item.spans[0]
        self.assertEqual(
            [field.name for field in dataclasses.fields(item)],
            ["signal_type", "spans"],
        )
        self.assertEqual(repr(result), "<AutoMemoryExtractionV2>")
        self.assertEqual(repr(item), "<AutoMemoryProposalV2>")
        self.assertEqual(repr(span), "<AutoMemorySourceSpanV2>")
        self.assertFalse(hasattr(item, "__dict__"))
        self.assertFalse(hasattr(span, "__dict__"))


if __name__ == "__main__":
    unittest.main()
