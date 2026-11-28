"""Exercise the pinned native history protocol with a disposable home and local upstream."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import tempfile
import unittest

from backend.codex_app_server_shared_transport import _bundled_codex_path


class NativeRewriteHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_answer_reaches_next_request_without_creating_a_turn(self):
        observed = asyncio.get_running_loop().create_future()

        async def provider(reader, writer):
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                fields = dict(line.split(b":", 1) for line in headers.lower().split(b"\r\n")[1:] if b":" in line)
                body = json.loads(await reader.readexactly(int(fields[b"content-length"])))
                if not observed.done():
                    observed.set_result(body["input"])
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(provider, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as home:
            Path(home, "config.toml").write_text(
                f'model_provider="fixture"\n[model_providers.fixture]\nname="fixture"\n'
                f'base_url="http://127.0.0.1:{port}/v1"\nwire_api="responses"\n', encoding="utf-8",
            )
            env = {"PATH": os.environ["PATH"], "HOME": home, "CODEX_HOME": home,
                   "HTTPS_PROXY": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1",
                   "NO_PROXY": "127.0.0.1,localhost"}
            proc = await asyncio.create_subprocess_exec(
                _bundled_codex_path(), "app-server", "--listen", "stdio://", cwd=home, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            request_id = 0

            async def rpc(method, params):
                nonlocal request_id
                request_id += 1
                proc.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}).encode() + b"\n")
                await proc.stdin.drain()
                while line := await proc.stdout.readline():
                    message = json.loads(line)
                    if message.get("id") == request_id:
                        self.assertNotIn("error", message, method)
                        return message["result"]
                self.fail(f"App Server exited during {method}")

            try:
                async with asyncio.timeout(20):
                    await rpc("initialize", {"clientInfo": {"name": "fixture", "version": "1"},
                                             "capabilities": {"experimentalApi": True}})
                    started = await rpc("thread/start", {"model": "fixture-model", "cwd": home,
                        "historyMode": "paginated", "ephemeral": False, "sandbox": "read-only",
                        "approvalPolicy": "never", "environments": [], "runtimeWorkspaceRoots": []})
                    thread = {"threadId": started["thread"]["id"]}
                    rewritten = "前缀🙂\r\n    pass\r\n后缀"
                    history = [{"type": "message", "role": role, "content": [{"type": kind, "text": text}]}
                               for role, kind, text in [("user", "input_text", "原问题"),
                                                       ("assistant", "output_text", rewritten)]]
                    self.assertEqual(await rpc("thread/inject_items", {**thread, "items": history}), {})
                    self.assertEqual((await rpc("thread/turns/list", thread))["data"], [])
                    await rpc("turn/start", {**thread, "input": [{"type": "text", "text": "追问"}], "environments": []})
                    tail = (await observed)[-3:]
                    self.assertEqual([(x["role"], x["content"][0]["text"]) for x in tail],
                                     [("user", "原问题"), ("assistant", rewritten), ("user", "追问")])
            finally:
                with suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
