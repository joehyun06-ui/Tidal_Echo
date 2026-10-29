"""Stage52 P0: anonymous root-owner relationship, NO DIRECTORY ENUMERATION.

Owner-approved P0 production *metadata* diagnostic, to be manually run only
after exact-source synthetic CI. Does not open /var/data, list directory items,
inspect any candidates, read content/secrets, or write. P1 NOT implemented.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path("/var/data")
STAGE = 52
GATE = "G1B1_PERMISSION_P0"


class ScopeChanged(ValueError):
    pass


def snapshot_root(root: Path) -> tuple:
    st = root.lstat()  # NO OPEN; NO scandir; NO reads; NO directory atime change
    if not stat.S_ISDIR(st.st_mode):
        raise ScopeChanged("root_invalid_type")
    return (st.st_mode, st.st_dev, st.st_ino, st.st_uid,
            st.st_gid, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def audit_p0(root: Path = ROOT, *, synthetic: bool = False,
             synthetic_euid: int | None = None) -> dict:
    if not root.is_absolute() or ".." in root.parts:
        raise ScopeChanged("root_invalid")
    if not synthetic:
        if (root != ROOT or os.name != "posix" or
                not sys.platform.startswith("linux") or
                not hasattr(os, "geteuid") or synthetic_euid is not None):
            raise ScopeChanged("production_scope_invalid")
    elif root == ROOT:
        # Prevent synthetic mode bypass against the production target.
        raise ScopeChanged("synthetic_production_target")
    first = snapshot_root(root)
    owner = first[3]
    caller = synthetic_euid if synthetic else os.geteuid()
    if not isinstance(caller, int) or caller < 0:
        raise ScopeChanged("caller_invalid")
    second = snapshot_root(root)
    if first != second:
        raise ScopeChanged("root_metadata_changed")
    return {
        "stage": STAGE,
        "gate": GATE,
        "scope": "SYNTHETIC_ONLY" if synthetic else "AUTHORIZED_ROOT_METADATA_ONLY",
        "result": "ROOT_OWNER_RELATION_HINT_ONLY",
        "root_owner_relation": "MATCH" if owner == caller else "MISMATCH",
        "candidate_count": "UNKNOWN_NOT_ENUMERATED",
        "candidate_owner_counts": None,
        "candidate_file_opens": 0,
        "candidate_content_bytes_read": 0,
        "root_directory_opened": False,
        "root_directory_enumerated": False,
        "filesystem_noatime_verified": False,
        "g1b1_retried": False,
        "sqlite_header_or_schema_checked": False,
        "source_writes": False,
        "backup_or_restore": "NOT_AUTHORIZED_NOT_TESTED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--perform-stage52-p0", action="store_true", required=True)
    parser.add_argument("--owner-authorized-stage52-p0", action="store_true", required=True)
    parser.add_argument("--synthetic-root", type=Path)
    parser.add_argument("--synthetic-euid", type=int)
    args = parser.parse_args()
    try:
        synthetic = args.synthetic_root is not None
        if synthetic:
            if (os.environ.get("GUITING_STAGE52_SYNTHETIC_ONLY") != "true" or
                    args.synthetic_euid is None):
                raise ScopeChanged("synthetic_gate_missing")
        elif args.synthetic_euid is not None:
            raise ScopeChanged("synthetic_uid_in_production")
        result = audit_p0(
            args.synthetic_root if synthetic else ROOT,
            synthetic=synthetic,
            synthetic_euid=args.synthetic_euid,
        )
    except (ScopeChanged, OSError, ValueError, TypeError):
        # No paths, UIDs, raw permission bits, errno or traceback.
        print('{"stage":52,"gate":"G1B1_PERMISSION_P0","result":"STOP_SCOPE_OR_ACCESS_UNVERIFIED"}')
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
