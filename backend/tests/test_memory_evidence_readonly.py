"""Isolated fixture checks for read-only memory evidence. No keys/network."""
import sqlite3
import unittest

from backend.memory_evidence_readonly import (
    MemoryEvidenceReadonlyError,
    build_snapshot,
)


def database():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE memory_items(id INTEGER PRIMARY KEY, memory_key TEXT,
        normalized_content TEXT, last_confirmed_at TEXT, scope_type TEXT,
        scope_ref TEXT, status TEXT, sensitivity TEXT);
      CREATE TABLE memory_sources(id INTEGER PRIMARY KEY, memory_id INTEGER,
        evidence_event_id INTEGER, canonical_message_id INTEGER,
        evidence_type TEXT, evidence_role TEXT);
      CREATE TABLE memory_evidence_events(id INTEGER PRIMARY KEY,
        canonical_message_id INTEGER, evidence_type TEXT,
        reality_scope TEXT, subject_scope TEXT);
      CREATE TABLE messages(id INTEGER PRIMARY KEY, meta TEXT);
    """)
    conn.execute("""INSERT INTO memory_items VALUES
      (1, ?, 'Friday at 16:30', '2026-10-24T10:00:00Z',
       'session', 's1', 'active','normal')""", ("m"*32,))
    conn.execute("INSERT INTO messages VALUES(1, ?)", ('{"api_session":"s1"}',))
    conn.execute("""INSERT INTO memory_evidence_events VALUES
      (1, 1, 'confirmed_user_fact','real','user')""")
    conn.execute("""INSERT INTO memory_sources VALUES
      (1,1,1,1,'confirmed_user_fact','user')""")
    return conn


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.conn = database()

    def tearDown(self):
        self.conn.close()

    def build(self, session="s1", known=("s1", "s2")):
        return build_snapshot(self.conn, session_id=session, allowed_sessions=known)

    def test_verified_session_evidence(self):
        value = self.build()
        self.assertEqual(value["views"][0]["answer"], "Friday at 16:30")
        self.assertEqual(value["views"][0]["source_ids"], ["memory-source-1"])
        self.assertEqual(value["api_session"], "s1")

    def test_unknown_session_rejected(self):
        with self.assertRaises(MemoryEvidenceReadonlyError):
            self.build(known=("s2",))

    def test_injection_session_rejected(self):
        with self.assertRaises(MemoryEvidenceReadonlyError):
            self.build(session="s1' OR 1=1 --")

    def test_other_session_does_not_get_memory(self):
        self.assertEqual(self.build(session="s2")["views"], [])

    def test_source_canonical_message_other_session(self):
        self.conn.execute("UPDATE messages SET meta=? WHERE id=1", ('{"api_session":"s2"}',))
        self.assertEqual(self.build()["views"], [])

    def test_invalid_json_meta_skipped(self):
        self.conn.execute("UPDATE messages SET meta='not json'")
        self.assertEqual(self.build()["views"], [])

    def test_sensitive_does_not_leak(self):
        self.conn.execute("UPDATE memory_items SET sensitivity='sensitive'")
        self.assertEqual(self.build()["views"], [])

    def test_candidate_does_not_leak(self):
        self.conn.execute("UPDATE memory_items SET status='candidate'")
        self.assertEqual(self.build()["views"], [])

    def test_global_scope_does_not_leak(self):
        self.conn.execute("UPDATE memory_items SET scope_type='global_user', scope_ref=''")
        self.assertEqual(self.build()["views"], [])

    def test_fiction_does_not_leak(self):
        self.conn.execute("UPDATE memory_evidence_events SET reality_scope='fiction'")
        self.assertEqual(self.build()["views"], [])

    def test_third_party_does_not_leak(self):
        self.conn.execute("UPDATE memory_evidence_events SET subject_scope='third_party'")
        self.assertEqual(self.build()["views"], [])

    def test_assistant_source_does_not_leak(self):
        self.conn.execute("UPDATE memory_sources SET evidence_role='assistant'")
        self.assertEqual(self.build()["views"], [])

    def test_no_source_does_not_leak(self):
        self.conn.execute("DELETE FROM memory_sources")
        self.assertEqual(self.build()["views"], [])

    def test_missing_table_fails_closed(self):
        self.conn.execute("DROP TABLE memory_sources")
        with self.assertRaises(MemoryEvidenceReadonlyError):
            self.build()

    def test_malformed_date_fails_closed(self):
        self.conn.execute("UPDATE memory_items SET last_confirmed_at='yesterday'")
        with self.assertRaises(MemoryEvidenceReadonlyError):
            self.build()

    def test_overlong_content_not_exposed(self):
        self.conn.execute("UPDATE memory_items SET normalized_content=?", ("a"*4001,))
        self.assertEqual(self.build()["views"], [])

    def test_invalid_memory_key_fails_closed(self):
        self.conn.execute("UPDATE memory_items SET memory_key='bad'")
        with self.assertRaises(MemoryEvidenceReadonlyError):
            self.build()

    def test_source_count_limit(self):
        for i in range(2, 23):
            self.conn.execute("INSERT INTO messages VALUES(?, ?)", (i,'{"api_session":"s1"}'))
            self.conn.execute("INSERT INTO memory_evidence_events VALUES (?, ?, 'confirmed_user_fact','real','user')", (i,i))
            self.conn.execute("INSERT INTO memory_sources VALUES (?,?,?,?,?,?)", (i,1,i,i,"confirmed_user_fact","user"))
        self.assertEqual(self.build()["views"], [])

    def test_no_write_performed(self):
        count_before = self.conn.total_changes
        self.build()
        self.assertEqual(self.conn.total_changes, count_before)


if __name__ == "__main__":
    unittest.main()
