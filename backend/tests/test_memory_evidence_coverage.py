"""Tests for aggregate-only provenance coverage; no real user data or network."""
import json
import sqlite3
import unittest

from backend.memory_evidence_coverage import summarize_session_coverage
from backend.memory_evidence_readonly import MemoryEvidenceReadonlyError
from backend.tests.test_memory_evidence_readonly import database


class CoverageAuditTests(unittest.TestCase):
    def setUp(self):
        self.conn = database()

    def tearDown(self):
        self.conn.close()

    def audit(self, session="s1", allowed=("s1", "s2")):
        return summarize_session_coverage(
            self.conn, session_id=session, allowed_sessions=allowed,
        )

    def count(self, name):
        return self.audit()["counts"][name]

    def test_only_aggregate_fields_are_returned(self):
        result = self.audit()
        self.assertEqual(result["total_eligible_scope_items"], 1)
        self.assertEqual(result["counts"]["verified_session_evidence"], 1)
        self.assertEqual(result["contains_personal_data"], False)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(set(result), {
            "schema", "scope", "total_eligible_scope_items",
            "counts", "contains_personal_data", "writes",
        })

    def test_original_memory_text_is_never_echoed(self):
        self.conn.execute("UPDATE memory_items SET normalized_content=?", ("PRIVATE_SYNTHETIC_XYZ",))
        serialized = json.dumps(self.audit(), sort_keys=True)
        self.assertNotIn("PRIVATE_SYNTHETIC_XYZ", serialized)
        self.assertNotIn("memory-source", serialized)
        self.assertNotIn("m"*32, serialized)

    def test_missing_session_binding_detected(self):
        self.conn.execute("UPDATE messages SET meta=?",
                          ('{"channel":"web","source":"relay"}',))
        self.assertEqual(self.count("missing_session_binding"), 1)
        self.assertEqual(self.count("verified_session_evidence"), 0)

    def test_invalid_metadata_is_unbound_not_exposed(self):
        self.conn.execute("UPDATE messages SET meta='not-json'")
        self.assertEqual(self.count("missing_session_binding"), 1)

    def test_other_session_binding_detected_separately(self):
        self.conn.execute("UPDATE messages SET meta=?", ('{"api_session":"s2"}',))
        self.assertEqual(self.count("different_session_binding"), 1)
        self.assertEqual(self.count("verified_session_evidence"), 0)

    def test_fiction_is_not_qualifying_source(self):
        self.conn.execute("UPDATE memory_evidence_events SET reality_scope='fiction'")
        self.assertEqual(self.count("missing_qualifying_source"), 1)

    def test_assistant_source_is_not_qualifying(self):
        self.conn.execute("UPDATE memory_sources SET evidence_role='assistant'")
        self.assertEqual(self.count("missing_qualifying_source"), 1)

    def test_source_removed_classified_not_trusted(self):
        self.conn.execute("DELETE FROM memory_sources")
        self.assertEqual(self.count("missing_qualifying_source"), 1)

    def test_sensitive_records_not_even_counted(self):
        self.conn.execute("UPDATE memory_items SET sensitivity='sensitive'")
        result = self.audit()
        self.assertEqual(result["total_eligible_scope_items"], 0)
        self.assertEqual(sum(result["counts"].values()), 0)

    def test_inactive_records_not_counted(self):
        self.conn.execute("UPDATE memory_items SET status='candidate'")
        self.assertEqual(self.audit()["total_eligible_scope_items"], 0)

    def test_cross_session_records_not_counted(self):
        self.assertEqual(self.audit("s2")["total_eligible_scope_items"], 0)

    def test_unauthorized_session_fails_closed(self):
        with self.assertRaises(MemoryEvidenceReadonlyError):
            self.audit("s1", allowed=("s2",))

    def test_malformed_session_fails_closed(self):
        with self.assertRaises(MemoryEvidenceReadonlyError):
            self.audit("s1' OR 1=1")

    def test_content_over_display_limit_classified(self):
        self.conn.execute("UPDATE memory_items SET normalized_content=?", ("a"*4001,))
        self.assertEqual(self.count("content_over_display_limit"), 1)

    def test_excess_sources_classified(self):
        for i in range(2, 23):
            self.conn.execute("INSERT INTO messages VALUES(?, ?)",(i, '{"api_session":"s1"}'))
            self.conn.execute(
                "INSERT INTO memory_evidence_events VALUES (?, ?, 'confirmed_user_fact','real','user')",
                (i,i),
            )
            self.conn.execute(
                "INSERT INTO memory_sources VALUES (?,?,?,?,?,?)",
                (i,1,i,i,"confirmed_user_fact","user"),
            )
        self.assertEqual(self.count("too_many_sources"),1)

    def test_audit_never_writes(self):
        count = self.conn.total_changes
        self.audit()
        self.assertEqual(self.conn.total_changes,count)

    def test_query_only_sqlite(self):
        self.conn.commit()
        self.conn.execute("PRAGMA query_only=ON")
        self.assertEqual(self.audit()["counts"]["verified_session_evidence"],1)
        self.conn.execute("PRAGMA query_only=OFF")

    def test_invalid_session_type_fails_closed(self):
        with self.assertRaises(MemoryEvidenceReadonlyError):
            self.audit(session=None)


if __name__ == "__main__":
    unittest.main()
