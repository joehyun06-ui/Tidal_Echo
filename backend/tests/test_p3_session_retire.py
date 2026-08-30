from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException

from backend import p3_session_retire, web_session_delete
from backend.tests._support import NoNetworkMixin, load_app, request


class P3SessionRetireProjectionTests(unittest.TestCase):
    def test_busy_loop_error_maps_to_existing_delete_job_active_contract(self):
        with self.assertRaises(p3_session_retire.P3SessionRetireError) as raised:
            p3_session_retire.raise_loop_retire_error(
                409,
                json.dumps({
                    "ok": False,
                    "dispatch_uncertain": False,
                    "error": "codex_generation_session_busy",
                }),
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.category, web_session_delete.DELETE_JOB_ACTIVE)

    def test_success_requires_codex_row_and_shared_delete_authority(self):
        upstream = {
            "ok": True,
            "provider": "api",
            "retired": {"api_session": "api-codex", "status": "retired"},
        }
        state = {
            "sessions": [{
                "id": "api-codex",
                "provider": "codex",
                "delete_allowed": True,
            }]
        }
        self.assertEqual(
            p3_session_retire.project_retired(upstream, state, "api-codex"),
            {
                "ok": True,
                "retired": {
                    "id": "api-codex",
                    "provider": "codex",
                    "status": "retired",
                    "delete_allowed": True,
                },
            },
        )

    def test_success_fails_closed_if_delete_authority_not_yet_true(self):
        upstream = {
            "ok": True,
            "retired": {"api_session": "api-codex", "status": "retired"},
        }
        state = {
            "sessions": [{
                "id": "api-codex",
                "provider": "codex",
                "delete_allowed": False,
            }]
        }
        with self.assertRaisesRegex(
            p3_session_retire.P3SessionRetireError,
            p3_session_retire.RETIRE_UNAVAILABLE,
        ):
            p3_session_retire.project_retired(upstream, state, "api-codex")


class P3SessionRetireRelayTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        load_app(self.temp.name, telegram=False)
        os.environ.update({
            "LEGACY_CHAT_BRIDGE_TOKEN": "test-legacy-bridge-token-1234567890",
            "LEGACY_CHAT_BRIDGE_SESSION": "legacy-test",
            "CODEX_CONTROL_ENABLED": "false",
            "CODEX_CANARY_ENTRYPOINTS_ENABLED": "false",
            "CODEX_GENERATION_ENABLED": "false",
        })
        package = sys.modules.get("backend")
        for name in ("backend.p3_relay_app", "backend.legacy_chat_bridge_app"):
            sys.modules.pop(name, None)
            if package is not None:
                attr = name.rsplit(".", 1)[-1]
                if hasattr(package, attr):
                    delattr(package, attr)
        self.module = importlib.import_module("backend.p3_relay_app")
        self.addCleanup(sys.modules.pop, "backend.p3_relay_app", None)
        self.addCleanup(sys.modules.pop, "backend.legacy_chat_bridge_app", None)

    @staticmethod
    def codex_state(*, delete_allowed: bool) -> dict:
        return {
            "active_session": "api-codex",
            "sessions": [{
                "id": "api-codex",
                "provider": "codex",
                "delete_allowed": delete_allowed,
            }],
        }

    async def test_retire_route_requires_existing_relay_auth(self):
        response = await request(
            self.module,
            "POST",
            "/app/sessions/api-codex/retire",
        )
        self.assertEqual(response.status_code, 401)

    async def test_api_session_cannot_be_retired(self):
        api_state = {
            "active_session": "api-normal",
            "sessions": [{
                "id": "api-normal",
                "provider": "api",
                "delete_allowed": True,
            }],
        }
        with mock.patch.object(
            self.module.relay_app,
            "loop_json",
            return_value=api_state,
        ) as proxied:
            response = await request(
                self.module,
                "POST",
                "/app/sessions/api-normal/retire",
                headers={"Authorization": "Bearer test-relay-secret"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], p3_session_retire.RETIRE_FORBIDDEN)
        proxied.assert_called_once_with("/loop/sessions")

    async def test_busy_codex_session_stops_before_delete(self):
        busy = HTTPException(
            status_code=409,
            detail=json.dumps({
                "ok": False,
                "dispatch_uncertain": False,
                "error": "codex_generation_session_busy",
            }),
        )
        with mock.patch.object(
            self.module.relay_app,
            "loop_json",
            side_effect=[self.codex_state(delete_allowed=False), busy],
        ) as proxied:
            response = await request(
                self.module,
                "POST",
                "/app/sessions/api-codex/retire",
                headers={"Authorization": "Bearer test-relay-secret"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], web_session_delete.DELETE_JOB_ACTIVE)
        self.assertEqual(proxied.call_count, 2)
        self.assertEqual(
            proxied.call_args_list[1],
            mock.call("/loop/provider/canary/api-codex/retire", method="POST"),
        )

    async def test_retire_success_rechecks_delete_authority(self):
        upstream = {
            "ok": True,
            "provider": "api",
            "retired": {"api_session": "api-codex", "status": "retired"},
        }
        with mock.patch.object(
            self.module.relay_app,
            "loop_json",
            side_effect=[
                self.codex_state(delete_allowed=False),
                upstream,
                self.codex_state(delete_allowed=True),
            ],
        ) as proxied:
            response = await request(
                self.module,
                "POST",
                "/app/sessions/api-codex/retire",
                headers={"Authorization": "Bearer test-relay-secret"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True,
            "retired": {
                "id": "api-codex",
                "provider": "codex",
                "status": "retired",
                "delete_allowed": True,
            },
        })
        self.assertEqual(proxied.call_count, 3)
        self.assertEqual(proxied.call_args_list, [
            mock.call("/loop/sessions"),
            mock.call("/loop/provider/canary/api-codex/retire", method="POST"),
            mock.call("/loop/sessions"),
        ])

    async def test_retire_success_but_unconfirmed_delete_state_fails_closed(self):
        upstream = {
            "ok": True,
            "retired": {"api_session": "api-codex", "status": "retired"},
        }
        with mock.patch.object(
            self.module.relay_app,
            "loop_json",
            side_effect=[
                self.codex_state(delete_allowed=False),
                upstream,
                self.codex_state(delete_allowed=False),
            ],
        ):
            response = await request(
                self.module,
                "POST",
                "/app/sessions/api-codex/retire",
                headers={"Authorization": "Bearer test-relay-secret"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], p3_session_retire.RETIRE_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
