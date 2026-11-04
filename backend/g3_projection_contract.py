"""GM-01A: strictly read-only G3 authority and derived ProjectionCard contract.

No production adapter, network client, storage connector, model, or route is installed.
A future authenticated adapter MUST derive AccessScope server-side, not from a
browser-supplied scope. This interface has no mutation methods by design.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


class ProjectionError(ValueError):
    """Stable denial category; never include raw G3 bytes or private IDs in text."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


Layer = Literal["fixed", "short_term", "long_term"]
FactKind = Literal["event", "preference", "feeling", "commitment"]


@dataclass(frozen=True)
class AccessScope:
    """Trusted request context; MUST be created by a server-side authority."""

    principal_id: str
    allowed_scopes: frozenset[str]


@dataclass(frozen=True)
class G3SourceRef:
    event_id: str
    revision: int
    source_id: str
    source_sha256: str
    scope: str


@dataclass(frozen=True)
class G3Event:
    """A view of the current/selected authority revision, not a projection."""

    ref: G3SourceRef
    cursor: int
    committed_at: str
    status: Literal["active", "revoked"]
    layer: Layer
    fact_kind: FactKind
    reviewed: bool = False


@dataclass(frozen=True)
class G3Revocation:
    event_id: str
    revision: int
    cursor: int


class G3ReadOnlySourceAdapter(Protocol):
    """Adapter methods are reads; real implementation is out of GM-01A scope."""

    def list_events(self, *, after_cursor: int, access: AccessScope) -> tuple[G3Event, ...]: ...

    def read_event(self, event_id: str, *, revision: int | None, access: AccessScope) -> G3Event: ...

    def open_source(self, ref: G3SourceRef, *, access: AccessScope) -> bytes: ...

    def read_revocations(self, *, after_cursor: int, access: AccessScope) -> tuple[G3Revocation, ...]: ...


@dataclass(frozen=True)
class ProjectionCard:
    projection_card_id: str
    source_refs: tuple[G3SourceRef, ...]
    projection_text: str
    layer: Layer
    fact_kind: FactKind
    origin: Literal["g3_verified_excerpt"]
    source_cursor: int
    source_committed_at: str
    projection_version: int = 1
    schema_version: Literal["g3.projection_card.v1"] = "g3.projection_card.v1"
    authority: Literal["derived"] = "derived"
    status: Literal["valid"] = "valid"
    confidence: float | None = None
    valence: float | None = None
    arousal: float | None = None
    importance: float | None = None
    retrieval_weight: float | None = None


@dataclass(frozen=True)
class ProjectionDelta:
    """Read-only candidate feed; caller may evict by event ID before upsert."""

    changed_event_ids: tuple[str, ...]
    valid_cards: tuple[ProjectionCard, ...]
    next_cursor: int
    authority: Literal["derived"] = "derived"
