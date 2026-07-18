from __future__ import annotations

import dataclasses
import importlib
import json
import os
import signal
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import deployment_config
from scripts import render_start
from scripts import configure_telegram_webhook

from backend.tests._support import NoNetworkMixin, load_app, request


def render_env(root: Path) -> dict[str, str]:
    return {
        "PORT": "10000",
        "RENDER_TELEGRAM_MVP": "true",
        "RENDER_PERSISTENT_ROOT": str(root),
        "RELAY_SECRET": "relay-secret-distinct",
        "RELAY_DB": str(root / "relay.db"),
        "RELAY_UPLOAD_DIR": str(root / "uploads"),
        "RELAY_BRAIN_FILE": str(root / "brain_target"),
        "RELAY_BRAIN_TARGET": "loop",
        "RELAY_LOOP_INGEST_URL": "http://127.0.0.1:3020/loop/ingest",
        "LOOP_CONFIG": str(root / "api_loop.config.json"),
        "LOOP_PORT": "3020",
        "LOOP_MODEL_TOTAL_TIMEOUT_SECONDS": "120",
        "LOOP_CALLBACK_TIMEOUT_SECONDS": "30",
        "LOOP_TIMEOUT_SAFETY_MARGIN_SECONDS": "15",
        "LOOP_DISPATCH_TIMEOUT_SECONDS": "180",
        "TELEGRAM_ENABLED": "true",
        "TELEGRAM_BOT_TOKEN": "invalid-test-token",
        "TELEGRAM_WEBHOOK_SECRET": "webhook-secret-distinct",
        "CHANNEL_AUDIT_HMAC_SECRET": "audit-secret-distinct",
        "TELEGRAM_BOT_ACCOUNT_ID": "test-bot",
        "TELEGRAM_ALLOWED_USER_IDS": "11001",
        "TELEGRAM_ALLOWED_CHAT_IDS": "22001",
        "TELEGRAM_API_BASE": "https://api.telegram.org",
        "TELEGRAM_TEST_MODE": "false",
        "LLM_API_BASE": "https://model.invalid/v1",
        "LLM_API_KEY": "invalid-test-model-key",
        "LLM_MODEL": "test-model",
    }


