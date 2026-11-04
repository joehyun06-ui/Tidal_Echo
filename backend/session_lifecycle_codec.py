"""Strict, bounded v1 lifecycle storage format. Checksums are NOT signatures."""
from dataclasses import asdict, fields
import hashlib
import json
import re

from .g3_projection import _valid_id
from .g3_projection_contract import G3SourceRef
from .session_lifecycle import LifecycleError, LifecycleReceipt, LifecycleRecord, SourceCursor, _session_id

MAX_BYTES = 256 * 1024
HEX = re.compile(r'[0-9a-f]{64}\Z')


def corrupt():
    raise LifecycleError('lifecycle_storage_corrupt')


def integer(value, low=0, high=2**63-1):
    if type(value) is not int or not low <= value <= high:
        corrupt()


def identifiers(value, maximum=128):
    if (not isinstance(value, tuple) or len(value) > maximum
            or not all(_valid_id(v) for v in value) or len(set(value)) != len(value)):
        corrupt()


def hex_value(value):
    if not isinstance(value, str) or HEX.fullmatch(value) is None:
        corrupt()


def validate_record(state):
    if (not isinstance(state, LifecycleRecord) or not _session_id(state.conversation_id)
            or not _session_id(state.version_id) or state.provider not in ('api', 'codex')
            or state.phase not in ('closed', 'prepared', 'active')):
        corrupt()
    integer(state.revision, 1); integer(state.context_epoch); integer(state.message_cursor)
    hex_value(state.access_binding)
    if not isinstance(state.source_cursor, SourceCursor) or not _valid_id(state.source_cursor.generation):
        corrupt()
    integer(state.source_cursor.position)
    if not all(_valid_id(v) for v in (state.window_id, state.model_run_id, state.closeout_id, state.closeout_boundary_stamp)):
        corrupt()
    for value in (state.pending, state.used_windows, state.used_model_runs):
        identifiers(value)
    if state.window_id not in state.used_windows or state.model_run_id not in state.used_model_runs:
        corrupt()
    identifiers(state.closeout_evidence_candidates, 24)
    if state.closeout_checks != ('stream_idle', 'tool_idle', 'dispatch_known'):
        corrupt()
    if not isinstance(state.active_state, tuple) or len(state.active_state) > 16:
        corrupt()
    keys = []
    for item in state.active_state:
        if (not isinstance(item, tuple) or len(item) != 2 or not _valid_id(item[0])
                or not isinstance(item[1], str) or len(item[1]) > 1024):
            corrupt()
        keys.append(item[0])
    if len(set(keys)) != len(keys):
        corrupt()
    if (not isinstance(state.summary_candidates, tuple) or len(state.summary_candidates) > 8
            or any(not isinstance(v, str) or len(v) > 2048 for v in state.summary_candidates)):
        corrupt()
    seen = set()
    for refs, maximum in ((state.anchor_refs, 8), (state.recent_refs, 16)):
        if not isinstance(refs, tuple) or len(refs) > maximum:
            corrupt()
        for ref in refs:
            if (not isinstance(ref, G3SourceRef)
                    or not all(_valid_id(v) for v in (ref.event_id, ref.source_id, ref.scope))
                    or ref.event_id in seen):
                corrupt()
            integer(ref.revision, 1); hex_value(ref.source_sha256); seen.add(ref.event_id)
    if not isinstance(state.message_refs, tuple) or len(state.message_refs) > 128:
        corrupt()
    previous = 0
    for mid in state.message_refs:
        integer(mid, previous + 1, state.message_cursor); previous = mid
    integer(state.packet_budget_bytes, 1, 65536)
    if state.phase == 'prepared':
        if not _valid_id(state.target_window_id) or state.target_window_id in state.used_windows:
            corrupt()
    elif state.target_window_id is not None:
        corrupt()
    if state.phase == 'closed':
        if state.packet_sha256 is not None or state.anchor_refs or state.recent_refs or state.message_refs:
            corrupt()
    else:
        hex_value(state.packet_sha256)
    return state


def validate_receipt(receipt):
    if not isinstance(receipt, LifecycleReceipt) or not _valid_id(receipt.request_id):
        corrupt()
    hex_value(receipt.input_sha256); integer(receipt.revision_applied, 1)
    if (receipt.phase_at_apply not in ('closed', 'prepared', 'active')
            or type(receipt.window_confirmed) is not bool
            or receipt.window_confirmed != (receipt.phase_at_apply == 'active')
            or receipt.reply_completed is not False or receipt.replayed is not False):
        corrupt()
    integer(receipt.source_changes, 0, 1000); integer(receipt.new_message_refs, 0, 128)
    return receipt


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _encode(kind, value):
    raw = _canonical({'schema_version': 1, 'kind': kind, 'data': asdict(value)})
    if len(raw.encode()) > MAX_BYTES:
        raise LifecycleError('lifecycle_storage_budget')
    return raw


def encode_record(value):
    return _encode('record', validate_record(value))


def encode_receipt(value):
    return _encode('receipt', validate_receipt(value))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            corrupt()
        result[key] = value
    return result


def _decode(raw, kind, cls):
    try:
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_BYTES:
            corrupt()
        obj = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: corrupt())
        if (not isinstance(obj, dict) or set(obj) != {'schema_version','kind','data'}
                or type(obj['schema_version']) is not int or obj['schema_version'] != 1
                or obj['kind'] != kind or not isinstance(obj['data'], dict)
                or set(obj['data']) != {f.name for f in fields(cls)} or _canonical(obj) != raw):
            corrupt()
        value = obj['data']
        if kind == 'record':
            if not isinstance(value['source_cursor'], dict) or set(value['source_cursor']) != {'generation','position'}:
                corrupt()
            value['source_cursor'] = SourceCursor(**value['source_cursor'])
            for key in ('pending', 'used_windows', 'used_model_runs', 'message_refs', 'closeout_checks',
                        'closeout_evidence_candidates', 'summary_candidates'):
                if not isinstance(value[key], list): corrupt()
                value[key] = tuple(value[key])
            if not isinstance(value['active_state'], list) or any(not isinstance(v, list) for v in value['active_state']):
                corrupt()
            value['active_state'] = tuple(tuple(v) for v in value['active_state'])
            for key in ('anchor_refs', 'recent_refs'):
                if not isinstance(value[key], list): corrupt()
                if any(not isinstance(v, dict) or set(v) != {f.name for f in fields(G3SourceRef)} for v in value[key]):
                    corrupt()
                value[key] = tuple(G3SourceRef(**v) for v in value[key])
        return cls(**value)
    except LifecycleError:
        raise
    except (TypeError, ValueError, KeyError, RecursionError, UnicodeError):
        raise LifecycleError('lifecycle_storage_corrupt') from None


def decode_record(raw):
    return validate_record(_decode(raw, 'record', LifecycleRecord))


def decode_receipt(raw):
    return validate_receipt(_decode(raw, 'receipt', LifecycleReceipt))


def checksum(raw):
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()
