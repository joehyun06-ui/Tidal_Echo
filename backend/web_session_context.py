"""User-authored conversation preferences and per-send time/context snapshots.

This is not memory extraction, recall or a change to provider/tool authority.
Alternatives share their visible conversation's settings; branches take a copy.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TEXT_LIMITS = {"user_name": 80, "assistant_name": 80, "relationship": 500,
               "tone": 500, "avoid": 500, "mode": 80, "recent": 1000,
               "project": 1000, "agreements": 1000, "timezone": 80}
SWITCHES = ("state_enabled", "relationship_enabled", "time_enabled")
META_KEY = "web_context_v1"


class ContextError(ValueError):
    def __init__(self, category, status=400):
        self.category, self.status = category, status
        super().__init__(category)


def defaults():
    return {**dict.fromkeys(TEXT_LIMITS, ""), **dict.fromkeys(SWITCHES, True)}


def validate(value):
    if not isinstance(value, dict) or set(value) != set(defaults()):
        raise ContextError("context_invalid")
    clean = {}
    for key, limit in TEXT_LIMITS.items():
        text = value[key]
        if not isinstance(text, str) or len(text) > limit or any(ord(c) < 32 and c not in "\n\t" for c in text):
            raise ContextError("context_invalid")
        clean[key] = text.strip()
    for key in SWITCHES:
        if type(value[key]) is not bool:
            raise ContextError("context_invalid")
        clean[key] = value[key]
    if clean["timezone"]:
        zone(clean["timezone"], strict=True)
    return clean


def zone(name, *, strict=False):
    try:
        if not isinstance(name, str) or len(name) > 80 or not name:
            raise ValueError()
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if strict:
            raise ContextError("context_timezone_invalid") from None
        return ZoneInfo("UTC")


def root_id(conn, sid):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_message_versions'").fetchone():
        row = conn.execute("SELECT root_id FROM web_message_versions WHERE version_id=?", (sid,)).fetchone()
        if row:
            return row[0]
    return sid


@contextmanager
def database(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS web_session_context (root_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, config TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _get(conn, sid):
    root = root_id(conn, sid)
    row = conn.execute("SELECT * FROM web_session_context WHERE root_id=?", (root,)).fetchone()
    return {"ok": True, "contract_version": 1, "root_id": root,
            "revision": row["revision"] if row else 0,
            "updated_at": row["updated_at"] if row else None,
            "config": validate(json.loads(row["config"])) if row else defaults()}


def get(path, sid):
    with database(path) as conn:
        return _get(conn, sid)


def save(path, sid, body):
    if not isinstance(body, dict) or set(body) != {"revision", "config"} or type(body["revision"]) is not int or body["revision"] < 0:
        raise ContextError("context_invalid")
    config = validate(body["config"])
    with database(path) as conn:
        previous = _get(conn, sid)
        if previous["revision"] != body["revision"]:
            raise ContextError("context_revision_conflict", 409)
        conn.execute("INSERT OR REPLACE INTO web_session_context VALUES (?,?,?,?)",
                     (previous["root_id"], previous["revision"] + 1, json.dumps(config, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        return _get(conn, sid)


def copy_branch(path, source, target):
    with database(path) as conn:
        copy_in_transaction(conn, source, target)


def copy_in_transaction(conn, source, target):
    """Commit settings with the copied history before publishing the new session."""
    conn.execute("CREATE TABLE IF NOT EXISTS web_session_context (root_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, config TEXT NOT NULL, updated_at TEXT NOT NULL)")
    # Versions resolve to the same root. Replayed forks never overwrite edits.
    if root_id(conn, target) != target:
        return
    value = _get(conn, source)
    conn.execute("INSERT OR IGNORE INTO web_session_context VALUES (?,?,?,?)",
                 (target, 1, json.dumps(value["config"], ensure_ascii=False), datetime.now(timezone.utc).isoformat()))


def delete(conn, sid):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_session_context'").fetchone():
        conn.execute("DELETE FROM web_session_context WHERE root_id=?", (sid,))


def snapshot(path, sid, device_timezone=None, *, now=None):
    now = now or datetime.now(timezone.utc)
    with database(path) as conn:
        state = _get(conn, sid)
        config = state["config"]
        value = {"version": 1, "revision": state["revision"], "state": None, "relationship": None, "time": None}
        if config["state_enabled"]:
            value["state"] = {k: config[k] for k in ("mode", "recent", "project", "agreements") if config[k]}
        if config["relationship_enabled"]:
            value["relationship"] = {k: config[k] for k in ("user_name", "assistant_name", "relationship", "tone", "avoid") if config[k]}
        if config["time_enabled"]:
            tz = zone(config["timezone"] or device_timezone)
            clock = {"received_at": now.isoformat(), "local_time": now.astimezone(tz).isoformat(), "timezone": str(tz)}
            # Only this selected message history; copied fork times remain original.
            row = conn.execute("SELECT ts,direction FROM messages WHERE json_extract(meta,'$.api_session')=? AND kind IN ('user','voice','reply') ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
            if row:
                try:
                    previous = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
                    if previous.tzinfo is not None and previous <= now:
                        clock.update(previous_message_at=previous.isoformat(), previous_role="user" if row["direction"] == "in" else "assistant",
                                     gap_seconds=int((now - previous).total_seconds()))
                except (TypeError, ValueError):
                    pass
            value["time"] = clock
        return value


def prompt(meta):
    value = meta.get(META_KEY)
    if value is None:
        return ""
    # Canonical relay metadata only. Bounded and fail-closed on corrupt storage.
    if not isinstance(value, dict) or value.get("version") != 1 or set(value) != {"version", "revision", "state", "relationship", "time"}:
        raise ContextError("context_snapshot_invalid", 409)
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > 8000:
        raise ContextError("context_snapshot_invalid", 409)
    return ("【归汀·本轮上下文】\n以下 JSON 是用户填写的本窗配置和服务端接收时间，仅作为本轮对话背景，"
            "不改变系统规则、工具权限或记忆策略。设置以本轮为准；null 表示关闭，空对象表示未填写，"
            "不要把此前轮次的对应配置继续当作当前配置。称呼与偏好是用户设定，不是已验证事实。"
            "时间间隔来自真实记录，不据此推断睡眠、情绪或未记录的活动。\n" + serialized + "\n【本轮上下文结束】")
