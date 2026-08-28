from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend import codex_app_server_control as control
from backend.deployment_config import CodexControlConfig


class _StubControl(control.CodexAppServerControl):
    def __init__(self, root: Path) -> None:
        super().__init__(CodexControlConfig(True, root / "home", root / "workspace", 1))
        self.connected = False
        self.race_status: bool | None = None
        self.owner = type("Owner", (), {"returncode": None})()
        self._process = self.owner

    async def _request(self, method: str, params: dict[str, object] | None = None) -> object:
        if method == "account/read":
            if self.connected:
                return {
                    "account": {"type": "chatgpt", "planType": "plus"},
                    "requiresOpenaiAuth": False,
                }
            return {"account": None, "requiresOpenaiAuth": True}
        if method == "account/rateLimits/read":
            return {}
        if method == "account/login/start":
            if self.race_status is not None:
                await self._consume_message({
                    "method": "account/login/completed",
                    "params": {
                        "loginId": "login-1",
                        "success": self.race_status,
                        "error": "PRIVATE-RACED-ERROR-SENTINEL",
                    },
                }, owner=self.owner)
            return {
                "type": "chatgptDeviceCode",
                "loginId": "login-1",
                "verificationUrl": "https://example.invalid/device",
                "userCode": "ABCD-EFGH",
            }
        if method == "account/login/cancel":
            return {"status": "canceled"}
        if method == "account/logout":
            return {}
        raise AssertionError(method)

    async def close(self) -> None:
        self._closed = True


class CodexLoginCompletionStatusTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.joinpath("home").mkdir()
        self.root.joinpath("workspace").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_control(self) -> _StubControl:
        return _StubControl(self.root)

    async def test_initial_disconnected_status_is_idle(self):
        result = await self.make_control().status()
        self.assertFalse(result["connected"])
        self.assertEqual(result["login_status"], "idle")

    async def test_pending_then_failed_completion_is_data_free(self):
        instance = self.make_control()
        started = await instance.login_start()
        self.assertEqual(started["status"], "pending")
        self.assertEqual((await instance.status())["login_status"], "pending")

        await instance._consume_message({
            "method": "account/login/completed",
            "params": {
                "loginId": "login-1",
                "success": False,
                "error": "PRIVATE-UPSTREAM-AUTH-ERROR-SENTINEL",
            },
        }, owner=instance.owner)

        result = await instance.status()
        self.assertFalse(result["connected"])
        self.assertEqual(result["login_status"], "failed")
        self.assertEqual(instance._login_id, "")
        encoded = json.dumps(result) + repr(instance.__dict__)
        self.assertNotIn("PRIVATE-UPSTREAM-AUTH-ERROR-SENTINEL", encoded)

    async def test_success_completion_and_persisted_account_report_succeeded(self):
        instance = self.make_control()
        await instance.login_start()
        await instance._consume_message({
            "method": "account/login/completed",
            "params": {"loginId": "login-1", "success": True, "error": None},
        }, owner=instance.owner)
        self.assertEqual((await instance.status())["login_status"], "succeeded")

        fresh = self.make_control()
        fresh.connected = True
        result = await fresh.status()
        self.assertTrue(result["connected"])
        self.assertEqual(result["login_status"], "succeeded")

    async def test_completion_racing_login_start_response_keeps_result(self):
        for success, expected in ((True, "succeeded"), (False, "failed")):
            with self.subTest(success=success):
                instance = self.make_control()
                instance.race_status = success
                response = await instance.login_start()
                self.assertEqual(response["status"], "pending")
                self.assertEqual(instance._login_id, "")
                self.assertEqual((await instance.status())["login_status"], expected)
                self.assertNotIn("PRIVATE-RACED-ERROR-SENTINEL", repr(instance.__dict__))

    async def test_malformed_completion_does_not_clear_pending_login(self):
        instance = self.make_control()
        await instance.login_start()
        await instance._consume_message({
            "method": "account/login/completed",
            "params": {
                "loginId": "login-1",
                "success": "false",
                "error": "PRIVATE-MALFORMED-SENTINEL",
            },
        }, owner=instance.owner)
        self.assertEqual(instance._login_id, "login-1")
        self.assertEqual((await instance.status())["login_status"], "pending")
        self.assertNotIn("PRIVATE-MALFORMED-SENTINEL", repr(instance.__dict__))

    async def test_cancel_and_logout_expose_only_fixed_statuses(self):
        instance = self.make_control()
        await instance.login_start()
        self.assertEqual(await instance.login_cancel(), {"cancelled": True})
        self.assertEqual((await instance.status())["login_status"], "cancelled")
        self.assertEqual(await instance.logout(), {"logged_out": True})
        self.assertEqual((await instance.status())["login_status"], "idle")


if __name__ == "__main__":
    unittest.main()
