"""HTTPS staging boundary. All policy inputs come from durable server state."""

from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
import ssl
import sys
from urllib.parse import parse_qs, urlsplit

from backend.g3_projection_contract import G3SourceRef, ProjectionError
from backend.session_lifecycle import LifecycleError
from .authority import Authority, Denied
from .oidc import InvalidLogin, OidcLogin
from .session import CanonicalSession

ROUTE = re.compile(r"/v1/(case_[a-z_]+)/(event|source|prepare|packet|activate|audit)\Z")
KEYS = {
    "event": {"version_id", "event_id"}, "source": {"version_id", "ref"},
    "prepare": {"version_id"}, "packet": {"version_id"},
    "activate": {"version_id", "packet_sha256", "request_id"}, "audit": set(),
}


def make_handler(root, authority, oidc, gateway):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _cookie(self, key):
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            return cookies[key].value if key in cookies else None

        def _respond(self, status, data=None, **headers):
            content = json.dumps(data).encode() if data is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(content)))
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            self.connection.settimeout(10)
            url = urlsplit(self.path)
            try:
                if self.path == "/healthz":
                    return self._respond(200, {"ready": True})
                if self.path == "/login":
                    destination, interaction = oidc.start()
                    return self._respond(302, Location=destination,
                                         Set_Cookie=f"formal_flow={interaction}; Path=/login; Secure; HttpOnly; SameSite=Lax")
                if url.path == "/login/callback" and len(url.query) < 4096:
                    args = parse_qs(url.query, max_num_fields=4)
                    if set(args) not in ({"code", "state"}, {"code", "state", "iss"}) or any(len(v) != 1 for v in args.values()):
                        raise InvalidLogin
                    if "iss" in args and args["iss"][0] != oidc.issuer:
                        raise InvalidLogin
                    verified = oidc.complete(code=args["code"][0], state=args["state"][0],
                                             interaction=self._cookie("formal_flow"))
                    session = authority.login(verified)
                    return self._respond(200, {"authenticated": True},
                                         Set_Cookie=f"formal_login={session}; Path=/; Secure; HttpOnly; SameSite=Lax")
                self._respond(404, {"error": "formal_route_unavailable"})
            except (InvalidLogin, Denied, ValueError):
                self._respond(401, {"error": "formal_login_required"})

        def do_POST(self):
            self.connection.settimeout(10)
            match = ROUTE.fullmatch(self.path)
            if match is None:
                return self._respond(404, {"error": "formal_route_unavailable"})
            cid, operation = match.groups()
            try:
                cookie = self._cookie("formal_login")
                with authority.fence():
                    with authority.connect() as conn:
                        identity = authority.identity(conn, cookie)
                        if self.headers.get("Origin") != gateway or self.headers.get("Content-Type") != "application/json":
                            raise Denied()
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 2 <= size <= 4096:
                            raise Denied(400, "formal_request_invalid")
                        args = json.loads(self.rfile.read(size))
                        if type(args) is not dict or set(args) != KEYS[operation]:
                            raise Denied(400, "formal_request_invalid")
                        path = root / (cid + ".sqlite3")
                        if operation == "audit":
                            allowed = identity["auditor"] and conn.execute(
                                "SELECT 1 FROM audit_grants WHERE principal=? AND cid=?",
                                (identity["principal"], cid),
                            ).fetchone()
                            row = conn.execute("SELECT * FROM formal_history WHERE cid=?", (cid,)).fetchone()
                            if not allowed or row is None:
                                raise Denied()
                            with closing(CanonicalSession(path)) as case:
                                return self._respond(200, case.history(row))
                        grant, reader = authority.issue_reader(conn, cookie, cid)
                    with closing(CanonicalSession(path, grant, reader)) as case:
                        version = case.resolve_version(cid)
                        if version != grant.version or version != args["version_id"]:
                            raise Denied(403, "formal_session_mismatch")
                        result = self._operate(case, reader, cid, operation, args)
                self._respond(200, result)
            except Denied as error:
                self._respond(error.status, {"error": error.category})
            except (LifecycleError, ProjectionError) as error:
                self._respond(403, {"error": error.category})
            except (TypeError, ValueError, KeyError):
                self._respond(400, {"error": "formal_request_invalid"})
            except (sqlite3.Error, OSError):
                self._respond(503, {"error": "formal_storage_unavailable"})

        @staticmethod
        def _operate(case, reader, cid, operation, args):
            if operation == "event":
                return asdict(reader.read_event(args["event_id"], access=reader.access))
            if operation == "source":
                content = reader.open_source(G3SourceRef(**args["ref"]), access=reader.access)
                return {"source_sha256": sha256(content).hexdigest()}
            if operation == "prepare":
                with authority.connect(write=True) as conn:
                    result = case.prepare()
                    conn.execute("INSERT INTO formal_history VALUES (?,?,?,?,?,?)",
                                 (cid, reader.access.principal_id, case.grant.dataset, result["packet_sha256"],
                                  result["attempt_id"], result["activation_id"]))
                return result
            if operation == "packet":
                case.engine.packet(cid, access=reader.access)
                return {"readable": True}
            return case.activate(args["request_id"], args["packet_sha256"])

    return Handler


def main():
    config = Path(sys.argv[1]).resolve()
    root = config.parent
    if config.name != "gateway.json" or not (root / "dedicated-gm02as-test").is_file():
        raise RuntimeError("formal_test_root_required")
    cfg = json.loads(config.read_text())
    public_key = Path(cfg["publicKey"]).read_bytes()
    oidc = OidcLogin(issuer=cfg["issuer"], gateway=cfg["gateway"], public_key=public_key,
                     client_secret=cfg["clientSecret"], ca=cfg["issuerCert"])
    authority = Authority(root, bytes.fromhex(cfg["readerKey"]))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cfg["cert"], cfg["tlsKey"])
    with ThreadingHTTPServer(("127.0.0.1", urlsplit(cfg["gateway"]).port),
                             make_handler(root, authority, oidc, cfg["gateway"])) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
