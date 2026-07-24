"""Persistent Telegram mappings, jobs, completion identities, and outboxes."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str) -> sqlite3.Connection:
    try:
        timeout = float(os.environ.get("SQLITE_BUSY_TIMEOUT_SECONDS", "30"))
    except (TypeError, ValueError):
        timeout = 30.0
    timeout = timeout if timeout > 0 else 30.0
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {max(1, int(timeout * 1000))}")
    return conn


SCHEMA_MIGRATIONS_DDL = """CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)"""

RELAY_TABLE_DDL: dict[str, str] = {
    "messages": """CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            direction TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            meta TEXT NOT NULL DEFAULT '{}')""",
    "push_subscriptions": """CREATE TABLE push_subscriptions (
            endpoint TEXT PRIMARY KEY,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            ua TEXT,
            created TEXT NOT NULL,
            last_ok TEXT)""",
}

CORE_V1_TABLE_DDL: dict[str, str] = {
    "channel_accounts": """CREATE TABLE channel_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(channel, external_account_id))""",
    "channel_conversations": """CREATE TABLE channel_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, external_conversation_id TEXT NOT NULL,
            conversation_type TEXT NOT NULL, api_session TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(channel, external_account_id, external_conversation_id))""",
    "inbound_events": """CREATE TABLE inbound_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, update_id TEXT NOT NULL, event_type TEXT NOT NULL,
            status TEXT NOT NULL, error_category TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(channel, external_account_id, update_id))""",
    "external_messages": """CREATE TABLE external_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, external_conversation_id TEXT NOT NULL,
            external_message_id TEXT NOT NULL, direction TEXT NOT NULL,
            canonical_message_id INTEGER, generation_id TEXT, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(channel, external_account_id, external_conversation_id, external_message_id))""",
    "generation_jobs": """CREATE TABLE generation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, inbound_message_id INTEGER NOT NULL UNIQUE,
            canonical_message_id INTEGER NOT NULL, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, external_conversation_id TEXT NOT NULL,
            api_session TEXT NOT NULL, stream_id TEXT, generation_id TEXT UNIQUE,
            reply_to TEXT, reply_message_id INTEGER, status TEXT NOT NULL, lease_until TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0, error_category TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(inbound_message_id) REFERENCES external_messages(id))""",
    "delivery_attempts": """CREATE TABLE delivery_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, generation_job_id INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, external_conversation_id TEXT NOT NULL,
            payload_text TEXT NOT NULL, status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
            external_message_id TEXT, error_category TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(generation_job_id) REFERENCES generation_jobs(id))""",
    "channel_audit_events": """CREATE TABLE channel_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, channel TEXT NOT NULL,
            external_id_hash TEXT, request_job_id TEXT, status TEXT NOT NULL, error_category TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "channel_rate_limits": """CREATE TABLE channel_rate_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            external_account_id TEXT NOT NULL, external_user_id TEXT NOT NULL,
            window_started_at TEXT NOT NULL, event_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(channel, external_account_id, external_user_id))""",
}

CORE_V1_INDEX_DDL: dict[str, str] = {
    "idx_generation_jobs_status_lease":
        "CREATE INDEX idx_generation_jobs_status_lease ON generation_jobs(status, lease_until, id)",
    "idx_delivery_attempts_status":
        "CREATE INDEX idx_delivery_attempts_status ON delivery_attempts(status, id)",
}

CORE_V2_TABLE_DDL: dict[str, str] = {
    "telegram_completions": """CREATE TABLE telegram_completions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        completion_identity TEXT NOT NULL UNIQUE,
        generation_job_id INTEGER NOT NULL UNIQUE,
        canonical_message_id INTEGER NOT NULL UNIQUE,
        delivery_id INTEGER NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        FOREIGN KEY(generation_job_id) REFERENCES generation_jobs(id),
        FOREIGN KEY(delivery_id) REFERENCES delivery_attempts(id))""",
    "delivery_parts": """CREATE TABLE delivery_parts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        delivery_id INTEGER NOT NULL,
        part_index INTEGER NOT NULL,
        total_parts INTEGER NOT NULL,
        text_hash TEXT NOT NULL,
        text_length INTEGER NOT NULL,
        payload_text TEXT NOT NULL,
        status TEXT NOT NULL,
        telegram_message_id TEXT,
        error_category TEXT,
        retry_after_seconds INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(delivery_id, part_index),
        FOREIGN KEY(delivery_id) REFERENCES delivery_attempts(id))""",
}

CORE_V2_INDEX_DDL: dict[str, str] = {
    "idx_delivery_parts_status":
        "CREATE INDEX idx_delivery_parts_status ON delivery_parts(delivery_id,status,part_index)",
}


def _migration_001(conn: sqlite3.Connection) -> None:
    for statement in (*CORE_V1_TABLE_DDL.values(), *CORE_V1_INDEX_DDL.values()):
        conn.execute(statement)


def _migration_002(conn: sqlite3.Connection) -> None:
    # v1 is immutable. New state-machine columns and tables live in v2.
    conn.execute("ALTER TABLE generation_jobs ADD COLUMN dispatch_started_at TEXT")
    conn.execute("ALTER TABLE generation_jobs ADD COLUMN awaiting_reply_since TEXT")
    conn.execute("ALTER TABLE delivery_attempts ADD COLUMN retry_after_seconds INTEGER")
    conn.execute("ALTER TABLE channel_conversations ADD COLUMN external_user_id TEXT")
    for statement in (*CORE_V2_TABLE_DDL.values(), *CORE_V2_INDEX_DDL.values()):
        conn.execute(statement)


KELIVO_V3_TABLE_DDL: dict[str, str] = {
    "kelivo_clients": """CREATE TABLE kelivo_clients (
            client_id TEXT PRIMARY KEY,
            api_session TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            mapping_revision INTEGER NOT NULL DEFAULT 1 CHECK(mapping_revision > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""",
    "kelivo_requests": """CREATE TABLE kelivo_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL,
            request_payload_hash TEXT NOT NULL,
            request_identity_hash TEXT NOT NULL,
            client_id TEXT NOT NULL,
            api_session TEXT NOT NULL,
            mapping_revision INTEGER NOT NULL CHECK(mapping_revision > 0),
            history_before_id INTEGER NOT NULL CHECK(history_before_id >= 0),
            context_bundle_json TEXT NOT NULL,
            context_bundle_hash TEXT NOT NULL,
            provider_messages_json TEXT NOT NULL,
            prompt_contract_version TEXT NOT NULL,
            persona_hash TEXT NOT NULL,
            persona_source TEXT NOT NULL,
            provider_model TEXT NOT NULL,
            effective_temperature REAL NOT NULL CHECK(effective_temperature >= 0 AND effective_temperature <= 2),
            effective_max_tokens INTEGER NOT NULL CHECK(effective_max_tokens >= 1 AND effective_max_tokens <= 32768),
            status TEXT NOT NULL CHECK(status IN
                ('prepared','dispatching','dispatch_uncertain','failed','completed')),
            dispatch_expires_at TEXT,
            generation_id TEXT NOT NULL UNIQUE,
            user_message_id INTEGER,
            assistant_message_id INTEGER,
            response_json TEXT,
            error_category TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(client_id,idempotency_key),
            FOREIGN KEY(client_id) REFERENCES kelivo_clients(client_id),
            FOREIGN KEY(user_message_id) REFERENCES messages(id),
            FOREIGN KEY(assistant_message_id) REFERENCES messages(id))""",
    "companion_context_snapshots": """CREATE TABLE companion_context_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_session TEXT NOT NULL,
            snapshot_type TEXT NOT NULL,
            normalized_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            version INTEGER NOT NULL CHECK(version > 0),
            created_at TEXT NOT NULL,
            UNIQUE(api_session, snapshot_type, content_hash),
            UNIQUE(api_session, snapshot_type, version))""",
    "kelivo_rate_limits": """CREATE TABLE kelivo_rate_limits (
            client_id TEXT NOT NULL,
            window_started_at INTEGER NOT NULL CHECK(window_started_at >= 0),
            request_count INTEGER NOT NULL CHECK(request_count > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(client_id,window_started_at),
            FOREIGN KEY(client_id) REFERENCES kelivo_clients(client_id))""",
}

KELIVO_V3_INDEX_DDL: dict[str, str] = {
    "idx_kelivo_requests_status":
        "CREATE INDEX idx_kelivo_requests_status ON kelivo_requests(status,dispatch_expires_at,id)",
    "idx_kelivo_rate_limits_window":
        "CREATE INDEX idx_kelivo_rate_limits_window ON kelivo_rate_limits(window_started_at,client_id)",
    "idx_context_snapshots_lookup":
        "CREATE INDEX idx_context_snapshots_lookup ON companion_context_snapshots(api_session,snapshot_type,active,version)",
    "idx_context_snapshots_one_active":
        "CREATE UNIQUE INDEX idx_context_snapshots_one_active ON companion_context_snapshots(api_session,snapshot_type) WHERE active=1",
}


def _migration_003(conn: sqlite3.Connection) -> None:
    """Kelivo client mapping, idempotent requests, and normalized context."""
    for statement in (*KELIVO_V3_TABLE_DDL.values(), *KELIVO_V3_INDEX_DDL.values()):
        conn.execute(statement)


KELIVO_TABLE_DDL: dict[str, str] = {
    **KELIVO_V3_TABLE_DDL,
    "kelivo_requests": """CREATE TABLE kelivo_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL,
            idempotency_mode TEXT NOT NULL DEFAULT 'explicit'
                CHECK(idempotency_mode IN ('explicit','automatic')),
            automatic_fingerprint TEXT,
            automatic_replay_until TEXT,
            request_payload_hash TEXT NOT NULL,
            request_identity_hash TEXT NOT NULL,
            client_id TEXT NOT NULL,
            api_session TEXT NOT NULL,
            mapping_revision INTEGER NOT NULL CHECK(mapping_revision > 0),
            history_before_id INTEGER NOT NULL CHECK(history_before_id >= 0),
            context_bundle_json TEXT NOT NULL,
            context_bundle_hash TEXT NOT NULL,
            provider_messages_json TEXT NOT NULL,
            prompt_contract_version TEXT NOT NULL,
            persona_hash TEXT NOT NULL,
            persona_source TEXT NOT NULL,
            provider_model TEXT NOT NULL,
            effective_temperature REAL NOT NULL CHECK(effective_temperature >= 0 AND effective_temperature <= 2),
            effective_max_tokens INTEGER NOT NULL CHECK(effective_max_tokens >= 1 AND effective_max_tokens <= 32768),
            status TEXT NOT NULL CHECK(status IN
                ('prepared','dispatching','dispatch_uncertain','failed','completed')),
            dispatch_expires_at TEXT,
            generation_id TEXT NOT NULL UNIQUE,
            user_message_id INTEGER,
            assistant_message_id INTEGER,
            response_json TEXT,
            error_category TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(client_id,idempotency_key),
            CHECK(
                (idempotency_mode='explicit' AND automatic_fingerprint IS NULL AND automatic_replay_until IS NULL)
                OR
                (idempotency_mode='automatic' AND automatic_fingerprint IS NOT NULL
                 AND length(automatic_fingerprint)=64
                 AND automatic_fingerprint NOT GLOB '*[^0-9a-f]*'
                 AND automatic_replay_until IS NOT NULL AND length(automatic_replay_until)>0)
            ),
            FOREIGN KEY(client_id) REFERENCES kelivo_clients(client_id),
            FOREIGN KEY(user_message_id) REFERENCES messages(id),
            FOREIGN KEY(assistant_message_id) REFERENCES messages(id))""",
}

KELIVO_INDEX_DDL: dict[str, str] = {
    **KELIVO_V3_INDEX_DDL,
    "idx_kelivo_requests_automatic":
        "CREATE INDEX idx_kelivo_requests_automatic ON kelivo_requests(client_id,automatic_fingerprint,created_at,status)",
}


def _migration_004(conn: sqlite3.Connection) -> None:
    """Add bounded automatic idempotency metadata without touching Telegram tables."""
    validate_kelivo_schema(conn, version=3)
    conn.execute("DROP INDEX idx_kelivo_requests_status")
    conn.execute("ALTER TABLE kelivo_requests RENAME TO kelivo_requests_v3")
    conn.execute(KELIVO_TABLE_DDL["kelivo_requests"])
    if conn.execute("SELECT EXISTS(SELECT 1 FROM kelivo_requests_v3)").fetchone()[0]:
        conn.execute("""INSERT INTO kelivo_requests
            (id,idempotency_key,idempotency_mode,automatic_fingerprint,automatic_replay_until,
             request_payload_hash,request_identity_hash,client_id,api_session,mapping_revision,
             history_before_id,context_bundle_json,context_bundle_hash,provider_messages_json,
             prompt_contract_version,persona_hash,persona_source,provider_model,
             effective_temperature,effective_max_tokens,status,dispatch_expires_at,generation_id,
             user_message_id,assistant_message_id,response_json,error_category,created_at,updated_at)
            SELECT id,idempotency_key,'explicit',NULL,NULL,
             request_payload_hash,request_identity_hash,client_id,api_session,mapping_revision,
             history_before_id,context_bundle_json,context_bundle_hash,provider_messages_json,
             prompt_contract_version,persona_hash,persona_source,provider_model,
             effective_temperature,effective_max_tokens,status,dispatch_expires_at,generation_id,
             user_message_id,assistant_message_id,response_json,error_category,created_at,updated_at
            FROM kelivo_requests_v3""")
    conn.execute("DROP TABLE kelivo_requests_v3")
    conn.execute(KELIVO_INDEX_DDL["idx_kelivo_requests_status"])
    conn.execute(KELIVO_INDEX_DDL["idx_kelivo_requests_automatic"])


