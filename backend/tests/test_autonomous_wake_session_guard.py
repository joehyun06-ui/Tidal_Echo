from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from backend import autonomous_wake_session_guard as guard
from backend import codex_generation_store


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class AutonomousWakeSessionGuardTests(unittest.TestCase):
    def _codex_env(self, store_path: Path) -> dict[str, str]:
        return {
            "CODEX_GENERATION_ENABLED": "true",
            "CODEX_GENERATION_DB": str(store_path),
        }

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

    def test_default_target_is_first_non_codex_session_not_active_window(self):
        with tempfile.TemporaryDirectory() as temp:
            store_path = Path(temp) / "codex-generation.db"
            self._pin_canary(store_path)
            sessions = [
                {"id": "api-main", "title": "Main"},
                {"id": "api-canary", "title": "Anything"},
            ]

            selected = guard.select_wake_api_session(
                sessions,
                self._codex_env(store_path),
            )

        self.assertEqual(selected, "api-main")

    def test_explicit_active_codex_target_is_rejected_by_store_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            store_path = Path(temp) / "codex-generation.db"
            self._pin_canary(store_path)
            env = {
                **self._codex_env(store_path),
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
            store_path = Path(temp) / "codex-generation.db"
            self._pin_canary(store_path)
            env = {
                **self._codex_env(store_path),
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
            store_path = Path(temp) / "codex-generation.db"
            self._pin_canary(store_path)
            selected = guard.select_wake_api_session(
                [{"id": "api-canary"}],
                self._codex_env(store_path),
            )

        self.assertEqual(selected, "")


if __name__ == "__main__":
    unittest.main()
