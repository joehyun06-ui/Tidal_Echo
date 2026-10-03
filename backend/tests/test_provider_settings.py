from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi import FastAPI, HTTPException

from backend import codex_generation_store as store, codex_session_model, provider_settings
from backend.codex_generation_protocol import CodexGenerationConfig, CodexGenerationProtocol, CodexProcessActivityGate, ModelSelection
from backend.codex_model_catalog import read_models
from backend.tests._support import NoNetworkMixin


class ProviderSettingsTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = {
            "API_LOOP_INTERNAL_TOKEN": "internal-test-token-12345678901234567890", "LOOP_CONFIG": str(self.root / "loop.json"),
            "RELAY_DB": str(self.root / "relay.db"), "LLM_MODEL": "before-model",
            "LLM_API_BASE": "https://api.example.test/v1", "LLM_API_KEY": "server-secret-private",
            "CODEX_CONTROL_ENABLED": "false", "RENDER_TELEGRAM_MVP": "false",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("examples.api_loop", None)
        self.addCleanup(sys.modules.pop, "examples.api_loop", None)
        self.loop = importlib.import_module("examples.api_loop")
        self.loop.save_config({"history_n": 17, "sessions": [{"id": "api-1", "title": "keep"}], "active_session": "api-1"})

    async def call(self, method, path, body=None, authenticated=True, app=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app or self.loop.app), base_url="http://test") as client:
            return await client.request(method, path, json=body, headers={"X-API-Loop-Internal-Token": "internal-test-token-12345678901234567890"} if authenticated else {})

    async def test_api_config_is_authenticated_and_never_returns_key(self):
        denied = await self.call("GET", "/loop/provider/api/config", authenticated=False)
        self.assertEqual(denied.status_code, 401)
        read = await self.call("GET", "/loop/provider/api/config")
        self.assertTrue(read.json()["has_key"])
        self.assertNotIn("server-secret", read.text)
        saved = await self.call("POST", "/loop/provider/api/config", {"url": "https://api.example.test/v1", "model": "after-model"})
        self.assertEqual(saved.status_code, 200)
        self.assertNotIn("server-secret", saved.text)
        # A fresh disk read, rather than a process-local picker value, drives main_chain.
        config = json.loads(Path(self.loop.LOOP_CONFIG).read_text())
        self.assertEqual(config["history_n"], 17)
        self.assertEqual(config["sessions"][0]["title"], "keep")
        self.assertEqual(self.loop.main_chain()[0]["model"], "after-model")
        self.assertEqual(self.loop.main_chain()[0]["key"], "server-secret-private")

    async def test_url_change_requires_new_key_and_invalid_body_cannot_write(self):
        before = Path(self.loop.LOOP_CONFIG).read_bytes()
        bodies = [
            {"url": "https://other.test/v1", "model": "next"},
            {"url": "https://user:password@other.test/v1", "model": "next", "key": "new"},
            {"url": "https://other.test/v1", "model": "next", "key": "new\nheader"},
            {"url": "https://other.test/v1", "model": "next", "key": "new", "sessions": []},
        ]
        for body in bodies:
            response = await self.call("POST", "/loop/provider/api/config", body)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(Path(self.loop.LOOP_CONFIG).read_bytes(), before)
        changed = await self.call("POST", "/loop/provider/api/config", {"url": "https://other.test/v1", "model": "next", "key": "new-key"})
        self.assertEqual(changed.status_code, 200)
        self.assertNotIn("new-key", changed.text)

    async def test_api_catalog_uses_server_credentials_and_does_not_follow_redirects(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={"data": [{"id": "one", "secret": "private"}, {"id": "two"}]})
        client = httpx.AsyncClient
        with mock.patch.object(provider_settings.httpx, "AsyncClient", side_effect=lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs)) as constructor:
            response = await provider_settings.api_models(self.loop)
        self.assertEqual(response, {"models": ["one", "two"]})
        self.assertEqual(calls[0].headers["authorization"], "Bearer server-secret-private")
        self.assertFalse(constructor.call_args.kwargs["follow_redirects"])
        def denied(request):
            return httpx.Response(401, text="server-secret-private")
        with mock.patch.object(provider_settings.httpx, "AsyncClient", side_effect=lambda **kwargs: client(transport=httpx.MockTransport(denied), **kwargs)):
            with self.assertRaises(HTTPException) as error:
                await provider_settings.api_models(self.loop)
        self.assertEqual(error.exception.detail, "provider_auth_rejected")

    async def test_relay_auth_and_proxy_errors_are_fixed(self):
        relay = SimpleNamespace(check_auth=self.loop.check_internal_auth, loop_json=mock.Mock(side_effect=HTTPException(500, "RAW_SECRET")))
        app = FastAPI()
        provider_settings.install_relay(app, relay)
        for method, path in [("GET", "/app/provider/api/config"), ("POST", "/app/provider/api/config"), ("GET", "/app/provider/api/models"), ("GET", "/app/sessions/api-1/model"), ("POST", "/app/sessions/api-1/model")]:
            denied = await self.call(method, path, {}, authenticated=False, app=app)
            self.assertEqual(denied.status_code, 401)
        async def inline(operation, *args, **kwargs):
            return operation(*args, **kwargs)
        with mock.patch.object(provider_settings, "run_in_threadpool", new=inline):
            response = await self.call("GET", "/app/provider/api/config", app=app)
        self.assertEqual(response.json(), {"detail": "provider_settings_unavailable"})


class CodexModelSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "codex.db"
        store.initialize(self.path)
        store.pin_session(self.path, api_session="api-codex", model="model-old", model_provider="openai", reasoning_effort="high", persona_hash="a" * 64)
        self.calls = []
        async def rpc(method, params):
            self.calls.append((method, params))
            if method == "account/read":
                return {"account": {"type": "chatgpt"}}
            if method == "model/list":
                return {"data": [{"model": "model-new", "displayName": "New", "defaultReasoningEffort": "low", "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]}]}
            if method == "thread/start":
                return {"thread": {"id": "thr-1", "ephemeral": False, "historyMode": "paginated"}, "model": params["model"], "modelProvider": "openai", "cwd": params["cwd"]}
            if method == "turn/start":
                return {"turn": {"id": "turn-1", "status": "inProgress"}}
            raise AssertionError(method)
        self.protocol = CodexGenerationProtocol(CodexGenerationConfig(True, Path(self.temp.name) / "workspace"), SimpleNamespace(request=rpc))
        self.gate = CodexProcessActivityGate()
        self.runtime = SimpleNamespace(generation_enabled=True, config=SimpleNamespace(store_path=self.path), foundation=SimpleNamespace(activity_gate=self.gate, generation=self.protocol))
        def auth(request):
            if request.headers.get("authorization") != "Bearer test":
                raise HTTPException(401, "unauthorized")
        self.app = FastAPI()
        codex_session_model.install(self.app, SimpleNamespace(check_internal_auth=auth), self.runtime, SimpleNamespace(provider_for_session=lambda sid: "codex"))

    async def save(self, body):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            return await client.post("/loop/sessions/api-codex/model", json=body, headers={"authorization": "Bearer test"})

    async def test_selected_model_is_durable_and_used_by_first_thread_and_turn(self):
        result = await self.save({"model": "model-new", "reasoning_effort": "high"})
        self.assertEqual(result.status_code, 200)
        row = store.get_session(self.path, "api-codex")
        self.assertEqual(row["model_provider"], "openai")
        self.assertEqual(row["persona_hash"], "a" * 64)
        thread = await self.protocol.start_thread(api_session="api-codex", attempt_id="attempt-1", persona="persona", selection=ModelSelection(row["model"], row["reasoning_effort"]))
        await self.protocol.start_turn(thread_id=thread.thread_id, client_message_id="client-1", text="hello", model=row["model"], reasoning_effort=row["reasoning_effort"])
        self.assertEqual([p["model"] for m,p in self.calls if m in {"thread/start", "turn/start"}], ["model-new", "model-new"])
        self.assertEqual(self.calls[-1][1]["effort"], "high")

    async def test_busy_queued_and_uncertain_jobs_block_switch_without_changing_selection(self):
        async with self.gate.generation():
            result = await self.save({"model": "model-new"})
        self.assertEqual(result.status_code, 409)
        store.enqueue_job(self.path, api_session="api-codex", canonical_message_id=1, input_digest=hashlib.sha256(b"hi").hexdigest(), generation_id="g-1", client_message_id="c-1", callback_identity="cb-1")
        for status in store.ACTIVE_JOB_STATUSES:
            conn = store.connect(self.path)
            conn.execute("UPDATE codex_generation_jobs SET status=?", (status,))
            conn.close()
            result = await self.save({"model": "model-new"})
            self.assertEqual(result.status_code, 409, status)
        self.assertEqual(store.get_session(self.path, "api-codex")["model"], "model-old")

    async def test_unknown_model_effort_and_retired_session_cannot_be_changed(self):
        for body in [{"model": "missing"}, {"model": "model-new", "reasoning_effort": "invalid"}]:
            response = await self.save(body)
            self.assertEqual(response.status_code, 400)
        conn = store.connect(self.path)
        conn.execute("UPDATE codex_sessions SET status='retired'")
        conn.close()
        self.assertEqual((await self.save({"model": "model-new"})).status_code, 409)

    async def test_catalog_pagination_redaction_and_repeated_cursor(self):
        calls = []
        async def page(method, params):
            calls.append(params)
            return {"data": [{"model": "second" if params.get("cursor") else "first", "secret": "DO_NOT_RETURN"}], "nextCursor": None if params.get("cursor") else "page2"}
        result = await read_models(page)
        self.assertEqual([m["model"] for m in result], ["first", "second"])
        self.assertNotIn("DO_NOT_RETURN", repr(result))
        self.assertEqual(calls[1]["cursor"], "page2")
        async def repeated(*args):
            return {"data": [], "nextCursor": "same"}
        with self.assertRaises(ValueError):
            await read_models(repeated)


class CodexLoginStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_login_tracks_correlated_failure_without_retaining_raw_error(self):
        from backend.codex_account_control_facade import CodexAccountControlFacade
        async def rpc(method, params):
            if method == "account/login/start":
                return {"loginId": "attempt-1", "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "ABCD"}
            if method == "account/read":
                return {"account": None}
            if method == "model/list":
                return {"data": [{"model": "available", "secret": "PRIVATE"}]}
            return {}
        control = CodexAccountControlFacade(SimpleNamespace(request=rpc), CodexProcessActivityGate())
        await control.login_start()
        self.assertEqual((await control.status())["login_status"], "pending")
        await control.on_notification("account/login/completed", {"loginId": "old-attempt", "success": False})
        self.assertEqual((await control.status())["login_status"], "pending")
        await control.on_notification("account/login/completed", {"loginId": "attempt-1", "success": False, "error": "PRIVATE"})
        self.assertEqual((await control.status())["login_status"], "failed")
        self.assertNotIn("PRIVATE", repr(control.__dict__))
        self.assertNotIn("PRIVATE", repr(await control.models()))
        await control.logout()
        self.assertEqual((await control.status())["login_status"], "idle")

    async def test_completion_arriving_before_login_response_is_not_lost(self):
        from backend.codex_account_control_facade import CodexAccountControlFacade
        async def rpc(method, params):
            if method == "account/login/start":
                await control.on_notification("account/login/completed", {"loginId": "attempt-2", "success": True})
                return {"loginId": "attempt-2", "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "ABCD"}
            return {"account": None}
        control = CodexAccountControlFacade(SimpleNamespace(request=rpc), CodexProcessActivityGate())
        await control.login_start()
        self.assertEqual((await control.status())["login_status"], "succeeded")
        self.assertEqual(control._login_id, "")
