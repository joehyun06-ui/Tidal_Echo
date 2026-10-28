"""Offline proof of Uvicorn CLI proxy-header isolation for Render child processes.

No socket dial, real Render ingress, production login, API usage, or memory write.
The ASGI exercise uses Uvicorn's loaded app stack, not a mock parser.
"""
from __future__ import annotations

import importlib
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner
from starlette.requests import Request
from uvicorn import Config

from scripts.render_start import child_commands
from backend.tests._support import NoNetworkMixin

CLI = importlib.import_module("uvicorn.main")
FORGED = [
    (b"host", b"relay.internal.test"),
    (b"x-forwarded-for", b"8.8.8.8"),
    (b"x-forwarded-proto", b"https"),
    (b"x-forwarded-host", b"evil.example.test"),
]
FAKE_CONFIG = SimpleNamespace(
    deployment=SimpleNamespace(loop_port=3020),
    relay_port=3011,
    autonomous_wake_enabled=False,
)


def _commands():
    return child_commands(FAKE_CONFIG, executable="python-review")


def _scope(headers=None, *, client=("127.0.0.1", 41000)):
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "GET", "path": "/__offline_probe__",
        "raw_path": b"/__offline_probe__", "root_path": "",
        "query_string": b"", "scheme": "http",
        "server": ("127.0.0.1", 8081), "client": client,
        "headers": list(FORGED if headers is None else headers),
    }


async def _loaded_scope(proxy_headers, headers=None, client=("127.0.0.1", 41000)):
    seen = []

    async def asgi_app(scope, receive, send):
        seen.append({
            "scheme": scope["scheme"],
            "client": scope["client"],
            "server": scope["server"],
            "headers": list(scope["headers"]),
        })
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    # Even with a deliberately broad trusted_ips value, explicitly disabling
    # the proxy middleware must preserve the original scope.
    config = Config(
        asgi_app, interface="asgi3", log_config=None, lifespan="off",
        proxy_headers=proxy_headers, forwarded_allow_ips="*",
    )
    config.load()
    events = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        events.append(event)

    await config.loaded_app(_scope(headers, client=client), receive, send)
    assert len(seen) == 1
    assert events[0]["status"] == 204
    return seen[0]


class UvicornProxyCLIReviewTests(NoNetworkMixin, unittest.TestCase):
    def test_both_children_explicitly_disable_proxy_headers(self):
        commands = _commands()
        self.assertEqual(set(commands), {"api_loop", "relay"})
        for command in commands.values():
            self.assertEqual(command.count("--no-proxy-headers"), 1)
            self.assertNotIn("--proxy-headers", command)
            self.assertNotIn("--forwarded-allow-ips", command)

    def test_existing_internal_and_public_bindings_unchanged(self):
        commands = _commands()
        self.assertEqual(commands["api_loop"][:7], [
            "python-review", "-m", "uvicorn", "examples.api_loop:app",
            "--host", "127.0.0.1", "--port",
        ])
        self.assertEqual(commands["relay"][:7], [
            "python-review", "-m", "uvicorn", "backend.p3_relay_app:app",
            "--host", "0.0.0.0", "--port",
        ])
        self.assertEqual(commands["api_loop"][7:9], ["3020", "--workers"])
        self.assertEqual(commands["relay"][7:9], ["3011", "--workers"])
        for cmd in commands.values():
            self.assertEqual(cmd[9:11], ["1", "--no-access-log"])
            self.assertEqual(cmd[-1], "--no-proxy-headers")

    def test_environment_cannot_remove_explicit_off_flag(self):
        with mock.patch.dict(os.environ, {
            "FORWARDED_ALLOW_IPS": "*",
            "UVICORN_PROXY_HEADERS": "true",
            "OPENAI_API_KEY": "",
            "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "false",
        }):
            self.assertTrue(all(
                "--no-proxy-headers" in value for value in _commands().values()
            ))

    def test_real_uvicorn_cli_parser_disables_proxy_middleware(self):
        for name, command in _commands().items():
            with self.subTest(process=name):
                # Avoid invoking any process or importing either ASGI app.
                with mock.patch.object(CLI, "run") as invoked:
                    result = CliRunner().invoke(CLI.main, command[3:])
                self.assertEqual(result.exit_code, 0, result.output)
                invoked.assert_called_once()
                opts = invoked.call_args.kwargs
                self.assertIs(opts["proxy_headers"], False)
                self.assertIs(opts["access_log"], False)
                self.assertEqual(opts["workers"], 1)
                self.assertEqual(opts["host"], (
                    "127.0.0.1" if name == "api_loop" else "0.0.0.0"
                ))
                self.assertEqual(opts["port"], (
                    3020 if name == "api_loop" else 3011
                ))

    def test_real_cli_flag_wins_when_forwarded_allow_ips_is_wildcard(self):
        with mock.patch.dict(os.environ, {"FORWARDED_ALLOW_IPS": "*"}):
            with mock.patch.object(CLI, "run") as invoked:
                result = CliRunner().invoke(
                    CLI.main, _commands()["relay"][3:],
                )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIs(invoked.call_args.kwargs["proxy_headers"], False)

    def test_no_network_or_production_entrypoint_dependency(self):
        self.assertFalse(any(
            "memory_oidc" in item for cmd in _commands().values() for item in cmd
        ))


class UvicornProxyScopeReviewTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def test_disabled_rewriting_preserves_raw_http_scheme_and_peer(self):
        seen = await _loaded_scope(False)
        self.assertEqual(seen["scheme"], "http")
        self.assertEqual(seen["client"], ("127.0.0.1", 41000))
        self.assertEqual(seen["server"], ("127.0.0.1", 8081))
        self.assertEqual(seen["headers"], FORGED)

    async def test_disabled_rewriting_preserves_duplicate_untrusted_fields(self):
        raw = FORGED + [
            (b"x-forwarded-for", b"1.1.1.1"),
            (b"forwarded", b"for=1.1.1.1;proto=https;host=evil.test"),
        ]
        seen = await _loaded_scope(False, raw)
        self.assertEqual(seen["headers"], raw)
        self.assertEqual(seen["scheme"], "http")
        self.assertEqual(seen["client"], ("127.0.0.1", 41000))

    async def test_disabled_rewriting_preserves_untrusted_nonloopback_peer(self):
        seen = await _loaded_scope(False, client=("203.0.113.44", 52222))
        self.assertEqual(seen["client"], ("203.0.113.44", 52222))
        self.assertEqual(seen["scheme"], "http")

    async def test_positive_control_enabled_middleware_changes_scope(self):
        # Demonstrates that the same real Uvicorn stack WOULD trust forged
        # client/proto fields if middleware were enabled for a broad peer.
        seen = await _loaded_scope(True)
        self.assertEqual(seen["scheme"], "https")
        self.assertEqual(seen["client"][0], "8.8.8.8")

    async def test_disabled_scope_is_not_an_authorization_result(self):
        seen = await _loaded_scope(False)
        self.assertNotIn("principal", seen)
        self.assertNotIn("may_write_memory", seen)
        self.assertNotIn("may_issue_browser_session", seen)
        self.assertNotIn("authorization", [key.decode() for key, _ in seen["headers"]])


if __name__ == "__main__":
    unittest.main()
