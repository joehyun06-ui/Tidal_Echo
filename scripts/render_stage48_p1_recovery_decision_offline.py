"""Stage48: data-free decision policy for owner-observed Stage47 metadata.

Pure in-memory policy only. NO filesystem, network, environment, SQLite or
production data reads, backups, restoration, migration, provider execution.
Reject any unexpected raw names, paths, keys, freeform text or schema drift.
"""
from __future__ import annotations

import argparse
import json
import os

FIXED_DB = (
    "codex-generation.db",
    "codex-generation.db-wal",
    "codex-generation.db-shm",
    "codex-generation.db-journal",
)
ANONYMOUS_BUCKETS = (
    "directories", "symlinks", "other", "secret_extension_candidates",
    "sqlite_sidecar_candidates", "sqlite_candidates",
    "structured_config_candidates", "other_files",
)
EXPECTED_KEYS = frozenset((
    "stage", "scope", "result", "codex_generation_files",
    "codex_home_status", "codex_home", "anonymous_unclassified_root",
    "contents_read", "environment_values_read", "file_names_emitted",
    "source_writes", "backup_created", "restoration_tested",
    "usage_ledger_status",
))
STATUSES = frozenset(("present", "missing", "wrong_type", "inaccessible"))


class RejectedEvidence(ValueError):
    pass


def natural(value: object) -> int:
    if type(value) is not int or value < 0:
        raise RejectedEvidence("invalid_nonnegative_count")
    return value


def keys_exact(data: object, expected: set[str] | frozenset[str]) -> dict:
    if not isinstance(data, dict) or set(data) != expected:
        raise RejectedEvidence("unexpected_schema")
    return data