HEARTBEAT_TABLE_DDL: dict[str, str] = {
    "heartbeat_state": """CREATE TABLE heartbeat_state (
            state_key TEXT PRIMARY KEY CHECK(state_key='default'),
            last_tick_at TEXT,
            last_success_at TEXT,
            last_contact_at TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_failures >= 0),
            status TEXT NOT NULL DEFAULT 'idle' CHECK(status IN
                ('idle','disabled','quiet_hours','cooldown','observe',
                 'journal_candidate','contact_candidate','failed')),
            pause_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(
                (status IN ('idle','observe','journal_candidate','contact_candidate') AND pause_reason IS NULL)
                OR
                (status IN ('disabled','quiet_hours','cooldown','failed') AND pause_reason IS NOT NULL)
            ))""",
    "heartbeat_runs": """CREATE TABLE heartbeat_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            dedupe_key TEXT NOT NULL UNIQUE
                CHECK(length(dedupe_key)=64 AND dedupe_key NOT GLOB '*[^0-9a-f]*'),
            scheduled_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            outcome TEXT NOT NULL CHECK(outcome IN ('running','completed','failed')),
            decision TEXT CHECK(decision IS NULL OR decision IN
                ('disabled','quiet_hours','cooldown','observe','journal_candidate','contact_candidate')),
            error_category TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
                CHECK(json_valid(metadata_json) AND json_type(metadata_json)='object'
                      AND length(metadata_json)<=4096),
            attempt_count INTEGER NOT NULL DEFAULT 1 CHECK(attempt_count > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(
                (outcome='running' AND completed_at IS NULL AND decision IS NULL AND error_category IS NULL)
                OR
                (outcome='completed' AND completed_at IS NOT NULL AND decision IS NOT NULL
                 AND error_category IS NULL)
                OR
                (outcome='failed' AND completed_at IS NOT NULL AND decision IS NULL
                 AND error_category IS NOT NULL)
            ))""",
    "journal_entries": """CREATE TABLE journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type TEXT NOT NULL CHECK(entry_type IN ('journal_candidate','contact_candidate')),
            content TEXT NOT NULL CHECK(length(content)>0 AND length(content)<=256),
            created_at TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source='heartbeat'),
            heartbeat_run_id INTEGER,
            dedupe_key TEXT NOT NULL UNIQUE
                CHECK(length(dedupe_key)=64 AND dedupe_key NOT GLOB '*[^0-9a-f]*'),
            FOREIGN KEY(heartbeat_run_id) REFERENCES heartbeat_runs(id))""",
    "timeline_events": """CREATE TABLE timeline_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL CHECK(event_type IN
                ('disabled','quiet_hours','cooldown','observe','journal_candidate','contact_candidate')),
            summary TEXT NOT NULL CHECK(length(summary)>0 AND length(summary)<=256),
            event_at TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source='heartbeat'),
            heartbeat_run_id INTEGER,
            dedupe_key TEXT NOT NULL UNIQUE
                CHECK(length(dedupe_key)=64 AND dedupe_key NOT GLOB '*[^0-9a-f]*'),
            FOREIGN KEY(heartbeat_run_id) REFERENCES heartbeat_runs(id))""",
}

HEARTBEAT_INDEX_DDL: dict[str, str] = {
    "idx_heartbeat_runs_schedule":
        "CREATE INDEX idx_heartbeat_runs_schedule ON heartbeat_runs(scheduled_at,outcome,id)",
    "idx_journal_entries_run":
        "CREATE INDEX idx_journal_entries_run ON journal_entries(heartbeat_run_id,created_at,id)",
    "idx_timeline_events_time":
        "CREATE INDEX idx_timeline_events_time ON timeline_events(event_at,id)",
}


def _migration_005(conn: sqlite3.Connection) -> None:
    """Add the local-only Dylan heartbeat state, run ledger, journal, and timeline."""
    validate_kelivo_schema(conn, version=4)
    for statement in (*HEARTBEAT_TABLE_DDL.values(), *HEARTBEAT_INDEX_DDL.values()):
        conn.execute(statement)


HEARTBEAT_HARDENING_TABLE_DDL: dict[str, str] = {
    "heartbeat_schedule_revisions": """CREATE TABLE heartbeat_schedule_revisions (
            schedule_revision TEXT PRIMARY KEY
                CHECK(length(schedule_revision) BETWEEN 1 AND 64
                      AND schedule_revision NOT GLOB '*[^A-Za-z0-9._-]*'
                      AND substr(schedule_revision,1,1) GLOB '[A-Za-z0-9]'),
            schedule_fingerprint TEXT NOT NULL
                CHECK(length(schedule_fingerprint)=64
                      AND schedule_fingerprint NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""",
    "heartbeat_run_inputs": """CREATE TABLE heartbeat_run_inputs (
            heartbeat_run_id INTEGER PRIMARY KEY,
            schedule_revision TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL
                CHECK(length(input_fingerprint)=64
                      AND input_fingerprint NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL,
            FOREIGN KEY(heartbeat_run_id) REFERENCES heartbeat_runs(id),
            FOREIGN KEY(schedule_revision)
                REFERENCES heartbeat_schedule_revisions(schedule_revision))""",
}

HEARTBEAT_HARDENING_INDEX_DDL: dict[str, str] = {
    "idx_heartbeat_run_inputs_revision":
        "CREATE INDEX idx_heartbeat_run_inputs_revision "
        "ON heartbeat_run_inputs(schedule_revision,heartbeat_run_id)",
}


def _migration_006(conn: sqlite3.Connection) -> None:
    """Bind logical ticks to validated schedule revisions and request fingerprints."""
    validate_heartbeat_schema(conn)
    for statement in (
        *HEARTBEAT_HARDENING_TABLE_DDL.values(), *HEARTBEAT_HARDENING_INDEX_DDL.values(),
    ):
        conn.execute(statement)


MEMORY_TABLE_DDL: dict[str, str] = {
    "memory_items": """CREATE TABLE memory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_key TEXT NOT NULL UNIQUE
                CHECK(length(memory_key) BETWEEN 32 AND 96
                      AND memory_key NOT GLOB '*[^A-Za-z0-9_-]*'),
            kind TEXT NOT NULL CHECK(kind IN
                ('user_preference','user_profile','relationship','shared_episode',
                 'project','decision','task_or_progress','assistant_experience')),
            scope_type TEXT NOT NULL CHECK(scope_type IN
                ('global_user','channel','session','project')),
            scope_ref TEXT NOT NULL,
            normalized_content TEXT,
            normalized_fingerprint BLOB,
            fingerprint_version INTEGER NOT NULL CHECK(fingerprint_version > 0),
            status TEXT NOT NULL CHECK(status IN
                ('candidate','active','superseded','forgotten','rejected')),
            explicitness TEXT NOT NULL CHECK(explicitness IN ('explicit','inferred')),
            confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
            sensitivity TEXT NOT NULL CHECK(sensitivity IN ('normal','sensitive','restricted')),
            first_observed_at TEXT NOT NULL,
            last_confirmed_at TEXT NOT NULL,
            superseded_by_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(
                (scope_type='global_user' AND scope_ref='')
                OR
                (scope_type!='global_user' AND length(scope_ref) BETWEEN 1 AND 128
                 AND scope_ref NOT GLOB '*[^A-Za-z0-9._:-]*')
            ),
            CHECK(
                (status IN ('candidate','active','rejected')
                 AND normalized_content IS NOT NULL AND length(normalized_content)>0
                 AND normalized_fingerprint IS NOT NULL
                 AND typeof(normalized_fingerprint)='blob' AND length(normalized_fingerprint)=32
                 AND superseded_by_id IS NULL)
                OR
                (status='superseded'
                 AND normalized_content IS NOT NULL AND length(normalized_content)>0
                 AND normalized_fingerprint IS NOT NULL
                 AND typeof(normalized_fingerprint)='blob' AND length(normalized_fingerprint)=32
                 AND superseded_by_id IS NOT NULL)
                OR
                (status='forgotten' AND normalized_content IS NULL
                 AND normalized_fingerprint IS NULL AND superseded_by_id IS NULL)
            ),
            CHECK(superseded_by_id IS NULL OR superseded_by_id != id),
            FOREIGN KEY(superseded_by_id) REFERENCES memory_items(id) ON DELETE RESTRICT)""",
    "memory_fingerprint_profile": """CREATE TABLE memory_fingerprint_profile (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            key_id TEXT NOT NULL
                CHECK(length(key_id) BETWEEN 1 AND 64
                      AND key_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
            key_check BLOB NOT NULL
                CHECK(typeof(key_check)='blob' AND length(key_check)=32),
            normalization_version INTEGER NOT NULL CHECK(normalization_version > 0),
            fingerprint_version INTEGER NOT NULL CHECK(fingerprint_version > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""",
    "memory_evidence_events": """CREATE TABLE memory_evidence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_message_id INTEGER NOT NULL UNIQUE,
            action_id TEXT NOT NULL UNIQUE
                CHECK(length(action_id) BETWEEN 24 AND 96
                      AND action_id NOT GLOB '*[^A-Za-z0-9_-]*'),
            action_type TEXT NOT NULL CHECK(action_type IN
                ('remember_explicit_user','confirm_project_decision',
                 'correct_explicit_user','forget_explicit_user',
                 'record_assistant_experience')),
            action_binding_version INTEGER NOT NULL
                CHECK(action_binding_version=1),
            evidence_type TEXT NOT NULL CHECK(evidence_type IN
                ('explicit_user_memory','confirmed_user_fact',
                 'confirmed_project_decision','explicit_user_correction',
                 'user_forget','assistant_experience')),
            reality_scope TEXT NOT NULL CHECK(reality_scope IN
                ('real','roleplay','joke','fiction','third_party')),
            subject_scope TEXT NOT NULL CHECK(subject_scope IN
                ('user','project','assistant','third_party')),
            created_by_component TEXT NOT NULL CHECK(created_by_component IN
                ('memory_admin','web_adapter','telegram_adapter','kelivo_adapter',
                 'operit_adapter','galatea_adapter','assistant_runtime')),
            created_at TEXT NOT NULL,
            UNIQUE(id,canonical_message_id,evidence_type),
            FOREIGN KEY(canonical_message_id) REFERENCES messages(id) ON DELETE RESTRICT)""",
    "memory_sources": """CREATE TABLE memory_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL,
            canonical_message_id INTEGER NOT NULL,
            evidence_event_id INTEGER NOT NULL,
            channel TEXT NOT NULL
                CHECK(length(channel) BETWEEN 1 AND 64
                      AND channel NOT GLOB '*[^A-Za-z0-9._:-]*'),
            source TEXT NOT NULL DEFAULT ''
                CHECK(length(source)<=64
                      AND source NOT GLOB '*[^A-Za-z0-9._:-]*'),
            evidence_role TEXT NOT NULL CHECK(evidence_role IN ('user','assistant')),
            evidence_type TEXT NOT NULL CHECK(evidence_type IN
                ('explicit_user_memory','confirmed_user_fact',
                 'confirmed_project_decision','explicit_user_correction',
                 'user_forget','assistant_experience')),
            created_at TEXT NOT NULL,
            UNIQUE(memory_id,evidence_event_id),
            FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE RESTRICT,
            FOREIGN KEY(canonical_message_id) REFERENCES messages(id) ON DELETE RESTRICT,
            FOREIGN KEY(evidence_event_id,canonical_message_id,evidence_type)
                REFERENCES memory_evidence_events(id,canonical_message_id,evidence_type)
                ON DELETE RESTRICT)""",
    "memory_suppressions": """CREATE TABLE memory_suppressions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL CHECK(scope_type IN
                ('global_user','channel','session','project')),
            scope_ref TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN
                ('user_preference','user_profile','relationship','shared_episode',
                 'project','decision','task_or_progress','assistant_experience')),
            normalized_fingerprint BLOB NOT NULL
                CHECK(typeof(normalized_fingerprint)='blob'
                      AND length(normalized_fingerprint)=32),
            fingerprint_version INTEGER NOT NULL CHECK(fingerprint_version > 0),
            reason_category TEXT NOT NULL CHECK(reason_category IN
                ('user_forget','user_reject','privacy_policy','corrected_obsolete')),
            created_at TEXT NOT NULL,
            CHECK(
                (scope_type='global_user' AND scope_ref='')
                OR
                (scope_type!='global_user' AND length(scope_ref) BETWEEN 1 AND 128
                 AND scope_ref NOT GLOB '*[^A-Za-z0-9._:-]*')
            ),
            UNIQUE(scope_type,scope_ref,kind,fingerprint_version,normalized_fingerprint))""",
}

