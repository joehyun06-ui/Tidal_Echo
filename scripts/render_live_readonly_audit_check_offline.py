#!/usr/bin/env python3
"""Verify a redacted, archived Render observation without connecting to Render.

Permanent exit code 2: neither audit data nor CLI success grants deploy,
rollback, login, memory-write or paid API authority.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.render_live_observation_review_offline import (
    ReadonlyRenderAuditUnavailable,
    classify_render_readonly_observation,
)


def main(argv=None):
    p = argparse.ArgumentParser(description="Offline redacted Render observation review; always blocked")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--expected-live-sha", required=True)
    args = p.parse_args(argv)
    try:
        path = Path(args.snapshot)
        if not path.is_file() or path.stat().st_size > 8192:
            raise ReadonlyRenderAuditUnavailable()
        raw = path.read_bytes()
        if not raw:
            raise ReadonlyRenderAuditUnavailable()
        record = json.loads(raw.decode("utf-8", "strict"))
        result = classify_render_readonly_observation(
            record, expected_live_sha=args.expected_live_sha,
        )
        fields = {
            "status": "release_and_rollback_blocked",
            "live_sha_matches_expected": result.live_sha_matches_expected,
            "instance_recovered_in_sample": result.instance_recovered_after_switch_in_metric,
            "observed_http_502": result.observed_http_502,
            "later_http_200_count_without_path_proof": result.later_http_200_count,
            "blockers": list(result.blockers),
        }
    except Exception:
        fields = {"status": "audit_unavailable"}
    for key in ("may_deploy", "may_rollback", "may_enable_oidc",
                "may_write_memory", "may_use_paid_api"):
        fields[key] = False
    print(json.dumps(fields, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
