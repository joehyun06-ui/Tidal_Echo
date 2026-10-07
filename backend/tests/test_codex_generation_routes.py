import asyncio
import json
import unittest
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException

from backend.codex_generation_routes import install_loop, install_relay
from backend.tests import test_codex_generation_streaming as fixtures


class GenerationRoutesTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.GenerationControlsTest.asyncSetUp
    running = fixtures.GenerationControlsTest.running

    async def build_apps(self):
        def auth(request):
            if request.headers.get("authorization") != "Bearer fixture-only":
                raise HTTPException(401)
        async def body(request):
            return await request.json()
        self.runtime = SimpleNamespace(generation_enabled=True, controls=self.controls)
        integration = SimpleNamespace(session_authority=SimpleNamespace(row_for_session=lambda sid: {"provider": "codex" if sid == "a" else "api"} if sid in {"a", "api"} else None))
        loop = FastAPI()
        install_loop(loop, SimpleNamespace(check_internal_auth=auth, read_internal_json=body), integration, self.runtime)
        self.forwarded = []
        def loop_json(path, method="GET", body=None):
            self.forwarded.append((path, method, body))
            async def call():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=loop), base_url="http://loop") as client:
                    response = await client.request(method, path, json=body, headers={"authorization": "Bearer fixture-only"})
                    if response.is_error:
                        raise HTTPException(response.status_code, response.text)
                    return response.json()
            return asyncio.run(call())
        self.broadcasts = []
        async def broadcast(_subs, payload):
            self.broadcasts.append(payload)
        async def original(kind, body):
            return {"original": True}
        self.relay = SimpleNamespace(app=FastAPI(), check_auth=auth, loop_json=loop_json, handle_stream_delta=original, broadcast=broadcast, app_subs=set())
        install_relay(self.relay)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.relay.app), base_url="http://relay", headers={"authorization": "Bearer fixture-only"})

    async def test_relay_to_loop_preserves_authority_and_ack_semantics(self):
        self.running()
        async with await self.build_apps() as client:
            response = await client.get("/app/sessions/a/generation")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["generation"]["can_stop"])
            response = await client.post("/app/sessions/a/generation/stop", json={"generation_id": "codex-gen-1"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["generation"]["status"], "stopping")
            self.protocol.interrupt.assert_awaited_once_with(thread_id="thr-1", turn_id="turn-1")
            self.assertNotIn("thr-1", response.text)
            self.assertNotIn("turn-1", response.text)

    async def test_unauthorized_bad_body_cross_session_and_disabled_are_rejected(self):
        async with await self.build_apps() as client:
            response = await client.get("/app/sessions/a/generation", headers={"authorization": "Bearer wrong"})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(self.forwarded, [])
            for body in ({"generation_id": "codex-gen-1", "thread_id": "evil"}, None, {"generation_id": "x"*2000}):
                response = await client.post("/app/sessions/a/generation/stop", content=json.dumps(body))
                self.assertEqual(response.status_code, 400)
            response = await client.post("/app/sessions/api/generation/stop", json={"generation_id": "codex-gen-1"})
            self.assertEqual(response.status_code, 409)
            response = await client.get("/app/sessions/missing/generation")
            self.assertEqual(response.status_code, 404)
            self.runtime.generation_enabled = False
            response = await client.get("/app/sessions/a/generation")
            self.assertEqual(response.status_code, 503)
            self.protocol.interrupt.assert_not_awaited()

    async def test_snapshot_broadcast_is_ephemeral_and_existing_delta_path_is_unchanged(self):
        await self.build_apps()
        result = await self.relay.handle_stream_delta("reply_delta", {"text": "api chunk"})
        self.assertEqual(result, {"original": True})
        payload = {"snapshot": True, "provider": "codex", "api_session": "a", "generation_id": "codex-gen-1", "canonical_message_id": 1, "stream_id": "codex-gen-1", "epoch": "fixture", "revision": 1, "text": "partial", "ts": "2026-10-07T00:00:00Z", "ignored_internal_field": "must not forward"}
        await self.relay.handle_stream_delta("reply_delta", payload)
        self.assertEqual(self.broadcasts[0]["type"], "reply_snapshot")
        self.assertNotIn("ignored_internal_field", self.broadcasts[0])
        with self.assertRaises(HTTPException):
            await self.relay.handle_stream_delta("reply_delta", {**payload, "canonical_message_id": 2})

    async def test_proxy_error_cannot_leak_upstream_url_or_secret(self):
        async with await self.build_apps() as client:
            def fail(*_args, **_kwargs):
                raise HTTPException(502, "loop proxy error https://private.invalid?token=private")
            self.relay.loop_json = fail
            response = await client.get("/app/sessions/a/generation")
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json(), {"ok": False, "error": "generation_unavailable"})
