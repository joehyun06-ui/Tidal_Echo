"""Process-separated OIDC, canonical session, G3 scope and revocation acceptance."""

from base64 import urlsafe_b64encode
from contextlib import closing, contextmanager, ExitStack
from hashlib import scrypt
import json
import os
from pathlib import Path
from secrets import token_hex, token_urlsafe
import socket
import sqlite3
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx

from backend import deployment_config
from backend.g3_projection_contract import ProjectionError
from .authority import Authority
from .session import CanonicalSession, provision_case

ISSUER = Path(__file__).with_name("issuer") / "server.mjs"
A_CASES = ("case_active", "case_scope", "case_credential", "case_login")


def _assert(condition, label):
    if not condition:
        raise AssertionError(label)


def _origin(host):
    with socket.socket() as sock:
        sock.bind((host, 0))
        return f"https://{host}:{sock.getsockname()[1]}"


def _save(path, data):
    with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(data, stream)


def _certificate(root, host):
    cert, key = root / "cert.pem", root / "tls-key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", str(key),
         "-out", str(cert), "-subj", f"/CN={host}", "-addext", f"subjectAltName=IP:{host}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    return {"cert": str(cert), "tlsKey": str(key)}


def _configure(root):
    issuer_root, gateway_root = root / "issuer", root / "gateway"
    for directory in (issuer_root, gateway_root):
        directory.mkdir(mode=0o700)
    (gateway_root / "dedicated-gm02as-test").touch()
    issuer, gateway = _origin("127.0.0.2"), _origin("127.0.0.1")
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = private.private_numbers()
    pub = gateway_root / "issuer-signing-public.pem"
    pub.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM,
                                                      serialization.PublicFormat.SubjectPublicKeyInfo))
    pairs = zip(("n", "e", "d", "p", "q", "dp", "dq", "qi"),
                (key.public_numbers.n, key.public_numbers.e, key.d, key.p, key.q, key.dmp1, key.dmq1, key.iqmp))
    jwk = {name: urlsafe_b64encode(number.to_bytes((number.bit_length() + 7) // 8, "big")).decode().rstrip("=")
           for name, number in pairs}
    jwk.update(kty="RSA", alg="RS256", use="sig", kid=uuid4().hex)
    accounts = {name: uuid4().hex for name in ("a", "b", "audit")}
    passwords = {name: token_urlsafe(24) for name in accounts}
    credential_rows = []
    for username, subject in accounts.items():
        salt = token_hex(16)
        password_digest = scrypt(passwords[username].encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32)
        credential_rows.append({"name": username, "subject": subject, "passwordDigest": password_digest.hex(), "salt": salt})
    shared = {"issuer": issuer, "clientSecret": token_urlsafe(32)}
    issuer_cfg = {**shared, **_certificate(issuer_root, "127.0.0.2"), "callback": gateway + "/login/callback",
                  "cookieKey": token_urlsafe(32), "signingKey": jwk, "accounts": credential_rows,
                  "dummyDigest": token_hex(32), "dummySalt": token_hex(16)}
    gateway_key = bytes.fromhex(token_hex(32))
    gateway_cfg = {**shared, **_certificate(gateway_root, "127.0.0.1"), "gateway": gateway, "readerKey": gateway_key.hex(),
                   "issuerCert": issuer_cfg["cert"], "publicKey": str(pub)}
    _save(issuer_root / "issuer.json", issuer_cfg)
    _save(gateway_root / "gateway.json", gateway_cfg)
    authority = Authority(gateway_root, gateway_key)
    cases = [(cid, "a", "dataset_a", "source_a") for cid in A_CASES]
    cases.append(("case_b", "b", "dataset_b", "source_b"))
    authority.provision(issuer, accounts, cases, [
        ("dataset_a", "source_a", "event_a"), ("dataset_b", "source_b", "event_b"),
        ("dataset_a", "source_excluded", "event_excluded"),
    ])
    for cid, *_ in cases:
        provision_case(gateway_root / (cid + ".sqlite3"), cid)
    return issuer_root, gateway_root, issuer_cfg, gateway_cfg, passwords, authority


@contextmanager
def _process(arguments, log_path, *, append=False):
    mode = "ab" if append else "wb"
    with log_path.open(mode) as output:
        process = subprocess.Popen(arguments, stdout=output, stderr=output)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def _ready(http, url, node, gateway):
    for _ in range(100):
        _assert(node.poll() is None and gateway.poll() is None, "test service process exited")
        try:
            if http.get(url).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise AssertionError("test service did not start")


def _http(issuer, gateway):
    ssl_context = ssl.create_default_context(cafile=issuer["cert"])
    ssl_context.load_verify_locations(cafile=gateway["cert"])
    return httpx.Client(verify=ssl_context, trust_env=False, timeout=8)


def _login(client, gateway, issuer, username, password, *, accepted=True):
    response = client.get(gateway + "/login")
    _assert(response.status_code == 302, "login start")
    origin = urlsplit(issuer)
    target_original = urlsplit(gateway)
    for attempt in range(10):
        if response.status_code in (302, 303):
            dest = urljoin(str(response.url), response.headers["location"])
            split = urlsplit(dest)
            _assert((split.scheme, split.netloc) in {(origin.scheme, origin.netloc),
                    (target_original.scheme, target_original.netloc)}, "redirect outside isolated servers")
            response = client.get(dest)
        if response.status_code == 200 and str(response.url).startswith(issuer + "/login/") and "<form" in response.text:
            response = client.post(response.url, headers={"Origin": issuer}, data={"username": username, "password": password})
            if not accepted:
                _assert(response.status_code == 401, "issuer did not reject invalid account password")
                return
        if response.status_code == 200 and str(response.url).startswith(gateway + "/login/callback?"):
            _assert(response.json() == {"authenticated": True} and client.cookies.get("formal_login"),
                    "OIDC login did not create an authenticated session")
            return
        if response.status_code not in (302, 303):
            raise AssertionError(f"unexpected login step HTTP {response.status_code}, iteration {attempt}, path {response.url.path}")
    raise AssertionError("OIDC login did not complete")


def _call(http, gateway, cid, operation, body, expected=200):
    response = http.post(f"{gateway}/v1/{cid}/{operation}", headers={"Origin": gateway}, json=body)
    _assert(response.status_code == expected, f"{cid}/{operation}: expected HTTP {expected}, got {response.status_code} {response.text}")
    return response.json()


def _case_call(http, gateway, cid, operation, expected=200, **body):
    return _call(http, gateway, cid, operation, {"version_id": cid, **body}, expected)


def _check_revoked(gateway, credentials, auditor, prepared, ref):
    results = {}
    for cid, (client, status) in credentials.items():
        observation = prepared[cid]
        activation = {
            "packet_sha256": observation["packet_sha256"], "request_id": "formal_new_activation",
        }
        results[cid] = {
            "old_source": _case_call(client, gateway, cid, "source", status, ref=ref)["error"],
            "old_packet": _case_call(client, gateway, cid, "packet", status)["error"],
            "new_activation_with_old_receipt": _case_call(client, gateway, cid, "activate", status, **activation)["error"],
            "historical_status": _call(auditor, gateway, cid, "audit", {}),
        }
        if cid == "case_active":
            results[cid]["old_activation_replay"] = _case_call(
                client, gateway, cid, "activate", status, packet_sha256=observation["packet_sha256"],
                request_id=observation["activation_id"],
            )["error"]
        history = results[cid]["historical_status"]
        _assert(history["packet_sha256"] == observation["packet_sha256"]
                and history["input_sha256"] == observation["input_sha256"]
                and not history["current_authorization"] and not history["current_window_confirmed"]
                and not history["retry_authorized"], "historical receipt became authorizing")
        _assert(history["activation_at_apply"] == (cid == "case_active"), "historical active status diverged")
    return results


def _check_old_readers(readers):
    for reader in readers.values():
        try:
            reader.read_event("event_a", access=reader.access)
        except ProjectionError:
            continue
        raise AssertionError("old read-only G3 credential survived revocation")


def _revocation_fence(authority):
    completed, started = Event(), Event()

    def revoke():
        started.set()
        authority.revoke("case_scope", "scope")
        completed.set()

    with authority.fence():
        worker = Thread(target=revoke, daemon=True)
        worker.start()
        _assert(started.wait(5) and not completed.wait(0.2), "revocation bypassed operation commit fence")
    worker.join(5)
    _assert(completed.is_set(), "revocation did not complete after durable fence")


def run(root):
    (issuer_root, work, issuer_cfg, gateway_cfg, passwords, authority) = _configure(root)
    gateway, issuer = gateway_cfg["gateway"], issuer_cfg["issuer"]
    node = ["node", str(ISSUER), str(issuer_root / "issuer.json")]
    app = [sys.executable, "-m", "services.g3_formal_auth.gateway", str(work / "gateway.json")]
    with ExitStack() as stack:
        a, b, audit, guest, login_to_revoke = [stack.enter_context(_http(issuer_cfg, gateway_cfg)) for _ in range(5)]
        with _process(node, root / "issuer.log") as auth, _process(app, root / "gateway.log") as web:
            _ready(guest, issuer + "/.well-known/openid-configuration", auth, web)
            _ready(guest, gateway + "/healthz", auth, web)
            _login(guest, gateway, issuer, "a", "incorrect dedicated password", accepted=False)
            for http, name in ((a, "a"), (b, "b"), (audit, "audit"), (login_to_revoke, "a")):
                _login(http, gateway, issuer, name, passwords[name])
            _assert(a.cookies.get("formal_login") != login_to_revoke.cookies.get("formal_login"), "distinct login epochs missing")
            ref = _case_call(a, gateway, "case_scope", "event", event_id="event_a")["ref"]
            ref_b = _case_call(b, gateway, "case_b", "event", event_id="event_b")["ref"]
            _assert("formal_login" not in a.build_request("GET", issuer).headers.get("Cookie", ""),
                    "gateway login cookie leaked to the issuer host")
            _assert(_case_call(a, gateway, "case_scope", "source", ref=ref)["source_sha256"] == ref["source_sha256"],
                    "read-only capability did not read the granted source")
            _assert(guest.post(f"{gateway}/v1/case_scope/event", headers={"Origin": gateway, "X-Principal-Id": "a"},
                               json={"version_id": "case_scope", "event_id": "event_a"}).status_code == 401,
                    "caller-supplied principal authenticated")
            _assert(ref["scope"] == "dataset_a" and ref_b["scope"] == "dataset_b", "scope not mapped by server")
            negative = {
                "unauthenticated": _case_call(guest, gateway, "case_scope", "event", 401, event_id="event_a")["error"],
                "cross_principal": _case_call(a, gateway, "case_b", "event", 403, event_id="event_b")["error"],
                "out_of_dataset": _case_call(a, gateway, "case_scope", "source", 403, ref=ref_b)["error"],
                "out_of_source_in_dataset": _case_call(a, gateway, "case_scope", "event", 403, event_id="event_excluded")["error"],
                "client_scope_injection": _case_call(a, gateway, "case_scope", "event", 400, event_id="event_b", dataset="dataset_b")["error"],
                "cross_session": _call(a, gateway, "case_scope", "event",
                                       {"version_id": "case_active", "event_id": "event_a"}, 403)["error"],
                "audit_role_required": None,
            }
            alternate = "case_b_alternate"
            config = work / "case_b.json"
            original_config = config.read_text()
            with authority.fence(mutate=True), closing(CanonicalSession(work / "case_b.sqlite3", test_control=True)) as session:
                setup = json.loads(original_config)
                setup["sessions"].append({**setup["sessions"][0], "id": alternate})
                deployment_config.atomic_write_text(config, json.dumps(setup))
                session.conn.execute("INSERT INTO web_message_versions VALUES(?,?)", (alternate, "case_b"))
                session.conn.execute("INSERT INTO web_version_selections VALUES(?,?)", ("case_b", alternate))
            negative["canonical_selected_version_changed"] = _case_call(
                b, gateway, "case_b", "event", 403, event_id="event_b",
            )["error"]
            with authority.fence(mutate=True), closing(CanonicalSession(work / "case_b.sqlite3", test_control=True)) as session:
                session.conn.execute("DELETE FROM web_version_selections WHERE root_id=?", ("case_b",))
                session.conn.execute("DELETE FROM web_message_versions WHERE version_id=?", (alternate,))
                deployment_config.atomic_write_text(config, original_config)
            cookies = [(a, "case_active"), (a, "case_scope"), (a, "case_credential"), (login_to_revoke, "case_login")]
            prepared = {cid: _case_call(client, gateway, cid, "prepare") for client, cid in cookies}
            observation = prepared["case_active"]
            positive = work / "case_active.sqlite3"
            for field in ("window_id", "model_run_id"):
                with authority.fence(mutate=True), closing(CanonicalSession(positive, test_control=True)) as session:
                    runtime = session.get("runtime")
                    session.put("runtime", {**runtime, field: "wrong_" + field})
                negative["target_" + field + "_mismatch"] = _case_call(
                    a, gateway, "case_active", "activate", 403, packet_sha256=observation["packet_sha256"],
                    request_id=observation["activation_id"],
                )["error"]
                with authority.fence(mutate=True), closing(CanonicalSession(positive, test_control=True)) as session:
                    session.put("runtime", runtime)
            _assert(_case_call(a, gateway, "case_active", "activate", packet_sha256=observation["packet_sha256"],
                               request_id=observation["activation_id"])["window_confirmed"], "positive control did not activate")
            negative["audit_role_required"] = _call(b, gateway, "case_active", "audit", {}, 403)["error"]
            with authority.connect() as conn:
                old_readers = {cid: authority.issue_reader(conn, client.cookies.get("formal_login"), cid)[1]
                               for client, cid in cookies}
            old_reader = old_readers["case_scope"]
            _assert(old_reader.read_event("event_a", access=old_reader.access).ref.source_id == ref["source_id"],
                    "valid internal reader could not open granted source")
            for excluded in ("event_b", "event_excluded"):
                try:
                    old_reader.read_event(excluded, access=old_reader.access)
                except ProjectionError:
                    continue
                raise AssertionError("valid internal G3 credential escaped source scope")
            _revocation_fence(authority)
            authority.revoke("case_active", "scope")
            authority.revoke("case_credential", "credential")
            authority.revoke("case_login", "login", raw_login=login_to_revoke.cookies.get("formal_login"))
            _check_old_readers(old_readers)
            credentials = {cid: (client, 401 if cid == "case_login" else 403) for client, cid in cookies}
            before = _check_revoked(gateway, credentials, audit, prepared, ref)
            original_pids = [auth.pid, web.pid]
        with _process(node, root / "issuer.log", append=True) as auth, _process(app, root / "gateway.log", append=True) as web:
            _ready(guest, issuer + "/.well-known/openid-configuration", auth, web)
            _ready(guest, gateway + "/healthz", auth, web)
            _assert(original_pids != [auth.pid, web.pid], "processes did not restart")
            _assert(before == _check_revoked(gateway, credentials, audit, prepared, ref), "restart changed policy results")
            _check_old_readers(old_readers)
            _assert(_case_call(b, gateway, "case_b", "event", event_id="event_b")["ref"] == ref_b,
                    "restart or unrelated revocation denied B")
            new_b = stack.enter_context(_http(issuer_cfg, gateway_cfg))
            _login(new_b, gateway, issuer, "b", passwords["b"])
            _assert(_case_call(new_b, gateway, "case_b", "event", event_id="event_b")["ref"] == ref_b,
                    "issuer could not sign in after restart")
            new_a = stack.enter_context(_http(issuer_cfg, gateway_cfg))
            _login(new_a, gateway, issuer, "a", passwords["a"])
            _case_call(new_a, gateway, "case_scope", "packet", 403)
            _case_call(new_a, gateway, "case_credential", "activate", 403,
                       packet_sha256=prepared["case_credential"]["packet_sha256"], request_id="after_relogin")
            _case_call(new_a, gateway, "case_login", "event", event_id="event_a")
            _case_call(new_a, gateway, "case_login", "activate", 403,
                       packet_sha256=prepared["case_login"]["packet_sha256"], request_id="new_login_old_receipt")
        counts = {}
        consumption_counts = {}
        for cid in A_CASES:
            path = work / (cid + ".sqlite3")
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                counts[cid] = conn.execute("SELECT count(*) FROM session_lifecycle_receipts "
                                           "WHERE json_extract(payload, '$.data.phase_at_apply')='active'").fetchone()[0]
                receipt_rows = conn.execute(
                    "SELECT packet_sha256,input_sha256,access_binding,conversation_id,version_id,target_window_id,model_run_id "
                    "FROM session_window_api_consumption_v2",
                ).fetchall()
                consumption_counts[cid] = len(receipt_rows)
                state = json.loads(conn.execute("SELECT payload FROM session_lifecycle_records").fetchone()[0])["data"]
                proof = prepared[cid]
                _assert(state["source_authorization_epoch"] == proof["authorization_sha256"]
                        and receipt_rows == [(
                            proof["packet_sha256"], proof["input_sha256"], state["access_binding"], cid,
                            proof["version_id"], proof["target_window_id"], proof["model_run_id"],
                        )], "persisted receipt lost its identity, grant version, or target binding")
        _assert(all(count == 1 for count in consumption_counts.values()), "historical v2 receipt was rewritten")
        _assert(counts == {"case_active": 1, "case_scope": 0, "case_credential": 0, "case_login": 0},
                "revocation produced an additional activation receipt")
        return {"status": "PASS", "scope": "isolated formal-auth test", "issuer": "oidc-provider@9.15.0",
                "issuer_and_gateway_restarted": True, "bad_password_rejected": True,
                "credential_scope_verified": True, "revocation_excluded_until_commit_fence_released": True,
                "v2_identity_and_authorization_binding_verified": True,
                "authorized_sources": {"a": ref, "b": ref_b},
                "negative_cases": negative, "prepared": prepared, "v2_receipt_counts": consumption_counts,
                "revoked_before_and_after_restart": before, "activation_receipt_counts": counts,
                "production": "HOLD"}


def main():
    with TemporaryDirectory(prefix="gm02as-formal-auth-") as directory:
        root = Path(directory)
        try:
            result = run(root)
        except Exception:
            for name in ("issuer.log", "gateway.log"):
                path = root / name
                if path.exists():
                    print(path.read_text()[-3500:], file=sys.stderr)
            raise
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
