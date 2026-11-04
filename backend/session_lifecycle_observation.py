"""GM-02C read-only store observations, NOT a lifecycle runtime authority.

No job recovery, schema initialization, dispatch, model calls or confirmation.
Independent Codex/relay transactions are point observations, not an atomic view.
Database quietness never proves stream/tool completion or consumed cursors.
"""
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .session_lifecycle import LifecycleError
from .session_lifecycle_storage import _selected, _source_schema
from .codex_generation_store import JOB_STATUSES, ACTIVE_JOB_STATUSES


def readonly_factory(path):
    """Explicit existing store only; mode=ro cannot create a missing database."""
    path = Path(path)
    def connect():
        if not path.is_absolute() or '..' in path.parts or path.resolve() != path:
            raise LifecycleError('observation_path_invalid')
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None, timeout=5)
        conn.execute('PRAGMA foreign_keys=ON')
        return conn
    return connect


@dataclass(frozen=True)
class StoreObservation:
    conversation_id: str
    version_id: str
    provider: str
    job_counts: tuple[tuple[str, int], ...]
    active_jobs: int
    explicit_uncertain_jobs: int
    session_available: bool
    native_thread_present: bool | None
    compaction_pending: int | None
    stores_atomic: bool = False
    snapshot_complete: bool = False
    handoff_supported: bool = False
    runtime_idle: None = None
    window_id: None = None
    context_epoch: None = None
    consumed_source_cursor: None = None
    consumed_message_cursor: None = None
    stream_active: None = None
    tool_inflight: None = None
    confirmed_receipt: None = None


class RuntimeStoreObserver:
    production_supported = False

    def __init__(self, connect_relay, config_reader, *, connect_codex=None, coordinator=None, enabled=False):
        self._relay, self._reader, self._codex = connect_relay, config_reader, connect_codex
        self._coordinator, self.enabled = coordinator, enabled is True

    @contextmanager
    def _read(self, factory):
        with closing(factory()) as conn:
            if conn.in_transaction or conn.isolation_level is not None:
                raise LifecycleError('observation_connection_invalid')
            # Defense in depth even if a caller accidentally supplies an RW factory.
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            try: yield conn
            finally: conn.rollback()

    @staticmethod
    def _table(conn, name, *, optional=False):
        row = conn.execute('SELECT type FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if not row and optional: return False
        if not row or row[0] != 'table': raise LifecycleError('observation_schema_unavailable')
        return True

    def observe(self, conversation_id):
        if not self.enabled: raise LifecycleError('lifecycle_disabled')
        try:
            with self._coordinator.fence() if self._coordinator is not None else nullcontext():
                view = self._reader.capture(conversation_id)
                with self._read(self._relay) as conn:
                    _source_schema(conn)
                    version = _selected(conn, view)
                    if view.provider == 'api':
                        self._table(conn, 'api_web_generations'); self._table(conn, 'api_web_closed_sessions')
                        rows = conn.execute('SELECT status,upstream_unknown,count(*) FROM api_web_generations WHERE session_id=? GROUP BY status,upstream_unknown', (version,)).fetchall()
                        counts, uncertain = {}, 0
                        for status, unknown, count in rows:
                            if status not in {'queued','running','stopping','completed','interrupted','failed'} or unknown not in (0, 1):
                                raise LifecycleError('observation_schema_unavailable')
                            counts[status] = counts.get(status, 0) + count
                            if unknown: uncertain += count
                        available = not bool(conn.execute('SELECT 1 FROM api_web_closed_sessions WHERE session_id=?', (version,)).fetchone())
                        result = StoreObservation(conversation_id, version, 'api', tuple(sorted(counts.items())),
                            sum(count for status, count in counts.items() if status in {'queued','running','stopping'}),
                            uncertain, available, None, None)
                    elif view.provider != 'codex':
                        raise LifecycleError('observation_provider_unsupported')
                if view.provider == 'codex':
                    if self._codex is None: raise LifecycleError('observation_codex_store_required')
                    with self._read(self._codex) as conn:
                        for table in ('codex_generation_schema','codex_sessions','codex_generation_jobs'): self._table(conn, table)
                        if [tuple(row) for row in conn.execute('SELECT version FROM codex_generation_schema')] != [(1,)]:
                            raise LifecycleError('observation_schema_unavailable')
                        pin = conn.execute('SELECT provider,status,thread_id FROM codex_sessions WHERE api_session=?', (version,)).fetchone()
                        if not pin or pin[0] != 'codex' or pin[1] not in ('active', 'retired'):
                            raise LifecycleError('observation_session_unavailable')
                        counts = dict(conn.execute('SELECT status,count(*) FROM codex_generation_jobs WHERE api_session=? GROUP BY status', (version,)).fetchall())
                        if set(counts) - JOB_STATUSES: raise LifecycleError('observation_schema_unavailable')
                        pending = None
                        if self._table(conn, 'web_codex_compactions', optional=True):
                            pending = conn.execute("SELECT count(*) FROM web_codex_compactions WHERE session_id=? AND status NOT IN ('completed','failed')", (version,)).fetchone()[0]
                            if conn.execute('SELECT 1 FROM web_codex_compactions WHERE session_id=? AND status IS NULL LIMIT 1', (version,)).fetchone():
                                raise LifecycleError('observation_schema_unavailable')
                        result = StoreObservation(conversation_id, version, 'codex', tuple(sorted(counts.items())),
                            sum(count for status, count in counts.items() if status in ACTIVE_JOB_STATUSES),
                            counts.get('dispatch_uncertain', 0), pin[1] == 'active', bool(pin[2]), pending)
                if not self._reader.is_current(view): raise LifecycleError('lifecycle_authority_changed')
                return result
        except (OSError, sqlite3.Error):
            raise LifecycleError('observation_store_unavailable') from None
