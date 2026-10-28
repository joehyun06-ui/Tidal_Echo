"""Request identity trust boundary; synthetic only, no HTTP server or writes."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from backend.memory_auth_epoch_store import (
    AuthEpochRegistry, AuthenticatedLogin, ServerIssuedEpoch,
)
from backend.memory_explicit_actions import RememberExplicitMemoryRequest
from backend.memory_request_identity_boundary import (
    MemoryRequestIdentityBoundary, MemoryRequestIdentityError,
    ServerAuthenticatedRequest, RequestIdentityReviewResult,
)
from backend.memory_shared_confirmation_ticket import TrustedTicketContext
from backend.tests._memory_session_confirm_worker import valid_request
from backend.tests._support import NoNetworkMixin


class RequestIdentityBoundaryTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "synthetic-auth-epoch.sqlite")
        flag = mock.patch.dict(os.environ, {
            "MEMORY_PER_USER_AUTH_REVIEW_ENABLED": "true",
            "MEMORY_TRUSTED_AUTH_EPOCH_ENABLED": "true",
            "OPENAI_API_KEY": "",
        })
        flag.start()
        self.addCleanup(flag.stop)
        self.state = {
            "principal": "synthetic-user",
            "login": "L" * 32,
            "epoch": None,
            "session": "s1",
            "csrf": "C" * 43,
            "origin": "https://example.test",
        }
        self.registry = AuthEpochRegistry(
            path=self.path,
            identity_provider=lambda: AuthenticatedLogin(
                self.state["principal"], self.state["login"]
            ),
        )
        self.state["epoch"] = self.registry.issue_after_authenticated_login()
        self.request_data = valid_request()
        self.authenticator = lambda _request: ServerAuthenticatedRequest(
            identity=TrustedTicketContext(
                principal_id=self.state["principal"],
                login_id=self.state["login"],
                auth_epoch=self.state["epoch"].value,
                session_id=self.state["session"],
                canonical_sessions=({"id": "s1"}, {"id": "s2"}),
            ),
            csrf_token=self.state["csrf"],
            browser_origin=self.state["origin"],
        )
        self.check_epoch = lambda identity: self.registry.assert_current(
            ServerIssuedEpoch(identity.auth_epoch)
        )
        self.boundary = MemoryRequestIdentityBoundary(
            authenticator=self.authenticator,
            epoch_checker=self.check_epoch,
            expected_origin="https://example.test",
        )

    def request(self, *, method="POST",
                path="/__offline_review__/memory/identity", query="",
                headers=None):
        pairs = [
            ("origin", "https://example.test"),
            ("sec-fetch-site", "same-origin"),
            ("x-memory-csrf", "C" * 43),
            ("content-type", "application/json"),
            # Even this valid shared relay token cannot establish identity.
            ("authorization", "Bearer shared-relay-secret-not-a-user"),
        ] if headers is None else headers
        return Request({
            "type": "http", "method": method, "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": [
                (name.encode("ascii"), value.encode("ascii"))
                for name, value in pairs
            ],
            "scheme": "https", "server": ("example.test", 443),
            "client": ("127.0.0.1", 1234),
        })

    def reject(self, *, headers=None, method="POST", path=None, query=""):
        with self.assertRaises(MemoryRequestIdentityError):
            self.boundary.review(
                self.request(
                    headers=headers, method=method,
                    path=path or "/__offline_review__/memory/identity",
                    query=query,
                ),
                self.request_data,
            )

    def test_no_provider_even_with_shared_relay_bearer_fails_closed(self):
        boundary = MemoryRequestIdentityBoundary(
            expected_origin="https://example.test",
        )
        with self.assertRaises(MemoryRequestIdentityError):
            boundary.review(self.request(), self.request_data)

    def test_default_off_even_with_mock_per_user_provider(self):
        os.environ.pop("MEMORY_PER_USER_AUTH_REVIEW_ENABLED")
        self.reject()

    def test_synthetic_authenticated_request_review_only_never_writes(self):
        result = self.boundary.review(self.request(), self.request_data)
        self.assertEqual(result, RequestIdentityReviewResult())
        self.assertTrue(result.accepted_for_review)
        self.assertFalse(result.may_write_memory)
        self.assertNotIn("synthetic-user", repr(result))
        self.assertFalse(hasattr(self.boundary, "remember"))
        self.assertFalse(hasattr(self.boundary, "commit"))

    def test_absent_and_wrong_browser_origin_fails(self):
        for origin in ("", "null", "https://evil.example.test",
                       "https://example.test.evil", "http://example.test",
                       "https://example.test:443", "https://example.test/path"):
            with self.subTest(origin=origin):
                headers = [
                    ("origin", origin), ("sec-fetch-site", "same-origin"),
                    ("x-memory-csrf", "C" * 43),
                    ("content-type", "application/json"),
                ]
                self.reject(headers=headers)

    def test_query_credentials_and_client_session_selection_rejected(self):
        for query in ("token=secret", "session_id=s2", "principal=synthetic-user"):
            with self.subTest(query=query):
                self.reject(query=query)

    def test_browser_headers_cannot_assert_principal_or_login_epoch(self):
        for name in (
            "x-principal-id", "x-user-id", "x-account-id", "x-auth-epoch",
            "x-login-id", "x-session-id", "x-memory-identity",
        ):
            with self.subTest(header=name):
                self.reject(headers=[
                    ("origin", "https://example.test"),
                    ("sec-fetch-site", "same-origin"),
                    ("x-memory-csrf", "C"*43),
                    ("content-type", "application/json"),
                    (name, "forged"),
                ])

    def test_duplicate_origin_or_csrf_headers_rejected(self):
        for duplicate in ("origin", "x-memory-csrf", "sec-fetch-site"):
            with self.subTest(duplicate=duplicate):
                base = [
                    ("origin", "https://example.test"),
                    ("sec-fetch-site", "same-origin"),
                    ("x-memory-csrf", "C"*43),
                    ("content-type", "application/json"),
                ]
                base.append(next(item for item in base if item[0] == duplicate))
                self.reject(headers=base)

    def test_wrong_csrf_and_absent_csrf_rejected(self):
        for csrf in (None, "", "X"*43, "C"*42, "C"*44):
            with self.subTest(csrf=csrf):
                self.reject(headers=[
                    ("origin", "https://example.test"),
                    ("sec-fetch-site", "same-origin"),
                    ("content-type", "application/json"),
                    *([] if csrf is None else [("x-memory-csrf", csrf)]),
                ])

    def test_only_same_origin_fetch_metadata_allowed(self):
        for site in ("cross-site", "same-site", "none", "", "Same-Origin"):
            with self.subTest(site=site):
                self.reject(headers=[
                    ("origin", "https://example.test"),
                    ("sec-fetch-site", site),
                    ("x-memory-csrf", "C"*43),
                    ("content-type", "application/json"),
                ])

    def test_wrong_method_path_and_content_type_rejected(self):
        self.reject(method="GET")
        self.reject(path="/app/memory/session-write/preflight")
        self.reject(headers=[
            ("origin", "https://example.test"),
            ("sec-fetch-site", "same-origin"),
            ("x-memory-csrf", "C"*43),
            ("content-type", "application/x-www-form-urlencoded"),
        ])

    def test_relay_token_without_per_user_login_fails_even_when_headers_valid(self):
        boundary = MemoryRequestIdentityBoundary(
            authenticator=lambda request: None,
            epoch_checker=self.check_epoch,
            expected_origin="https://example.test",
        )
        with self.assertRaises(MemoryRequestIdentityError):
            boundary.review(self.request(), self.request_data)

    def test_client_request_state_does_not_grant_identity(self):
        req = self.request()
        req.state.principal_id = "synthetic-user"
        req.state.auth_epoch = self.state["epoch"].value
        req.state.csrf_token = self.state["csrf"]
        boundary = MemoryRequestIdentityBoundary(
            authenticator=None, epoch_checker=self.check_epoch,
            expected_origin="https://example.test",
        )
        with self.assertRaises(MemoryRequestIdentityError):
            boundary.review(req, self.request_data)

    def test_provider_wrong_type_or_missing_identity_rejected(self):
        for result in (
            {"principal_id": "synthetic-user"},
            True, object(), None,
        ):
            with self.subTest(result=type(result).__name__):
                b = MemoryRequestIdentityBoundary(
                    authenticator=lambda _r, v=result: v,
                    epoch_checker=self.check_epoch,
                    expected_origin="https://example.test",
                )
                with self.assertRaises(MemoryRequestIdentityError):
                    b.review(self.request(), self.request_data)

    def test_cross_session_memory_scope_denied(self):
        self.state["session"] = "s2"
        self.reject()

    def test_rotated_epoch_denied_until_trusted_provider_rotates(self):
        self.registry.issue_after_authenticated_login()
        self.reject()

    def test_logout_revoked_epoch_denied(self):
        self.assertTrue(self.registry.revoke_before_logout())
        self.reject()

    def test_wrong_principal_or_login_denied(self):
        self.state["principal"] = "synthetic-other"
        self.reject()
        self.state["principal"] = "synthetic-user"
        self.state["login"] = "D"*32
        self.reject()

    def test_trusted_provider_error_fails_closed(self):
        def broken(_request):
            raise RuntimeError("synthetic_login_failed")
        b = MemoryRequestIdentityBoundary(
            authenticator=broken, epoch_checker=self.check_epoch,
            expected_origin="https://example.test",
        )
        with self.assertRaisesRegex(
            MemoryRequestIdentityError, "memory_per_user_auth_not_ready"
        ):
            b.review(self.request(), self.request_data)

    def test_missing_epoch_checker_not_accepted(self):
        b = MemoryRequestIdentityBoundary(
            authenticator=self.authenticator,
            expected_origin="https://example.test",
        )
        with self.assertRaises(MemoryRequestIdentityError):
            b.review(self.request(), self.request_data)

    def test_feature_flag_revoked_in_epoch_checker_fails_closed(self):
        def disable(identity):
            self.check_epoch(identity)
            os.environ["MEMORY_PER_USER_AUTH_REVIEW_ENABLED"] = "false"
        b = MemoryRequestIdentityBoundary(
            authenticator=self.authenticator,
            epoch_checker=disable,
            expected_origin="https://example.test",
        )
        with self.assertRaises(MemoryRequestIdentityError):
            b.review(self.request(), self.request_data)

    def test_invalid_expected_origin_denied_at_construction(self):
        for origin in ("http://example.test", "https://example.test/path",
                       "https://evil.test@host.test", "null"):
            with self.assertRaises(MemoryRequestIdentityError):
                MemoryRequestIdentityBoundary(expected_origin=origin)

    def test_canonical_session_absent_denied(self):
        request = RememberExplicitMemoryRequest(
            request_id=self.request_data.request_id,
            kind=self.request_data.kind,
            scope_type=self.request_data.scope_type,
            scope_ref="s3",
            content=self.request_data.content,
            sensitivity=self.request_data.sensitivity,
        )
        with self.assertRaises(MemoryRequestIdentityError):
            self.boundary.review(self.request(), request)


if __name__ == "__main__":
    unittest.main()