class DeploymentConfigTests(NoNetworkMixin, unittest.TestCase):
    def test_enabled_telegram_missing_config_fails_app_startup(self):
        env = {"RELAY_SECRET": "relay-only", "TELEGRAM_ENABLED": "true"}
        with mock.patch.dict(os.environ, env, clear=True):
            for name in ("backend.app", "backend.telegram_integration", "backend.channel_store"):
                sys.modules.pop(name, None)
            with self.assertRaises(SystemExit) as raised:
                importlib.import_module("backend.app")
        self.assertIn("telegram_config_incomplete", str(raised.exception))

    def test_equal_secrets_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root))
            env["CHANNEL_AUDIT_HMAC_SECRET"] = env["RELAY_SECRET"]
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "telegram_secrets"):
                render_start.preflight(env)

    def test_invalid_allowlist_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root)); env["TELEGRAM_ALLOWED_USER_IDS"] = "11001,*"
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "telegram_config_invalid"):
                render_start.preflight(env)

    def test_brain_target_is_initialized_atomically(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root))
            render_start.preflight(env)
            brain = Path(env["RELAY_BRAIN_FILE"])
            self.assertEqual(brain.read_text(encoding="utf-8"), "loop\n")
            self.assertEqual(list(brain.parent.glob(".brain_target.*")), [])

    def test_non_loop_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root)); env["RELAY_BRAIN_TARGET"] = "desktop"
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "invalid_brain_target"):
                render_start.preflight(env)

    def test_fallback_environment_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root)); env["LLM_MODEL_2"] = "fallback"
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "model_fallback"):
                render_start.preflight(env)
            env = render_env(Path(root)); env["LLM_MODEL_2"] = " "
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "model_fallback"):
                render_start.preflight(env)

    def test_multimodel_loop_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root))
            Path(env["LOOP_CONFIG"]).write_text(
                json.dumps({"main_chain": [{"model": "a"}, {"model": "b"}]}), encoding="utf-8"
            )
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "model_fallback"):
                render_start.preflight(env)

    def test_invalid_timeout_relationship_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root)); env["LOOP_DISPATCH_TIMEOUT_SECONDS"] = "100"
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "timeout_relationship"):
                render_start.preflight(env)

    def test_persistent_path_outside_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            env = render_env(Path(root)); env["RELAY_DB"] = str(Path(outside) / "relay.db")
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "persistent_path"):
                render_start.preflight(env)

    def test_relay_and_loop_ports_must_differ(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root)); env["PORT"] = env["LOOP_PORT"]
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "ports_must_differ"):
                render_start.preflight(env)

    def test_typo_booleans_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("RENDER_TELEGRAM_MVP", "TELEGRAM_ENABLED", "TELEGRAM_TEST_MODE"):
                env = render_env(Path(root)); env[name] = "treu"
                with self.subTest(name=name), self.assertRaises(deployment_config.DeploymentConfigError):
                    render_start.preflight(env)
        with self.assertRaises(deployment_config.DeploymentConfigError):
            deployment_config.parse_strict_bool(" true ", "invalid_bool")

    def test_non_finite_timeouts_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for value in ("nan", "inf", "-inf", "0", "-1"):
                env = render_env(Path(root)); env["LOOP_MODEL_TOTAL_TIMEOUT_SECONDS"] = value
                with self.subTest(value=value), self.assertRaisesRegex(
                    deployment_config.DeploymentConfigError, "invalid_loop_timeout"
                ):
                    render_start.preflight(env)

    def test_noncanonical_ports_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for value in ("+3020", " 3020", "3020 ", "3_020", "3020.0", "3e3", "３０２０"):
                env = render_env(Path(root)); env["LOOP_PORT"] = value
                with self.subTest(value=value), self.assertRaisesRegex(
                    deployment_config.DeploymentConfigError, "invalid_loop_port"
                ):
                    render_start.preflight(env)

    def test_webhook_secret_format_and_length_are_strict(self):
        with tempfile.TemporaryDirectory() as root:
            for value in ("has space", "unicode密钥", "a" * 257):
                env = render_env(Path(root)); env["TELEGRAM_WEBHOOK_SECRET"] = value
                with self.subTest(value=value[:12]), self.assertRaisesRegex(
                    deployment_config.DeploymentConfigError, "telegram_config_invalid"
                ):
                    render_start.preflight(env)
            env = render_env(Path(root)); env["TELEGRAM_WEBHOOK_SECRET"] = "A" * 256
            render_start.preflight(env)

    def test_allowlist_empty_duplicate_and_unicode_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for value in ("11001,", ",11001", "11001,,11002", "11001,11001", "１１００１"):
                env = render_env(Path(root)); env["TELEGRAM_ALLOWED_USER_IDS"] = value
                with self.subTest(value=value), self.assertRaisesRegex(
                    deployment_config.DeploymentConfigError, "telegram_config_invalid"
                ):
                    render_start.preflight(env)

    def test_nested_persistent_directories_are_created(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root))
            env["RELAY_DB"] = str(Path(root) / "db" / "relay.db")
            env["RELAY_UPLOAD_DIR"] = str(Path(root) / "files" / "uploads")
            env["RELAY_BRAIN_FILE"] = str(Path(root) / "state" / "brain_target")
            env["LOOP_CONFIG"] = str(Path(root) / "loop" / "config.json")
            render_start.preflight(env)
            for directory in ("db", "files/uploads", "state", "loop"):
                self.assertTrue((Path(root) / directory).is_dir())

    def test_parent_refs_and_similar_prefix_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root)); env["RELAY_DB"] = str(Path(root) / "db" / ".." / "relay.db")
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "persistent_path"):
                render_start.preflight(env)
            env = render_env(Path(root)); env["RELAY_DB"] = str(Path(root + "-other") / "relay.db")
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "persistent_path"):
                render_start.preflight(env)

    def test_existing_symlink_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            link = Path(root) / "linked"
            try:
                link.symlink_to(Path(outside), target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {type(exc).__name__}")
            env = render_env(Path(root)); env["RELAY_DB"] = str(link / "relay.db")
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "persistent_path"):
                render_start.preflight(env)

    def test_loop_config_invalid_shapes_and_size_fail_fast(self):
        with tempfile.TemporaryDirectory() as root:
            cases = ("", "{broken", "[]", '{"main_chain":"one"}', '{"main_chain":[{}]}')
            for index, content in enumerate(cases):
                env = render_env(Path(root)); path = Path(root) / f"bad-{index}.json"; env["LOOP_CONFIG"] = str(path)
                path.write_text(content, encoding="utf-8")
                with self.subTest(index=index), self.assertRaises(deployment_config.DeploymentConfigError):
                    render_start.preflight(env)
            env = render_env(Path(root)); path = Path(root) / "huge.json"; env["LOOP_CONFIG"] = str(path)
            path.write_text(" " * (deployment_config.LOOP_CONFIG_MAX_BYTES + 1), encoding="utf-8")
            with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "invalid_loop_config_size"):
                render_start.preflight(env)

    def test_render_loop_update_requires_one_complete_exact_route(self):
        invalid = (
            {"main_chain": []},
            {"main_chain": [{}]},
            {"main_chain": [{"url": "https://one.invalid", "key": "key", "model": " "}]},
            {"main_chain": [{"url": "https://one.invalid", "key": "key", "model": "one", "alias": "x"}]},
            {"main_chain": [{"url": {"nested": True}, "key": "key", "model": "one"}]},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(deployment_config.DeploymentConfigError):
                deployment_config.validate_loop_config_update_request(payload, render_mvp=True)


class _TaskState:
    def __init__(self, done: bool = False): self._done = done
    def done(self) -> bool: return self._done


class ReadinessTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name)
        self.module.app.state.telegram_worker_task = _TaskState(False)

    async def test_healthz_does_not_probe_api_loop(self):
        with mock.patch.object(self.module, "_api_loop_ready", side_effect=AssertionError("must not run")):
            response = await request(self.module, "GET", "/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    async def test_readyz_reports_each_failed_check(self):
        async def yes(): return True
        cases = (
            ("database", mock.patch.object(self.module, "_database_ready", return_value=False)),
            ("persistent_path", mock.patch.object(self.module.deployment_config, "path_within_root", return_value=False)),
            ("brain_target", mock.patch.object(self.module, "brain_target", return_value="desktop")),
            ("telegram_config", mock.patch.object(
                self.module, "TELEGRAM", dataclasses.replace(self.module.TELEGRAM, enabled=False)
            )),
            ("api_loop", mock.patch.object(
                self.module, "_api_loop_ready", new=mock.AsyncMock(return_value=False)
            )),
        )
        original_deployment = self.module.DEPLOYMENT
        self.module.DEPLOYMENT = dataclasses.replace(
            original_deployment, render_telegram_mvp=True,
            persistent_root=Path(self.temp.name).resolve(),
        )
        self.addCleanup(setattr, self.module, "DEPLOYMENT", original_deployment)
        for failed, patcher in cases:
            extra = (
                mock.patch.object(self.module, "_database_ready", return_value=True)
                if failed == "api_loop"
                else mock.patch.object(self.module, "_api_loop_ready", new=mock.AsyncMock(side_effect=yes))
            )
            with self.subTest(check=failed), patcher, extra:
                response = await request(self.module, "GET", "/readyz")
                self.assertEqual(response.status_code, 503)
                self.assertFalse(response.json()["checks"][failed])

    async def test_worker_task_exit_makes_readyz_fail(self):
        self.module.app.state.telegram_worker_task = _TaskState(True)
        with mock.patch.object(self.module, "_api_loop_ready", new=mock.AsyncMock(return_value=True)):
            response = await request(self.module, "GET", "/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["telegram_worker"])

    async def test_readyz_succeeds_when_all_local_checks_pass(self):
        with mock.patch.object(self.module, "_api_loop_ready", new=mock.AsyncMock(return_value=True)):
            response = await request(self.module, "GET", "/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")

    async def test_missing_required_table_makes_readyz_fail(self):
        conn = sqlite3.connect(self.module.DB_PATH)
        try:
            conn.execute("DROP TABLE delivery_parts")
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(self.module, "_api_loop_ready", new=mock.AsyncMock(return_value=True)):
            response = await request(self.module, "GET", "/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["database"])

    async def test_api_loop_identity_and_nonce_are_required(self):
        class Response:
            status_code = 200
            def __init__(self, nonce): self.nonce = nonce
            def json(self): return {"ok": True, "service": "api_loop", "instance_nonce": self.nonce}

        class Client:
            def __init__(self, nonce): self.nonce = nonce
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get(self, _url): return Response(self.nonce)

        original = self.module.DEPLOYMENT
        self.module.DEPLOYMENT = dataclasses.replace(original, render_telegram_mvp=True)
        self.module.API_LOOP_EXPECTED_NONCE = "expected"
        self.addCleanup(setattr, self.module, "DEPLOYMENT", original)
        with mock.patch.object(self.module.httpx, "AsyncClient", return_value=Client("wrong")):
            self.assertFalse(await self.module._api_loop_ready())
        with mock.patch.object(self.module.httpx, "AsyncClient", return_value=Client("expected")):
            self.assertTrue(await self.module._api_loop_ready())

    async def test_render_runtime_routes_preserve_loop_and_single_model(self):
        original = self.module.DEPLOYMENT
        self.module.DEPLOYMENT = dataclasses.replace(original, render_telegram_mvp=True)
        self.addCleanup(setattr, self.module, "DEPLOYMENT", original)
        headers = {"Authorization": "Bearer test-relay-secret"}
        for target in ("desktop", "", " ", " loop ", "LOOP"):
            response = await request(self.module, "POST", "/app/brain", headers=headers, json={"target": target})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.module.brain_target(), "loop")
        response = await request(self.module, "POST", "/app/loop_config", headers=headers, json={
            "main_chain": [
                {"url": "https://one.invalid", "key": "one", "model": "one"},
                {"url": "https://two.invalid", "key": "two", "model": "two"},
            ]
        })
        self.assertEqual(response.status_code, 400)

    async def test_healthz_is_minimal(self):
        response = await request(self.module, "GET", "/healthz")
        self.assertEqual(response.json(), {"ok": True})

    async def test_worker_terminal_exit_requests_one_restart_but_shutdown_does_not(self):
        original = self.module.DEPLOYMENT
        self.module.DEPLOYMENT = dataclasses.replace(original, render_telegram_mvp=True)
        self.module.app.state.shutting_down = False
        self.module.app.state.worker_restart_requested = False
        self.addCleanup(setattr, self.module, "DEPLOYMENT", original)
        with mock.patch.object(self.module, "_request_worker_restart") as restart:
            self.module._telegram_worker_done(mock.Mock())
            self.module._telegram_worker_done(mock.Mock())
            restart.assert_called_once_with()
            self.module.app.state.shutting_down = True
            self.module.app.state.worker_restart_requested = False
            self.module._telegram_worker_done(mock.Mock())
            restart.assert_called_once_with()

    async def test_render_mode_disables_public_api_docs(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root)); env["RELAY_PORT"] = "10000"; env["API_LOOP_EXPECTED_NONCE"] = "nonce"
            with mock.patch.dict(os.environ, env, clear=True):
                for name in ("backend.app", "backend.telegram_integration", "backend.channel_store"):
                    sys.modules.pop(name, None)
                module = importlib.import_module("backend.app")
            paths = {route.path for route in module.app.routes}
            self.assertNotIn("/docs", paths)
            self.assertNotIn("/redoc", paths)
            self.assertNotIn("/openapi.json", paths)


class _FakeSocket:
    def close(self): pass


class _FakeProcess:
    next_pid = 4100

    def __init__(self, exit_code=None):
        self.pid = _FakeProcess.next_pid; _FakeProcess.next_pid += 1
        self.exit_code = exit_code
        self.signals: list[int] = []
        self.killed = False

    def poll(self): return self.exit_code
    def send_signal(self, signum): self.signals.append(signum); self.exit_code = 0
    def kill(self): self.killed = True; self.exit_code = -9
    def wait(self, timeout=None): return self.exit_code


class _StubbornProcess(_FakeProcess):
    def send_signal(self, signum): self.signals.append(signum)


class _Clock:
    def __init__(self): self.now = 0.0
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds


class SupervisorTests(NoNetworkMixin, unittest.TestCase):
    def config(self, root: Path): return render_start.preflight(render_env(root))

    def supervisor(self, processes, health_probe=lambda *a, **k: True):
        clock = _Clock(); calls = []

        def popen(command, **kwargs):
            calls.append(command)
            return processes[len(calls) - 1]

        supervisor = render_start.RenderSupervisor(
            popen=popen, health_probe=health_probe, monotonic=clock.monotonic, sleep=clock.sleep,
            logger=lambda *args: None,
        )
        supervisor._send_signal = lambda process, signum: process.send_signal(signum)
        supervisor._force_kill = lambda process: process.kill()
        return supervisor, calls

    def test_start_order_and_child_exit_terminates_peer(self):
        with tempfile.TemporaryDirectory() as root:
            loop = _FakeProcess(); relay = _FakeProcess(exit_code=7)
            supervisor, calls = self.supervisor([loop, relay])
            result = supervisor.run(self.config(Path(root)), render_env(Path(root)))
        self.assertEqual(result, 1)
        self.assertIn("examples.api_loop:app", calls[0])
        self.assertIn("backend.app:app", calls[1])
        self.assertEqual(calls[0][calls[0].index("--host") + 1], "127.0.0.1")
        self.assertEqual(calls[1][calls[1].index("--host") + 1], "0.0.0.0")
        self.assertEqual(calls[0][calls[0].index("--workers") + 1], "1")
        self.assertEqual(calls[1][calls[1].index("--workers") + 1], "1")
        self.assertIn(signal.SIGTERM, loop.signals)

    def test_loop_exit_before_ready_prevents_relay_start(self):
        with tempfile.TemporaryDirectory() as root:
            loop = _FakeProcess(exit_code=3)
            supervisor, calls = self.supervisor([loop])
            result = supervisor.run(self.config(Path(root)), render_env(Path(root)))
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)

    def test_signal_is_forwarded_to_both_children(self):
        with tempfile.TemporaryDirectory() as root:
            loop = _FakeProcess(); relay = _FakeProcess()
            supervisor, _calls = self.supervisor([loop, relay])

            def sleep_then_signal(*_args, **_kwargs):
                supervisor.handle_signal(signal.SIGINT)

            supervisor._sleep = sleep_then_signal
            result = supervisor.run(self.config(Path(root)), render_env(Path(root)))
        self.assertEqual(result, 0)
        self.assertEqual(loop.signals, [signal.SIGINT])
        self.assertEqual(relay.signals, [signal.SIGINT])

    def test_loop_readiness_timeout_stops_before_relay(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root)); env["SUPERVISOR_LOOP_READY_TIMEOUT_SECONDS"] = "0.2"
            loop = _FakeProcess()
            supervisor, calls = self.supervisor([loop], health_probe=lambda *a, **k: False)
            result = supervisor.run(render_start.preflight(env), env)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn(signal.SIGTERM, loop.signals)

    def test_shutdown_force_kills_after_grace_timeout(self):
        clock = _Clock(); process = _StubbornProcess()
        supervisor = render_start.RenderSupervisor(
            monotonic=clock.monotonic, sleep=clock.sleep, logger=lambda *args: None
        )
        supervisor.processes = {"api_loop": process}
        supervisor._send_signal = lambda child, signum: child.send_signal(signum)
        supervisor._force_kill = lambda child: child.kill()
        supervisor.shutdown(0.2)
        self.assertIn(signal.SIGTERM, process.signals)
        self.assertTrue(process.killed)

    def test_nonce_mismatch_or_unrelated_listener_never_starts_relay(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root)); env["SUPERVISOR_LOOP_READY_TIMEOUT_SECONDS"] = "0.2"
            observed = []
            loop = _FakeProcess()
            supervisor, calls = self.supervisor(
                [loop], health_probe=lambda port, nonce, timeout: observed.append((port, nonce, timeout)) or False
            )
            config = render_start.preflight(env)
            result = supervisor.run(config, env)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(observed)
        self.assertTrue(all(item[1] == config.instance_nonce for item in observed))

    def test_process_lookup_error_does_not_stop_peer_cleanup(self):
        first = _FakeProcess(); second = _FakeProcess()
        supervisor = render_start.RenderSupervisor(logger=lambda *args: None)
        supervisor.processes = {"api_loop": first, "relay": second}

        def send(child, signum):
            if child is first:
                first.exit_code = 0
                raise ProcessLookupError()
            child.send_signal(signum)

        supervisor._send_signal = send
        supervisor.shutdown(0.01)
        self.assertIn(signal.SIGTERM, second.signals)

    def test_posix_process_group_signal_path(self):
        process = _FakeProcess()
        with mock.patch.object(render_start.os, "name", "posix"), \
             mock.patch.object(render_start.os, "getpgid", return_value=9876, create=True) as getpgid, \
             mock.patch.object(render_start.os, "killpg", create=True) as killpg:
            render_start.RenderSupervisor._send_signal(process, signal.SIGTERM)
        getpgid.assert_called_once_with(process.pid)
        killpg.assert_called_once_with(9876, signal.SIGTERM)

    def test_cleanup_exception_does_not_override_child_exit_code(self):
        with tempfile.TemporaryDirectory() as root:
            loop = _FakeProcess(); relay = _FakeProcess(exit_code=9)
            supervisor, _calls = self.supervisor([loop, relay])
            supervisor.shutdown = mock.Mock(side_effect=RuntimeError("cleanup"))
            result = supervisor.run(self.config(Path(root)), render_env(Path(root)))
        self.assertEqual(result, 1)

    def test_http_health_probe_validates_service_and_nonce(self):
        class Response:
            status = 200
            def __init__(self, payload): self.payload = payload
            def read(self, _limit): return json.dumps(self.payload).encode()

        class Connection:
            payload = {"ok": True, "service": "api_loop", "instance_nonce": "nonce"}
            def __init__(self, *_args, **_kwargs): pass
            def request(self, *_args, **_kwargs): pass
            def getresponse(self): return Response(self.payload)
            def close(self): pass

        with mock.patch.object(render_start.http.client, "HTTPConnection", Connection):
            self.assertTrue(render_start.api_loop_health_probe(3020, "nonce", 0.5))
            self.assertFalse(render_start.api_loop_health_probe(3020, "wrong", 0.5))
            Connection.payload = {"ok": True, "service": "other", "instance_nonce": "nonce"}
            self.assertFalse(render_start.api_loop_health_probe(3020, "nonce", 0.5))

    def test_child_exit_after_health_response_still_prevents_relay(self):
        with tempfile.TemporaryDirectory() as root:
            loop = _FakeProcess()
            def probe(*_args):
                loop.exit_code = 4
                return True
            supervisor, calls = self.supervisor([loop], health_probe=probe)
            result = supervisor.run(self.config(Path(root)), render_env(Path(root)))
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)

    def test_nonce_is_scoped_to_child_roles(self):
        with tempfile.TemporaryDirectory() as root:
            loop = _FakeProcess(); relay = _FakeProcess(exit_code=1); envs = []
            processes = [loop, relay]
            def popen(_command, **kwargs):
                envs.append(kwargs["env"])
                return processes[len(envs) - 1]
            supervisor = render_start.RenderSupervisor(
                popen=popen, health_probe=lambda *_args: True,
                monotonic=_Clock().monotonic, sleep=lambda _seconds: None, logger=lambda *args: None,
            )
            supervisor._send_signal = lambda process, signum: process.send_signal(signum)
            config = self.config(Path(root))
            env = render_env(Path(root))
            env["API_LOOP_INSTANCE_NONCE"] = "stale-instance"
            env["API_LOOP_EXPECTED_NONCE"] = "stale-expected"
            self.assertEqual(supervisor.run(config, env), 1)
        self.assertEqual(envs[0]["API_LOOP_INSTANCE_NONCE"], config.instance_nonce)
        self.assertNotIn("API_LOOP_EXPECTED_NONCE", envs[0])
        self.assertEqual(envs[1]["API_LOOP_EXPECTED_NONCE"], config.instance_nonce)
        self.assertNotIn("API_LOOP_INSTANCE_NONCE", envs[1])


