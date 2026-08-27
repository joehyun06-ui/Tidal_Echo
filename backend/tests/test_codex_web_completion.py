from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import codex_web_completion


class CodexWebCompletionTest(unittest.TestCase):
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

    def complete(self, *, text="answer", callback="codex-callback-1"):
        return codex_web_completion.complete_codex_web_generation(
            self.path,
            callback_identity=callback,
            generation_id="codex-gen-1",
            client_message_id="codex-client-1",
            api_session="api-canary",
            reply_to=41,
            text=text,
            ts="2026-08-27T12:00:00+00:00",
            usage={"input_tokens": 4, "output_tokens": 2},
        )

    def test_first_completion_inserts_canonical_reply(self):
        result = self.complete()
        self.assertFalse(result["duplicate"])
        msg = result["message"]
        self.assertEqual((msg["direction"], msg["kind"], msg["text"]), ("out", "reply", "answer"))
        self.assertEqual(msg["meta"]["provider"], "codex")
        self.assertEqual(msg["meta"]["codex_callback_identity"], "codex-callback-1")
        self.assertEqual(msg["meta"]["reply_to"], "41")

    def test_same_callback_identity_is_idempotent_and_returns_original_message(self):
        first = self.complete()
        second = self.complete()
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["message"]["id"], first["message"]["id"])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_same_identity_with_different_text_is_conflict(self):
        self.complete()
        with self.assertRaisesRegex(codex_web_completion.CodexWebCompletionError, "completion_conflict"):
            self.complete(text="different")

    def test_same_identity_with_different_correlation_is_conflict(self):
        self.complete()
        with self.assertRaisesRegex(codex_web_completion.CodexWebCompletionError, "completion_conflict"):
            codex_web_completion.complete_codex_web_generation(
                self.path,
                callback_identity="codex-callback-1",
                generation_id="codex-gen-other",
                client_message_id="codex-client-other",
                api_session="api-canary",
                reply_to=41,
                text="answer",
                ts="2026-08-27T12:00:00+00:00",
            )

    def test_duplicate_rows_are_fail_closed_as_corruption(self):
        first = self.complete()
        meta = first["message"]["meta"]
        import json
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                ("2026-08-27T12:00:01+00:00", "out", "reply", "answer", json.dumps(meta)),
            )
        with self.assertRaisesRegex(codex_web_completion.CodexWebCompletionError, "completion_corrupt"):
            self.complete()

    def test_no_new_table_or_index_is_required(self):
        self.complete()
        with sqlite3.connect(self.path) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            indexes = list(conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"))
        self.assertEqual(tables, {"messages"})
        self.assertEqual(indexes, [])


if __name__ == "__main__":
    unittest.main()
