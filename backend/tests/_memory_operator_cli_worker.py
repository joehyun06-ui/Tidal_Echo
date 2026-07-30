"""Subprocess-only fault injector for the operator CLI test suite."""

from __future__ import annotations

import os
import sqlite3
from unittest import mock

from backend import channel_store, memory_operator_cli


class _CommitFailureConnection:
    def __init__(self, connection, *, after_commit: bool):
        self._connection = connection
        self._after_commit = after_commit

    @property
    def in_transaction(self):
        return self._connection.in_transaction

    def execute(self, sql, parameters=()):
        normalized = " ".join(str(sql).strip().upper().split())
        if normalized == "COMMIT":
            if self._after_commit:
                self._connection.execute(sql, parameters)
            raise sqlite3.OperationalError("synthetic_commit_failure")
        return self._connection.execute(sql, parameters)

    def close(self):
        self._connection.close()

    def __getattr__(self, name):
        return getattr(self._connection, name)


def main() -> int:
    mode = os.environ.get("MEMORY_OPERATOR_CLI_TEST_INJECTION", "")
    if mode not in {"before_commit", "after_commit"}:
        return memory_operator_cli.main()
    real_connect = channel_store.connect

    def connect(path):
        return _CommitFailureConnection(
            real_connect(path),
            after_commit=mode == "after_commit",
        )

    with mock.patch.object(channel_store, "connect", side_effect=connect):
        return memory_operator_cli.main()


if __name__ == "__main__":
    raise SystemExit(main())
