import unittest

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.cors_config import allowed_origins, LOCAL_DEV_ORIGINS
from backend.deployment_config import DeploymentConfigError


class CorsConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_dev_preview_and_disallowed_preflights(self):
        cases = [
            ({"RELAY_ALLOW_ORIGINS": "https://www.ouoalways.com"}, "http://localhost:5173", False),
            ({"RELAY_ALLOW_ORIGINS": "https://www.ouoalways.com"}, "https://www.ouoalways.com", True),
            ({"RELAY_CORS_DEV_ENABLED": "true"}, "http://localhost:5173", True),
            ({"RELAY_CORS_DEV_ENABLED": "true"}, "http://127.0.0.1:3000", True),
            ({"RELAY_CORS_DEV_ENABLED": "true", "RELAY_DEV_ALLOW_ORIGINS": "https://branch.example"}, "https://branch.example", True),
            ({"RELAY_CORS_DEV_ENABLED": "true", "RELAY_DEV_ALLOW_ORIGINS": "https://branch.example"}, "https://other.example", False),
            ({"RELAY_CORS_DEV_ENABLED": "true"}, "http://localhost.evil.test:5173", False),
        ]
        for env, origin, permitted in cases:
            with self.subTest(origin=origin, env=env):
                app = FastAPI()
                app.add_middleware(CORSMiddleware, allow_origins=allowed_origins(env), allow_methods=["*"], allow_headers=["*"])
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    response = await client.options("/app/send", headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
                self.assertEqual(response.status_code, 200 if permitted else 400)
                self.assertEqual(response.headers.get("access-control-allow-origin"), origin if permitted else None)

    def test_only_explicit_origins_and_strict_switch(self):
        self.assertEqual(set(allowed_origins({"RELAY_ALLOW_ORIGINS":"", "RELAY_CORS_DEV_ENABLED":"true"})), set(LOCAL_DEV_ORIGINS))
        for origin in ["*", "https://*.example", "null", "https://user:pass@example", "https://example/path", "https://example?x=1", "https://example:bad"]:
            with self.subTest(origin=origin), self.assertRaises(DeploymentConfigError):
                allowed_origins({"RELAY_ALLOW_ORIGINS":origin})
        with self.assertRaises(DeploymentConfigError):
            allowed_origins({"RELAY_CORS_DEV_ENABLED":"maybe"})
