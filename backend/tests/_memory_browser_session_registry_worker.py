"""Isolated synthetic browser session credentials; no network or write routes."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from backend.memory_auth_epoch_store import AuthEpochRegistry, AuthenticatedLogin
from backend.memory_browser_session_registry import (
    BrowserLoginSessionRegistry, BrowserLoginSessionError,
    BrowserSessionReview,
)
from backend.memory_shared_confirmation_ticket import TrustedTicketContext
from backend.tests._memory_session_confirm_worker import valid_request
from backend.tests._support import NoNetworkMixin


class BrowserLoginSessionTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "browser-sessions.sqlite")
        patch = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            "MEMORY_BROWSER_SESSION_REGISTRY_REVIEW_ENABLED": "true",
            "MEMORY_TRUSTED_AUTH_EPOCH_ENABLED": "true",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.state = {
            "principal": "synthetic-person",
            "login": "L" * 32,
            "epoch": None, "session": "s1",
            "canonical": ({"id": "s1"}, {"id": "s2"}),
        }
        self.now = [10000.0]
        self.epochs = AuthEpochRegistry(
            path=self.path,
            identity_provider=lambda: AuthenticatedLogin(
                self.state["principal"], self.state["login"]
            ),
        )
        self.state["epoch"] = self.epochs.issue_after_authenticated_login()
        self.issuer = lambda: TrustedTicketContext(
            principal_id=self.state["principal"],
            login_id=self.state["login"],
            auth_epoch=self.state["epoch"].value,
            session_id=self.state["session"],
            canonical_sessions=self.state["canonical"],
        )
        self.store = self.new_store()
        self.memory = valid_request()
        self.credentials = None

    def new_store(self, *, issuer=True, canonical=True):
        return BrowserLoginSessionRegistry(
            path=self.path, expected_origin="https://example.test",
            login_event_provider=self.issuer if issuer else None,
            canonical_sessions_provider=(
                lambda: self.state["canonical"] if canonical else None
            ),
            clock=lambda: self.now[0],
        )

    def issued(self):
        self.credentials = self.store.issue_after_independently_authenticated_login()
        return self.credentials

    def asgi(self, creds=None, *, headers=None, query="", method="POST",
             path="/__offline_review__/memory/session"):
        credentials = creds or self.credentials
        pairs = [
            ("origin", "https://example.test"),
            ("sec-fetch-site", "same-origin"),
            ("x-memory-csrf", credentials.csrf_token if credentials else "C" * 43),
            ("content-type", "application/json"),
            ("cookie", "__Host-gtmem-review=" + (
                credentials.token if credentials else "T"*43
            )),
            ("authorization", "Bearer legacy-shared-relay-key"),
        ] if headers is None else headers
        return Request({
            "type": "http", "method": method, "path": path,
            "raw_path": path.encode("ascii"),
            "scheme": "https", "server": ("example.test", 443),
            "client": ("127.0.0.1", 12345),
            "query_string": query.encode("ascii"),
            "headers": [(k.encode("ascii"), v.encode("ascii")) for k,v in pairs],
        })

    def accepted(self, creds=None, request=None, store=None):
        return (store or self.store).verify_request_for_review(
            request or self.asgi(creds), self.memory,
        )

    def test_default_off_issues_nothing(self):
        del os.environ["MEMORY_BROWSER_SESSION_REGISTRY_REVIEW_ENABLED"]
        with self.assertRaises(BrowserLoginSessionError):
            self.issued()
        self.assertFalse(Path(self.path).exists() and self._table_exists())

    def _table_exists(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name="
                "'memory_browser_login_session_v1'"
            ).fetchone())

    def test_no_independent_login_provider_even_with_relay_bearer(self):
        with self.assertRaises(BrowserLoginSessionError):
            self.new_store(issuer=False).issue_after_independently_authenticated_login()
        with self.assertRaises(BrowserLoginSessionError):
            self.accepted()

    def test_issued_credentials_verify_only_as_review(self):
        creds = self.issued()
        self.assertEqual(self.accepted(), BrowserSessionReview())
        self.assertFalse(self.accepted().may_write_memory)
        self.assertNotIn(creds.token, repr(creds))
        self.assertNotIn(creds.csrf_token, repr(creds))
        self.assertFalse(hasattr(self.store, "remember"))
        self.assertFalse(hasattr(self.store, "commit"))

    def test_secret_values_not_persisted_in_sqlite_rows(self):
        creds = self.issued()
        with closing(sqlite3.connect(self.path)) as conn:
            row = str(conn.execute(
                "SELECT * FROM memory_browser_login_session_v1"
            ).fetchall())
        self.assertNotIn(creds.token, row)
        self.assertNotIn(creds.csrf_token, row)
        self.assertIn("synthetic-person", row)

    def test_shared_bearer_alone_cannot_verify_session(self):
        self.issued()
        headers = [
            ("origin", "https://example.test"),
            ("sec-fetch-site", "same-origin"),
            ("x-memory-csrf", self.credentials.csrf_token),
            ("content-type", "application/json"),
            ("authorization", "Bearer legacy-shared-relay-key"),
        ]
        with self.assertRaises(BrowserLoginSessionError):
            self.accepted(request=self.asgi(headers=headers))

    def test_random_cookie_and_wrong_csrf_rejected(self):
        self.issued()
        fake = type(self.credentials)(token="X"*43,
                                      csrf_token=self.credentials.csrf_token)
        with self.assertRaises(BrowserLoginSessionError):
            self.accepted(creds=fake)
        fake = type(self.credentials)(token=self.credentials.token,
                                      csrf_token="X"*43)
        with self.assertRaises(BrowserLoginSessionError):
            self.accepted(creds=fake)

    def test_duplicate_cookie_and_csrf_headers_rejected(self):
        self.issued()
        pairs = [
            ("origin", "https://example.test"),
            ("sec-fetch-site", "same-origin"),
            ("x-memory-csrf", self.credentials.csrf_token),
            ("content-type", "application/json"),
            ("cookie", "__Host-gtmem-review=" + self.credentials.token),
        ]
        for duplicate in ("cookie", "x-memory-csrf", "origin", "sec-fetch-site"):
            with self.subTest(duplicate=duplicate):
                with self.assertRaises(BrowserLoginSessionError):
                    self.accepted(request=self.asgi(
                        headers=pairs + [next(p for p in pairs if p[0] == duplicate)]
                    ))
        with self.assertRaises(BrowserLoginSessionError):
            self.accepted(request=self.asgi(headers=pairs[:-1]+[
                ("cookie", "__Host-gtmem-review="+self.credentials.token+
                 "; __Host-gtmem-review="+self.credentials.token)
            ]))

    def test_cross_origin_and_fetch_site_forbidden(self):
        self.issued()
        orig = self.asgi().headers
        for key, value in (
            ("origin", "https://evil.test"),
            ("origin", "https://example.test.evil"),
            ("origin", "http://example.test"),
            ("sec-fetch-site", "cross-site"),
            ("sec-fetch-site", "none"),
            ("content-type", "application/x-www-form-urlencoded"),
        ):
            with self.subTest(key=key, value=value):
                h = [(k.decode(),v.decode()) for k,v in orig.raw
                     if k.decode() != key]
                h.append((key,value))
                with self.assertRaises(BrowserLoginSessionError):
                    self.accepted(request=self.asgi(headers=h))

    def test_query_and_user_supplied_header_rejected(self):
        self.issued()
        for query in ("token=fake", "session_id=s2", "principal=synthetic-person"):
            with self.subTest(query=query):
                with self.assertRaises(BrowserLoginSessionError):
                    self.accepted(request=self.asgi(query=query))
        for name in ("x-principal-id", "x-user-id", "x-login-id",
                     "x-session-id", "x-auth-epoch", "x-memory-identity"):
            with self.subTest(name=name):
                h = [(k.decode(),v.decode()) for k,v in self.asgi().headers.raw]
                with self.assertRaises(BrowserLoginSessionError):
                    self.accepted(request=self.asgi(headers=h+[(name,"forged")]))

    def test_wrong_method_and_public_path_rejected(self):
        self.issued()
        for method, path in (
            ("GET", "/__offline_review__/memory/session"),
            ("POST", "/app/memory/session-write/preflight"),
            ("POST", "/app/memory/session-write"),
        ):
            with self.assertRaises(BrowserLoginSessionError):
                self.accepted(request=self.asgi(method=method,path=path))

    def test_session_scope_mismatch_and_removed_canonical_session(self):
        self.issued()
        self.memory = valid_request(session="s2")
        with self.assertRaises(BrowserLoginSessionError):
            self.accepted()
        self.memory = valid_request()
        self.state["canonical"] = ({"id": "s2"},)
        with self.assertRaises(BrowserLoginSessionError):
            self.accepted()

    def test_epoch_rotation_invalidates_old_cookie_across_instances(self):
        self.issued()
        self.assertEqual(self.accepted(store=self.new_store()).verified_for_review, True)
        self.state["epoch"] = self.epochs.issue_after_authenticated_login()
        with self.assertRaises(BrowserLoginSessionError):
            self.accepted(store=self.new_store())

    def test_logout_epoch_revocation_invalidates_even_unrevoked_cookie(self):
        self.issued()
        self.assertTrue(self.epochs.revoke_before_logout())
        with self.assertRaises(BrowserLoginSessionError):
            self.accepted()

    def test_cookie_logout_revokes_across_instances(self):
        self.issued()
        self.new_store().logout(self.asgi())
        with self.assertRaises(BrowserLoginSessionError):
            self.accepted()
        with self.assertRaises(BrowserLoginSessionError):
            self.new_store().logout(self.asgi())

    def test_atomic_rotation_invalidates_old_and_accepts_new(self):
        old = self.issued()
        new = self.new_store().rotate(self.asgi(old))
        self.assertNotEqual(old.token, new.token)
        with self.assertRaises(BrowserLoginSessionError):
            self.accepted(creds=old)
        self.assertTrue(self.accepted(creds=new).verified_for_review)
        with self.assertRaises(BrowserLoginSessionError):
            self.new_store().rotate(self.asgi(old))

    def test_concurrent_rotation_one_winner(self):
        old = self.issued()
        barrier = threading.Barrier(3)
        winners, losses = [], []
        def rotate():
            barrier.wait(timeout=4)
            try:
                winners.append(self.new_store().rotate(self.asgi(old)))
            except BrowserLoginSessionError:
                losses.append(True)
        threads = [threading.Thread(target=rotate) for _ in range(2)]
        for t in threads: t.start()
        barrier.wait(timeout=4)
        for t in threads:
            t.join(timeout=7)
            self.assertFalse(t.is_alive())
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losses), 1)
        self.assertTrue(self.accepted(creds=winners[0]).verified_for_review)

    def test_session_expiry_is_fail_closed(self):
        self.issued()
        self.now[0] += 901
        with self.assertRaises(BrowserLoginSessionError):
            self.accepted()
        with self.assertRaises(BrowserLoginSessionError):
            self.new_store().rotate(self.asgi())

    def test_missing_or_wrong_epoch_does_not_issue(self):
        self.state["epoch"] = type(self.state["epoch"])("X"*43)
        with self.assertRaises(BrowserLoginSessionError):
            self.issued()
        self.assertEqual(self._rows(), 0)

    def _rows(self):
        with closing(sqlite3.connect(self.path)) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='memory_browser_login_session_v1'"
            ).fetchone()
            if exists is None:
                # Transactional DDL rolls back as well: no table is even
                # stronger fail-closed evidence than an empty table.
                return 0
            return conn.execute(
                "SELECT COUNT(*) FROM memory_browser_login_session_v1"
            ).fetchone()[0]

    def test_logout_before_issue_blocks(self):
        self.assertTrue(self.epochs.revoke_before_logout())
        with self.assertRaises(BrowserLoginSessionError):
            self.issued()
        self.assertEqual(self._rows(), 0)

    def test_provider_wrong_type_cannot_mint(self):
        for val in (None, True, {"principal":"synthetic-person"}):
            wrong = BrowserLoginSessionRegistry(
                path=self.path, expected_origin="https://example.test",
                login_event_provider=lambda v=val: v,
                canonical_sessions_provider=lambda: self.state["canonical"],
            )
            with self.assertRaises(BrowserLoginSessionError):
                wrong.issue_after_independently_authenticated_login()

    def test_disable_on_callback_rolls_back_issuance(self):
        def disable():
            os.environ["MEMORY_BROWSER_SESSION_REGISTRY_REVIEW_ENABLED"]="false"
            return self.issuer()
        wrong = BrowserLoginSessionRegistry(
            path=self.path, expected_origin="https://example.test",
            login_event_provider=disable,
            canonical_sessions_provider=lambda: self.state["canonical"],
        )
        with self.assertRaises(BrowserLoginSessionError):
            wrong.issue_after_independently_authenticated_login()
        self.assertEqual(self._rows(), 0)

    def test_untrusted_request_state_does_not_identify_person(self):
        self.issued()
        req = self.asgi()
        req.state.principal_id = "other"
        req.state.login_id = "X"*32
        self.assertEqual(self.accepted(request=req).verified_for_review, True)
        # The identity comes from durable token binding, not request.state.

    def test_no_write_route_added_to_existing_relay(self):
        self.assertFalse(hasattr(self.store, "app"))
        self.assertFalse(hasattr(self.store, "remember"))
        self.assertFalse(hasattr(self.store, "prepare"))
        self.assertFalse(hasattr(self.store, "confirm"))


if __name__ == "__main__":
    unittest.main()
