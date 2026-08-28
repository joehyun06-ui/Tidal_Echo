from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException, Request

from backend import codex_canary_admin_proxy as proxy
from backend import codex_generation_store as generation_store


class CodexCanaryAdminProxyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        app = FastAPI()

        def check_auth(request: Request):
            if request.headers.get("authorization") != "Bearer test-secret":
                raise HTTPException(status_code=401, detail="unauthorized")

        def loop_json(path: str, method: str = "GET", body=None):
            self.calls.append((path, method, body))
            if path == "/loop/provider/canary/create":
                return {
                    "ok": True,
                    "provider": "codex",
                    "created": {"id": "api-canary-1", "title": (body or {}).get("title") or "Codex canary"},
                }
            if path == "/loop/provider/canary/api-canary-1/status":
                return {
                    "ok": True,
                    "provider": "codex",
                    "session": {
                        "api_session": "api-canary-1",
                        "status": "active",
                        "model": "gpt-test",
                        "model_provider": "unresolved",
                        "reasoning_effort": "high",
                        "thread_bound": False,
                        "persona_hash": "must-not-escape",
                    },
                }
            if path == "/loop/provider/canary/api-canary-1/retire":
                return {
                    "ok": True,
                    "provider": "api",
                    "retired": {"api_session": "api-canary-1", "status": "retired"},
                }
            raise HTTPException(status_code=503, detail="internal unexpected detail")

        self.relay = SimpleNamespace(app=app, check_auth=check_auth, loop_json=loop_json)
        proxy.install(self.relay)
        self.transport = httpx.ASGITransport(app=app)

    async def request(self, method: str, path: str, **kwargs):
        async with httpx.AsyncClient(transport=self.transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    async def test_auth_is_required_before_loop_proxy(self):
        response = await self.request("POST", "/provider/canary/create", json={})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.calls, [])

    async def test_create_proxies_bounded_body_and_projects_only_public_fields(self):
        response = await self.request(
            "POST",
            "/provider/canary/create",
            headers={"Authorization": "Bearer test-secret"},
            json={"title": "trial"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True,
            "provider": "codex",
            "created": {"api_session": "api-canary-1", "title": "trial"},
        })
        self.assertEqual(self.calls, [
            ("/loop/provider/canary/create", "POST", {"title": "trial"})
        ])

    async def test_create_rejects_unknown_fields_and_oversized_body_without_proxying(self):
        headers = {"Authorization": "Bearer test-secret"}
        response = await self.request("POST", "/provider/canary/create", headers=headers, json={"x": 1})
        self.assertEqual(response.status_code, 400)
        big = json.dumps({"title": "x" * proxy.MAX_ADMIN_BODY_BYTES})
        response = await self.request(
            "POST",
            "/provider/canary/create",
            content=big,
            headers={**headers, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])

    async def test_status_is_strongly_correlated_and_does_not_expose_store_secrets(self):
        response = await self.request(
            "GET",
            "/provider/canary/api-canary-1/status",
            headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["session"]["api_session"], "api-canary-1")
        self.assertEqual(payload["session"]["thread_bound"], False)
        self.assertNotIn("persona_hash", payload["session"])
        self.assertNotIn("thread_id", payload["session"])

    async def test_diagnostic_route_is_authenticated_and_projects_only_bounded_state(self):
        diagnostic = {
            "ok": True,
            "provider": "codex",
            "diagnostic": {
                "api_session": "api-canary-1",
                "session_status": "active",
                "thread_bound": True,
                "model_provider": "openai",
                "latest_job": {
                    "status": "dispatch_uncertain",
                    "attempt_count": 1,
                    "recovery_count": 2,
                    "turn_bound": True,
                    "assistant_message_bound": False,
                    "error_category": "codex_dispatch_uncertain",
                },
            },
        }
        with patch.object(proxy, "_read_generation_diagnostic", return_value=diagnostic) as read:
            response = await self.request(
                "GET",
                "/provider/canary/api-canary-1/diagnostic",
                headers={"Authorization": "Bearer test-secret"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), diagnostic)
        read.assert_called_once_with("api-canary-1")
        self.assertNotIn("thread_id", response.text)
        self.assertNotIn("client_message_id", response.text)
        self.assertNotIn("callback_identity", response.text)

    async def test_diagnostic_reader_works_while_generation_is_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store_path = root / "codex-generation.db"
            generation_store.initialize(store_path)
            generation_store.pin_session(
                store_path,
                api_session="api-canary-1",
                model="gpt-test",
                model_provider="openai",
                reasoning_effort="low",
                persona_hash="a" * 64,
            )
            generation_store.enqueue_job(
                store_path,
                api_session="api-canary-1",
                canonical_message_id=41,
                input_digest="b" * 64,
                generation_id="codex-gen-41",
                client_message_id="codex-client-41",
                callback_identity="codex-callback-41",
            )
            with patch.dict(os.environ, {
                "RENDER_PERSISTENT_ROOT": str(root),
                "CODEX_GENERATION_DB": str(store_path),
                "CODEX_GENERATION_ENABLED": "false",
            }, clear=False):
                payload = proxy._read_generation_diagnostic("api-canary-1")
        self.assertEqual(payload["diagnostic"]["session_status"], "active")
        self.assertEqual(payload["diagnostic"]["model_provider"], "openai")
        self.assertEqual(payload["diagnostic"]["latest_job"], {
            "status": "queued",
            "attempt_count": 0,
            "recovery_count": 0,
            "turn_bound": False,
            "assistant_message_bound": False,
            "error_category": None,
        })

    async def test_retire_requires_exact_session_correlation(self):
        response = await self.request(
            "POST",
            "/provider/canary/api-canary-1/retire",
            headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["retired"], {
            "api_session": "api-canary-1",
            "status": "retired",
        })

    async def test_invalid_session_id_never_reaches_loop_proxy(self):
        response = await self.request(
            "GET",
            "/provider/canary/bad%20session/status",
            headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])

    async def test_unknown_loop_error_is_collapsed(self):
        response = await self.request(
            "GET",
            "/provider/canary/api-other/status",
            headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"ok": False, "error": "codex_canary_unavailable"})
        self.assertNotIn("internal unexpected detail", response.text)

    def test_known_structured_loop_error_is_safely_preserved(self):
        error = proxy._loop_error(HTTPException(
            status_code=503,
            detail='{"ok":false,"dispatch_uncertain":false,"error":"codex_generation_disabled"}',
        ))
        self.assertEqual(error.category, "codex_generation_disabled")
        self.assertEqual(error.status_code, 503)

    def test_malformed_created_session_is_server_unavailable_not_user_error(self):
        with self.assertRaises(proxy.CodexCanaryAdminProxyError) as raised:
            proxy._project_created({
                "ok": True,
                "provider": "codex",
                "created": {"id": "bad session", "title": "trial"},
            })
        self.assertEqual(raised.exception.category, "codex_canary_unavailable")
        self.assertEqual(raised.exception.status_code, 503)

    def test_install_is_idempotent(self):
        before = [(getattr(route, "path", None), tuple(sorted(getattr(route, "methods", set())))) for route in self.relay.app.routes]
        proxy.install(self.relay)
        after = [(getattr(route, "path", None), tuple(sorted(getattr(route, "methods", set())))) for route in self.relay.app.routes]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
