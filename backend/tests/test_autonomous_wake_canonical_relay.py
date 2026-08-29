from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import autonomous_wake_worker_notification_compat as compat  # noqa: E402


class AutonomousWakeCanonicalRelayTests(unittest.TestCase):
    def make_db(self, path: Path) -> None:
        with sqlite3.connect(path) as conn:
            conn.execute(
                """CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    meta TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            rows = [
                ("2026-08-25T10:00:00+00:00", "out", "reply", "other session", {"api_session": "api-b"}),
                ("2026-08-25T10:01:00+00:00", "in", "user", "current user", {"api_session": "api-a"}),
                ("2026-08-25T10:02:00+00:00", "out", "reply", "current assistant", {"api_session": "api-a"}),
                ("2026-08-25T10:03:00+00:00", "in", "user", "legacy user", {}),
            ]
            conn.executemany(
                "INSERT INTO messages (ts,direction,kind,text,meta) VALUES (?,?,?,?,?)",
                [(ts, direction, kind, text, json.dumps(meta)) for ts, direction, kind, text, meta in rows],
            )
            conn.commit()

    def test_context_is_scoped_to_active_api_session(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "relay.db"
            self.make_db(db_path)
            with mock.patch.dict(os.environ, {"RELAY_DB": str(db_path)}, clear=False), mock.patch.object(
                compat, "_active_api_session", return_value="api-a"
            ):
                context = compat._canonical_context()

        self.assertEqual(context["api_session"], "api-a")
        self.assertTrue(context["has_user_context"])
        self.assertEqual(
            [(item["role"], item["content"]) for item in context["recent_messages"]],
            [("user", "current user"), ("assistant", "current assistant")],
        )

    def test_legacy_context_excludes_tagged_sessions(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "relay.db"
            self.make_db(db_path)
            with mock.patch.dict(os.environ, {"RELAY_DB": str(db_path)}, clear=False), mock.patch.object(
                compat, "_active_api_session", return_value=""
            ):
                context = compat._canonical_context()

        self.assertEqual(context["api_session"], "")
        self.assertTrue(context["has_user_context"])
        self.assertEqual(
            [(item["role"], item["content"]) for item in context["recent_messages"]],
            [("user", "legacy user")],
        )

    def test_direct_execution_bootstraps_repo_root_without_pythonpath(self):
        with tempfile.TemporaryDirectory() as temp:
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env["AUTONOMOUS_WAKE_ENABLED"] = "true"
            env["AUTONOMOUS_WAKE_BRIDGE_URL"] = ""
            env["AUTONOMOUS_WAKE_TOKEN"] = ""
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "autonomous_wake_worker_notification_compat.py"),
                ],
                cwd=temp,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertNotIn("ModuleNotFoundError", completed.stdout + completed.stderr)
        self.assertIn("invalid_autonomous_wake_bridge_url", completed.stdout)


if __name__ == "__main__":
    unittest.main()
