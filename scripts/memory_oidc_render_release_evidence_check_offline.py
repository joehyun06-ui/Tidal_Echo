#!/usr/bin/env python3
"""Operator-facing *offline* release blocker reporter; never approves deploy.

Reads only a repository Blueprint and an optional strictly redacted JSON
metadata record. No Render credentials or API, no network, no mutation.
Record is unverified input even if it was originally copied from Render.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.memory_oidc_render_release_evidence_offline import (
    RenderEvidenceOfflineError, assess_render_release_evidence_offline,
)


def read_bounded_json(filename: str, max_bytes: int):
    content = Path(filename).read_bytes()
    if not content or len(content) > max_bytes:
        raise RenderEvidenceOfflineError()
    return json.loads(content.decode("utf-8", errors="strict"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Report offline release blockers. Never authorizes a deployment."
    )
    parser.add_argument("--blueprint", default="render.yaml")
    parser.add_argument("--observed", default=None,
                        help="Strictly redacted non-secret metadata JSON; NOT proof")
    parser.add_argument("--review-branch", required=True)
    parser.add_argument("--review-sha", required=True)
    args = parser.parse_args(argv)
    try:
        result = assess_render_release_evidence_offline(
            read_bounded_json(args.blueprint, 128 * 1024),
            review_branch=args.review_branch,
            review_sha=args.review_sha,
            observed_metadata=(
                read_bounded_json(args.observed, 8 * 1024)
                if args.observed is not None else None
            ),
        )
    except Exception:
        # Never echo supplied paths, JSON, credentials, branch or exception.
        print(json.dumps({
            "status": "review_unavailable", "may_merge": False,
            "may_deploy": False, "may_enable_oidc": False,
            "may_write_memory": False,
        }, sort_keys=True))
        return 2
    print(json.dumps({
        "status": "release_blocked",
        "blueprint_branch_matches_review": result.blueprint_branch_matches_review,
        "blueprint_command_matches_actual_record":
            result.blueprint_command_matches_actual_record,
        "blueprint_autodeploy_off": result.blueprint_autodeploy_off,
        "record_present_unverified": result.record_present_unverified,
        "deployed_sha_matches_review": result.deployed_sha_matches_review,
        "record_branch_matches_review": result.record_branch_matches_review,
        "record_command_matches_p3": result.record_command_matches_p3,
        "record_autodeploy_off": result.record_autodeploy_off,
        "public_ipv4_unrestricted": result.public_ipv4_unrestricted,
        "default_subdomain_enabled": result.default_subdomain_enabled,
        "blockers": list(result.blockers),
        "may_merge": False,
        "may_deploy": False,
        "may_enable_oidc": False,
        "may_write_memory": False,
    }, sort_keys=True))
    # Exit code NEVER means release-approved. True Render metadata is not
    # evidence for per-request TLS provenance or a reviewed IdP session.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
