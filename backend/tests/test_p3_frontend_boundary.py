"""P3 wrappers must expose the same authenticated browser transport boundary."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from backend.tests import test_p3_production_codex_relay as fixtures


class P3FrontendBoundaryTests(unittest.TestCase):
    def test_both_p3_relays_share_routes_auth_and_development_cors(self):
        script = r'''
import asyncio, importlib, sys
import httpx
module = importlib.import_module(sys.argv[1])
paths = {getattr(route, "path", "") for route in module.app.routes}
required = {
    "/app/provider/capabilities", "/app/provider/status", "/provider/status",
    "/provider/login/start", "/provider/login/cancel", "/provider/logout", "/provider/models",
    "/app/provider/api/config", "/app/provider/api/models", "/app/sessions/{session_id}/model",
    "/app/send", "/app/history", "/app/stream", "/app/sessions", "/app/sessions/{session_id}/retire",
}
assert required <= paths, sorted(required-paths)
assert "/provider/canary/create" not in paths
async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
        for origin, allowed in [("http://localhost:5173", True), ("https://www.ouoalways.com", True), ("https://unknown.example", False)]:
            response = await client.options("/app/provider/api/config", headers={"Origin":origin, "Access-Control-Request-Method":"POST", "Access-Control-Request-Headers":"authorization,content-type"})
            assert response.status_code == (200 if allowed else 400)
            assert response.headers.get("access-control-allow-origin") == (origin if allowed else None)
        for path in ("/app/provider/api/config", "/app/provider/api/models", "/provider/models", "/app/sessions/test/model", "/app/provider/capabilities", "/app/stream"):
            response = await client.get(path, headers={"Origin":"http://localhost:5173"})
            assert response.status_code == 401, (path, response.status_code)
            assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"
        response = await client.get("/app/provider/capabilities", headers={"Authorization":"Bearer test-relay-secret"})
        assert response.status_code == 200
        data = response.json()
        assert data["contract_version"] == 1
        assert data["web_sessions"]["provider_immutable"] is True
        assert data["web_sessions"]["providers"]["codex"]["create"] is False
asyncio.run(run())
print("p3-boundary-ok")
'''
        for module in ("backend.p3_relay_app", "backend.p3_codex_relay_app"):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as temporary:
                env = fixtures.P3ProductionCodexRelayTests()._base_env(Path(temporary))
                env.update({"RELAY_ALLOW_ORIGINS":"https://www.ouoalways.com", "RELAY_CORS_DEV_ENABLED":"true", "CODEX_CANARY_ENTRYPOINTS_ENABLED":"false"})
                env.pop("RELAY_DEV_ALLOW_ORIGINS", None)
                result = subprocess.run([sys.executable, "-c", script, module], cwd=fixtures.ROOT, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("p3-boundary-ok", result.stdout)
