"""Stage49 synthetic-only staged identity-review policy for unknown DB candidates.

This module contains NO filesystem, SQLite, subprocess, network or Render I/O.
The user has NOT authorized production unknown-file identity, headers or exports.
Never use this to process real file paths, filenames, keys or database content.
"""
from __future__ import annotations

import argparse
import json
import os

STAGED_KEYS = frozenset({
    "scope", "candidate_extension_count", "identity_review_count",
    "header_review_count", "sqlite_validated_count",
    "isolated_backup_verified_count", "isolated_restore_verified_count",
    "unknown_or_blocked_count",
})
PHASES = (
    "G1A_SEPARATE_APPROVAL_FOR_PRIVATE_FILENAME_IDENTITY",
    "G1B_SEPARATE_APPROVAL_FOR_READONLY_HEADER_OR_SQLITE_SCHEMA",
    "G1C_SEPARATE_APPROVAL_FOR_DEFINED_DB_BACKUP_SCOPE",
    "G1D_SEPARATE_APPROVAL_FOR_ISOLATED_RESTORE_OR_REPLAY",
)
MAX_SYNTHETIC = 20000


class DeniedEvidence(ValueError):
    pass


def exact_record(value: object) -> dict:
    if type(value) is not dict or set(value) != STAGED_KEYS:
        raise DeniedEvidence("unapproved_fields_or_shape")
    if value["scope"] != "SYNTHETIC_ONLY":
        raise DeniedEvidence("real_data_not_accepted")
    for key in STAGED_KEYS - {"scope"}:
        item = value[key]
        if type(item) is not int or not 0 <= item <= MAX_SYNTHETIC:
            raise DeniedEvidence("invalid_count")
    n = value["candidate_extension_count"]
    steps = (
        value["identity_review_count"], value["header_review_count"],
        value["sqlite_validated_count"],
        value["isolated_backup_verified_count"],
        value["isolated_restore_verified_count"],
    )
    if not (n >= steps[0] >= steps[1] >= steps[2] >= steps[3] >= steps[4]):
        raise DeniedEvidence("stage_gates_out_of_order")
    if value["unknown_or_blocked_count"] != n - steps[0]:
        raise DeniedEvidence("unreviewed_count_inconsistent")
    return value


def design_only(data: dict) -> dict:
    item = exact_record(data)
    count = item["candidate_extension_count"]
    # Deliberately never claim a live result from mock counts.
    return {
        "stage": 49,
        "result": "DESIGN_ONLY",
        "scope": "SYNTHETIC_ONLY",
        "candidate_extension_count": count,
        "synthetic_reviewed": item["identity_review_count"],
        "synthetic_sqlite_validated": item["sqlite_validated_count"],
        "synthetic_copy_checks": item["isolated_backup_verified_count"],
        "synthetic_restore_checks": item["isolated_restore_verified_count"],
        "actual_valid_sqlite_count": "UNKNOWN_NOT_INSPECTED",
        "usage_ledger_identity": "UNKNOWN_NOT_INSPECTED",
        "real_p1_contents_read": False,
        "real_file_names_read": False,
        "real_p1_files_exported": False,
        "production_authorization": "NONE",
        "production_transfer_allowed": False,
        "production_recovery_status": "NOT_TESTED",
        "approval_phases": list(PHASES),
        "notes": [
            "EXTENSION_IS_NOT_SQLITE_PROOF",
            "DO_NOT_EXPOSE_NAMES_PATHS_SIZE_HASHES_OR_SCHEMA",
            "NO_UNSALTED_NAME_HASH_OR_STABLE_FILE_ID_IN_PUBLIC_OUTPUT",
            "SQLite_WAL_FAMILY_REQUIRES_TRANSACTIONALLY_CONSISTENT_BACKUP",
            "CROSS_DATABASE_JOBS_MUST_NOT_AUTOMATICALLY_REPLAY",
        ],
    }


def synthetic_fixture() -> dict:
    return {
        "scope": "SYNTHETIC_ONLY",
        "candidate_extension_count": 11,
        "identity_review_count": 4,
        "header_review_count": 3,
        "sqlite_validated_count": 2,
        "isolated_backup_verified_count": 1,
        "isolated_restore_verified_count": 0,
        "unknown_or_blocked_count": 7,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage49 SIMULATED candidate-review authorization gates; no real input"
    )
    parser.add_argument("--synthetic-only", action="store_true", required=True)
    args = parser.parse_args()
    if args.synthetic_only and os.environ.get("GUITING_STAGE49_SYNTHETIC_ONLY") == "true":
        print(json.dumps(design_only(synthetic_fixture()), sort_keys=True, separators=(",", ":")))
        return 0
    print('{"stage":49,"result":"DENIED_WITHOUT_SYNTHETIC_GATE"}')
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
