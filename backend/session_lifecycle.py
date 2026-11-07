"""GM-02A lifecycle protocol/state machine; disabled, synthetic adapters only.

No live adapter, database path/schema migration, session creation, sending,
model request, route, worker, environment setting, or automatic rollover exists.
Repository and authority MUST share the existing conversation's commit fence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from typing import ContextManager, Protocol

from .g3_projection import _valid_id, _validate_event, build_projection_card
from .g3_projection_contract import AccessScope, G3Event, G3SourceRef, ProjectionCard, ProjectionError
from .g3_projection_feed import _binding
from .web_session_provider_authority import _safe_session_id, WebSessionProviderAuthorityError


class LifecycleError(ValueError):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def _deny(category='lifecycle_invalid'):
    raise LifecycleError(category)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _ids(values, limit=128):
    if (not isinstance(values, tuple) or len(values) > limit
            or not all(_valid_id(v) for v in values) or len(set(values)) != len(values)):
        _deny()


def _session_id(value):
    # Canonical Web IDs allow periods and up to 160 chars; G3 IDs are narrower.
    try:
        _safe_session_id(value)
        return True
    except WebSessionProviderAuthorityError:
        return False


@dataclass(frozen=True)
class SourceCursor:
    generation: str
    position: int


@dataclass(frozen=True)
class SessionSnapshot:
    """Trusted composition of canonical session/version/queue and G3 read state.

    observed cursors are what the old runtime durably consumed; heads may be
    ahead. stamp must cover ALL fields plus G3 source/ACL changes. Not a C proof.
    """
    conversation_id: str
    provider: str
    version_id: str
    window_id: str
    model_run_id: str
    context_epoch: int
    stamp: str
    observed_source_cursor: SourceCursor
    observed_message_cursor: int
    source_head: SourceCursor
    message_head: int
    pending: tuple[str, ...]
    settled: tuple[str, ...]
    anchor_event_ids: tuple[str, ...]
    recent_event_ids: tuple[str, ...]
    streaming: bool = False
    tool_inflight: bool = False
    uncertain_dispatch: bool = False


@dataclass(frozen=True)
class LifecycleDeltaPage:
    stamp: str
    source_after: int
    source_next: int
    message_after: int
    message_next: int
    source_events: tuple[G3Event, ...]
    message_ids: tuple[int, ...]
    done: bool


@dataclass(frozen=True)
class LifecycleRecord:
    revision: int
    conversation_id: str
    provider: str
    version_id: str
    access_binding: str
    window_id: str
    model_run_id: str
    context_epoch: int
    phase: str
    closeout_id: str
    source_cursor: SourceCursor
    message_cursor: int
    pending: tuple[str, ...]
    active_state: tuple[tuple[str, str], ...]
    used_windows: tuple[str, ...]
    used_model_runs: tuple[str, ...]
    closeout_boundary_stamp: str = ''
    closeout_checks: tuple[str, ...] = ()
    closeout_evidence_candidates: tuple[str, ...] = ()
    summary_candidates: tuple[str, ...] = ()
    target_window_id: str | None = None
    anchor_refs: tuple[G3SourceRef, ...] = ()
    recent_refs: tuple[G3SourceRef, ...] = ()
    message_refs: tuple[int, ...] = ()
    packet_sha256: str | None = None
    packet_budget_bytes: int = 32768


@dataclass(frozen=True)
class ReentryPacket:
    conversation_id: str
    provider: str
    version_id: str
    from_window_id: str
    target_window_id: str
    context_epoch: int
    source_cursor: SourceCursor
    message_cursor: int
    pending: tuple[str, ...]
    active_state: tuple[tuple[str, str], ...]
    canonical_message_ids: tuple[int, ...]
    anchors: tuple[ProjectionCard, ...]
    recent: tuple[ProjectionCard, ...]
    summary_candidates: tuple[str, ...] = ()
    authority: str = 'derived'


@dataclass(frozen=True)
class WindowReceipt:
    conversation_id: str
    provider: str
    version_id: str
    from_window_id: str
    target_window_id: str
    model_run_id: str
    context_epoch: int
    packet_sha256: str
    source_cursor: SourceCursor
    message_cursor: int
    status: str


@dataclass(frozen=True)
class LifecycleReceipt:
    request_id: str
    input_sha256: str
    revision_applied: int
    phase_at_apply: str
    window_confirmed: bool = False
    source_changes: int = 0
    new_message_refs: int = 0
    replayed: bool = False
    reply_completed: bool = False


class LifecycleAuthority(Protocol):
    """Real adapter must reuse canonical session authority and its commit fence."""
    def fence(self, conversation_id: str, *, access: AccessScope) -> ContextManager[SessionSnapshot]: ...
    def validate(self, snapshot: SessionSnapshot, *, access: AccessScope) -> bool: ...
    def read_delta(self, snapshot: SessionSnapshot, *, source_after: int, message_after: int,
                   access: AccessScope) -> LifecycleDeltaPage: ...


class LifecycleTransaction(Protocol):
    def load(self) -> LifecycleRecord | None: ...
    def receipt(self, request_id: str) -> LifecycleReceipt | None: ...
    def save(self, expected_revision: int, state: LifecycleRecord, receipt: LifecycleReceipt) -> bool: ...


class LifecycleRepository(Protocol):
    """State + idempotency receipt commit atomically in the SAME canonical DB.

    No second conversation authority. This protocol installs no SQL or migration.
    Exceptions must be normalized by the real repository adapter.
    """
    def transaction(self, conversation_id: str) -> ContextManager[LifecycleTransaction]: ...


class LifecycleRuntime(Protocol):
    """Observation only; never dispatch/retry an external effect here."""
    def read_receipt(self, packet_sha256: str, *, access: AccessScope) -> WindowReceipt | None: ...


class SessionLifecycle:
    def __init__(self, repository: LifecycleRepository, authority: LifecycleAuthority, evidence, runtime: LifecycleRuntime,
                 *, enabled=False, supported_providers=frozenset()):
        self.repository, self.authority, self.evidence, self.runtime = repository, authority, evidence, runtime
        self.enabled, self.supported = enabled is True, supported_providers

    def _snapshot(self, snapshot, cid):
        if (not isinstance(snapshot, SessionSnapshot) or snapshot.conversation_id != cid
                or not all(_session_id(v) for v in (snapshot.conversation_id, snapshot.version_id))
                or not all(_valid_id(v) for v in (snapshot.window_id, snapshot.model_run_id, snapshot.stamp))
                or snapshot.provider not in ('api', 'codex')
                or type(snapshot.context_epoch) is not int or snapshot.context_epoch < 0
                or type(snapshot.message_head) is not int or type(snapshot.observed_message_cursor) is not int
                or not 0 <= snapshot.observed_message_cursor <= snapshot.message_head):
            _deny()
        for cursor in (snapshot.source_head, snapshot.observed_source_cursor):
            if (not isinstance(cursor, SourceCursor) or not _valid_id(cursor.generation)
                    or type(cursor.position) is not int or cursor.position < 0):
                _deny()
        if (snapshot.source_head.generation != snapshot.observed_source_cursor.generation
                or snapshot.observed_source_cursor.position > snapshot.source_head.position):
            _deny('lifecycle_cursor_invalid')
        for field in ('streaming', 'tool_inflight', 'uncertain_dispatch'):
            if type(getattr(snapshot, field)) is not bool:
                _deny()
            if getattr(snapshot, field):
                _deny('lifecycle_unsafe_boundary')
        for values, limit in ((snapshot.pending, 128), (snapshot.settled, 128),
                              (snapshot.anchor_event_ids, 8), (snapshot.recent_event_ids, 16)):
            _ids(values, limit)
        if set(snapshot.pending) & set(snapshot.settled) or set(snapshot.anchor_event_ids) & set(snapshot.recent_event_ids):
            _deny()
        if snapshot.provider not in self.supported:
            _deny('lifecycle_provider_unsupported')

    @staticmethod
    def _bound(state, snapshot, binding):
        if state is None:
            return
        if (not isinstance(state, LifecycleRecord) or state.access_binding != binding
                or state.conversation_id != snapshot.conversation_id):
            _deny('lifecycle_access_denied')
        if (type(state.revision) is not int or state.revision < 1
                or type(state.context_epoch) is not int or state.context_epoch < 0
                or type(state.message_cursor) is not int or state.message_cursor < 0
                or state.phase not in ('closed', 'prepared', 'active')
                or not isinstance(state.source_cursor, SourceCursor)
                or not _valid_id(state.source_cursor.generation)
                or type(state.source_cursor.position) is not int or state.source_cursor.position < 0
                or not all(_valid_id(v) for v in (state.window_id, state.model_run_id, state.closeout_id))
                or not _valid_id(state.closeout_boundary_stamp)
                or state.closeout_checks != ('stream_idle', 'tool_idle', 'dispatch_known')
                or type(state.packet_budget_bytes) is not int or not 1 <= state.packet_budget_bytes <= 65536
                or (state.phase == 'prepared' and not _valid_id(state.target_window_id))
                or (state.phase != 'prepared' and state.target_window_id is not None)):
            _deny('lifecycle_state_invalid')
        for values in (state.pending, state.used_windows, state.used_model_runs):
            _ids(values)
        if state.window_id not in state.used_windows or state.model_run_id not in state.used_model_runs:
            _deny('lifecycle_state_invalid')
        current_window = (snapshot.window_id, snapshot.model_run_id, snapshot.context_epoch)
        if current_window != (state.window_id, state.model_run_id, state.context_epoch):
            if (state.phase != 'prepared' or snapshot.window_id != state.target_window_id
                    or snapshot.context_epoch != state.context_epoch + 1
                    or snapshot.model_run_id in state.used_model_runs):
                _deny('lifecycle_window_changed')
        if state.provider != snapshot.provider or state.version_id != snapshot.version_id:
            _deny('lifecycle_session_changed')
        if (state.source_cursor.generation != snapshot.source_head.generation
                or state.source_cursor.position > snapshot.source_head.position
                or state.message_cursor > snapshot.message_head):
            _deny('lifecycle_cursor_invalid')
        if not set(state.pending) <= set(snapshot.pending) | set(snapshot.settled):
            _deny('lifecycle_pending_unaccounted')

    def _run(self, cid, rid, op, payload, access, handler, *, transaction_guard=None):
        if not self.enabled:
            _deny('lifecycle_disabled')
        if not _session_id(cid) or not _valid_id(rid):
            _deny()
        binding = _binding(access)
        digest = hashlib.sha256(_json([op, cid, binding, payload])).hexdigest()
        try:
            with self.authority.fence(cid, access=access) as snapshot:
                self._snapshot(snapshot, cid)
                with self.repository.transaction(cid) as tx:
                    state = tx.load()
                    self._bound(state, snapshot, binding)
                    if transaction_guard is not None:
                        transaction_guard(tx, state, snapshot, binding)
                    old = tx.receipt(rid)
                    if old is not None:
                        if (not isinstance(old, LifecycleReceipt) or old.request_id != rid
                                or type(old.revision_applied) is not int or state is None
                                or not 1 <= old.revision_applied <= state.revision
                                or old.phase_at_apply not in ('closed', 'prepared', 'active')
                                or type(old.window_confirmed) is not bool or old.reply_completed is not False):
                            _deny('lifecycle_state_invalid')
                        if old.input_sha256 != digest:
                            _deny('lifecycle_request_conflict')
                        if self.authority.validate(snapshot, access=access) is not True:
                            _deny('lifecycle_authority_changed')
                        return replace(old, replayed=True)
                    updated, confirmed, counts = handler(state, snapshot, binding)
                    revision = (state.revision if state else 0) + 1
                    updated = replace(updated, revision=revision)
                    receipt = LifecycleReceipt(rid, digest, revision, updated.phase, confirmed, *counts)
                    if transaction_guard is not None:
                        transaction_guard(tx, state, snapshot, binding)
                    if self.authority.validate(snapshot, access=access) is not True:
                        _deny('lifecycle_authority_changed')
                    if not tx.save(revision - 1, updated, receipt):
                        _deny('lifecycle_commit_conflict')
                return receipt
        except ProjectionError:
            raise LifecycleError('lifecycle_evidence_unavailable') from None
        except OSError:
            # Unknown commit completion is reconciled by the SAME request ID.
            raise LifecycleError('lifecycle_storage_unavailable') from None

    def closeout(self, cid, request_id, *, window_id, context_epoch, active_state=(), summary_candidates=(), access):
        if (not _valid_id(window_id) or type(context_epoch) is not int or context_epoch < 0
                or not isinstance(active_state, tuple) or len(active_state) > 16):
            _deny()
        for item in active_state:
            if (not isinstance(item, tuple) or len(item) != 2 or not _valid_id(item[0])
                    or not isinstance(item[1], str) or len(item[1]) > 1024):
                _deny()
        if len({item[0] for item in active_state}) != len(active_state):
            _deny()
        if (not isinstance(summary_candidates, tuple) or len(summary_candidates) > 8
                or any(not isinstance(text, str) or len(text) > 2048 for text in summary_candidates)):
            _deny()
        def apply(state, snapshot, binding):
            if state is not None and state.phase != 'active':
                _deny('lifecycle_phase_conflict')
            if (snapshot.window_id != window_id or snapshot.context_epoch != context_epoch
                    or state is not None and (state.window_id != window_id or state.context_epoch != context_epoch
                                              or state.model_run_id != snapshot.model_run_id)):
                _deny('lifecycle_window_changed')
            if state and (snapshot.observed_source_cursor.position < state.source_cursor.position
                          or snapshot.observed_message_cursor < state.message_cursor):
                _deny('lifecycle_cursor_invalid')
            record = LifecycleRecord(0, cid, snapshot.provider, snapshot.version_id, binding,
                    window_id, snapshot.model_run_id, context_epoch, 'closed', request_id,
                    snapshot.observed_source_cursor, snapshot.observed_message_cursor, snapshot.pending,
                    active_state, state.used_windows if state else (window_id,),
                    state.used_model_runs if state else (snapshot.model_run_id,),
                    closeout_boundary_stamp=snapshot.stamp,
                    closeout_checks=('stream_idle', 'tool_idle', 'dispatch_known'),
                    closeout_evidence_candidates=snapshot.anchor_event_ids + snapshot.recent_event_ids,
                    summary_candidates=summary_candidates)
            return record, False, (0, 0)
        return self._run(cid, request_id, 'closeout', [window_id, context_epoch, active_state, summary_candidates], access, apply)

    def _catch_up(self, state, snapshot, access):
        source, message = state.source_cursor.position, state.message_cursor
        seen, revisions, message_refs = set(), {}, []
        source_count = 0
        for _ in range(100):
            page = self.authority.read_delta(snapshot, source_after=source, message_after=message, access=access)
            if (not isinstance(page, LifecycleDeltaPage) or page.stamp != snapshot.stamp
                    or any(type(x) is not int for x in (page.source_after, page.source_next, page.message_after, page.message_next))
                    or (page.source_after, page.message_after) != (source, message)
                    or not source <= page.source_next <= snapshot.source_head.position
                    or not message <= page.message_next <= snapshot.message_head
                    or type(page.done) is not bool or not isinstance(page.source_events, tuple)
                    or not isinstance(page.message_ids, tuple)
                    or len(page.source_events) > 100 or len(page.message_ids) > 100):
                _deny('lifecycle_delta_invalid')
            terminal = (page.source_next, page.message_next) == (snapshot.source_head.position, snapshot.message_head)
            if page.done != terminal or not page.done and (page.source_next, page.message_next) == (source, message):
                _deny('lifecycle_delta_invalid')
            for event in page.source_events:
                _validate_event(event, access)
                key = (event.ref.event_id, event.ref.revision)
                if (not source < event.cursor <= page.source_next or key in seen
                        or event.ref.revision <= revisions.get(event.ref.event_id, 0)):
                    _deny('lifecycle_delta_invalid')
                seen.add(key); revisions[event.ref.event_id] = event.ref.revision
                source = event.cursor; source_count += 1
            for mid in page.message_ids:
                if type(mid) is not int or not message < mid <= page.message_next:
                    _deny('lifecycle_delta_invalid')
                message_refs.append(mid); message = mid
            source, message = page.source_next, page.message_next
            if source_count > 1000 or len(message_refs) + len(state.message_refs) > 128:
                _deny('lifecycle_budget_exceeded')
            if page.done:
                return source_count, tuple(message_refs)
        _deny('lifecycle_budget_exceeded')

    def _cards(self, snapshot, access):
        anchors = tuple(build_projection_card(self.evidence, eid, access=access) for eid in snapshot.anchor_event_ids)
        recent = tuple(build_projection_card(self.evidence, eid, access=access) for eid in snapshot.recent_event_ids)
        if (any(card.layer != 'fixed' for card in anchors)
                or any(card.source_cursor > snapshot.source_head.position for card in anchors + recent)):
            _deny('lifecycle_evidence_invalid')
        return anchors, recent

    def _packet(self, state, snapshot, access):
        anchors, recent = self._cards(snapshot, access)
        if (tuple(c.source_refs[0] for c in anchors) != state.anchor_refs
                or tuple(c.source_refs[0] for c in recent) != state.recent_refs):
            _deny('lifecycle_delta_required')
        packet = ReentryPacket(state.conversation_id, state.provider, state.version_id, state.window_id,
                state.target_window_id, state.context_epoch + 1, state.source_cursor, state.message_cursor,
                state.pending, state.active_state, state.message_refs, anchors, recent)
        packet = replace(packet, summary_candidates=state.summary_candidates)
        raw = _json(asdict(packet))
        if len(raw) > state.packet_budget_bytes:
            _deny('lifecycle_budget_exceeded')
        return packet, hashlib.sha256(raw).hexdigest()

    def _prepare(self, state, snapshot, access, target, budget):
        changes, mids = self._catch_up(state, snapshot, access)
        anchors, recent = self._cards(snapshot, access)
        updated = replace(state, phase='prepared', target_window_id=target, source_cursor=snapshot.source_head,
                message_cursor=snapshot.message_head, pending=snapshot.pending,
                anchor_refs=tuple(c.source_refs[0] for c in anchors), recent_refs=tuple(c.source_refs[0] for c in recent),
                message_refs=state.message_refs + mids, packet_budget_bytes=budget)
        _, digest = self._packet(updated, snapshot, access)
        return replace(updated, packet_sha256=digest), False, (changes, len(mids))

    def reentry(self, cid, request_id, *, closeout_id, target_window_id, packet_budget_bytes=32768, access):
        if (not _valid_id(closeout_id) or not _valid_id(target_window_id)
                or type(packet_budget_bytes) is not int or not 1 <= packet_budget_bytes <= 65536):
            _deny()
        def apply(state, snapshot, _):
            if state is None or state.phase != 'closed' or state.closeout_id != closeout_id:
                _deny('lifecycle_phase_conflict')
            if target_window_id in state.used_windows or len(state.used_windows) >= 128:
                _deny('lifecycle_window_reused')
            return self._prepare(state, snapshot, access, target_window_id, packet_budget_bytes)
        return self._run(cid, request_id, 'reentry', [closeout_id, target_window_id, packet_budget_bytes], access, apply)

    def reentry_delta(self, cid, request_id, *, after_source: SourceCursor, after_message: int, access):
        if (not isinstance(after_source, SourceCursor) or not _valid_id(after_source.generation)
                or type(after_source.position) is not int or after_source.position < 0
                or type(after_message) is not int or after_message < 0):
            _deny()
        def apply(state, snapshot, _):
            if state is None or state.phase != 'prepared':
                _deny('lifecycle_phase_conflict')
            if (state.source_cursor, state.message_cursor) != (after_source, after_message):
                _deny('lifecycle_delta_cursor_conflict')
            return self._prepare(state, snapshot, access, state.target_window_id, state.packet_budget_bytes)
        return self._run(cid, request_id, 'delta', [asdict(after_source), after_message], access, apply)

    @staticmethod
    def _current(state, snapshot):
        if state.phase != 'prepared':
            _deny('lifecycle_phase_conflict')
        if (state.source_cursor != snapshot.source_head or state.message_cursor != snapshot.message_head
                or state.pending != snapshot.pending):
            _deny('lifecycle_delta_required')

    def packet(self, cid, *, access):
        if not self.enabled:
            _deny('lifecycle_disabled')
        if not _session_id(cid):
            _deny()
        binding = _binding(access)
        try:
            with self.authority.fence(cid, access=access) as snapshot:
                self._snapshot(snapshot, cid)
                with self.repository.transaction(cid) as tx:
                    state = tx.load()
                    if state is None:
                        _deny('lifecycle_phase_conflict')
                    self._bound(state, snapshot, binding); self._current(state, snapshot)
                    packet, digest = self._packet(state, snapshot, access)
                    if digest != state.packet_sha256:
                        _deny('lifecycle_packet_changed')
                    if self.authority.validate(snapshot, access=access) is not True:
                        _deny('lifecycle_authority_changed')
                    return packet
        except ProjectionError:
            raise LifecycleError('lifecycle_evidence_unavailable') from None

    def activate(self, cid, request_id, *, packet_sha256, access,
                 _attempt_guard=None, _attempt_request_id=None):
        if not isinstance(packet_sha256, str) or len(packet_sha256) != 64:
            _deny()
        def apply(state, snapshot, _):
            if state is None:
                _deny('lifecycle_phase_conflict')
            self._current(state, snapshot)
            _, digest = self._packet(state, snapshot, access)
            if digest != state.packet_sha256 or packet_sha256 != digest:
                _deny('lifecycle_packet_changed')
            ack = self.runtime.read_receipt(digest, access=access)
            if not isinstance(ack, WindowReceipt) or ack.status == 'unknown':
                _deny('lifecycle_confirmation_unknown')
            if ack.status != 'confirmed':
                _deny('lifecycle_confirmation_failed')
            expected = WindowReceipt(cid, state.provider, state.version_id, state.window_id, state.target_window_id,
                    ack.model_run_id, state.context_epoch + 1, digest, state.source_cursor, state.message_cursor, 'confirmed')
            if (ack != expected or not _valid_id(ack.model_run_id) or ack.model_run_id in state.used_model_runs
                    or type(ack.context_epoch) is not int or type(ack.message_cursor) is not int
                    or not isinstance(ack.source_cursor, SourceCursor)
                    or type(ack.source_cursor.position) is not int
                    or (snapshot.window_id, snapshot.model_run_id, snapshot.context_epoch)
                       != (ack.target_window_id, ack.model_run_id, ack.context_epoch)
                    or snapshot.observed_source_cursor != ack.source_cursor
                    or snapshot.observed_message_cursor != ack.message_cursor):
                _deny('lifecycle_confirmation_mismatch')
            return replace(state, phase='active', window_id=state.target_window_id, model_run_id=ack.model_run_id,
                    target_window_id=None, context_epoch=state.context_epoch + 1,
                    used_windows=state.used_windows + (state.target_window_id,),
                    used_model_runs=state.used_model_runs + (ack.model_run_id,)), True, (0, 0)
        op = 'activate_recorded' if _attempt_guard is not None else 'activate'
        payload = [packet_sha256, _attempt_request_id] if _attempt_guard is not None else packet_sha256
        return self._run(cid, request_id, op, payload, access, apply,
                         transaction_guard=_attempt_guard)
