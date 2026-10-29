"""Stage43 operator-only isolated application boot on an existing private backup.
Never execute in CI with real data. No raw DB content is displayed or uploaded.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import closing, redirect_stdout, redirect_stderr
import hashlib
import io
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from unittest import mock

EXPECTED_HEAD = "58ecb255e5cb32e37ee0d949aaa7d8bff896d971"
NAME = "relay-backup.sqlite3"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def forbid_network(*args, **kwargs):
    raise AssertionError("OUTBOUND_NETWORK_BLOCKED")


def verify_db(path: Path, channel_store) -> None:
    with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        assert [tuple(r) for r in conn.execute("PRAGMA integrity_check")] == [("ok",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchone() is None
        channel_store.validate_core_schema_v1_v6(conn, require_relay_tables=True)


def guard_run(repo_root: Path, backup: Path, expected_hash: str | None = None) -> dict[str, str]:
    if sys.version_info < (3, 11):
        raise ValueError("PYTHON_VERSION_INVALID")
    if not (repo_root / "backend" / "channel_store.py").is_file() or repo_root.is_symlink():
        raise ValueError("REPO_INVALID")
    if backup.is_symlink() or not backup.is_file() or backup.name != NAME:
        raise ValueError("BACKUP_INVALID")
    private_root = Path(os.environ["LOCALAPPDATA"]).resolve(strict=True)
    if backup.parent.resolve(strict=True) != (private_root / "GuitingPrivateRecovery").resolve(strict=True):
        raise ValueError("BACKUP_NOT_PRIVATE")
    git_head = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, check=True, timeout=15).stdout.strip()
    if git_head != EXPECTED_HEAD:
        raise ValueError("SOURCE_COMMIT_MISMATCH")
    dirty = subprocess.run(["git", "-C", str(repo_root), "status", "--porcelain", "--",
                            "backend/app.py", "backend/channel_store.py", "backend/tests/_support.py"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, check=True, timeout=15).stdout.strip()
    if dirty:
        raise ValueError("SOURCE_MODIFIED")
    digest = sha256(backup)
    if expected_hash and digest.lower() != expected_hash.lower():
        raise ValueError("BACKUP_HASH_MISMATCH")

    # Application init_db() may update the temporary copy, never the source.
    with tempfile.TemporaryDirectory(prefix="guiting-stage43-", dir=private_root) as t:
        temp = Path(t)
        copy = temp / "test-relay.sqlite3"
        shutil.copyfile(backup, copy)
        if sha256(copy) != digest:
            raise AssertionError("COPY_HASH_MISMATCH")
        old_env, old_cwd = dict(os.environ), Path.cwd()
        old_bytecode = sys.dont_write_bytecode
        allowed = ("SystemRoot", "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT",
                   "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
                   "HOME", "HOMEDRIVE", "HOMEPATH", "COMSPEC")
        minimal_env = {key: old_env[key] for key in allowed if key in old_env}
        try:
            os.chdir(temp)
            sys.dont_write_bytecode = True
            os.environ.clear()
            os.environ.update(minimal_env)
            sys.path.insert(0, str(repo_root))
            # Block every Python-level external connect and subprocess while
            # importing the real backend, entering its ASGI lifespan and probing.
            with mock.patch("socket.socket.connect", side_effect=forbid_network), \
                 mock.patch("socket.socket.connect_ex", side_effect=forbid_network), \
                 mock.patch("socket.create_connection", side_effect=forbid_network), \
                 mock.patch("socket.getaddrinfo", side_effect=forbid_network), \
                 mock.patch("subprocess.Popen", side_effect=forbid_network), \
                 mock.patch("os.system", side_effect=forbid_network), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                from backend.tests._support import load_app
                from backend import channel_store
                app = load_app(str(temp), telegram=False, brain="loop", kelivo=False,
                               operit_share=False, memory=False, memory_writes=False,
                               memory_entry=False, memory_context=False)
                if app.TELEGRAM.enabled or app.DEPLOYMENT.memory.enabled or app.DEPLOYMENT.kelivo.enabled:
                    raise AssertionError("UNSAFE_FEATURE")
                if app.CORE_STARTUP_ERROR or not app._database_ready():
                    raise AssertionError("DB_STARTUP_FAILED")
                verify_db(copy, channel_store)

                async def probe():
                    import httpx
                    async with app.app.router.lifespan_context(app.app):
                        if getattr(app.app.state, "telegram_worker_task", None) is not None:
                            raise AssertionError("WORKER_ENABLED")
                        transport = httpx.ASGITransport(app=app.app)
                        async with httpx.AsyncClient(transport=transport, base_url="http://offline.invalid") as c:
                            response = await c.get("/healthz")
                            if response.status_code != 200 or response.json().get("ok") is not True:
                                raise AssertionError("HEALTH_FAILED")
                        if not app._database_ready():
                            raise AssertionError("DB_NOT_READY")
                asyncio.run(probe())
                verify_db(copy, channel_store)
        finally:
            os.chdir(old_cwd)
            sys.dont_write_bytecode = old_bytecode
            os.environ.clear()
            os.environ.update(old_env)
            if str(repo_root) in sys.path:
                sys.path.remove(str(repo_root))
    if sha256(backup) != digest:
        raise AssertionError("ORIGINAL_BACKUP_CHANGED")
    return {"result": "PASS", "core_startup": "PASS", "local_asgi_healthz": "PASS",
            "schema": "PASS", "source_sha256_unchanged": "true",
            "network_disabled_in_python": "true", "telegram_disabled": "true",
            "production_full_restore": "NOT_TESTED"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--expected-sha256", default=None)
    args = parser.parse_args()
    try:
        path = Path(os.environ["LOCALAPPDATA"]) / "GuitingPrivateRecovery" / NAME
        result = guard_run(args.repo_root.resolve(strict=True), path, args.expected_sha256)
    except Exception as exc:
        print("GUITING_STAGE43_RESULT=FAIL")
        if isinstance(exc, AssertionError):
            category = "ISOLATED_ASSERTION"
        elif isinstance(exc, sqlite3.Error):
            category = "SQLITE_INTEGRITY_OR_SCHEMA"
        elif isinstance(exc, (OSError, ValueError, subprocess.SubprocessError)):
            category = "LOCAL_SETUP_OR_SOURCE"
        else:
            category = "ISOLATED_APP_STARTUP"
        print("GUITING_STAGE43_REASON=" + category)
        return 1
    for key, value in result.items():
        print("GUITING_STAGE43_" + key.upper() + "=" + value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
