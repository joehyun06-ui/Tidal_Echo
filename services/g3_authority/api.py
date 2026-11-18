"""Authenticated private, read-only HTTP interface for the test authority."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sqlite3

from backend.g3_projection_contract import ProjectionError
from .store import AuthorityStore, require_token

OPERATIONS = {
    "scan": {"after_cursor"},
    "validate": {"scan"},
    "page": {"scan", "after_cursor", "limit"},
    "event": {"event_id", "revision"},
    "source": {"ref"},
    "revocations": {"after_cursor"},
}


def make_server(store, host, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, value):
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                self.respond(200, {"ready": True})
            else:
                self.respond(404, {"error": "g3_test_route_unavailable"})

        def do_POST(self):
            operation = self.path.removeprefix("/v1/")
            if self.path != f"/v1/{operation}" or operation not in OPERATIONS:
                self.respond(404, {"error": "g3_test_route_unavailable"})
                return
            self.connection.settimeout(5)
            try:
                scheme, _, token = self.headers.get("Authorization", "").partition(" ")
                if scheme != "Bearer":
                    raise ProjectionError("g3_test_access_denied")
                require_token(token, "G3_TEST_READER_TOKEN")
            except ProjectionError:
                self.respond(403, {"error": "g3_test_access_denied"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 1 <= size <= 8192:
                    raise ValueError
                arguments = json.loads(self.rfile.read(size))
                if not isinstance(arguments, dict) or set(arguments) != OPERATIONS[operation]:
                    raise ValueError
                result = store.read(operation, arguments)
            except ProjectionError as error:
                self.respond(403, {"error": error.category})
            except (TypeError, KeyError, ValueError, OverflowError):
                self.respond(400, {"error": "g3_test_input_invalid"})
            except (sqlite3.Error, OSError):
                self.respond(503, {"error": "g3_test_store_unavailable"})
            else:
                self.respond(200, {"result": result})

    return ThreadingHTTPServer((host, port), Handler)


def main():
    if len(os.environ.get("G3_TEST_READER_TOKEN", "")) < 32:
        raise RuntimeError("g3_test_reader_token_required")
    store = AuthorityStore.deployed()
    store.initialize()
    with make_server(store, "0.0.0.0", int(os.environ.get("PORT", "10000"))) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
