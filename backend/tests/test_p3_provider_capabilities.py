from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

from backend import p3_provider_status, web_provider_capabilities
from backend.tests._support import NoNetworkMixin, load_app, request


class WebProviderCapabilitiesContractTests(unittest.TestCase):
    def test_default_is_api_only_and_secret_free(self):
        payload = web_provider_capabilities.public_capabilities({})
        self.assertEqual(payload, {
            "ok": True,
            "contract_version": 1,
            "web_sessions": {
                "default_provider": "api",
                "provider_immutable": True,
                "providers": {
                    "api": {"create": True},
                    "codex": {"create": False, "text_only": True},
                },
            },
        })
        rendered = repr(payload).lower()
        for forbidden in ("secret", "token", "key", "account", "model"):
            self.assertNotIn(forbidden, rendered)

    def test_codex_create_requires_all_three_runtime_authorities(self):
        names = (
            web_provider_capabilities.CONTROL_FLAG,
            web_provider_capabilities.ENTRYPOINT_FLAG,
            web_provider_capabilities.GENERATION_FLAG,
        )
        for missing in names:
            env = {name: "true" for name in names}
            env[missing] = "false"
            with self.subTest(missing=missing):
                payload = web_provider_capabilities.public_capabilities(env)
                self.assertFalse(
                    payload["web_sessions"]["providers"]["codex"]["create"]
                )
        enabled = web_provider_capabilities.public_capabilities(
            {name: "true" for name in names}
        )
        self.assertTrue(enabled["web_sessions"]["providers"]["codex"]["create"])

    def test_every_gate_is_validated_even_when_codex_is_already_disabled(self):
        for name in (
            web_provider_capabilities.CONTROL_FLAG,
            web_provider_capabilities.ENTRYPOINT_FLAG,
            web_provider_capabilities.GENERATION_FLAG,
        ):
            env = {
                web_provider_capabilities.CONTROL_FLAG: "false",
                web_provider_capabilities.ENTRYPOINT_FLAG: "false",
                web_provider_capabilities.GENERATION_FLAG: "false",
                name: " true ",
            }
            with self.subTest(name=name), self.assertRaisesRegex(
                web_provider_capabilities.WebProviderCapabilitiesError,
                "web_provider_capabilities_unavailable",
            ):
                web_provider_capabilities.public_capabilities(env)


class P3ProviderStatusProjectionTests(unittest.TestCase):
    @staticmethod
    def capabilities(*, codex: bool) -> dict[str, object]:
        value = "true" if codex else "false"
        return web_provider_capabilities.public_capabilities({
            web_provider_capabilities.CONTROL_FLAG: value,
            web_provider_capabilities.ENTRYPOINT_FLAG: value,
            web_provider_capabilities.GENERATION_FLAG: value,
        })

    def test_active_provider_comes_only_from_durable_session_projection(self):
        projected = p3_provider_status.project_provider_status(
            {
                "active_session": "api-codex",
                "sessions": [
                    {"id": "api-api", "provider": "api", "title": "Codex-looking title"},
                    {"id": "api-codex", "provider": "codex", "title": "ordinary title"},
                ],
            },
            self.capabilities(codex=True),
        )
        self.assertEqual(projected["active_session"], "api-codex")
        self.assertEqual(projected["active_provider"], "codex")
        self.assertTrue(projected["web_sessions"]["providers"]["codex"]["create"])
        self.assertNotIn("generation_provider", projected)

    def test_empty_session_state_is_explicitly_providerless(self):
        projected = p3_provider_status.project_provider_status(
            {"active_session": "", "sessions": []},
            self.capabilities(codex=False),
        )
        self.assertIsNone(projected["active_session"])
        self.assertIsNone(projected["active_provider"])

    def test_inconsistent_or_malformed_authority_fails_closed(self):
        bad_states = [
            {"active_session": "missing", "sessions": []},
            {"active_session": "", "sessions": [{"id": "api-a", "provider": "api"}]},
            {"active_session": "api-a", "sessions": [{"id": "api-a", "provider": "other"}]},
            {
                "active_session": "api-a",
                "sessions": [
                    {"id": "api-a", "provider": "api"},
                    {"id": "api-a", "provider": "api"},
                ],
            },
        ]
        for state in bad_states:
            with self.subTest(state=state), self.assertRaisesRegex(
                p3_provider_status.P3ProviderStatusError,
                p3_provider_status.ERROR_CATEGORY,
            ):
                p3_provider_status.project_provider_status(
                    state,
                    self.capabilities(codex=True),
                )


class P3ProviderCapabilitiesRelayTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        load_app(self.temp.name, telegram=False)
        os.environ.update({
            "LEGACY_CHAT_BRIDGE_TOKEN": "test-legacy-bridge-token-1234567890",
            "LEGACY_CHAT_BRIDGE_SESSION": "legacy-test",
            web_provider_capabilities.CONTROL_FLAG: "false",
            web_provider_capabilities.ENTRYPOINT_FLAG: "false",
            web_provider_capabilities.GENERATION_FLAG: "false",
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

    async def test_route_requires_existing_relay_auth(self):
        response = await request(
            self.module,
            "GET",
            "/app/provider/capabilities",
        )
        self.assertEqual(response.status_code, 401)

    async def test_api_only_runtime_projects_codex_unavailable(self):
        response = await request(
            self.module,
            "GET",
            "/app/provider/capabilities",
            headers={"Authorization": "Bearer test-relay-secret"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["web_sessions"]["providers"]["api"]["create"])
        self.assertFalse(payload["web_sessions"]["providers"]["codex"]["create"])
        self.assertTrue(self.module.relay_app._P3_PROVIDER_CAPABILITY_INSTALLED)

    async def test_enabled_runtime_projects_codex_available_without_ui_inference(self):
        os.environ.update({
            web_provider_capabilities.CONTROL_FLAG: "true",
            web_provider_capabilities.ENTRYPOINT_FLAG: "true",
            web_provider_capabilities.GENERATION_FLAG: "true",
        })
        response = await request(
            self.module,
            "GET",
            "/app/provider/capabilities",
            headers={"Authorization": "Bearer test-relay-secret"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.json()["web_sessions"]["providers"]["codex"]["create"]
        )

    async def test_invalid_runtime_flag_returns_fixed_503(self):
        os.environ[web_provider_capabilities.GENERATION_FLAG] = " true "
        response = await request(
            self.module,
            "GET",
            "/app/provider/capabilities",
            headers={"Authorization": "Bearer test-relay-secret"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": "web_provider_capabilities_unavailable"},
        )

    async def test_status_route_requires_existing_relay_auth(self):
        response = await request(
            self.module,
            "GET",
            "/app/provider/status",
        )
        self.assertEqual(response.status_code, 401)

    async def test_status_route_is_authority_only_and_does_not_start_account_control(self):
        os.environ.update({
            web_provider_capabilities.CONTROL_FLAG: "true",
            web_provider_capabilities.ENTRYPOINT_FLAG: "true",
            web_provider_capabilities.GENERATION_FLAG: "true",
        })
        session_state = {
            "active_session": "api-codex",
            "sessions": [
                {"id": "api-api", "provider": "api"},
                {"id": "api-codex", "provider": "codex"},
            ],
        }
        with mock.patch.object(
            self.module.relay_app,
            "loop_json",
            return_value=session_state,
        ) as loop_json, mock.patch.object(
            self.module.relay_app,
            "provider_loop_json",
            side_effect=AssertionError("provider control must stay cold"),
        ) as provider_loop_json:
            response = await request(
                self.module,
                "GET",
                "/app/provider/status",
                headers={"Authorization": "Bearer test-relay-secret"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["active_session"], "api-codex")
        self.assertEqual(payload["active_provider"], "codex")
        self.assertNotIn("generation_provider", payload)
        loop_json.assert_called_once_with("/loop/sessions")
        provider_loop_json.assert_not_called()
        self.assertTrue(self.module.relay_app._P3_PROVIDER_STATUS_INSTALLED)

    async def test_status_route_collapses_malformed_authority_to_fixed_503(self):
        with mock.patch.object(
            self.module.relay_app,
            "loop_json",
            return_value={"active_session": "missing", "sessions": []},
        ):
            response = await request(
                self.module,
                "GET",
                "/app/provider/status",
                headers={"Authorization": "Bearer test-relay-secret"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"ok": False, "error": p3_provider_status.ERROR_CATEGORY},
        )


if __name__ == "__main__":
    unittest.main()
