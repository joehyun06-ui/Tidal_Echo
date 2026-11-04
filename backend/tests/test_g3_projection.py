"""GM-01A isolation tests: synthetic sources only; no network/LLM/production G3."""
from __future__ import annotations

import hashlib
import inspect
import unittest
from dataclasses import replace

from backend.g3_projection import (
    build_projection_card, list_projection_delta, verify_projection_card,
)
from backend.g3_projection_contract import (
    AccessScope, G3Event, G3Revocation, G3SourceRef, ProjectionError,
    G3ReadOnlySourceAdapter, ProjectionCard,
)


class SyntheticG3ReadOnly:
    """Test-only read interface with mutable *test harness* authority fixtures."""

    def __init__(self):
        self._heads = {}
        self._sources = {}
        self._timeline = []
        self._revocations = []
        self._denied = set()
        self._wrong_bytes = {}
        self._replace_on_open = None
        self.event_reads = 0
        self.open_reads = 0

    def add_revision(self, eid, body, *, revision=1, cursor=1, scope="private_A",
                     source_id="synthetic_source", status="active", layer="long_term",
                     kind="event", reviewed=False, committed_at="2026-11-04T00:00:00Z"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        ref = G3SourceRef(eid, revision, source_id, hashlib.sha256(raw).hexdigest(), scope)
        event = G3Event(ref, cursor, committed_at, status, layer, kind, reviewed)
        self._heads[eid] = event
        self._sources[(eid, revision)] = raw
        self._timeline.append(event)
        if status == "revoked":
            self._revocations.append(G3Revocation(eid, revision, cursor))
        return event

    def deny(self, principal, scope):
        self._denied.add((principal, scope))

    def _authorize(self, access, scope):
        if not isinstance(access, AccessScope) or scope not in access.allowed_scopes or (access.principal_id, scope) in self._denied:
            raise ProjectionError("projection_access_denied")

    def list_events(self, *, after_cursor, access):
        changed = {}
        for item in self._timeline:
            if item.cursor > after_cursor:
                changed[item.ref.event_id] = item
        result = []
        for item in sorted(changed.values(), key=lambda x: x.cursor):
            if item.ref.scope in access.allowed_scopes:
                self._authorize(access, item.ref.scope)
                result.append(item)
        return tuple(result)

    def read_event(self, event_id, *, revision, access):
        self.event_reads += 1
        if event_id not in self._heads:
            raise ProjectionError("projection_source_unavailable")
        head = self._heads[event_id]
        self._authorize(access, head.ref.scope)
        if revision is None or revision == head.ref.revision:
            return head
        raise ProjectionError("projection_source_unavailable")

    def open_source(self, ref, *, access):
        self.open_reads += 1
        self._authorize(access, ref.scope)
        if ref.event_id not in self._heads or self._heads[ref.event_id].ref != ref:
            raise ProjectionError("projection_source_unavailable")
        if self._replace_on_open is not None:
            replace_fn, self._replace_on_open = self._replace_on_open, None
            replace_fn()
        return self._wrong_bytes.get(ref.event_id, self._sources[(ref.event_id, ref.revision)])

    def read_revocations(self, *, after_cursor, access):
        return tuple(x for x in self._revocations if x.cursor > after_cursor and
                     self._heads[x.event_id].ref.scope in access.allowed_scopes)


class G3ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SyntheticG3ReadOnly()
        self.ctx = AccessScope("test_principal", frozenset({"private_A"}))
        self.adapter.add_revision("event_A", "First synthetic conversation event", revision=1, cursor=1)

    def card(self):
        return build_projection_card(self.adapter, "event_A", access=self.ctx)

    def rejects(self, category, fn):
        with self.assertRaises(ProjectionError) as c:
            fn()
        self.assertEqual(c.exception.category, category)
        self.assertEqual(str(c.exception), category)

    def test_01_deterministic_rebuild(self):
        self.assertEqual(self.card(), self.card())

    def test_02_authority_is_derived(self):
        self.assertEqual(self.card().authority, "derived")

    def test_03_schema_has_projection_card_name(self):
        self.assertEqual(self.card().schema_version, "g3.projection_card.v1")
        self.assertTrue(self.card().projection_card_id.startswith("pc_"))

    def test_04_raw_evidence_not_in_ref(self):
        r = self.card().source_refs[0]
        self.assertEqual(r.revision, 1)
        self.assertEqual(r.source_sha256, hashlib.sha256(b"First synthetic conversation event").hexdigest())

    def test_05_successful_read_rechecks_head(self):
        before = self.adapter.event_reads
        self.assertEqual(verify_projection_card(self.adapter, self.card(), access=self.ctx).status, "valid")
        self.assertGreaterEqual(self.adapter.event_reads - before, 2)

    def test_06_wrong_expected_revision_rejected(self):
        self.rejects("projection_stale_or_revoked", lambda: build_projection_card(
            self.adapter, "event_A", access=self.ctx, expected_revision=2))

    def test_07_revision_supersedes_card(self):
        old = self.card()
        self.adapter.add_revision("event_A", "Changed", revision=2, cursor=2)
        self.rejects("projection_stale_or_revoked", lambda: verify_projection_card(
            self.adapter, old, access=self.ctx))
        self.assertNotEqual(self.card().projection_card_id, old.projection_card_id)

    def test_08_revocation_blocks_card(self):
        old = self.card()
        self.adapter.add_revision("event_A", b"", revision=2, cursor=2, status="revoked")
        self.rejects("projection_stale_or_revoked", lambda: verify_projection_card(
            self.adapter, old, access=self.ctx))

    def test_09_acl_revocation_blocks_card(self):
        old = self.card()
        self.adapter.deny("test_principal", "private_A")
        self.rejects("projection_access_denied", lambda: verify_projection_card(
            self.adapter, old, access=self.ctx))

    def test_10_missing_scope_rejected(self):
        other = AccessScope("test_principal", frozenset({"private_B"}))
        self.rejects("projection_access_denied", lambda: self.adapter.read_event(
            "event_A", revision=None, access=other))

    def test_11_imprint_mismatch(self):
        self.adapter._wrong_bytes["event_A"] = b"substituted raw bytes"
        self.rejects("projection_imprint_mismatch", self.card)

    def test_12_changed_byte_rejected(self):
        self.adapter._wrong_bytes["event_A"] = b"First synthetic conversation evenT"
        self.rejects("projection_imprint_mismatch", self.card)

    def test_13_invalid_utf8_rejected(self):
        self.adapter.add_revision("event_A", b"\xff\xfe", revision=2, cursor=2)
        self.rejects("projection_source_invalid", self.card)

    def test_14_oversized_raw_rejected(self):
        self.adapter.add_revision("event_A", b"a" * (2 * 1024 * 1024 + 1), revision=2, cursor=2)
        self.rejects("projection_source_invalid", self.card)

    def test_15_fixed_anchor_requires_review(self):
        self.adapter.add_revision("event_A", "anchor", revision=2, cursor=2, layer="fixed", reviewed=False)
        self.rejects("projection_unreviewed_anchor", self.card)

    def test_16_reviewed_anchor_allowed(self):
        self.adapter.add_revision("event_A", "approved anchor", revision=2, cursor=2, layer="fixed", reviewed=True)
        self.assertEqual(self.card().layer, "fixed")

    def test_17_short_term_layer_allowed(self):
        self.adapter.add_revision("event_A", "pending", revision=2, cursor=2, layer="short_term")
        self.assertEqual(self.card().layer, "short_term")

    def test_18_stale_before_commit_rejected(self):
        self.adapter._replace_on_open = lambda: self.adapter.add_revision(
            "event_A", "race revised", revision=2, cursor=2)
        self.rejects("projection_stale_or_revoked", self.card)

    def test_19_preview_is_bounded(self):
        self.adapter.add_revision("event_A", "x" * 1000, revision=2, cursor=2)
        self.assertEqual(len(self.card().projection_text), 240)

    def test_20_poisoned_projection_text_rejected(self):
        card = self.card()
        self.rejects("projection_card_tampered_or_stale", lambda: verify_projection_card(
            self.adapter, replace(card, projection_text="not real"), access=self.ctx))

    def test_21_fake_verified_authority_rejected(self):
        card = self.card()
        self.rejects("projection_card_invalid", lambda: verify_projection_card(
            self.adapter, replace(card, authority="verified"), access=self.ctx))

    def test_22_forged_ref_hash_rejected(self):
        card = self.card()
        ref = replace(card.source_refs[0], source_sha256="0" * 64)
        self.rejects("projection_card_tampered_or_stale", lambda: verify_projection_card(
            self.adapter, replace(card, source_refs=(ref,)), access=self.ctx))

    def test_23_schema_downgrade_rejected(self):
        card = self.card()
        self.rejects("projection_card_invalid", lambda: verify_projection_card(
            self.adapter, replace(card, schema_version="unknown"), access=self.ctx))

    def test_24_card_cannot_assert_confidence(self):
        card = self.card()
        self.assertIsNone(card.confidence)
        self.rejects("projection_card_tampered_or_stale", lambda: verify_projection_card(
            self.adapter, replace(card, confidence=1.0), access=self.ctx))

    def test_25_cursor_bad_values(self):
        for bad in (-1, True, "2"):
            self.rejects("projection_cursor_invalid", lambda: list_projection_delta(
                self.adapter, after_cursor=bad, access=self.ctx))

    def test_26_initial_feed(self):
        d = list_projection_delta(self.adapter, after_cursor=0, access=self.ctx)
        self.assertEqual(d.changed_event_ids, ("event_A",))
        self.assertEqual(len(d.valid_cards), 1)
        self.assertEqual(d.next_cursor, 1)

    def test_27_feed_revision_evict_old(self):
        old = self.card()
        self.adapter.add_revision("event_A", "updated", revision=2, cursor=2)
        d = list_projection_delta(self.adapter, after_cursor=1, access=self.ctx)
        self.assertEqual(d.changed_event_ids, ("event_A",))
        self.assertNotEqual(d.valid_cards[0], old)
        self.assertEqual(d.next_cursor, 2)

    def test_28_feed_revocation_evict_only(self):
        self.adapter.add_revision("event_A", b"", revision=2, cursor=2, status="revoked")
        d = list_projection_delta(self.adapter, after_cursor=1, access=self.ctx)
        self.assertEqual(d.changed_event_ids, ("event_A",))
        self.assertEqual(d.valid_cards, ())

    def test_29_duplicate_feed_rejected(self):
        self.adapter.add_revision("event_B", "another", cursor=1)
        self.rejects("projection_feed_invalid", lambda: list_projection_delta(
            self.adapter, after_cursor=0, access=self.ctx))

    def test_30_read_only_protocol_methods(self):
        attrs = {x for x in vars(G3ReadOnlySourceAdapter) if not x.startswith("_")}
        self.assertEqual(attrs, {"list_events", "read_event", "open_source", "read_revocations"})

    def test_31_no_network_or_model_imports(self):
        from backend import g3_projection as mod
        source = inspect.getsource(mod)
        for forbidden in ("requests", "openai", "boto3", "httpx", "urllib.request", "subprocess", "sqlite3.connect"):
            self.assertNotIn(forbidden, source)

    def test_32_bad_event_id_rejected(self):
        self.rejects("projection_source_invalid", lambda: build_projection_card(
            self.adapter, "../event_A", access=self.ctx))

    def test_33_wrong_timeline_cursor_rejected(self):
        self.adapter.add_revision("event_B", "another", cursor=1)
        self.rejects("projection_feed_invalid", lambda: list_projection_delta(
            self.adapter, after_cursor=0, access=self.ctx))

    def test_34_preview_exact_literal_excerpt(self):
        self.assertEqual(self.card().projection_text, "First synthetic conversation event")

    def test_35_no_external_time_claim(self):
        self.assertEqual(self.card().source_committed_at, "2026-11-04T00:00:00Z")
        self.assertFalse(hasattr(self.card(), "trusted_timestamp"))

    def test_36_scope_confusion_refused(self):
        self.adapter.add_revision("event_B", "private B", cursor=2, scope="private_B")
        self.rejects("projection_access_denied", lambda: build_projection_card(
            self.adapter, "event_B", access=self.ctx))

    def test_37_explicit_immutable_card(self):
        with self.assertRaises(Exception):
            self.card().projection_text = "changed"

    def test_38_revocation_feed_has_no_raw_data(self):
        self.adapter.add_revision("event_A", b"", revision=2, cursor=2, status="revoked")
        d = list_projection_delta(self.adapter, after_cursor=1, access=self.ctx)
        self.assertFalse(hasattr(d, "raw_bytes"))

    def test_39_false_revision_boolean_rejected(self):
        self.rejects("projection_source_invalid", lambda: build_projection_card(
            self.adapter, "event_A", access=self.ctx, expected_revision=True))

    def test_40_empty_delta(self):
        d = list_projection_delta(self.adapter, after_cursor=1, access=self.ctx)
        self.assertEqual(d.changed_event_ids, ())
        self.assertEqual(d.next_cursor, 1)


if __name__ == "__main__":
    unittest.main()
