"""Stage50 G1A: private name-hint-only review for eleven root SQLite candidates.

Operator-authorized G1A only. No file opens, header reads, SQLite connections,
environment reads (except synthetic test gate), writes, network or transfers.
Never prints real filenames, paths, hashes, inode IDs, sizes or timestamps.
Name hints are NOT confirmed file identity, valid SQLite or recovery proof.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat

ROOT = Path("/var/data")
EXPECTED_SQLITE_CANDIDATES = 11
EXPECTED_UNKNOWN_DIRECTORIES = 4
MAX_ROOT_ENTRIES = 4096
FIXED_ROOT_NAMES = frozenset({
    "relay.db", "relay.db-wal", "relay.db-shm", "uploads",
    "brain_target", "api_loop.config.json", "codex-home",
    "codex-workspace", "codex-generation.db",
})
SQLITE_EXTENSIONS = frozenset({".db", ".sqlite", ".sqlite3"})
HINT_BUCKETS = (
    "codex_name_hint", "ledger_name_hint",
    "archive_name_hint", "unclassified_name_hint",
)


class ScopeChanged(ValueError):
    pass


def name_hint(basename: str) -> str:
    """Untrusted hints based on a private name. NEVER actual DB identity."""
    lower = basename.casefold()
    if "codex" in lower or "generation" in lower or "thread" in lower:
        return "codex_name_hint"
    if "ledger" in lower or "usage" in lower or "billing" in lower:
        return "ledger_name_hint"
    if "backup" in lower or "archive" in lower or "snapshot" in lower or "old" in lower:
        return "archive_name_hint"
    return "unclassified_name_hint"


def scan_once(root_fd_or_path: int | Path, *, synthetic: bool) -> tuple[tuple, dict]:
    candidates = []
    unknown_dirs = 0
    fingerprint = []
    with os.scandir(root_fd_or_path) as iterator:
        for item in iterator:
            if item.name in FIXED_ROOT_NAMES:
                continue
            if len(fingerprint) >= MAX_ROOT_ENTRIES:
                raise ScopeChanged("root_entry_limit")
            try:
                if synthetic:
                    st = item.stat(follow_symlinks=False)
                else:
                    st = os.stat(item.name, dir_fd=root_fd_or_path, follow_symlinks=False)
            except OSError:
                raise ScopeChanged("metadata_unavailable") from None
            # Non-regular files and hardlinks are not treated as safe candidates.
            is_dir = stat.S_ISDIR(st.st_mode)
            is_file = stat.S_ISREG(st.st_mode)
            if is_dir:
                unknown_dirs += 1
                kind = "dir"
            elif is_file and st.st_nlink == 1 and Path(item.name).suffix.lower() in SQLITE_EXTENSIONS:
                candidates.append(item.name)
                kind = "candidate"
            else:
                raise ScopeChanged("scope_type_or_name_drift")
            # All exact names/metadata stay only in volatile operator-side memory,
            # never included in returned result, terminal output or an artifact.
            fingerprint.append((
                item.name, kind, st.st_dev, st.st_ino, st.st_mode,
                st.st_nlink, st.st_size, st.st_mtime_ns,
            ))
    if len(candidates) != EXPECTED_SQLITE_CANDIDATES or unknown_dirs != EXPECTED_UNKNOWN_DIRECTORIES:
        raise ScopeChanged("count_drift")
    buckets = {key: 0 for key in HINT_BUCKETS}
    for basename in candidates:
        buckets[name_hint(basename)] += 1
    return tuple(sorted(fingerprint)), buckets


def identify_name_hints(root: Path, *, synthetic: bool) -> dict:
    if not root.is_absolute() or ".." in root.parts:
        raise ScopeChanged("root_invalid")
    if not synthetic and (os.name != "posix" or root != ROOT):
        raise ScopeChanged("production_root_invalid")
    if synthetic:
        try:
            st = root.lstat()
            if not stat.S_ISDIR(st.st_mode):
                raise ScopeChanged("synthetic_root_not_plain_dir")
        except OSError:
            raise ScopeChanged("synthetic_root_unavailable") from None
        first, buckets = scan_once(root, synthetic=True)
        second, again = scan_once(root, synthetic=True)
    else:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(root, flags)
        except OSError:
            raise ScopeChanged("root_unavailable") from None
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise ScopeChanged("root_not_directory")
            first, buckets = scan_once(fd, synthetic=False)
            second, again = scan_once(fd, synthetic=False)
        finally:
            os.close(fd)
    if first != second or buckets != again:
        raise ScopeChanged("metadata_changed_during_audit")
    return {
        "stage": 50,
        "scope": "SYNTHETIC_ONLY" if synthetic else "AUTHORIZED_G1A_PRIVATE_NAMES_ONLY",
        "result": "NAME_HINTS_ONLY",
        "candidate_extensions_observed": EXPECTED_SQLITE_CANDIDATES,
        "unclassified_root_directories_observed": EXPECTED_UNKNOWN_DIRECTORIES,
        "name_hint_counts": buckets,
        "identity_confirmed_count": 0,
        "identity_confirmation": "NOT_PROVEN_BY_NAME",
        "sqlite_header_or_schema_checked": False,
        "valid_sqlite_count": "UNKNOWN_NOT_INSPECTED",
        "ledger_activation": "UNKNOWN_NOT_INSPECTED",
        "content_or_secret_values_read": False,
        "raw_names_emitted": False,
        "real_files_exported": False,
        "source_writes": False,
        "backup_or_restore": "NOT_AUTHORIZED_NOT_TESTED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--perform-g1a-readonly", action="store_true", required=True)
    parser.add_argument("--synthetic-root", type=Path)
    args = parser.parse_args()
    try:
        synthetic = args.synthetic_root is not None
        if synthetic and os.environ.get("GUITING_STAGE50_SYNTHETIC_ONLY") != "true":
            raise ScopeChanged("synthetic_test_gate_missing")
        result = identify_name_hints(args.synthetic_root if synthetic else ROOT, synthetic=synthetic)
    except (ScopeChanged, OSError, ValueError):
        print('{"stage":50,"result":"STOP_SCOPE_CHANGED_OR_UNAVAILABLE"}')
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
