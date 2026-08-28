from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import codex_canary_ingress


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CodexCanaryIngressTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "relay.db"
        with sqlite3.connect(self.path) as conn:
            conn.execute("""CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                direction TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                meta TEXT NOT NULL DEFAULT '{}')""")

    def insert(self, *, text="hello", direction="in", kind="user", meta=None) -> int:
        if meta is None:
            meta = {
                "user": "human",
                "channel": "web",
                "source": "relay",
                "api_session": "api-canary",
                "attachments": [],
            }
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                (
                    "2026-08-27T12:00:00+00:00",
                    direction,
                    kind,
                    text,
                    json.dumps(meta, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def load(self, mid: int, *, text="hello", session="api-canary") -> str:
        return codex_canary_ingress.load_text_only_web_message(
            self.path,
            canonical_message_id=mid,
            api_session=session,
            expected_digest=sha(text),
        )

    def test_plain_canonical_web_text_is_eligible(self):
        mid = self.insert()
        self.assertEqual(self.load(mid), "hello")

    def test_canonical_digest_mismatch_fails_closed(self):
        mid = self.insert(text="hello")
        with self.assertRaisesRegex(
            codex_canary_ingress.CodexCanaryIngressError,
            "codex_canary_input_contract_changed",
        ):
            self.load(mid, text="mutated")

    def test_non_web_or_non_relay_provenance_is_ineligible(self):
        for meta in (
            {"channel": "telegram", "source": "relay", "api_session": "api-canary", "attachments": []},
            {"channel": "web", "source": "other", "api_session": "api-canary", "attachments": []},
        ):
            with self.subTest(meta=meta):
                mid = self.insert(meta=meta)
                with self.assertRaisesRegex(
                    codex_canary_ingress.CodexCanaryIngressError,
                    "codex_canary_surface_ineligible",
                ):
                    self.load(mid)

    def test_only_inbound_user_messages_are_eligible(self):
        for direction, kind in (("out", "reply"), ("in", "voice")):
            with self.subTest(direction=direction, kind=kind):
                mid = self.insert(direction=direction, kind=kind)
                with self.assertRaisesRegex(
                    codex_canary_ingress.CodexCanaryIngressError,
                    "codex_canary_surface_ineligible",
                ):
                    self.load(mid)

    def test_session_mismatch_is_rejected(self):
        mid = self.insert()
        with self.assertRaisesRegex(
            codex_canary_ingress.CodexCanaryIngressError,
            "codex_canary_session_mismatch",
        ):
            self.load(mid, session="api-other")

    def test_attachments_are_rejected_even_when_text_exists(self):
        mid = self.insert(meta={
            "channel": "web",
            "source": "relay",
            "api_session": "api-canary",
            "attachments": [{"name": "photo.jpg"}],
        })
        with self.assertRaisesRegex(
            codex_canary_ingress.CodexCanaryIngressError,
            "codex_canary_attachments_unsupported",
        ):
            self.load(mid)

    def test_missing_canonical_message_is_not_treated_as_safe_input(self):
        with self.assertRaisesRegex(
            codex_canary_ingress.CodexCanaryIngressError,
            "codex_canary_message_missing",
        ):
            self.load(999)

    def test_continuity_gate_allows_only_explicit_empty(self):
        codex_canary_ingress.require_continuity_empty("empty")
        with self.assertRaisesRegex(
            codex_canary_ingress.CodexCanaryIngressError,
            "codex_canary_continuity_unsupported",
        ):
            codex_canary_ingress.require_continuity_empty("applied")
        with self.assertRaisesRegex(
            codex_canary_ingress.CodexCanaryIngressError,
            "codex_canary_continuity_unavailable",
        ):
            codex_canary_ingress.require_continuity_empty("unavailable")


if __name__ == "__main__":
    unittest.main()
