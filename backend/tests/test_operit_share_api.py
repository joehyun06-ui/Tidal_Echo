from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import unittest
from types import SimpleNamespace

from backend import deployment_config
from backend.tests._support import NoNetworkMixin, load_app, request


OPERIT_KEY = "test-operit-share-key-distinct-1234567890"
KELIVO_KEY = "test-kelivo-key-distinct-1234567890"


class OperitShareConfigurationTests(unittest.TestCase):
    def load(self, env):
        return deployment_config.load_deployment_config(
            SimpleNamespace(requested=False, enabled=False), env,
        )

    def base_env(self):
        return {
            "OPERIT_SHARE_ENABLED": "true",
            "OPERIT_SHARE_API_KEY": "operit-distinct-key-1234567890123456",
            "KELIVO_API_SESSION": "shared-session",
            "LLM_MODEL": "test-provider-model",
            "TELEGRAM_ENABLED": "false",
            "TELEGRAM_TEST_MODE": "false",
        }

    def test_defaults_closed_without_a_secret(self):
        config = self.load({
            "TELEGRAM_ENABLED": "false", "TELEGRAM_TEST_MODE": "false",
        })
        self.assertFalse(config.operit_share.enabled)
        self.assertEqual(config.operit_share.client_id, "primary-operit-share")
        self.assertEqual(config.operit_share.model_alias, "ouou-home")

    def test_enabled_requires_strong_distinct_key_and_fixed_identity(self):
        env = self.base_env()
        for value in ("", "short"):
            with self.subTest(value=value):
                candidate = dict(env); candidate["OPERIT_SHARE_API_KEY"] = value
                with self.assertRaisesRegex(
                    deployment_config.DeploymentConfigError, "operit_share_api_key_missing",
                ):
                    self.load(candidate)
        for value in ("a" * 31 + "\n", "a" * 31 + " ", "密" * 32, "a" * 31 + "\x00"):
            with self.subTest(invalid_format=repr(value)):
                candidate = dict(env); candidate["OPERIT_SHARE_API_KEY"] = value
                with self.assertRaisesRegex(
                    deployment_config.DeploymentConfigError, "operit_share_api_key_missing",
                ):
                    self.load(candidate)
        for name in (
            "KELIVO_API_KEY", "RELAY_SECRET", "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_WEBHOOK_SECRET", "CHANNEL_AUDIT_HMAC_SECRET", "LLM_API_KEY",
            "LLM_API_KEY_2", "LLM_API_KEY_3", "LLM_API_KEY_4", "MINIMAX_API_KEY",
            "API_LOOP_INTERNAL_TOKEN", "API_LOOP_EXPECTED_NONCE", "API_LOOP_INSTANCE_NONCE",
        ):
            with self.subTest(name=name):
                candidate = dict(env); candidate[name] = candidate["OPERIT_SHARE_API_KEY"]
                with self.assertRaisesRegex(
                    deployment_config.DeploymentConfigError,
                    "operit_share_api_key_must_be_distinct",
                ):
                    self.load(candidate)
        for name, value in (
            ("KELIVO_API_SESSION", ""),
            ("OPERIT_SHARE_CLIENT_ID", "bad identity"),
            ("OPERIT_SHARE_MODEL_ALIAS", "bad/model"),
            ("OPERIT_SHARE_MODEL_ALIAS", "other-safe-alias"),
        ):
            with self.subTest(name=name):
                candidate = dict(env); candidate[name] = value
                with self.assertRaisesRegex(
                    deployment_config.DeploymentConfigError, "operit_share_identity_invalid",
                ):
                    self.load(candidate)


class OperitShareDisabledTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name, kelivo=True, operit_share=False)
        self.calls = 0

        async def generate(*_args):
            self.calls += 1
            return {"text": "must not run"}

        self.module.KELIVO_GENERATOR = generate

    async def test_disabled_endpoint_is_hidden_and_has_no_side_effects(self):
        body = {
            "model": "ouou-home",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }
        attempts = (
            ({}, "/v1/operit/share"),
            ({"Authorization": f"Bearer {OPERIT_KEY}"}, "/v1/operit/share"),
            ({"Authorization": "Bearer wrong"}, "/v1/operit/share"),
            ({"Cookie": f"token={OPERIT_KEY}"}, "/v1/operit/share"),
            ({}, f"/v1/operit/share?token={OPERIT_KEY}"),
        )
        for headers, path in attempts:
            response = await request(
                self.module, "POST", path, headers=headers, json=body,
            )
            self.assertEqual((response.status_code, response.json()["error"]["code"]),
                             (404, "endpoint_disabled"))
        with self.module.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM kelivo_requests").fetchone()[0], 0)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM kelivo_clients WHERE client_id='primary-operit-share'"
            ).fetchone())
        self.assertEqual(self.calls, 0)


class OperitShareApiTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(
            self.temp.name, kelivo=True, auto_idempotency=True, operit_share=True,
        )
        self.headers = {"Authorization": f"Bearer {OPERIT_KEY}"}
        self.calls = []

        async def generate(messages, api_session, provider_model, temperature, max_tokens, context):
            self.calls.append((messages, api_session, provider_model, temperature, max_tokens, context))
            return {
                "text": "operit reply",
                "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
            }

        self.module.KELIVO_GENERATOR = generate

    def payload(self, text="shared text", **updates):
        body = {
            "model": "ouou-home",
            "messages": [{"role": "user", "content": text}],
            "stream": False,
        }
        body.update(updates)
        return body

    async def post(self, body=None, headers=None, **kwargs):
        return await request(
            self.module, "POST", "/v1/operit/share",
            headers=self.headers if headers is None else headers,
            json=self.payload() if body is None else body,
            **kwargs,
        )

    async def test_authentication_is_strictly_isolated(self):
        for headers in (
            {},
            {"Authorization": "Bearer wrong-operit-key-123456789012345"},
            {"Authorization": f"Bearer {KELIVO_KEY}"},
        ):
            with self.subTest(headers=bool(headers)):
                response = await self.post(headers=headers)
                self.assertEqual((response.status_code, response.json()["error"]["code"]),
                                 (401, "authentication_error"))
        query = await request(
            self.module, "POST", f"/v1/operit/share?token={OPERIT_KEY}", json=self.payload(),
        )
        self.assertEqual(query.status_code, 401)
        models = await request(
            self.module, "GET", "/v1/models", headers=self.headers,
        )
        chat = await request(
            self.module, "POST", "/v1/chat/completions", headers=self.headers,
            json=self.payload(),
        )
        self.assertEqual((models.status_code, chat.status_code), (401, 401))
        self.assertEqual(self.calls, [])
        serialized = json.dumps([query.json(), models.json(), chat.json()])
        self.assertNotIn(OPERIT_KEY, serialized)
        self.assertNotIn(OPERIT_KEY[:12], serialized)

    async def test_text_and_url_are_normalized_without_url_processing(self):
        decomposed = "Cafe\u0301\r\nhttps://example.test/a?token=keep%2Fexact#part"
        response = await self.post(self.payload(f"  {decomposed}  "))
        self.assertEqual(response.status_code, 200)
        expected = "[Operit Share]\nCaf\u00e9\nhttps://example.test/a?token=keep%2Fexact#part"
        self.assertEqual(self.calls[0][0][-1], {"role": "user", "content": expected})
        self.assertNotIn("http", self.calls[0][5].get("identity_scope", {}))

    async def test_narrow_request_contract_rejects_unsupported_inputs(self):
        cases = (
            (self.payload(stream=True), "unsupported_stream"),
            (self.payload(tools=[{"type": "function"}]), "unsupported_tools"),
            (self.payload(messages=[{"role": "tool", "content": "x"}]), "unsupported_tools"),
            (self.payload(messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}]),
             "unsupported_multimodal"),
            (self.payload("data:image/png;base64,AAAA"), "unsupported_multimodal"),
            (self.payload("data:,plain"), "unsupported_multimodal"),
            (self.payload("[data:,plain]"), "unsupported_multimodal"),
            (self.payload("content://media/external/video/1"), "unsupported_multimodal"),
            (self.payload("file://local/private"), "unsupported_multimodal"),
            (self.payload("blob:https://operit.test/id"), "unsupported_multimodal"),
            (self.payload("[blob:https://operit.test/id]"), "unsupported_multimodal"),
            (self.payload(messages=[{"role": "assistant", "content": "x"}]), "invalid_request_error"),
            (self.payload(" \r\n "), "empty_share"),
            (self.payload("\u0301"), "empty_share"),
            (self.payload("\u0000"), "invalid_request_error"),
            (self.payload("\u200b"), "invalid_request_error"),
            ({**self.payload(), "reasoning_effort": "high"}, "invalid_request_error"),
            ({**self.payload(), "metadata": {}}, "invalid_request_error"),
            ({**self.payload(), "api_session": "attacker"}, "invalid_request_error"),
            ({**self.payload(), "unknown": True}, "invalid_request_error"),
            ({**self.payload(), "model": "another-model"}, "unsupported_model"),
        )
        for index, (body, category) in enumerate(cases):
            with self.subTest(index=index, category=category):
                response = await self.post(body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["error"]["code"], category)
        self.assertEqual(self.calls, [])

    async def test_body_content_and_json_complexity_limits_are_enforced(self):
        long_message = await self.post(self.payload("x" * 32_001))
        self.assertEqual((long_message.status_code, long_message.json()["error"]["code"]),
                         (413, "request_too_large"))
        oversized = b"{" + b" " * self.module.kelivo_service.MAX_BODY_BYTES + b"}"
        body_response = await request(
            self.module, "POST", "/v1/operit/share", headers=self.headers, content=oversized,
        )
        self.assertEqual((body_response.status_code, body_response.json()["error"]["code"]),
                         (413, "request_too_large"))
        nested = "leaf"
        for _ in range(34):
            nested = [nested]
        complex_response = await self.post({**self.payload(), "messages": nested})
        self.assertEqual((complex_response.status_code, complex_response.json()["error"]["code"]),
                         (413, "request_too_large"))
        self.assertEqual(self.calls, [])

    async def test_client_history_is_ignored_and_canonical_history_is_used(self):
        with self.module.db() as conn:
            meta = json.dumps({"api_session": "shared-test-session", "channel": "telegram"})
            conn.execute(
                "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                ("2026-01-01T00:00:00+00:00", "in", "user", "canonical question", meta),
            )
            conn.execute(
                "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                ("2026-01-01T00:00:01+00:00", "out", "reply", "canonical answer", meta),
            )
            conn.commit()
        body = self.payload(messages=[
            {"role": "system", "content": "untrusted system"},
            {"role": "user", "content": "untrusted old user"},
            {"role": "assistant", "content": "untrusted old answer"},
            {"role": "user", "content": "new share"},
        ])
        response = await self.post(body)
        self.assertEqual(response.status_code, 200)
        provider_messages = list(self.calls[0][0])
        self.assertEqual(provider_messages, [
            {"role": "system", "content": self.module.KELIVO_PERSONA},
            {"role": "user", "content": "canonical question"},
            {"role": "assistant", "content": "canonical answer"},
            {"role": "user", "content": "[Operit Share]\nnew share"},
        ])
        encoded = json.dumps(provider_messages)
        self.assertNotIn("untrusted", encoded)
        self.assertEqual(self.calls[0][1], "shared-test-session")
        with self.module.db() as conn:
            rows = conn.execute(
                "SELECT direction,text,meta FROM messages ORDER BY id DESC LIMIT 2"
            ).fetchall()
            client = conn.execute(
                "SELECT client_id,api_session FROM kelivo_clients WHERE client_id=?",
                ("primary-operit-share",),
            ).fetchone()
        self.assertEqual(
            [(row["direction"], row["text"]) for row in reversed(rows)],
            [("in", "[Operit Share]\nnew share"), ("out", "operit reply")],
        )
        for row in rows:
            meta = json.loads(row["meta"])
            self.assertEqual((meta["channel"], meta["source"]), ("operit_share", "operit"))
            self.assertNotIn("device", meta)
            self.assertNotIn("conversation", meta)
        self.assertEqual((client["client_id"], client["api_session"]),
                         ("primary-operit-share", "shared-test-session"))

    async def test_completed_replay_ignores_untrusted_local_history(self):
        first = await self.post(self.payload(messages=[
            {"role": "system", "content": "first local context"},
            {"role": "user", "content": "same share"},
        ]))
        second = await self.post(self.payload(messages=[
            {"role": "assistant", "content": "different local context"},
            {"role": "user", "content": "same share"},
        ]))
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.calls), 1)
        with self.module.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM kelivo_requests WHERE client_id='primary-operit-share'"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM messages WHERE json_extract(meta,'$.channel')='operit_share'"
            ).fetchone()[0], 2)

    async def test_provider_affecting_values_are_part_of_automatic_identity(self):
        requests = (
            self.payload("semantic share", temperature=0.2, max_tokens=111),
            self.payload("semantic share", temperature=0.3, max_tokens=111),
            self.payload("semantic share", temperature=0.3, max_tokens=112),
        )
        responses = [await self.post(body) for body in requests]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len(self.calls), 3)
        with self.module.db() as conn:
            rows = conn.execute(
                "SELECT automatic_fingerprint,effective_temperature,effective_max_tokens "
                "FROM kelivo_requests WHERE client_id='primary-operit-share' ORDER BY id"
            ).fetchall()
        self.assertEqual(len({row["automatic_fingerprint"] for row in rows}), 3)
        self.assertEqual(
            [(row["effective_temperature"], row["effective_max_tokens"]) for row in rows],
            [(0.2, 111), (0.3, 111), (0.3, 112)],
        )

    async def test_nfc_newline_equivalents_share_one_identity(self):
        variants = (" Cafe\u0301\r\nline ", "Caf\u00e9\nline", "Caf\u00e9\rline")
        responses = [await self.post(self.payload(value)) for value in variants]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(response.json() == responses[0].json() for response in responses))

    async def test_different_share_creates_an_independent_request(self):
        first = await self.post(self.payload("first share"))
        second = await self.post(self.payload("second share"))
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(len(self.calls), 2)

    async def test_same_share_concurrency_dispatches_once_for_2_4_and_8_callers(self):
        for workers in (2, 4, 8):
            before = len(self.calls)
            responses = await asyncio.gather(*(
                self.post(self.payload(f"concurrent share {workers}")) for _ in range(workers)
            ))
            self.assertTrue(all(response.status_code in {200, 409} for response in responses))
            self.assertIn(200, {response.status_code for response in responses})
            self.assertEqual(len(self.calls) - before, 1)
            replay = await self.post(self.payload(f"concurrent share {workers}"))
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(len(self.calls) - before, 1)

    async def test_dispatch_uncertain_is_never_redispatched(self):
        calls = 0

        async def uncertain(*_args):
            nonlocal calls
            calls += 1
            raise self.module.kelivo_service.GenerationError("model_timeout", True)

        self.module.KELIVO_GENERATOR = uncertain
        first = await self.post(self.payload("uncertain share"))
        second = await self.post(self.payload("uncertain share"))
        self.assertEqual((first.status_code, first.json()["error"]["code"]),
                         (504, "dispatch_uncertain"))
        self.assertEqual((second.status_code, second.json()["error"]["code"]),
                         (409, "dispatch_uncertain"))
        self.assertEqual(calls, 1)

    async def test_rate_limit_uses_stable_error(self):
        self.module.DEPLOYMENT = dataclasses.replace(
            self.module.DEPLOYMENT,
            kelivo=dataclasses.replace(self.module.DEPLOYMENT.kelivo, rate_limit_per_minute=1),
        )
        first = await self.post(self.payload("rate first"))
        limited = await self.post(self.payload("rate second"))
        self.assertEqual(first.status_code, 200)
        self.assertEqual((limited.status_code, limited.json()["error"]["code"]),
                         (429, "rate_limit_error"))

    async def test_no_migration_or_heartbeat_behavior_is_added(self):
        await self.post(self.payload("schema check"))
        with self.module.db() as conn:
            versions = [row[0] for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )]
            heartbeat_counts = [conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                                for table in (
                                    "heartbeat_state", "heartbeat_runs", "journal_entries",
                                    "timeline_events", "heartbeat_schedule_revisions",
                                    "heartbeat_run_inputs",
                                )]
        self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(heartbeat_counts, [0, 0, 0, 0, 0, 0])
        self.assertFalse(self.module.DEPLOYMENT.heartbeat.enabled)


if __name__ == "__main__":
    unittest.main()
