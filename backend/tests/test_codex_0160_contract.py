"""Validate emitted wire requests against schemas from the installed real CLI."""
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_cli_bin import bundled_codex_path
from jsonschema import Draft7Validator

from backend.codex_generation_hardening_transport import CodexGenerationHardeningTransport
from backend.codex_generation_protocol import CodexGenerationConfig, CodexGenerationProtocol
from backend.codex_model_catalog import read_models
from backend.codex_app_server_shared_transport import CodexSharedAppServerRuntime, CodexSharedTransportConfig
from backend.codex_generation_progress import ReplyProgress
from backend.codex_generation_images import ImageInput, ImageMessageInput


class Codex0160ContractTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.schema_dir = cls.root / "schema"
        cls.version = subprocess.check_output([str(bundled_codex_path()), "--version"], text=True).strip()
        subprocess.run([str(bundled_codex_path()), "app-server", "generate-json-schema", "--experimental", "--out", str(cls.schema_dir)], check=True, capture_output=True, timeout=30)

    def schema(self, name):
        return json.loads((self.schema_dir / "v2" / (name + ".json")).read_text())

    async def test_reply_projection_accepts_real_cli_notification_schemas(self):
        progress = ReplyProgress()
        context = {"threadId": "thr-fixture", "turnId": "turn-fixture"}
        events = [
            ("item/started", "ItemStartedNotification", {**context, "startedAtMs": 1, "item": {"type": "agentMessage", "id": "a1", "text": "", "phase": "final_answer"}}),
            ("item/agentMessage/delta", "AgentMessageDeltaNotification", {**context, "itemId": "a1", "delta": "哈哈"}),
            ("item/completed", "ItemCompletedNotification", {**context, "completedAtMs": 2, "item": {"type": "agentMessage", "id": "a1", "text": "哈哈，完成", "phase": "final_answer"}}),
        ]
        for method, schema_name, payload in events:
            Draft7Validator(self.schema(schema_name)).validate(payload)
            progress.receive(method, payload)
        snapshot = progress.snapshot({"thread_id": "thr-fixture", "turn_id": "turn-fixture", "api_session": "a", "generation_id": "codex-gen-1", "canonical_message_id": 1, "created_at": "2026-10-07T00:00:00Z"})
        self.assertEqual(snapshot["text"], "哈哈，完成")

    async def test_pinned_binary_and_emitted_generation_requests(self):
        self.assertEqual(importlib.metadata.version("openai-codex"), "0.160.0")
        self.assertEqual(self.version, "codex-cli 0.160.0")
        names = {
            "account/read": "GetAccountParams", "model/list": "ModelListParams",
            "config/read": "ConfigReadParams", "thread/start": "ThreadStartParams",
            "thread/resume": "ThreadResumeParams", "turn/start": "TurnStartParams",
            "thread/fork": "ThreadForkParams",
            "turn/interrupt": "TurnInterruptParams", "thread/unsubscribe": "ThreadUnsubscribeParams",
        }
        calls = []
        owner = self
        class Transport:
            async def request(self, method, params):
                schema = owner.schema(names[method])
                Draft7Validator(schema).validate(params)
                owner.assertFalse(set(params) - schema.get("properties", {}).keys(), method)
                calls.append((method, params))
                if method == "account/read": return {"account": {"type":"chatgpt"}}
                if method == "model/list": return {"data":[{"model":"fixture-model", "isDefault":True, "defaultReasoningEffort":"adaptive", "supportedReasoningEfforts":[{"reasoningEffort":"adaptive"}]}]}
                if method == "config/read": return {"config":{"mcp_servers":{"fixture":{"command":"never-launched"}}}}
                if method in {"thread/start", "thread/resume", "thread/fork"}:
                    return {"thread":{"id":"thr-fork" if method == "thread/fork" else "thr-fixture", "ephemeral":False, "historyMode":"paginated"}, "model":params["model"], "modelProvider":"openai", "cwd":params["cwd"], "sandbox":{"type":"readOnly"}, "approvalPolicy":"never", "initialTurnsPage":{"data":[]}}
                if method == "turn/start": return {"turn":{"id":"turn-fixture", "status":"inProgress"}}
                return {}
        transport = CodexGenerationHardeningTransport(Transport())
        protocol = CodexGenerationProtocol(CodexGenerationConfig(True, self.root / "workspace"), transport)
        models = await read_models(protocol._request)
        self.assertEqual(models[0]["reasoning_efforts"], ["adaptive"])
        thread = await protocol.start_thread(api_session="api-fixture", attempt_id="attempt-fixture", persona="test")
        await protocol.resume_thread(thread_id=thread.thread_id, model=thread.model, model_provider=thread.model_provider, reasoning_effort="adaptive", cwd=thread.cwd, persona="test")
        await protocol.start_turn(thread_id=thread.thread_id, client_message_id="msg-fixture", text="schema test only", model=thread.model, reasoning_effort="adaptive")
        for caption in ("", "look at this image"):
            image = ImageInput(self.root / "upload.png", "a" * 64, "image/png", 128)
            await protocol.start_turn(thread_id=thread.thread_id, client_message_id="msg-image", text=ImageMessageInput(caption, (image,)), model=thread.model, reasoning_effort="adaptive")
            self.assertEqual(calls[-1][1]["input"][-1], {"type":"localImage", "path":str(image.path)})
        await protocol.interrupt(thread_id=thread.thread_id, turn_id="turn-fixture")
        await protocol.unsubscribe(thread_id=thread.thread_id)
        fork = await protocol.fork_thread(thread_id=thread.thread_id, last_turn_id="turn-fixture", api_session="fork-fixture", attempt_id="fork-fixture", model=thread.model, model_provider=thread.model_provider, reasoning_effort="adaptive", persona="test")
        self.assertEqual(fork.thread_id, "thr-fork")
        for method, params in calls:
            if method in {"thread/start", "thread/resume", "thread/fork"}:
                self.assertEqual(params["config"]["model_reasoning_effort"], "adaptive")
                self.assertEqual(params["config"]["default_permissions"], ":read-only")
                self.assertEqual(params["config"]["cloud.skills.enabled"], False)
                self.assertEqual(params["config"]["mcp_servers"], {"fixture":{"enabled":False}})
                self.assertNotIn("effort", params)

    async def test_real_binary_initialize_account_and_flattened_config_without_login(self):
        # A separate empty home; no real account, login request or inference.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "home").mkdir(); (root / "workspace").mkdir()
            (root / "home" / "config.toml").write_text('default_permissions = ":read-only"\n[mcp_servers.fixture]\ncommand = "never-launched"\nenabled = false\n')
            runtime = CodexSharedAppServerRuntime(CodexSharedTransportConfig(True, root / "home", root / "workspace", 10), _parent_environment={"PATH":os.defpath})
            scope = runtime.scope(methods=frozenset({"account/read", "config/read"}))
            try:
                account = await scope.request("account/read", {"refreshToken":False})
                self.assertIsNone(account["account"])
                config = await scope.request("config/read", {"includeLayers":False, "cwd":str(root / "workspace")})
                self.assertIn("fixture", config["config"]["mcp_servers"])
                self.assertEqual(config["config"]["default_permissions"], ":read-only")
                Draft7Validator(self.schema("ConfigReadResponse")).validate(config)
            finally:
                await runtime.close()
