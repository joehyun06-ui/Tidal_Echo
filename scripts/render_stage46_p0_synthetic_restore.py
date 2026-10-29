"""Stage46 P0 synthetic-only attachment and API Loop config restore drill.

Never use against real Render storage, chats or credentials. No names, keys,
message metadata or file hashes are printed by this tool.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
from urllib.parse import urlsplit

from backend import deployment_config

MAX_FILES = 128
MAX_BYTES = 16 * 1024 * 1024
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z", re.ASCII)


class DrillError(ValueError):
    """Synthetic-only failure category."""


def plain_file(path: Path) -> int:
    try:
        st = path.lstat()
    except OSError:
        raise DrillError("source_unavailable") from None
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise DrillError("not_regular_file")
    return int(st.st_size)


def plain_dir(path: Path) -> None:
    try:
        st = path.lstat()
    except OSError:
        raise DrillError("directory_unavailable") from None
    if not stat.S_ISDIR(st.st_mode):
        raise DrillError("directory_not_plain")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def attachment_refs(conn: sqlite3.Connection) -> set[str]:
    # Synthetic fixture has only id/meta. Never parse real message tables.
    names: set[str] = set()
    for (raw,) in conn.execute("SELECT meta FROM messages"):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            raise DrillError("fixture_meta_invalid") from None
        if not isinstance(data, dict) or not isinstance(data.get("attachments"), list):
            raise DrillError("fixture_meta_invalid")
        for attachment in data["attachments"]:
            if not isinstance(attachment, dict) or not isinstance(attachment.get("url"), str):
                raise DrillError("fixture_reference_invalid")
            url = urlsplit(attachment["url"])
            if (url.scheme or url.netloc or url.query or url.fragment
                    or not url.path.startswith("/uploads/")):
                raise DrillError("fixture_reference_invalid")
            filename = url.path[len("/uploads/"):]
            if not SAFE_NAME.fullmatch(filename) or filename in {".", ".."}:
                raise DrillError("fixture_reference_invalid")
            names.add(filename)
    return names


def synthetic_restore(root: Path) -> dict:
    if not root.is_absolute() or root == Path("/var/data"):
        raise DrillError("fixture_root_invalid")
    plain_dir(root)
    uploads = root / "uploads"
    plain_dir(uploads)
    config = root / "api_loop.config.json"
    db = root / "synthetic-messages.sqlite3"
    if not 0 < plain_file(config) <= 1024 * 1024:
        raise DrillError("fixture_config_invalid")
    if not 0 < plain_file(db) <= MAX_BYTES:
        raise DrillError("fixture_db_invalid")
    original_hashes = {config: digest(config), db: digest(db)}
    inventory = {}
    total = 0
    with os.scandir(uploads) as entries:
        for item in entries:
            if not SAFE_NAME.fullmatch(item.name) or item.name in {".", ".."}:
                raise DrillError("fixture_name_invalid")
            path = Path(item.path)
            total += plain_file(path)
            if len(inventory) >= MAX_FILES or total > MAX_BYTES:
                raise DrillError("fixture_size_limit")
            inventory[item.name] = digest(path)
            original_hashes[path] = inventory[item.name]
    if not inventory:
        raise DrillError("fixture_uploads_empty")
    with closing(sqlite3.connect(db.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as conn:
        refs = attachment_refs(conn)
    if not refs or not refs.issubset(inventory.keys()):
        raise DrillError("fixture_reference_missing")

    with tempfile.TemporaryDirectory(prefix="guiting-stage46-synthetic-") as temporary:
        dest = Path(temporary)
        (dest / "uploads").mkdir()
        copy_config = dest / config.name
        copy_db = dest / db.name
        shutil.copyfile(config, copy_config)
        shutil.copyfile(db, copy_db)
        for filename in inventory:
            shutil.copyfile(uploads / filename, dest / "uploads" / filename)

        # Verify actual bytes in a disposable restore copy; never print content.
        for source, original_hash in original_hashes.items():
            relative = source.relative_to(root)
            if digest(dest / relative) != original_hash:
                raise DrillError("fixture_copy_mismatch")

        # Use exactly the app's config validator; it can contain main_chain.key.
        deployment_config.validate_loop_config_file(copy_config, render_mvp=True)
        with closing(sqlite3.connect(copy_db.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as conn:
            restored_refs = attachment_refs(conn)
        if restored_refs != refs:
            raise DrillError("fixture_reference_changed")
        for name in restored_refs:
            if not (dest / "uploads" / name).is_file():
                raise DrillError("fixture_reference_missing_after_restore")

    for source, original_hash in original_hashes.items():
        if digest(source) != original_hash:
            raise DrillError("fixture_source_modified")
    return {
        "stage": 46, "scope": "SYNTHETIC_ONLY", "result": "PASS",
        "files_verified": len(inventory),
        "attachment_references": "PASS", "api_loop_schema": "PASS",
        "original_sources_unchanged": True, "real_render_recovery": "NOT_TESTED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--perform-synthetic-restore", action="store_true", required=True)
    parser.add_argument("--synthetic-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if os.environ.get("GUITING_STAGE46_SYNTHETIC_ONLY") != "true":
            raise DrillError("fixture_gate_required")
        result = synthetic_restore(args.synthetic_root)
    except (ValueError, OSError, sqlite3.Error):
        print('{"stage":46,"scope":"SYNTHETIC_ONLY","result":"FAIL"}')
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