MEMORY_INDEX_DDL: dict[str, str] = {
    "idx_memory_items_active_lookup":
        "CREATE INDEX idx_memory_items_active_lookup "
        "ON memory_items(status,scope_type,scope_ref,kind,sensitivity,last_confirmed_at,id)",
    "idx_memory_items_superseded_by":
        "CREATE INDEX idx_memory_items_superseded_by ON memory_items(superseded_by_id)",
    "idx_memory_items_live_fingerprint":
        "CREATE UNIQUE INDEX idx_memory_items_live_fingerprint "
        "ON memory_items(scope_type,scope_ref,kind,fingerprint_version,normalized_fingerprint) "
        "WHERE status IN ('active','candidate')",
    "idx_memory_sources_memory":
        "CREATE INDEX idx_memory_sources_memory ON memory_sources(memory_id,id)",
    "idx_memory_sources_canonical":
        "CREATE INDEX idx_memory_sources_canonical ON memory_sources(canonical_message_id,id)",
}

MEMORY_TRIGGER_DDL: dict[str, str] = {
    "memory_evidence_events_immutable_update":
        """CREATE TRIGGER memory_evidence_events_immutable_update
           BEFORE UPDATE ON memory_evidence_events
           BEGIN
             SELECT RAISE(ABORT,'memory_evidence_event_immutable');
           END""",
    "memory_evidence_events_immutable_delete":
        """CREATE TRIGGER memory_evidence_events_immutable_delete
           BEFORE DELETE ON memory_evidence_events
           BEGIN
             SELECT RAISE(ABORT,'memory_evidence_event_immutable');
           END""",
}


def _migration_007(conn: sqlite3.Connection) -> None:
    """Add the disabled-by-default, explicit derived Memory Core foundation."""
    validate_heartbeat_hardening_schema(conn)
    for statement in (
        *MEMORY_TABLE_DDL.values(),
        *MEMORY_INDEX_DDL.values(),
        *MEMORY_TRIGGER_DDL.values(),
    ):
        conn.execute(statement)


MEMORY_ACTION_REQUEST_TABLE_DDL = """CREATE TABLE memory_action_requests (
        request_id TEXT PRIMARY KEY NOT NULL
            CHECK(length(request_id) BETWEEN 32 AND 96
                  AND request_id NOT GLOB '*[^A-Za-z0-9_-]*'),
        action_kind TEXT NOT NULL
            CHECK(action_kind IN ('remember','correct','forget')),
        origin TEXT NOT NULL
            CHECK(origin IN ('operator_cli','mcp','telegram','operit')),
        request_binding_digest BLOB NOT NULL
            CHECK(typeof(request_binding_digest)='blob'
                  AND length(request_binding_digest)=32),
        target_memory_key TEXT
            CHECK(target_memory_key IS NULL
                  OR (length(target_memory_key) BETWEEN 32 AND 96
                      AND target_memory_key NOT GLOB '*[^A-Za-z0-9_-]*')),
        canonical_message_id INTEGER UNIQUE,
        result_memory_key TEXT
            CHECK(result_memory_key IS NULL
                  OR (length(result_memory_key) BETWEEN 32 AND 96
                      AND result_memory_key NOT GLOB '*[^A-Za-z0-9_-]*')),
        status TEXT NOT NULL CHECK(status IN ('completed','failed')),
        result_category TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(
            (action_kind='remember' AND target_memory_key IS NULL)
            OR
            (action_kind IN ('correct','forget') AND target_memory_key IS NOT NULL)
        ),
        CHECK(
            (
                status='completed'
                AND canonical_message_id IS NOT NULL
                AND (
                    (
                        action_kind='remember'
                        AND (
                            (result_category IN ('created','idempotent_existing')
                             AND result_memory_key IS NOT NULL)
                            OR
                            (result_category='suppressed'
                             AND result_memory_key IS NULL)
                        )
                    )
                    OR
                    (
                        action_kind='correct'
                        AND (
                            (result_category IN ('corrected','unchanged')
                             AND result_memory_key IS NOT NULL)
                            OR
                            (result_category='suppressed'
                             AND result_memory_key IS NULL)
                        )
                    )
                    OR
                    (
                        action_kind='forget'
                        AND result_category IN ('forgotten','already_forgotten')
                        AND result_memory_key=target_memory_key
                    )
                )
            )
            OR
            (
                status='failed'
                AND canonical_message_id IS NULL
                AND result_memory_key IS NULL
                AND result_category IN (
                    'authorization_expired','authorization_invalid',
                    'authorization_not_yet_valid','authorization_replayed',
                    'conflict','explicit_writes_disabled','feature_disabled',
                    'invalid_content','invalid_kind','invalid_memory_key',
                    'invalid_provenance','invalid_request','invalid_scope',
                    'invalid_sensitivity','invalid_state',
                    'memory_configuration_invalid','memory_schema_invalid',
                    'not_found','request_binding_conflict',
                    'sensitive_storage_disabled','sensitivity_downgrade',
                    'storage_unavailable','terminal_semantics_invalid',
                    'unsupported_evidence'
                )
            )
        ),
        CHECK(
            length(created_at) BETWEEN 25 AND 40
            AND created_at NOT GLOB '*[^0-9T:+.-]*'
            AND substr(created_at,5,1)='-'
            AND substr(created_at,8,1)='-'
            AND substr(created_at,11,1)='T'
            AND substr(created_at,14,1)=':'
            AND substr(created_at,17,1)=':'
            AND substr(created_at,-6)='+00:00'
            AND updated_at=created_at
        ),
        FOREIGN KEY(canonical_message_id)
            REFERENCES messages(id) ON DELETE RESTRICT)"""

MEMORY_ACTION_REQUEST_INDEX_DDL = {
    "idx_memory_action_requests_status_created":
        "CREATE INDEX idx_memory_action_requests_status_created "
        "ON memory_action_requests(status,created_at,request_id)",
}

MEMORY_ACTION_REQUEST_TRIGGER_DDL = {
    "memory_action_requests_immutable_update":
        """CREATE TRIGGER memory_action_requests_immutable_update
           BEFORE UPDATE ON memory_action_requests
           BEGIN
             SELECT RAISE(ABORT,'memory_action_request_immutable');
           END""",
    "memory_action_requests_immutable_delete":
        """CREATE TRIGGER memory_action_requests_immutable_delete
           BEFORE DELETE ON memory_action_requests
           BEGIN
             SELECT RAISE(ABORT,'memory_action_request_immutable');
           END""",
}


def _migration_008(conn: sqlite3.Connection) -> None:
    """Add the terminal explicit-action request ledger without changing v1-v7."""
    validate_memory_schema(conn)
    conn.execute(MEMORY_ACTION_REQUEST_TABLE_DDL)
    for statement in (
        *MEMORY_ACTION_REQUEST_INDEX_DDL.values(),
        *MEMORY_ACTION_REQUEST_TRIGGER_DDL.values(),
    ):
        conn.execute(statement)


def _index_columns(conn: sqlite3.Connection, index_name: str) -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA index_xinfo({index_name})").fetchall()
    return tuple(row["name"] for row in rows if row["key"] == 1 and row["cid"] >= 0)


