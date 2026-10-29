"""Stage51 G1B-1: owner-authorized bounded SQLite HEADER-ONLY audit.

Stand-alone stdin-compatible source. Frozen Stage50 G1A root scope/fingerprint
scan is intentionally preserved so the owner can pipe one audited Python file
through existing SSH without installing any files on the server.

PRODUCTION: fixed /var/data on Linux; O_NOATIME is mandatory for directory
and all candidate files (fail closed if unsupported/EPERM). Read <=100 bytes
per candidate with pread. No sqlite3, SQL, WAL/SHM, key/env reads, network,
disk writes, backup or restore. Only aggregate classifications are emitted.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

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
HEADER_LENGTH = 100
HEADER_MAGIC = b"SQLite format 3\x00"


class ScopeChanged(ValueError):
    """Private details must never appear in exceptions exposed to users."""


class ReadNotSafe(ValueError):
    """Stop before any fallback that might alter filesystem metadata."""


def scan_once(root_fd_or_path: int | Path, *, synthetic: bool) -> tuple:
    """Stage50 G1A scan_once shape/guard, no file content opens.

    Intentionally mirrors Stage50 frozen 8bb7355c snapshot logic.
    CI checks snapshot equivalence against the original on fake data.
    """
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
            is_dir = stat.S_ISDIR(st.st_mode)
            is_file = stat.S_ISREG(st.st_mode)
            if is_dir:
                unknown_dirs += 1
                kind = "dir"
            elif is_file and (st.st_nlink == 1 or (synthetic and os.name == "nt")) and Path(item.name).suffix.lower() in SQLITE_EXTENSIONS:
                candidates.append(item.name)
                kind = "candidate"
            else:
                raise ScopeChanged("scope_type_or_name_drift")
            fingerprint.append((
                item.name, kind, st.st_dev, st.st_ino, st.st_mode,
                st.st_nlink, st.st_size, st.st_mtime_ns,
            ))
    if len(candidates) != EXPECTED_SQLITE_CANDIDATES or unknown_dirs != EXPECTED_UNKNOWN_DIRECTORIES:
        raise ScopeChanged("count_drift")
    return tuple(sorted(fingerprint))


def file_fingerprint(name: str, st: os.stat_result) -> tuple:
    return (name, "candidate", st.st_dev, st.st_ino, st.st_mode,
            st.st_nlink, st.st_size, st.st_mtime_ns)


def classify_header(data: bytes) -> str:
    """Compatibility hint only; does not prove a complete valid database."""
    if len(data) < HEADER_LENGTH:
        return "header_insufficient_or_unsupported_count"
    if not data.startswith(HEADER_MAGIC):
        return "header_nonmatching_count"
    page_value = int.from_bytes(data[16:18], "big")
    page_size = 65536 if page_value == 1 else page_value
    supported_page = (512 <= page_size <= 65536 and
                      (page_size & (page_size - 1)) == 0)
    # See SQLite file format: file-format write/read version 1/2;
    # fraction fields 64/32/32; at least 480 usable bytes.
    supported_format = (
        data[18] in (1, 2) and data[19] in (1, 2)
        and data[21:24] == bytes((64, 32, 32))
        and page_size - data[20] >= 480
    )
    if not supported_page or not supported_format:
        return "header_insufficient_or_unsupported_count"
    return "header_compatible_count"


def checked_file_header(root_fd_or_path: int | Path, entry: tuple, *, synthetic: bool) -> bytes:
    """Use an anchored dirfd and O_NOATIME in production, never fallback."""
    name = entry[0]
    if synthetic:
        # Fake fixtures only; Windows POSIX flags intentionally not needed.
        target = root_fd_or_path / name
        try:
            before = target.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise ScopeChanged("candidate_drift")
            # Windows synthetic DirEntry.stat vs Path.lstat may differ for
            # inode/link fields; this relaxation NEVER runs in production.
            if os.name == "nt":
                if before.st_size != entry[6] or before.st_mtime_ns != entry[7]:
                    raise ScopeChanged("candidate_drift")
            elif file_fingerprint(name, before) != entry:
                raise ScopeChanged("candidate_drift")
            with target.open("rb") as handle:
                data = handle.read(HEADER_LENGTH)
            after = target.lstat()
            if os.name == "nt":
                if not stat.S_ISREG(after.st_mode) or after.st_size != entry[6] or after.st_mtime_ns != entry[7]:
                    raise ScopeChanged("candidate_drift")
            elif file_fingerprint(name, after) != entry:
                raise ScopeChanged("candidate_drift")
            return data
        except OSError:
            raise ReadNotSafe("synthetic_read_unavailable") from None

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        before = os.stat(name, dir_fd=root_fd_or_path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ScopeChanged("candidate_changed")
        if file_fingerprint(name, before) != entry:
            raise ScopeChanged("candidate_changed")
        fd = os.open(name, flags, dir_fd=root_fd_or_path)
    except OSError:
        raise ReadNotSafe("candidate_open_not_safe") from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ScopeChanged("candidate_replaced")
        if file_fingerprint(name, opened) != entry or opened.st_atime_ns != before.st_atime_ns:
            raise ScopeChanged("candidate_replaced")
        # One bounded positioned read. No retries, no reads beyond offset 100.
        data = os.pread(fd, HEADER_LENGTH, 0)
        after = os.fstat(fd)
        linked = os.stat(name, dir_fd=root_fd_or_path, follow_symlinks=False)
        if (file_fingerprint(name, after) != entry or
                file_fingerprint(name, linked) != entry or
                after.st_atime_ns != before.st_atime_ns or
                linked.st_atime_ns != before.st_atime_ns):
            raise ScopeChanged("metadata_changed_after_read")
        return data
    except OSError:
        raise ReadNotSafe("candidate_read_not_safe") from None
    finally:
        os.close(fd)


def audit_g1b1(root: Path, *, synthetic: bool) -> dict:
    if not root.is_absolute() or ".." in root.parts:
        raise ScopeChanged("root_invalid")
    if synthetic:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise ScopeChanged("synthetic_root_invalid")
        directory = root
        root_fd = None
    else:
        if root != ROOT or os.name != "posix" or not sys.platform.startswith("linux"):
            raise ScopeChanged("production_root_invalid")
        required = ("O_NOATIME", "O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK", "pread")
        if not all(hasattr(os, name) for name in required):
            raise ReadNotSafe("linux_noatime_or_pread_missing")
        # Must prevent directory atime writes too; never retry without NOATIME.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOATIME
        try:
            root_fd = os.open(root, flags)
        except OSError:
            raise ReadNotSafe("directory_noatime_unavailable") from None
        directory = root_fd

    try:
        root_before = os.fstat(root_fd) if root_fd is not None else None
        if root_before is not None and not stat.S_ISDIR(root_before.st_mode):
            raise ScopeChanged("root_not_directory")
        first = scan_once(directory, synthetic=synthetic)
        fields = {
            "header_compatible_count": 0,
            "header_nonmatching_count": 0,
            "header_insufficient_or_unsupported_count": 0,
        }
        # Names and metadata never leave this process; only 11 count totals.
        files = tuple(entry for entry in first if entry[1] == "candidate")
        for entry in files:
            header = checked_file_header(directory, entry, synthetic=synthetic)
            fields[classify_header(header)] += 1
        second = scan_once(directory, synthetic=synthetic)
        if first != second:
            raise ScopeChanged("metadata_changed_during_audit")
        if root_fd is not None:
            root_after = os.fstat(root_fd)
            if (root_after.st_atime_ns != root_before.st_atime_ns or
                    root_after.st_mtime_ns != root_before.st_mtime_ns or
                    root_after.st_ino != root_before.st_ino or
                    root_after.st_dev != root_before.st_dev):
                raise ScopeChanged("root_metadata_changed")
        if sum(fields.values()) != EXPECTED_SQLITE_CANDIDATES:
            raise ScopeChanged("aggregate_inconsistent")
    finally:
        if root_fd is not None:
            os.close(root_fd)

    return {
        "stage": 51,
        "gate": "G1B1",
        "scope": "SYNTHETIC_ONLY" if synthetic else "AUTHORIZED_G1B1_HEADER_ONLY",
        "result": "HEADER_CLASSIFICATION_ONLY",
        "candidate_count": EXPECTED_SQLITE_CANDIDATES,
        **fields,
        "identity_confirmed_count": 0,
        "identity_confirmation": "NOT_PROVEN_BY_HEADER",
        "valid_sqlite_count": "UNKNOWN_NOT_VERIFIED",
        "schema_checked": False,
        "database_engine_opened": False,
        "raw_names_or_header_bytes_emitted": False,
        "source_writes": False,
        "real_files_exported": False,
        "backup_or_restore": "NOT_AUTHORIZED_NOT_TESTED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--perform-g1b1-header-only", action="store_true", required=True)
    parser.add_argument("--owner-authorized-g1b1", action="store_true", required=True)
    parser.add_argument("--synthetic-root", type=Path)
    args = parser.parse_args()
    try:
        synthetic = args.synthetic_root is not None
        if synthetic and os.environ.get("GUITING_STAGE51_SYNTHETIC_ONLY") != "true":
            raise ScopeChanged("synthetic_gate_missing")
        report = audit_g1b1(args.synthetic_root if synthetic else ROOT, synthetic=synthetic)
    except ReadNotSafe:
        print('{"stage":51,"gate":"G1B1","result":"STOP_READ_NOT_SAFE"}')
        return 2
    except (ScopeChanged, OSError, ValueError):
        print('{"stage":51,"gate":"G1B1","result":"STOP_SCOPE_CHANGED_OR_UNAVAILABLE"}')
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
