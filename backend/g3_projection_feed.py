"""GM-01A2: bounded paged scans and rebuildable derived indexes, offline only.

Explicitly instantiated in-memory candidate; no runtime hook, persistence, route,
credentials, scheduler, model, or production G3 connector is installed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from threading import RLock
from typing import Protocol

from .g3_projection import (
    _valid_id, _validate_event, build_projection_card, verify_projection_card,
)
from .g3_projection_contract import (
    AccessScope, G3Event, G3ReadOnlySourceAdapter, ProjectionCard, ProjectionError,
)


def _deny(category="projection_feed_invalid"):
    raise ProjectionError(category)


def _binding(access):
    if (not isinstance(access, AccessScope) or not _valid_id(access.principal_id)
            or not isinstance(access.allowed_scopes, frozenset)
            or not all(_valid_id(value) for value in access.allowed_scopes)):
        _deny("projection_access_invalid")
    payload = [access.principal_id, sorted(access.allowed_scopes)]
    return hashlib.sha256(json.dumps(payload, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class G3Scan:
    """Trusted adapter declaration, NOT a signed completeness/freshness proof.

    authorization_epoch must change for policy AND per-event visibility changes.
    source_generation identifies a source history, not an independently trusted C.
    """
    scan_id: str
    source_generation: str
    source_epoch: str
    authorization_epoch: str
    access_binding: str
    after_cursor: int
    upper_cursor: int


@dataclass(frozen=True)
class G3FeedPage:
    scan_id: str
    after_cursor: int
    next_cursor: int
    events: tuple[G3Event, ...]
    done: bool


class PagedG3ReadOnlySourceAdapter(G3ReadOnlySourceAdapter, Protocol):
    """Proposed reads only. Real G3 must provide transactional snapshot semantics.

    read_page must cover its entire declared cursor interval, including changes
    hidden by ACL; filtered pages may be empty. It must NOT silently omit data.
    validate_scan must fail when source history or authorization changes.
    """
    def begin_scan(self, *, after_cursor: int, access: AccessScope) -> G3Scan: ...

    def read_page(self, scan: G3Scan, *, after_cursor: int, limit: int,
                  access: AccessScope) -> G3FeedPage: ...

    def validate_scan(self, scan: G3Scan, *, access: AccessScope) -> bool: ...


@dataclass(frozen=True)
class ProjectionIndexProgress:
    """Derived operational counters only, not evidence of current source ACL."""
    cursor: int
    projection_count: int
    authority: str = "derived"


@dataclass(frozen=True)
class ProjectionRefresh:
    cursor: int
    pages: int
    changed_events: int
    projection_count: int
    rebuilt_for_authorization: bool
    rebuilt_for_invalidated_read: bool = False
    authority: str = "derived"


class RebuildableProjectionIndex:
    """Private staging then atomic pointer swap; cursors are NOT durable yet.

    Consumers must use read(), which reopens authority on every call. Internal
    cards are never a safe serving/export API. clear() deletes derived state only.
    """
    def __init__(self):
        self._lock = RLock()
        self._serial = 0
        self._context = None
        self._generation = None
        self._authorization = None
        self._needs_rebuild = False
        self._cursor = 0
        self._cards: dict[str, ProjectionCard] = {}

    def _bound(self, binding):
        if self._context is not None and self._context != binding:
            _deny("projection_access_binding_mismatch")

    def progress(self, *, access: AccessScope) -> ProjectionIndexProgress:
        with self._lock:
            self._bound(_binding(access))
            return ProjectionIndexProgress(self._cursor, len(self._cards))

    def clear(self, *, access: AccessScope) -> None:
        with self._lock:
            self._bound(_binding(access))
            self._cards = {}
            self._context = self._generation = self._authorization = None
            self._needs_rebuild = False
            self._cursor = 0
            self._serial += 1

    @staticmethod
    def _scan(adapter, after, access, binding, generation):
        scan = adapter.begin_scan(after_cursor=after, access=access)
        if (not isinstance(scan, G3Scan)
                or not all(_valid_id(value) for value in (scan.scan_id, scan.source_generation,
                               scan.source_epoch, scan.authorization_epoch))
                or scan.access_binding != binding or type(scan.after_cursor) is not int
                or type(scan.upper_cursor) is not int or scan.after_cursor != after
                or scan.upper_cursor < after):
            _deny()
        if generation is not None and scan.source_generation != generation:
            _deny("projection_source_generation_changed")
        return scan

    def refresh(self, adapter: PagedG3ReadOnlySourceAdapter, *, access: AccessScope,
                page_size=100, max_pages=100, max_changes=10000) -> ProjectionRefresh:
        if (type(page_size) is not int or not 1 <= page_size <= 200
                or type(max_pages) is not int or not 1 <= max_pages <= 1000
                or type(max_changes) is not int or not 1 <= max_changes <= 10000):
            _deny("projection_scan_budget_invalid")
        binding = _binding(access)
        with self._lock:
            self._bound(binding)
            serial, cursor = self._serial, self._cursor
            generation, authorization = self._generation, self._authorization
            invalidated_read = self._needs_rebuild
            cards = dict(self._cards)
        scan = self._scan(adapter, cursor, access, binding, generation)
        rebuilt = authorization is not None and authorization != scan.authorization_epoch
        if rebuilt or invalidated_read:
            cards = {}
            cursor = 0
            scan = self._scan(adapter, 0, access, binding, generation)
        changed = {}
        total = pages = 0
        while True:
            if pages >= max_pages:
                _deny("projection_scan_budget_exceeded")
            page = adapter.read_page(scan, after_cursor=cursor, limit=page_size, access=access)
            pages += 1
            if (not isinstance(page, G3FeedPage) or page.scan_id != scan.scan_id
                    or type(page.after_cursor) is not int or page.after_cursor != cursor
                    or type(page.next_cursor) is not int or type(page.done) is not bool
                    or not isinstance(page.events, tuple) or len(page.events) > page_size
                    or not cursor <= page.next_cursor <= scan.upper_cursor
                    or (page.done and page.next_cursor != scan.upper_cursor)
                    or (not page.done and not cursor < page.next_cursor < scan.upper_cursor)):
                _deny()
            last = cursor
            for event in page.events:
                _validate_event(event, access)
                if not last < event.cursor <= page.next_cursor:
                    _deny()
                previous = changed.get(event.ref.event_id)
                old_card = cards.get(event.ref.event_id)
                previous_revision = (previous.ref.revision if previous is not None else
                                     old_card.source_refs[0].revision if old_card is not None else 0)
                if event.ref.revision <= previous_revision:
                    _deny()
                changed[event.ref.event_id] = event
                last = event.cursor
                total += 1
                if total > max_changes:
                    _deny("projection_scan_budget_exceeded")
            cursor = page.next_cursor
            if page.done:
                break
        # Collapse multiple revisions before deriving; stale historical bytes are
        # never opened. A revoked head is only an eviction, not an authority write.
        for event_id, event in changed.items():
            current = adapter.read_event(event_id, revision=None, access=access)
            if current != event:
                _deny("projection_scan_changed")
            cards.pop(event_id, None)
            if event.status == "active":
                cards[event_id] = build_projection_card(adapter, event_id, access=access,
                                                        expected_revision=event.ref.revision)
        if len(cards) > 10000:
            _deny("projection_scan_budget_exceeded")
        with self._lock:
            if adapter.validate_scan(scan, access=access) is not True:
                _deny("projection_scan_changed")
            if self._serial != serial:
                _deny("projection_refresh_conflict")
            self._cards, self._cursor = cards, cursor
            self._context, self._generation = binding, scan.source_generation
            self._authorization = scan.authorization_epoch
            self._needs_rebuild = False
            self._serial += 1
        return ProjectionRefresh(cursor, pages, len(changed), len(cards), rebuilt, invalidated_read)

    def read(self, adapter: PagedG3ReadOnlySourceAdapter, event_id: str, *, access: AccessScope) -> ProjectionCard:
        if not _valid_id(event_id):
            _deny("projection_source_invalid")
        binding = _binding(access)
        with self._lock:
            self._bound(binding)
            card = self._cards.get(event_id)
            cursor, generation = self._cursor, self._generation
        if card is None:
            _deny("projection_not_indexed")
        try:
            scan = self._scan(adapter, cursor, access, binding, generation)
            verified = verify_projection_card(adapter, card, access=access)
            if adapter.validate_scan(scan, access=access) is not True:
                _deny("projection_scan_changed")
        except ProjectionError:
            with self._lock:
                if self._cards.get(event_id) is card:
                    self._cards = {key: value for key, value in self._cards.items() if key != event_id}
                    self._needs_rebuild = True
                    self._serial += 1
            raise
        with self._lock:
            self._bound(binding)
            if self._cards.get(event_id) is not card:
                _deny("projection_refresh_conflict")
        return verified