class RuntimeConfigAndWebhookTests(NoNetworkMixin, unittest.TestCase):
    def test_api_loop_stream_typo_fails_import(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root)); env.update({"API_LOOP_INSTANCE_NONCE": "nonce", "LOOP_STREAM": "treu"})
            with mock.patch.dict(os.environ, env, clear=True):
                sys.modules.pop("examples.api_loop", None)
                with self.assertRaisesRegex(deployment_config.DeploymentConfigError, "invalid_loop_stream"):
                    importlib.import_module("examples.api_loop")

    def test_direct_api_loop_config_rejects_multimodel_without_writing(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root))
            env.update({"API_LOOP_INSTANCE_NONCE": "nonce", "LOOP_STREAM": "0"})
            with mock.patch.dict(os.environ, env, clear=True):
                sys.modules.pop("examples.api_loop", None)
                module = importlib.import_module("examples.api_loop")
            path = Path(env["LOOP_CONFIG"])
            with self.assertRaisesRegex(Exception, "model_fallback_not_allowed"):
                module.update_config({"main_chain": [
                    {"url": "https://one.invalid", "key": "one", "model": "one"},
                    {"url": "https://two.invalid", "key": "two", "model": "two"},
                ]})
            self.assertFalse(path.exists())

    def test_direct_api_loop_config_single_route_is_atomic(self):
        with tempfile.TemporaryDirectory() as root:
            env = render_env(Path(root))
            env.update({"API_LOOP_INSTANCE_NONCE": "nonce", "LOOP_STREAM": "false"})
            with mock.patch.dict(os.environ, env, clear=True):
                sys.modules.pop("examples.api_loop", None)
                module = importlib.import_module("examples.api_loop")
            result = module.update_config({"main_chain": [
                {"url": "https://one.invalid", "key": "one", "model": "one"}
            ]})
            self.assertEqual(len(result["main_chain"]), 1)
            self.assertEqual(list(Path(root).glob(".api_loop.config.json.*")), [])

    def test_webhook_helper_uses_injected_transport_and_safe_failures(self):
        captured = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, _limit): return b'{"ok":true}'

        def opener(request, timeout):
            captured.append((request, timeout))
            return Response()

        configure_telegram_webhook.configure_webhook(
            "invalid-test-token", "valid_webhook-secret", "https://service.invalid/integrations/telegram/webhook",
            opener=opener,
        )
        self.assertEqual(len(captured), 1)
        with self.assertRaisesRegex(ValueError, "invalid_webhook_secret"):
            configure_telegram_webhook.configure_webhook(
                "invalid-test-token", "bad secret", "https://service.invalid/hook", opener=opener
            )


