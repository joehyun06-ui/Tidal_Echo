"""Real P3 ASGI no-network adversarial header tests.

Purpose: forwarded/Host/identity claims must never authenticate current P3
protected routes without a real credential. NOT a Render-edge sanitization
attestation and NOT a production OIDC approval.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest

from backend.tests._support import NoNetworkMixin, load_app, request

_FAKE_BEARER = "Bearer test-relay-secret"
_PUBLIC_HOST = "ouou-home-telegram.onrender.com"
_P3_AUTH_ROUTES = (
    "/app/status",
    "/app/provider/capabilities",
    "/app/memory/hybrid-active/status",
    "/app/memory/hybrid-shadow/status",
    "/app/memory/session-write/preflight",
    "/app/sessions/ordinary-offline-session/memory/evidence",
)
_FORGED_X = [
    ("host", "testserver"),
    ("x-forwarded-for", "8.8.8.8"),
    ("x-forwarded-proto", "https"),
    ("x-forwarded-host", _PUBLIC_HOST),
]
_FORGED_RFC = [
    ("host", "testserver"),
    ("forwarded", "for=8.8.8.8;proto=https;host=" + _PUBLIC_HOST),
]


class PublicP3IngressSpoofTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        load_app(self.temp.name, telegram=False)
        os.environ.update({
            "LEGACY_CHAT_BRIDGE_TOKEN": "offline-legacy-token-not-a-real-credential-987654321",
            "LEGACY_CHAT_BRIDGE_SESSION": "offline-p3-session",
            "CODEX_CONTROL_ENABLED": "false",
            "CODEX_CANARY_ENTRYPOINTS_ENABLED": "false",
            "CODEX_GENERATION_ENABLED": "false",
            "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "false",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
            "MEMORY_SESSION_WRITE_PREFLIGHT_ENABLED": "false",
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

    async def get(self, path="/app/status", headers=None):
        return await request(self.module, "GET", path, headers=headers or {})

    async def deny(self, path="/app/status", headers=None):
        response = await self.get(path, headers)
        self.assertEqual(
            response.status_code, 401,
            f"{path}: expected 401, got {response.status_code}",
        )
        self.assertNotIn("access_token", response.text.lower())
        self.assertNotIn("refresh_token", response.text.lower())

    async def test_actual_p3_app_imports_same_relay_auth_boundary(self):
        self.assertIs(self.module.app, self.module.relay_app.app)

    async def test_baseline_anonymous_p3_route_is_denied(self):
        await self.deny()

    async def test_forged_global_xff_client_cannot_replace_authorization(self):
        await self.deny(headers=_FORGED_X)

    async def test_forged_local_xff_client_cannot_replace_authorization(self):
        await self.deny(headers=[
            ("host", "testserver"),
            ("x-forwarded-for", "127.0.0.1"),
            ("x-forwarded-proto", "https"),
            ("x-forwarded-host", _PUBLIC_HOST),
        ])

    async def test_forged_proxy_peer_xff_cannot_replace_authorization(self):
        await self.deny(headers=[
            ("host", "testserver"),
            ("x-forwarded-for", "10.0.0.1"),
            ("x-forwarded-proto", "https"),
            ("x-forwarded-host", _PUBLIC_HOST),
            ("x-real-ip", "127.0.0.1"),
        ])

    async def test_forged_rfc_forwarded_identity_is_denied(self):
        await self.deny(headers=_FORGED_RFC)

    async def test_combined_forwarded_families_are_not_credentials(self):
        await self.deny(headers=[
            *_FORGED_X, ("forwarded",
                         "for=8.8.8.8;proto=https;host=" + _PUBLIC_HOST),
        ])

    async def test_multihop_forwarded_claim_is_denied(self):
        await self.deny(headers=[
            ("host", "testserver"),
            ("x-forwarded-for", "8.8.8.8, 10.0.0.1"),
            ("x-forwarded-proto", "https"),
        ])

    async def test_duplicate_xff_and_host_are_not_authorization(self):
        await self.deny(headers=[
            ("host", "testserver"), ("host", "evil.example.test"),
            ("x-forwarded-for", "8.8.8.8"),
            ("x-forwarded-for", "127.0.0.1"),
            ("x-forwarded-proto", "https"),
        ])

    async def test_untrusted_public_host_value_cannot_grant_access(self):
        await self.deny(headers=[
            ("host", _PUBLIC_HOST),
            ("x-forwarded-host", _PUBLIC_HOST),
            ("x-forwarded-proto", "https"),
        ])

    async def test_evil_host_value_also_cannot_grant_access(self):
        await self.deny(headers=[
            ("host", "evil.example.test"),
            ("x-forwarded-host", _PUBLIC_HOST),
            ("x-forwarded-for", "8.8.8.8"),
        ])

    async def test_identity_impersonation_headers_cannot_grant_access(self):
        await self.deny(headers=[
            *_FORGED_X,
            ("x-user-id", "1"),
            ("x-principal-id", "admin"),
            ("x-auth-epoch", "9999"),
            ("x-memory-identity", "trusted"),
        ])

    async def test_wrong_bearer_with_proxy_headers_stays_unauthorized(self):
        await self.deny(headers=[
            *_FORGED_X,
            ("authorization", "Bearer synthetically-wrong-secret"),
        ])

    async def test_real_p3_provider_capabilities_remains_protected(self):
        await self.deny("/app/provider/capabilities", _FORGED_X)

    async def test_real_p3_hybrid_active_status_remains_protected(self):
        await self.deny("/app/memory/hybrid-active/status", _FORGED_RFC)

    async def test_real_p3_hybrid_shadow_status_remains_protected(self):
        await self.deny("/app/memory/hybrid-shadow/status", _FORGED_X)

    async def test_real_p3_memory_evidence_remains_protected(self):
        await self.deny(
            "/app/sessions/ordinary-offline-session/memory/evidence", _FORGED_X
        )

    async def test_real_p3_memory_write_preflight_requires_authorization(self):
        await self.deny("/app/memory/session-write/preflight", _FORGED_X)

    async def test_memory_preflight_rejects_legacy_query_token_with_spoofs(self):
        await self.deny(
            "/app/memory/session-write/preflight?token=test-relay-secret",
            _FORGED_X,
        )

    async def test_memory_preflight_rejects_duplicate_authorizations(self):
        await self.deny(
            "/app/memory/session-write/preflight",
            [
                *_FORGED_X,
                ("authorization", _FAKE_BEARER),
                ("authorization", _FAKE_BEARER),
            ],
        )

    async def test_valid_bearer_positive_control_does_not_need_forwarded(self):
        response = await self.get(headers={"authorization": _FAKE_BEARER})
        self.assertEqual(response.status_code, 200)

    async def test_public_health_route_is_not_confused_with_identity_gate(self):
        response = await self.get("/healthz", _FORGED_X)
        self.assertEqual(response.status_code, 200)

    async def test_no_real_oidc_routes_enabled_by_current_p3_entry(self):
        paths = [getattr(route, "path", "") for route in self.module.app.routes]
        self.assertFalse(any(path.startswith("/auth/oidc/") for path in paths))
        self.assertEqual(os.environ["MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED"],
                         "false")


if __name__ == "__main__":
    unittest.main()
