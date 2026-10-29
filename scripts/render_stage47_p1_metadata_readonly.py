"""Stage47: P1 Render persistent metadata inventory. NO CONTENT OR SECRETS.

Live mode is POSIX-only and uses directory FDs with O_NOFOLLOW. No file
contents, environment values, filenames or digests are returned. No writes.
Synthetic root mode is explicit and may also run on Windows for offline CI.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
import sys
import time

ROOT = Path("/var/data")
ENTRY_CAP = 20000
DEPTH_CAP = 12
TIME_CAP_SECONDS = 8.0
# EXACT same root names as Stage45, to keep anonymous-count semantics.
STAGE45_NAMES = frozenset((
    "relay.db", "relay.db-wal", "relay.db-shm", "uploads",
    "brain_target", "api_loop.config.json", "codex-home",
    "codex-workspace", "codex-generation.db",
))
P1_FILES = ("codex-generation.db", "codex-generation.db-wal",
            "codex-generation.db-shm", "codex-generation.db-journal")
SECRET_EXTS = frozenset((".key", ".pem", ".env", ".token", ".secret",
                        ".p12", ".pfx", ".kdbx"))
DB_EXTS = frozenset((".db", ".sqlite", ".sqlite3"))


def kind(mode: int) -> str:
    if stat.S_ISLNK(mode): return "symlink"
    if stat.S_ISREG(mode): return "file"
    if stat.S_ISDIR(mode): return "dir"
    return "other"


def bucket(name: str, k: str) -> str:
    if k == "dir": return "directories"
    if k == "symlink": return "symlinks"
    if k != "file": return "other"
    suffix = Path(name).suffix.lower()
    if suffix in SECRET_EXTS: return "secret_extension_candidates"
    if name.endswith(("-wal", "-shm", "-journal")): return "sqlite_sidecar_candidates"
    if suffix in DB_EXTS: return "sqlite_candidates"
    if suffix in {".json", ".jsonl", ".yaml", ".yml"}: return "structured_config_candidates"
    return "other_files"


def zero_counts() -> dict:
    return dict(files=0, directories=0, symlinks=0, other=0, file_bytes=0,
                truncated=False, stop_reason="none")


@dataclass
class Budget:
    entries: int = 0
    deadline: float = field(default_factory=lambda: time.monotonic() + TIME_CAP_SECONDS)
    stopped: str = "none"

    def tick(self) -> bool:
        if self.entries >= ENTRY_CAP:
            self.stopped = "entry_limit"
            return False
        if time.monotonic() > self.deadline:
            self.stopped = "time_limit"
            return False
        self.entries += 1
        return True


def add(counts: dict, info: os.stat_result) -> None:
    k = kind(info.st_mode)
    if k == "file":
        counts["files"] += 1
        counts["file_bytes"] += int(info.st_size)
    elif k == "dir": counts["directories"] += 1
    elif k == "symlink": counts["symlinks"] += 1
    else: counts["other"] += 1


def walk_posix(directory_fd: int, root_device: int, budget: Budget,
               *, depth: int = 0, totals: dict | None = None) -> dict:
    totals = zero_counts() if totals is None else totals
    if budget.stopped != "none": return totals
    try:
        with os.scandir(directory_fd) as iterator:
            for ent in iterator:
                if not budget.tick(): break
                try:
                    info = os.stat(ent.name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError:
                    totals["other"] += 1
                    continue
                add(totals, info)
                if kind(info.st_mode) != "dir": continue
                if depth >= DEPTH_CAP:
                    budget.stopped = "depth_limit"
                    break
                if info.st_dev != root_device:
                    budget.stopped = "other_mount"
                    break
                try:
                    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    flags |= getattr(os, "O_CLOEXEC", 0)
                    fd = os.open(ent.name, flags, dir_fd=directory_fd)
                except OSError:
                    budget.stopped = "unavailable_child"
                    break
                try:
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        budget.stopped = "changed_during_scan"
                        break
                    walk_posix(fd, root_device, budget, depth=depth+1, totals=totals)
                    if budget.stopped != "none": break
                finally:
                    os.close(fd)
    except OSError:
        budget.stopped = "unreadable"
    return totals


def walk_synthetic_windows(root: Path, budget: Budget, *, depth: int = 0,
                           totals: dict | None = None) -> dict:
    """Windows CI only; not used for live POSIX audit."""
    totals = zero_counts() if totals is None else totals
    if budget.stopped != "none": return totals
    try:
        with os.scandir(root) as iterator:
            for ent in iterator:
                if not budget.tick(): break
                try: info = ent.stat(follow_symlinks=False)
                except OSError:
                    totals["other"] += 1
                    continue
                add(totals, info)
                if kind(info.st_mode) != "dir": continue
                if depth >= DEPTH_CAP:
                    budget.stopped = "depth_limit"
                    break
                walk_synthetic_windows(Path(ent.path), budget, depth=depth+1,
                                       totals=totals)
                if budget.stopped != "none": break
    except OSError:
        budget.stopped = "unreadable"
    return totals


def describe(root: Path, *, synthetic: bool) -> dict:
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("root_invalid")
    if not synthetic and (os.name != "posix" or root != ROOT):
        raise ValueError("production_root_must_be_fixed")
    if os.name == "posix":
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        base = os.open(root, flags)
        try:
            root_stat = os.fstat(base)
            if not stat.S_ISDIR(root_stat.st_mode):
                raise ValueError("root_not_directory")
            def fixed(name):
                try: info = os.stat(name, dir_fd=base, follow_symlinks=False)
                except FileNotFoundError: return {"status": "missing"}
                except OSError: return {"status": "inaccessible"}
                return {"status": "present" if kind(info.st_mode) == "file" else "wrong_type",
                        "type": kind(info.st_mode),
                        **({"bytes": int(info.st_size)} if kind(info.st_mode) == "file" else {})}
            file_summary = {name: fixed(name) for name in P1_FILES}
            try:
                info = os.stat("codex-home", dir_fd=base, follow_symlinks=False)
                home_state = "present" if kind(info.st_mode) == "dir" else "wrong_type"
            except FileNotFoundError:
                home_state = "missing"
            except OSError:
                home_state = "inaccessible"
            home_counts = None
            if home_state == "present":
                budget = Budget()
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                flags |= getattr(os, "O_CLOEXEC", 0)
                try:
                    fd = os.open("codex-home", flags, dir_fd=base)
                    try:
                        home_counts = walk_posix(fd, root_stat.st_dev, budget)
                    finally: os.close(fd)
                except OSError:
                    budget.stopped = "unavailable_child"
                    home_counts = zero_counts()
                home_counts["truncated"] = budget.stopped != "none"
                home_counts["stop_reason"] = budget.stopped
            unknown = anonymous_root(base, synthetic=False)
        finally:
            os.close(base)
    else:
        if not synthetic or not stat.S_ISDIR(root.lstat().st_mode):
            raise ValueError("synthetic_root_invalid")
        def fixed(name):
            try: info = (root / name).lstat()
            except FileNotFoundError: return {"status": "missing"}
            except OSError: return {"status": "inaccessible"}
            return {"status": "present" if kind(info.st_mode) == "file" else "wrong_type",
                    "type": kind(info.st_mode),
                    **({"bytes": int(info.st_size)} if kind(info.st_mode) == "file" else {})}
        file_summary = {name: fixed(name) for name in P1_FILES}
        try:
            info = (root / "codex-home").lstat()
            home_state = "present" if kind(info.st_mode) == "dir" else "wrong_type"
        except FileNotFoundError: home_state = "missing"
        except OSError: home_state = "inaccessible"
        home_counts = None
        if home_state == "present":
            budget = Budget()
            home_counts = walk_synthetic_windows(root / "codex-home", budget)
            home_counts["truncated"] = budget.stopped != "none"
            home_counts["stop_reason"] = budget.stopped
        unknown = anonymous_root(root, synthetic=True)

    return {"stage": 47, "scope": "SYNTHETIC_ONLY" if synthetic else "LIVE_READONLY_METADATA",
            "result": "PASS_METADATA" if not unknown["truncated"] else "PARTIAL_METADATA",
            "codex_generation_files": file_summary,
            "codex_home_status": home_state, "codex_home": home_counts,
            "anonymous_unclassified_root": unknown,
            "contents_read": False, "environment_values_read": False,
            "file_names_emitted": False, "source_writes": False,
            "backup_created": False, "restoration_tested": False,
            "usage_ledger_status": "UNKNOWN_CONFIG_DEPENDENT"}


def anonymous_root(fd_or_path, *, synthetic: bool) -> dict:
    counters = {k: 0 for k in ("directories", "symlinks", "other", "secret_extension_candidates",
                               "sqlite_sidecar_candidates", "sqlite_candidates",
                               "structured_config_candidates", "other_files")}
    count = 0
    truncated = False
    try:
        with os.scandir(fd_or_path) as iterator:
            for ent in iterator:
                if ent.name in STAGE45_NAMES: continue
                count += 1
                if count > ENTRY_CAP:
                    truncated = True
                    break
                try:
                    info = ent.stat(follow_symlinks=False)
                except OSError:
                    counters["other"] += 1
                    continue
                counters[bucket(ent.name, kind(info.st_mode))] += 1
    except OSError:
        truncated = True
    return {"total": count if not truncated else min(count, ENTRY_CAP),
            "buckets": counters, "truncated": truncated}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--perform-readonly", action="store_true", required=True)
    parser.add_argument("--synthetic-root", type=Path)
    args = parser.parse_args()
    try:
        synthetic = args.synthetic_root is not None
        if synthetic and os.environ.get("GUITING_STAGE47_SYNTHETIC_ONLY") != "true":
            raise ValueError("synthetic_gate_required")
        result = describe(args.synthetic_root if synthetic else ROOT, synthetic=synthetic)
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 0 if result["result"] == "PASS_METADATA" else 3
    except (OSError, ValueError):
        print('{"stage":47,"result":"FAIL_METADATA","reason":"SAFE_INVENTORY_UNAVAILABLE"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
