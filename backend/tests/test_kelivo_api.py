import asyncio
import dataclasses
import json
import importlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from backend import deployment_config
from backend.tests._support import NoNetworkMixin, load_app, request


class KelivoConfigurationTests(unittest.TestCase):
    def base_env(self):
        return {
            "KELIVO_ENABLED": "true",
            "KELIVO_API_KEY": "kelivo-distinct-key-1234567890123456",
            "KELIVO_CLIENT_ID": "primary-kelivo",
            "KELIVO_API_SESSION": "shared-session",
            "KELIVO_MODEL_ALIAS": "ouou-home",
            "KELIVO_AUTO_IDEMPOTENCY_ENABLED": "false",
            "KELIVO_AUTO_IDEMPOTENCY_REPLAY_SECONDS": "300",
            "LLM_MODEL": "test-provider-model",
            "RELAY_SECRET": "relay-distinct",
            "TELEGRAM_ENABLED": "false",
            "TELEGRAM_TEST_MODE": "false",
        }

    def load(self, env):
        return deployment_config.load_deployment_config(SimpleNamespace(requested=False, enabled=False), env)

    def test_enabled_is_strict_boolean(self):
        env = self.base_env(); env["KELIVO_ENABLED"] = "maybe"
        with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "invalid_kelivo_enabled"):
            self.load(env)

    def test_enabled_requires_key(self):
        env = self.base_env(); env["KELIVO_API_KEY"] = ""
        with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "kelivo_api_key_missing"):
            self.load(env)

    def test_key_must_differ_from_every_protected_secret(self):
        for name in ("RELAY_SECRET", "TELEGRAM_WEBHOOK_SECRET", "CHANNEL_AUDIT_HMAC_SECRET", "LLM_API_KEY"):
            with self.subTest(name=name):
                env = self.base_env(); env[name] = env["KELIVO_API_KEY"]
                with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "kelivo_api_key_must_be_distinct"):
                    self.load(env)

    def test_disabled_needs_no_kelivo_secret(self):
        config = self.load({"KELIVO_ENABLED": "false", "TELEGRAM_ENABLED": "false", "TELEGRAM_TEST_MODE": "false"})
        self.assertFalse(config.kelivo.enabled)

    def test_stale_boundary_includes_queue_sqlite_and_commit_margin(self):
        env = self.base_env()
        env.update({
            "LOOP_MODEL_TOTAL_TIMEOUT_SECONDS": "120", "KELIVO_QUEUE_TIMEOUT_SECONDS": "2",
            "SQLITE_BUSY_TIMEOUT_SECONDS": "30", "KELIVO_COMPLETION_COMMIT_MARGIN_SECONDS": "15",
            "KELIVO_DISPATCH_STALE_SECONDS": "167",
        })
        with self.assertRaisesRegex(deployment_config.DeploymentConfigError,
                                    "invalid_kelivo_stale_dispatch_relationship"):
            self.load(env)
        env["KELIVO_DISPATCH_STALE_SECONDS"] = "168"
        self.assertTrue(self.load(env).kelivo.enabled)

    def test_disabled_ignores_kelivo_cross_field_relationships(self):
        env = {
            "KELIVO_ENABLED": "false", "TELEGRAM_ENABLED": "false", "TELEGRAM_TEST_MODE": "false",
            "KELIVO_GLOBAL_CONCURRENCY": "1", "KELIVO_CLIENT_CONCURRENCY": "16",
            "KELIVO_DISPATCH_STALE_SECONDS": "30", "LOOP_MODEL_TOTAL_TIMEOUT_SECONDS": "1000",
            "LOOP_DISPATCH_TIMEOUT_SECONDS": "1050",
        }
        self.assertFalse(self.load(env).kelivo.enabled)
        env["KELIVO_ENABLED"] = "true"
        env.update(self.base_env())
        env.update({"KELIVO_GLOBAL_CONCURRENCY": "1", "KELIVO_CLIENT_CONCURRENCY": "16"})
        with self.assertRaisesRegex(deployment_config.DeploymentConfigError,
                                    "invalid_kelivo_concurrency_relationship"):
            self.load(env)

    def test_auto_idempotency_configuration_is_explicit_and_bounded(self):
        disabled = {
            "KELIVO_ENABLED": "false", "TELEGRAM_ENABLED": "false", "TELEGRAM_TEST_MODE": "false",
            "KELIVO_AUTO_IDEMPOTENCY_ENABLED": "not-a-boolean",
            "KELIVO_AUTO_IDEMPOTENCY_REPLAY_SECONDS": "not-an-integer",
        }
        self.assertFalse(self.load(disabled).kelivo.auto_idempotency_enabled)
        for name, value, category in (
            ("KELIVO_AUTO_IDEMPOTENCY_ENABLED", "maybe", "invalid_kelivo_auto_idempotency_enabled"),
            ("KELIVO_AUTO_IDEMPOTENCY_REPLAY_SECONDS", "59", "invalid_kelivo_auto_idempotency_replay_seconds"),
            ("KELIVO_AUTO_IDEMPOTENCY_REPLAY_SECONDS", "3601", "invalid_kelivo_auto_idempotency_replay_seconds"),
            ("KELIVO_AUTO_IDEMPOTENCY_REPLAY_SECONDS", "300.0", "invalid_kelivo_auto_idempotency_replay_seconds"),
        ):
            env = self.base_env(); env[name] = value
            with self.subTest(name=name, value=value), self.assertRaisesRegex(
                deployment_config.DeploymentConfigError, category,
            ):
                self.load(env)
        env = self.base_env(); env["KELIVO_AUTO_IDEMPOTENCY_ENABLED"] = "true"
        with self.assertRaisesRegex(deployment_config.DeploymentConfigError,
                                    "invalid_kelivo_auto_idempotency_replay_window"):
            self.load(env)
        env["KELIVO_AUTO_IDEMPOTENCY_REPLAY_SECONDS"] = "301"
        config = self.load(env)
        self.assertTrue(config.kelivo.auto_idempotency_enabled)
        self.assertEqual(config.kelivo.auto_idempotency_replay_seconds, 301)


class KelivoApiTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name, kelivo=True)
        self.headers = {
            "Authorization": "Bearer test-kelivo-key-distinct-1234567890",
            "Idempotency-Key": "request-key-0001",
        }
        self.calls = []

        async def generate(messages, api_session, provider_model, temperature, max_tokens, context):
            self.calls.append((messages, api_session, provider_model, temperature, max_tokens, context))
            return {"text": "model reply", "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}

        self.module.KELIVO_GENERATOR = generate

    def payload(self, **updates):
        body = {
            "model": "ouou-home",
            "messages": [{"role": "user", "content": "current question"}],
            "stream": False,
        }
        body.update(updates)
        return body

    def enable_auto_idempotency(self, replay_seconds=600):
        self.module.DEPLOYMENT = dataclasses.replace(
            self.module.DEPLOYMENT,
            kelivo=dataclasses.replace(
                self.module.DEPLOYMENT.kelivo,
                auto_idempotency_enabled=True,
                auto_idempotency_replay_seconds=replay_seconds,
            ),
        )

    @property
    def auto_headers(self):
        return {"Authorization": self.headers["Authorization"]}

    async def test_missing_idempotency_header_requires_explicit_compatibility_switch(self):
        disabled = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers,
            json=self.payload(),
        )
        self.assertEqual((disabled.status_code, disabled.json()["error"]["code"]),
                         (400, "invalid_idempotency_key"))
        self.assertEqual(self.calls, [])
        self.enable_auto_idempotency()
        enabled = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers,
            json=self.payload(),
        )
        self.assertEqual(enabled.status_code, 200)
        self.assertEqual(len(self.calls), 1)
        with self.module.db() as conn:
            row = conn.execute(
                """SELECT idempotency_mode,automatic_fingerprint,automatic_replay_until,
                          idempotency_key FROM kelivo_requests"""
            ).fetchone()
        self.assertEqual(row["idempotency_mode"], "automatic")
        self.assertEqual(len(row["automatic_fingerprint"]), 64)
        self.assertIsNotNone(row["automatic_replay_until"])
        self.assertTrue(row["idempotency_key"].startswith("@auto:"))

    async def test_present_empty_whitespace_or_invalid_key_never_enters_auto_mode(self):
        self.enable_auto_idempotency()
        for index, value in enumerate(("", "   ", "short", "invalid key value")):
            with self.subTest(index=index):
                response = await request(
                    self.module, "POST", "/v1/chat/completions",
                    headers={**self.auto_headers, "Idempotency-Key": value}, json=self.payload(),
                )
                self.assertEqual((response.status_code, response.json()["error"]["code"]),
                                 (400, "invalid_idempotency_key"))
        with self.module.db() as conn:
            count = conn.execute("SELECT count(*) FROM kelivo_requests").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.calls, [])

    async def test_automatic_completed_request_replays_and_does_not_reconsume_rate_limit(self):
        self.enable_auto_idempotency()
        first = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        second = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        self.assertEqual(second.json(), first.json())
        self.assertEqual(len(self.calls), 1)
        with self.module.db() as conn:
            requests = conn.execute("SELECT count(*) FROM kelivo_requests").fetchone()[0]
            rate_count = conn.execute("SELECT sum(request_count) FROM kelivo_rate_limits").fetchone()[0]
        self.assertEqual((requests, rate_count), (1, 1))

    async def test_automatic_same_request_concurrency_calls_provider_once(self):
        self.enable_auto_idempotency()
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def slow(*_args):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"text": "automatic reply", "usage": {}}
        self.module.KELIVO_GENERATOR = slow
        first = asyncio.create_task(request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        ))
        await started.wait()
        second = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        self.assertEqual((second.status_code, second.json()["error"]["code"]),
                         (409, "idempotency_in_progress"))
        release.set()
        self.assertEqual((await first).status_code, 200)
        replay = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(calls, 1)
        self.assertEqual(self.module.KELIVO_KEY_LOCKS.entry_count, 0)

    async def test_automatic_expired_terminal_request_can_create_new_generation(self):
        self.enable_auto_idempotency()
        first = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        with self.module.db() as conn:
            conn.execute("UPDATE kelivo_requests SET automatic_replay_until='2000-01-01T00:00:00+00:00'")
        second = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(len(self.calls), 2)
        with self.module.db() as conn:
            rows = conn.execute(
                "SELECT generation_id,automatic_fingerprint FROM kelivo_requests ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["generation_id"], rows[1]["generation_id"])
        self.assertEqual(rows[0]["automatic_fingerprint"], rows[1]["automatic_fingerprint"])

    async def test_automatic_failed_request_blocks_within_window_then_can_retry_after_expiry(self):
        self.enable_auto_idempotency()
        calls = 0
        async def fail(*_args):
            nonlocal calls
            calls += 1
            raise self.module.kelivo_service.GenerationError("provider_rejected", False)
        self.module.KELIVO_GENERATOR = fail
        first = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        blocked = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        self.assertEqual((first.status_code, blocked.status_code), (502, 409))
        self.assertEqual(calls, 1)
        with self.module.db() as conn:
            conn.execute("UPDATE kelivo_requests SET automatic_replay_until='2000-01-01T00:00:00+00:00'")
        retried = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        self.assertEqual(retried.status_code, 502)
        self.assertEqual(calls, 2)
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM kelivo_requests").fetchone()[0], 2)

    async def test_automatic_uncertain_request_is_never_redispatched_even_after_window(self):
        self.enable_auto_idempotency()
        calls = 0
        async def uncertain(*_args):
            nonlocal calls
            calls += 1
            raise self.module.kelivo_service.GenerationError("model_timeout", True)
        self.module.KELIVO_GENERATOR = uncertain
        first = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        with self.module.db() as conn:
            conn.execute("UPDATE kelivo_requests SET automatic_replay_until='2000-01-01T00:00:00+00:00'")
        blocked = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        self.assertEqual((first.status_code, blocked.status_code), (504, 409))
        self.assertEqual(calls, 1)
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM kelivo_requests").fetchone()[0], 1)

    async def test_automatic_fingerprint_distinguishes_messages_and_conversation_history(self):
        self.enable_auto_idempotency()
        bodies = (
            self.payload(messages=[{"role": "user", "content": "first"}]),
            self.payload(messages=[{"role": "user", "content": "different"}]),
            self.payload(messages=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "prior answer"},
                {"role": "user", "content": "next"},
            ]),
        )
        for body in bodies:
            response = await request(
                self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=body,
            )
            self.assertEqual(response.status_code, 200)
        with self.module.db() as conn:
            fingerprints = [row[0] for row in conn.execute(
                "SELECT automatic_fingerprint FROM kelivo_requests ORDER BY id"
            )]
        self.assertEqual((len(self.calls), len(set(fingerprints))), (3, 3))

    async def test_automatic_and_explicit_modes_are_fully_isolated(self):
        self.enable_auto_idempotency()
        automatic = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        explicit = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload(),
        )
        automatic_replay = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.auto_headers, json=self.payload(),
        )
        explicit_replay = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload(),
        )
        self.assertEqual(automatic_replay.json(), automatic.json())
        self.assertEqual(explicit_replay.json(), explicit.json())
        self.assertEqual(len(self.calls), 2)
        with self.module.db() as conn:
            modes = [row[0] for row in conn.execute(
                "SELECT idempotency_mode FROM kelivo_requests ORDER BY id"
            )]
        self.assertEqual(modes, ["automatic", "explicit"])

    async def test_auth_and_models_shape(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = await request(self.module, "GET", "/v1/models", headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("wrong", response.text)
        response = await request(self.module, "GET", "/v1/models", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"object": "list", "data": [{
            "id": "ouou-home", "object": "model", "created": 0, "owned_by": "ouou-home"
        }]})

    async def test_kelivo_key_cannot_access_relay_namespaces_or_query_auth(self):
        for path in ("/app/history", "/channel/in", "/uploads/not-a-file"):
            response = await request(self.module, "GET", path, headers={"Authorization": self.headers["Authorization"]})
            self.assertEqual(response.status_code, 401)
        response = await request(
            self.module, "POST", "/integrations/telegram/webhook",
            headers={"Authorization": self.headers["Authorization"]}, json={},
        )
        self.assertEqual(response.status_code, 401)
        response = await request(self.module, "GET", "/v1/models?token=test-kelivo-key-distinct-1234567890")
        self.assertEqual(response.status_code, 401)

    async def test_model_alias_and_stream_are_strict(self):
        wrong = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers,
                              json=self.payload(model="real-provider-model"))
        stream = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers,
                               json=self.payload(stream=True, stream_options={"include_usage": True}))
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(stream.status_code, 400)
        self.assertEqual(stream.json()["error"]["code"], "streaming_not_supported")
        self.assertEqual(self.calls, [])

    async def test_nonstream_stream_options_shapes_are_ignored(self):
        missing = object()
        shapes = (missing, None, {}, {"include_usage": True}, {"include_usage": False})
        responses = []
        for index, shape in enumerate(shapes):
            headers = dict(self.headers)
            headers["Idempotency-Key"] = f"stream-options-shape-{index:04d}"
            body = self.payload()
            if shape is not missing:
                body["stream_options"] = shape
            responses.append(await request(
                self.module, "POST", "/v1/chat/completions", headers=headers, json=body,
            ))
        self.assertEqual([response.status_code for response in responses], [200] * len(shapes))
        self.assertEqual([response.json()["usage"]["total_tokens"] for response in responses], [5] * len(shapes))
        self.assertEqual(len(self.calls), len(shapes))
        self.assertTrue(all("stream_options" not in json.dumps(call) for call in self.calls))
        with self.module.db() as conn:
            rows = conn.execute(
                "SELECT request_payload_hash,request_identity_hash,context_bundle_json,provider_messages_json "
                "FROM kelivo_requests ORDER BY id"
            ).fetchall()
            snapshots = conn.execute("SELECT count(*) FROM companion_context_snapshots").fetchone()[0]
        self.assertEqual(len({row["request_payload_hash"] for row in rows}), 1)
        self.assertEqual(len({row["request_identity_hash"] for row in rows}), 1)
        self.assertTrue(all("stream_options" not in row["context_bundle_json"] for row in rows))
        self.assertTrue(all("stream_options" not in row["provider_messages_json"] for row in rows))
        self.assertEqual(snapshots, 0)

    async def test_stream_options_variants_replay_same_key_without_second_generation(self):
        first = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers,
            json=self.payload(stream_options={"include_usage": True}),
        )
        second = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers,
            json=self.payload(stream_options={"include_usage": False}),
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(len(self.calls), 1)

    async def test_invalid_stream_options_are_rejected_without_generation(self):
        cases = (
            "true", 1, [], {"unknown": True}, {"nested": {}},
            {"include_usage": "true"}, {"include_usage": 1}, {"include_usage": None},
        )
        for index, stream_options in enumerate(cases):
            with self.subTest(index=index):
                headers = dict(self.headers)
                headers["Idempotency-Key"] = f"invalid-stream-options-{index:04d}"
                response = await request(
                    self.module, "POST", "/v1/chat/completions", headers=headers,
                    json=self.payload(stream_options=stream_options),
                )
                self.assertEqual((response.status_code, response.json()["error"]["code"]),
                                 (422, "invalid_stream_options"))
        self.assertEqual(self.calls, [])

    async def test_body_size_and_duplicate_json_keys(self):
        response = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers,
            content=b"{}",
        )
        self.assertNotEqual(response.status_code, 413)
        oversized = b"{" + b" " * self.module.kelivo_service.MAX_BODY_BYTES + b"}"
        response = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers, content=oversized)
        self.assertEqual(response.status_code, 413)
        duplicate = b'{"model":"ouou-home","model":"x","messages":[]}'
        response = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers, content=duplicate)
        self.assertEqual(response.status_code, 400)

    async def test_compression_depth_last_role_and_tools_have_stable_errors(self):
        compressed = await request(
            self.module, "POST", "/v1/chat/completions", headers={**self.headers, "Content-Encoding": "gzip"},
            content=b"not-really-gzip",
        )
        self.assertEqual(compressed.status_code, 415)
        assistant_last = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers,
            json=self.payload(messages=[{"role": "user", "content": "x"},
                                        {"role": "assistant", "content": "y"}]),
        )
        self.assertEqual(assistant_last.json()["error"]["code"], "last_message_must_be_user")
        tools = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers,
            json=self.payload(tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}]),
        )
        self.assertEqual((tools.status_code, tools.json()["error"]["code"]), (400, "tools_not_supported"))
        nested = {}; cursor = nested
        for _ in range(34):
            cursor["x"] = {}; cursor = cursor["x"]
        deep_headers = dict(self.headers); deep_headers["Idempotency-Key"] = "deep-json-key-0001"
        deep = await request(
            self.module, "POST", "/v1/chat/completions", headers=deep_headers,
            json={**self.payload(), "messages": [{"role": "user", "content": "x"}], "unknown": nested},
        )
        self.assertEqual(deep.json()["error"]["code"], "json_too_complex")

    async def test_messages_tools_and_ssrf_fields_are_rejected(self):
        cases = [
            self.payload(messages=[{"role": "owner", "content": "x"}]),
            self.payload(messages=[{"role": "user", "content": [{"type": "image_url"}]}]),
            self.payload(messages=[{"role": "user", "content": "x" * 32001}]),
            self.payload(messages=[{"role": "user", "content": "x"}] * 101),
            self.payload(messages=[
                {"role": "user", "content": "x" * 32000},
                {"role": "assistant", "content": "x" * 32000},
                {"role": "user", "content": "x" * 32000},
                {"role": "assistant", "content": "x"},
            ]),
            self.payload(tools={"type": "function"}),
            self.payload(tools=[
                {"type": "function", "function": {"name": f"tool_{number}", "parameters": {}}}
                for number in range(33)
            ]),
            self.payload(tools=[{"type": "function", "function": {"name": "bad name", "parameters": {}}}]),
            self.payload(tools=[{"type": "function", "function": {
                "name": "oversized", "description": "x" * 4097, "parameters": {}
            }}]),
            {**self.payload(), "api_base": "http://127.0.0.1/private"},
            {**self.payload(), "provider": "arbitrary"},
            {**self.payload(), "fallback_models": ["x"]},
        ]
        for index, body in enumerate(cases):
            with self.subTest(index=index):
                headers = dict(self.headers); headers["Idempotency-Key"] = f"reject-key-{index:04d}"
                response = await request(self.module, "POST", "/v1/chat/completions", headers=headers, json=body)
                self.assertIn(response.status_code, (400, 422))
        self.assertEqual(self.calls, [])

    async def test_idempotent_completion_replays_without_second_generation(self):
        first = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload())
        second = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(first.json()["model"], "ouou-home")
        self.assertEqual(first.json()["usage"]["total_tokens"], 5)

    async def test_same_key_different_request_conflicts(self):
        first = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload())
        second = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers,
                               json=self.payload(messages=[{"role": "user", "content": "different"}]))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error"]["code"], "idempotency_conflict")

    async def test_same_key_concurrency_is_stable_and_calls_provider_once(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def slow(*_args):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"text": "one reply", "usage": {}}
        self.module.KELIVO_GENERATOR = slow
        first = asyncio.create_task(request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload()
        ))
        await started.wait()
        same = await request(self.module, "POST", "/v1/chat/completions",
                             headers=self.headers, json=self.payload())
        conflict = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers,
            json=self.payload(messages=[{"role": "user", "content": "different"}]),
        )
        self.assertEqual((same.status_code, same.json()["error"]["code"]),
                         (409, "idempotency_in_progress"))
        self.assertEqual((conflict.status_code, conflict.json()["error"]["code"]),
                         (409, "idempotency_conflict"))
        release.set()
        self.assertEqual((await first).status_code, 200)
        self.assertEqual(calls, 1)
        self.assertEqual(self.module.KELIVO_KEY_LOCKS.entry_count, 0)

    async def test_first_same_key_requests_really_contend_on_key_lock(self):
        provider_started, provider_release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def slow(*_args):
            nonlocal calls
            calls += 1
            provider_started.set()
            await provider_release.wait()
            return {"text": "one", "usage": {}}
        self.module.KELIVO_GENERATOR = slow
        async with self.module.KELIVO_KEY_LOCKS.hold("primary-kelivo", "request-key-0001"):
            tasks = [asyncio.create_task(request(
                self.module, "POST", "/v1/chat/completions", headers=self.headers,
                json=self.payload(),
            )) for _ in range(2)]
            await asyncio.sleep(0.02)
            self.assertEqual(calls, 0)
            self.assertEqual(self.module.KELIVO_KEY_LOCKS.entry_count, 1)
        await provider_started.wait()
        await asyncio.sleep(0.02)
        provider_release.set()
        responses = await asyncio.gather(*tasks)
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        conflict = next(response for response in responses if response.status_code == 409)
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_in_progress")
        self.assertEqual(calls, 1)
        self.assertEqual(self.module.KELIVO_KEY_LOCKS.entry_count, 0)

    async def test_same_key_slow_provider_beats_queue_timeout_semantics(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(*_args):
            started.set()
            await release.wait()
            return {"text": "one", "usage": {}}
        self.module.KELIVO_GENERATOR = slow
        self.module.KELIVO_ADMISSION = self.module.kelivo_service.KelivoAdmissionController(1, 1, 0.01)
        first = asyncio.create_task(request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload(),
        ))
        await started.wait()
        await asyncio.sleep(0.03)
        second = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload(),
        )
        self.assertEqual((second.status_code, second.json()["error"]["code"]),
                         (409, "idempotency_in_progress"))
        release.set()
        self.assertEqual((await first).status_code, 200)

    async def test_key_lock_waiter_cancellation_restores_registry(self):
        registry = self.module.kelivo_service.IdempotencyLockRegistry()
        entered = asyncio.Event()
        async def waiter():
            async with registry.hold("client", "cancel-key"):
                entered.set()
        async with registry.hold("client", "cancel-key"):
            task = asyncio.create_task(waiter())
            await asyncio.sleep(0)
            self.assertEqual(registry.entry_count, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(registry.entry_count, 1)
            self.assertFalse(entered.is_set())
        self.assertEqual(registry.entry_count, 0)

    async def test_cancel_after_slots_before_claim_releases_slots_and_fails_prepared(self):
        original = self.module.kelivo_service.begin_dispatch
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def blocked_begin(*args, **kwargs):
            entered.set()
            release.wait(2)
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()
        with mock.patch.object(self.module.kelivo_service, "begin_dispatch", new=blocked_begin):
            task = asyncio.create_task(request(
                self.module, "POST", "/v1/chat/completions", headers=self.headers,
                json=self.payload(),
            ))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        release.set()
        self.assertTrue(await asyncio.to_thread(finished.wait, 1))
        with self.module.db() as conn:
            row = conn.execute("SELECT status,error_category FROM kelivo_requests").fetchone()
        self.assertEqual((row["status"], row["error_category"]),
                         ("failed", "request_cancelled_before_dispatch"))
        self.assertEqual(self.module.KELIVO_ADMISSION._global._value,
                         self.module.DEPLOYMENT.kelivo.global_concurrency)

    async def test_completed_replay_keeps_old_persona_but_inflight_change_conflicts(self):
        first = await request(self.module, "POST", "/v1/chat/completions",
                              headers=self.headers, json=self.payload())
        self.module.KELIVO_PERSONA = "changed after completion"
        os.environ.update({
            "LLM_MODEL": "changed-provider-after-completion",
            "LLM_TEMPERATURE": "1.2", "LLM_MAX_TOKENS": "333",
        })
        replay = await request(self.module, "POST", "/v1/chat/completions",
                               headers=self.headers, json=self.payload())
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(len(self.calls), 1)
        os.environ.update({
            "LLM_MODEL": "test-provider-model", "LLM_TEMPERATURE": "0.7",
            "LLM_MAX_TOKENS": "2000",
        })
        headers = dict(self.headers); headers["Idempotency-Key"] = "persona-change-key-0002"
        validated = self.module.kelivo_service.validate_completion(self.payload(), "ouou-home")
        self.module.kelivo_service.prepare_request(
            self.module.DB_PATH, "primary-kelivo", "persona-change-key-0002", validated,
            persona_text=self.module.KELIVO_PERSONA, persona_source="test",
            provider_model="test-provider-model", effective_temperature=0.7,
            effective_max_tokens=2000,
        )
        self.module.KELIVO_PERSONA = "another persona before dispatch"
        conflict = await request(self.module, "POST", "/v1/chat/completions",
                                 headers=headers, json=self.payload())
        self.assertEqual((conflict.status_code, conflict.json()["error"]["code"]),
                         (409, "idempotency_conflict"))

    async def test_unfinished_same_payload_conflicts_when_provider_contract_changes(self):
        validated = self.module.kelivo_service.validate_completion(self.payload(), "ouou-home")
        self.module.kelivo_service.prepare_request(
            self.module.DB_PATH, "primary-kelivo", "provider-change-key-0001", validated,
            persona_text=self.module.KELIVO_PERSONA, persona_source="test",
            provider_model="test-provider-model", effective_temperature=0.7,
            effective_max_tokens=2000,
        )
        os.environ.update({"LLM_MODEL": "provider-new", "LLM_TEMPERATURE": "1.1", "LLM_MAX_TOKENS": "999"})
        headers = dict(self.headers); headers["Idempotency-Key"] = "provider-change-key-0001"
        response = await request(
            self.module, "POST", "/v1/chat/completions", headers=headers, json=self.payload(),
        )
        self.assertEqual((response.status_code, response.json()["error"]["code"]),
                         (409, "idempotency_conflict"))
        self.assertEqual(self.calls, [])

    async def test_full_relay_loop_provider_prompt_contract(self):
        os.environ.update({
            "RELAY_DB": self.module.DB_PATH,
            "LOOP_CONFIG": str(Path(self.temp.name) / "loop-config.json"),
            "LLM_API_BASE": "https://provider.invalid/v1", "LLM_API_KEY": "test-provider-key",
            "LLM_MODEL": "provider-model", "LOOP_STREAM": "0",
            "LLM_TEMPERATURE": "0.61", "LLM_MAX_TOKENS": "456",
            "API_LOOP_INTERNAL_TOKEN": "test-internal-loop-token-1234567890",
        })
        for suffix in ("_2", "_3", "_4"):
            for field in ("API_BASE", "API_KEY", "MODEL"):
                os.environ.pop(f"LLM_{field}{suffix}", None)
        sys.modules.pop("examples.api_loop", None)
        loop = importlib.import_module("examples.api_loop")
        captured = []
        real_client = httpx.AsyncClient
        def provider_handler(req):
            captured.append(json.loads(req.content))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "end to end"}}],
                "usage": {"total_tokens": 7},
            })
        loop._provider_client = lambda **kwargs: real_client(
            transport=httpx.MockTransport(provider_handler), timeout=kwargs.get("timeout")
        )
        loop.PERSONA = "must never be injected on Kelivo path"
        adapter = self.module.kelivo_service.LoopGenerationClient(
            "http://127.0.0.1:9/loop/ingest", 2,
            "test-internal-loop-token-1234567890",
            transport=httpx.ASGITransport(app=loop.app),
        )
        first_dispatch = True
        async def mutate_defaults_after_prepare(*args):
            nonlocal first_dispatch
            if first_dispatch:
                first_dispatch = False
                os.environ.update({"LLM_TEMPERATURE": "1.7", "LLM_MAX_TOKENS": "987"})
            return await adapter.generate(*args)
        self.module.KELIVO_GENERATOR = mutate_defaults_after_prepare
        body = self.payload(messages=[
            {"role": "system", "content": " client system "},
            {"role": "developer", "content": "developer rule"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": " exact user "},
        ], stream_options={"include_usage": True})
        response = await request(self.module, "POST", "/v1/chat/completions",
                                 headers=self.headers, json=body)
        self.assertEqual(response.status_code, 200)
        explicit_headers = dict(self.headers)
        explicit_headers["Idempotency-Key"] = "provider-explicit-key-0002"
        explicit = await request(
            self.module, "POST", "/v1/chat/completions", headers=explicit_headers,
            json=self.payload(temperature=0.25, max_tokens=321,
                              stream_options={"include_usage": False}),
        )
        self.assertEqual(explicit.status_code, 200)
        self.assertEqual(len(captured), 2)
        expected = [{"role": "system", "content": self.module.KELIVO_PERSONA}, *body["messages"]]
        self.assertEqual(captured[0]["messages"], expected)
        self.assertEqual(captured[0]["model"], "provider-model")
        self.assertEqual((captured[0]["temperature"], captured[0]["max_tokens"]), (0.61, 456))
        self.assertEqual((captured[1]["temperature"], captured[1]["max_tokens"]), (0.25, 321))
        self.assertTrue(all("stream_options" not in provider_payload for provider_payload in captured))
        with self.module.db() as conn:
            rows = conn.execute(
                """SELECT context_bundle_json,context_bundle_hash,provider_model,
                          effective_temperature,effective_max_tokens,request_identity_hash
                   FROM kelivo_requests ORDER BY id"""
            ).fetchall()
        row = rows[0]
        bundle = json.loads(row["context_bundle_json"])
        self.assertEqual(bundle["provider_messages"], captured[0]["messages"])
        self.assertEqual(row["context_bundle_hash"], self.module.kelivo_service.content_hash(bundle)[1])
        self.assertEqual((row["provider_model"], row["effective_temperature"], row["effective_max_tokens"]),
                         ("provider-model", 0.61, 456))
        self.assertNotEqual(rows[0]["request_identity_hash"], rows[1]["request_identity_hash"])
        self.assertNotIn("must never be injected", json.dumps(captured[0]))

    async def test_dispatching_and_uncertain_are_never_redispatched(self):
        validated = self.module.kelivo_service.validate_completion(self.payload(), "ouou-home")
        self.module.kelivo_service.prepare_request(
            self.module.DB_PATH, "primary-kelivo", "blocked-key-0001", validated
        )
        headers = dict(self.headers); headers["Idempotency-Key"] = "blocked-key-0001"
        response = await request(self.module, "POST", "/v1/chat/completions", headers=headers, json=self.payload())
        self.assertEqual(response.status_code, 409)
        self.module.kelivo_service.fail_request(
            self.module.DB_PATH, "primary-kelivo", "blocked-key-0001", "transport_uncertain", True
        )
        response = await request(self.module, "POST", "/v1/chat/completions", headers=headers, json=self.payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.calls, [])

    async def test_only_current_turn_is_canonical_and_context_is_snapshotted(self):
        body = self.payload(messages=[
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current question"},
        ])
        response = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers, json=body)
        self.assertEqual(response.status_code, 200)
        with self.module.db() as conn:
            messages = conn.execute("SELECT direction,text FROM messages ORDER BY id").fetchall()
            snapshots = conn.execute(
                "SELECT snapshot_type,api_session FROM companion_context_snapshots"
            ).fetchall()
        self.assertEqual([(row["direction"], row["text"]) for row in messages],
                         [("in", "current question"), ("out", "model reply")])
        self.assertEqual([(row["snapshot_type"], row["api_session"]) for row in snapshots],
                         [("system", "shared-test-session")])
        self.assertEqual(list(self.calls[0][0])[1:], body["messages"])
        self.assertEqual(self.calls[0][5]["snapshots"]["system"]["value"],
                         [{"role": "system", "content": "persona"}])

    async def test_mapping_is_server_fixed(self):
        response = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[0][1], "shared-test-session")
        body = {**self.payload(), "api_session": "attacker-session"}
        headers = dict(self.headers); headers["Idempotency-Key"] = "request-key-0002"
        response = await request(self.module, "POST", "/v1/chat/completions", headers=headers, json=body)
        self.assertEqual(response.status_code, 422)

    async def test_generation_failure_is_classified_and_not_retried(self):
        async def fail(*args):
            self.calls.append(args)
            raise self.module.kelivo_service.GenerationError("model_timeout", True)
        self.module.KELIVO_GENERATOR = fail
        first = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload())
        second = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload())
        self.assertEqual(first.status_code, 504)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(len(self.calls), 1)

    async def test_sqlite_rate_limit_returns_retry_after(self):
        self.module.DEPLOYMENT = dataclasses.replace(
            self.module.DEPLOYMENT,
            kelivo=dataclasses.replace(self.module.DEPLOYMENT.kelivo, rate_limit_per_minute=1),
        )
        first = await request(self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload())
        headers = dict(self.headers); headers["Idempotency-Key"] = "rate-limit-key-0002"
        second = await request(self.module, "POST", "/v1/chat/completions", headers=headers, json=self.payload())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertGreaterEqual(int(second.headers["Retry-After"]), 1)

    async def test_client_queue_does_not_consume_another_clients_global_slot(self):
        controller = self.module.kelivo_service.KelivoAdmissionController(2, 1, 0.05)
        first = await controller.acquire("client-a")
        queued = asyncio.create_task(controller.acquire("client-a"))
        await asyncio.sleep(0)
        other = await controller.acquire("client-b")
        other.release()
        with self.assertRaises(self.module.kelivo_service.KelivoError) as raised:
            await queued
        self.assertEqual(raised.exception.status_code, 429)
        first.release()

    async def test_admission_cancellation_releases_partial_slots(self):
        controller = self.module.kelivo_service.KelivoAdmissionController(1, 1, 1)
        held = await controller.acquire("client-a")
        waiting_global = asyncio.create_task(controller.acquire("client-b"))
        await asyncio.sleep(0.02)
        waiting_global.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting_global
        held.release()
        lease = await controller.acquire("client-b")
        lease.release()

        held = await controller.acquire("client-c")
        waiting_client = asyncio.create_task(controller.acquire("client-c"))
        await asyncio.sleep(0)
        waiting_client.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting_client
        held.release()
        lease = await controller.acquire("client-c")
        lease.release()

    async def test_cancellation_while_queued_is_failed_before_dispatch_and_slot_recovers(self):
        controller = self.module.kelivo_service.KelivoAdmissionController(1, 1, 1)
        held = await controller.acquire("primary-kelivo")
        self.module.KELIVO_ADMISSION = controller
        task = asyncio.create_task(request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload()
        ))
        for _ in range(100):
            with self.module.db() as conn:
                row = conn.execute("SELECT status FROM kelivo_requests").fetchone()
            if row:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        held.release()
        with self.module.db() as conn:
            row = conn.execute("SELECT status,error_category FROM kelivo_requests").fetchone()
        self.assertEqual((row["status"], row["error_category"]),
                         ("failed", "request_cancelled_before_dispatch"))
        lease = await controller.acquire("primary-kelivo")
        lease.release()
        self.assertEqual(self.calls, [])

    async def test_cancellation_after_dispatch_becomes_uncertain_and_never_redispatches(self):
        started = asyncio.Event()
        calls = 0
        async def pending(*_args):
            nonlocal calls
            calls += 1
            started.set()
            await asyncio.Future()
        self.module.KELIVO_GENERATOR = pending
        task = asyncio.create_task(request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload()
        ))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.module.db() as conn:
            row = conn.execute("SELECT status,error_category FROM kelivo_requests").fetchone()
        self.assertEqual((row["status"], row["error_category"]),
                         ("dispatch_uncertain", "client_cancelled_after_dispatch"))
        replay = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers, json=self.payload()
        )
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
