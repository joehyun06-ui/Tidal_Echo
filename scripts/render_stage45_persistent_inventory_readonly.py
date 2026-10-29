"""Metadata-only, read-only inventory of the approved Render persistent root.

Never reads file contents, keys, directory entry names in output, or credentials.
Never creates/deletes files, walks through symlinks, invokes a network call or mutates DB.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat


DEFAULT_ROOT = Path("/var/data")
ENTRY_CAP = 10000
DEPTH_CAP = 3

# These are SOURCE-CODE / BLUEPRINT EXPECTED paths, not existence claims.
EXPECTED = (
    ("relay_db", "relay.db", "file"),
    ("relay_wal", "relay.db-wal", "file"),
    ("relay_shm", "relay.db-shm", "file"),
    ("uploads", "uploads", "dir"),
    ("brain_target", "brain_target", "file"),
    ("api_loop_config", "api_loop.config.json", "file"),
    ("codex_home", "codex-home", "dir"),
    ("codex_workspace", "codex-workspace", "dir"),
    ("codex_generation_db", "codex-generation.db", "file"),
)


def classify(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    return "other"


def metadata(path: Path, expected: str) -> dict:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"status": "missing"}
    except OSError:
        return {"status": "inaccessible"}
    actual = classify(info.st_mode)
    if actual != expected:
        return {"status": "type_mismatch", "type": actual}
    if actual == "file":
        return {"status": "present", "bytes": int(info.st_size)}
    return {"status": "present"}


def count_tree(root: Path) -> dict:
    totals = {"files": 0, "directories": 0, "symlinks": 0, "other": 0, "file_bytes": 0}
    stack = [(root, 0)]
    visits = 0
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visits += 1
                    if visits > ENTRY_CAP:
                        return {**totals, "truncated": True, "error": "entry_limit"}
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        totals["other"] += 1
                        continue
                    kind = classify(st.st_mode)
                    if kind == "file":
                        totals["files"] += 1
                        totals["file_bytes"] += int(st.st_size)
                    elif kind == "dir":
                        totals["directories"] += 1
                        if depth < DEPTH_CAP:
                            stack.append((Path(entry.path), depth + 1))
                        else:
                            return {**totals, "truncated": True, "error": "depth_limit"}
                    elif kind == "symlink":
                        totals["symlinks"] += 1
                    else:
                        totals["other"] += 1
        except OSError:
            return {**totals, "truncated": True, "error": "inaccessible"}
    return {**totals, "truncated": False}


def inventory(root: Path) -> dict:
    # No root.resolve() (it would follow links). Source root itself must not link.
    if not root.is_absolute() or metadata(root, "dir")["status"] != "present":
        raise ValueError("inventory_root_unavailable")
    items = {}
    for label, relative, kind in EXPECTED:
        path = root / relative
        items[label] = metadata(path, kind)
        if kind == "dir" and items[label]["status"] == "present":
            items[label]["inventory"] = count_tree(path)
    # Include only aggregate metadata for unknown root entries, never their names.
    unknown = {"count": 0, "files": 0, "directories": 0, "symlinks": 0, "other": 0}
    expected_names = {relative for _, relative, _ in EXPECTED}
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name in expected_names:
                    continue
                unknown["count"] += 1
                if unknown["count"] > ENTRY_CAP:
                    raise ValueError("inventory_entry_limit")
                try:
                    kind = classify(entry.stat(follow_symlinks=False).st_mode)
                except OSError:
                    kind = "other"
                unknown[{"file": "files", "dir": "directories", "symlink": "symlinks", "other": "other"}[kind]] += 1
    except OSError as exc:
        raise ValueError("inventory_root_unreadable") from None
    return {
        "stage": 45, "result": "METADATA_ONLY", "source": "operator_read_only_inventory",
        "root": "/var/data" if root == DEFAULT_ROOT else "SYNTHETIC",
        "items": items, "unclassified_root_entries": unknown,
        "file_contents_read": False, "secret_values_read": False,
        "source_mutations": False, "snapshot_restored": False,
        "actual_restore_proven": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--perform-readonly", action="store_true", required=True)
    parser.add_argument("--synthetic-root", type=Path)
    args = parser.parse_args()
    try:
        if args.synthetic_root is None:
            root = DEFAULT_ROOT
        else:
            if os.environ.get("GUITING_STAGE45_SYNTHETIC_ONLY") != "true":
                raise ValueError("synthetic_root_requires_test_gate")
            root = args.synthetic_root
        print(json.dumps(inventory(root), sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError):
        print('{"stage":45,"result":"FAIL","reason":"INVENTORY_UNAVAILABLE"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
