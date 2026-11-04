"""GM-01A: reconstructible, permission-checked projections of G3 evidence.

This module neither installs a route nor reads production G3. It never creates,
changes, revokes, or deletes authority events. All materialization and serving
fail closed against the current G3 head; no cached card is safe to serve alone.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from .g3_projection_contract import (
    AccessScope, G3Event, G3ReadOnlySourceAdapter, G3SourceRef,
    ProjectionCard, ProjectionDelta, ProjectionError,
)

_ID = re.compile(r"[A-Za-z0-9:_-]{1,128}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_LAYERS = frozenset({"fixed", "short_term", "long_term"})
_KINDS = frozenset({"event", "preference", "feeling", "commitment"})
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_PREVIEW_CHARS = 240


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _validate_access(access: AccessScope, scope: str) -> None:
    if (not isinstance(access, AccessScope) or not _valid_id(access.principal_id)
            or not isinstance(access.allowed_scopes, frozenset)
            or not all(_valid_id(s) for s in access.allowed_scopes)):
        raise ProjectionError("projection_access_invalid")
    if scope not in access.allowed_scopes:
        raise ProjectionError("projection_access_denied")


def _validate_event(event: G3Event, access: AccessScope) -> None:
    if not isinstance(event, G3Event) or not isinstance(event.ref, G3SourceRef):
        raise ProjectionError("projection_source_invalid")
    r = event.ref
    if (not all(_valid_id(x) for x in (r.event_id, r.source_id, r.scope))
            or not isinstance(r.source_sha256, str) or not _HEX.fullmatch(r.source_sha256)
            or type(r.revision) is not int or r.revision < 1
            or type(event.cursor) is not int or event.cursor < 1
            or event.status not in ("active", "revoked")
            or event.layer not in _LAYERS or event.fact_kind not in _KINDS
            or type(event.reviewed) is not bool):
        raise ProjectionError("projection_source_invalid")
    try:
        when = datetime.fromisoformat(event.committed_at.replace("Z", "+00:00"))
        if when.tzinfo is None or when.utcoffset() != timezone.utc.utcoffset(when):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ProjectionError("projection_source_invalid") from None
    _validate_access(access, r.scope)
    if event.layer == "fixed" and not event.reviewed:
        raise ProjectionError("projection_unreviewed_anchor")


def _check_head(adapter: G3ReadOnlySourceAdapter, original: G3Event, access: AccessScope) -> None:
    """Prevent silently publishing a revision superseded during materialization."""
    fresh = adapter.read_event(original.ref.event_id, revision=None, access=access)
    _validate_event(fresh, access)
    if fresh != original or fresh.status != "active":
        raise ProjectionError("projection_stale_or_revoked")


def _make_card(event: G3Event, raw: bytes) -> ProjectionCard:
    if not isinstance(raw, bytes) or len(raw) > _MAX_SOURCE_BYTES:
        raise ProjectionError("projection_source_invalid")
    if hashlib.sha256(raw).hexdigest() != event.ref.source_sha256:
        raise ProjectionError("projection_imprint_mismatch")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ProjectionError("projection_source_invalid") from None
    # Only a bounded, exact, unprocessed source excerpt is allowed in v1.
    # LLM-based interpretation is a distinct, not-yet-implemented feature.
    preview = text[:_PREVIEW_CHARS]
    r = event.ref
    identity = json.dumps(
        ["g3.projection_card.v1", r.event_id, r.revision, r.source_id,
         r.source_sha256, r.scope, event.cursor],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    card_id = "pc_" + hashlib.sha256(identity).hexdigest()
    return ProjectionCard(
        projection_card_id=card_id, source_refs=(r,), projection_text=preview,
        layer=event.layer, fact_kind=event.fact_kind,
        origin="g3_verified_excerpt", source_cursor=event.cursor,
        source_committed_at=event.committed_at,
    )


def build_projection_card(
    adapter: G3ReadOnlySourceAdapter, event_id: str, *, access: AccessScope,
    expected_revision: int | None = None,
) -> ProjectionCard:
    """Derive a deterministic card from one CURRENT, authorized G3 revision."""
    if not _valid_id(event_id):
        raise ProjectionError("projection_source_invalid")
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
        raise ProjectionError("projection_source_invalid")
    event = adapter.read_event(event_id, revision=None, access=access)
    _validate_event(event, access)
    if event.ref.event_id != event_id:
        raise ProjectionError("projection_source_invalid")
    if event.status != "active":
        raise ProjectionError("projection_stale_or_revoked")
    if expected_revision is not None and event.ref.revision != expected_revision:
        raise ProjectionError("projection_stale_or_revoked")
    raw = adapter.open_source(event.ref, access=access)
    card = _make_card(event, raw)
    _check_head(adapter, event, access)
    return card


def verify_projection_card(
    adapter: G3ReadOnlySourceAdapter, card: ProjectionCard, *, access: AccessScope,
) -> ProjectionCard:
    """Required at every read boundary. Never serve an unverified cached card."""
    if (not isinstance(card, ProjectionCard) or card.authority != "derived"
            or card.schema_version != "g3.projection_card.v1"
            or card.status != "valid" or card.projection_version != 1
            or len(card.source_refs) != 1):
        raise ProjectionError("projection_card_invalid")
    ref = card.source_refs[0]
    if not isinstance(ref, G3SourceRef):
        raise ProjectionError("projection_card_invalid")
    regenerated = build_projection_card(adapter, ref.event_id, access=access,
                                        expected_revision=ref.revision)
    if regenerated != card:
        raise ProjectionError("projection_card_tampered_or_stale")
    return regenerated


def list_projection_delta(
    adapter: G3ReadOnlySourceAdapter, *, after_cursor: int, access: AccessScope,
) -> ProjectionDelta:
    """Read-only change candidate; evict ALL old cards for changed_event_ids.

    Cursor gaps are permitted (ACL filtering); the production adapter must
    supply a complete, ordered snapshot and an authoritative continuation cursor.
    This offline prototype intentionally does not advertise pagination safety.
    """
    if type(after_cursor) is not int or after_cursor < 0:
        raise ProjectionError("projection_cursor_invalid")
    events = adapter.list_events(after_cursor=after_cursor, access=access)
    if not isinstance(events, tuple):
        raise ProjectionError("projection_feed_invalid")
    last = after_cursor
    seen = set()
    changed = []
    cards = []
    for event in events:
        _validate_event(event, access)
        if event.cursor <= last or event.ref.event_id in seen:
            raise ProjectionError("projection_feed_invalid")
        last = event.cursor
        seen.add(event.ref.event_id)
        changed.append(event.ref.event_id)
        if event.status == "active":
            cards.append(build_projection_card(adapter, event.ref.event_id, access=access,
                                               expected_revision=event.ref.revision))
        else:
            # A revocation is a tombstone for the derived index, not a G3 delete.
            current = adapter.read_event(event.ref.event_id, revision=None, access=access)
            if current != event:
                raise ProjectionError("projection_feed_invalid")
    return ProjectionDelta(tuple(changed), tuple(cards), last)
