from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import web_session_delete
from backend import web_session_provider_authority
from backend.tests._support import NoNetworkMixin, load_app, request


class FakeLegacy:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.saved = []

    def load_config(self):
        cfg = dict(self.cfg)
        cfg["sessions"] = [dict(row) for row in cfg.get("sessions", [])]
        return cfg

    def save_config(self, cfg):
        self.cfg = dict(cfg)
        self.cfg["sessions"] = [dict(row) for row in cfg.get("sessions", [])]
        self.saved.append(self.load_config())

    def now_iso(self):
        return "2026-08-30T00:00:00+00:00"


def row(session_id: str, provider: str = "api"):
    return {
        "id": session_id,
        "title": session_id,
        "since_id": 0,
        "created_at": "2026-08-30T00:00:00+00:00",
        "pinned": False,
        "provider": provider,
    }


class WebSessionDeleteAuthorityTests(unittest.TestCase):
    def test_delete_active_api_selects_newest_remaining_api_not_codex(self):
        legacy = FakeLegacy({
            "sessions": [
                row("api-first"),
                row("api-codex-history", "codex"),
                row("api-active"),
            ],
            "active_session": "api-active",
        })
        authority = web_session_provider_authority.WebSessionProviderAuthority(legacy)

        result = web_session_delete.delete_api_session(authority, "api-active")

        self.assertEqual(result["active_session"], "api-first")
        self.assertEqual(
            [item["id"] for item in result["sessions"]],
            ["api-first", "api-codex-history"],
        )
        self.assertEqual(result["deleted"], {
            "id": "api-active",
            "provider": "api",
            "messages_deleted": False,
        })
        self.assertEqual(legacy.cfg["active_session"], "api-first")

    def test_delete_non_active_api_preserves_active_pointer(self):
        legacy = FakeLegacy({
            "sessions": [row("api-old"), row("api-active")],
            "active_session": "api-active",
        })
        authority = web_session_provider_authority.WebSessionProviderAuthority(legacy)
        result = web_session_delete.delete_api_session(authority, "api-old")
        self.assertEqual(result["active_session"], "api-active")
        self.assertEqual([item["id"] for item in result["sessions"]], ["api-active"])

    def test_delete_last_api_clears_active_pointer(self):
        legacy = FakeLegacy({
            "sessions": [row("api-only")],
            "active_session": "api-only",
        })
        authority = web_session_provider_authority.WebSessionProviderAuthority(legacy)
        result = web_session_delete.delete_api_session(authority, "api-only")
        self.assertEqual(result["sessions"], [])
        self.assertEqual(result["active_session"], "")
        self.assertEqual(legacy.cfg["active_session"], "")

    def test_codex_authority_row_is_never_deleted(self):
        legacy = FakeLegacy({
            "sessions": [row("api-codex", "codex")],
            "active_session": "api-codex",
        })
        authority = web_session_provider_authority.WebSessionProviderAuthority(legacy)
        before = legacy.load_config()
        with self.assertRaisesRegex(
            web_session_provider_authority.WebSessionProviderAuthorityError,
            web_session_delete.DELETE_FORBIDDEN,
        ):
            web_session_delete.delete_api_session(authority, "api-codex")
        self.assertEqual(legacy.load_config(), before)
        self.assertEqual(legacy.saved, [])

    def test_historical_codex_row_without_provider_is_never_deleted(self):
        legacy = FakeLegacy({
            "sessions": [{
                "id": "api-historical-codex",
                "title": "old",
                "since_id": 0,
                "created_at": "2026-08-30T00:00:00+00:00",
            }]
        })
        authority = web_session_provider_authority.WebSessionProviderAuthority(
            legacy,
            historical_provider=lambda sid: "codex" if sid == "api-historical-codex" else None,
        )
        with self.assertRaisesRegex(
            web_session_provider_authority.WebSessionProviderAuthorityError,
            web_session_delete.DELETE_FORBIDDEN,
        ):
            web_session_delete.delete_api_session(authority, "api-historical-codex")
        self.assertEqual(len(legacy.cfg["sessions"]), 1)

    def test_missing_row_is_404_category(self):
        authority = web_session_provider_authority.WebSessionProviderAuthority(FakeLegacy())
        with self.assertRaisesRegex(
            web_session_provider_authority.WebSessionProviderAuthorityError,
            "web_session_not_found",
        ):
            web_session_delete.delete_api_session(authority, "api-missing")


class P3SessionDeleteRelayTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
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

    async def test_delete_route_requires_existing_relay_auth(self):
        response = await request(
            self.module,
            "DELETE",
            "/app/sessions/api-test",
        )
        self.assertEqual(response.status_code, 401)

    async def test_delete_route_proxies_exact_session_and_method(self):
        with mock.patch.object(
            self.module.relay_app,
            "loop_json",
            return_value={
                "active_session": "api-next",
                "sessions": [],
                "deleted": {
                    "id": "api-test",
                    "provider": "api",
                    "messages_deleted": False,
                },
            },
        ) as proxied:
            response = await request(
                self.module,
                "DELETE",
                "/app/sessions/api-test",
                headers={"Authorization": "Bearer test-relay-secret"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"]["id"], "api-test")
        proxied.assert_called_once_with(
            "/loop/sessions/api-test",
            method="DELETE",
        )
        self.assertTrue(self.module.relay_app._P3_SESSION_DELETE_INSTALLED)


class ProviderGuardDeleteContractTests(unittest.TestCase):
    def test_guard_exposes_delete_and_maps_forbidden_to_conflict(self):
        source = Path("examples/api_loop_provider_guard.py").read_text(encoding="utf-8")
        self.assertIn('@app.delete("/loop/sessions/{session_id}")', source)
        self.assertIn("web_session_delete.delete_api_session(AUTHORITY, session_id)", source)
        self.assertIn("web_session_delete.DELETE_FORBIDDEN", source)


if __name__ == "__main__":
    unittest.main()
