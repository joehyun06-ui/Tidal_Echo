"""Optional read-only message-count source for the offline ledger.

The injected authority resolves live rows (including deletion checks). History
selection is read in one SQLite snapshot. No history text, memory database,
provider client, schema creation, or message mutation is involved. Viewport
virtualization is not folding or archiving. Neither has an authoritative store
yet, so those counters remain unavailable.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

from .usage_ledger import identifier


KEYS = ('total', 'visible', 'folded', 'archived', 'turns')


def unavailable(reason):
    return {'counts': dict.fromkeys(KEYS), 'provenance': {
        'source': 'relay_selected_history', 'status': 'unavailable', 'reason': reason}}


class MessageCounts:
    def __init__(self, relay_db, *, session_row):
        self.path = Path(relay_db).resolve()
        self.session_row = session_row

    def __call__(self, mode, sid, *, all_sessions=False):
        identifier(sid)
        if all_sessions:
            return unavailable('all_sessions_not_supported')
        checked = {}

        def live(value):
            row = self.session_row(value)
            if not isinstance(row, dict) or row.get('id') != value or row.get('provider') != mode:
                raise ValueError('session_unavailable')
            checked[value] = dict(row)

        try:
            live(sid)
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'messages' not in tables:
                    return unavailable('history_store_unavailable')
                version_tables = {'web_message_versions', 'web_version_selections'}
                if tables & version_tables and not version_tables <= tables:
                    return unavailable('version_store_incomplete')
                root = selected = sid
                if version_tables <= tables:
                    edge = conn.execute('SELECT root_id FROM web_message_versions WHERE version_id=?', (sid,)).fetchone()
                    root = edge[0] if edge else sid
                    live(root)
                    if conn.execute('SELECT 1 FROM web_message_versions WHERE version_id=?', (root,)).fetchone():
                        raise ValueError('version_selection_invalid')
                    selection = conn.execute('SELECT version_id FROM web_version_selections WHERE root_id=?', (root,)).fetchone()
                    selected = selection[0] if selection else root
                    # Validate the selected alternative's full parent chain, not
                    # every other alternative or every row in the conversation.
                    cursor, seen = selected, set()
                    while cursor != root:
                        if cursor in seen or len(seen) >= 256:
                            raise ValueError('version_selection_invalid')
                        seen.add(cursor)
                        live(cursor)
                        edge = conn.execute('SELECT root_id,parent_id,mode FROM web_message_versions WHERE version_id=?', (cursor,)).fetchone()
                        if not edge or edge[0] != root or edge[2] not in ('edit', 'regenerate'):
                            raise ValueError('version_selection_invalid')
                        cursor = edge[1]
                live(selected)
                # Aggregate all selected-history rows, independent of paging,
                # DOM limits, model filters and number of provider requests.
                # Prefix copies are visible history and counted exactly once.
                total, turns, copied = conn.execute("""
                    SELECT COUNT(*),
                           COALESCE(SUM(CASE WHEN kind IN ('user','voice') AND direction='in' THEN 1 ELSE 0 END),0),
                           COALESCE(SUM(CASE WHEN json_extract(meta,'$.version_copy')=1 OR
                               json_extract(meta,'$.branch_origin.session_id') IS NOT NULL THEN 1 ELSE 0 END),0)
                    FROM messages WHERE json_valid(meta) AND json_extract(meta,'$.api_session')=?
                    AND (kind IN ('user','voice') AND direction='in' OR kind='reply' AND direction='out')
                """, (selected,)).fetchone()
                # Authority lives outside this database. A concurrent deletion
                # or provider change invalidates the entire snapshot.
                if any(self.session_row(key) != row for key, row in checked.items()):
                    return unavailable('session_changed')
                return {'counts': {'total': total, 'visible': total, 'folded': None, 'archived': None, 'turns': turns},
                        'provenance': {'source': 'relay_selected_history', 'status': 'available',
                                       'root_session_id': root, 'selected_session_id': selected,
                                       'copied_messages': copied, 'folded_supported': False,
                                       'archived_supported': False, 'turn_basis': 'incoming_user_or_voice'}}
        except ValueError as exc:
            return unavailable(str(exc))
        except (sqlite3.Error, OSError):
            return unavailable('history_store_unavailable')
