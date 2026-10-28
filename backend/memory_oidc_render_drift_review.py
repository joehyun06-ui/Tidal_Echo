"""Read-only, OFF-by-default Render deployment metadata drift review.

An operator-supplied JSON snapshot is NOT trusted Render evidence. This module
compares non-secret metadata with a repository Blueprint and a specified review
SHA. It never grants deployment, OIDC login or memory-write authority.
No SDK, HTTP calls, secrets or filesystem mutations.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping

_FLAG = "MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED"
_SHA = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z", re.ASCII)
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z", re.ASCII)
_SNAPSHOT_KEYS = frozenset({
    "service_name", "service_type", "repository_full_name", "branch",
    "deployed_commit_sha", "start_command", "auto_deploy_trigger",
    "deploy_status",
})
_BASE_BLOCKERS = (
    "real_render_workspace_not_confirmed",
    "snapshot_source_not_authenticated",
    "raw_socket_peer_not_attested",
    "edge_forwarded_headers_not_attested",
    "network_acl_not_attested",
    "tls_host_origin_not_attested",
    "real_idp_login_not_accepted",
    "human_consent_and_memory_writer_not_accepted",
)


class RenderDriftReviewError(RuntimeError):
    def __init__(self):
        super().__init__("memory_oidc_render_drift_review_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class RenderDriftReviewResult:
    """Metadata comparison only; never a real Render or release attestation."""
    blueprint_branch_matches_review: bool
    blueprint_auto_deploy_off: bool
    blueprint_start_command_expected: bool
    snapshot_supplied: bool
    recorded_branch_matches_review: bool | None
    recorded_commit_matches_review: bool | None
    recorded_start_command_matches_blueprint: bool | None
    recorded_auto_deploy_off: bool | None
    blockers: tuple[str, ...] = field(repr=False)
    may_deploy: bool = False
    may_enable_browser_login: bool = False
    may_write_memory: bool = False

    def __repr__(self):
        return "<RenderDriftReviewResult blocked offline_metadata_only>"


def _clean(value: object, limit: int, *, allow_space: bool = False) -> str:
    if (
        type(value) is not str or not 1 <= len(value) <= limit
        or not value.isascii()
        or any(ord(c) < (32 if allow_space else 33) or ord(c) > 126 for c in value)
    ):
        raise RenderDriftReviewError()
    return value


def _review_ref(branch: object, sha: object) -> tuple[str, str]:
    b, s = _clean(branch, 200), _clean(sha, 40)
    if (
        not _BRANCH.fullmatch(b) or b.startswith(("/", "-"))
        or ".." in b or "//" in b or b.endswith(("/", "."))
        or not _SHA.fullmatch(s)
    ):
        raise RenderDriftReviewError()
    return b, s


def _auto_off(service: Mapping[str, object]) -> bool:
    # Current Render Blueprint autoDeployTrigger overrides the legacy field.
    trigger = service.get("autoDeployTrigger")
    if trigger is not None:
        return type(trigger) is str and trigger == "off"
    return service.get("autoDeploy") is False


def assess_render_deployment_drift(
    blueprint: object, *,
    expected_review_branch: str,
    expected_review_sha: str,
    snapshot: object = None,
) -> RenderDriftReviewResult:
    """Return blockers only from static metadata; never authorize deployment.

    'snapshot' is a *manually supplied, unverified* redacted metadata record.
    It is not an authenticated connector response or a proxy trust proof.
    """
    if os.environ.get(_FLAG) != "true":
        raise RenderDriftReviewError()
    try:
        branch, sha = _review_ref(expected_review_branch, expected_review_sha)
        if type(blueprint) is not dict or type(blueprint.get("services")) is not list:
            raise RenderDriftReviewError()
        matches = [
            s for s in blueprint["services"]
            if type(s) is dict and s.get("name") == "ouou-home-telegram"
        ]
        if len(matches) != 1:
            raise RenderDriftReviewError()
        service = matches[0]
        if service.get("type") != "web":
            raise RenderDriftReviewError()
        b_branch = _clean(service.get("branch"), 200)
        b_command = _clean(service.get("startCommand"), 300, allow_space=True)
        blueprint_branch_ok = b_branch == branch
        blueprint_auto_off = _auto_off(service)
        blueprint_start_ok = b_command == "python scripts/render_start.py"
        blockers = list(_BASE_BLOCKERS)
        if not blueprint_branch_ok:
            blockers.append("blueprint_branch_differs_from_review")
        if not blueprint_auto_off:
            blockers.append("blueprint_autodeploy_not_off")
        if not blueprint_start_ok:
            blockers.append("blueprint_start_command_unreviewed")

        state: tuple[bool | None, bool | None, bool | None, bool | None]
        if snapshot is None:
            blockers.append("live_service_snapshot_missing")
            state = (None, None, None, None)
        else:
            if type(snapshot) is not dict or set(snapshot) != _SNAPSHOT_KEYS:
                raise RenderDriftReviewError()
            # No additional keys: never accept envVars, tokens, headers, logs
            # or claims about a proof from unauthenticated JSON.
            values = {k: _clean(v, 300, allow_space=(k == "start_command"))
                      for k, v in snapshot.items()}
            if (
                values["service_name"] != "ouou-home-telegram"
                or values["service_type"] != "web_service"
                or not _REPO.fullmatch(values["repository_full_name"])
                or not _SHA.fullmatch(values["deployed_commit_sha"])
                or not _BRANCH.fullmatch(values["branch"])
                or values["auto_deploy_trigger"] not in ("off", "commit", "checksPass")
                or values["deploy_status"] not in ("live", "not_live")
            ):
                raise RenderDriftReviewError()
            state = (
                values["branch"] == branch,
                values["deployed_commit_sha"] == sha,
                values["start_command"] == b_command,
                values["auto_deploy_trigger"] == "off",
            )
            if values["repository_full_name"] != "joehyun06-ui/Tidal_Echo":
                blockers.append("recorded_repository_mismatch")
            if not state[0]:
                blockers.append("recorded_branch_differs_from_review")
            if not state[1]:
                blockers.append("recorded_commit_differs_from_review")
            if not state[2]:
                blockers.append("recorded_start_command_differs_from_blueprint")
            if not state[3]:
                blockers.append("recorded_autodeploy_not_off")
            if values["deploy_status"] != "live":
                blockers.append("recorded_deployment_not_live")

        if os.environ.get(_FLAG) != "true":
            raise RenderDriftReviewError()
        return RenderDriftReviewResult(
            blueprint_branch_matches_review=blueprint_branch_ok,
            blueprint_auto_deploy_off=blueprint_auto_off,
            blueprint_start_command_expected=blueprint_start_ok,
            snapshot_supplied=snapshot is not None,
            recorded_branch_matches_review=state[0],
            recorded_commit_matches_review=state[1],
            recorded_start_command_matches_blueprint=state[2],
            recorded_auto_deploy_off=state[3],
            blockers=tuple(blockers),
        )
    except Exception:
        raise RenderDriftReviewError() from None
