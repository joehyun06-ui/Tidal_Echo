"""Real certificate-chain and hostname TLS handshakes on localhost only.

All outbound socket calls are guarded: original sender attempts numeric
8.8.8.8:443, test redirects only that exact attempt to a 127.0.0.1 listener.
No public DNS, Internet, real IdP, OAuth code or memory writer.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import ssl
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from backend import memory_oidc_https_sender_review as sender_module
from backend.memory_oidc_https_sender_review import (
    OidcHttpsSenderReviewError, PinnedOidcHttpsSenderReview,
    _PinnedHttpsConnection,
)
from backend.memory_oidc_token_exchange_review import OfflineTokenRequest

HOST = "login.example.test"
PUBLIC = "8.8.8.8"
TOKEN_URL = "https://" + HOST + "/token"
_FAKE_BODY = json.dumps(
    {"message": "TLS chain verified only; no actual identity claim"},
    separators=(",", ":"),
).encode("ascii")


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _certificate(
    *, subject, issuer, public_key, issuer_key, is_ca, dns_name=None,
    expired=False,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    starts = now - datetime.timedelta(days=6 if expired else 1)
    ends = now - datetime.timedelta(days=1) if expired else (
        now + datetime.timedelta(days=3)
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(starts).not_valid_after(ends)
        .add_extension(
            x509.BasicConstraints(
                ca=is_ca, path_length=0 if is_ca else None,
            ), critical=True,
        )
    )
    if not is_ca:
        cert = (
            cert.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(dns_name)]),
                critical=False,
            ).add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
        )
    return cert.sign(issuer_key, hashes.SHA256())


class _FixtureCa:
    def __init__(self, folder: Path, *, marker: str):
        self.folder = folder
        self.marker = marker
        self.key = _key()
        self.subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "offline-" + marker + "-ca"),
        ])
        self.certificate = _certificate(
            subject=self.subject, issuer=self.subject,
            public_key=self.key.public_key(), issuer_key=self.key, is_ca=True,
        )
        self.ca_file = self.folder / (marker + "-ca.pem")
        self.ca_file.write_bytes(
            self.certificate.public_bytes(serialization.Encoding.PEM)
        )

    def leaf(self, *, name=HOST, expired=False, tag="valid"):
        server_key = _key()
        sub = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, name),
        ])
        cert = _certificate(
            subject=sub, issuer=self.subject, public_key=server_key.public_key(),
            issuer_key=self.key, is_ca=False, dns_name=name, expired=expired,
        )
        cert_file = self.folder / (self.marker + "-" + tag + "-cert.pem")
        key_file = self.folder / (self.marker + "-" + tag + "-key.pem")
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass  # Windows chmod semantics differ from Unix.
        return cert_file, key_file, cert


class _LoopbackTls:
    def __init__(self, cert_file: Path, key_file: Path):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.listener.settimeout(0.15)
        self.port = self.listener.getsockname()[1]
        self.sni = []
        self.requests = []
        self.handshake_failures = 0
        self.server_errors = []
        self.stopping = threading.Event()
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ctx.load_cert_chain(str(cert_file), str(key_file))
        self.ctx.set_servername_callback(
            lambda _sock, name, _context: self.sni.append(name),
        )
        self.worker = threading.Thread(
            target=self._serve, name="gt-offline-tls-loopback", daemon=True,
        )
        self.worker.start()

    def _serve(self):
        while not self.stopping.is_set():
            try:
                accepted, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                accepted.settimeout(3)
                with self.ctx.wrap_socket(accepted, server_side=True) as secured:
                    secured.settimeout(3)
                    data = bytearray()
                    while b"\r\n\r\n" not in data and len(data) <= 8192:
                        fragment = secured.recv(2048)
                        if not fragment:
                            break
                        data.extend(fragment)
                    self.requests.append(bytes(data))
                    if b"\r\n\r\n" not in data:
                        continue
                    response = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Cache-Control: no-store\r\n"
                        b"Pragma: no-cache\r\n"
                        b"Connection: close\r\n"
                        + ("Content-Length: " + str(len(_FAKE_BODY)) +
                           "\r\n\r\n").encode("ascii")
                        + _FAKE_BODY
                    )
                    secured.sendall(response)
            except (ssl.SSLError, OSError) as error:
                self.server_errors.append(type(error).__name__)
                self.handshake_failures += 1
                try:
                    accepted.close()
                except OSError:
                    pass

    def close(self):
        self.stopping.set()
        self.listener.close()
        self.worker.join(timeout=6)


class PinnedLoopbackTlsCaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.root = _FixtureCa(self.folder, marker="trusted")
        self.other = _FixtureCa(self.folder, marker="untrusted")
        self.attempts = []

    @staticmethod
    def _context(root: _FixtureCa | None):
        if root is None:
            return ssl.create_default_context()
        context = ssl.create_default_context(cafile=str(root.ca_file))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def _server(self, *, ca=None, hostname=HOST, expired=False, tag="valid"):
        ca = ca or self.root
        cert, private, certificate = ca.leaf(
            name=hostname, expired=expired, tag=tag,
        )
        service = _LoopbackTls(cert, private)
        self.addCleanup(service.close)
        return service, certificate

    @contextmanager
    def _only_local_ip_dial(self, server: _LoopbackTls):
        real_create = socket.create_connection
        real_connect = socket.socket.connect
        real_lookup = socket.getaddrinfo

        def guard_connect(sock, address):
            if (
                not isinstance(address, tuple)
                or not address
                or address[0] != "127.0.0.1"
            ):
                raise AssertionError("an attempted non-loopback TCP connection")
            return real_connect(sock, address)

        def pinned_create(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                          source_address=None):
            self.attempts.append(address)
            if address != (PUBLIC, 443):
                raise AssertionError("TLS sender must dial only the vetted IP")
            return real_create(
                ("127.0.0.1", server.port), timeout, source_address,
            )

        def only_numeric_local_dns(host, *args, **kwargs):
            if host != "127.0.0.1":
                raise AssertionError("unexpected DNS lookup")
            return real_lookup(host, *args, **kwargs)

        # Intercept the GLOBAL socket module as a defense in depth: even a
        # future test regression must not connect to an external address.
        with mock.patch.object(socket.socket, "connect", guard_connect):
            with mock.patch.object(socket, "getaddrinfo", only_numeric_local_dns):
                with mock.patch.object(socket, "create_connection", pinned_create):
                    yield

    def _direct(self, server, *, trust=None, expect_fail=False):
        context = self._context(self.root if trust is None else trust)
        conn = _PinnedHttpsConnection(
            HOST, PUBLIC, context=context,
        )
        try:
            with self._only_local_ip_dial(server):
                if expect_fail:
                    with self.assertRaises(ssl.SSLCertVerificationError):
                        conn.request("POST", "/token", body=b"synthetic=true")
                else:
                    conn.request("POST", "/token", body=b"synthetic=true")
                    certificate = conn.sock.getpeercert()
                    try:
                        result = conn.getresponse()
                    except Exception as error:
                        self.fail(
                            'TLS local response failed: ' + type(error).__name__
                            + '; accepted=' + str(len(server.requests))
                            + '; server_errors=' + repr(server.server_errors)
                            + '; sni=' + repr(server.sni)
                        )
                    self.assertEqual(result.status, 200)
                    self.assertEqual(result.read(), _FAKE_BODY)
                    return certificate
        finally:
            conn.close()

    def _sender(self, server, *, trust=None, enabled=True):
        context = self._context(self.root if trust is None else trust)
        transport = PinnedOidcHttpsSenderReview(
            approved_token_endpoint=TOKEN_URL,
            approved_dns_hostname=HOST,
            network_approval_provider=lambda: True,
        )
        request = OfflineTokenRequest(
            method="POST", url=TOKEN_URL,
            headers=(
                ("accept", "application/json"),
                ("content-type", "application/x-www-form-urlencoded"),
                ("cache-control", "no-store"),
            ), body=b"grant_type=authorization_code&code=synthetic-code",
        )
        env = {
            "OPENAI_API_KEY": "",
            "MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED": (
                "true" if enabled else "false"
            ),
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": (
                "true" if enabled else "false"
            ),
        }
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(
                sender_module, "_validated_addresses", return_value=(PUBLIC,),
            ):
                with mock.patch.object(
                    sender_module.ssl, "create_default_context",
                    return_value=context,
                ):
                    with self._only_local_ip_dial(server):
                        return transport(request)

    def test_trusted_ca_actual_tls_handshake_sni_original_dns_host(self):
        srv, certificate = self._server()
        peer = self._direct(srv)
        self.assertEqual(self.attempts, [(PUBLIC, 443)])
        self.assertEqual(srv.sni, [HOST])
        self.assertIn(("subjectAltName"), peer)
        self.assertIn(("DNS", HOST), peer["subjectAltName"])
        self.assertEqual(len(srv.requests), 1)
        self.assertIn(b"POST /token HTTP/1.1", srv.requests[0])
        self.assertIn(("Host: " + HOST).encode(), srv.requests[0])
        self.assertEqual(srv.handshake_failures, 0)

    def test_sender_real_tls_handshake_through_review_transport(self):
        srv, _ = self._server()
        try:
            response = self._sender(srv)
        except OidcHttpsSenderReviewError:
            self.fail(
                'sender local TLS failed: accepted=' + str(len(srv.requests))
                + '; server_errors=' + repr(srv.server_errors)
                + '; sni=' + repr(srv.sni)
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.final_url, TOKEN_URL)
        self.assertEqual(response.body, _FAKE_BODY)
        self.assertEqual(srv.sni, [HOST])
        self.assertEqual(self.attempts, [(PUBLIC, 443)])
        self.assertNotIn("token=SECRET", repr(response))

    def test_untrusted_ca_actual_tls_chain_rejected(self):
        srv, _ = self._server(ca=self.other, tag="badca")
        self._direct(srv, expect_fail=True)
        self.assertEqual(srv.requests, [])
        self.assertEqual(self.attempts, [(PUBLIC, 443)])

    def test_wrong_hostname_actual_certificate_rejected(self):
        srv, _ = self._server(hostname="evil.example.test", tag="badname")
        self._direct(srv, expect_fail=True)
        self.assertEqual(srv.sni, [HOST])
        self.assertEqual(srv.requests, [])

    def test_expired_leaf_actual_certificate_rejected(self):
        srv, _ = self._server(expired=True, tag="expired")
        self._direct(srv, expect_fail=True)
        self.assertEqual(srv.requests, [])

    def test_system_default_ca_does_not_trust_ephemeral_root(self):
        srv, _ = self._server()
        conn = _PinnedHttpsConnection(
            HOST, PUBLIC, context=self._context(None),
        )
        try:
            with self._only_local_ip_dial(srv):
                with self.assertRaises(ssl.SSLCertVerificationError):
                    conn.request("POST", "/token", body=b"synthetic")
        finally:
            conn.close()
        self.assertEqual(len(self.attempts), 1)

    def test_sender_certificate_failure_is_generic_and_no_response(self):
        srv, _ = self._server(ca=self.other, tag="failclose")
        with self.assertRaises(OidcHttpsSenderReviewError) as caught:
            self._sender(srv)
        self.assertEqual(
            str(caught.exception), "memory_oidc_https_sender_unavailable",
        )
        self.assertNotIn("untrusted", str(caught.exception))
        self.assertEqual(srv.requests, [])

    def test_sender_wrong_dns_cert_fails_closed(self):
        srv, _ = self._server(hostname="evil.example.test", tag="failhost")
        with self.assertRaises(OidcHttpsSenderReviewError):
            self._sender(srv)
        self.assertEqual(srv.requests, [])

    def test_sender_default_off_rejects_before_local_connect(self):
        srv, _ = self._server()
        with self.assertRaises(OidcHttpsSenderReviewError):
            self._sender(srv, enabled=False)
        self.assertEqual(self.attempts, [])
        self.assertEqual(srv.requests, [])

    def test_raw_pinned_connection_rejects_unexpected_hostname_dial(self):
        context = self._context(self.root)
        connection = _PinnedHttpsConnection(HOST, PUBLIC, context=context)
        try:
            with self.assertRaises(OidcHttpsSenderReviewError):
                connection._dial_numeric(("other.example.test", 443), 2, None)
        finally:
            connection.close()
        self.assertEqual(self.attempts, [])

    def test_real_tls_config_hostname_verification_and_minimum_version(self):
        context = self._context(self.root)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertNotIn("BEGIN PRIVATE KEY", self.root.ca_file.read_text())


if __name__ == "__main__":
    unittest.main()
