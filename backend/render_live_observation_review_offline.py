"""Conservative, bounded review of a manually archived Render read-only observation.

No Render API, network, credentials, mutations, rollback, login or memory
access. Input is always *operator-transcribed* metadata, never an authenticated
request-level provenance attestation. Even perfect metadata never grants rights.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

_SHA = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_ID = re.compile(r"dep-[a-z0-9]{16,32}\Z", re.ASCII)
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{2,70}\Z", re.ASCII)
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z", re.ASCII)
_SOURCE = "transcribed_from_authorized_readonly_render_connector_not_a_signed_edge_attestation"
_REQUIRED = {
    "evidence_kind", "provenance", "observed_date_utc",
    "service_name", "repository", "deployment_branch", "live_deploy_id",
    "live_sha", "live_status", "live_trigger", "deploy_started_utc",
    "deploy_finished_utc", "previous_deploy_id", "previous_sha",
    "previous_status", "service_start_command", "auto_deploy_trigger",
    "health_check_path", "instance_expected_count",
    "instance_count_at_switch", "instance_count_after_switch",
    "instance_count_after_switch_observed_until_utc", "public_ipv4_cidr",
    "render_subdomain_policy", "maintenance_enabled", "suspended",
    "p3_supervisor_event_categories", "deploy_end_event",
    "failure_event_count_in_filtered_window", "http_metric_502_count",
    "http_metric_502_bucket_utc", "http_metric_200_count_at_later_bucket",
    "http_metric_200_bucket_utc", "http_200_paths_unavailable",
    "healthz_request_log_count", "app_error_log_count_in_filtered_window",
    "request_502_log_count_in_filtered_window",
    "public_http_health_response_verified", "production_edge_tls_peer_verified",
    "forwarding_header_sanitization_verified", "live_oidc_flags_checked",
    "live_memory_write_flags_checked", "paid_api_activation_checked",
    "disk_snapshot_and_compatibility_checked",
    "rollback_build_artifact_available_checked",
    "audit_authorizes_deployment", "audit_authorizes_rollback",
    "audit_authorizes_oidc", "audit_authorizes_memory_write",
    "audit_authorizes_paid_api",
}
_UNVERIFIED_FLAGS = (
    "public_http_health_response_verified",
    "production_edge_tls_peer_verified",
    "forwarding_header_sanitization_verified",
    "live_oidc_flags_checked",
    "live_memory_write_flags_checked",
    "paid_api_activation_checked",
    "disk_snapshot_and_compatibility_checked",
    "rollback_build_artifact_available_checked",
)
_FALSE_PERMISSIONS = (
    "audit_authorizes_deployment", "audit_authorizes_rollback",
    "audit_authorizes_oidc", "audit_authorizes_memory_write",
    "audit_authorizes_paid_api",
)
_COUNTERS = (
    "instance_expected_count", "instance_count_at_switch",
    "instance_count_after_switch", "failure_event_count_in_filtered_window",
    "http_metric_502_count", "http_metric_200_count_at_later_bucket",
    "healthz_request_log_count", "app_error_log_count_in_filtered_window",
    "request_502_log_count_in_filtered_window",
)
_TIME_FIELDS = (
    "deploy_started_utc", "deploy_finished_utc",
    "instance_count_after_switch_observed_until_utc",
    "http_metric_502_bucket_utc", "http_metric_200_bucket_utc",
)
_EXPECTED_EVENTS = (
    "signal_received:SIGTERM", "api_loop:stopping:SIGTERM",
    "relay:stopping:SIGTERM", "api_loop:started", "api_loop:ready",
    "relay:started",
)
_ALWAYS_BLOCKERS = (
    "archived_metadata_not_a_live_recheck",
    "public_http_health_not_independently_verified",
    "request_bound_tls_and_raw_peer_unattested",
    "forwarded_header_sanitization_unattested",
    "real_oidc_memory_and_paid_api_activation_not_checked",
    "durable_disk_snapshot_and_schema_rollback_not_checked",
    "previous_deploy_artifact_retention_not_checked",
    "rollback_not_user_authorized",
)


class ReadonlyRenderAuditUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("render_live_readonly_audit_unavailable")


@dataclass(frozen=True, repr=False)
class RenderReadonlyAudit:
    live_sha_matches_expected: bool
    p3_start_verified_by_metadata_only: bool
    live_service_recorded: bool
    instance_recovered_after_switch_in_metric: bool
    observed_http_502: bool
    later_http_200_count: int
    blockers: tuple[str, ...]
    may_deploy: bool = False
    may_rollback: bool = False
    may_enable_oidc: bool = False
    may_write_memory: bool = False
    may_use_paid_api: bool = False

    def __repr__(self):
        return "<RenderReadonlyAudit offline_metadata_only all_actions_blocked>"


def _utc(value: Any) -> dt.datetime:
    if type(value) is not str or len(value) > 40 or not value.endswith("Z"):
        raise ReadonlyRenderAuditUnavailable()
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ReadonlyRenderAuditUnavailable() from None
    if result.tzinfo is None or result.utcoffset() != dt.timedelta(0):
        raise ReadonlyRenderAuditUnavailable()
    return result


def classify_render_readonly_observation(
    record: object, *, expected_live_sha: str,
) -> RenderReadonlyAudit:
    """Strict archival *report*. Never verifies Render TLS, grants or release."""
    try:
        if (type(record) is not dict or set(record) != _REQUIRED
            or type(expected_live_sha) is not str
            or not _SHA.fullmatch(expected_live_sha)):
            raise ReadonlyRenderAuditUnavailable()
        for field in _REQUIRED:
            value = record[field]
            if type(value) is str and (
                len(value) > 300 or not value.isascii()
                or any(ord(c) < 32 or ord(c) > 126 for c in value)
            ):
                raise ReadonlyRenderAuditUnavailable()
        if (record["evidence_kind"] != "render_readonly_metadata_observation"
            or record["provenance"] != _SOURCE
            or record["observed_date_utc"] != "2026-10-28"
            or not _NAME.fullmatch(record["service_name"])
            or record["service_name"] != "ouou-home-telegram"
            or record["repository"] != "joehyun06-ui/Tidal_Echo"
            or type(record["deployment_branch"]) is not str
            or not _BRANCH.fullmatch(record["deployment_branch"])
            or record["deployment_branch"] != "feat/render-telegram-deployment"
            or type(record["live_sha"]) is not str
            or not _SHA.fullmatch(record["live_sha"])
            or type(record["previous_sha"]) is not str
            or not _SHA.fullmatch(record["previous_sha"])
            or record["live_sha"] == record["previous_sha"]
            or type(record["live_deploy_id"]) is not str
            or not _ID.fullmatch(record["live_deploy_id"])
            or type(record["previous_deploy_id"]) is not str
            or not _ID.fullmatch(record["previous_deploy_id"])
            or record["live_deploy_id"] == record["previous_deploy_id"]
            or record["live_status"] != "live"
            or record["previous_status"] != "deactivated"
            or record["live_trigger"] != "api"
            or record["deploy_end_event"] != "succeeded"
            or record["service_start_command"] != "python scripts/render_start_p3.py"
            or record["auto_deploy_trigger"] != "off"
            or record["health_check_path"] != "/healthz"
            or record["public_ipv4_cidr"] != "0.0.0.0/0"
            or record["render_subdomain_policy"] != "enabled"):
            raise ReadonlyRenderAuditUnavailable()
        if (any(type(record[f]) is not bool or record[f]
                for f in _UNVERIFIED_FLAGS + _FALSE_PERMISSIONS)
            or any(type(record[f]) is not int or not 0 <= record[f] <= 1000000
                   for f in _COUNTERS)
            or type(record["maintenance_enabled"]) is not bool
            or record["maintenance_enabled"]
            or type(record["suspended"]) is not bool
            or record["suspended"]
            or type(record["http_200_paths_unavailable"]) is not bool
            or record["http_200_paths_unavailable"] is not True
            or type(record["p3_supervisor_event_categories"]) is not list
            or record["p3_supervisor_event_categories"] != list(_EXPECTED_EVENTS)):
            raise ReadonlyRenderAuditUnavailable()
        moments = {f: _utc(record[f]) for f in _TIME_FIELDS}
        if (moments["deploy_finished_utc"] < moments["deploy_started_utc"]
            or moments["instance_count_after_switch_observed_until_utc"]
                < moments["deploy_finished_utc"]
            or moments["http_metric_200_bucket_utc"]
                <= moments["http_metric_502_bucket_utc"]):
            raise ReadonlyRenderAuditUnavailable()
        blockers = list(_ALWAYS_BLOCKERS)
        if record["live_sha"] != expected_live_sha:
            blockers.append("observed_live_sha_differs_from_reviewed_sha")
        if record["instance_count_at_switch"] == 0:
            blockers.append("sampled_instance_count_zero_during_switch")
        if record["instance_count_after_switch"] != record["instance_expected_count"]:
            blockers.append("instance_count_not_recovered_to_expected")
        if record["http_metric_502_count"] > 0:
            blockers.append("post_deploy_502_requires_correlation")
        if record["healthz_request_log_count"] == 0:
            blockers.append("healthz_log_absence_is_not_success")
        if record["request_502_log_count_in_filtered_window"] == 0:
            blockers.append("missing_502_request_log_prevents_root_cause")
        if record["failure_event_count_in_filtered_window"] != 0:
            blockers.append("failure_events_need_incident_review")
        return RenderReadonlyAudit(
            live_sha_matches_expected=record["live_sha"] == expected_live_sha,
            p3_start_verified_by_metadata_only=True,
            live_service_recorded=True,
            instance_recovered_after_switch_in_metric=(
                record["instance_count_after_switch"]
                == record["instance_expected_count"]
            ),
            observed_http_502=record["http_metric_502_count"] > 0,
            later_http_200_count=record["http_metric_200_count_at_later_bucket"],
            blockers=tuple(blockers),
        )
    except Exception:
        raise ReadonlyRenderAuditUnavailable() from None
