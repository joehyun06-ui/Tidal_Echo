#!/usr/bin/env python3
"""Supervise the private api_loop and public relay in one Render instance."""

from __future__ import annotations

import os
import http.client
import json
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend import deployment_config
from backend.deployment_config import (
    DeploymentConfig,
    DeploymentConfigError,
    initialize_brain_target,
    load_deployment_config,
)
from backend.telegram_integration import TelegramConfig


@dataclass(frozen=True)
class SupervisorConfig:
    deployment: DeploymentConfig
    relay_port: int
    loop_ready_timeout: float
    shutdown_grace: float
    instance_nonce: str


def preflight(environ: Mapping[str, str] | None = None) -> SupervisorConfig:
    env = os.environ if environ is None else environ
    telegram = TelegramConfig.from_env(env)
    deployment = load_deployment_config(telegram, env)
    relay_port = deployment_config.parse_port(env.get("PORT", ""), "invalid_relay_port")
    if relay_port == deployment.loop_port:
        raise DeploymentConfigError("relay_and_loop_ports_must_differ")
    loop_ready_timeout = deployment_config.parse_positive_finite_float(
        env.get("SUPERVISOR_LOOP_READY_TIMEOUT_SECONDS", "15"), "invalid_loop_ready_timeout"
    )
    shutdown_grace = deployment_config.parse_positive_finite_float(
        env.get("SUPERVISOR_SHUTDOWN_GRACE_SECONDS", "10"), "invalid_shutdown_grace"
    )
    deployment_config.prepare_persistent_paths(deployment)
    initialize_brain_target(deployment)
    return SupervisorConfig(deployment, relay_port, loop_ready_timeout, shutdown_grace, secrets.token_urlsafe(32))


def child_environment(config: SupervisorConfig, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    env.pop("API_LOOP_INSTANCE_NONCE", None)
    env.pop("API_LOOP_EXPECTED_NONCE", None)
    env["RELAY_URL"] = f"http://127.0.0.1:{config.relay_port}"
    env["RELAY_PORT"] = str(config.relay_port)
    env["LOOP_PORT"] = str(config.deployment.loop_port)
    return env


def child_commands(config: SupervisorConfig, executable: str | None = None) -> dict[str, list[str]]:
    python = executable or sys.executable
    return {
        "api_loop": [
            python, "-m", "uvicorn", "examples.api_loop:app", "--host", "127.0.0.1",
            "--port", str(config.deployment.loop_port), "--workers", "1",
        ],
        "relay": [
            python, "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0",
            "--port", str(config.relay_port), "--workers", "1",
        ],
    }


def safe_log(process: str, pid: int | None, state: str, category: str = "") -> None:
    fields = [f"process={process}", f"pid={pid if pid is not None else '-'}", f"state={state}"]
    if category:
        fields.append(f"category={category}")
    print("[render-supervisor] " + " ".join(fields), file=sys.stderr, flush=True)


def api_loop_health_probe(port: int, expected_nonce: str, timeout: float) -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            return False
        raw = response.read(65537)
        if len(raw) > 65536:
            return False
        payload = json.loads(raw.decode("utf-8"))
        return (
            isinstance(payload, dict)
            and payload.get("ok") is True
            and payload.get("service") == "api_loop"
            and isinstance(payload.get("instance_nonce"), str)
            and secrets.compare_digest(payload["instance_nonce"], expected_nonce)
        )
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, http.client.HTTPException):
        return False
    finally:
        connection.close()