class BlueprintTests(unittest.TestCase):
    def test_render_blueprint_structure_and_secret_placeholders(self):
        blueprint = json.loads((Path(__file__).parents[2] / "render.yaml").read_text(encoding="utf-8"))
        self.assertEqual(len(blueprint["services"]), 1)
        service = blueprint["services"][0]
        self.assertEqual((service["type"], service["runtime"], service["plan"]), ("web", "python", "starter"))
        self.assertEqual(service["numInstances"], 1)
        self.assertEqual(service["buildCommand"], "python -m pip install -r backend/requirements.txt")
        self.assertEqual(service["startCommand"], "python scripts/render_start.py")
        self.assertEqual(service["healthCheckPath"], "/healthz")
        self.assertIs(service["autoDeploy"], False)
        self.assertEqual(service["branch"], "feat/render-telegram-deployment")
        self.assertEqual(service["disk"]["mountPath"], "/var/data")
        self.assertNotIn("maxShutdownDelaySeconds", service)
        env = {item["key"]: item for item in service["envVars"]}
        self.assertEqual(env["PYTHON_VERSION"]["value"], "3.12.11")
        shutdown_grace = env["SUPERVISOR_SHUTDOWN_GRACE_SECONDS"]["value"]
        self.assertEqual(shutdown_grace, "10")
        self.assertGreater(float(shutdown_grace), 0)
        for key in (
            "RELAY_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET",
            "CHANNEL_AUDIT_HMAC_SECRET", "TELEGRAM_BOT_ACCOUNT_ID",
            "TELEGRAM_ALLOWED_USER_IDS", "TELEGRAM_ALLOWED_CHAT_IDS",
            "LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL",
        ):
            self.assertEqual(env[key], {"key": key, "sync": False})


if __name__ == "__main__":
    unittest.main()
