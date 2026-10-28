"""No-network, no-credential evidence classification regression (Stage 35)."""
from __future__ import annotations

import dataclasses
import unittest
from datetime import datetime, timezone
from unittest import mock

from backend.render_telegram_disk_evidence_offline import (
    EXPECTED_DISK_ID, EXPECTED_MOUNT, EXPECTED_WEBHOOK_URL,
    EXPECTED_OLD_COMMIT, EXPECTED_OLD_DEPLOY,
    EvidenceStatus, review_telegram_webhook, review_disk_snapshot, review_old_deploy,
)

NOW = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)


def webhook(**overrides):
    result = {"url": EXPECTED_WEBHOOK_URL, "pending_update_count": 0}
    result.update(overrides)
    return {"ok": True, "result": result}


def snapshot(**overrides):
    item = {"diskId": EXPECTED_DISK_ID, "mountPath": EXPECTED_MOUNT,
            "createdAt": "2026-10-28T12:00:00Z"}
    item.update(overrides)
    return item


def old_deploy(**overrides):
    item = {"id": EXPECTED_OLD_DEPLOY, "commit": {"id": EXPECTED_OLD_COMMIT}, "status": "deactivated"}
    item.update(overrides)
    return item


class Stage35OfflineEvidenceTests(unittest.TestCase):
    def test_webhook_matches_only_claimed_registration(self):
        self.assertEqual(review_telegram_webhook(webhook()).status, "registered_no_reported_error")

    def test_webhook_not_registered(self):
        self.assertEqual(review_telegram_webhook(webhook(url="")).status, "not_registered")

    def test_webhook_host_mismatch(self):
        self.assertEqual(review_telegram_webhook(webhook(url="https://fake.example/path")).status, "target_mismatch")

    def test_webhook_path_or_query_mismatch(self):
        for value in (EXPECTED_WEBHOOK_URL + "?token=TOP_SECRET", EXPECTED_WEBHOOK_URL + "/x"):
            with self.subTest(value="redacted"):
                self.assertEqual(review_telegram_webhook(webhook(url=value)).status, "target_mismatch")

    def test_webhook_pending_not_success_or_failure(self):
        self.assertEqual(review_telegram_webhook(webhook(pending_update_count=1)).status,
                         "registered_pending_updates")

    def test_webhook_error_is_historical_not_current_failure(self):
        item = webhook(last_error_date=100, last_error_message="BOT_SECRET user id")
        self.assertEqual(review_telegram_webhook(item).status, "registered_historical_error")

    def test_webhook_sync_error_is_historical(self):
        self.assertEqual(review_telegram_webhook(webhook(last_synchronization_error_date=123)).status,
                         "registered_historical_error")

    def test_webhook_bad_response_fails_closed(self):
        for item in (None, {}, [], {"ok": 1}, {"ok": False, "result": {}}, {"ok": True, "result": 3}):
            with self.subTest(kind=type(item).__name__):
                self.assertEqual(review_telegram_webhook(item).status, "invalid_or_unavailable")

    def test_webhook_bad_field_types(self):
        for item in (webhook(url=123), webhook(url="x" * 2100), webhook(url="\ud800"),
                     webhook(pending_update_count="0"), webhook(pending_update_count=True),
                     webhook(pending_update_count=-1), webhook(last_error_date="123"),
                     webhook(last_error_message={}), webhook(last_synchronization_error_date=-1)):
            with self.subTest(field="redacted"):
                self.assertEqual(review_telegram_webhook(item).status, "invalid_or_unavailable")

    def test_no_secret_in_output_or_repr(self):
        raw = webhook(last_error_date=123, last_error_message="BOT_SECRET")
        raw["result"]["extra"] = {"secret": "BOT_SECRET"}
        status = review_telegram_webhook(raw)
        self.assertNotIn("BOT_SECRET", repr(status))
        self.assertNotIn("BOT_SECRET", str(dataclasses.asdict(status)))
        self.assertNotIn(EXPECTED_WEBHOOK_URL, repr(status))

    def test_snapshot_metadata_fresh_but_restore_unverified(self):
        self.assertEqual(review_disk_snapshot(snapshot(), observed_at=NOW).status,
                         "snapshot_record_observed_restore_unverified")

    def test_snapshot_stale(self):
        self.assertEqual(review_disk_snapshot(snapshot(createdAt="2026-10-20T00:00:00Z"), observed_at=NOW).status,
                         "snapshot_metadata_stale")

    def test_snapshot_wrong_disk(self):
        self.assertEqual(review_disk_snapshot(snapshot(diskId="dsk-other"), observed_at=NOW).status,
                         "invalid_or_unavailable")

    def test_snapshot_wrong_mount(self):
        self.assertEqual(review_disk_snapshot(snapshot(mountPath="/tmp"), observed_at=NOW).status,
                         "disk_mismatch")

    def test_snapshot_future_or_malformed_timestamp(self):
        for created in ("2026-10-29T01:00:00Z", "not-a-time", "2026-10-28", None, "x" * 1000):
            with self.subTest(created="redacted"):
                self.assertEqual(review_disk_snapshot(snapshot(createdAt=created), observed_at=NOW).status,
                                 "invalid_or_unavailable")

    def test_snapshot_timezone_offset_accepted(self):
        self.assertEqual(review_disk_snapshot(snapshot(createdAt="2026-10-28T20:00:00+08:00"),
                                              observed_at=NOW).status,
                         "snapshot_record_observed_restore_unverified")

    def test_snapshot_observation_must_be_aware_datetime(self):
        self.assertEqual(review_disk_snapshot(snapshot(), observed_at=datetime(2026, 10, 29)).status,
                         "invalid_or_unavailable")

    def test_old_deploy_status_only_candidate_not_artifact(self):
        self.assertEqual(review_old_deploy(old_deploy()).status,
                         "candidate_record_only_artifact_unverified")

    def test_old_deploy_mismatch_blocked(self):
        for item in (old_deploy(id="dep-other"), old_deploy(commit={"id": "other"}), None, {}):
            with self.subTest(kind=str(type(item))):
                self.assertEqual(review_old_deploy(item).status, "invalid_or_unavailable")

    def test_old_deploy_state_different(self):
        self.assertEqual(review_old_deploy(old_deploy(status="live")).status, "state_different")

    def test_every_result_forbids_action(self):
        samples = (review_telegram_webhook(webhook()), review_disk_snapshot(snapshot(), observed_at=NOW),
                   review_old_deploy(old_deploy()), review_telegram_webhook({}),
                   review_disk_snapshot({}, observed_at=NOW), review_old_deploy({}))
        for obj in samples:
            self.assertIsInstance(obj, EvidenceStatus)
            for attr in ("may_deploy", "may_rollback", "may_write_memory",
                         "may_enable_oidc", "may_use_paid_api"):
                self.assertIs(getattr(obj, attr), False)

    def test_inputs_not_modified(self):
        raw = webhook(last_error_message="secret")
        baseline = repr(raw)
        review_telegram_webhook(raw)
        self.assertEqual(repr(raw), baseline)

    def test_never_make_network_calls(self):
        with mock.patch("socket.socket", side_effect=AssertionError("NETWORK FORBIDDEN")):
            self.assertEqual(review_telegram_webhook(webhook()).source, "telegram_webhook")
            self.assertEqual(review_disk_snapshot(snapshot(), observed_at=NOW).source, "render_disk_snapshot")
            self.assertEqual(review_old_deploy(old_deploy()).source, "render_old_deploy")


if __name__ == "__main__":
    unittest.main()
