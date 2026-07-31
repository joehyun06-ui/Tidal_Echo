from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import sqlite3
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from backend import (
    channel_store,
    deployment_config,
    memory_explicit_actions,
    memory_operator_cli,
    memory_operator_composition,
    memory_policy,
    memory_runtime,
    memory_service,
    memory_store,
    telegram_integration,
)


TEST_KEY_ID = "operator-cli-synthetic-key"
TEST_SECRET = "Synthetic-Operator-Cli-HMAC-Key-2026!Z9q7"
OUTPUT_KEYS = (
    "ok",
    "request_id",
    "action",
    "status",
    "category",
    "memory_key",
    "replayed",
)
BUSINESS_TABLES = (
    "messages",
    "memory_action_requests",
    "memory_evidence_events",
    "memory_items",
    "memory_sources",
    "memory_suppressions",
)


def initialize_v8(path: Path) -> None:
    with channel_store.connect(str(path)) as conn:
        for statement in channel_store.RELAY_TABLE_DDL.values():
            conn.execute(statement)
    channel_store.run_migrations(str(path))


def operator_environment(path: Path, **overrides: str) -> dict[str, str]:
    values = {
        key: os.environ[key]
        for key in (
            "COMSPEC",
            "PATH",
            "PATHEXT",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "WINDIR",
        )
        if key in os.environ
    }
    values.update(
        {
            "TELEGRAM_ENABLED": "false",
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_EXPLICIT_WRITES_ENABLED": "true",
            "MEMORY_EXPLICIT_ENTRY_ENABLED": "true",
            "MEMORY_SENSITIVE_STORAGE_ENABLED": "false",
            "MEMORY_MAX_ITEM_CHARS": "1000",
            "MEMORY_FORGET_RETENTION_POLICY": "tombstone_without_content",
            "MEMORY_FINGERPRINT_KEY_ID": TEST_KEY_ID,
            "MEMORY_FINGERPRINT_HMAC_SECRET": TEST_SECRET,
            "RELAY_DB": str(path),
            "SQLITE_BUSY_TIMEOUT_SECONDS": "2",
        }
    )
    values.update(overrides)
    return values


def counts(path: Path) -> dict[str, int]:
    with channel_store.connect(str(path)) as conn:
        return {
            table: int(
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            )
            for table in BUSINESS_TABLES
        }


def snapshot(path: Path) -> tuple[str, tuple[tuple[str, int], ...]]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    current = counts(path)
    return digest, tuple((name, current[name]) for name in BUSINESS_TABLES)


class MemoryOperatorCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "base.sqlite3"
        initialize_v8(self.base)

    def copy_database(self, name: str) -> Path:
        path = self.root / f"{name}.sqlite3"
        shutil.copyfile(self.base, path)
        return path

    def run_cli(
        self,
        command: str,
        *,
        path: Path | None = None,
        payload: object | None = None,
        raw: bytes | None = None,
        extra_argv: tuple[str, ...] = (),
        environment: dict[str, str] | None = None,
        worker: bool = False,
        timeout: float = 30,
    ) -> tuple[subprocess.CompletedProcess[bytes], dict[str, object]]:
        if raw is None:
            raw = (
                b""
                if payload is None
                else json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        env = (
            operator_environment(path or self.base)
            if environment is None
            else environment.copy()
        )
        module = (
            "backend.tests._memory_operator_cli_worker"
            if worker
            else "backend.memory_operator_cli"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                module,
                command,
                *extra_argv,
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            input=raw,
            capture_output=True,
            timeout=timeout,
        )
        lines = completed.stdout.decode("utf-8", errors="strict").splitlines()
        self.assertEqual(len(lines), 1, completed.stdout)
        result = json.loads(lines[0])
        self.assertEqual(tuple(result), OUTPUT_KEYS)
        return completed, result

    def invoke_main(
        self,
        command: str,
        *,
        raw: bytes = b"",
        environment: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[bytes], dict[str, object]]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {} if environment is None else environment,
            clear=True,
        ):
            returncode = memory_operator_cli.main(
                [command],
                stdin=io.BytesIO(raw),
                stdout=stdout,
                stderr=stderr,
            )
        completed = subprocess.CompletedProcess(
            [command],
            returncode,
            stdout.getvalue().encode("utf-8"),
            stderr.getvalue().encode("utf-8"),
        )
        lines = completed.stdout.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1, completed.stdout)
        result = json.loads(lines[0])
        self.assertEqual(tuple(result), OUTPUT_KEYS)
        return completed, result

    def assert_success(
        self,
        completed: subprocess.CompletedProcess[bytes],
        result: dict[str, object],
        *,
        action: str,
        category: str,
        replayed: bool,
    ) -> None:
        self.assertEqual(
            completed.returncode,
            0,
            (completed.stdout, completed.stderr),
        )
        self.assertEqual(completed.stderr, b"")
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], action)
        self.assertEqual(result["category"], category)
        self.assertIs(result["replayed"], replayed)

        if action in {"remember", "correct", "forget"}:
            self.assertEqual(result["status"], "completed")
            self.assertRegex(
                str(result["request_id"]),
                r"^[A-Za-z0-9_-]{32,96}$",
            )
            if category == "suppressed":
                self.assertIsNone(result["memory_key"])
            else:
                self.assertRegex(
                    str(result["memory_key"]),
                    r"^[A-Za-z0-9_-]{32,96}$",
                )
            return

        self.assertIsNone(result["memory_key"])
        self.assertIs(result["replayed"], False)
        if action == "generate_request_id":
            self.assertEqual(result["status"], "generated")
            self.assertRegex(
                str(result["request_id"]),
                r"^[A-Za-z0-9_-]{32,96}$",
            )
        else:
            self.assertIsNone(result["request_id"])

    def assert_failure(
        self,
        completed: subprocess.CompletedProcess[bytes],
        result: dict[str, object],
        *,
        exit_code: int,
        category: str,
        action: str,
        forbidden: tuple[str, ...] = (),
    ) -> None:
        self.assertEqual(completed.returncode, exit_code)
        self.assertEqual(
            completed.stderr,
            (category + "\n").encode("ascii"),
        )
        self.assertEqual(
            result,
            {
                "ok": False,
                "request_id": None,
                "action": action,
                "status": "failed",
                "category": category,
                "memory_key": None,
                "replayed": False,
            },
        )
        public = completed.stdout + completed.stderr
        self.assertNotIn(b"Traceback", public)
        self.assertNotRegex(public.decode("ascii"), r"0x[0-9a-fA-F]+")
        for value in forbidden:
            self.assertNotIn(value.encode("utf-8"), public)

    @staticmethod
    def remember_payload(
        request_id: str,
        content: str,
        *,
        scope_type: str = "global_user",
        scope_ref: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "request_id": request_id,
            "kind": "project",
            "scope_type": scope_type,
            "content": content,
            "sensitivity": "normal",
        }
        if scope_ref is not None:
            payload["scope_ref"] = scope_ref
        return payload

    def forget(self, path: Path, memory_key: str, marker: str) -> None:
        completed, result = self.run_cli(
            "forget",
            path=path,
            payload={
                "request_id": marker * 32,
                "memory_key": memory_key,
            },
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn(result["category"], {"forgotten", "already_forgotten"})

    def test_import_isolated_from_app_fastapi_and_handlers(self):
        code = textwrap.dedent(
            """
            import json
            import sys
            import backend.memory_operator_cli
            print(json.dumps({
                "app": "backend.app" in sys.modules,
                "fastapi": "fastapi" in sys.modules,
                "kelivo": "backend.kelivo_service" in sys.modules,
            }, sort_keys=True))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            {"app": False, "fastapi": False, "kelivo": False},
        )
        self.assertEqual(completed.stderr, "")
        cli_source = (
            Path(memory_operator_cli.__file__).read_text(encoding="utf-8")
        )
        for forbidden in (
            "memory_store",
            "PrivilegedMemoryActions",
            "_PROCESS_AUTHORITY",
            "_action_unit_of_work",
            "backend.app",
        ):
            self.assertNotIn(forbidden, cli_source)
        app_source = (
            Path(__file__).resolve().parents[1] / "app.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("memory_operator_cli", app_source)

    def test_main_contract_has_fixed_json_stderr_and_category_mapping(self):
        expected_map = {
            "invalid_request": "input_invalid",
            "invalid_memory_key": "input_invalid",
            "invalid_content": "input_invalid",
            "content_too_long": "input_invalid",
            "empty_content": "input_invalid",
            "invalid_scope": "input_invalid",
            "invalid_kind": "input_invalid",
            "invalid_sensitivity": "input_invalid",
            "secret_detected": "input_invalid",
            "sensitivity_downgrade": "input_invalid",
            "sensitive_storage_disabled": "input_invalid",
            "forbidden_test_content": "input_invalid",
            "forbidden_log_content": "input_invalid",
            "technical_identifier_forbidden": "input_invalid",
            "invalid_state": "unsupported_action",
            "conflict": "request_binding_conflict",
            "request_binding_conflict": "request_binding_conflict",
            "not_found": "not_found",
            "unsupported_evidence": "unsupported_action",
            "storage_unavailable": "storage_unavailable",
            "transaction_outcome_uncertain": (
                "transaction_outcome_uncertain"
            ),
            "feature_disabled": "readiness_failed",
            "explicit_writes_disabled": "readiness_failed",
            "memory_configuration_invalid": "readiness_failed",
            "memory_schema_invalid": "readiness_failed",
            "memory_fingerprint_profile_mismatch": "readiness_failed",
        }
        self.assertEqual(memory_operator_cli._ACTION_CATEGORY_MAP, expected_map)
        self.assertEqual(
            memory_operator_cli._PUBLIC_EXIT_CODES,
            {
                "internal_error": 1,
                "input_invalid": 2,
                "readiness_failed": 3,
                "request_binding_conflict": 4,
                "not_found": 5,
                "unsupported_action": 5,
                "storage_unavailable": 6,
                "transaction_outcome_uncertain": 7,
            },
        )
        for internal, public in expected_map.items():
            with self.subTest(internal=internal):
                failure = memory_operator_cli._action_failure(internal)
                self.assertEqual(failure.category, public)
        self.assertEqual(
            memory_operator_cli._action_failure("unknown").category,
            "internal_error",
        )
        for internal in memory_operator_cli._COMPOSITION_READINESS_CATEGORIES:
            with self.subTest(composition=internal):
                self.assertEqual(
                    memory_operator_cli._composition_failure(
                        internal
                    ).category,
                    "readiness_failed",
                )
        for internal in memory_operator_cli._COMPOSITION_STORAGE_CATEGORIES:
            with self.subTest(composition=internal):
                self.assertEqual(
                    memory_operator_cli._composition_failure(
                        internal
                    ).category,
                    "storage_unavailable",
                )
        self.assertEqual(
            memory_operator_cli._composition_failure(
                "unknown_internal"
            ).category,
            "internal_error",
        )
        for internal in (
            "entry_composition_invalid",
            "memory_operation_failed",
            "terminal_semantics_invalid",
            "memory_runtime_already_initialized",
            "runtime_authority_invalid",
            "memory_operator_composition_failed",
        ):
            with self.subTest(internal_error=internal):
                self.assertEqual(
                    memory_operator_cli._composition_failure(
                        internal
                    ).category,
                    "internal_error",
                )
                self.assertEqual(
                    memory_operator_cli._action_failure(internal).category,
                    "internal_error",
                )

    def test_status_is_config_only_and_database_need_not_exist(self):
        missing = self.root / "status-missing.sqlite3"
        environment = operator_environment(missing)
        completed, result = self.run_cli(
            "status",
            environment=environment,
            raw=b" \r\n\t",
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(
            result,
            {
                "ok": True,
                "request_id": None,
                "action": "status",
                "status": "configured",
                "category": "configured",
                "memory_key": None,
                "replayed": False,
            },
        )
        self.assertFalse(missing.exists())

    def test_status_and_generate_do_not_open_storage_or_construct_runtime(self):
        environment = operator_environment(
            self.root / "must-not-open.sqlite3"
        )
        telegram = telegram_integration.TelegramConfig.from_env(environment)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                channel_store,
                "connect",
                side_effect=AssertionError("storage opened"),
            ),
            mock.patch.object(
                channel_store,
                "connect_read_only",
                side_effect=AssertionError("storage opened"),
            ),
            mock.patch.object(
                memory_runtime,
                "_bootstrap_memory_runtime_scope",
                side_effect=AssertionError("runtime constructed"),
            ),
            mock.patch.dict(os.environ, environment, clear=True),
        ):
            status_exit = memory_operator_cli.main(
                ["status"],
                stdin=io.BytesIO(b""),
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(status_exit, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            telegram.requested,
            False,
        )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                channel_store,
                "connect",
                side_effect=AssertionError("storage opened"),
            ),
            mock.patch.object(
                channel_store,
                "connect_read_only",
                side_effect=AssertionError("storage opened"),
            ),
            mock.patch.object(
                memory_operator_composition,
                "compose_operator_memory_service_from_environment",
                side_effect=AssertionError("runtime constructed"),
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            generated_exit = memory_operator_cli.main(
                ["generate-request-id"],
                stdin=io.BytesIO(b"\n"),
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(generated_exit, 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_validate_is_read_only_and_does_not_bootstrap_runtime(self):
        path = self.copy_database("validate")
        before = snapshot(path)
        completed, result = self.run_cli(
            "validate",
            path=path,
            raw=b"",
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(
            result,
            {
                "ok": True,
                "request_id": None,
                "action": "validate",
                "status": "ready",
                "category": "ready",
                "memory_key": None,
                "replayed": False,
            },
        )
        self.assertEqual(snapshot(path), before)

        missing = self.root / "validate-missing.sqlite3"
        completed, result = self.run_cli(
            "validate",
            environment=operator_environment(missing),
        )
        self.assert_failure(
            completed,
            result,
            exit_code=6,
            category="storage_unavailable",
            action="validate",
            forbidden=(str(missing), TEST_SECRET, TEST_KEY_ID),
        )
        self.assertFalse(missing.exists())

    def test_write_uses_one_environment_snapshot_and_only_operator_service(self):
        path = self.copy_database("formal-composition-only")
        environment = operator_environment(path)
        request_id = "U" * 32
        memory_key = "V" * 32
        fake_result = memory_explicit_actions.ExplicitMemoryActionResult(
            request_id=request_id,
            action_kind="remember",
            status="completed",
            category="created",
            memory_key=memory_key,
            kind="project",
            scope_type="global_user",
            sensitivity="normal",
            replayed=False,
        )
        service = mock.Mock()
        service.remember_explicit_user_memory.return_value = fake_result
        captured: dict[str, object] = {}
        real_from_env = telegram_integration.TelegramConfig.from_env

        def telegram_from_env(environ):
            captured["telegram_environ"] = environ
            return real_from_env(environ)

        def compose(telegram_config, environ):
            captured["compose_environ"] = environ
            captured["telegram_config"] = telegram_config
            return service

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                telegram_integration.TelegramConfig,
                "from_env",
                side_effect=telegram_from_env,
            ),
            mock.patch.object(
                memory_operator_composition,
                "compose_operator_memory_service_from_environment",
                side_effect=compose,
            ) as composition,
            mock.patch.object(
                memory_operator_composition,
                "preflight_operator_memory_from_environment",
                side_effect=AssertionError("separate preflight forbidden"),
            ) as preflight,
            mock.patch.object(
                channel_store,
                "run_migrations",
                side_effect=AssertionError("migration forbidden"),
            ) as migrations,
            mock.patch.object(
                channel_store,
                "recover_inflight_generations",
                side_effect=AssertionError("recovery forbidden"),
            ) as generation_recovery,
            mock.patch.object(
                channel_store,
                "recover_inflight_deliveries",
                side_effect=AssertionError("recovery forbidden"),
            ) as delivery_recovery,
            mock.patch.object(
                deployment_config,
                "prepare_persistent_paths",
                side_effect=AssertionError("path preparation forbidden"),
            ) as prepare_paths,
            mock.patch.object(
                deployment_config,
                "initialize_brain_target",
                side_effect=AssertionError("brain initialization forbidden"),
            ) as brain,
            mock.patch.object(
                socket.socket,
                "connect",
                side_effect=AssertionError("network forbidden"),
            ) as network,
            mock.patch.dict(os.environ, environment, clear=True),
        ):
            exit_code = memory_operator_cli.main(
                ["remember"],
                stdin=io.BytesIO(
                    json.dumps(
                        self.remember_payload(
                            request_id,
                            "Synthetic formal composition content",
                        )
                    ).encode()
                ),
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            captured["telegram_environ"],
            {"TELEGRAM_ENABLED": "false"},
        )
        self.assertIsNotNone(captured["compose_environ"])
        self.assertEqual(captured["compose_environ"], environment)
        telegram_config = captured["telegram_config"]
        self.assertIs(type(telegram_config), telegram_integration.TelegramConfig)
        self.assertIs(telegram_config.requested, False)
        self.assertIs(telegram_config.enabled, False)
        self.assertEqual(telegram_config.error, "")
        composition.assert_called_once()
        preflight.assert_not_called()
        migrations.assert_not_called()
        generation_recovery.assert_not_called()
        delivery_recovery.assert_not_called()
        prepare_paths.assert_not_called()
        brain.assert_not_called()
        network.assert_not_called()
        service.remember_explicit_user_memory.assert_called_once()

    def test_generate_request_id_is_offline_unique_and_well_formed(self):
        values = []
        environment = operator_environment(
            self.root / "must-not-be-opened.sqlite3",
            MEMORY_CORE_ENABLED="false",
            MEMORY_EXPLICIT_WRITES_ENABLED="false",
            MEMORY_EXPLICIT_ENTRY_ENABLED="false",
        )
        for _index in range(2):
            completed, result = self.run_cli(
                "generate-request-id",
                environment=environment,
                raw=b"\n",
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stderr, b"")
            self.assertEqual(result["action"], "generate_request_id")
            self.assertEqual(result["status"], "generated")
            self.assertEqual(result["category"], "generated")
            self.assertRegex(result["request_id"], r"\A[A-Za-z0-9_-]{32,96}\Z")
            values.append(result["request_id"])
        self.assertEqual(len(set(values)), 2)

    def test_all_commands_ignore_custom_telegram_base_without_network(self):
        path = self.copy_database("network-free")
        environment = operator_environment(
            path,
            TELEGRAM_API_BASE="https://review.example.invalid",
            TELEGRAM_API_BASE_ALLOWLIST="https://review.example.invalid",
            TELEGRAM_TEST_MODE="false",
        )
        request_ids = {
            "remember": "R" * 32,
            "correct": "C" * 32,
            "forget": "F" * 32,
        }
        memory_key = "a" * 64
        service = mock.Mock()
        for command, category in (
            ("remember", "created"),
            ("correct", "corrected"),
            ("forget", "forgotten"),
        ):
            getattr(
                service,
                {
                    "remember": "remember_explicit_user_memory",
                    "correct": "correct_explicit_user_memory",
                    "forget": "forget_explicit_user_memory",
                }[command],
            ).return_value = (
                memory_explicit_actions.ExplicitMemoryActionResult(
                    request_id=request_ids[command],
                    action_kind=command,
                    status="completed",
                    category=category,
                    memory_key=memory_key,
                    kind="project",
                    scope_type="global_user",
                    sensitivity="normal",
                    replayed=False,
                )
            )
        real_from_env = telegram_integration.TelegramConfig.from_env
        telegram_environments: list[dict[str, str]] = []

        def telegram_from_env(
            environ: dict[str, str],
        ) -> telegram_integration.TelegramConfig:
            telegram_environments.append(dict(environ))
            return real_from_env(environ)

        forbidden = AssertionError("network/provider/outbox forbidden")
        with (
            mock.patch.object(
                telegram_integration.TelegramConfig,
                "from_env",
                side_effect=telegram_from_env,
            ) as telegram_config,
            mock.patch.object(
                memory_operator_composition,
                "compose_operator_memory_service_from_environment",
                return_value=service,
            ) as composition,
            mock.patch.object(
                socket,
                "getaddrinfo",
                side_effect=forbidden,
            ) as getaddrinfo,
            mock.patch.object(
                socket,
                "create_connection",
                side_effect=forbidden,
            ) as create_connection,
            mock.patch.object(
                socket.socket,
                "connect",
                side_effect=forbidden,
            ) as connect,
            mock.patch.object(
                socket.socket,
                "connect_ex",
                side_effect=forbidden,
            ) as connect_ex,
            mock.patch.object(
                urllib.request,
                "urlopen",
                side_effect=forbidden,
            ) as urlopen,
            mock.patch.object(
                telegram_integration.TelegramClient,
                "send_part",
                side_effect=forbidden,
            ) as telegram_send,
            mock.patch.object(
                channel_store,
                "enqueue_telegram_update",
                side_effect=forbidden,
            ) as enqueue_outbox,
        ):
            completed, result = self.invoke_main(
                "generate-request-id",
                raw=b" \n",
                environment=environment,
            )
            self.assert_success(
                completed,
                result,
                action="generate_request_id",
                category="generated",
                replayed=False,
            )
            for command, category in (
                ("status", "configured"),
                ("validate", "ready"),
            ):
                completed, result = self.invoke_main(
                    command,
                    environment=environment,
                )
                self.assert_success(
                    completed,
                    result,
                    action=command,
                    category=category,
                    replayed=False,
                )

            payloads = {
                "remember": self.remember_payload(
                    request_ids["remember"],
                    "Synthetic custom Telegram base content",
                ),
                "correct": {
                    "request_id": request_ids["correct"],
                    "memory_key": memory_key,
                    "replacement_content": "Synthetic network-free replacement",
                    "sensitivity": "normal",
                },
                "forget": {
                    "request_id": request_ids["forget"],
                    "memory_key": memory_key,
                },
            }
            for command, category in (
                ("remember", "created"),
                ("correct", "corrected"),
                ("forget", "forgotten"),
            ):
                completed, result = self.invoke_main(
                    command,
                    raw=json.dumps(payloads[command]).encode("utf-8"),
                    environment=environment,
                )
                self.assert_success(
                    completed,
                    result,
                    action=command,
                    category=category,
                    replayed=False,
                )

            completed, result = self.invoke_main(
                "remember",
                raw=b"{}",
                environment=environment,
            )
            self.assert_failure(
                completed,
                result,
                exit_code=2,
                category="input_invalid",
                action="remember",
            )

        self.assertEqual(
            telegram_environments,
            [{"TELEGRAM_ENABLED": "false"}] * 5,
        )
        self.assertEqual(telegram_config.call_count, 5)
        self.assertEqual(composition.call_count, 3)
        for entrypoint in (
            getaddrinfo,
            create_connection,
            connect,
            connect_ex,
            urlopen,
            telegram_send,
            enqueue_outbox,
        ):
            entrypoint.assert_not_called()

    def test_generate_is_environment_independent_and_bad_telegram_flags_fail_closed(
        self,
    ):
        path = self.copy_database("telegram-flag-matrix")
        remember = json.dumps(
            self.remember_payload(
                "W" * 32,
                "Synthetic Telegram flag matrix content",
            )
        ).encode("utf-8")
        for telegram_value in ("not-a-bool", "true"):
            with self.subTest(telegram_enabled=telegram_value):
                environment = operator_environment(
                    path,
                    TELEGRAM_ENABLED=telegram_value,
                    TELEGRAM_API_BASE="https://review.example.invalid",
                    TELEGRAM_API_BASE_ALLOWLIST=(
                        "https://review.example.invalid"
                    ),
                    MEMORY_CORE_ENABLED="malformed",
                    MEMORY_EXPLICIT_WRITES_ENABLED="malformed",
                    MEMORY_FINGERPRINT_HMAC_SECRET="",
                    RELAY_DB=str(self.root / "does-not-exist.sqlite3"),
                    KELIVO_ENABLED="malformed",
                    LOOP_PORT="malformed",
                    HEARTBEAT_ENABLED="malformed",
                )
                before = snapshot(path)
                issued = "G" * 32
                forbidden = AssertionError("network forbidden")
                with (
                    mock.patch.object(
                        memory_explicit_actions,
                        "issue_request_id",
                        return_value=issued,
                    ) as issuer,
                    mock.patch.object(
                        telegram_integration.TelegramConfig,
                        "from_env",
                        side_effect=AssertionError(
                            "TelegramConfig forbidden"
                        ),
                    ) as telegram_config,
                    mock.patch.object(
                        socket,
                        "getaddrinfo",
                        side_effect=forbidden,
                    ) as getaddrinfo,
                    mock.patch.object(
                        socket.socket,
                        "connect",
                        side_effect=forbidden,
                    ) as connect,
                    mock.patch.object(
                        urllib.request,
                        "urlopen",
                        side_effect=forbidden,
                    ) as urlopen,
                    mock.patch.object(
                        memory_operator_composition,
                        "compose_operator_memory_service_from_environment",
                        side_effect=AssertionError(
                            "composition forbidden"
                        ),
                    ) as composition,
                ):
                    completed, result = self.invoke_main(
                        "generate-request-id",
                        raw=b"\n",
                        environment=environment,
                    )
                    self.assert_success(
                        completed,
                        result,
                        action="generate_request_id",
                        category="generated",
                        replayed=False,
                    )
                    self.assertEqual(result["request_id"], issued)
                    issuer.assert_called_once_with()

                    for command, raw in (
                        ("status", b""),
                        ("validate", b""),
                        ("remember", remember),
                    ):
                        completed, result = self.invoke_main(
                            command,
                            raw=raw,
                            environment=environment,
                        )
                        self.assert_failure(
                            completed,
                            result,
                            exit_code=3,
                            category="readiness_failed",
                            action=command,
                        )

                telegram_config.assert_not_called()
                composition.assert_not_called()
                getaddrinfo.assert_not_called()
                connect.assert_not_called()
                urlopen.assert_not_called()
                self.assertEqual(snapshot(path), before)

    def test_operator_telegram_disabled_contract_fails_closed(self):
        formal = telegram_integration.TelegramConfig.from_env(
            {"TELEGRAM_ENABLED": "false"}
        )
        invalid_configs = (
            object(),
            dataclasses.replace(formal, requested=True),
            dataclasses.replace(formal, enabled=True),
            dataclasses.replace(formal, error="invalid"),
        )
        for index, invalid_config in enumerate(invalid_configs):
            with self.subTest(index=index):
                with (
                    mock.patch.object(
                        telegram_integration.TelegramConfig,
                        "from_env",
                        return_value=invalid_config,
                    ) as from_env,
                    mock.patch.object(
                        deployment_config,
                        "load_deployment_config",
                        side_effect=AssertionError(
                            "deployment config must not load"
                        ),
                    ) as load_config,
                ):
                    completed, result = self.invoke_main(
                        "status",
                        environment={"TELEGRAM_ENABLED": "false"},
                    )
                self.assert_failure(
                    completed,
                    result,
                    exit_code=1,
                    category="internal_error",
                    action="status",
                )
                from_env.assert_called_once_with(
                    {"TELEGRAM_ENABLED": "false"}
                )
                load_config.assert_not_called()

    def test_generate_does_not_snapshot_environment(self):
        class ForbiddenEnvironment:
            def __iter__(self):
                raise AssertionError("environment iterated")

            def __getitem__(self, _key):
                raise AssertionError("environment indexed")

            def get(self, _key, _default=None):
                raise AssertionError("environment read")

            def keys(self):
                raise AssertionError("environment keys read")

        stdout = io.StringIO()
        stderr = io.StringIO()
        issued = "Z" * 32
        with (
            mock.patch.object(
                memory_operator_cli.os,
                "environ",
                ForbiddenEnvironment(),
            ),
            mock.patch.object(
                memory_explicit_actions,
                "issue_request_id",
                return_value=issued,
            ) as issuer,
            mock.patch.object(
                telegram_integration.TelegramConfig,
                "from_env",
                side_effect=AssertionError("Telegram config constructed"),
            ) as telegram_config,
        ):
            exit_code = memory_operator_cli.main(
                ["generate-request-id"],
                stdin=io.BytesIO(b"\n"),
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["request_id"], issued)
        issuer.assert_called_once_with()
        telegram_config.assert_not_called()

    def test_real_remember_correct_forget_and_fresh_process_replays(self):
        path = self.copy_database("lifecycle")
        content = "Synthetic operator CLI lifecycle content"
        replacement = "Synthetic operator CLI replacement content"
        scope_sentinel = "scope-sentinel-not-used"
        before = counts(path)
        remember = self.remember_payload("A" * 32, content)
        completed, created = self.run_cli(
            "remember",
            path=path,
            payload=remember,
        )
        self.assert_success(
            completed,
            created,
            action="remember",
            category="created",
            replayed=False,
        )
        memory_key = created["memory_key"]
        self.assertRegex(memory_key, r"\A[A-Za-z0-9_-]{32,96}\Z")
        after_remember = counts(path)
        self.assertEqual(
            {
                table: after_remember[table] - before[table]
                for table in BUSINESS_TABLES
            },
            {
                "messages": 1,
                "memory_action_requests": 1,
                "memory_evidence_events": 1,
                "memory_items": 1,
                "memory_sources": 1,
                "memory_suppressions": 0,
            },
        )
        with channel_store.connect(str(path)) as conn:
            canonical = conn.execute(
                "SELECT meta FROM messages ORDER BY id DESC LIMIT 1"
            ).fetchone()
        metadata = json.loads(canonical["meta"])
        self.assertEqual(metadata["channel"], "web")
        self.assertEqual(metadata["source"], "relay")

        completed, replay = self.run_cli(
            "remember",
            path=path,
            payload=remember,
        )
        self.assert_success(
            completed,
            replay,
            action="remember",
            category="created",
            replayed=True,
        )
        self.assertEqual(replay["memory_key"], memory_key)
        self.assertEqual(counts(path), after_remember)

        completed, conflict = self.run_cli(
            "remember",
            path=path,
            payload=self.remember_payload(
                "A" * 32,
                "Different binding content",
            ),
        )
        self.assert_failure(
            completed,
            conflict,
            exit_code=4,
            category="request_binding_conflict",
            action="remember",
            forbidden=(
                content,
                replacement,
                scope_sentinel,
                TEST_SECRET,
                TEST_KEY_ID,
                str(path),
            ),
        )
        self.assertEqual(counts(path), after_remember)

        correct_request = {
            "request_id": "B" * 32,
            "memory_key": memory_key,
            "replacement_content": replacement,
            "sensitivity": "normal",
        }
        completed, corrected = self.run_cli(
            "correct",
            path=path,
            payload=correct_request,
        )
        self.assert_success(
            completed,
            corrected,
            action="correct",
            category="corrected",
            replayed=False,
        )
        replacement_key = corrected["memory_key"]
        self.assertNotEqual(replacement_key, memory_key)
        after_correct = counts(path)
        self.assertEqual(
            {
                table: after_correct[table] - after_remember[table]
                for table in BUSINESS_TABLES
            },
            {
                "messages": 1,
                "memory_action_requests": 1,
                "memory_evidence_events": 1,
                "memory_items": 1,
                "memory_sources": 1,
                "memory_suppressions": 1,
            },
        )

        completed, correct_replay = self.run_cli(
            "correct",
            path=path,
            payload=correct_request,
        )
        self.assert_success(
            completed,
            correct_replay,
            action="correct",
            category="corrected",
            replayed=True,
        )
        self.assertEqual(counts(path), after_correct)

        forget_request = {
            "request_id": "C" * 32,
            "memory_key": replacement_key,
        }
        completed, forgotten = self.run_cli(
            "forget",
            path=path,
            payload=forget_request,
        )
        self.assert_success(
            completed,
            forgotten,
            action="forget",
            category="forgotten",
            replayed=False,
        )
        after_forget = counts(path)
        self.assertEqual(
            {
                table: after_forget[table] - after_correct[table]
                for table in BUSINESS_TABLES
            },
            {
                "messages": 1,
                "memory_action_requests": 1,
                "memory_evidence_events": 1,
                "memory_items": 0,
                "memory_sources": 1,
                "memory_suppressions": 1,
            },
        )
        completed, forget_replay = self.run_cli(
            "forget",
            path=path,
            payload=forget_request,
        )
        self.assert_success(
            completed,
            forget_replay,
            action="forget",
            category="forgotten",
            replayed=True,
        )
        self.assertEqual(counts(path), after_forget)
        public = (
            completed.stdout.decode("utf-8")
            + completed.stderr.decode("utf-8")
        )
        for secret in (
            content,
            replacement,
            scope_sentinel,
            TEST_SECRET,
            TEST_KEY_ID,
            str(path),
        ):
            self.assertNotIn(secret, public)

    def test_real_success_category_matrix_has_exact_store_effects(self):
        path = self.copy_database("success-matrix")
        base_effect = {
            "messages": 1,
            "memory_action_requests": 1,
            "memory_evidence_events": 1,
            "memory_items": 0,
            "memory_sources": 1,
            "memory_suppressions": 0,
        }

        def execute(
            command: str,
            payload: dict[str, object],
            *,
            category: str,
            replayed: bool,
            evidence: int = 1,
            items: int = 0,
            sources: int = 1,
            suppressions: int = 0,
        ) -> dict[str, object]:
            before = counts(path)
            completed, result = self.run_cli(
                command,
                path=path,
                payload=payload,
            )
            self.assert_success(
                completed,
                result,
                action=command,
                category=category,
                replayed=replayed,
            )
            after = counts(path)
            expected = (
                {table: 0 for table in BUSINESS_TABLES}
                if replayed
                else {
                    **base_effect,
                    "memory_evidence_events": evidence,
                    "memory_items": items,
                    "memory_sources": sources,
                    "memory_suppressions": suppressions,
                }
            )
            self.assertEqual(
                {
                    table: after[table] - before[table]
                    for table in BUSINESS_TABLES
                },
                expected,
            )
            if not replayed:
                with channel_store.connect(str(path)) as connection:
                    canonical = connection.execute(
                        "SELECT meta FROM messages ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                self.assertEqual(
                    json.loads(canonical["meta"]),
                    {"channel": "web", "source": "relay"},
                )
            return result

        primary_request = self.remember_payload(
            "1" * 32,
            "Synthetic success matrix primary",
        )
        primary = execute(
            "remember",
            primary_request,
            category="created",
            replayed=False,
            items=1,
        )
        execute(
            "remember",
            primary_request,
            category="created",
            replayed=True,
        )
        existing = execute(
            "remember",
            self.remember_payload(
                "2" * 32,
                "Synthetic success matrix primary",
            ),
            category="idempotent_existing",
            replayed=False,
        )
        self.assertEqual(existing["memory_key"], primary["memory_key"])

        suppression_seed = execute(
            "remember",
            self.remember_payload(
                "3" * 32,
                "Synthetic success matrix suppressed remember",
            ),
            category="created",
            replayed=False,
            items=1,
        )
        execute(
            "forget",
            {
                "request_id": "4" * 32,
                "memory_key": suppression_seed["memory_key"],
            },
            category="forgotten",
            replayed=False,
            suppressions=1,
        )
        suppressed_remember_request = self.remember_payload(
            "5" * 32,
            "Synthetic success matrix suppressed remember",
        )
        execute(
            "remember",
            suppressed_remember_request,
            category="suppressed",
            replayed=False,
            evidence=0,
            sources=0,
        )
        execute(
            "remember",
            suppressed_remember_request,
            category="suppressed",
            replayed=True,
        )

        unchanged_target = execute(
            "remember",
            self.remember_payload(
                "6" * 32,
                "Synthetic success matrix unchanged",
            ),
            category="created",
            replayed=False,
            items=1,
        )
        unchanged_request = {
            "request_id": "7" * 32,
            "memory_key": unchanged_target["memory_key"],
            "replacement_content": "Synthetic success matrix unchanged",
            "sensitivity": "normal",
        }
        unchanged = execute(
            "correct",
            unchanged_request,
            category="unchanged",
            replayed=False,
        )
        self.assertEqual(
            unchanged["memory_key"],
            unchanged_target["memory_key"],
        )
        execute(
            "correct",
            unchanged_request,
            category="unchanged",
            replayed=True,
        )

        correction_target = execute(
            "remember",
            self.remember_payload(
                "8" * 32,
                "Synthetic success matrix correction target",
            ),
            category="created",
            replayed=False,
            items=1,
        )
        correction_request = {
            "request_id": "9" * 32,
            "memory_key": correction_target["memory_key"],
            "replacement_content": (
                "Synthetic success matrix corrected replacement"
            ),
            "sensitivity": "normal",
        }
        corrected = execute(
            "correct",
            correction_request,
            category="corrected",
            replayed=False,
            items=1,
            suppressions=1,
        )
        execute(
            "correct",
            correction_request,
            category="corrected",
            replayed=True,
        )

        for field, changed in (
            (
                "replacement_content",
                "Synthetic different correction binding",
            ),
            ("sensitivity", "sensitive"),
            ("memory_key", unchanged_target["memory_key"]),
        ):
            conflicting = dict(correction_request)
            conflicting[field] = changed
            before = counts(path)
            completed, result = self.run_cli(
                "correct",
                path=path,
                payload=conflicting,
                environment=(
                    operator_environment(
                        path,
                        MEMORY_SENSITIVE_STORAGE_ENABLED="true",
                    )
                    if field == "sensitivity"
                    else None
                ),
            )
            self.assert_failure(
                completed,
                result,
                exit_code=4,
                category="request_binding_conflict",
                action="correct",
            )
            self.assertEqual(counts(path), before)

        before = counts(path)
        completed, result = self.run_cli(
            "correct",
            path=path,
            payload={
                **correction_request,
                "request_id": "A" * 32,
            },
        )
        self.assert_failure(
            completed,
            result,
            exit_code=5,
            category="unsupported_action",
            action="correct",
        )
        self.assertEqual(counts(path), before)

        suppressed_target = execute(
            "remember",
            self.remember_payload(
                "B" * 32,
                "Synthetic suppressed correction target",
            ),
            category="created",
            replayed=False,
            items=1,
        )
        corrected_suppression_seed = execute(
            "remember",
            self.remember_payload(
                "C" * 32,
                "Synthetic corrected suppression content",
            ),
            category="created",
            replayed=False,
            items=1,
        )
        execute(
            "forget",
            {
                "request_id": "D" * 32,
                "memory_key": corrected_suppression_seed["memory_key"],
            },
            category="forgotten",
            replayed=False,
            suppressions=1,
        )
        suppressed_correct_request = {
            "request_id": "E" * 32,
            "memory_key": suppressed_target["memory_key"],
            "replacement_content": (
                "Synthetic corrected suppression content"
            ),
            "sensitivity": "normal",
        }
        execute(
            "correct",
            suppressed_correct_request,
            category="suppressed",
            replayed=False,
            evidence=0,
            sources=0,
        )
        execute(
            "correct",
            suppressed_correct_request,
            category="suppressed",
            replayed=True,
        )

        forgotten_target = execute(
            "remember",
            self.remember_payload(
                "F" * 32,
                "Synthetic forgotten target matrix",
            ),
            category="created",
            replayed=False,
            items=1,
        )
        forgotten_request = {
            "request_id": "G" * 32,
            "memory_key": forgotten_target["memory_key"],
        }
        execute(
            "forget",
            forgotten_request,
            category="forgotten",
            replayed=False,
            suppressions=1,
        )
        execute(
            "forget",
            forgotten_request,
            category="forgotten",
            replayed=True,
        )
        already_request = {
            "request_id": "H" * 32,
            "memory_key": forgotten_target["memory_key"],
        }
        execute(
            "forget",
            already_request,
            category="already_forgotten",
            replayed=False,
        )
        execute(
            "forget",
            already_request,
            category="already_forgotten",
            replayed=True,
        )

        for target_key, marker in (
            (correction_target["memory_key"], "I"),
            (forgotten_target["memory_key"], "J"),
        ):
            before = counts(path)
            completed, result = self.run_cli(
                "correct",
                path=path,
                payload={
                    "request_id": marker * 32,
                    "memory_key": target_key,
                    "replacement_content": (
                        "Synthetic stale target replacement"
                    ),
                    "sensitivity": "normal",
                },
            )
            self.assert_failure(
                completed,
                result,
                exit_code=5,
                category="unsupported_action",
                action="correct",
            )
            self.assertEqual(counts(path), before)

        before = counts(path)
        completed, result = self.run_cli(
            "forget",
            path=path,
            payload={
                "request_id": "K" * 32,
                "memory_key": "f" * 64,
            },
        )
        self.assert_failure(
            completed,
            result,
            exit_code=5,
            category="not_found",
            action="forget",
        )
        self.assertEqual(counts(path), before)

        for memory_key, marker in (
            (primary["memory_key"], "L"),
            (unchanged_target["memory_key"], "M"),
            (corrected["memory_key"], "N"),
            (suppressed_target["memory_key"], "O"),
        ):
            execute(
                "forget",
                {
                    "request_id": marker * 32,
                    "memory_key": memory_key,
                },
                category="forgotten",
                replayed=False,
                suppressions=1,
            )

        with channel_store.connect(str(path)) as connection:
            statuses = {
                row["memory_key"]: row["status"]
                for row in connection.execute(
                    "SELECT memory_key,status FROM memory_items"
                )
            }
        self.assertEqual(statuses[correction_target["memory_key"]], "superseded")
        self.assertEqual(statuses[suppressed_target["memory_key"]], "forgotten")
        self.assertEqual(statuses[corrected["memory_key"]], "forgotten")
        self.assertEqual(statuses[unchanged_target["memory_key"]], "forgotten")
        self.assertEqual(statuses[primary["memory_key"]], "forgotten")

    def test_not_found_and_unsupported_action_are_closed(self):
        path = self.copy_database("closed-errors")
        before = snapshot(path)
        completed, result = self.run_cli(
            "forget",
            path=path,
            payload={
                "request_id": "D" * 32,
                "memory_key": "M" * 32,
            },
        )
        self.assert_failure(
            completed,
            result,
            exit_code=5,
            category="not_found",
            action="forget",
            forbidden=(TEST_SECRET, TEST_KEY_ID, str(path)),
        )
        self.assertEqual(snapshot(path), before)

        stdout = io.StringIO()
        stderr = io.StringIO()
        service = mock.Mock()
        service.remember_explicit_user_memory.side_effect = (
            memory_explicit_actions.ExplicitMemoryActionError(
                "unsupported_evidence"
            )
        )
        environment = operator_environment(path)
        with (
            mock.patch.object(
                memory_operator_composition,
                "compose_operator_memory_service_from_environment",
                return_value=service,
            ),
            mock.patch.dict(os.environ, environment, clear=True),
        ):
            exit_code = memory_operator_cli.main(
                ["remember"],
                stdin=io.BytesIO(
                    json.dumps(
                        self.remember_payload(
                            "E" * 32,
                            "Synthetic unsupported mapping",
                        )
                    ).encode()
                ),
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(exit_code, 5)
        self.assertEqual(stderr.getvalue(), "unsupported_action\n")
        self.assertEqual(
            json.loads(stdout.getvalue())["category"],
            "unsupported_action",
        )

    def test_commit_failure_mapping_distinguishes_rollback_and_uncertain(self):
        for mode, category, exit_code, expected_messages in (
            ("before_commit", "storage_unavailable", 6, 0),
            ("after_commit", "transaction_outcome_uncertain", 7, 1),
        ):
            with self.subTest(mode=mode):
                path = self.copy_database(mode)
                before = counts(path)
                environment = operator_environment(path)
                environment["MEMORY_OPERATOR_CLI_TEST_INJECTION"] = mode
                content = f"Synthetic {mode} operator CLI content"
                completed, result = self.run_cli(
                    "remember",
                    path=path,
                    payload=self.remember_payload("F" * 32, content),
                    environment=environment,
                    worker=True,
                )
                self.assert_failure(
                    completed,
                    result,
                    exit_code=exit_code,
                    category=category,
                    action="remember",
                    forbidden=(
                        content,
                        TEST_SECRET,
                        TEST_KEY_ID,
                        str(path),
                    ),
                )
                after = counts(path)
                delta = {
                    table: after[table] - before[table]
                    for table in BUSINESS_TABLES
                }
                if expected_messages == 0:
                    self.assertTrue(all(value == 0 for value in delta.values()))
                else:
                    self.assertEqual(
                        delta,
                        {
                            "messages": 1,
                            "memory_action_requests": 1,
                            "memory_evidence_events": 1,
                            "memory_items": 1,
                            "memory_sources": 1,
                            "memory_suppressions": 0,
                        },
                    )
                    with channel_store.connect(str(path)) as conn:
                        row = conn.execute(
                            """SELECT memory_key FROM memory_items
                               WHERE status='active'"""
                        ).fetchone()
                    self.forget(path, row["memory_key"], "G")

    def test_same_request_concurrency_for_2_4_8_processes(self):
        for workers in (2, 4, 8):
            with self.subTest(workers=workers):
                path = self.copy_database(f"concurrent-{workers}")
                before = counts(path)
                payload = self.remember_payload(
                    str(workers) * 32,
                    f"Synthetic concurrent CLI memory {workers}",
                )
                raw = json.dumps(payload, separators=(",", ":")).encode()
                environment = operator_environment(path)
                processes = [
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-B",
                            "-m",
                            "backend.memory_operator_cli",
                            "remember",
                        ],
                        cwd=Path(__file__).resolve().parents[2],
                        env=environment,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    for _index in range(workers)
                ]
                results = []
                for process in processes:
                    stdout, stderr = process.communicate(raw, timeout=30)
                    self.assertEqual(process.returncode, 0, stderr)
                    self.assertEqual(stderr, b"")
                    results.append(json.loads(stdout))
                self.assertEqual(
                    sum(not result["replayed"] for result in results),
                    1,
                )
                self.assertEqual(
                    sum(result["replayed"] for result in results),
                    workers - 1,
                )
                self.assertEqual(
                    {result["memory_key"] for result in results},
                    {results[0]["memory_key"]},
                )
                after = counts(path)
                self.assertEqual(
                    {
                        table: after[table] - before[table]
                        for table in BUSINESS_TABLES
                    },
                    {
                        "messages": 1,
                        "memory_action_requests": 1,
                        "memory_evidence_events": 1,
                        "memory_items": 1,
                        "memory_sources": 1,
                        "memory_suppressions": 0,
                    },
                )
                self.forget(path, results[0]["memory_key"], "H")

    def test_same_correct_request_concurrency_for_2_4_8_processes(self):
        for workers, marker in ((2, "Q"), (4, "R"), (8, "S")):
            with self.subTest(workers=workers):
                path = self.copy_database(f"correct-concurrent-{workers}")
                completed, target = self.run_cli(
                    "remember",
                    path=path,
                    payload=self.remember_payload(
                        marker * 32,
                        f"Synthetic concurrent correct target {workers}",
                    ),
                )
                self.assert_success(
                    completed,
                    target,
                    action="remember",
                    category="created",
                    replayed=False,
                )
                before = counts(path)
                payload = {
                    "request_id": str(workers) * 32,
                    "memory_key": target["memory_key"],
                    "replacement_content": (
                        f"Synthetic concurrent correction {workers}"
                    ),
                    "sensitivity": "normal",
                }
                raw = json.dumps(payload, separators=(",", ":")).encode()
                environment = operator_environment(path)
                processes = [
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-B",
                            "-m",
                            "backend.memory_operator_cli",
                            "correct",
                        ],
                        cwd=Path(__file__).resolve().parents[2],
                        env=environment,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    for _index in range(workers)
                ]
                observed: list[
                    tuple[
                        subprocess.CompletedProcess[bytes],
                        dict[str, object],
                    ]
                ] = []
                for process in processes:
                    stdout, stderr = process.communicate(raw, timeout=30)
                    result = json.loads(stdout)
                    completed = subprocess.CompletedProcess(
                        ["correct"],
                        process.returncode,
                        stdout,
                        stderr,
                    )
                    self.assertEqual(len(stdout.splitlines()), 1)
                    observed.append((completed, result))
                results = [item[1] for item in observed]
                self.assertEqual(
                    sum(result["replayed"] is False for result in results),
                    1,
                )
                self.assertEqual(
                    sum(result["replayed"] is True for result in results),
                    workers - 1,
                )
                fresh = [
                    item for item in observed
                    if item[1]["replayed"] is False
                ]
                replays = [
                    item for item in observed
                    if item[1]["replayed"] is True
                ]
                self.assert_success(
                    fresh[0][0],
                    fresh[0][1],
                    action="correct",
                    category="corrected",
                    replayed=False,
                )
                for completed, result in replays:
                    self.assert_success(
                        completed,
                        result,
                        action="correct",
                        category="corrected",
                        replayed=True,
                    )
                replacement_keys = {
                    result["memory_key"] for result in results
                }
                self.assertEqual(len(replacement_keys), 1)
                replacement_key = replacement_keys.pop()
                after = counts(path)
                self.assertEqual(
                    {
                        table: after[table] - before[table]
                        for table in BUSINESS_TABLES
                    },
                    {
                        "messages": 1,
                        "memory_action_requests": 1,
                        "memory_evidence_events": 1,
                        "memory_items": 1,
                        "memory_sources": 1,
                        "memory_suppressions": 1,
                    },
                )
                with channel_store.connect(str(path)) as connection:
                    rows = {
                        row["memory_key"]: row["status"]
                        for row in connection.execute(
                            "SELECT memory_key,status FROM memory_items"
                        )
                    }
                self.assertEqual(
                    rows[target["memory_key"]],
                    "superseded",
                )
                self.assertEqual(rows[replacement_key], "active")
                self.forget(path, replacement_key, marker.lower())

    def test_same_correct_id_different_binding_concurrency_is_closed(self):
        path = self.copy_database("correct-binding-race")
        completed, target = self.run_cli(
            "remember",
            path=path,
            payload=self.remember_payload(
                "T" * 32,
                "Synthetic correct binding race target",
            ),
        )
        self.assert_success(
            completed,
            target,
            action="remember",
            category="created",
            replayed=False,
        )
        request_id = "U" * 32
        payloads = tuple(
            {
                "request_id": request_id,
                "memory_key": target["memory_key"],
                "replacement_content": (
                    f"Synthetic correct binding race replacement {suffix}"
                ),
                "sensitivity": "normal",
            }
            for suffix in ("alpha", "beta")
        )
        before = counts(path)
        environment = operator_environment(path)
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "backend.memory_operator_cli",
                    "correct",
                ],
                cwd=Path(__file__).resolve().parents[2],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _payload in payloads
        ]
        observed = []
        for process, payload in zip(processes, payloads, strict=True):
            stdout, stderr = process.communicate(
                json.dumps(payload, separators=(",", ":")).encode(),
                timeout=30,
            )
            observed.append((process.returncode, json.loads(stdout), stderr))
        self.assertEqual(sorted(item[0] for item in observed), [0, 4])
        success = next(item for item in observed if item[0] == 0)
        failure = next(item for item in observed if item[0] == 4)
        self.assert_success(
            subprocess.CompletedProcess(
                ["correct"],
                success[0],
                json.dumps(success[1]).encode(),
                success[2],
            ),
            success[1],
            action="correct",
            category="corrected",
            replayed=False,
        )
        self.assertEqual(
            failure[1],
            {
                "ok": False,
                "request_id": None,
                "action": "correct",
                "status": "failed",
                "category": "request_binding_conflict",
                "memory_key": None,
                "replayed": False,
            },
        )
        self.assertEqual(failure[2], b"request_binding_conflict\n")
        after = counts(path)
        self.assertEqual(
            {
                table: after[table] - before[table]
                for table in BUSINESS_TABLES
            },
            {
                "messages": 1,
                "memory_action_requests": 1,
                "memory_evidence_events": 1,
                "memory_items": 1,
                "memory_sources": 1,
                "memory_suppressions": 1,
            },
        )
        with channel_store.connect(str(path)) as connection:
            rows = {
                row["memory_key"]: row["status"]
                for row in connection.execute(
                    "SELECT memory_key,status FROM memory_items"
                )
            }
        self.assertEqual(rows[target["memory_key"]], "superseded")
        self.assertEqual(rows[success[1]["memory_key"]], "active")
        self.forget(path, success[1]["memory_key"], "V")

    def test_same_id_different_binding_concurrency_is_closed(self):
        path = self.copy_database("binding-race")
        request_id = "I" * 32
        payloads = (
            self.remember_payload(
                request_id,
                "Synthetic binding race content alpha",
            ),
            self.remember_payload(
                request_id,
                "Synthetic binding race content beta",
            ),
        )
        environment = operator_environment(path)
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "backend.memory_operator_cli",
                    "remember",
                ],
                cwd=Path(__file__).resolve().parents[2],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _payload in payloads
        ]
        observed = []
        for process, payload in zip(processes, payloads, strict=True):
            stdout, stderr = process.communicate(
                json.dumps(payload, separators=(",", ":")).encode(),
                timeout=30,
            )
            observed.append((process.returncode, json.loads(stdout), stderr))
        self.assertEqual(sorted(item[0] for item in observed), [0, 4])
        success = next(item[1] for item in observed if item[0] == 0)
        failure = next(item for item in observed if item[0] == 4)
        self.assertEqual(failure[1]["category"], "request_binding_conflict")
        self.assertEqual(failure[2], b"request_binding_conflict\n")
        self.forget(path, success["memory_key"], "J")

    def test_serial_correct_correct_and_correct_forget(self):
        path = self.copy_database("serial")
        completed, created = self.run_cli(
            "remember",
            path=path,
            payload=self.remember_payload(
                "K" * 32,
                "Synthetic serial original",
            ),
        )
        self.assertEqual(completed.returncode, 0)
        first_key = created["memory_key"]
        completed, first = self.run_cli(
            "correct",
            path=path,
            payload={
                "request_id": "L" * 32,
                "memory_key": first_key,
                "replacement_content": "Synthetic serial replacement one",
                "sensitivity": "normal",
            },
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(first["category"], "corrected")
        completed, stale = self.run_cli(
            "correct",
            path=path,
            payload={
                "request_id": "M" * 32,
                "memory_key": first_key,
                "replacement_content": "Synthetic stale replacement",
                "sensitivity": "normal",
            },
        )
        self.assert_failure(
            completed,
            stale,
            exit_code=5,
            category="unsupported_action",
            action="correct",
        )

        completed, second = self.run_cli(
            "correct",
            path=path,
            payload={
                "request_id": "N" * 32,
                "memory_key": first["memory_key"],
                "replacement_content": "Synthetic serial replacement two",
                "sensitivity": "normal",
            },
        )
        self.assertEqual(completed.returncode, 0)
        completed, forgotten = self.run_cli(
            "forget",
            path=path,
            payload={
                "request_id": "O" * 32,
                "memory_key": second["memory_key"],
            },
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(forgotten["category"], "forgotten")

    def test_configuration_schema_profile_and_storage_failure_matrix(self):
        cases = (
            (
                "core-disabled",
                {"MEMORY_CORE_ENABLED": "false"},
                None,
                3,
                "readiness_failed",
            ),
            (
                "writes-disabled",
                {
                    "MEMORY_EXPLICIT_WRITES_ENABLED": "false",
                    "MEMORY_EXPLICIT_ENTRY_ENABLED": "false",
                },
                None,
                3,
                "readiness_failed",
            ),
            (
                "entry-disabled",
                {"MEMORY_EXPLICIT_ENTRY_ENABLED": "false"},
                None,
                3,
                "readiness_failed",
            ),
            (
                "bad-secret",
                {"MEMORY_FINGERPRINT_HMAC_SECRET": "weak"},
                None,
                3,
                "readiness_failed",
            ),
            (
                "bad-schema",
                {},
                "DROP INDEX idx_memory_items_live_fingerprint",
                3,
                "readiness_failed",
            ),
        )
        for name, overrides, mutation, exit_code, category in cases:
            with self.subTest(name=name):
                path = self.copy_database(name)
                if mutation is not None:
                    with channel_store.connect(str(path)) as conn:
                        conn.execute(mutation)
                before = snapshot(path)
                environment = operator_environment(path, **overrides)
                completed, result = self.run_cli(
                    "validate",
                    environment=environment,
                )
                self.assert_failure(
                    completed,
                    result,
                    exit_code=exit_code,
                    category=category,
                    action="validate",
                    forbidden=(TEST_SECRET, TEST_KEY_ID, str(path)),
                )
                self.assertEqual(snapshot(path), before)

        profile = self.copy_database("bad-profile")
        stamp = channel_store.now_iso()
        with channel_store.connect(str(profile)) as conn:
            conn.execute(
                """INSERT INTO memory_fingerprint_profile
                   (singleton,key_id,key_check,normalization_version,
                    fingerprint_version,created_at,updated_at)
                   VALUES(1,?,?,?,?,?,?)""",
                (
                    "other-key",
                    memory_policy.fingerprint_profile_check(TEST_SECRET),
                    memory_policy.NORMALIZATION_VERSION,
                    memory_policy.FINGERPRINT_VERSION,
                    stamp,
                    stamp,
                ),
            )
        before = snapshot(profile)
        completed, result = self.run_cli("validate", path=profile)
        self.assert_failure(
            completed,
            result,
            exit_code=3,
            category="readiness_failed",
            action="validate",
            forbidden=(TEST_SECRET, TEST_KEY_ID, str(profile)),
        )
        self.assertEqual(snapshot(profile), before)

    def test_stdin_and_command_negative_matrix_is_data_free(self):
        path = self.copy_database("parser")
        valid = self.remember_payload(
            "P" * 32,
            "plain-content-sentinel",
        )
        valid_raw = json.dumps(valid, separators=(",", ":")).encode()
        cases: list[tuple[str, bytes, tuple[str, ...], str]] = [
            ("remember", b"", (), "remember"),
            ("remember", b"x" * (32 * 1024 + 1), (), "remember"),
            ("remember", b"\xff", (), "remember"),
            ("remember", b"\xef\xbb\xbf" + valid_raw, (), "remember"),
            ("remember", b"not-json", (), "remember"),
            ("remember", b"[]", (), "remember"),
            ("remember", b"null", (), "remember"),
            ("remember", b"1", (), "remember"),
            (
                "remember",
                b'{"request_id":"PPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPP",'
                b'"request_id":"QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ",'
                b'"kind":"project","scope_type":"global_user",'
                b'"content":"x","sensitivity":"normal"}',
                (),
                "remember",
            ),
            (
                "remember",
                json.dumps({**valid, "extra": "x"}).encode(),
                (),
                "remember",
            ),
            (
                "remember",
                json.dumps(
                    {key: value for key, value in valid.items() if key != "kind"}
                ).encode(),
                (),
                "remember",
            ),
            (
                "remember",
                json.dumps({**valid, "content": True}).encode(),
                (),
                "remember",
            ),
            (
                "remember",
                json.dumps({**valid, "content": None}).encode(),
                (),
                "remember",
            ),
            (
                "remember",
                json.dumps({**valid, "content": 1}).encode(),
                (),
                "remember",
            ),
            (
                "remember",
                valid_raw.replace(b'"normal"', b'NaN'),
                (),
                "remember",
            ),
            (
                "remember",
                valid_raw.replace(b'"normal"', b'Infinity'),
                (),
                "remember",
            ),
            (
                "remember",
                valid_raw.replace(b'"normal"', b'-Infinity'),
                (),
                "remember",
            ),
            ("remember", valid_raw + b" {}", (), "remember"),
            ("remember", valid_raw + b" trailing", (), "remember"),
            (
                "remember",
                json.dumps(
                    self.remember_payload(
                        "Q" * 32,
                        "x",
                        scope_type="project",
                    )
                ).encode(),
                (),
                "remember",
            ),
            (
                "remember",
                json.dumps(
                    self.remember_payload(
                        "R" * 32,
                        "x",
                        scope_ref="non-empty",
                    )
                ).encode(),
                (),
                "remember",
            ),
            (
                "remember",
                json.dumps({**valid, "request_id": "short"}).encode(),
                (),
                "remember",
            ),
            (
                "correct",
                json.dumps(
                    {
                        "request_id": "S" * 32,
                        "memory_key": "short",
                        "replacement_content": "replacement-sentinel",
                        "sensitivity": "normal",
                    }
                ).encode(),
                (),
                "correct",
            ),
            (
                "remember",
                json.dumps(
                    {**valid, "kind": "assistant_experience"}
                ).encode(),
                (),
                "remember",
            ),
            ("unknown-command", b"", (), "unknown"),
            ("--help", b"", (), "unknown"),
            ("remember", valid_raw, ("extra",), "remember"),
            ("status", b"x", (), "status"),
            ("validate", b"x", (), "validate"),
            ("generate-request-id", b"x", (), "generate_request_id"),
        ]
        before = snapshot(path)
        for index, (command, raw, extra, action) in enumerate(cases):
            with self.subTest(index=index, command=command):
                completed, result = self.run_cli(
                    command,
                    path=path,
                    raw=raw,
                    extra_argv=extra,
                )
                self.assert_failure(
                    completed,
                    result,
                    exit_code=2,
                    category="input_invalid",
                    action=action,
                    forbidden=(
                        "plain-content-sentinel",
                        "replacement-sentinel",
                        "non-empty",
                        TEST_SECRET,
                        TEST_KEY_ID,
                        str(path),
                    ),
                )
                self.assertEqual(snapshot(path), before)

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "backend.memory_operator_cli",
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=operator_environment(path),
            input=b"",
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, b"input_invalid\n")
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "ok": False,
                "request_id": None,
                "action": "unknown",
                "status": "failed",
                "category": "input_invalid",
                "memory_key": None,
                "replayed": False,
            },
        )
        self.assertEqual(snapshot(path), before)

    def test_oversized_injected_reader_and_unclosed_pipe_are_bounded(self):
        class ContractViolatingReader:
            def __init__(self) -> None:
                self.read_sizes: list[int] = []

            def read(self, size: int) -> bytes:
                self.read_sizes.append(size)
                if len(self.read_sizes) != 1:
                    raise AssertionError("reader called more than once")
                return b"x" * (size + 1)

        reader = ContractViolatingReader()
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = memory_operator_cli.main(
            ["remember"],
            stdin=reader,
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(reader.read_sizes, [32 * 1024 + 1])
        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "input_invalid\n")
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "ok": False,
                "request_id": None,
                "action": "remember",
                "status": "failed",
                "category": "input_invalid",
                "memory_key": None,
                "replayed": False,
            },
        )
        self.assertNotIn("reader-content", stdout.getvalue())

        path = self.copy_database("unclosed-pipe")
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "backend.memory_operator_cli",
                "remember",
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=operator_environment(path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIsNotNone(process.stdin)
        self.assertIsNotNone(process.stdout)
        self.assertIsNotNone(process.stderr)
        try:
            process.stdin.write(b"x" * (32 * 1024 + 1))
            process.stdin.flush()
            returncode = process.wait(timeout=15)
            pipe_stdout = process.stdout.read()
            pipe_stderr = process.stderr.read()
        finally:
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.assertEqual(returncode, 2)
        self.assertEqual(pipe_stderr, b"input_invalid\n")
        self.assertEqual(
            json.loads(pipe_stdout),
            {
                "ok": False,
                "request_id": None,
                "action": "remember",
                "status": "failed",
                "category": "input_invalid",
                "memory_key": None,
                "replayed": False,
            },
        )
        self.assertNotIn(b"pipe-content", pipe_stdout + pipe_stderr)

    def test_invalid_input_never_reaches_config_runtime_backend_or_service(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                telegram_integration.TelegramConfig,
                "from_env",
                side_effect=AssertionError("config forbidden"),
            ) as telegram,
            mock.patch.object(
                deployment_config,
                "load_deployment_config",
                side_effect=AssertionError("deployment forbidden"),
            ) as deployment,
            mock.patch.object(
                memory_operator_composition,
                "compose_operator_memory_service_from_environment",
                side_effect=AssertionError("composition forbidden"),
            ) as composition,
            mock.patch.object(
                memory_operator_composition,
                "preflight_operator_memory_from_environment",
                side_effect=AssertionError("preflight forbidden"),
            ) as preflight,
        ):
            exit_code = memory_operator_cli.main(
                ["remember"],
                stdin=io.BytesIO(b"{}"),
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "input_invalid\n")
        telegram.assert_not_called()
        deployment.assert_not_called()
        composition.assert_not_called()
        preflight.assert_not_called()

    def test_keyboard_interrupt_is_fixed_internal_error_without_traceback(self):
        path = self.copy_database("keyboard")
        stdout = io.StringIO()
        stderr = io.StringIO()
        environment = operator_environment(path)
        with (
            mock.patch.object(
                memory_operator_composition,
                "compose_operator_memory_service_from_environment",
                side_effect=KeyboardInterrupt("secret-interrupt-sentinel"),
            ),
            mock.patch.dict(os.environ, environment, clear=True),
        ):
            exit_code = memory_operator_cli.main(
                ["remember"],
                stdin=io.BytesIO(
                    json.dumps(
                        self.remember_payload(
                            "T" * 32,
                            "secret-content-sentinel",
                        )
                    ).encode()
                ),
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "internal_error\n")
        public = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn("secret-interrupt-sentinel", public)
        self.assertNotIn("secret-content-sentinel", public)
        self.assertNotIn("Traceback", public)


if __name__ == "__main__":
    unittest.main()
