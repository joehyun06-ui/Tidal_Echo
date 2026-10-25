"""Loopback-TCP smoke test for the real relay urllib forwarder.

Runs only as an explicitly invoked worker, not inside legacy test discovery.
Synthetic SQLite fixture, ephemeral 127.0.0.1 listener, no Internet/provider calls.
"""
from __future__ import annotations

import importlib
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import uvicorn

from backend.tests._support import load_app, request
from backend.tests._memory_evidence_pair_worker import MemoryEvidencePairedASGITests


class MemoryEvidenceTcpLoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = load_app(
            self.temp.name, telegram=False, memory=True,
            memory_secret="SYNTHETIC_MEMORY_TEST_SECRET_WITHOUT_REAL_CREDENTIALS",
        )
        self.dbpath = Path(os.environ["RELAY_DB"])
        os.environ.update({
            "MEMORY_EVIDENCE_UI_READONLY_ENABLED": "true",
            "LEGACY_CHAT_BRIDGE_TOKEN": "synthetic-legacy-bridge-token-1234567890",
            "LEGACY_CHAT_BRIDGE_SESSION": "test-legacy",
            "RENDER_TELEGRAM_MVP": "false",
            "CODEX_CONTROL_ENABLED": "false",
        })
        for name in ("backend.p3_relay_app", "backend.legacy_chat_bridge_app", "examples.api_loop"):
            sys.modules.pop(name, None)
            package = sys.modules.get("backend")
            if package is not None and name.startswith("backend."):
                if hasattr(package, name.rsplit(".", 1)[-1]):
                    delattr(package, name.rsplit(".", 1)[-1])
        self.addCleanup(sys.modules.pop, "examples.api_loop", None)
        self.addCleanup(sys.modules.pop, "backend.p3_relay_app", None)
        self.addCleanup(sys.modules.pop, "backend.legacy_chat_bridge_app", None)
        self.loop = importlib.import_module("examples.api_loop")
        self.relay = importlib.import_module("backend.p3_relay_app")
        self.assertEqual(self.relay.relay_app.API_LOOP_INTERNAL_TOKEN,
                         self.loop.API_LOOP_INTERNAL_TOKEN)
        # Same actual migrated SQLite schema and canonical synthetic provenance.
        MemoryEvidencePairedASGITests._insert_real_schema_fixture(self)
        patch = mock.patch.object(self.loop, "session_rows",
                                  return_value=[{"id": "s1"}, {"id": "s2"}])
        patch.start()
        self.addCleanup(patch.stop)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(32)
        self.port = sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(
            self.loop.app, host="127.0.0.1", log_level="error",
            lifespan="off", access_log=False,
        ))
        self.thread = threading.Thread(
            target=lambda: self.server.run(sockets=[sock]),
            daemon=True, name="memory-evidence-local-loop",
        )
        self.thread.start()

        def stop_server():
            self.server.should_exit = True
            self.thread.join(timeout=6)
            sock.close()
            self.assertFalse(self.thread.is_alive(), "local TCP server failed to shut down")
        self.addCleanup(stop_server)
        deadline = time.monotonic() + 10
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            await __import__("asyncio").sleep(0.05)
        self.assertTrue(self.server.started, "local loop service failed to start")

    async def test_real_urllib_transport_from_relay_to_loop_and_sqlite(self):
        with mock.patch.object(
            self.relay.relay_app, "LOOP_INGEST_URL",
            f"http://127.0.0.1:{self.port}/loop/ingest",
        ):
            result = await request(
                self.relay, "GET", "/app/sessions/s1/memory/evidence",
                headers={"Authorization": "Bearer test-relay-secret"},
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "private, no-store")
        payload = result.json()
        self.assertEqual(payload["api_session"], "s1")
        self.assertEqual(payload["views"][0]["answer"], "Aurora demo: Friday 16:30")

    async def test_real_tcp_internal_endpoint_rejects_missing_token(self):
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/loop/sessions/s1/memory/evidence",
                timeout=5,
            )
        self.assertEqual(rejected.exception.code, 401)
        rejected.exception.close()


if __name__ == "__main__":
    unittest.main()
