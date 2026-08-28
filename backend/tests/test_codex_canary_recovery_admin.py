from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException, Request

from backend import codex_canary_recovery_admin as admin
from backend import codex_generation_store as store


class RecoveryStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store_path = self.root / "codex-generation.db"
        self.cwd = str(self.root / "workspace" / "sessions" / "api-canary" / "attempt-1")
        Path(self.cwd).mkdir(parents=True)
        store.initialize(self.store_path)
        store.pin_session(
            self.store_path,
            api_session="api-canary",
            model="gpt-5.6-sol",
            model_provider="openai",
            reasoning_effort="low",
            persona_hash="a" * 64,
        )
        job = store.enqueue_job(
            self.store_path,
            api_session="api-canary",
            canonical_message_id=41,
            input_digest="b" * 64,
            generation_id="codex-gen-41",
            client_message_id="codex-client-41",
            callback_identity="codex-callback-41",
        )
        claimed = store.claim_next_job(self.store_path)
        self.assertEqual(claimed["id"], job["id"])
        store.begin_thread_dispatch(
            self.store_path,
            job_id=job["id"],
            thread_attempt_id="attempt-1",
            cwd=self.cwd,
        )
        store.bind_session_thread(
            self.store_path,
            job_id=job["id"],
            thread_attempt_id="attempt-1",
            thread_id="thr-1",
            cwd=self.cwd,
        )
        store.begin_turn_dispatch(self.store_path, job_id=job["id"])
        store.record_turn_started(self.store_path, job_id=job["id"], turn_id="turn-1")
        store.mark_failed(
            self.store_path,
            job_id=job["id"],
            category="codex_generation_empty_response",
        )
        self.job_id = job["id"]

    def arm(self):
        with (
            patch.object(admin, "_store_path", return_value=self.store_path),
            patch.dict(os.environ, {"CODEX_GENERATION_ENABLED": "false"}, clear=False),
        ):
            return admin._arm_existing_completion_recovery("api-canary")

    def test_arms_only_existing_failed_turn_and_never_changes_attempt_count(self):
        payload = self.arm()
        self.assertEqual(payload["recovery"]["status"], "armed")
        self.assertEqual(payload["recovery"]["attempt_count"], 1)
        job = store.get_job(self.store_path, self.job_id)
        self.assertEqual(job["status"], "dispatch_uncertain")
        self.assertEqual(job["attempt_count"], 1)
        self.assertEqual(job["recovery_count"], 0)
        self.assertEqual(job["turn_id"], "turn-1")
        self.assertIsNone(job["assistant_message_id"])
        self.assertEqual(job["error_category"], "codex_generation_recovery_armed")

    def test_arm_is_idempotent_while_frozen(self):
        first = self.arm()
        second = self.arm()
        self.assertEqual(first, second)
        job = store.get_job(self.store_path, self.job_id)
        self.assertEqual(job["attempt_count"], 1)
        self.assertEqual(job["status"], "dispatch_uncertain")

    def test_recovery_requires_generation_off(self):
        with (
            patch.object(admin, "_store_path", return_value=self.store_path),
            patch.dict(os.environ, {"CODEX_GENERATION_ENABLED": "true"}, clear=False),
            self.assertRaises(admin.CodexCanaryRecoveryAdminError) as raised,
        ):
            admin._arm_existing_completion_recovery("api-canary")
        self.assertEqual(
            raised.exception.category,
            "codex_canary_recovery_requires_disabled_generation",
        )
        self.assertEqual(store.get_job(self.store_path, self.job_id)["status"], "failed")

    def test_wrong_failure_category_is_not_eligible(self):
        store.mark_failed(self.store_path, job_id=self.job_id, category="different_failure")
        with (
            patch.object(admin, "_store_path", return_value=self.store_path),
            patch.dict(os.environ, {"CODEX_GENERATION_ENABLED": "false"}, clear=False),
            self.assertRaises(admin.CodexCanaryRecoveryAdminError) as raised,
        ):
            admin._arm_existing_completion_recovery("api-canary")
        self.assertEqual(raised.exception.category, "codex_canary_recovery_not_eligible")


class RecoveryRouteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = FastAPI()

        def check_auth(request: Request):
            if request.headers.get("authorization") != "Bearer test-secret":
                raise HTTPException(status_code=401, detail="unauthorized")

        self.relay = SimpleNamespace(app=app, check_auth=check_auth)
        admin.install(self.relay)
        self.transport = httpx.ASGITransport(app=app)

    async def request(self, **kwargs):
        async with httpx.AsyncClient(transport=self.transport, base_url="http://test") as client:
            return await client.request(**kwargs)

    async def test_auth_is_required_before_recovery(self):
        with patch.object(admin, "_arm_existing_completion_recovery") as arm:
            response = await self.request(
                method="POST",
                url="/provider/canary/api-canary/recover-existing",
            )
        self.assertEqual(response.status_code, 401)
        arm.assert_not_called()

    async def test_route_returns_only_bounded_recovery_state(self):
        payload = {
            "ok": True,
            "provider": "codex",
            "recovery": {
                "api_session": "api-canary",
                "status": "armed",
                "attempt_count": 1,
                "recovery_count": 0,
                "turn_bound": True,
                "assistant_message_bound": False,
            },
        }
        with patch.object(admin, "_arm_existing_completion_recovery", return_value=payload) as arm:
            response = await self.request(
                method="POST",
                url="/provider/canary/api-canary/recover-existing",
                headers={"Authorization": "Bearer test-secret"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        arm.assert_called_once_with("api-canary")
        self.assertNotIn("thread_id", response.text)
        self.assertNotIn("turn_id", response.text)
        self.assertNotIn("callback_identity", response.text)

    def test_install_is_idempotent(self):
        before = [(getattr(route, "path", None), tuple(sorted(getattr(route, "methods", set())))) for route in self.relay.app.routes]
        admin.install(self.relay)
        after = [(getattr(route, "path", None), tuple(sorted(getattr(route, "methods", set())))) for route in self.relay.app.routes]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
