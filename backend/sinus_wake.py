"""Persistent volition-driven wake scheduling for autonomous OUO runs.

The state contract intentionally follows the core semantics of sinus-rhythm
(https://github.com/rossignol6712/sinus-rhythm, upstream commit
 d8eff28acd55515dd22e5547d7c2e9d0469ed289): the agent chooses the next
interval and passes a short ``did`` baton into the next run.  This module is the
application-owned scheduling boundary; it performs no model or network calls.

Production runs on Linux and uses a lock file plus atomic ``os.replace`` so a
future MCP process can share the same state safely with the daemon.  On
platforms without ``fcntl`` the state remains thread-safe, but cross-process
sharing is intentionally unsupported.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # POSIX production path; keep imports usable on Windows test/dev hosts.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None


DEFAULT_DID = "No previous autonomous wake has run."
MAX_DID_CHARS = 240
_STATUS_LOG_LOCK = threading.Lock()
_STATUS_LOG_LAST_MONOTONIC = 0.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise ValueError("invalid_wake_time")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid_wake_time")
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("invalid_wake_state")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid_wake_state") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid_wake_state")
    return parsed.astimezone(timezone.utc)


@dataclass
class WakeState:
    next_wakeup_at: str | None = None
    did: str = DEFAULT_DID
    last_heartbeat_at: str | None = None
    last_external_interaction_at: str | None = None
    wakeup_reason: str = "scheduled"
    consecutive_fallbacks: int = 0
    schedule_generation: int = 0
    focus_mode: bool = False

    @classmethod
    def from_dict(cls, data: object) -> "WakeState":
        if not isinstance(data, dict):
            raise ValueError("invalid_wake_state")
        known = {field.name for field in cls.__dataclass_fields__.values()}
        state = cls(**{key: value for key, value in data.items() if key in known})
        state.validate()
        return state

    def validate(self) -> None:
        from_iso(self.next_wakeup_at)
        from_iso(self.last_heartbeat_at)
        from_iso(self.last_external_interaction_at)
        if not isinstance(self.did, str) or not self.did.strip() or len(self.did) > MAX_DID_CHARS:
            raise ValueError("invalid_wake_state")
        if self.wakeup_reason not in {"scheduled", "fallback"}:
            raise ValueError("invalid_wake_state")
        if type(self.consecutive_fallbacks) is not int or self.consecutive_fallbacks < 0:
            raise ValueError("invalid_wake_state")
        if type(self.schedule_generation) is not int or self.schedule_generation < 0:
            raise ValueError("invalid_wake_state")
        if type(self.focus_mode) is not bool:
            raise ValueError("invalid_wake_state")


def _status_logging_enabled() -> bool:
    return os.environ.get("AUTONOMOUS_WAKE_STATUS_LOG_ENABLED", "false").strip().lower() == "true"


def _maybe_log_status(state: WakeState, *, interval_seconds: float = 60.0) -> None:
    """Emit bounded scheduler telemetry without exposing the causal ``did``."""
    if not _status_logging_enabled():
        return
    global _STATUS_LOG_LAST_MONOTONIC
    now_mono = time.monotonic()
    with _STATUS_LOG_LOCK:
        if _STATUS_LOG_LAST_MONOTONIC and now_mono - _STATUS_LOG_LAST_MONOTONIC < interval_seconds:
            return
        _STATUS_LOG_LAST_MONOTONIC = now_mono
    next_wakeup = state.next_wakeup_at or "none"
    last_heartbeat = state.last_heartbeat_at or "none"
    print(
        "[sinus-status] "
        f"next_wakeup_at={next_wakeup} "
        f"last_heartbeat_at={last_heartbeat} "
        f"wakeup_reason={state.wakeup_reason} "
        f"consecutive_fallbacks={state.consecutive_fallbacks} "
        f"schedule_generation={state.schedule_generation}",
        flush=True,
    )


class WakeStateStore:
    """Atomically persist one scheduler state with a separate lock file."""

    def __init__(self, path: str | os.PathLike[str]):
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("invalid_wake_state_path")
        self.path = Path(raw).expanduser().resolve(strict=False)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._thread_lock = threading.RLock()

    @contextmanager
    def _exclusive(self):
        with self._thread_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a", encoding="utf-8") as lock_handle:
                if fcntl is not None:
                    fcntl.flock(lock_handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_handle, fcntl.LOCK_UN)

    def _load(self) -> WakeState:
        if not self.path.exists():
            return WakeState()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError("invalid_wake_state") from None
        return WakeState.from_dict(data)

    def _save(self, state: WakeState) -> None:
        state.validate()
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(state), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load(self) -> WakeState:
        with self._exclusive():
            state = self._load()
        _maybe_log_status(state)
        return state

    def save(self, state: WakeState) -> None:
        with self._exclusive():
            self._save(state)

    def schedule_wakeup(
        self,
        minutes: int,
        did: str,
        *,
        min_minutes: int,
        max_minutes: int,
        now: datetime | None = None,
    ) -> tuple[WakeState, int]:
        if type(minutes) is not int:
            raise ValueError("invalid_wake_minutes")
        if type(min_minutes) is not int or type(max_minutes) is not int:
            raise ValueError("invalid_wake_bounds")
        if min_minutes < 1 or max_minutes < min_minutes:
            raise ValueError("invalid_wake_bounds")
        summary = str(did or "").strip()
        if not summary or len(summary) > MAX_DID_CHARS:
            raise ValueError("invalid_wake_did")
        instant = now or utc_now()
        clamped = max(min_minutes, min(max_minutes, minutes))
        with self._exclusive():
            state = self._load()
            state.next_wakeup_at = to_iso(instant + timedelta(minutes=clamped))
            state.last_heartbeat_at = to_iso(instant)
            state.did = summary
            state.wakeup_reason = "scheduled"
            state.consecutive_fallbacks = 0
            state.schedule_generation += 1
            self._save(state)
            return state, clamped

    def schedule_fallback(
        self,
        minutes: int,
        *,
        now: datetime | None = None,
    ) -> WakeState:
        if type(minutes) is not int or minutes < 1:
            raise ValueError("invalid_wake_fallback")
        instant = now or utc_now()
        with self._exclusive():
            state = self._load()
            state.next_wakeup_at = to_iso(instant + timedelta(minutes=minutes))
            state.last_heartbeat_at = to_iso(instant)
            state.wakeup_reason = "fallback"
            state.consecutive_fallbacks += 1
            self._save(state)
            return state

    def record_external_interaction(self, *, now: datetime | None = None) -> WakeState:
        instant = now or utc_now()
        with self._exclusive():
            state = self._load()
            state.last_external_interaction_at = to_iso(instant)
            self._save(state)
            return state


def is_due(state: WakeState, *, now: datetime | None = None) -> bool:
    due = from_iso(state.next_wakeup_at)
    return due is None or due <= (now or utc_now()).astimezone(timezone.utc)


def wake_run_id(state: WakeState) -> str:
    """Stable idempotency identity for the currently due schedule generation."""
    due = state.next_wakeup_at or "initial"
    raw = f"sinus-wake-v1\x1f{state.schedule_generation}\x1f{due}".encode("utf-8")
    return "sinus-v1-" + hashlib.sha256(raw).hexdigest()[:32]


def heartbeat_status(state: WakeState) -> dict[str, Any]:
    return {
        "next_wakeup_at": state.next_wakeup_at,
        "last_heartbeat_at": state.last_heartbeat_at,
        "last_did": state.did,
        "wakeup_reason": state.wakeup_reason,
        "consecutive_fallbacks": state.consecutive_fallbacks,
        "focus_mode": state.focus_mode,
        "schedule_generation": state.schedule_generation,
    }
