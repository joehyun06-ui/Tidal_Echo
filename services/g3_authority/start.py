"""Entrypoint restricted to the authorized private test service."""

import os
import subprocess
import sys
from uuid import uuid4

from .api import make_server
from .store import AuthorityStore


def main():
    store = AuthorityStore.deployed()
    if len(os.environ.get("G3_TEST_READER_TOKEN", "")) < 32:
        raise RuntimeError("g3_test_reader_token_required")
    os.umask(0o077)
    store.initialize()
    port = int(os.environ.get("PORT", "10000"))
    with make_server(store, "0.0.0.0", port) as server:
        if os.environ.get("G3_TEST_ACCEPTANCE") == "1":
            boot = uuid4().hex
            instance = os.environ.get("RENDER_INSTANCE_ID", "unavailable")
            url = f"http://127.0.0.1:{port}"
            subprocess.Popen([sys.executable, "-m", "services.g3_authority.acceptance", url, boot, instance])
        os.environ.pop("G3_TEST_ADMIN_TOKEN", None)
        server.serve_forever()


if __name__ == "__main__":
    main()
