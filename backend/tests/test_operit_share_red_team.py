from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import logging
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from fastapi import HTTPException, Request

from backend import deployment_config
from backend.tests._support import NoNetworkMixin, load_app, request


OPERIT_KEY = "test-operit-share-key-distinct-1234567890"
OPERIT_HEADERS = {"Authorization": f"Bearer {OPERIT_KEY}"}


def payload(text: str = "red-team share", **updates):
    body = {
        "model": "ouou-home",
        "messages": [{"role": "user", "content": text}],
        "stream": False,
    }
    body.update(updates)
    return body


class OperitAuthRedTeamTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name, kelivo=True, operit_share=True)
        self.calls = 0

        async def generate(*_args):
            self.calls += 1
            return {"text": "ok", "usage": {}}

        self.module.KELIVO_GENERATOR = generate

    def raw_request(self, authorization_values):
        return Request({
            "type": "http",
            "method": "POST",
            "path": "/v1/operit/share",
            "query_string": b"",
            "headers": [(b"authorization", value) for value in authorization_values],
        })

    async def test_all_cross_namespace_and_malformed_credentials_are_rejected(self):
        foreign_tokens = (
            "test-kelivo-key-distinct-1234567890",
            "test-relay-secret",
            "FAKE_TEST_TOKEN_WITHOUT_BOT_FORMAT",
            "test-webhook-secret",
            "invalid-test-only-audit-secret",
            "test-internal-loop-token-1234567890",
        )
        malformed = (
            {},
            {"Authorization": ""},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer "},
            {"Authorization": f"Bearer  {OPERIT_KEY}"},
            {"Authorization": f"Bearer {OPERIT_KEY} "},
            {"Authorization": f"Basic {OPERIT_KEY}"},
            {"Cookie": f"Authorization=Bearer%20{OPERIT_KEY}"},
            *({"Authorization": f"Bearer {token}"} for token in foreign_tokens),
        )
        for headers in malformed:
            response = await request(
                self.module, "POST", "/v1/operit/share", headers=headers, json=payload(),
            )
            self.assertEqual((response.status_code, response.json()["error"]["code"]),
                             (401, "authentication_error"))
        query = await request(
            self.module, "POST", f"/v1/operit/share?token={OPERIT_KEY}&share=secret",
            json=payload(),
        )
        self.assertEqual(query.status_code, 401)
        relay = await request(
            self.module, "GET", "/app/history", headers=OPERIT_HEADERS,
        )
        models = await request(self.module, "GET", "/v1/models", headers=OPERIT_HEADERS)
        chat = await request(
            self.module, "POST", "/v1/chat/completions", headers=OPERIT_HEADERS,
            json=payload(),
        )
        self.assertEqual((relay.status_code, models.status_code, chat.status_code), (401, 401, 401))
        self.assertEqual(self.calls, 0)

    async def test_duplicate_control_and_overlong_authorization_headers_fail_closed(self):
        invalid_raw = (
            [f"Bearer {OPERIT_KEY}".encode(), f"Bearer {OPERIT_KEY}".encode()],
            [f"Bearer {OPERIT_KEY}\nignored".encode()],
            [f"Bearer {OPERIT_KEY}\x00ignored".encode()],
            [("Bearer " + "a" * 513).encode()],
        )
        for values in invalid_raw:
            with self.assertRaises(HTTPException) as raised:
                self.module.check_operit_share_auth(self.raw_request(values))
            self.assertEqual(raised.exception.status_code, 401)
        self.module.check_operit_share_auth(
            self.raw_request([f"Bearer {OPERIT_KEY}".encode()])
        )

    async def test_secret_comparison_uses_fixed_length_digests(self):
        compared = []
        real_compare = self.module.hmac.compare_digest

        def record(left, right):
            compared.append((left, right))
            return real_compare(left, right)

        with mock.patch.object(self.module.hmac, "compare_digest", new=record):
            for token in ("x", OPERIT_KEY[:8], OPERIT_KEY[:-1] + "x"):
                with self.assertRaises(HTTPException):
                    self.module.check_operit_share_auth(
                        self.raw_request([f"Bearer {token}".encode()])
                    )
        self.assertEqual(len(compared), 3)
        self.assertTrue(all(
            isinstance(left, bytes) and isinstance(right, bytes)
            and len(left) == len(right) == 32
            for left, right in compared
        ))

    async def test_query_is_removed_from_the_shared_asgi_scope_before_access_logging(self):
        observed = {}

        async def downstream(scope, _receive, _send):
            observed["query_string"] = scope["query_string"]

        middleware = self.module.OperitQueryRedactionMiddleware(downstream)
        scope = {
            "type": "http", "path": "/v1/operit/share",
            "query_string": b"token=secret&url=https%3A%2F%2Fprivate.invalid",
        }
        await middleware(scope, None, None)
        self.assertEqual(observed["query_string"], b"")
        self.assertEqual(scope["query_string"], b"")


class OperitTransportRedTeamTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name, kelivo=True, operit_share=True)
        self.calls = []

        async def generate(*args):
            self.calls.append(args)
            return {"text": "transport ok", "usage": {}}

        self.module.KELIVO_GENERATOR = generate

    async def test_duplicate_nonfinite_malformed_encoding_and_length_attacks(self):
        raw_cases = (
            (b'{"model":"ouou-home","model":"evil"}', OPERIT_HEADERS),
            (b'{"model":"ouou-home","messages":[],"temperature":NaN}', OPERIT_HEADERS),
            (b'{"model":', OPERIT_HEADERS),
            (json.dumps(payload()).encode(), {**OPERIT_HEADERS, "Content-Encoding": "gzip"}),
            (json.dumps(payload()).encode(), {**OPERIT_HEADERS, "Content-Length": "-1"}),
            (json.dumps(payload()).encode(), {**OPERIT_HEADERS, "Content-Length": "9" * 5000}),
            (b"{}", {**OPERIT_HEADERS, "Content-Length": str(128 * 1024 + 1)}),
        )
        for body, headers in raw_cases:
            response = await request(
                self.module, "POST", "/v1/operit/share", headers=headers, content=body,
            )
            self.assertIn(response.status_code, {413, 422})
            self.assertNotIn("evil", response.text)
        for header_name, header_values in (
            ("content-length", [b"2", b"3"]),
            ("content-encoding", [b"identity", b"gzip"]),
        ):
            headers = [("Authorization", f"Bearer {OPERIT_KEY}")]
            headers.extend((header_name, value.decode()) for value in header_values)
            response = await request(
                self.module, "POST", "/v1/operit/share", headers=headers,
                content=json.dumps(payload()).encode(),
            )
            self.assertEqual((response.status_code, response.json()["error"]["code"]),
                             (422, "invalid_request_error"))
        self.assertEqual(self.calls, [])

    async def test_chunked_actual_size_and_structural_limits_are_enforced(self):
        async def chunks():
            for _ in range(129):
                yield b"x" * 1024

        oversized = await request(
            self.module, "POST", "/v1/operit/share",
            headers={**OPERIT_HEADERS, "Transfer-Encoding": "chunked"}, content=chunks(),
        )
        self.assertEqual((oversized.status_code, oversized.json()["error"]["code"]),
                         (413, "request_too_large"))
        huge_array = await request(
            self.module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
            json={**payload(), "messages": ["x"] * 257},
        )
        huge_object = await request(
            self.module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
            json={**payload(), **{f"unknown_{index}": index for index in range(129)}},
        )
        self.assertEqual((huge_array.status_code, huge_array.json()["error"]["code"]),
                         (413, "request_too_large"))
        self.assertEqual((huge_object.status_code, huge_object.json()["error"]["code"]),
                         (413, "request_too_large"))
        self.assertEqual(self.calls, [])

    async def test_unusual_content_type_does_not_bypass_validation(self):
        invalid = await request(
            self.module, "POST", "/v1/operit/share",
            headers={**OPERIT_HEADERS, "Content-Type": "text/plain"},
            content=json.dumps({**payload(), "device_id": "private-device"}),
        )
        self.assertEqual((invalid.status_code, invalid.json()["error"]["code"]),
                         (422, "invalid_request_error"))
        valid = await request(
            self.module, "POST", "/v1/operit/share",
            headers={**OPERIT_HEADERS, "Content-Type": "application/octet-stream"},
            content=json.dumps(payload("content type valid")),
        )
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(len(self.calls), 1)


class OperitIdentityAndCanonicalRedTeamTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name, kelivo=True, operit_share=True)
        self.calls = []

        async def generate(*args):
            self.calls.append(args)
            return {"text": "identity ok", "usage": {}}

        self.module.KELIVO_GENERATOR = generate

    async def post(self, body):
        return await request(
            self.module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS, json=body,
        )

    async def test_client_history_and_fake_identity_never_pollute_provider_or_canonical_meta(self):
        attacker_markers = (
            "fake-channel", "fake-source", "fake-session", "device-android-id",
            "operit-conversation-id", "douyin-account", "douyin-author",
        )
        history = [
            {"role": "system", "content": attacker_markers[0]},
            {"role": "developer", "content": attacker_markers[1]},
            {"role": "assistant", "content": attacker_markers[2]},
            {"role": "user", "content": " ".join(attacker_markers[3:])},
            {"role": "user", "content": "clean final share"},
        ]
        response = await self.post(payload(messages=history))
        self.assertEqual(response.status_code, 200)
        provider_messages = self.calls[0][0]
        encoded_provider = json.dumps(provider_messages)
        self.assertTrue(all(marker not in encoded_provider for marker in attacker_markers))
        self.assertEqual(provider_messages[-1], {
            "role": "user", "content": "[Operit Share]\nclean final share",
        })
        with self.module.db() as conn:
            messages = conn.execute(
                "SELECT direction,text,meta FROM messages ORDER BY id"
            ).fetchall()
            bundle = conn.execute(
                "SELECT context_bundle_json FROM kelivo_requests"
            ).fetchone()[0]
        self.assertEqual([(row["direction"], row["text"]) for row in messages], [
            ("in", "[Operit Share]\nclean final share"), ("out", "identity ok"),
        ])
        for row in messages:
            meta = json.loads(row["meta"])
            self.assertEqual((meta["channel"], meta["source"]), ("operit_share", "operit"))
            self.assertTrue(all(marker not in row["meta"] for marker in attacker_markers))
        self.assertTrue(all(marker not in bundle for marker in attacker_markers))

    async def test_client_metadata_overrides_are_rejected(self):
        forbidden = {
            "channel": "kelivo", "source": "telegram", "session": "attacker",
            "client_id": "attacker", "device_id": "android", "conversation_id": "operit",
            "account": "douyin", "author": "douyin-author", "route": "internal",
        }
        for name, value in forbidden.items():
            response = await self.post({**payload(f"field {name}"), name: value})
            self.assertEqual((response.status_code, response.json()["error"]["code"]),
                             (422, "invalid_request_error"))
        self.assertEqual(self.calls, [])

    async def test_stream_options_are_explicitly_nonsemantic_but_provider_contract_changes_are_not(self):
        first = await self.post(payload("stable share", stream_options={"include_usage": False}))
        replay = await self.post(payload("stable share", stream_options={"include_usage": True}))
        self.assertEqual((first.status_code, replay.status_code), (200, 200))
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(len(self.calls), 1)
        self.module.KELIVO_PERSONA = "changed server persona"
        os.environ.update({"LLM_MODEL": "changed-provider", "LLM_TEMPERATURE": "0.9"})
        changed = await self.post(payload("stable share", stream_options={"include_usage": True}))
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(len(self.calls), 2)
        with self.module.db() as conn:
            rows = conn.execute(
                "SELECT automatic_fingerprint,provider_model,persona_hash FROM kelivo_requests ORDER BY id"
            ).fetchall()
        self.assertEqual(len({row["automatic_fingerprint"] for row in rows}), 2)
        self.assertNotEqual(rows[0]["provider_model"], rows[1]["provider_model"])
        self.assertNotEqual(rows[0]["persona_hash"], rows[1]["persona_hash"])


class OperitLifecycleRedTeamTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def test_all_kelivo_operit_enablement_combinations_and_operit_only_readiness(self):
        combinations = ((True, False), (True, True), (False, True), (False, False))
        for kelivo, operit in combinations:
            with self.subTest(kelivo=kelivo, operit=operit):
                with tempfile.TemporaryDirectory() as root:
                    module = load_app(
                        root, telegram=False, kelivo=kelivo, operit_share=operit,
                    )
                    self.assertEqual(module.DEPLOYMENT.kelivo.enabled, kelivo)
                    self.assertEqual(module.DEPLOYMENT.operit_share.enabled, operit)
                    self.assertEqual(module.KELIVO_GENERATOR is not None, kelivo or operit)
                    with module.db() as conn:
                        mapping = conn.execute(
                            "SELECT api_session FROM kelivo_clients WHERE client_id=?",
                            ("primary-operit-share",),
                        ).fetchone()
                    self.assertEqual(mapping is not None, operit)

        with tempfile.TemporaryDirectory() as root:
            module = load_app(root, telegram=False, kelivo=False, operit_share=True)
            calls = []

            async def generate(*args):
                calls.append(args)
                return {"text": "operit only", "usage": {}}

            module.KELIVO_GENERATOR = generate
            response = await request(
                module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                json=payload("operit only share"),
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(calls[0][1], "shared-test-session")
            models = await request(module, "GET", "/v1/models", headers=OPERIT_HEADERS)
            self.assertEqual(models.status_code, 404)
            ready = await module.readyz()
            self.assertIn("operit_share", json.loads(bytes(ready.body))["checks"])

    async def test_prepared_dispatching_and_uncertain_states_never_redispatch(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root, kelivo=True, operit_share=True)
            calls = 0

            async def generate(*_args):
                nonlocal calls
                calls += 1
                return {"text": "must not run", "usage": {}}

            module.KELIVO_GENERATOR = generate
            validated = module.kelivo_service.validate_operit_share(
                payload("blocked lifecycle"), "ouou-home",
            )
            defaults = deployment_config.resolve_kelivo_provider_contract_defaults(
                os.environ, module.DEPLOYMENT.loop_config,
            )
            contract = module.kelivo_service.freeze_automatic_request(
                module.DB_PATH, "primary-operit-share", validated,
                persona_text=module.KELIVO_PERSONA, persona_source=module.KELIVO_PERSONA_SOURCE,
                provider_model=defaults.provider_model,
                effective_temperature=defaults.temperature,
                effective_max_tokens=defaults.max_tokens,
                identity_scope=module.kelivo_service.OPERIT_SHARE_IDENTITY_SCOPE,
                include_canonical_history=True,
            )
            prepared = module.kelivo_service.prepare_automatic_request(
                module.DB_PATH, "primary-operit-share", contract.automatic_fingerprint,
                validated, contract, persona_source=module.KELIVO_PERSONA_SOURCE,
                provider_model=defaults.provider_model,
                effective_temperature=defaults.temperature,
                effective_max_tokens=defaults.max_tokens,
            )
            first = await request(
                module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                json=payload("blocked lifecycle"),
            )
            self.assertEqual(first.status_code, 409)
            module.kelivo_service.begin_dispatch(
                module.DB_PATH, "primary-operit-share", prepared.idempotency_key,
                stale_seconds=300,
            )
            second = await request(
                module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                json=payload("blocked lifecycle"),
            )
            self.assertEqual(second.status_code, 409)
            module.kelivo_service.fail_request(
                module.DB_PATH, "primary-operit-share", prepared.idempotency_key,
                "transport_uncertain", True,
            )
            third = await request(
                module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                json=payload("blocked lifecycle"),
            )
            self.assertEqual((third.status_code, third.json()["error"]["code"]),
                             (409, "dispatch_uncertain"))
            self.assertEqual(calls, 0)

    async def test_disconnect_after_provider_before_commit_finishes_once_and_replays(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root, kelivo=True, operit_share=True)
            provider_calls = 0
            entered, release = threading.Event(), threading.Event()

            async def generate(*_args):
                nonlocal provider_calls
                provider_calls += 1
                return {"text": "committed once", "usage": {}}

            real_complete = module.kelivo_service.complete_request

            def blocked_complete(*args, **kwargs):
                entered.set()
                release.wait(2)
                return real_complete(*args, **kwargs)

            module.KELIVO_GENERATOR = generate
            with mock.patch.object(module.kelivo_service, "complete_request", new=blocked_complete):
                task = asyncio.create_task(request(
                    module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                    json=payload("disconnect after provider"),
                ))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            replay = await request(
                module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                json=payload("disconnect after provider"),
            )
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(provider_calls, 1)
            with module.db() as conn:
                self.assertEqual(conn.execute(
                    "SELECT status FROM kelivo_requests"
                ).fetchone()[0], "completed")
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM messages WHERE json_extract(meta,'$.channel')='operit_share'"
                ).fetchone()[0], 2)

    async def test_cancellation_before_dispatch_is_terminal_and_never_calls_provider(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root, kelivo=True, operit_share=True)
            calls = 0
            controller = module.kelivo_service.KelivoAdmissionController(1, 1, 2)
            held = await controller.acquire("primary-operit-share")
            module.KELIVO_ADMISSION = controller

            async def generate(*_args):
                nonlocal calls
                calls += 1
                return {"text": "must not run", "usage": {}}

            module.KELIVO_GENERATOR = generate
            task = asyncio.create_task(request(
                module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                json=payload("cancel before dispatch"),
            ))
            for _ in range(200):
                with module.db() as conn:
                    row = conn.execute(
                        "SELECT status FROM kelivo_requests WHERE client_id='primary-operit-share'"
                    ).fetchone()
                if row:
                    break
                await asyncio.sleep(0.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            held.release()
            with module.db() as conn:
                state = conn.execute(
                    "SELECT status,error_category FROM kelivo_requests"
                ).fetchone()
            self.assertEqual((state["status"], state["error_category"]),
                             ("failed", "request_cancelled_before_dispatch"))
            retry = await request(
                module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                json=payload("cancel before dispatch"),
            )
            self.assertEqual(retry.status_code, 409)
            self.assertEqual(calls, 0)


class OperitLogAndLegacyRedTeamTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_material_is_absent_from_error_logs_and_responses(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(root, kelivo=True, operit_share=True)
            sensitive = {
                OPERIT_KEY, OPERIT_KEY[:12], "private share body", "private-session",
                "private-telegram-id", "private-operit-conversation",
                "https://private.invalid/path?token=secret",
            }
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            logger = logging.getLogger()
            logger.addHandler(handler)
            try:
                with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                    responses = [
                        await request(module, "POST", "/v1/operit/share", json=payload()),
                        await request(
                            module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                            content=b'{"private share body":',
                        ),
                        await request(
                            module, "POST",
                            "/v1/operit/share?token=secret&url=https://private.invalid/path?token=secret",
                            headers={"Authorization": "Bearer wrong"}, json=payload(),
                        ),
                        await request(
                            module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                            content=b"x" * (128 * 1024 + 1),
                        ),
                    ]
                    with mock.patch.object(
                        module.kelivo_service, "freeze_automatic_request",
                        side_effect=sqlite3.OperationalError("private share body private-session"),
                    ):
                        responses.append(await request(
                            module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                            json=payload("private share body"),
                        ))
                    async def provider_failure(*_args):
                        raise module.kelivo_service.GenerationError(
                            "private share body provider raw response", False,
                        )
                    module.KELIVO_GENERATOR = provider_failure
                    responses.append(await request(
                        module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                        json=payload("provider 500 private share body"),
                    ))
                    async def provider_uncertain(*_args):
                        raise module.kelivo_service.GenerationError(
                            "private-session timeout", True,
                        )
                    module.KELIVO_GENERATOR = provider_uncertain
                    responses.append(await request(
                        module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                        json=payload("provider timeout private share body"),
                    ))
                    async def provider_ok(*_args):
                        return {"text": "safe", "usage": {}}
                    module.KELIVO_GENERATOR = provider_ok
                    with mock.patch.object(
                        module.kelivo_service, "complete_request",
                        side_effect=sqlite3.OperationalError("private-operit-conversation"),
                    ):
                        responses.append(await request(
                            module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                            json=payload("commit failure private share body"),
                        ))
                    module.DEPLOYMENT = dataclasses.replace(
                        module.DEPLOYMENT,
                        kelivo=dataclasses.replace(
                            module.DEPLOYMENT.kelivo, rate_limit_per_minute=1,
                        ),
                    )
                    responses.append(await request(
                        module, "POST", "/v1/operit/share", headers=OPERIT_HEADERS,
                        json=payload("rate limited private share body"),
                    ))
            finally:
                logger.removeHandler(handler)
            serialized = stream.getvalue() + "\n" + "\n".join(response.text for response in responses)
            self.assertEqual(
                [response.status_code for response in responses],
                [401, 422, 401, 413, 503, 502, 504, 504, 429],
            )
            for value in sensitive:
                self.assertNotIn(value, serialized)
            self.assertNotIn("Authorization", serialized)
            self.assertNotIn("Traceback", serialized)

    async def test_legacy_kelivo_automatic_records_and_explicit_mode_remain_compatible(self):
        with tempfile.TemporaryDirectory() as root:
            module = load_app(
                root, kelivo=True, auto_idempotency=True, operit_share=True,
            )
            calls = 0

            async def generate(*_args):
                nonlocal calls
                calls += 1
                return {"text": "legacy compatible", "usage": {}}

            module.KELIVO_GENERATOR = generate
            kelivo_headers = {"Authorization": "Bearer test-kelivo-key-distinct-1234567890"}
            first = await request(
                module, "POST", "/v1/chat/completions", headers=kelivo_headers,
                json=payload("old automatic record"),
            )
            replay = await request(
                module, "POST", "/v1/chat/completions", headers=kelivo_headers,
                json=payload("old automatic record"),
            )
            with module.db() as conn:
                conn.execute(
                    "UPDATE kelivo_requests SET status='dispatch_uncertain', "
                    "error_category='legacy_transport_uncertain' WHERE idempotency_mode='automatic'"
                )
                conn.commit()
            uncertain = await request(
                module, "POST", "/v1/chat/completions", headers=kelivo_headers,
                json=payload("old automatic record"),
            )
            explicit_headers = {
                **kelivo_headers, "Idempotency-Key": "legacy-explicit-key-0001",
            }
            explicit = await request(
                module, "POST", "/v1/chat/completions", headers=explicit_headers,
                json=payload("explicit unaffected"),
            )
            self.assertEqual(
                (first.status_code, replay.status_code, uncertain.status_code, explicit.status_code),
                (200, 200, 409, 200),
            )
            self.assertEqual(first.json(), replay.json())
            self.assertEqual(calls, 2)
            with module.db() as conn:
                automatic = conn.execute(
                    "SELECT * FROM kelivo_requests WHERE idempotency_mode='automatic'"
                ).fetchone()
                bundle = json.loads(automatic["context_bundle_json"])
                modes = [row[0] for row in conn.execute(
                    "SELECT idempotency_mode FROM kelivo_requests ORDER BY id"
                )]
            self.assertNotIn("automatic_fingerprint", bundle)
            self.assertEqual(automatic["automatic_fingerprint"], automatic["request_identity_hash"])
            self.assertEqual(modes, ["automatic", "explicit"])


if __name__ == "__main__":
    unittest.main()