class RenderSupervisor:
    def __init__(
        self,
        *,
        popen: Callable[..., subprocess.Popen] | None = None,
        health_probe: Callable[[int, str, float], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        logger: Callable[[str, int | None, str, str], None] = safe_log,
    ) -> None:
        self._popen = popen or subprocess.Popen
        self._health_probe = health_probe or api_loop_health_probe
        self._monotonic = monotonic
        self._sleep = sleep
        self._logger = logger
        self.processes: dict[str, subprocess.Popen] = {}
        self.stop_signal: int | None = None

    def handle_signal(self, signum: int, _frame=None) -> None:
        self.stop_signal = signum
        self._logger("supervisor", os.getpid(), "signal_received", signal.Signals(signum).name)

    def _start(self, name: str, command: list[str], env: Mapping[str, str]) -> subprocess.Popen:
        process = self._popen(command, env=dict(env), start_new_session=(os.name == "posix"))
        self.processes[name] = process
        self._logger(name, process.pid, "started", "")
        return process

    def _wait_for_loop(self, process: subprocess.Popen, port: int, nonce: str, timeout: float) -> None:
        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            if self.stop_signal is not None:
                raise DeploymentConfigError("supervisor_stopping")
            if process.poll() is not None:
                raise DeploymentConfigError("api_loop_exited_before_ready")
            if not self._health_probe(port, nonce, min(0.5, max(0.05, deadline - self._monotonic()))):
                if process.poll() is not None:
                    raise DeploymentConfigError("api_loop_exited_before_ready")
                self._sleep(0.1)
                continue
            if process.poll() is not None:
                raise DeploymentConfigError("api_loop_exited_before_ready")
            if self.stop_signal is not None:
                raise DeploymentConfigError("supervisor_stopping")
            self._logger("api_loop", process.pid, "ready", "")
            return
        raise DeploymentConfigError("api_loop_readiness_timeout")

    @staticmethod
    def _send_signal(process: subprocess.Popen, signum: int) -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signum)
        else:
            process.send_signal(signum)

    @staticmethod
    def _force_kill(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()

    def shutdown(self, grace: float) -> None:
        signum = self.stop_signal or signal.SIGTERM
        for name, process in self.processes.items():
            if process.poll() is None:
                self._logger(name, process.pid, "stopping", signal.Signals(signum).name)
                try:
                    self._send_signal(process, signum)
                except (ProcessLookupError, PermissionError, OSError):
                    self._logger(name, process.pid, "cleanup_continued", "signal_failed")
        deadline = self._monotonic() + grace
        while self._monotonic() < deadline and any(p.poll() is None for p in self.processes.values()):
            self._sleep(0.1)
        for name, process in self.processes.items():
            if process.poll() is None:
                self._logger(name, process.pid, "killing", "grace_timeout")
                try:
                    self._force_kill(process)
                except (ProcessLookupError, PermissionError, OSError):
                    self._logger(name, process.pid, "cleanup_continued", "kill_failed")
        for process in self.processes.values():
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def run(self, config: SupervisorConfig, environ: Mapping[str, str] | None = None) -> int:
        commands = child_commands(config)
        env = child_environment(config, environ)
        result = 0
        previous: dict[int, object] = {}
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, self.handle_signal)
            api_env = dict(env)
            api_env["API_LOOP_INSTANCE_NONCE"] = config.instance_nonce
            relay_env = dict(env)
            relay_env["API_LOOP_EXPECTED_NONCE"] = config.instance_nonce
            loop_process = self._start("api_loop", commands["api_loop"], api_env)
            self._wait_for_loop(loop_process, config.deployment.loop_port, config.instance_nonce, config.loop_ready_timeout)
            self._start("relay", commands["relay"], relay_env)
            while self.stop_signal is None:
                failed = next(
                    ((name, process) for name, process in self.processes.items() if process.poll() is not None),
                    None,
                )
                if failed:
                    name, process = failed
                    self._logger(name, process.pid, "exited", "child_exit")
                    result = 1
                    break
                self._sleep(0.2)
        except DeploymentConfigError as exc:
            self._logger("supervisor", os.getpid(), "startup_failed", exc.category)
            result = 0 if self.stop_signal is not None else 1
        except Exception:
            self._logger("supervisor", os.getpid(), "startup_failed", "unexpected_error")
            result = 1
        finally:
            try:
                self.shutdown(config.shutdown_grace)
            except Exception:
                self._logger("supervisor", os.getpid(), "cleanup_continued", "unexpected_cleanup_error")
            for signum, handler in previous.items():
                try:
                    signal.signal(signum, handler)
                except Exception:
                    self._logger("supervisor", os.getpid(), "cleanup_continued", "signal_restore_failed")
        return result


def main() -> int:
    try:
        config = preflight()
    except DeploymentConfigError as exc:
        safe_log("supervisor", os.getpid(), "preflight_failed", exc.category)
        return 2
    except Exception:
        safe_log("supervisor", os.getpid(), "preflight_failed", "unexpected_error")
        return 2
    return RenderSupervisor().run(config)


if __name__ == "__main__":
    raise SystemExit(main())
