"""Run the test-admin and read-only GM-02AS phases on the dedicated service disk."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from .store import AuthorityStore


def _save(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _read(path):
    return json.loads(path.read_text())


def _client(phase, url, canonical, event_id):
    completed = subprocess.run(
        [sys.executable, "-m", "backend.tests.test_g3_authority_integration", phase,
         url, str(canonical), event_id],
        env={"G3_TEST_READER_TOKEN": os.environ["G3_TEST_READER_TOKEN"]},
        timeout=120, check=True, stdout=subprocess.PIPE, text=True,
    )
    return json.loads(completed.stdout)


def _ready(url):
    http = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with http.open(url + "/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.2)
    raise RuntimeError("g3_test_authority_startup_timeout")


def run(store, url, boot_id, instance_id):
    _ready(url)
    token = os.environ["G3_TEST_ADMIN_TOKEN"]
    if token == os.environ["G3_TEST_READER_TOKEN"]:
        raise RuntimeError("g3_test_credentials_must_be_distinct")
    root = store.path.parent / "gm-02as-acceptance"
    root.mkdir(mode=0o700, exist_ok=True)
    prepared, revoked = root / "prepared.json", root / "revoked.json"
    canonical = root / "canonical.sqlite3"
    identity = root / "event.json"
    if not identity.exists():
        _save(identity, {"event_id": "test-" + uuid4().hex})
    event = _read(identity)["event_id"]
    if not prepared.exists():
        store.seed_test_event(event, token=token)
        for suffix in ("", "-shm", "-wal"):
            canonical.with_name(canonical.name + suffix).unlink(missing_ok=True)
        canonical.with_suffix(".json").unlink(missing_ok=True)
        _save(prepared, _client("prepare", url, canonical, event))

    original = _read(prepared)
    if not revoked.exists():
        store.revoke_test_event(event, token=token)
        result = _client("verify", url, canonical, event)
        evidence = {"phase": "before_restart", "boot_id": boot_id, "instance_id": instance_id,
                    "prepared": original, "revoked": result}
        _save(revoked, evidence)
    else:
        previous = _read(revoked)
        result = _client("verify", url, canonical, event)
        if result != previous["revoked"] or boot_id == previous["boot_id"]:
            raise RuntimeError("g3_test_restart_verification_failed")
        evidence = {"phase": "after_restart", "boot_id": boot_id, "instance_id": instance_id,
                    "prior_boot_id": previous["boot_id"], "prior_instance_id": previous["instance_id"],
                    "revoked": result, "packet_sha256": original["packet_sha256"]}
        _save(root / "restarted.json", evidence)
    print("GM02AS_G3_ACCEPTANCE " + json.dumps(evidence, sort_keys=True), flush=True)


if __name__ == "__main__":
    run(AuthorityStore.deployed(), *sys.argv[1:])
