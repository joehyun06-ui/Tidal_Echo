"""Stage 35: classify supplied Telegram/Render evidence, without network or writes.

Inputs must come from an operator-controlled environment and are never persisted
by this module. Metadata is not proof of delivery, restorability, or release
approval. Outputs contain only fixed labels, not URLs, Bot Tokens, or messages.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EXPECTED_WEBHOOK_URL = "https://ouou-home-telegram.onrender.com/integrations/telegram/webhook"
EXPECTED_DISK_ID = "dsk-d9dk0gbrjlhs73aph4ig"
EXPECTED_MOUNT = "/var/data"
EXPECTED_OLD_DEPLOY = "dep-dbdd6ld9fdbs73anfue0"
EXPECTED_OLD_COMMIT = "a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c"


@dataclass(frozen=True, repr=False)
class EvidenceStatus:
    source: str
    status: str
    may_deploy: bool = False
    may_rollback: bool = False
    may_write_memory: bool = False
    may_enable_oidc: bool = False
    may_use_paid_api: bool = False

    def __repr__(self) -> str:
        return "<EvidenceStatus offline_no_release_authority>"


def review_telegram_webhook(payload: object) -> EvidenceStatus:
    """Review locally supplied getWebhookInfo JSON. Does NOT call Telegram.

    A match only establishes what the supplied response *claims*. It cannot
    establish its provenance, that Telegram sends updates, or live reachability.
    Do not log, serialize or retain the raw payload; its URL/message may contain
    tokens, identifiers or operator-sensitive details.
    """
    source = "telegram_webhook"
    if type(payload) is not dict or payload.get("ok") is not True:
        return EvidenceStatus(source, "invalid_or_unavailable")
    result = payload.get("result")
    if type(result) is not dict:
        return EvidenceStatus(source, "invalid_or_unavailable")
    url = result.get("url")
    if type(url) is not str or len(url) > 2048:
        return EvidenceStatus(source, "invalid_or_unavailable")
    if not url:
        return EvidenceStatus(source, "not_registered")
    try:
        matches = hmac.compare_digest(url.encode("utf-8"), EXPECTED_WEBHOOK_URL.encode("utf-8"))
    except UnicodeError:
        return EvidenceStatus(source, "invalid_or_unavailable")
    if not matches:
        return EvidenceStatus(source, "target_mismatch")
    pending = result.get("pending_update_count")
    if type(pending) is not int or pending < 0:
        return EvidenceStatus(source, "invalid_or_unavailable")
    if "last_error_date" in result and (
        type(result["last_error_date"]) is not int or result["last_error_date"] < 0
    ):
        return EvidenceStatus(source, "invalid_or_unavailable")
    if "last_error_message" in result and type(result["last_error_message"]) is not str:
        return EvidenceStatus(source, "invalid_or_unavailable")
    if "last_synchronization_error_date" in result and (
        type(result["last_synchronization_error_date"]) is not int
        or result["last_synchronization_error_date"] < 0
    ):
        return EvidenceStatus(source, "invalid_or_unavailable")
    if result.get("last_error_date") or result.get("last_synchronization_error_date"):
        # A historical error is NOT proof of a current delivery failure.
        return EvidenceStatus(source, "registered_historical_error")
    if pending:
        # Pending updates may be normal; do not infer a failure or success.
        return EvidenceStatus(source, "registered_pending_updates")
    return EvidenceStatus(source, "registered_no_reported_error")


def _utc(value: object) -> datetime | None:
    if type(value) is not str or len(value) > 64:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(timezone.utc)


def review_disk_snapshot(metadata: object, *, observed_at: datetime) -> EvidenceStatus:
    """Classify a supplied snapshot inventory item, without accessing Render.

    Never construe a snapshot record as a verified restorable backup. The disk
    must still be tested using approved restore procedures and SQLite checks.
    """
    source = "render_disk_snapshot"
    if type(metadata) is not dict or type(observed_at) is not datetime or observed_at.tzinfo is None:
        return EvidenceStatus(source, "invalid_or_unavailable")
    created = _utc(metadata.get("createdAt"))
    if created is None or metadata.get("diskId") != EXPECTED_DISK_ID:
        return EvidenceStatus(source, "invalid_or_unavailable")
    if "mountPath" in metadata and metadata["mountPath"] != EXPECTED_MOUNT:
        return EvidenceStatus(source, "disk_mismatch")
    now = observed_at.astimezone(timezone.utc)
    if created > now + timedelta(minutes=5):
        return EvidenceStatus(source, "invalid_or_unavailable")
    if now - created > timedelta(hours=48):
        return EvidenceStatus(source, "snapshot_metadata_stale")
    return EvidenceStatus(source, "snapshot_record_observed_restore_unverified")


def review_old_deploy(deploy: object) -> EvidenceStatus:
    """An existing *deactivated* deploy does not attest retained build artifacts."""
    source = "render_old_deploy"
    if type(deploy) is not dict:
        return EvidenceStatus(source, "invalid_or_unavailable")
    commit = deploy.get("commit")
    if (
        deploy.get("id") != EXPECTED_OLD_DEPLOY
        or type(commit) is not dict
        or commit.get("id") != EXPECTED_OLD_COMMIT
    ):
        return EvidenceStatus(source, "invalid_or_unavailable")
    if deploy.get("status") != "deactivated":
        return EvidenceStatus(source, "state_different")
    return EvidenceStatus(source, "candidate_record_only_artifact_unverified")
