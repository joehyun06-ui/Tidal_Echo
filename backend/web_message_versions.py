"""Durable message alternatives inside one visible conversation.

Each alternative keeps an isolated provider-native context. Only explicit forks
are new visible conversations. Relationships contain IDs, never message content.
Reserve before forking so a lost response cannot publish an untracked alternative.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing

from .web_session_fork import ForkError


def connect(path):
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS web_message_versions ("
                 "version_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, parent_id TEXT NOT NULL, "
                 "mode TEXT NOT NULL, message_id INTEGER NOT NULL, point_key INTEGER NOT NULL, prompt_key INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS web_version_selections (root_id TEXT PRIMARY KEY, version_id TEXT NOT NULL)")
    return conn


def messages(conn, session_id):
    return conn.execute("SELECT * FROM messages WHERE json_extract(meta,'$.api_session')=? "
                        "AND kind IN ('user','voice','reply') ORDER BY id LIMIT 5001", (session_id,)).fetchall()


def message_key(conn, session_id, row):
    meta = json.loads(row['meta'])
    if type(meta.get('version_key')) is int:
        return meta['version_key']
    edge = conn.execute("SELECT * FROM web_message_versions WHERE version_id=?", (session_id,)).fetchone()
    if edge:
        # The first new pair is the alternative; later turns have their own IDs.
        fresh = [r for r in messages(conn, session_id) if not json.loads(r['meta']).get('version_copy')]
        first = next((r for r in fresh if r['direction'] == row['direction']), None)
        if first and first['id'] == row['id']:
            if row['direction'] == 'in':
                return edge['prompt_key'] if edge['mode'] == 'regenerate' else edge['point_key']
            if edge['mode'] == 'regenerate':
                return edge['point_key']
    return row['id']


def prepare(authority, session_id, body, *, relay_db):
    if not isinstance(body, dict) or set(body) != {'request_id', 'message_id', 'mode'}:
        raise ForkError('invalid_version_request', 400)
    try:
        request_id = uuid.UUID(body['request_id']).hex
    except (ValueError, TypeError, AttributeError):
        raise ForkError('invalid_version_request', 400) from None
    if type(body['message_id']) is not int or body['message_id'] <= 0 or body['mode'] not in {'edit', 'regenerate'}:
        raise ForkError('invalid_version_request', 400)
    source = authority.row_for_session(session_id)
    if not source:
        raise ForkError('web_session_not_found', 404)
    target = source['provider'] + '-fork-' + request_id
    if authority.tombstone_for_session(target):
        raise ForkError('web_session_deleted', 410)
    with closing(connect(relay_db)) as conn:
        previous = conn.execute('SELECT * FROM web_message_versions WHERE version_id=?', (target,)).fetchone()
        if previous:
            if (previous['parent_id'], previous['message_id'], previous['mode']) != (session_id, body['message_id'], body['mode']):
                raise ForkError('version_request_conflict')
            return dict(previous)
        if authority.row_for_session(target):
            raise ForkError('version_request_conflict')
        rows = messages(conn, session_id)
        if len(rows) > 5000:
            raise ForkError('fork_history_too_large', 413)
        chosen = next((r for r in rows if r['id'] == body['message_id']), None)
        expected = 'in' if body['mode'] == 'edit' else 'out'
        if not chosen or chosen['direction'] != expected:
            raise ForkError('fork_message_role_invalid')
        prompt = next((r for r in reversed(rows) if r['id'] <= chosen['id'] and r['direction'] == 'in'), None)
        if not prompt:
            raise ForkError('fork_prompt_missing')
        parent = conn.execute('SELECT root_id FROM web_message_versions WHERE version_id=?', (session_id,)).fetchone()
        root_id = parent['root_id'] if parent else session_id
        root = authority.row_for_session(root_id)
        if not root or root['provider'] != source['provider']:
            raise ForkError('version_root_unavailable')
        edge = dict(version_id=target, root_id=root_id, parent_id=session_id, mode=body['mode'],
                    message_id=chosen['id'], point_key=message_key(conn, session_id, chosen),
                    prompt_key=message_key(conn, session_id, prompt))
        with conn:
            conn.execute('INSERT INTO web_message_versions VALUES (?,?,?,?,?,?,?)', tuple(edge.values()))
        return edge


def finish(authority, target, *, relay_db, select=False):
    """Annotate copied prefixes; safe on replay and after a process restart."""
    with closing(connect(relay_db)) as conn:
        edge = conn.execute('SELECT * FROM web_message_versions WHERE version_id=?', (target,)).fetchone()
        if not edge or not authority.row_for_session(target):
            return
        with conn:
            for row in messages(conn, target):
                meta = json.loads(row['meta'])
                origin = meta.get('branch_origin', {})
                if meta.get('version_copy') or origin.get('session_id') != edge['parent_id']:
                    continue
                original = conn.execute("SELECT * FROM messages WHERE id=? AND json_extract(meta,'$.api_session')=?",
                                        (origin.get('message_id'), edge['parent_id'])).fetchone()
                if not original:
                    raise ForkError('version_history_unavailable')
                original_meta = json.loads(original['meta'])
                meta.update(version_copy=True, version_key=message_key(conn, edge['parent_id'], original))
                api = original_meta.get('api')
                usage = original_meta.get('version_usage') or (api.get('usage') if isinstance(api, dict) else None) or original_meta.get('usage')
                if isinstance(usage, dict):
                    meta['version_usage'] = usage
                conn.execute('UPDATE messages SET meta=? WHERE id=?', (json.dumps(meta, ensure_ascii=False), row['id']))
            if select:
                conn.execute('INSERT OR REPLACE INTO web_version_selections VALUES (?,?)', (edge['root_id'], target))


def public_state(authority, *, relay_db):
    with closing(connect(relay_db)) as conn:
        edges = [dict(r) for r in conn.execute('SELECT * FROM web_message_versions ORDER BY rowid')]
        selections = dict(conn.execute('SELECT root_id,version_id FROM web_version_selections'))
    rows = {r['id']: r for r in authority.session_rows()}
    visible = []
    for edge in edges:
        root, target = rows.get(edge['root_id']), rows.get(edge['version_id'])
        if root and target and root['provider'] == target['provider']:
            finish(authority, edge['version_id'], relay_db=relay_db)
            visible.append(edge)
    valid = {e['version_id']: e['root_id'] for e in visible}
    return {'ok': True, 'versions': visible, 'selections': [
        {'root_id': root, 'version_id': target} for root, target in selections.items()
        if root in rows and (target == root or valid.get(target) == root)]}


def select_version(authority, root_id, body, *, relay_db):
    if not isinstance(body, dict) or set(body) != {'version_id'} or not isinstance(body['version_id'], str):
        raise ForkError('invalid_version_selection', 400)
    state = public_state(authority, relay_db=relay_db)
    root = authority.row_for_session(root_id)
    target = authority.row_for_session(body['version_id'])
    if (not root or not target or any(e['version_id'] == root_id for e in state['versions']) or root['provider'] != target['provider'] or
            (target['id'] != root_id and not any(e['root_id'] == root_id and e['version_id'] == target['id'] for e in state['versions']))):
        raise ForkError('version_selection_conflict')
    with closing(connect(relay_db)) as conn, conn:
        conn.execute('INSERT OR REPLACE INTO web_version_selections VALUES (?,?)', (root_id, target['id']))
    return {'ok': True, 'root_id': root_id, 'version_id': target['id']}


def delete_versions(authority, root_id, *, relay_db, upload_dir=None, codex_store=None):
    """Delete every internal alternative, keeping explicit branches independent.

    The caller holds the session lock and, for Codex, the runtime activity gate.
    Preflight all native jobs before retirement/deletion. Root is removed last,
    so a storage failure can be retried without losing the group membership.
    """
    from . import codex_generation_store as store
    from .web_session_delete import delete_conversation
    with closing(connect(relay_db)) as conn:
        if conn.execute('SELECT 1 FROM web_message_versions WHERE version_id=?', (root_id,)).fetchone():
            raise ForkError('version_root_required')
        ids = [r[0] for r in conn.execute('SELECT version_id FROM web_message_versions WHERE root_id=? ORDER BY rowid DESC', (root_id,))]
    ids.append(root_id)
    live = [r for sid in ids if (r := authority.row_for_session(sid))]
    native = [r for r in live if r['provider'] == 'codex']
    if native:
        if not codex_store:
            raise ForkError('codex_generation_unavailable')
        with closing(store.connect(codex_store)) as conn:
            statuses = sorted(store.ACTIVE_JOB_STATUSES)
            for row in native:
                if not store.get_session(codex_store, row['id']):
                    raise ForkError('codex_session_unavailable')
                active = conn.execute('SELECT 1 FROM codex_generation_jobs WHERE api_session=? AND status IN (' + ','.join('?' for _ in statuses) + ') LIMIT 1', (row['id'], *statuses)).fetchone()
                if active:
                    raise ForkError('web_session_delete_job_active')
        for row in native:
            store.retire_session(codex_store, api_session=row['id'])
    deleted_ids = []
    for sid in ids:
        if not authority.row_for_session(sid) and not authority.tombstone_for_session(sid):
            continue  # A reserved alternative may never have been published.
        result = delete_conversation(authority, sid, relay_db=relay_db, upload_dir=upload_dir, codex_store=codex_store)
        if sid == root_id or any(r['id'] == sid for r in live):
            deleted_ids.append(sid)
    if root_id not in deleted_ids:
        raise ForkError('web_session_not_found', 404)
    return {**result, 'deleted_ids': deleted_ids}
