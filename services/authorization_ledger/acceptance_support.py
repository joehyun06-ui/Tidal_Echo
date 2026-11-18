"""Disposable localhost PostgreSQL and fault injection for the acceptance suite."""

import fcntl
import json
import os
import select
import shlex
import socket
import struct
import subprocess
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from secrets import token_urlsafe
from tempfile import NamedTemporaryFile

import psycopg
from psycopg import sql

from .contracts import LedgerError

class SandboxPostgres:
    def __init__(self, bin_dir, root):
        self.bin_dir = Path(bin_dir).resolve()
        self.root = root
        self.data = root / "pg-data"
        self.password = token_urlsafe(32)
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]

    def _run(self, binary, *args):
        proc = subprocess.run([str(self.bin_dir / binary), *args], cwd=self.root,
                              check=False, capture_output=True, text=True, timeout=45)
        if proc.returncode:
            log_path = self.root / "pg.log"
            log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            raise RuntimeError(f"{binary} failed: {proc.stdout}\n{proc.stderr}\n{log}")

    def start(self):
        with NamedTemporaryFile(mode="w", dir=self.root, encoding="utf-8") as secret:
            secret.write(self.password + "\n")
            secret.flush()
            self._run("initdb", "-D", str(self.data), "-U", "ledger_test", "-A", "scram-sha-256",
                      f"--pwfile={secret.name}", "--locale=C", "--encoding=UTF8")
        opts = shlex.join(["-p", str(self.port), "-c", "listen_addresses=127.0.0.1",
                           "-c", "unix_socket_directories="])
        self._run("pg_ctl", "-D", str(self.data), "-l", str(self.root / "pg.log"),
                  "-w", "-t", "20", "-o", opts, "start")

    def stop(self):
        if (self.data / "postmaster.pid").exists():
            self._run("pg_ctl", "-D", str(self.data), "-w", "-t", "20", "-m", "fast", "stop")

    def connect(self, database, *, application="ledger-acceptance", proxy_port=None):
        return psycopg.connect(
            dbname=database, user="ledger_test", host="127.0.0.1", password=self.password,
            port=proxy_port or self.port, sslmode="disable", gssencmode="disable", connect_timeout=3,
            application_name=application, autocommit=True,
        )

    def create_database(self, name, *, template="template0"):
        if not name.startswith("gm02as_test_"):
            raise ValueError("isolated database name required")
        with self.connect("postgres") as conn:
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(template),
            ))


class RecoveryController:
    """Cross-process file fence outside the disposable PostgreSQL data/restore directory."""

    def __init__(self, root):
        self.root = root
        self.state = root / "current.json"
        self.lock = root / "control.lock"

    def initialize(self):
        self.root.mkdir(mode=0o700)
        self.lock.touch(mode=0o600, exist_ok=False)
        self._store({"incarnation": 1, "frozen": False})

    def _store(self, value):
        with NamedTemporaryFile(mode="w", dir=self.root, delete=False, encoding="utf-8") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, self.state)
        directory = os.open(self.root, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @contextmanager
    def _locked(self, exclusive=False):
        try:
            with self.lock.open("rb") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    state = json.loads(self.state.read_text(encoding="utf-8"))
                    if (type(state["incarnation"]) is not int or state["incarnation"] <= 0
                            or type(state["frozen"]) is not bool):
                        raise ValueError("invalid recovery state")
                except (OSError, KeyError, ValueError) as exc:
                    raise LedgerError("recovery_unavailable") from exc
                yield state
        except FileNotFoundError as exc:
            raise LedgerError("recovery_unavailable") from exc

    @contextmanager
    def lease(self, *, frozen):
        with self._locked() as state:
            if state["frozen"] is not frozen:
                raise LedgerError("recovery_frozen" if state["frozen"] else "recovery_not_frozen")
            yield state["incarnation"]

    def freeze(self):
        with self._locked(exclusive=True) as state:
            if not state["frozen"]:
                self._store({"incarnation": state["incarnation"] + 1, "frozen": True})

    def thaw(self, incarnation):
        with self._locked(exclusive=True) as state:
            if not state["frozen"] or state["incarnation"] != incarnation:
                raise LedgerError("recovery_state_changed")
            self._store({"incarnation": incarnation, "frozen": False})


def _receive(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def _message(sock):
    header = _receive(sock, 5)
    return header, _receive(sock, struct.unpack("!I", header[1:])[0] - 4)


@contextmanager
def commit_cut(postgres, *, after_commit):
    """Cut one real PostgreSQL connection immediately before or after COMMIT."""
    outcomes = []
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(8)

        def relay():
            try:
                client, _ = listener.accept()
                with client, closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as server:
                    server.connect(("127.0.0.1", postgres.port))
                    client.settimeout(8)
                    server.settimeout(8)
                    header = _receive(client, 4)
                    server.sendall(header + _receive(client, struct.unpack("!I", header)[0] - 4))
                    while True:
                        readable, _, _ = select.select([client, server], [], [], 8)
                        if not readable:
                            raise RuntimeError("no PostgreSQL protocol traffic")
                        for origin in readable:
                            header, data = _message(origin)
                            if origin is client and header[:1] == b"Q" and data.rstrip(b"\0").strip().upper() == b"COMMIT":
                                if after_commit:
                                    server.sendall(header + data)
                                    response, body = _message(server)
                                    if response[:1] != b"C" or body != b"COMMIT\0":
                                        raise RuntimeError(f"expected PostgreSQL COMMIT acknowledgement, got {response[:1]}")
                                    response, _ = _message(server)
                                    if response[:1] != b"Z":
                                        raise RuntimeError("expected PostgreSQL ready after COMMIT acknowledgement")
                                outcomes.append("after_commit" if after_commit else "before_commit")
                                return
                            peer = client if origin is server else server
                            peer.sendall(header + data)
            except Exception as exc:
                outcomes.append(exc)

        worker = threading.Thread(target=relay, daemon=True)
        worker.start()
        try:
            yield listener.getsockname()[1]
        finally:
            worker.join(10)
            desired = ["after_commit" if after_commit else "before_commit"]
            if outcomes != desired or worker.is_alive():
                raise AssertionError(f"wire fault was not reached: {outcomes!r}")
