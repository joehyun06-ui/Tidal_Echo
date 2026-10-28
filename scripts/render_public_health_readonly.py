#!/usr/bin/env python3
"""Read-only public health observation. Disabled unless locally approved.

Never use as a release, rollback, OIDC, Telegram, memory or paid-API gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.render_public_health_probe_readonly import probe_render_public_health


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pinned Render HTTPS health GET; OFF by default")
    parser.add_argument("--endpoint", choices=("healthz", "readyz"), required=True)
    args = parser.parse_args(argv)
    result = probe_render_public_health(args.endpoint)
    print(json.dumps({
        "endpoint": result.endpoint,
        "status": result.status,
        "http_status": result.http_status,
        "check_count": result.check_count,
        "failed_checks": list(result.failed_checks),
        "may_deploy": False, "may_rollback": False,
        "may_enable_oidc": False, "may_write_memory": False,
        "may_use_paid_api": False,
    }, sort_keys=True))
    # Never supply a zero exit status as evidence that a release is approved.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