def _sql_fingerprint(sql: str) -> tuple[str, ...]:
    """Tokenize the limited migration DDL, ignoring whitespace/comments but preserving boolean structure."""
    tokens: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            end = sql.find("\n", index + 2)
            index = len(sql) if end < 0 else end + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise sqlite3.DatabaseError("invalid kelivo schema SQL")
            index = end + 2
            continue
        if char == "'":
            end = index + 1
            while end < len(sql):
                if sql[end] == "'":
                    if end + 1 < len(sql) and sql[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            if end > len(sql) or sql[end - 1] != "'":
                raise sqlite3.DatabaseError("invalid kelivo schema SQL")
            tokens.append(sql[index:end])
            index = end
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            tokens.append(sql[index:end].lower())
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < len(sql) and (sql[end].isdigit() or sql[end] == "."):
                end += 1
            tokens.append(sql[index:end])
            index = end
            continue
        operator = sql[index:index + 2]
        if operator in {">=", "<=", "!=", "<>", "=="}:
            tokens.append(operator)
            index += 2
            continue
        if char in "(),=><+-*/;":
            tokens.append(char)
            index += 1
            continue
        # Current migration deliberately uses no quoted identifiers or expressions.
        raise sqlite3.DatabaseError("invalid kelivo schema SQL")
    return tuple(tokens)


def _validate_index_xinfo(conn: sqlite3.Connection, name: str, columns: tuple[str, ...]) -> None:
    rows = conn.execute(f"PRAGMA index_xinfo({name})").fetchall()
    key_rows = [row for row in rows if row["key"] == 1]
    auxiliary = [row for row in rows if row["key"] == 0]
    if tuple(row["name"] for row in key_rows) != columns:
        raise sqlite3.DatabaseError(f"invalid kelivo index columns: {name}")
    if any(row["cid"] < 0 or row["desc"] != 0 or str(row["coll"]).upper() != "BINARY" for row in key_rows):
        raise sqlite3.DatabaseError(f"invalid kelivo index expression: {name}")
    if len(auxiliary) != 1 or auxiliary[0]["cid"] != -1:
        raise sqlite3.DatabaseError(f"invalid kelivo index auxiliary shape: {name}")


def validate_kelivo_schema(conn: sqlite3.Connection, *, version: int = 4) -> None:
    """Reject an applied Kelivo marker unless its complete structural fingerprint matches."""
    if version not in {3, 4}:
        raise sqlite3.DatabaseError("unsupported kelivo schema version")
    expected_columns = {
        "kelivo_clients": {
            "client_id": ("TEXT", 0, None, 1), "api_session": ("TEXT", 1, None, 0),
            "enabled": ("INTEGER", 1, "1", 0), "mapping_revision": ("INTEGER", 1, "1", 0),
            "created_at": ("TEXT", 1, None, 0), "updated_at": ("TEXT", 1, None, 0),
        },
        "kelivo_requests": {
            "id": ("INTEGER", 0, None, 1), "idempotency_key": ("TEXT", 1, None, 0),
            "request_payload_hash": ("TEXT", 1, None, 0), "request_identity_hash": ("TEXT", 1, None, 0),
            "client_id": ("TEXT", 1, None, 0), "api_session": ("TEXT", 1, None, 0),
            "mapping_revision": ("INTEGER", 1, None, 0), "history_before_id": ("INTEGER", 1, None, 0),
            "context_bundle_json": ("TEXT", 1, None, 0), "context_bundle_hash": ("TEXT", 1, None, 0),
            "provider_messages_json": ("TEXT", 1, None, 0),
            "prompt_contract_version": ("TEXT", 1, None, 0), "persona_hash": ("TEXT", 1, None, 0),
            "persona_source": ("TEXT", 1, None, 0), "provider_model": ("TEXT", 1, None, 0),
            "effective_temperature": ("REAL", 1, None, 0),
            "effective_max_tokens": ("INTEGER", 1, None, 0), "status": ("TEXT", 1, None, 0),
            "dispatch_expires_at": ("TEXT", 0, None, 0), "generation_id": ("TEXT", 1, None, 0),
            "user_message_id": ("INTEGER", 0, None, 0), "assistant_message_id": ("INTEGER", 0, None, 0),
            "response_json": ("TEXT", 0, None, 0), "error_category": ("TEXT", 0, None, 0),
            "created_at": ("TEXT", 1, None, 0), "updated_at": ("TEXT", 1, None, 0),
        },
        "companion_context_snapshots": {
            "id": ("INTEGER", 0, None, 1), "api_session": ("TEXT", 1, None, 0),
            "snapshot_type": ("TEXT", 1, None, 0), "normalized_json": ("TEXT", 1, None, 0),
            "content_hash": ("TEXT", 1, None, 0), "active": ("INTEGER", 1, "1", 0),
            "version": ("INTEGER", 1, None, 0), "created_at": ("TEXT", 1, None, 0),
        },
        "kelivo_rate_limits": {
            "client_id": ("TEXT", 1, None, 1), "window_started_at": ("INTEGER", 1, None, 2),
            "request_count": ("INTEGER", 1, None, 0), "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
    }
    if version >= 4:
        request_columns = expected_columns["kelivo_requests"]
        request_columns["idempotency_mode"] = ("TEXT", 1, "'explicit'", 0)
        request_columns["automatic_fingerprint"] = ("TEXT", 0, None, 0)
        request_columns["automatic_replay_until"] = ("TEXT", 0, None, 0)
    for table, expected in expected_columns.items():
        xinfo_rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
        if any(int(row["hidden"]) != 0 for row in xinfo_rows):
            raise sqlite3.DatabaseError(f"invalid hidden kelivo column: {table}")
        actual = {
            row["name"]: (str(row["type"]).upper(), int(row["notnull"]), row["dflt_value"], int(row["pk"]))
            for row in xinfo_rows
        }
        if actual != expected:
            raise sqlite3.DatabaseError(f"invalid kelivo schema: {table} columns")

    expected_indexes = {
        "kelivo_clients": {
            "sqlite_autoindex_kelivo_clients_1": (True, "pk", False, ("client_id",)),
        },
        "kelivo_requests": {
            "sqlite_autoindex_kelivo_requests_1": (True, "u", False, ("generation_id",)),
            "sqlite_autoindex_kelivo_requests_2": (True, "u", False, ("client_id", "idempotency_key")),
            "idx_kelivo_requests_status": (False, "c", False, ("status", "dispatch_expires_at", "id")),
        },
        "companion_context_snapshots": {
            "sqlite_autoindex_companion_context_snapshots_1":
                (True, "u", False, ("api_session", "snapshot_type", "content_hash")),
            "sqlite_autoindex_companion_context_snapshots_2":
                (True, "u", False, ("api_session", "snapshot_type", "version")),
            "idx_context_snapshots_lookup":
                (False, "c", False, ("api_session", "snapshot_type", "active", "version")),
            "idx_context_snapshots_one_active":
                (True, "c", True, ("api_session", "snapshot_type")),
        },
        "kelivo_rate_limits": {
            "sqlite_autoindex_kelivo_rate_limits_1":
                (True, "pk", False, ("client_id", "window_started_at")),
            "idx_kelivo_rate_limits_window":
                (False, "c", False, ("window_started_at", "client_id")),
        },
    }
    if version >= 4:
        expected_indexes["kelivo_requests"]["idx_kelivo_requests_automatic"] = (
            False, "c", False, ("client_id", "automatic_fingerprint", "created_at", "status")
        )
    for table, expected in expected_indexes.items():
        actual_rows = {row["name"]: row for row in conn.execute(f"PRAGMA index_list({table})")}
        if set(actual_rows) != set(expected):
            raise sqlite3.DatabaseError(f"invalid kelivo index set: {table}")
        for name, (unique, origin, partial, columns) in expected.items():
            row = actual_rows[name]
            if (bool(row["unique"]), row["origin"], bool(row["partial"])) != (unique, origin, partial):
                raise sqlite3.DatabaseError(f"invalid kelivo index attributes: {name}")
            _validate_index_xinfo(conn, name, columns)
    expected_fks = {
        "kelivo_requests": {
            ("client_id", "kelivo_clients", "client_id", "NO ACTION", "NO ACTION", "NONE"),
            ("user_message_id", "messages", "id", "NO ACTION", "NO ACTION", "NONE"),
            ("assistant_message_id", "messages", "id", "NO ACTION", "NO ACTION", "NONE"),
        },
        "kelivo_rate_limits": {
            ("client_id", "kelivo_clients", "client_id", "NO ACTION", "NO ACTION", "NONE"),
        },
        "kelivo_clients": set(), "companion_context_snapshots": set(),
    }
    for table, expected in expected_fks.items():
        actual = {
            (row["from"], row["table"], row["to"], row["on_update"], row["on_delete"], row["match"])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }
        if actual != expected:
            raise sqlite3.DatabaseError(f"invalid kelivo foreign key: {table}")
    expected_tables = KELIVO_TABLE_DDL if version >= 4 else KELIVO_V3_TABLE_DDL
    expected_index_ddl = KELIVO_INDEX_DDL if version >= 4 else KELIVO_V3_INDEX_DDL
    for table, expected_sql in expected_tables.items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row is None or _sql_fingerprint(str(row["sql"])) != _sql_fingerprint(expected_sql):
            raise sqlite3.DatabaseError(f"invalid kelivo table fingerprint: {table}")
    for name, expected_sql in expected_index_ddl.items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        if row is None or _sql_fingerprint(str(row["sql"])) != _sql_fingerprint(expected_sql):
            raise sqlite3.DatabaseError(f"invalid kelivo index fingerprint: {name}")


def validate_heartbeat_schema(conn: sqlite3.Connection) -> None:
    """Validate the complete v5 heartbeat schema rather than trusting its marker."""
    expected_columns = {
        "heartbeat_state": {
            "state_key": ("TEXT", 0, None, 1),
            "last_tick_at": ("TEXT", 0, None, 0),
            "last_success_at": ("TEXT", 0, None, 0),
            "last_contact_at": ("TEXT", 0, None, 0),
            "consecutive_failures": ("INTEGER", 1, "0", 0),
            "status": ("TEXT", 1, "'idle'", 0),
            "pause_reason": ("TEXT", 0, None, 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
        "heartbeat_runs": {
            "id": ("INTEGER", 0, None, 1),
            "run_id": ("TEXT", 1, None, 0),
            "dedupe_key": ("TEXT", 1, None, 0),
            "scheduled_at": ("TEXT", 1, None, 0),
            "started_at": ("TEXT", 1, None, 0),
            "completed_at": ("TEXT", 0, None, 0),
            "outcome": ("TEXT", 1, None, 0),
            "decision": ("TEXT", 0, None, 0),
            "error_category": ("TEXT", 0, None, 0),
            "metadata_json": ("TEXT", 1, "'{}'", 0),
            "attempt_count": ("INTEGER", 1, "1", 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
        "journal_entries": {
            "id": ("INTEGER", 0, None, 1),
            "entry_type": ("TEXT", 1, None, 0),
            "content": ("TEXT", 1, None, 0),
            "created_at": ("TEXT", 1, None, 0),
            "source": ("TEXT", 1, None, 0),
            "heartbeat_run_id": ("INTEGER", 0, None, 0),
            "dedupe_key": ("TEXT", 1, None, 0),
        },
        "timeline_events": {
            "id": ("INTEGER", 0, None, 1),
            "event_type": ("TEXT", 1, None, 0),
            "summary": ("TEXT", 1, None, 0),
            "event_at": ("TEXT", 1, None, 0),
            "source": ("TEXT", 1, None, 0),
            "heartbeat_run_id": ("INTEGER", 0, None, 0),
            "dedupe_key": ("TEXT", 1, None, 0),
        },
    }
    for table, expected in expected_columns.items():
        rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
        if any(int(row["hidden"]) != 0 for row in rows):
            raise sqlite3.DatabaseError(f"invalid hidden heartbeat column: {table}")
        actual = {
            row["name"]: (str(row["type"]).upper(), int(row["notnull"]), row["dflt_value"], int(row["pk"]))
            for row in rows
        }
        if actual != expected:
            raise sqlite3.DatabaseError(f"invalid heartbeat schema: {table} columns")

    expected_indexes = {
        "heartbeat_state": {
            "sqlite_autoindex_heartbeat_state_1": (True, "pk", False, ("state_key",)),
        },
        "heartbeat_runs": {
            "sqlite_autoindex_heartbeat_runs_1": (True, "u", False, ("run_id",)),
            "sqlite_autoindex_heartbeat_runs_2": (True, "u", False, ("dedupe_key",)),
            "idx_heartbeat_runs_schedule": (False, "c", False, ("scheduled_at", "outcome", "id")),
        },
        "journal_entries": {
            "sqlite_autoindex_journal_entries_1": (True, "u", False, ("dedupe_key",)),
            "idx_journal_entries_run": (False, "c", False, ("heartbeat_run_id", "created_at", "id")),
        },
        "timeline_events": {
            "sqlite_autoindex_timeline_events_1": (True, "u", False, ("dedupe_key",)),
            "idx_timeline_events_time": (False, "c", False, ("event_at", "id")),
        },
    }
    for table, expected in expected_indexes.items():
        actual_rows = {row["name"]: row for row in conn.execute(f"PRAGMA index_list({table})")}
        if set(actual_rows) != set(expected):
            raise sqlite3.DatabaseError(f"invalid heartbeat index set: {table}")
        for name, (unique, origin, partial, columns) in expected.items():
            row = actual_rows[name]
            if (bool(row["unique"]), row["origin"], bool(row["partial"])) != (unique, origin, partial):
                raise sqlite3.DatabaseError(f"invalid heartbeat index attributes: {name}")
            _validate_index_xinfo(conn, name, columns)

    expected_fks = {
        "heartbeat_state": set(),
        "heartbeat_runs": set(),
        "journal_entries": {
            ("heartbeat_run_id", "heartbeat_runs", "id", "NO ACTION", "NO ACTION", "NONE"),
        },
        "timeline_events": {
            ("heartbeat_run_id", "heartbeat_runs", "id", "NO ACTION", "NO ACTION", "NONE"),
        },
    }
    for table, expected in expected_fks.items():
        actual = {
            (row["from"], row["table"], row["to"], row["on_update"], row["on_delete"], row["match"])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }
        if actual != expected:
            raise sqlite3.DatabaseError(f"invalid heartbeat foreign key: {table}")

    for table, expected_sql in HEARTBEAT_TABLE_DDL.items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row is None or _sql_fingerprint(str(row["sql"])) != _sql_fingerprint(expected_sql):
            raise sqlite3.DatabaseError(f"invalid heartbeat table fingerprint: {table}")
    for name, expected_sql in HEARTBEAT_INDEX_DDL.items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        if row is None or _sql_fingerprint(str(row["sql"])) != _sql_fingerprint(expected_sql):
            raise sqlite3.DatabaseError(f"invalid heartbeat index fingerprint: {name}")


def validate_heartbeat_hardening_schema(conn: sqlite3.Connection) -> None:
    """Validate the v6 schedule-revision and logical-input schema."""
    expected_columns = {
        "heartbeat_schedule_revisions": {
            "schedule_revision": ("TEXT", 0, None, 1),
            "schedule_fingerprint": ("TEXT", 1, None, 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
        "heartbeat_run_inputs": {
            "heartbeat_run_id": ("INTEGER", 0, None, 1),
            "schedule_revision": ("TEXT", 1, None, 0),
            "input_fingerprint": ("TEXT", 1, None, 0),
            "created_at": ("TEXT", 1, None, 0),
        },
    }
    for table, expected in expected_columns.items():
        rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
        if any(int(row["hidden"]) != 0 for row in rows):
            raise sqlite3.DatabaseError(f"invalid hidden heartbeat hardening column: {table}")
        actual = {
            row["name"]: (
                str(row["type"]).upper(), int(row["notnull"]), row["dflt_value"], int(row["pk"]),
            )
            for row in rows
        }
        if actual != expected:
            raise sqlite3.DatabaseError(f"invalid heartbeat hardening schema: {table} columns")

    expected_indexes = {
        "heartbeat_schedule_revisions": {
            "sqlite_autoindex_heartbeat_schedule_revisions_1": (
                True, "pk", False, ("schedule_revision",),
            ),
        },
        "heartbeat_run_inputs": {
            "idx_heartbeat_run_inputs_revision": (
                False, "c", False, ("schedule_revision", "heartbeat_run_id"),
            ),
        },
    }
    for table, expected in expected_indexes.items():
        actual_rows = {row["name"]: row for row in conn.execute(f"PRAGMA index_list({table})")}
        if set(actual_rows) != set(expected):
            raise sqlite3.DatabaseError(f"invalid heartbeat hardening index set: {table}")
        for name, (unique, origin, partial, columns) in expected.items():
            row = actual_rows[name]
            if (bool(row["unique"]), row["origin"], bool(row["partial"])) != (
                unique, origin, partial,
            ):
                raise sqlite3.DatabaseError(f"invalid heartbeat hardening index attributes: {name}")
            _validate_index_xinfo(conn, name, columns)

    expected_fks = {
        "heartbeat_schedule_revisions": set(),
        "heartbeat_run_inputs": {
            ("heartbeat_run_id", "heartbeat_runs", "id", "NO ACTION", "NO ACTION", "NONE"),
            (
                "schedule_revision", "heartbeat_schedule_revisions", "schedule_revision",
                "NO ACTION", "NO ACTION", "NONE",
            ),
        },
    }
    for table, expected in expected_fks.items():
        actual = {
            (row["from"], row["table"], row["to"], row["on_update"], row["on_delete"], row["match"])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }
        if actual != expected:
            raise sqlite3.DatabaseError(f"invalid heartbeat hardening foreign key: {table}")

    for table, expected_sql in HEARTBEAT_HARDENING_TABLE_DDL.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone()
        if row is None or _sql_fingerprint(str(row["sql"])) != _sql_fingerprint(expected_sql):
            raise sqlite3.DatabaseError(f"invalid heartbeat hardening table fingerprint: {table}")
    for name, expected_sql in HEARTBEAT_HARDENING_INDEX_DDL.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,),
        ).fetchone()
        if row is None or _sql_fingerprint(str(row["sql"])) != _sql_fingerprint(expected_sql):
            raise sqlite3.DatabaseError(f"invalid heartbeat hardening index fingerprint: {name}")


def validate_core_schema_v1_v6(
    conn: sqlite3.Connection,
    *,
    require_relay_tables: bool = False,
) -> None:
    """Validate every v1-v6 migration-owned object and, optionally, relay tables."""
    migration_columns = {
        "version": ("INTEGER", 0, None, 1),
        "name": ("TEXT", 1, None, 0),
        "status": ("TEXT", 1, None, 0),
        "created_at": ("TEXT", 1, None, 0),
        "updated_at": ("TEXT", 1, None, 0),
    }
    expected_columns = {
        "channel_accounts": {
            "id": ("INTEGER", 0, None, 1),
            "channel": ("TEXT", 1, None, 0),
            "external_account_id": ("TEXT", 1, None, 0),
            "status": ("TEXT", 1, "'active'", 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
        "channel_conversations": {
            "id": ("INTEGER", 0, None, 1),
            "channel": ("TEXT", 1, None, 0),
            "external_account_id": ("TEXT", 1, None, 0),
            "external_conversation_id": ("TEXT", 1, None, 0),
            "conversation_type": ("TEXT", 1, None, 0),
            "api_session": ("TEXT", 1, None, 0),
            "status": ("TEXT", 1, "'active'", 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
            "external_user_id": ("TEXT", 0, None, 0),
        },
        "inbound_events": {
            "id": ("INTEGER", 0, None, 1),
            "channel": ("TEXT", 1, None, 0),
            "external_account_id": ("TEXT", 1, None, 0),
            "update_id": ("TEXT", 1, None, 0),
            "event_type": ("TEXT", 1, None, 0),
            "status": ("TEXT", 1, None, 0),
            "error_category": ("TEXT", 0, None, 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
        "external_messages": {
            "id": ("INTEGER", 0, None, 1),
            "channel": ("TEXT", 1, None, 0),
            "external_account_id": ("TEXT", 1, None, 0),
            "external_conversation_id": ("TEXT", 1, None, 0),
            "external_message_id": ("TEXT", 1, None, 0),
            "direction": ("TEXT", 1, None, 0),
            "canonical_message_id": ("INTEGER", 0, None, 0),
            "generation_id": ("TEXT", 0, None, 0),
            "status": ("TEXT", 1, None, 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
        "generation_jobs": {
            "id": ("INTEGER", 0, None, 1),
            "inbound_message_id": ("INTEGER", 1, None, 0),
            "canonical_message_id": ("INTEGER", 1, None, 0),
            "channel": ("TEXT", 1, None, 0),
            "external_account_id": ("TEXT", 1, None, 0),
            "external_conversation_id": ("TEXT", 1, None, 0),
            "api_session": ("TEXT", 1, None, 0),
            "stream_id": ("TEXT", 0, None, 0),
            "generation_id": ("TEXT", 0, None, 0),
            "reply_to": ("TEXT", 0, None, 0),
            "reply_message_id": ("INTEGER", 0, None, 0),
            "status": ("TEXT", 1, None, 0),
            "lease_until": ("TEXT", 0, None, 0),
            "attempt_count": ("INTEGER", 1, "0", 0),
            "error_category": ("TEXT", 0, None, 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
            "dispatch_started_at": ("TEXT", 0, None, 0),
            "awaiting_reply_since": ("TEXT", 0, None, 0),
        },
        "delivery_attempts": {
            "id": ("INTEGER", 0, None, 1),
            "generation_job_id": ("INTEGER", 1, None, 0),
            "idempotency_key": ("TEXT", 1, None, 0),
            "channel": ("TEXT", 1, None, 0),
            "external_account_id": ("TEXT", 1, None, 0),
            "external_conversation_id": ("TEXT", 1, None, 0),
            "payload_text": ("TEXT", 1, None, 0),
            "status": ("TEXT", 1, None, 0),
            "attempt_count": ("INTEGER", 1, "0", 0),
            "external_message_id": ("TEXT", 0, None, 0),
            "error_category": ("TEXT", 0, None, 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
            "retry_after_seconds": ("INTEGER", 0, None, 0),
        },
        "channel_audit_events": {
            "id": ("INTEGER", 0, None, 1),
            "event_type": ("TEXT", 1, None, 0),
            "channel": ("TEXT", 1, None, 0),
            "external_id_hash": ("TEXT", 0, None, 0),
            "request_job_id": ("TEXT", 0, None, 0),
            "status": ("TEXT", 1, None, 0),
            "error_category": ("TEXT", 0, None, 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
        "channel_rate_limits": {
            "id": ("INTEGER", 0, None, 1),
            "channel": ("TEXT", 1, None, 0),
            "external_account_id": ("TEXT", 1, None, 0),
            "external_user_id": ("TEXT", 1, None, 0),
            "window_started_at": ("TEXT", 1, None, 0),
            "event_count": ("INTEGER", 1, None, 0),
            "status": ("TEXT", 1, "'active'", 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
        "telegram_completions": {
            "id": ("INTEGER", 0, None, 1),
            "completion_identity": ("TEXT", 1, None, 0),
            "generation_job_id": ("INTEGER", 1, None, 0),
            "canonical_message_id": ("INTEGER", 1, None, 0),
            "delivery_id": ("INTEGER", 1, None, 0),
            "created_at": ("TEXT", 1, None, 0),
        },
        "delivery_parts": {
            "id": ("INTEGER", 0, None, 1),
            "delivery_id": ("INTEGER", 1, None, 0),
            "part_index": ("INTEGER", 1, None, 0),
            "total_parts": ("INTEGER", 1, None, 0),
            "text_hash": ("TEXT", 1, None, 0),
            "text_length": ("INTEGER", 1, None, 0),
            "payload_text": ("TEXT", 1, None, 0),
            "status": ("TEXT", 1, None, 0),
            "telegram_message_id": ("TEXT", 0, None, 0),
            "error_category": ("TEXT", 0, None, 0),
            "retry_after_seconds": ("INTEGER", 0, None, 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
    }
    expected_indexes = {
        "channel_accounts": {
            "sqlite_autoindex_channel_accounts_1":
                (True, "u", False, ("channel", "external_account_id")),
        },
        "channel_conversations": {
            "sqlite_autoindex_channel_conversations_1":
                (True, "u", False, ("api_session",)),
            "sqlite_autoindex_channel_conversations_2":
                (True, "u", False, (
                    "channel", "external_account_id", "external_conversation_id",
                )),
        },
        "inbound_events": {
            "sqlite_autoindex_inbound_events_1":
                (True, "u", False, (
                    "channel", "external_account_id", "update_id",
                )),
        },
        "external_messages": {
            "sqlite_autoindex_external_messages_1":
                (True, "u", False, (
                    "channel", "external_account_id",
                    "external_conversation_id", "external_message_id",
                )),
        },
        "generation_jobs": {
            "sqlite_autoindex_generation_jobs_1":
                (True, "u", False, ("inbound_message_id",)),
            "sqlite_autoindex_generation_jobs_2":
                (True, "u", False, ("generation_id",)),
            "idx_generation_jobs_status_lease":
                (False, "c", False, ("status", "lease_until", "id")),
        },
        "delivery_attempts": {
            "sqlite_autoindex_delivery_attempts_1":
                (True, "u", False, ("idempotency_key",)),
            "idx_delivery_attempts_status":
                (False, "c", False, ("status", "id")),
        },
        "channel_audit_events": {},
        "channel_rate_limits": {
            "sqlite_autoindex_channel_rate_limits_1":
                (True, "u", False, (
                    "channel", "external_account_id", "external_user_id",
                )),
        },
        "telegram_completions": {
            "sqlite_autoindex_telegram_completions_1":
                (True, "u", False, ("completion_identity",)),
            "sqlite_autoindex_telegram_completions_2":
                (True, "u", False, ("generation_job_id",)),
            "sqlite_autoindex_telegram_completions_3":
                (True, "u", False, ("canonical_message_id",)),
            "sqlite_autoindex_telegram_completions_4":
                (True, "u", False, ("delivery_id",)),
        },
        "delivery_parts": {
            "sqlite_autoindex_delivery_parts_1":
                (True, "u", False, ("delivery_id", "part_index")),
            "idx_delivery_parts_status":
                (False, "c", False, (
                    "delivery_id", "status", "part_index",
                )),
        },
    }
    expected_fks = {
        "channel_accounts": set(),
        "channel_conversations": set(),
        "inbound_events": set(),
        "external_messages": set(),
        "generation_jobs": {
            (
                "inbound_message_id", "external_messages", "id",
                "NO ACTION", "NO ACTION", "NONE",
            ),
        },
        "delivery_attempts": {
            (
                "generation_job_id", "generation_jobs", "id",
                "NO ACTION", "NO ACTION", "NONE",
            ),
        },
        "channel_audit_events": set(),
        "channel_rate_limits": set(),
        "telegram_completions": {
            (
                "generation_job_id", "generation_jobs", "id",
                "NO ACTION", "NO ACTION", "NONE",
            ),
            (
                "delivery_id", "delivery_attempts", "id",
                "NO ACTION", "NO ACTION", "NONE",
            ),
        },
        "delivery_parts": {
            (
                "delivery_id", "delivery_attempts", "id",
                "NO ACTION", "NO ACTION", "NONE",
            ),
        },
    }

    def validate_columns(table: str, expected: dict) -> None:
        rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
        if any(int(row["hidden"]) != 0 for row in rows):
            raise sqlite3.DatabaseError("invalid core schema")
        actual = {
            row["name"]: (
                str(row["type"]).upper(),
                int(row["notnull"]),
                row["dflt_value"],
                int(row["pk"]),
            )
            for row in rows
        }
        if actual != expected:
            raise sqlite3.DatabaseError("invalid core schema")

    validate_columns("schema_migrations", migration_columns)
    migration_index_rows = conn.execute(
        "PRAGMA index_list(schema_migrations)"
    ).fetchall()
    if migration_index_rows:
        raise sqlite3.DatabaseError("invalid core schema")
    migration_sql = conn.execute(
        """SELECT sql FROM sqlite_master
           WHERE type='table' AND name='schema_migrations'"""
    ).fetchone()
    if (
        migration_sql is None
        or _sql_fingerprint(str(migration_sql["sql"]))
        != _sql_fingerprint(SCHEMA_MIGRATIONS_DDL)
    ):
        raise sqlite3.DatabaseError("invalid core schema")
    for table, expected in expected_columns.items():
        validate_columns(table, expected)
    for table, expected in expected_indexes.items():
        actual_rows = {
            row["name"]: row
            for row in conn.execute(f"PRAGMA index_list({table})")
        }
        if set(actual_rows) != set(expected):
            raise sqlite3.DatabaseError("invalid core schema")
        for name, (unique, origin, partial, columns) in expected.items():
            row = actual_rows[name]
            if (
                bool(row["unique"]), row["origin"], bool(row["partial"])
            ) != (unique, origin, partial):
                raise sqlite3.DatabaseError("invalid core schema")
            _validate_index_xinfo(conn, name, columns)
    for table, expected in expected_fks.items():
        actual = {
            (
                row["from"], row["table"], row["to"],
                row["on_update"], row["on_delete"], row["match"],
            )
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }
        if actual != expected:
            raise sqlite3.DatabaseError("invalid core schema")

    final_table_ddl = dict(CORE_V1_TABLE_DDL)
    final_table_ddl["channel_conversations"] = (
        CORE_V1_TABLE_DDL["channel_conversations"].replace(
            "updated_at TEXT NOT NULL,\n            UNIQUE",
            "updated_at TEXT NOT NULL, external_user_id TEXT,\n            UNIQUE",
        )
    )
    final_table_ddl["generation_jobs"] = (
        CORE_V1_TABLE_DDL["generation_jobs"].replace(
            "updated_at TEXT NOT NULL,\n            FOREIGN KEY",
            "updated_at TEXT NOT NULL, dispatch_started_at TEXT, "
            "awaiting_reply_since TEXT,\n            FOREIGN KEY",
        )
    )
    final_table_ddl["delivery_attempts"] = (
        CORE_V1_TABLE_DDL["delivery_attempts"].replace(
            "updated_at TEXT NOT NULL,\n            FOREIGN KEY",
            "updated_at TEXT NOT NULL, retry_after_seconds INTEGER,\n"
            "            FOREIGN KEY",
        )
    )
    final_table_ddl.update(CORE_V2_TABLE_DDL)
    for table, expected_sql in final_table_ddl.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if (
            row is None
            or _sql_fingerprint(str(row["sql"]))
            != _sql_fingerprint(expected_sql)
        ):
            raise sqlite3.DatabaseError("invalid core schema")
    expected_index_ddl = {**CORE_V1_INDEX_DDL, **CORE_V2_INDEX_DDL}
    for name, expected_sql in expected_index_ddl.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        if (
            row is None
            or _sql_fingerprint(str(row["sql"]))
            != _sql_fingerprint(expected_sql)
        ):
            raise sqlite3.DatabaseError("invalid core schema")

    marker_rows = conn.execute(
        """SELECT version,name,status FROM schema_migrations
           WHERE version BETWEEN 1 AND 6 ORDER BY version"""
    ).fetchall()
    expected_markers = [
        (version, name, "applied")
        for version, name, _apply in CORE_MIGRATIONS
    ]
    if [tuple(row) for row in marker_rows] != expected_markers:
        raise sqlite3.DatabaseError("invalid core schema")

    core_tables = set(expected_columns)
    core_trigger_rows = conn.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type='trigger'"
    ).fetchall()
    if any(row["tbl_name"] in core_tables for row in core_trigger_rows):
        raise sqlite3.DatabaseError("invalid core schema")

    validate_kelivo_schema(conn, version=4)
    validate_heartbeat_schema(conn)
    validate_heartbeat_hardening_schema(conn)

    if require_relay_tables:
        relay_columns = {
            "messages": {
                "id": ("INTEGER", 0, None, 1),
                "ts": ("TEXT", 1, None, 0),
                "direction": ("TEXT", 1, None, 0),
                "kind": ("TEXT", 1, None, 0),
                "text": ("TEXT", 1, None, 0),
                "meta": ("TEXT", 1, "'{}'", 0),
            },
            "push_subscriptions": {
                "endpoint": ("TEXT", 0, None, 1),
                "p256dh": ("TEXT", 1, None, 0),
                "auth": ("TEXT", 1, None, 0),
                "ua": ("TEXT", 0, None, 0),
                "created": ("TEXT", 1, None, 0),
                "last_ok": ("TEXT", 0, None, 0),
            },
        }
        for table, expected in relay_columns.items():
            validate_columns(table, expected)
            actual_fks = conn.execute(
                f"PRAGMA foreign_key_list({table})"
            ).fetchall()
            if actual_fks:
                raise sqlite3.DatabaseError("invalid core schema")
        if conn.execute("PRAGMA index_list(messages)").fetchall():
            raise sqlite3.DatabaseError("invalid core schema")
        push_indexes = {
            row["name"]: row
            for row in conn.execute("PRAGMA index_list(push_subscriptions)")
        }
        if set(push_indexes) != {"sqlite_autoindex_push_subscriptions_1"}:
            raise sqlite3.DatabaseError("invalid core schema")
        push_index = push_indexes["sqlite_autoindex_push_subscriptions_1"]
        if (
            bool(push_index["unique"]),
            push_index["origin"],
            bool(push_index["partial"]),
        ) != (True, "pk", False):
            raise sqlite3.DatabaseError("invalid core schema")
        _validate_index_xinfo(
            conn, "sqlite_autoindex_push_subscriptions_1", ("endpoint",),
        )
        for table, expected_sql in RELAY_TABLE_DDL.items():
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if (
                row is None
                or _sql_fingerprint(str(row["sql"]))
                != _sql_fingerprint(expected_sql)
            ):
                raise sqlite3.DatabaseError("invalid core schema")


def validate_memory_schema(conn: sqlite3.Connection) -> None:
    """Reject a v7 marker unless the complete Memory Core structure is exact."""
    expected_columns = {
        "memory_items": {
            "id": ("INTEGER", 0, None, 1),
            "memory_key": ("TEXT", 1, None, 0),
            "kind": ("TEXT", 1, None, 0),
            "scope_type": ("TEXT", 1, None, 0),
            "scope_ref": ("TEXT", 1, None, 0),
            "normalized_content": ("TEXT", 0, None, 0),
            "normalized_fingerprint": ("BLOB", 0, None, 0),
            "fingerprint_version": ("INTEGER", 1, None, 0),
            "status": ("TEXT", 1, None, 0),
            "explicitness": ("TEXT", 1, None, 0),
            "confidence": ("REAL", 1, None, 0),
            "sensitivity": ("TEXT", 1, None, 0),
            "first_observed_at": ("TEXT", 1, None, 0),
            "last_confirmed_at": ("TEXT", 1, None, 0),
            "superseded_by_id": ("INTEGER", 0, None, 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
        "memory_fingerprint_profile": {
            "singleton": ("INTEGER", 0, None, 1),
            "key_id": ("TEXT", 1, None, 0),
            "key_check": ("BLOB", 1, None, 0),
            "normalization_version": ("INTEGER", 1, None, 0),
            "fingerprint_version": ("INTEGER", 1, None, 0),
            "created_at": ("TEXT", 1, None, 0),
            "updated_at": ("TEXT", 1, None, 0),
        },
        "memory_evidence_events": {
            "id": ("INTEGER", 0, None, 1),
            "canonical_message_id": ("INTEGER", 1, None, 0),
            "action_id": ("TEXT", 1, None, 0),
            "action_type": ("TEXT", 1, None, 0),
            "action_binding_version": ("INTEGER", 1, None, 0),
            "evidence_type": ("TEXT", 1, None, 0),
            "reality_scope": ("TEXT", 1, None, 0),
            "subject_scope": ("TEXT", 1, None, 0),
            "created_by_component": ("TEXT", 1, None, 0),
            "created_at": ("TEXT", 1, None, 0),
        },
        "memory_sources": {
            "id": ("INTEGER", 0, None, 1),
            "memory_id": ("INTEGER", 1, None, 0),
            "canonical_message_id": ("INTEGER", 1, None, 0),
            "evidence_event_id": ("INTEGER", 1, None, 0),
            "channel": ("TEXT", 1, None, 0),
            "source": ("TEXT", 1, "''", 0),
            "evidence_role": ("TEXT", 1, None, 0),
            "evidence_type": ("TEXT", 1, None, 0),
            "created_at": ("TEXT", 1, None, 0),
        },
        "memory_suppressions": {
            "id": ("INTEGER", 0, None, 1),
            "scope_type": ("TEXT", 1, None, 0),
            "scope_ref": ("TEXT", 1, None, 0),
            "kind": ("TEXT", 1, None, 0),
            "normalized_fingerprint": ("BLOB", 1, None, 0),
            "fingerprint_version": ("INTEGER", 1, None, 0),
            "reason_category": ("TEXT", 1, None, 0),
            "created_at": ("TEXT", 1, None, 0),
        },
    }
    for table, expected in expected_columns.items():
        rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
        if any(int(row["hidden"]) != 0 for row in rows):
            raise sqlite3.DatabaseError(f"invalid hidden memory column: {table}")
        actual = {
            row["name"]: (
                str(row["type"]).upper(), int(row["notnull"]), row["dflt_value"], int(row["pk"]),
            )
            for row in rows
        }
        if actual != expected:
            raise sqlite3.DatabaseError(f"invalid memory schema: {table} columns")

    expected_indexes = {
        "memory_items": {
            "sqlite_autoindex_memory_items_1": (
                True, "u", False, ("memory_key",),
            ),
            "idx_memory_items_active_lookup": (
                False, "c", False,
                ("status", "scope_type", "scope_ref", "kind", "sensitivity",
                 "last_confirmed_at", "id"),
            ),
            "idx_memory_items_superseded_by": (
                False, "c", False, ("superseded_by_id",),
            ),
            "idx_memory_items_live_fingerprint": (
                True, "c", True,
                ("scope_type", "scope_ref", "kind", "fingerprint_version",
                 "normalized_fingerprint"),
            ),
        },
        "memory_fingerprint_profile": {},
        "memory_evidence_events": {
            "sqlite_autoindex_memory_evidence_events_1": (
                True, "u", False, ("canonical_message_id",),
            ),
            "sqlite_autoindex_memory_evidence_events_2": (
                True, "u", False, ("action_id",),
            ),
            "sqlite_autoindex_memory_evidence_events_3": (
                True, "u", False, ("id", "canonical_message_id", "evidence_type"),
            ),
        },
        "memory_sources": {
            "sqlite_autoindex_memory_sources_1": (
                True, "u", False, ("memory_id", "evidence_event_id"),
            ),
            "idx_memory_sources_memory": (
                False, "c", False, ("memory_id", "id"),
            ),
            "idx_memory_sources_canonical": (
                False, "c", False, ("canonical_message_id", "id"),
            ),
        },
        "memory_suppressions": {
            "sqlite_autoindex_memory_suppressions_1": (
                True, "u", False,
                ("scope_type", "scope_ref", "kind", "fingerprint_version",
                 "normalized_fingerprint"),
            ),
        },
    }
    for table, expected in expected_indexes.items():
        actual_rows = {row["name"]: row for row in conn.execute(f"PRAGMA index_list({table})")}
        if set(actual_rows) != set(expected):
            raise sqlite3.DatabaseError(f"invalid memory index set: {table}")
        for name, (unique, origin, partial, columns) in expected.items():
            row = actual_rows[name]
            if (bool(row["unique"]), row["origin"], bool(row["partial"])) != (
                unique, origin, partial,
            ):
                raise sqlite3.DatabaseError(f"invalid memory index attributes: {name}")
            _validate_index_xinfo(conn, name, columns)

    expected_fks = {
        "memory_items": {
            ("superseded_by_id", "memory_items", "id", "NO ACTION", "RESTRICT", "NONE"),
        },
        "memory_fingerprint_profile": set(),
        "memory_evidence_events": {
            ("canonical_message_id", "messages", "id", "NO ACTION", "RESTRICT", "NONE"),
        },
        "memory_sources": {
            ("memory_id", "memory_items", "id", "NO ACTION", "RESTRICT", "NONE"),
            ("canonical_message_id", "messages", "id", "NO ACTION", "RESTRICT", "NONE"),
            (
                "evidence_event_id", "memory_evidence_events", "id",
                "NO ACTION", "RESTRICT", "NONE",
            ),
            (
                "canonical_message_id", "memory_evidence_events", "canonical_message_id",
                "NO ACTION", "RESTRICT", "NONE",
            ),
            (
                "evidence_type", "memory_evidence_events", "evidence_type",
                "NO ACTION", "RESTRICT", "NONE",
            ),
        },
        "memory_suppressions": set(),
    }
    for table, expected in expected_fks.items():
        actual = {
            (row["from"], row["table"], row["to"], row["on_update"], row["on_delete"], row["match"])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }
        if actual != expected:
            raise sqlite3.DatabaseError(f"invalid memory foreign key: {table}")

    for table, expected_sql in MEMORY_TABLE_DDL.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone()
        if row is None or _sql_fingerprint(str(row["sql"])) != _sql_fingerprint(expected_sql):
            raise sqlite3.DatabaseError(f"invalid memory table fingerprint: {table}")
    for name, expected_sql in MEMORY_INDEX_DDL.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,),
        ).fetchone()
        if row is None or _sql_fingerprint(str(row["sql"])) != _sql_fingerprint(expected_sql):
            raise sqlite3.DatabaseError(f"invalid memory index fingerprint: {name}")
    actual_triggers = {
        row["name"]: row["sql"]
        for row in conn.execute(
            """SELECT name,sql FROM sqlite_master
               WHERE type='trigger' AND tbl_name='memory_evidence_events'"""
        )
    }
    if set(actual_triggers) != set(MEMORY_TRIGGER_DDL):
        raise sqlite3.DatabaseError("invalid memory trigger set")
    for name, expected_sql in MEMORY_TRIGGER_DDL.items():
        if _sql_fingerprint(str(actual_triggers[name])) != _sql_fingerprint(expected_sql):
            raise sqlite3.DatabaseError(f"invalid memory trigger fingerprint: {name}")


def validate_memory_action_schema(conn: sqlite3.Connection) -> None:
    """Reject a v8 marker unless the additive action ledger is exact."""
    validate_memory_schema(conn)
    marker = conn.execute(
        """SELECT name,status FROM schema_migrations
           WHERE version=8"""
    ).fetchone()
    if (
        marker is None
        or marker["name"] != "explicit_memory_action_request_ledger"
        or marker["status"] != "applied"
    ):
        raise sqlite3.DatabaseError("invalid memory action migration marker")

    rows = conn.execute("PRAGMA table_xinfo(memory_action_requests)").fetchall()
    if any(int(row["hidden"]) != 0 for row in rows):
        raise sqlite3.DatabaseError("invalid hidden memory action column")
    actual_columns = {
        row["name"]: (
            str(row["type"]).upper(),
            int(row["notnull"]),
            row["dflt_value"],
            int(row["pk"]),
        )
        for row in rows
    }
    expected_columns = {
        "request_id": ("TEXT", 1, None, 1),
        "action_kind": ("TEXT", 1, None, 0),
        "origin": ("TEXT", 1, None, 0),
        "request_binding_digest": ("BLOB", 1, None, 0),
        "target_memory_key": ("TEXT", 0, None, 0),
        "canonical_message_id": ("INTEGER", 0, None, 0),
        "result_memory_key": ("TEXT", 0, None, 0),
        "status": ("TEXT", 1, None, 0),
        "result_category": ("TEXT", 1, None, 0),
        "created_at": ("TEXT", 1, None, 0),
        "updated_at": ("TEXT", 1, None, 0),
    }
    if actual_columns != expected_columns:
        raise sqlite3.DatabaseError("invalid memory action schema columns")

    actual_indexes = {
        row["name"]: row
        for row in conn.execute("PRAGMA index_list(memory_action_requests)")
    }
    expected_indexes = {
        "sqlite_autoindex_memory_action_requests_1": (
            True, "pk", False, ("request_id",),
        ),
        "sqlite_autoindex_memory_action_requests_2": (
            True, "u", False, ("canonical_message_id",),
        ),
        "idx_memory_action_requests_status_created": (
            False, "c", False, ("status", "created_at", "request_id"),
        ),
    }
    if set(actual_indexes) != set(expected_indexes):
        raise sqlite3.DatabaseError("invalid memory action index set")
    for name, (unique, origin, partial, columns) in expected_indexes.items():
        row = actual_indexes[name]
        if (
            bool(row["unique"]),
            row["origin"],
            bool(row["partial"]),
        ) != (unique, origin, partial):
            raise sqlite3.DatabaseError("invalid memory action index attributes")
        try:
            _validate_index_xinfo(conn, name, columns)
        except sqlite3.DatabaseError:
            raise sqlite3.DatabaseError(
                "invalid memory action index columns"
            ) from None

    actual_fks = {
        (
            row["from"],
            row["table"],
            row["to"],
            row["on_update"],
            row["on_delete"],
            row["match"],
        )
        for row in conn.execute(
            "PRAGMA foreign_key_list(memory_action_requests)"
        )
    }
    if actual_fks != {
        (
            "canonical_message_id",
            "messages",
            "id",
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
    }:
        raise sqlite3.DatabaseError("invalid memory action foreign key")

    table = conn.execute(
        """SELECT sql FROM sqlite_master
           WHERE type='table' AND name='memory_action_requests'"""
    ).fetchone()
    if (
        table is None
        or _sql_fingerprint(str(table["sql"]))
        != _sql_fingerprint(MEMORY_ACTION_REQUEST_TABLE_DDL)
    ):
        raise sqlite3.DatabaseError("invalid memory action table fingerprint")
    for name, expected_sql in MEMORY_ACTION_REQUEST_INDEX_DDL.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        if (
            row is None
            or _sql_fingerprint(str(row["sql"]))
            != _sql_fingerprint(expected_sql)
        ):
            raise sqlite3.DatabaseError("invalid memory action index fingerprint")

    unexpected = {
        (row["type"], row["name"])
        for row in conn.execute(
            """SELECT type,name FROM sqlite_master
               WHERE (name LIKE 'memory_action_%'
                      OR name LIKE 'idx_memory_action_%')
                 AND name NOT LIKE 'sqlite_autoindex_%'"""
        )
    }
    expected_objects = {
        ("table", "memory_action_requests"),
        ("index", "idx_memory_action_requests_status_created"),
        ("trigger", "memory_action_requests_immutable_delete"),
        ("trigger", "memory_action_requests_immutable_update"),
    }
    if unexpected != expected_objects:
        raise sqlite3.DatabaseError("invalid memory action object set")
    actual_triggers = {
        row["name"]: row["sql"]
        for row in conn.execute(
            """SELECT name,sql FROM sqlite_master
           WHERE type='trigger' AND tbl_name='memory_action_requests'"""
        )
    }
    if set(actual_triggers) != set(MEMORY_ACTION_REQUEST_TRIGGER_DDL):
        raise sqlite3.DatabaseError("invalid memory action trigger set")
    for name, expected_sql in MEMORY_ACTION_REQUEST_TRIGGER_DDL.items():
        if (
            _sql_fingerprint(str(actual_triggers[name]))
            != _sql_fingerprint(expected_sql)
        ):
            raise sqlite3.DatabaseError(
                f"invalid memory action trigger fingerprint: {name}"
            )


MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "telegram_private_text_mvp", _migration_001),
    (2, "telegram_reliability", _migration_002),
    (3, "kelivo_nonstream_foundation", _migration_003),
    (4, "kelivo_automatic_idempotency", _migration_004),
    (5, "dylan_heartbeat_foundation", _migration_005),
    (6, "dylan_heartbeat_hardening", _migration_006),
    (7, "explicit_memory_core_foundation", _migration_007),
    (8, "explicit_memory_action_request_ledger", _migration_008),
)
CORE_MIGRATIONS = MIGRATIONS[:6]


def run_migrations(path: str, migrations: Iterable[tuple[int, str, Callable[[sqlite3.Connection], None]]] = MIGRATIONS) -> None:
    """Apply versions under a SQLite write lock; concurrent starters re-read state."""
    migrations = tuple(migrations)
    requested_latest = max((version for version, _name, _apply in migrations), default=0)
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            for version, name, apply in migrations:
                row = conn.execute("SELECT name,status FROM schema_migrations WHERE version=?", (version,)).fetchone()
                if row:
                    if row["status"] == "applied" and row["name"] == name:
                        continue
                    raise sqlite3.DatabaseError("invalid migration state")
                apply(conn)
                stamp = now_iso()
                conn.execute(
                    "INSERT INTO schema_migrations(version,name,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (version, name, "applied", stamp, stamp),
                )
            if requested_latest >= 3:
                validate_kelivo_schema(conn, version=4 if requested_latest >= 4 else 3)
            if requested_latest >= 5:
                validate_heartbeat_schema(conn)
            if requested_latest >= 6:
                validate_core_schema_v1_v6(conn)
            if requested_latest >= 7:
                validate_memory_schema(conn)
            if requested_latest >= 8:
                validate_memory_action_schema(conn)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def audit_id(secret: str, channel: str, account_id: str, external_id: str) -> str:
    value = f"{channel}\x1f{account_id}\x1f{external_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), value, hashlib.sha256).hexdigest()[:24]


def audit(conn: sqlite3.Connection, event_type: str, channel: str, account_id: str = "",
          external_id: str = "", request_job_id: str = "", status: str = "recorded",
          error_category: str | None = None) -> None:
    secret = os.environ.get("CHANNEL_AUDIT_HMAC_SECRET", "")
    external_hash = audit_id(secret, channel, account_id, external_id) if secret and external_id else None
    stamp = now_iso()
    conn.execute(
        """INSERT INTO channel_audit_events
           (event_type,channel,external_id_hash,request_job_id,status,error_category,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (event_type, channel, external_hash, request_job_id, status, error_category, stamp, stamp),
    )


def get_or_create_conversation(conn: sqlite3.Connection, channel: str, account_id: str,
                               conversation_id: str, conversation_type: str = "private",
                               external_user_id: str = "") -> sqlite3.Row:
    stamp = now_iso()
    conn.execute("""INSERT OR IGNORE INTO channel_accounts
        (channel,external_account_id,status,created_at,updated_at) VALUES(?,?,?,?,?)""",
        (channel, account_id, "active", stamp, stamp))
    row = conn.execute("""SELECT * FROM channel_conversations WHERE channel=?
        AND external_account_id=? AND external_conversation_id=?""",
        (channel, account_id, conversation_id)).fetchone()
    if row:
        if external_user_id and row["external_user_id"] != external_user_id:
            conn.execute("UPDATE channel_conversations SET external_user_id=?,updated_at=? WHERE id=?",
                         (external_user_id, stamp, row["id"]))
            row = conn.execute("SELECT * FROM channel_conversations WHERE id=?", (row["id"],)).fetchone()
        return row
    api_session = "api-tg-" + secrets.token_urlsafe(24)
    conn.execute("""INSERT INTO channel_conversations
        (channel,external_account_id,external_conversation_id,conversation_type,api_session,status,created_at,updated_at,external_user_id)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (channel, account_id, conversation_id, conversation_type, api_session, "active", stamp, stamp, external_user_id))
    return conn.execute("SELECT * FROM channel_conversations WHERE api_session=?", (api_session,)).fetchone()


def rate_limit_allowed(conn: sqlite3.Connection, channel: str, account_id: str, user_id: str,
                       limit: int, window_seconds: int) -> bool:
    now = datetime.now(timezone.utc); stamp = now.isoformat()
    row = conn.execute("""SELECT * FROM channel_rate_limits WHERE channel=?
        AND external_account_id=? AND external_user_id=?""", (channel, account_id, user_id)).fetchone()
    if not row:
        conn.execute("""INSERT INTO channel_rate_limits
            (channel,external_account_id,external_user_id,window_started_at,event_count,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)""", (channel, account_id, user_id, stamp, 1, "active", stamp, stamp))
        return True
    try:
        started = datetime.fromisoformat(row["window_started_at"])
    except ValueError:
        started = now - timedelta(seconds=window_seconds + 1)
    if now - started >= timedelta(seconds=window_seconds):
        conn.execute("UPDATE channel_rate_limits SET window_started_at=?,event_count=1,updated_at=? WHERE id=?",
                     (stamp, stamp, row["id"])); return True
    if int(row["event_count"]) >= limit:
        return False
    conn.execute("UPDATE channel_rate_limits SET event_count=event_count+1,updated_at=? WHERE id=?", (stamp, row["id"]))
    return True


def enqueue_telegram_update(path: str, *, account_id: str, update_id: str, chat_id: str,
                            user_id: str, external_message_id: str, text: str,
                            rate_limit: int, rate_window_seconds: int) -> dict:
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute("""SELECT id,status,error_category FROM inbound_events
                WHERE channel='telegram' AND external_account_id=? AND update_id=?""",
                (account_id, update_id)).fetchone()
            if existing:
                conn.execute("COMMIT")
                return {"duplicate": True, "ignored": existing["status"] == "rejected",
                        "reason": existing["error_category"] or "duplicate", "event_id": existing["id"]}
            existing_message = conn.execute("""SELECT id FROM external_messages WHERE channel='telegram'
                AND external_account_id=? AND external_conversation_id=? AND external_message_id=?""",
                (account_id, chat_id, external_message_id)).fetchone()
            if existing_message:
                conn.execute("COMMIT"); return {"duplicate": True}
            stamp = now_iso()
            cur = conn.execute("""INSERT INTO inbound_events
                (channel,external_account_id,update_id,event_type,status,created_at,updated_at)
                VALUES('telegram',?,?,'message','accepted',?,?)""", (account_id, update_id, stamp, stamp))
            event_id = cur.lastrowid
            if not rate_limit_allowed(conn, "telegram", account_id, user_id, rate_limit, rate_window_seconds):
                conn.execute("UPDATE inbound_events SET status='rejected',error_category='rate_limited',updated_at=? WHERE id=?",
                             (stamp, event_id))
                audit(conn, "webhook_rejected", "telegram", account_id, user_id, str(event_id), "rejected", "rate_limited")
                conn.execute("COMMIT")
                return {"duplicate": False, "rejected": "rate_limited", "event_id": event_id}
            conversation = get_or_create_conversation(conn, "telegram", account_id, chat_id, "private", user_id)
            meta = {"user": "human", "attachments": [], "api_session": conversation["api_session"],
                    "channel": "telegram", "external_event_id": str(event_id)}
            cur = conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                               (stamp, "in", "user", text, json.dumps(meta, ensure_ascii=False)))
            canonical_id = cur.lastrowid
            cur = conn.execute("""INSERT INTO external_messages
                (channel,external_account_id,external_conversation_id,external_message_id,direction,canonical_message_id,status,created_at,updated_at)
                VALUES('telegram',?,?,?,?,?,'received',?,?)""",
                (account_id, chat_id, external_message_id, "in", canonical_id, stamp, stamp))
            inbound_id = cur.lastrowid
            cur = conn.execute("""INSERT INTO generation_jobs
                (inbound_message_id,canonical_message_id,channel,external_account_id,external_conversation_id,api_session,status,created_at,updated_at)
                VALUES(?,?,'telegram',?,?,?,'queued',?,?)""",
                (inbound_id, canonical_id, account_id, chat_id, conversation["api_session"], stamp, stamp))
            job_id = cur.lastrowid
            audit(conn, "webhook_accepted", "telegram", account_id, user_id, str(job_id), "queued")
            conn.execute("COMMIT")
            return {"duplicate": False, "event_id": event_id, "canonical_message_id": canonical_id,
                    "job_id": job_id, "api_session": conversation["api_session"],
                    "message": {"id": canonical_id, "ts": stamp, "direction": "in", "kind": "user",
                                "text": text, "meta": meta}}
        except Exception:
            if conn.in_transaction: conn.execute("ROLLBACK")
            raise


def claim_generation_job(path: str, lease_seconds: int = 180, max_attempts: int = 2) -> dict | None:
    now = datetime.now(timezone.utc); stamp = now.isoformat(); lease = (now + timedelta(seconds=lease_seconds)).isoformat()
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Exhausted safe-to-retry work becomes terminal before selection.
        conn.execute("""UPDATE generation_jobs SET status='failed',error_category='max_attempts',lease_until=NULL,updated_at=?
            WHERE status IN ('queued','processing') AND attempt_count>=?""", (stamp, max_attempts))
        row = conn.execute("""SELECT * FROM generation_jobs WHERE attempt_count<? AND
            (status='queued' OR (status='processing' AND dispatch_started_at IS NULL AND lease_until < ?))
            ORDER BY id LIMIT 1""", (max_attempts, stamp)).fetchone()
        if not row:
            conn.execute("COMMIT"); return None
        generation_id = row["generation_id"] or ("tg-gen-" + secrets.token_urlsafe(16))
        stream_id = row["stream_id"] or ("tg-stream-" + secrets.token_urlsafe(16))
        updated = conn.execute("""UPDATE generation_jobs SET status='processing',lease_until=?,attempt_count=attempt_count+1,
            generation_id=?,stream_id=?,reply_to=?,error_category=NULL,updated_at=? WHERE id=? AND attempt_count<? AND
            (status='queued' OR (status='processing' AND dispatch_started_at IS NULL AND lease_until < ?))""",
            (lease, generation_id, stream_id, str(row["canonical_message_id"]), stamp, row["id"], max_attempts, stamp))
        conn.execute("COMMIT")
        if updated.rowcount != 1: return None
    with connect(path) as conn:
        claimed = conn.execute("SELECT * FROM generation_jobs WHERE id=?", (row["id"],)).fetchone()
        return dict(claimed) if claimed else None


def start_generation_dispatch(path: str, job_id: int) -> dict | None:
    stamp = now_iso()
    with connect(path) as conn:
        result = conn.execute("""UPDATE generation_jobs SET status='dispatching',dispatch_started_at=?,lease_until=NULL,updated_at=?
            WHERE id=? AND status='processing' AND generation_id IS NOT NULL AND stream_id IS NOT NULL""",
            (stamp, stamp, job_id))
        if result.rowcount != 1: return None
        row = conn.execute("SELECT * FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row)


def finish_generation_dispatch(path: str, job_id: int, outcome: str, error_category: str | None = None) -> None:
    if outcome not in {"awaiting_reply", "failed", "dispatch_uncertain"}:
        raise ValueError("invalid dispatch outcome")
    stamp = now_iso()
    with connect(path) as conn:
        conn.execute("""UPDATE generation_jobs SET status=?,lease_until=NULL,error_category=?,
            awaiting_reply_since=CASE WHEN ?='awaiting_reply' THEN ? ELSE awaiting_reply_since END,updated_at=?
            WHERE id=? AND status='dispatching'""", (outcome, error_category, outcome, stamp, stamp, job_id))


def recover_inflight_generations(path: str) -> int:
    with connect(path) as conn:
        result = conn.execute("""UPDATE generation_jobs SET status='dispatch_uncertain',error_category='worker_restarted',
            lease_until=NULL,updated_at=? WHERE status='dispatching'""", (now_iso(),))
        return result.rowcount


def _completion_identity(job: sqlite3.Row | dict) -> str:
    correlation = job["generation_id"] or job["stream_id"]
    return f"telegram\x1f{job['external_account_id']}\x1f{correlation}\x1f{job['reply_to']}"


def _split_text(text: str, limit: int = 4096) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut + 1 if cut >= 0 else limit
        parts.append(text[:cut]); text = text[cut:]
    if text: parts.append(text)
    return parts


def complete_telegram_generation(path: str, *, meta: dict, text: str, kind: str = "reply") -> dict | None:
    """Atomically deduplicate, validate correlation, save reply, complete job, and create outbox."""
    generation_id = str(meta.get("generation_id") or "").strip()
    stream_id = str(meta.get("stream_id") or "").strip()
    session = str(meta.get("api_session") or meta.get("session_id") or "").strip()
    reply_to = str(meta.get("reply_to") or "").strip()
    account = str(meta.get("channel_account") or meta.get("account_id") or "").strip()
    conversation = str(meta.get("channel_conversation") or meta.get("conversation_id") or "").strip()
    channel = str(meta.get("channel") or "").strip()
    if (kind != "reply" or not text or channel != "telegram" or not account or not conversation or
            not session or not reply_to or not (generation_id or stream_id)):
        return None
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            where = ["channel='telegram'", "api_session=?", "reply_to=?",
                     "status IN ('processing','dispatching','dispatch_uncertain','awaiting_reply','completed')"]
            params: list[object] = [session, reply_to]
            if generation_id: where.append("generation_id=?"); params.append(generation_id)
            if stream_id: where.append("stream_id=?"); params.append(stream_id)
            if account: where.append("external_account_id=?"); params.append(account)
            if conversation: where.append("external_conversation_id=?"); params.append(conversation)
            rows = conn.execute("SELECT * FROM generation_jobs WHERE " + " AND ".join(where), params).fetchall()
            if len(rows) != 1:
                audit(conn, "reply_uncorrelated", "telegram", account, session, generation_id or stream_id,
                      "ignored", "correlation_missing")
                conn.execute("COMMIT"); return None
            job = rows[0]
            inbound = conn.execute("""SELECT * FROM external_messages WHERE id=? AND channel='telegram'
                AND external_account_id=? AND external_conversation_id=? AND direction='in'
                AND canonical_message_id=?""", (job["inbound_message_id"], job["external_account_id"],
                job["external_conversation_id"], job["canonical_message_id"])).fetchone()
            conv = conn.execute("""SELECT * FROM channel_conversations WHERE channel='telegram'
                AND external_account_id=? AND external_conversation_id=? AND api_session=?
                AND conversation_type='private' AND status='active'""", (job["external_account_id"],
                job["external_conversation_id"], session)).fetchone()
            account_row = conn.execute("""SELECT id FROM channel_accounts WHERE channel='telegram'
                AND external_account_id=? AND status='active'""", (job["external_account_id"],)).fetchone()
            if not inbound or not conv or not account_row or reply_to != str(job["canonical_message_id"]):
                audit(conn, "reply_uncorrelated", "telegram", job["external_account_id"], session,
                      generation_id or stream_id, "ignored", "correlation_mismatch")
                conn.execute("COMMIT"); return None
            identity = _completion_identity(job)
            existing = conn.execute("""SELECT c.*,m.ts,m.direction,m.kind,m.text,m.meta FROM telegram_completions c
                JOIN messages m ON m.id=c.canonical_message_id WHERE c.completion_identity=?""", (identity,)).fetchone()
            if existing:
                conn.execute("COMMIT")
                return {"duplicate": True, "message": {"id": existing["canonical_message_id"], "ts": existing["ts"],
                        "direction": existing["direction"], "kind": existing["kind"], "text": existing["text"],
                        "meta": json.loads(existing["meta"] or "{}")}, "delivery_id": existing["delivery_id"]}
            if job["status"] == "failed":
                conn.execute("COMMIT"); return None
            stamp = str(meta.get("ts") or now_iso())
            stored_meta = dict(meta)
            stored_meta.update({"channel": "telegram", "channel_account": job["external_account_id"],
                                "channel_conversation": job["external_conversation_id"]})
            cur = conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                               (stamp, "out", "reply", text, json.dumps(stored_meta, ensure_ascii=False)))
            message_id = cur.lastrowid
            updated = conn.execute("""UPDATE generation_jobs SET status='completed',reply_message_id=?,lease_until=NULL,
                error_category=NULL,updated_at=? WHERE id=? AND status IN
                ('processing','dispatching','dispatch_uncertain','awaiting_reply')""", (message_id, stamp, job["id"]))
            if updated.rowcount != 1:
                raise sqlite3.IntegrityError("generation completion state changed")
            key = "telegram:completion:" + hashlib.sha256(identity.encode()).hexdigest()
            cur = conn.execute("""INSERT INTO delivery_attempts
                (generation_job_id,idempotency_key,channel,external_account_id,external_conversation_id,payload_text,status,created_at,updated_at)
                VALUES(?,?,'telegram',?,?,?,'pending',?,?)""", (job["id"], key, job["external_account_id"],
                job["external_conversation_id"], text, stamp, stamp))
            delivery_id = cur.lastrowid
            parts = _split_text(text)
            if not parts: raise sqlite3.IntegrityError("empty reply")
            for index, part in enumerate(parts):
                conn.execute("""INSERT INTO delivery_parts
                    (delivery_id,part_index,total_parts,text_hash,text_length,payload_text,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,'pending',?,?)""", (delivery_id, index, len(parts),
                    hashlib.sha256(part.encode()).hexdigest(), len(part), part, stamp, stamp))
            conn.execute("""INSERT INTO telegram_completions
                (completion_identity,generation_job_id,canonical_message_id,delivery_id,created_at)
                VALUES(?,?,?,?,?)""", (identity, job["id"], message_id, delivery_id, stamp))
            audit(conn, "generation_completed", "telegram", job["external_account_id"],
                  job["external_conversation_id"], str(job["id"]), "completed")
            conn.execute("COMMIT")
            return {"duplicate": False, "message": {"id": message_id, "ts": stamp, "direction": "out",
                    "kind": "reply", "text": text, "meta": stored_meta}, "delivery_id": delivery_id}
        except Exception:
            if conn.in_transaction: conn.execute("ROLLBACK")
            raise


def mark_uncorrelated(path: str, meta: dict) -> None:
    with connect(path) as conn:
        audit(conn, "reply_uncorrelated", "telegram", str(meta.get("channel_account") or ""),
              str(meta.get("api_session") or ""), str(meta.get("generation_id") or meta.get("stream_id") or ""),
              "ignored", "correlation_missing")


def claim_delivery_part(path: str) -> dict | None:
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""SELECT p.*,d.external_account_id,d.external_conversation_id,d.generation_job_id,
            d.status AS delivery_status FROM delivery_parts p JOIN delivery_attempts d ON d.id=p.delivery_id
            WHERE d.status='pending' AND p.status='pending' AND NOT EXISTS
              (SELECT 1 FROM delivery_parts prior WHERE prior.delivery_id=p.delivery_id
               AND prior.part_index<p.part_index AND prior.status!='delivered')
            ORDER BY d.id,p.part_index LIMIT 1""").fetchone()
        if not row:
            conn.execute("COMMIT"); return None
        stamp = now_iso()
        conn.execute("UPDATE delivery_attempts SET status='sending',attempt_count=attempt_count+1,updated_at=? WHERE id=?",
                     (stamp, row["delivery_id"]))
        updated = conn.execute("UPDATE delivery_parts SET status='sending',updated_at=? WHERE id=? AND status='pending'",
                               (stamp, row["id"]))
        conn.execute("COMMIT")
        return dict(row) if updated.rowcount == 1 else None


def finish_delivery_part(path: str, part_id: int, status: str, telegram_message_id: str | None = None,
                         error_category: str | None = None, retry_after_seconds: int | None = None) -> None:
    stamp = now_iso()
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        part = conn.execute("SELECT * FROM delivery_parts WHERE id=?", (part_id,)).fetchone()
        if not part or part["status"] != "sending": conn.execute("COMMIT"); return
        conn.execute("""UPDATE delivery_parts SET status=?,telegram_message_id=?,error_category=?,retry_after_seconds=?,updated_at=?
            WHERE id=?""", (status, telegram_message_id, error_category, retry_after_seconds, stamp, part_id))
        delivery = conn.execute("SELECT * FROM delivery_attempts WHERE id=?", (part["delivery_id"],)).fetchone()
        job = conn.execute("SELECT * FROM generation_jobs WHERE id=?", (delivery["generation_job_id"],)).fetchone()
        if telegram_message_id:
            conn.execute("""INSERT OR IGNORE INTO external_messages
                (channel,external_account_id,external_conversation_id,external_message_id,direction,canonical_message_id,generation_id,status,created_at,updated_at)
                VALUES('telegram',?,?,?,'out',?,?,?, ?,?)""", (delivery["external_account_id"],
                delivery["external_conversation_id"], telegram_message_id, job["reply_message_id"],
                job["generation_id"], "delivered", stamp, stamp))
        remaining = conn.execute("SELECT count(*) FROM delivery_parts WHERE delivery_id=? AND status!='delivered'",
                                 (delivery["id"],)).fetchone()[0]
        if status == "delivered" and remaining == 0:
            ids = [r[0] for r in conn.execute("SELECT telegram_message_id FROM delivery_parts WHERE delivery_id=? ORDER BY part_index",
                                              (delivery["id"],))]
            conn.execute("""UPDATE delivery_attempts SET status='delivered',external_message_id=?,error_category=NULL,
                retry_after_seconds=NULL,updated_at=? WHERE id=?""", (",".join(ids), stamp, delivery["id"]))
        elif status != "delivered":
            conn.execute("""UPDATE delivery_attempts SET status=?,error_category=?,retry_after_seconds=?,updated_at=? WHERE id=?""",
                         (status, error_category, retry_after_seconds, stamp, delivery["id"]))
        else:
            conn.execute("UPDATE delivery_attempts SET status='pending',updated_at=? WHERE id=?", (stamp, delivery["id"]))
        conn.execute("COMMIT")


# Compatibility helpers retained for callers/tests while delivery is now part-based.
def claim_delivery(path: str) -> dict | None:
    return claim_delivery_part(path)


def recover_inflight_deliveries(path: str) -> int:
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        parts = conn.execute("""UPDATE delivery_parts SET status='delivery_uncertain',error_category='worker_restarted',updated_at=?
            WHERE status='sending'""", (now_iso(),)).rowcount
        conn.execute("""UPDATE delivery_attempts SET status='delivery_uncertain',error_category='worker_restarted',updated_at=?
            WHERE status='sending'""", (now_iso(),))
        conn.execute("COMMIT"); return parts
