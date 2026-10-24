"""Exact relay and loop ingress tests; synthetic DB, no network or secrets."""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend.tests._support import NoNetworkMixin, load_app, request
from backend.tests.test_memory_evidence_readonly import database

TOKEN = "invalid-internal-test-token-1234567890"
ROUTE = "/loop/sessions/s1/memory/evidence"
PUBLIC = "/app/sessions/s1/memory/evidence"


class LoopEvidenceRouteTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.dbpath = root / "relay.sqlite"
        source = database()
        with sqlite3.connect(str(self.dbpath)) as dest:
            source.backup(dest)
        source.close()
        patch = mock.patch.dict(os.environ, {
            "LOOP_CONFIG": str(root / "loop.config.json"),
            "RELAY_DB": str(self.dbpath),
            "RELAY_SECRET": "invalid-test-relay-secret",
            "RELAY_URL": "http://invalid.test",
            "LLM_API_BASE": "https://provider.invalid/v1",
            "LLM_API_KEY": "invalid-key",
            "LLM_MODEL": "gpt-test",
            "API_LOOP_INTERNAL_TOKEN": TOKEN,
            "CODEX_CONTROL_ENABLED": "false",
            "RENDER_TELEGRAM_MVP": "false",
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_EVIDENCE_UI_READONLY_ENABLED": "true",
        }, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        sys.modules.pop("examples.api_loop", None)
        self.addCleanup(lambda: sys.modules.pop("examples.api_loop", None))
        self.module = importlib.import_module("examples.api_loop")

    async def get(self, *, token=TOKEN, url=ROUTE):
        headers = {} if token is None else {"X-API-Loop-Internal-Token": token}
        transport = httpx.ASGITransport(app=self.module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://loop.test") as client:
            return await client.get(url, headers=headers)

    async def test_authentication_precedes_session_query(self):
        with mock.patch.object(self.module, "session_rows", side_effect=AssertionError("accessed")):
            result = await self.get(token=None)
        self.assertEqual(result.status_code, 401)

    async def test_defaults_off_even_with_valid_auth(self):
        del os.environ["MEMORY_EVIDENCE_UI_READONLY_ENABLED"]
        with mock.patch.object(self.module, "session_rows", side_effect=AssertionError("accessed")):
            result = await self.get()
        self.assertEqual(result.status_code, 404)

    async def test_missing_session_returns_404(self):
        with mock.patch.object(self.module, "session_rows", return_value=[]):
            result = await self.get()
        self.assertEqual(result.status_code, 404)

    async def test_verified_session_reads_real_file_read_only(self):
        with mock.patch.object(self.module, "session_rows", return_value=[{"id": "s1"}]):
            result = await self.get()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "private, no-store")
        self.assertEqual(result.json()["views"][0]["source_ids"], ["memory-source-1"])

    async def test_only_session_bound_records_are_retrieved(self):
        with mock.patch.object(self.module, "session_rows", return_value=[{"id": "s1"},{"id":"s2"}]):
            result = await self.get(url="/loop/sessions/s2/memory/evidence")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["views"], [])

    async def test_internal_token_required(self):
        with mock.patch.object(self.module, "session_rows", side_effect=AssertionError("accessed")):
            result = await self.get(token="wrong")
        self.assertEqual(result.status_code, 401)


class RelayEvidenceRouteTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        load_app(self.temp.name, telegram=False)
        os.environ.update({
            "LEGACY_CHAT_BRIDGE_TOKEN": "invalid-test-bridge-token",
            "LEGACY_CHAT_BRIDGE_SESSION": "legacy-test",
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_EVIDENCE_UI_READONLY_ENABLED": "true",
            "CODEX_CONTROL_ENABLED": "false",
        })
        package=sys.modules.get("backend")
        for name in ("backend.p3_relay_app","backend.legacy_chat_bridge_app"):
            sys.modules.pop(name,None)
            if package is not None:
                attr=name.rsplit(".",1)[-1]
                if hasattr(package,attr):
                    delattr(package,attr)
        self.module=importlib.import_module("backend.p3_relay_app")
        self.addCleanup(sys.modules.pop,"backend.p3_relay_app",None)
        self.addCleanup(sys.modules.pop,"backend.legacy_chat_bridge_app",None)

    async def test_relay_denies_unauthenticated_before_upstream(self):
        with mock.patch.object(self.module.relay_app,"loop_json",side_effect=AssertionError("called")):
            response=await request(self.module,"GET",PUBLIC)
        self.assertEqual(response.status_code,401)

    async def test_relay_default_off_does_not_contact_upstream(self):
        del os.environ["MEMORY_EVIDENCE_UI_READONLY_ENABLED"]
        with mock.patch.object(self.module.relay_app,"loop_json",side_effect=AssertionError("called")):
            response=await request(self.module,"GET",PUBLIC,headers={"Authorization":"Bearer test-relay-secret"})
        self.assertEqual(response.status_code,404)

    async def test_relay_calls_exact_internal_endpoint(self):
        expected={"schema":"guitting-memory-retrieval-ui-v1","mode":"server_readonly",
            "api_session":"s1","ready_for_live_frontend":True,"views":[]}
        with mock.patch.object(self.module.relay_app,"loop_json",return_value=expected) as proxy:
            response=await request(self.module,"GET",PUBLIC,headers={"Authorization":"Bearer test-relay-secret"})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers["cache-control"],"private, no-store")
        self.assertEqual(response.json(),expected)
        proxy.assert_called_once_with("/loop/sessions/s1/memory/evidence")


if __name__=="__main__":
    unittest.main()
