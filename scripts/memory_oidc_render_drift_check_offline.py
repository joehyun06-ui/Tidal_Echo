#!/usr/bin/env python3
"""Read-only local Blueprint/snapshot comparison. Never returns release approval."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.memory_oidc_render_drift_review import (
    RenderDriftReviewError, assess_render_deployment_drift,
)

_MAX_BLUEPRINT = 128 * 1024
_MAX_SNAPSHOT = 8 * 1024


def _read_json(path: str, limit: int):
    data = Path(path).read_bytes()
    if not data or len(data) > limit:
        raise RenderDriftReviewError()
    return json.loads(data.decode("utf-8", errors="strict"))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline metadata drift review; NEVER authorizes a deploy.",
    )
    parser.add_argument("--blueprint", default="render.yaml")
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--review-branch", required=True)
    parser.add_argument("--review-sha", required=True)
    args = parser.parse_args(argv)
    try:
        result = assess_render_deployment_drift(
            _read_json(args.blueprint, _MAX_BLUEPRINT),
            expected_review_branch=args.review_branch,
            expected_review_sha=args.review_sha,
            snapshot=(
                _read_json(args.snapshot, _MAX_SNAPSHOT)
                if args.snapshot else None
            ),
        )
    except Exception:
        # Do not echo paths, secrets, raw JSON, service details or traceback.
        print(json.dumps({"status": "review_unavailable", "may_deploy": False}))
        return 2
    print(json.dumps({
        "status": "release_blocked",
        "blueprint_branch_matches_review": result.blueprint_branch_matches_review,
        "blueprint_auto_deploy_off": result.blueprint_auto_deploy_off,
        "blueprint_start_command_expected": result.blueprint_start_command_expected,
        "snapshot_supplied_unverified": result.snapshot_supplied,
        "recorded_branch_matches_review": result.recorded_branch_matches_review,
        "recorded_commit_matches_review": result.recorded_commit_matches_review,
        "recorded_start_command_matches_blueprint":
            result.recorded_start_command_matches_blueprint,
        "recorded_auto_deploy_off": result.recorded_auto_deploy_off,
        "blockers": list(result.blockers),
        "may_deploy": False,
        "may_enable_browser_login": False,
        "may_write_memory": False,
    }, sort_keys=True))
    # Even a perfect provided snapshot does not establish genuine provenance.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
