from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from backend.codex_app_server_shared_transport import (
    CodexSharedAppServerRuntime,
    CodexSharedTransportConfig,
    CodexTransportError,
)


FAKE_SERVER_SOURCE = r'''
import asyncio
import json
import sys

async def main():
    while True:
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            return
        msg = json.loads(line)
        method = msg.get("method")
        if method == "initialized":
            continue
        if method == "initialize":
            print(json.dumps({"id": msg["id"], "result": {"serverInfo": {"name": "fake"}}}), flush=True)
            continue
        if method == "account/read":
            print(json.dumps({"id": msg["id"], "result": {"account": {"type": "chatgpt"}}}), flush=True)
            continue
        if method == "turn/start":
            print(json.dumps({"id": msg["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress"}}}), flush=True)
            print(json.dumps({"method": "turn/started", "params": {"threadId": "thr-1", "turn": {"id": "turn-1", "status": "inProgress"}}}), flush=True)
            continue
        if method == "emit/server-request":
            print(json.dumps({"id": 990, "method": "item/tool/request", "params": {"secret": "PRIVATE"}}), flush=True)
            print(json.dumps({"id": msg["id"], "result": {}}), flush=True)
            continue
        if method == "delay":
            await asyncio.sleep(2)
            print(json.dumps({"id": msg["id"], "result": {}}), flush=True)
            continue
        print(json.dumps({"id": msg["id"], "result": {}}), flush=True)

asyncio.run(main())
'''


class SharedTransportTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.workspace = self.root / "workspace"
        self.home.mkdir()
        self.workspace.mkdir()
        self.fake = self.root / "fake.py"
        self.fake.write_text(FAKE_SERVER_SOURCE, encoding="utf-8")
        self.runtimes = []

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.close()
        self.temp.cleanup()

    def runtime(self, timeout=1.0, captured=None):
        config = CodexSharedTransportConfig(True, self.home, self.workspace, timeout)

        async def launcher(*command, **kwargs):
            if captured is not None:
                captured["command"] = command
                captured["env"] = dict(kwargs["env"])
                captured["stderr"] = kwargs["stderr"]
            return await asyncio.create_subprocess_exec(
                sys.executable,
                str(self.fake),
                stdin=kwargs["stdin"],
                stdout=kwargs["stdout"],
                stderr=kwargs["stderr"],
                cwd=kwargs["cwd"],
                env=kwargs["env"],
                limit=kwargs["limit"],
            )

        runtime = CodexSharedAppServerRuntime(
            config,
            _runtime_resolver=lambda: "pinned-codex",
            _process_launcher=launcher,
            _parent_environment={
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "LLM_API_KEY": "PRIVATE-LLM",
                "RELAY_SECRET": "PRIVATE-RELAY",
                "OPENAI_API_KEY": "PRIVATE-OPENAI",
            },
        )
        self.runtimes.append(runtime)
        return runtime

    async def test_scopes_enforce_independent_method_allowlists(self):
        runtime = self.runtime()
        control = runtime.scope(methods=frozenset({"account/read"}))
        generation = runtime.scope(methods=frozenset({"turn/start"}))
        account = await control.request("account/read", {"refreshToken": False})
        self.assertEqual(account["account"]["type"], "chatgpt")
        with self.assertRaisesRegex(CodexTransportError, "protocol_error"):
            await control.request("turn/start", {})
        turn = await generation.request("turn/start", {})
        self.assertEqual(turn["turn"]["id"], "turn-1")

    async def test_one_runtime_launch_is_shared_by_two_scopes(self):
        launches = 0
        runtime = self.runtime()
        original = runtime._process_launcher

        async def counting_launcher(*args, **kwargs):
            nonlocal launches
            launches += 1
            return await original(*args, **kwargs)

        runtime._process_launcher = counting_launcher
        control = runtime.scope(methods=frozenset({"account/read"}))
        generation = runtime.scope(methods=frozenset({"turn/start"}))
        await control.request("account/read", {})
        await generation.request("turn/start", {})
        self.assertEqual(launches, 1)

    async def test_child_environment_is_secret_free_and_launch_is_read_only(self):
        captured = {}
        runtime = self.runtime(captured=captured)
        control = runtime.scope(methods=frozenset({"account/read"}))
        await control.request("account/read", {})
        self.assertEqual(set(captured["env"]), {"PATH", "LANG", "CODEX_HOME", "HOME", "RUST_LOG"})
        self.assertFalse(any("PRIVATE" in value for value in captured["env"].values()))
        self.assertEqual(captured["command"], (
            "pinned-codex",
            "--config", 'approval_policy="never"',
            "--config", 'sandbox_mode="read-only"',
            "--config", "features.plugins=false",
            "--config", "features.web_search_request=false",
            "app-server", "--listen", "stdio://",
        ))

    async def test_generation_notification_is_delivered_only_to_subscribed_scope(self):
        seen = []

        async def handler(method, params):
            seen.append((method, params["threadId"]))

        runtime = self.runtime()
        runtime.scope(methods=frozenset({"account/read"}))
        generation = runtime.scope(
            methods=frozenset({"turn/start"}),
            notifications=frozenset({"turn/started"}),
            handler=handler,
        )
        await generation.request("turn/start", {})
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(seen, [("turn/started", "thr-1")])

    async def test_unsubscribed_notifications_are_silently_dropped(self):
        seen = []
        runtime = self.runtime()
        generation = runtime.scope(
            methods=frozenset({"turn/start"}),
            notifications=frozenset({"turn/completed"}),
            handler=lambda method, params: seen.append(method),
        )
        await generation.request("turn/start", {})
        await asyncio.sleep(0.02)
        self.assertEqual(seen, [])

    async def test_server_requests_are_denied_and_do_not_escape_scope(self):
        runtime = self.runtime()
        scope = runtime.scope(methods=frozenset({"emit/server-request"}))
        await scope.request("emit/server-request", {})
        self.assertIsNotNone(runtime._process)

    async def test_timeout_keeps_fixed_category_and_tears_down_process(self):
        runtime = self.runtime(timeout=0.05)
        scope = runtime.scope(methods=frozenset({"delay"}))
        with self.assertRaisesRegex(CodexTransportError, "codex_app_server_timeout"):
            await scope.request("delay", {})
        self.assertIsNone(runtime._process)

    async def test_disabled_runtime_never_launches(self):
        config = CodexSharedTransportConfig(False, self.home, self.workspace, 1)
        launched = False

        async def launcher(*args, **kwargs):
            nonlocal launched
            launched = True
            raise AssertionError

        runtime = CodexSharedAppServerRuntime(config, _process_launcher=launcher)
        self.runtimes.append(runtime)
        scope = runtime.scope(methods=frozenset({"account/read"}))
        with self.assertRaisesRegex(CodexTransportError, "codex_app_server_disabled"):
            await scope.request("account/read", {})
        self.assertFalse(launched)


if __name__ == "__main__":
    unittest.main()
