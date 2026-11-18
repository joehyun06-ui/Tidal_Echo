"""Explicit, test-dataset-only G3 authority adapter for GM-02AS."""

from dataclasses import asdict
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .g3_projection_contract import AccessScope, G3Event, G3Revocation, G3SourceRef, ProjectionError
from .g3_projection_feed import G3FeedPage, G3Scan

TEST_ACCESS = AccessScope("gm02as-g3-reader", frozenset({"gm02as-g3-test"}))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _event(value):
    return G3Event(**{**value, "ref": G3SourceRef(**value["ref"])})


class G3AuthorityTestClient:
    require_authorization_epoch = True

    def __init__(self, base_url, token):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "g3-authority-test")
                or parsed.path or parsed.query or parsed.fragment or parsed.username is not None
                or parsed.password is not None or len(token) < 32):
            raise ProjectionError("g3_test_client_binding_invalid")
        self._url = base_url
        self._token = token
        self._http = build_opener(ProxyHandler({}), _NoRedirect())

    def _get(self, operation, access, **arguments):
        if access != TEST_ACCESS:
            raise ProjectionError("g3_test_access_denied")
        request = Request(
            f"{self._url}/v1/{operation}", data=json.dumps(arguments).encode(),
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with self._http.open(request, timeout=5) as response:
                data = response.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise ValueError
                return json.loads(data)["result"]
        except HTTPError as error:
            error.close()
            category = "g3_test_reference_denied" if error.code == 403 else "g3_test_authority_unavailable"
            raise ProjectionError(category) from None
        except (OSError, ValueError, KeyError):
            raise ProjectionError("g3_test_authority_unavailable") from None

    def begin_scan(self, *, after_cursor, access):
        return G3Scan(**self._get("scan", access, after_cursor=after_cursor))

    def validate_scan(self, scan, *, access):
        return self._get("validate", access, scan=asdict(scan)) is True

    def read_page(self, scan, *, after_cursor, limit, access):
        value = self._get("page", access, scan=asdict(scan), after_cursor=after_cursor, limit=limit)
        return G3FeedPage(**{**value, "events": tuple(_event(item) for item in value["events"])})

    def list_events(self, *, after_cursor, access):
        scan = self.begin_scan(after_cursor=after_cursor, access=access)
        events = []
        for _ in range(50):
            page = self.read_page(scan, after_cursor=after_cursor, limit=200, access=access)
            events.extend(page.events)
            after_cursor = page.next_cursor
            if page.done:
                if not self.validate_scan(scan, access=access):
                    raise ProjectionError("g3_test_scan_changed")
                return tuple(events)
        raise ProjectionError("g3_test_read_budget_exceeded")

    def read_event(self, event_id, *, revision=None, access):
        return _event(self._get("event", access, event_id=event_id, revision=revision))

    def open_source(self, ref, *, access):
        return self._get("source", access, ref=asdict(ref)).encode("utf-8")

    def read_revocations(self, *, after_cursor, access):
        return tuple(G3Revocation(**row) for row in self._get("revocations", access, after_cursor=after_cursor))
