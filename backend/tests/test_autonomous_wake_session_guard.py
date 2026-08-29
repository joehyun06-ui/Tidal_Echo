from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from backend import autonomous_wake_session_guard as guard
from backend import codex_generation_store


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class AutonomousWakeSessionGuardTests(unittest.TestCase):
    def _write_loop_config(self, path: Path, sessions: list[dict]) -> None:
        path.write_text(
            json.dumps({"sessions": sessions}),
            encoding="utf-8",
        )

    def _codex_env(
        self,
        store_path: Path,
        loop_config: Path | None = None,
    ) -> dict[str, str]:
        env = {
            "CODEX_GENERATION_ENABLED": "true",
            "CODEX_GENERATION_DB": str(store_path),
        }
        if loop_config is not None:
            env["LOOP_CONFIG"] = str(loop_config)
        return env

    def _pin_canary(self, store_path: Path) -> None:
        codex_generation_store.initialize(store_path)
        codex_generation_store.pin_session(
            store_path,
            api_session="api-canary",
            model="gpt-5.6-sol",
            model_provider="openai",
            reasoning_effort="low",
            persona_hash=_sha("persona"),
        )

    def test_default_target_is_first_api_authority_not_active_window(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store_path = root / "codex-generation.db"
            loop_config = root / "loop.json"
            self._pin_canary(store_path)
            self._write_loop_config(loop_config, [
                {"id": "api-main", "provider": "api"},
                {"id": "api-canary", "provider": "codex"},
            ])
            sessions = [
                {"id": "api-main", "title": "Main"},
                {"id": "api-canary", "title": "Anything"},
            ]

            selected = guard.select_wake_api_session(
                sessions,
                self._codex_env(store_path, loop_config),
            )

        self.assertEqual(selected, "api-main")

    def test_explicit_active_codex_target_is_rejected_by_provider_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store_path = root / "codex-generation.db"
            loop_config = root / "loop.json"
            self._pin_canary(store_path)
            self._write_loop_config(loop_config, [
                {"id": "api-main", "provider": "api"},
                {"id": "api-canary", "provider": "codex"},
            ])
            env = {
                **self._codex_env(store_path, loop_config),
                "AUTONOMOUS_WAKE_API_SESSION": "api-canary",
            }

            with self.assertRaisesRegex(
                guard.AutonomousWakeSessionError,
                "autonomous_wake_codex_session_forbidden",
            ):
                guard.select_wake_api_session(
                    [{"id": "api-main"}, {"id": "api-canary"}],
                    env,
                )

    def test_explicit_ordinary_target_is_stable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store_path = root / "codex-generation.db"
            loop_config = root / "loop.json"
            self._pin_canary(store_path)
            self._write_loop_config(loop_config, [
                {"id": "api-other", "provider": "api"},
                {"id": "api-main", "provider": "api"},
                {"id": "api-canary", "provider": "codex"},
            ])
            env = {
                **self._codex_env(store_path, loop_config),
                "AUTONOMOUS_WAKE_API_SESSION": "api-main",
            }

            selected = guard.select_wake_api_session(
                [{"id": "api-other"}, {"id": "api-main"}, {"id": "api-canary"}],
                env,
            )

        self.assertEqual(selected, "api-main")

    def test_enabled_generation_without_authority_store_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing.db"
            with self.assertRaisesRegex(
                guard.AutonomousWakeSessionError,
                "autonomous_wake_session_guard_unavailable",
            ):
                guard.select_wake_api_session(
                    [{"id": "api-main"}],
                    self._codex_env(missing),
                )

    def test_no_ordinary_session_falls_back_to_legacy_surface(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store_path = root / "codex-generation.db"
            loop_config = root / "loop.json"
            self._pin_canary(store_path)
            self._write_loop_config(loop_config, [
                {"id": "api-canary", "provider": "codex"},
            ])
            selected = guard.select_wake_api_session(
                [{"id": "api-canary"}],
                self._codex_env(store_path, loop_config),
            )

        self.assertEqual(selected, "")

    def test_retired_codex_provider_is_still_excluded_from_wake(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store_path = root / "codex-generation.db"
            loop_config = root / "loop.json"
            self._pin_canary(store_path)
            codex_generation_store.retire_session(
                store_path,
                api_session="api-canary",
            )
            self._write_loop_config(loop_config, [
                {"id": "api-main", "provider": "api"},
                {"id": "api-canary", "provider": "codex"},
            ])

            selected = guard.select_wake_api_session(
                [{"id": "api-canary"}, {"id": "api-main"}],
                self._codex_env(store_path, loop_config),
            )

        self.assertEqual(selected, "api-main")

    def test_loop_config_authority_excludes_retired_codex_when_worker_rows_lack_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store_path = root / "codex-generation.db"
            loop_config = root / "loop.json"
            self._pin_canary(store_path)
            codex_generation_store.retire_session(
                store_path,
                api_session="api-canary",
            )
            self._write_loop_config(loop_config, [
                {"id": "api-canary", "provider": "codex"},
            ])

            selected = guard.select_wake_api_session(
                [{"id": "api-canary", "title": "ordinary-looking-title"}],
                self._codex_env(store_path, loop_config),
            )

        self.assertEqual(selected, "")

    def test_explicit_retired_codex_provider_target_is_forbidden(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store_path = root / "codex-generation.db"
            loop_config = root / "loop.json"
            self._pin_canary(store_path)
            codex_generation_store.retire_session(
                store_path,
                api_session="api-canary",
            )
            self._write_loop_config(loop_config, [
                {"id": "api-canary", "provider": "codex"},
            ])
            env = {
                **self._codex_env(store_path, loop_config),
                "AUTONOMOUS_WAKE_API_SESSION": "api-canary",
            }

            with self.assertRaisesRegex(
                guard.AutonomousWakeSessionError,
                "autonomous_wake_codex_session_forbidden",
            ):
                guard.select_wake_api_session(
                    [{"id": "api-canary"}],
                    env,
                )

    def test_api_authority_with_active_codex_pin_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store_path = root / "codex-generation.db"
            loop_config = root / "loop.json"
            self._pin_canary(store_path)
            self._write_loop_config(loop_config, [
                {"id": "api-canary", "provider": "api"},
            ])

            with self.assertRaisesRegex(
                guard.AutonomousWakeSessionError,
                "autonomous_wake_session_guard_unavailable",
            ):
                guard.select_wake_api_session(
                    [{"id": "api-canary"}],
                    self._codex_env(store_path, loop_config),
                )

    def test_malformed_loop_provider_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store_path = root / "codex-generation.db"
            loop_config = root / "loop.json"
            codex_generation_store.initialize(store_path)
            self._write_loop_config(loop_config, [
                {"id": "api-main", "provider": "ui-title-derived"},
            ])

            with self.assertRaisesRegex(
                guard.AutonomousWakeSessionError,
                "autonomous_wake_session_guard_unavailable",
            ):
                guard.select_wake_api_session(
                    [{"id": "api-main"}],
                    self._codex_env(store_path, loop_config),
                )


if __name__ == "__main__":
    unittest.main()
