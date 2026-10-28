"""OFF-by-default read-only metadata evidence matrix for Render OIDC release.

This does NOT establish network provenance. A JSON metadata record is ALWAYS
unverified input, never an authenticated Render response. No network, SDK,
secret reads, identity session creation, deployment or write authorization.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

_FLAG = "MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED"
_SHA = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z", re.ASCII)
_CIDR = re.compile(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}/(?:[0-9]|[12][0-9]|3[0-2])\Z")
_RECORD_FIELDS = frozenset((
    "service_name", "repository", "branch", "live_sha",
    "start_command", "auto_deploy_trigger", "render_subdomain_policy",
    "ingress_ipv4_cidrs", "health_check_path", "deploy_status",
))
_ALWAYS_BLOCKED = (
    "metadata_record_is_unverified_input",
    "real_edge_tls_termination_not_independently_attested",
    "per_request_ingress_peer_provenance_not_attested",
    "client_supplied_proxy_header_sanitization_not_attested",
    "public_host_alias_and_callback_policy_not_attested",
    "real_idp_browser_session_and_consent_not_accepted",
    "memory_writer_and_paid_api_not_authorized",
    "draft_security_stack_not_merged_and_deployed",
)


class RenderEvidenceOfflineError(RuntimeError):
    def __init__(self):
        super().__init__("render_release_evidence_offline_unavailable")


@dataclass(frozen=True, repr=False)
class RenderReleaseEvidence:
    blueprint_branch_matches_review: bool
    blueprint_command_matches_actual_record: bool | None
    blueprint_autodeploy_off: bool
    record_present_unverified: bool
    deployed_sha_matches_review: bool | None
    record_branch_matches_review: bool | None
    record_command_matches_p3: bool | None
    record_autodeploy_off: bool | None
    public_ipv4_unrestricted: bool | None
    default_subdomain_enabled: bool | None
    blockers: tuple[str, ...]
    may_merge: bool = False
    may_deploy: bool = False
    may_enable_oidc: bool = False
    may_write_memory: bool = False

    def __repr__(self):
        return "<RenderReleaseEvidence offline_only release_blocked>"


def _text(value: Any, max_length=300, *, spaces=False) -> str:
    if (type(value) is not str or not 1 <= len(value) <= max_length
        or not value.isascii()
        or any(ord(c) < (32 if spaces else 33) or ord(c) > 126 for c in value)):
        raise RenderEvidenceOfflineError()
    return value


def _ref(branch, sha):
    b = _text(branch, 200)
    s = _text(sha, 40)
    if (not _BRANCH.fullmatch(b) or ".." in b or "//" in b
        or b.startswith(("/", "-")) or b.endswith(("/", "."))
        or not _SHA.fullmatch(s)):
        raise RenderEvidenceOfflineError()
    return b, s


def _cidr(value):
    if type(value) is not str or not _CIDR.fullmatch(value):
        raise RenderEvidenceOfflineError()
    base, prefix = value.split("/")
    if any(int(part) > 255 or (len(part) > 1 and part.startswith("0"))
           for part in base.split(".")):
        raise RenderEvidenceOfflineError()
    return f"{base}/{int(prefix)}"


def assess_render_release_evidence_offline(
    blueprint: object, *,
    review_branch: str, review_sha: str,
    observed_metadata: object = None,
) -> RenderReleaseEvidence:
    """A drift *report*, never a release approval or real edge evidence."""
    if os.environ.get(_FLAG) != "true":
        raise RenderEvidenceOfflineError()
    try:
        branch, sha = _ref(review_branch, review_sha)
        if type(blueprint) is not dict or type(blueprint.get("services")) is not list:
            raise RenderEvidenceOfflineError()
        targets = [s for s in blueprint["services"]
                   if type(s) is dict and s.get("name") == "ouou-home-telegram"]
        if len(targets) != 1 or targets[0].get("type") != "web":
            raise RenderEvidenceOfflineError()
        service = targets[0]
        bp_branch = _text(service.get("branch"), 200)
        bp_command = _text(service.get("startCommand"), spaces=True)
        override = service.get("autoDeployTrigger")
        bp_off = (override == "off" if override is not None
                  else service.get("autoDeploy") is False)
        bp_branch_match = bp_branch == branch
        blockers = list(_ALWAYS_BLOCKED)
        if not bp_branch_match:
            blockers.append("blueprint_branch_not_review_branch")
        if not bp_off:
            blockers.append("blueprint_autodeploy_not_off")
        if bp_command != "python scripts/render_start.py":
            blockers.append("blueprint_start_command_changed")
        if observed_metadata is None:
            blockers.append("live_metadata_record_missing")
            results = (None,) * 7
        else:
            if type(observed_metadata) is not dict or set(observed_metadata) != _RECORD_FIELDS:
                raise RenderEvidenceOfflineError()
            record = observed_metadata
            strings = {key: _text(record[key], spaces=(key == "start_command"))
                       for key in _RECORD_FIELDS - {"ingress_ipv4_cidrs"}}
            if (strings["service_name"] != "ouou-home-telegram"
                or strings["repository"] != "joehyun06-ui/Tidal_Echo"
                or not _BRANCH.fullmatch(strings["branch"])
                or not _SHA.fullmatch(strings["live_sha"])
                or strings["auto_deploy_trigger"] not in ("off", "commit", "checksPass")
                or strings["render_subdomain_policy"] not in ("enabled", "disabled")
                or strings["health_check_path"] != "/healthz"
                or strings["deploy_status"] not in ("live", "not_live")):
                raise RenderEvidenceOfflineError()
            raw_cidrs = record["ingress_ipv4_cidrs"]
            if (type(raw_cidrs) is not list or not 1 <= len(raw_cidrs) <= 8
                or len(raw_cidrs) != len(set(_cidr(x) for x in raw_cidrs))):
                raise RenderEvidenceOfflineError()
            all_ipv4 = "0.0.0.0/0" in raw_cidrs
            deployed_match = strings["live_sha"] == sha
            branch_match = strings["branch"] == branch
            command_p3 = strings["start_command"] == "python scripts/render_start_p3.py"
            off = strings["auto_deploy_trigger"] == "off"
            bp_command_same = bp_command == strings["start_command"]
            if not bp_command_same:
                blockers.append("blueprint_command_differs_from_record")
            if not deployed_match:
                blockers.append("live_commit_not_reviewed_sha")
            if not branch_match:
                blockers.append("live_branch_not_review_branch")
            if not command_p3:
                blockers.append("observed_p3_entrypoint_not_confirmed")
            if not off:
                blockers.append("observed_auto_deploy_not_off")
            if strings["deploy_status"] != "live":
                blockers.append("observed_deployment_not_live")
            if all_ipv4:
                blockers.append("public_edge_ipv4_unrestricted_not_direct_socket_proof")
            if strings["render_subdomain_policy"] == "enabled":
                blockers.append("default_onrender_host_alias_requires_policy")
            results = (bp_command_same, deployed_match, branch_match,
                       command_p3, off, all_ipv4,
                       strings["render_subdomain_policy"] == "enabled")
        if os.environ.get(_FLAG) != "true":
            raise RenderEvidenceOfflineError()
        return RenderReleaseEvidence(
            blueprint_branch_matches_review=bp_branch_match,
            blueprint_command_matches_actual_record=results[0],
            blueprint_autodeploy_off=bp_off,
            record_present_unverified=observed_metadata is not None,
            deployed_sha_matches_review=results[1],
            record_branch_matches_review=results[2],
            record_command_matches_p3=results[3],
            record_autodeploy_off=results[4],
            public_ipv4_unrestricted=results[5],
            default_subdomain_enabled=results[6],
            blockers=tuple(blockers),
        )
    except Exception:
        raise RenderEvidenceOfflineError() from None
