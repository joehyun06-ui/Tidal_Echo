from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import codex_app_server_control as control
from backend.deployment_config import CodexControlConfig


FAKE_SERVER = Path(__file__).with_name("_fake_codex_app_server.py")


class ControlTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "codex-home"
        self.workspace = self.root / "codex-workspace"
        self.home.mkdir()
        self.workspace.mkdir()
        self.controls: list[control.CodexAppServerControl] = []

    async def asyncTearDown(self):
        for instance in self.controls:
            await instance.close()
        self.temporary.cleanup()

    def config(self, *, enabled: bool = True, timeout: float = 1) -> CodexControlConfig:
        return CodexControlConfig(enabled, self.home, self.workspace, timeout)

    def make_control(
        self,
        scenario: str | tuple[str, ...] = "normal",
        *,
        timeout: float = 1,
        parent: dict[str, str] | None = None,
        captured: dict | None = None,
    ) -> control.CodexAppServerControl:
        scenarios = (scenario,) if isinstance(scenario, str) else scenario
        if not scenarios:
            raise ValueError
        transcript = self.root / f"control-{len(self.controls)}.jsonl"
        launch_state = {"count": 0, "owners": []}

        async def launcher(*command, **kwargs):
            launch_index = launch_state["count"]
            launch_state["count"] += 1
            launch_scenario = scenarios[min(launch_index, len(scenarios) - 1)]
            if captured is not None:
                captured["command"] = command
                captured["env"] = dict(kwargs["env"])
                captured["stderr"] = kwargs["stderr"]
            owner = await asyncio.create_subprocess_exec(
                sys.executable,
                str(FAKE_SERVER),
                "--scenario", launch_scenario,
                "--transcript", str(transcript),
                stdin=kwargs["stdin"],
                stdout=kwargs["stdout"],
                stderr=kwargs["stderr"],
                cwd=kwargs["cwd"],
                env=kwargs["env"],
                limit=kwargs["limit"],
            )
            launch_state["owners"].append(owner)
            return owner

        instance = control.CodexAppServerControl(
            self.config(timeout=timeout),
            _runtime_resolver=lambda: "pinned-codex",
            _process_launcher=launcher,
            _parent_environment=parent or {"PATH": os.environ.get("PATH", "")},
        )
        instance.test_transcript = transcript
        instance.test_launch_state = launch_state
        self.controls.append(instance)
        return instance

    async def transcript(self, instance) -> list[dict]:
        for _ in range(100):
            path = instance.test_transcript
            if path.exists():
                try:
                    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                except (OSError, json.JSONDecodeError):
                    pass
            await asyncio.sleep(0.01)
        return []

    async def wait_for_transcript(self, instance, predicate) -> list[dict]:
        messages: list[dict] = []
        for _ in range(200):
            path = instance.test_transcript
            if path.exists():
                try:
                    messages = [
                        json.loads(line)
                        for line in path.read_text(encoding="utf-8").splitlines()
                    ]
                except (OSError, json.JSONDecodeError):
                    messages = []
                if predicate(messages):
                    return messages
            await asyncio.sleep(0.01)
        return messages

    async def test_disabled_mode_resolves_and_launches_nothing(self):
        called = False

        def resolver():
            nonlocal called
            called = True
            raise AssertionError

        instance = control.CodexAppServerControl(
            self.config(enabled=False), _runtime_resolver=resolver
        )
        self.controls.append(instance)
        with self.assertRaisesRegex(control.CodexControlError, "codex_control_disabled"):
            await instance.status()
        self.assertFalse(called)
        self.assertFalse(self.home.joinpath("auth.json").exists())

    async def test_runtime_resolution_failure_is_data_free(self):
        def resolver():
            raise RuntimeError("secret-runtime-path")

        instance = control.CodexAppServerControl(
            self.config(), _runtime_resolver=resolver
        )
        self.controls.append(instance)
        with self.assertRaises(control.CodexControlError) as raised:
            await instance.status()
        self.assertEqual(raised.exception.category, "codex_app_server_unavailable")
        self.assertNotIn("secret", str(raised.exception))

    async def test_child_environment_is_allowlisted_and_secret_free(self):
        captured: dict = {}
        parent = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "LLM_API_KEY": "model-sentinel",
            "RELAY_SECRET": "relay-sentinel",
            "API_LOOP_INTERNAL_TOKEN": "loop-sentinel",
            "TELEGRAM_BOT_TOKEN": "telegram-sentinel",
            "OPENAI_API_KEY": "openai-sentinel",
        }
        instance = self.make_control(parent=parent, captured=captured)
        await instance.status()
        self.assertEqual(
            set(captured["env"]), {"PATH", "LANG", "CODEX_HOME", "HOME", "RUST_LOG"}
        )
        self.assertEqual(captured["env"]["CODEX_HOME"], str(self.home))
        self.assertEqual(captured["env"]["HOME"], str(self.home))
        self.assertEqual(captured["env"]["RUST_LOG"], "warn")
        self.assertEqual(captured["stderr"], asyncio.subprocess.DEVNULL)
        self.assertFalse(any("sentinel" in value for value in captured["env"].values()))

    async def test_command_is_read_only_stdio_control_plane(self):
        captured: dict = {}
        instance = self.make_control(captured=captured)
        await instance.status()
        self.assertEqual(captured["command"], (
            "pinned-codex",
            "--config", 'approval_policy="never"',
            "--config", 'sandbox_mode="read-only"',
            "--config", "features.plugins=false",
            "--config", "features.web_search_request=false",
            "app-server", "--listen", "stdio://",
        ))

    async def test_initialize_then_initialized_sequence_is_exact(self):
        instance = self.make_control()
        await instance.status()
        messages = await self.transcript(instance)
        self.assertEqual(messages[0]["method"], "initialize")
        self.assertTrue(all("jsonrpc" not in message for message in messages))
        self.assertEqual(messages[1], {"method": "initialized", "params": {}})
        self.assertEqual(messages[2]["method"], "account/read")
        self.assertEqual(messages[2]["params"], {"refreshToken": False})

    async def test_concurrent_cold_start_waits_for_initialized_barrier(self):
        instance = self.make_control("delayed_initialize", timeout=2)
        status_task = asyncio.create_task(instance.status())
        usage_task = asyncio.create_task(instance.usage())
        status, usage = await asyncio.gather(status_task, usage_task)
        self.assertTrue(status["connected"])
        self.assertEqual(usage["lifetime_tokens"], 1234)
        messages = await self.wait_for_transcript(
            instance,
            lambda items: sum(
                item.get("method", "").startswith("account/") for item in items
            ) >= 4,
        )
        methods = [message.get("method") for message in messages]
        self.assertEqual(methods.count("initialize"), 1)
        self.assertEqual(methods[0:2], ["initialize", "initialized"])
        self.assertTrue(all(
            index > 1
            for index, method in enumerate(methods)
            if isinstance(method, str) and method.startswith("account/")
        ))
        self.assertEqual(instance.test_launch_state["count"], 1)

    async def test_concurrent_startup_failure_is_shared_and_data_free(self):
        instance = self.make_control("delayed_initialize", timeout=0.1)
        outcomes = await asyncio.gather(
            instance.status(), instance.usage(), return_exceptions=True
        )
        self.assertTrue(all(isinstance(item, control.CodexControlError) for item in outcomes))
        self.assertEqual(
            {item.category for item in outcomes}, {"codex_app_server_timeout"}
        )
        self.assertEqual(instance.test_launch_state["count"], 1)
        messages = await self.transcript(instance)
        self.assertEqual(
            [message.get("method") for message in messages], ["initialize"]
        )
        self.assertNotIn("secret", " ".join(str(item) for item in outcomes))

    async def test_account_and_rate_limits_are_sanitized(self):
        result = await self.make_control().status()
        self.assertEqual(result["connected"], True)
        self.assertEqual(result["account_type"], "chatgpt")
        self.assertEqual(result["plan_type"], "plus")
        serialized = json.dumps(result)
        for forbidden in (
            "accountId", "accessToken", "refreshToken", "unknownSecret",
            "PRIVATE-BALANCE-SENTINEL", "hasCredits", "unlimited", "balance",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(result["rate_limits"][0]["limit_id"], "primary")
        self.assertEqual(result["rate_limits"][0]["used_percent"], 12.5)
        self.assertEqual(result["rate_limits"][1]["limit_id"], "secondary")
        self.assertNotIn("credits", result["rate_limits"][0])
        self.assertEqual(result["reset_credit_count"], 2)

    def test_official_credits_snapshot_is_ignored_without_losing_limits(self):
        result = control.sanitize_rate_limits({
            "rateLimits": {
                "limitId": "primary",
                "primary": {"usedPercent": 25, "windowDurationMins": 15},
                "credits": {
                    "hasCredits": True,
                    "unlimited": False,
                    "balance": "PRIVATE-BALANCE-SENTINEL",
                },
            },
            "rateLimitResetCredits": {"availableCount": 7, "credits": []},
        })
        self.assertEqual(result["rate_limits"], [{
            "limit_id": "primary",
            "used_percent": 25,
            "window_duration_mins": 15,
        }])
        self.assertEqual(result["reset_credit_count"], 7)
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("PRIVATE-BALANCE-SENTINEL", serialized)
        self.assertNotIn('"credits"', serialized)

    async def test_usage_is_bounded_and_drops_unknown_fields(self):
        result = await self.make_control().usage()
        self.assertEqual(result["lifetime_tokens"], 1234)
        self.assertEqual(result["daily_usage_buckets"], [
            {"start_date": "2026-08-26", "tokens": 25}
        ])
        self.assertNotIn("rawAccount", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    async def test_device_login_exact_contract_and_internal_id(self):
        instance = self.make_control()
        result = await instance.login_start()
        self.assertEqual(set(result), {"verification_url", "user_code", "status"})
        self.assertEqual(result["status"], "pending")
        self.assertNotIn("login", json.dumps(result).casefold().replace("login_unavailable", ""))
        messages = await self.transcript(instance)
        start = next(message for message in messages if message.get("method") == "account/login/start")
        self.assertEqual(start["params"], {"type": "chatgptDeviceCode"})
        self.assertNotIn("internal-login-id", json.dumps(result))

    async def test_duplicate_concurrent_login_is_rejected(self):
        instance = self.make_control("delay", timeout=3)
        first = asyncio.create_task(instance.login_start())
        await asyncio.sleep(0.05)
        with self.assertRaises(control.CodexControlError) as raised:
            await instance.login_start()
        self.assertEqual(raised.exception.category, "codex_login_in_progress")
        self.assertEqual((await first)["status"], "pending")

    async def test_canceled_status_uses_retained_login_id_and_clears_it(self):
        instance = self.make_control()
        await instance.login_start()
        self.assertEqual(await instance.login_cancel(), {"cancelled": True})
        with self.assertRaises(control.CodexControlError) as raised:
            await instance.login_cancel()
        self.assertEqual(raised.exception.category, "codex_login_unavailable")
        messages = await self.transcript(instance)
        cancel = next(message for message in messages if message.get("method") == "account/login/cancel")
        self.assertEqual(cancel["params"], {"loginId": "internal-login-id"})

    async def test_not_found_cancel_status_is_terminal_and_clears_local_state(self):
        instance = self.make_control("cancel_not_found")
        await instance.login_start()
        self.assertEqual(await instance.login_cancel(), {"cancelled": True})
        with self.assertRaises(control.CodexControlError) as raised:
            await instance.login_cancel()
        self.assertEqual(raised.exception.category, "codex_login_unavailable")

    async def test_cancelled_spelling_is_rejected_as_non_protocol(self):
        instance = self.make_control("cancel_legacy_spelling")
        await instance.login_start()
        with self.assertRaises(control.CodexControlError) as raised:
            await instance.login_cancel()
        self.assertEqual(raised.exception.category, "codex_login_unavailable")

    async def test_secret_bearing_cancel_status_is_rejected_data_free(self):
        instance = self.make_control("cancel_secret_status")
        await instance.login_start()
        with self.assertRaises(control.CodexControlError) as raised:
            await instance.login_cancel()
        self.assertEqual(raised.exception.category, "codex_login_unavailable")
        self.assertNotIn("PRIVATE-CANCEL-STATUS-SENTINEL", str(raised.exception))
        self.assertNotIn("PRIVATE-CANCEL-STATUS-SENTINEL", repr(raised.exception))

    async def test_logout_is_bounded(self):
        self.assertEqual(await self.make_control().logout(), {"logged_out": True})

    async def test_non_p1_method_is_rejected_before_write(self):
        instance = self.make_control()
        for method in ("thread/start", "turn/start", "fs/read", "tool/call", "mcp/list"):
            with self.subTest(method=method), self.assertRaises(control.CodexControlError) as raised:
                await instance._request(method)
            self.assertEqual(raised.exception.category, "codex_app_server_protocol_error")
        messages = await self.transcript(instance)
        self.assertFalse(any(message.get("method", "").startswith(("thread/", "turn/", "fs/", "tool/", "mcp/")) for message in messages))

    async def test_server_request_is_answered_method_not_supported(self):
        instance = self.make_control("server_request")
        await instance.status()
        messages = await self.transcript(instance)
        response = next(message for message in messages if message.get("id") == 990 and "error" in message)
        self.assertEqual(response["error"]["code"], -32601)
        self.assertNotIn("result", response)

    async def test_process_exit_fails_data_free(self):
        instance = self.make_control("exit")
        with self.assertRaises(control.CodexControlError) as raised:
            await instance.status()
        self.assertEqual(raised.exception.category, "codex_app_server_unavailable")

    async def test_malformed_json_fails_closed(self):
        instance = self.make_control("malformed")
        with self.assertRaises(control.CodexControlError) as raised:
            await instance.status()
        self.assertEqual(raised.exception.category, "codex_app_server_protocol_error")

    async def test_oversized_jsonl_fails_closed(self):
        instance = self.make_control("oversized")
        with self.assertRaises(control.CodexControlError) as raised:
            await instance.status()
        self.assertEqual(raised.exception.category, "codex_app_server_protocol_error")

    async def test_request_timeout_is_fixed_and_data_free(self):
        instance = self.make_control("delay", timeout=0.25)
        with self.assertRaises(control.CodexControlError) as raised:
            await instance.status()
        self.assertEqual(raised.exception.category, "codex_app_server_timeout")

    async def test_timed_out_process_is_replaced_only_by_next_operation(self):
        instance = self.make_control(("delay", "normal"), timeout=0.25)
        with self.assertRaises(control.CodexControlError) as raised:
            await instance.status()
        self.assertEqual(raised.exception.category, "codex_app_server_timeout")
        self.assertIsNone(instance._process)
        self.assertEqual(instance.test_launch_state["count"], 1)
        self.assertTrue((await instance.status())["connected"])
        self.assertEqual(instance.test_launch_state["count"], 2)
        self.assertIsNot(
            instance.test_launch_state["owners"][0],
            instance.test_launch_state["owners"][1],
        )

    async def test_cancelled_written_request_taints_and_resets_owner(self):
        instance = self.make_control(("delay", "normal"), timeout=3)
        await instance._ensure_started()
        old_owner = instance._process
        request = asyncio.create_task(instance._request(
            "account/read", {"refreshToken": False}
        ))
        await self.wait_for_transcript(
            instance,
            lambda items: any(
                item.get("method") == "account/read" for item in items
            ),
        )
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertIsNone(instance._process)
        self.assertIsNotNone(old_owner)
        self.assertTrue((await instance.status())["connected"])
        self.assertEqual(instance.test_launch_state["count"], 2)

    async def test_late_old_owner_response_cannot_affect_replacement(self):
        instance = self.make_control(("delay", "short_delay"), timeout=3)
        await instance._ensure_started()
        old_owner = instance._process
        abandoned = asyncio.create_task(instance._request(
            "account/read", {"refreshToken": False}
        ))
        await self.wait_for_transcript(
            instance,
            lambda items: any(
                item.get("method") == "account/read" for item in items
            ),
        )
        abandoned.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await abandoned

        replacement = asyncio.create_task(instance._request(
            "account/read", {"refreshToken": False}
        ))
        replacement_owner = None
        replacement_id = None
        for _ in range(100):
            replacement_owner = instance._process
            candidates = [
                request_id
                for request_id, (owner, _future) in instance._pending.items()
                if owner is replacement_owner
            ]
            if replacement_owner is not None and candidates:
                replacement_id = candidates[0]
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(replacement_id)
        self.assertIsNot(replacement_owner, old_owner)
        await instance._consume_message(
            {"id": replacement_id, "result": {
                "account": {"type": "apiKey", "secret": "old-owner-sentinel"}
            }},
            owner=old_owner,
        )
        result = await replacement
        self.assertEqual(result["account"]["type"], "chatgpt")
        self.assertNotIn("old-owner-sentinel", json.dumps(result))

    async def test_taint_fails_other_pending_request_data_free(self):
        instance = self.make_control("delay", timeout=0.25)
        await instance._ensure_started()
        first = asyncio.create_task(instance._request(
            "account/read", {"refreshToken": False}
        ))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(instance._request("account/usage/read"))
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(all(isinstance(item, control.CodexControlError) for item in outcomes))
        self.assertEqual(
            sorted(item.category for item in outcomes),
            ["codex_app_server_timeout", "codex_app_server_unavailable"],
        )
        self.assertIsNone(instance._process)

    async def test_teardown_wait_and_pipe_close_are_bounded(self):
        class StallingStdin:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            async def wait_closed(self):
                await asyncio.Event().wait()

        class StallingProcess:
            def __init__(self):
                self.returncode = None
                self.stdin = StallingStdin()
                self.killed = False

            def kill(self):
                self.killed = True

            async def wait(self):
                await asyncio.Event().wait()

        instance = control.CodexAppServerControl(
            self.config(), _runtime_resolver=lambda: "unused"
        )
        self.controls.append(instance)
        process = StallingProcess()
        instance._process = process
        instance._ready_process = process
        started = asyncio.get_running_loop().time()
        with mock.patch.object(
            control, "PROCESS_TEARDOWN_STEP_TIMEOUT_SECONDS", 0.02
        ):
            await instance._fail_process("codex_app_server_unavailable")
        elapsed = asyncio.get_running_loop().time() - started
        self.assertLess(elapsed, 0.2)
        self.assertTrue(process.killed)
        self.assertTrue(process.stdin.closed)
        self.assertIsNone(instance._process)

    def test_malformed_usage_and_limits_are_rejected_without_repr(self):
        hostile = object()
        with self.assertRaises(ValueError):
            control.sanitize_usage({"usage": {"lifetimeTokens": hostile}})
        with self.assertRaises(ValueError):
            control.sanitize_rate_limits({"rateLimits": [hostile]})
        with self.assertRaises(ValueError):
            control.sanitize_rate_limits({
                "rateLimits": {"limitId": "primary", "primary": {"usedPercent": "secret"}}
            })

    def test_unknown_account_type_is_not_connected(self):
        result = control.sanitize_account({
            "account": {"type": "apiKey", "planType": "enterprise"},
            "requiresOpenaiAuth": False,
        })
        self.assertEqual(result, {
            "connected": False, "account_type": "", "plan_type": "unknown",
            "requires_openai_auth": True,
        })


if __name__ == "__main__":
    unittest.main()