def evaluate(stage47: dict) -> dict:
    keys_exact(stage47, EXPECTED_KEYS)
    if stage47["stage"] != 47 or stage47["scope"] not in (
        "LIVE_READONLY_METADATA", "SYNTHETIC_ONLY"
    ) or stage47["result"] not in ("PASS_METADATA", "PARTIAL_METADATA"):
        raise RejectedEvidence("wrong_evidence")
    for flag in (
        "contents_read", "environment_values_read", "file_names_emitted",
        "source_writes", "backup_created", "restoration_tested"
    ):
        if stage47[flag] is not False:
            raise RejectedEvidence("non_readonly_evidence")
    if stage47["usage_ledger_status"] != "UNKNOWN_CONFIG_DEPENDENT":
        raise RejectedEvidence("ledger_state_not_observed")
    db = keys_exact(stage47["codex_generation_files"], set(FIXED_DB))
    for name, entry in db.items():
        if not isinstance(entry, dict) or entry.get("status") not in STATUSES:
            raise RejectedEvidence("db_metadata_invalid")
        status = entry["status"]
        if status == "missing":
            keys_exact(entry, {"status"})
        elif status == "inaccessible":
            keys_exact(entry, {"status"})
        else:
            expected = {"status", "type"}
            if status == "present":
                expected.add("bytes")
            keys_exact(entry, expected)
            if entry["type"] not in ("file", "dir", "symlink", "other"):
                raise RejectedEvidence("db_metadata_type")
            if status == "present":
                if entry["type"] != "file": raise RejectedEvidence("present_non_file")
                natural(entry["bytes"])
    home_status = stage47["codex_home_status"]
    if home_status not in ("present", "missing", "wrong_type", "inaccessible"):
        raise RejectedEvidence("home_status_invalid")
    home = stage47["codex_home"]
    home_truncated = False
    if home_status == "present":
        keys_exact(home, {"files", "directories", "symlinks", "other",
                          "file_bytes", "truncated", "stop_reason"})
        for key in ("files", "directories", "symlinks", "other", "file_bytes"):
            natural(home[key])
        if type(home["truncated"]) is not bool:
            raise RejectedEvidence("home_truncation_invalid")
        if home["stop_reason"] not in (
            "none", "entry_limit", "time_limit", "depth_limit", "other_mount",
            "unavailable_child", "changed_during_scan", "unreadable"
        ):
            raise RejectedEvidence("home_limit_invalid")
        if home["truncated"] != (home["stop_reason"] != "none"):
            raise RejectedEvidence("home_truncation_inconsistent")
        home_truncated = home["truncated"]
    elif home is not None:
        raise RejectedEvidence("home_unexpected_metrics")

    root = keys_exact(stage47["anonymous_unclassified_root"],
                      {"total", "buckets", "truncated"})
    total = natural(root["total"])
    buckets = keys_exact(root["buckets"], set(ANONYMOUS_BUCKETS))
    for number in buckets.values():
        natural(number)
    if type(root["truncated"]) is not bool:
        raise RejectedEvidence("root_truncation_invalid")
    if sum(buckets.values()) != total:
        raise RejectedEvidence("anonymous_totals_inconsistent")
    if stage47["result"] == "PASS_METADATA" and (home_truncated or root["truncated"]):
        raise RejectedEvidence("scan_result_inconsistent")
    if stage47["result"] == "PARTIAL_METADATA" and not (home_truncated or root["truncated"]):
        raise RejectedEvidence("partial_without_reason")

    file_status = db["codex-generation.db"]["status"]
    return {
        "stage": 48,
        "result": "DESIGN_ONLY",
        "evidence_scope": stage47["scope"],
        "metadata": "BOUNDED_COMPLETE" if stage47["result"] == "PASS_METADATA" else "PARTIAL",
        "known_codex_sqlite": "PRESENT_UNVALIDATED" if file_status == "present"
                              else "NOT_CONFIRMED_VALID",
        "unknown_root_extension_candidates": buckets["sqlite_candidates"],
        "unknown_valid_sqlite_confirmed": 0,
        "unknown_valid_sqlite_confirmation": "NOT_TESTED",
        "codex_home": "PRESENT_UNCLASSIFIED" if home_status == "present"
                      else "NOT_CONFIRMED",
        "ledger_activation": "UNKNOWN_NOT_INSPECTED",
        "authorization_required_before_content_reads": True,
        "authorization_required_before_backups": True,
        "authorizes_export_or_backup": False,
        "any_database_opened": False,
        "production_changed": False,
        "real_p1_restore": "NOT_TESTED",
        "native_snapshot_restore": "NOT_TESTED",
        "full_production_restore": "NOT_TESTED",
        "next_decisions": [
            "REVIEW_P1_CODEX_SQLITE_READONLY_CONTENT_PROBE_SCOPE",
            "REVIEW_ANONYMOUS_ROOT_CANDIDATE_IDENTITY_SCOPE",
            "REVIEW_CODEX_HOME_SECRET_SEPARATION_SCOPE",
            "REVIEW_LEDGER_ACTIVATION_WITHOUT_KEY_DISCLOSURE",
            "SEPARATE_AUTHORIZATION_BEFORE_ANY_REAL_P1_EXPORT",
        ],
    }


def fixture() -> dict:
    """Fake metadata. Never load production input via the CLI."""
    return {
        "stage": 47, "scope": "SYNTHETIC_ONLY", "result": "PASS_METADATA",
        "codex_generation_files": {
            "codex-generation.db": {"status": "present", "type": "file", "bytes": 1024},
            "codex-generation.db-wal": {"status": "missing"},
            "codex-generation.db-shm": {"status": "missing"},
            "codex-generation.db-journal": {"status": "missing"},
        },
        "codex_home_status": "present",
        "codex_home": {"files": 2, "directories": 1, "symlinks": 0, "other": 0,
                       "file_bytes": 1024, "truncated": False, "stop_reason": "none"},
        "anonymous_unclassified_root": {
            "total": 3,
            "buckets": dict(directories=1, symlinks=0, other=0,
                            secret_extension_candidates=0,
                            sqlite_sidecar_candidates=0, sqlite_candidates=2,
                            structured_config_candidates=0, other_files=0),
            "truncated": False,
        },
        "contents_read": False, "environment_values_read": False,
        "file_names_emitted": False, "source_writes": False,
        "backup_created": False, "restoration_tested": False,
        "usage_ledger_status": "UNKNOWN_CONFIG_DEPENDENT",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthetic-only", action="store_true", required=True)
    args = parser.parse_args()
    if args.synthetic_only and os.environ.get("GUITING_STAGE48_SYNTHETIC_ONLY") == "true":
        print(json.dumps(evaluate(fixture()), sort_keys=True, separators=(",", ":")))
        return 0
    print('{"stage":48,"result":"DENIED_NOT_SYNTHETIC"}')
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
