#!/usr/bin/env python3
"""Operator-side live qualification harness for the staged Codex Web canary.

This tool never mutates Render configuration, deploys services, or changes startup
commands. The default command is ``plan`` and performs no network I/O. Live state
changes are split into explicit subcommands so an operator can stop between gates.

Secrets are read from an environment variable (``RELAY_SECRET`` by default) rather
than from argv, keeping the Bearer value out of normal process listings and receipts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
RECEIPT_VERSION = 1
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SAFE_USER_CODE = re.compile(r"^[A-Za-z0-9-]{3,64}$")
_SAFE_ERROR = frozenset({
    "unauthorized",
    "codex_control_disabled",
    "codex_not_authenticated",
    "codex_login_in_progress",
    "codex_login_unavailable",
    "codex_usage_unavailable",
    "codex_app_server_unavailable",
    "codex_app_server_timeout",
    "codex_app_server_protocol_error",
    "codex_generation_disabled",
    "codex_generation_unavailable",
    "codex_generation_busy",
    "codex_generation_account_unavailable",
    "codex_generation_model_unavailable",
    "codex_generation_provider_unavailable",
    "codex_generation_persona_invalid",
    "codex_generation_store_unavailable",
    "codex_generation_session_invalid",
    "codex_generation_session_conflict",
    "codex_canary_session_conflict",
    "codex_canary_session_contract_changed",
    "codex_canary_session_unavailable",
    "codex_canary_session_not_found",
    "codex_canary_unavailable",
    "invalid_canary_request",
})

PLAN = (
    "Stage alternate supervisor with CODEX_CANARY_ENTRYPOINTS_ENABLED=true while CODEX_GENERATION_ENABLED=false.",
    "Enable P1 control and verify provider status still reports generation_provider=api.",
    "If disconnected, run login-start and complete the ChatGPT device-code flow in a browser.",
    "Run account-check and verify connected=true plus bounded usage metadata.",
    "Enable CODEX_GENERATION_ENABLED=true, restart the service, then run account-check again to prove auth persistence.",
    "Run canary-create, refresh GuiTing, and manually select the new Codex canary window.",
    "Send one ordinary pure-text message manually in GuiTing; visually confirm exactly one assistant reply.",
    "Run wait-bound, then snapshot to record the pinned model/provider/effort contract before restart.",
    "Restart the service without changing gates and run verify-after-restart against the saved receipt.",
    "Send a second ordinary pure-text message manually after restart and visually confirm exactly one reply.",
    "Run canary-retire and verify-retired.",
    "Rollback production gates/startup explicitly, then run rollback-check; this harness never performs the rollback itself.",
)


class QualificationError(RuntimeError):
    def __init__(self, category: str, *, status_code: int | None = None):
        super().__init__(category)
        self.category = category
        self.status_code = status_code

    def __repr__(self) -> str:
        return f"<QualificationError category={self.category!r} status_code={self.status_code!r}>"


@dataclass(frozen=True)
class CanaryState:
    api_session: str
    status: str
    model: str
    model_provider: str
    reasoning_effort: str | None
    thread_bound: bool

    def public_dict(self) -> dict[str, object]:
        return {
            "api_session": self.api_session,
            "status": self.status,
            "model": self.model,
            "model_provider": self.model_provider,
            "reasoning_effort": self.reasoning_effort,
            "thread_bound": self.thread_bound,
        }


def _safe_session(value: object) -> str:
    if not isinstance(value, str) or _SAFE_SESSION.fullmatch(value) is None:
        raise QualificationError("qualification_session_invalid")
    return value


def _safe_model(value: object, category: str) -> str:
    if not isinstance(value, str) or _SAFE_MODEL.fullmatch(value) is None:
        raise QualificationError(category)
    return value


def _safe_effort(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or not value.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise QualificationError("qualification_reasoning_effort_invalid")
    return value


def normalize_base_url(raw: str) -> str:
    if not isinstance(raw, str) or not raw or raw != raw.strip() or len(raw) > 512:
        raise QualificationError("qualification_base_url_invalid")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise QualificationError("qualification_base_url_invalid")
    host = (parsed.hostname or "").casefold()
    localhost = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and localhost):
        raise QualificationError("qualification_base_url_invalid")
    if not parsed.netloc:
        raise QualificationError("qualification_base_url_invalid")
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def load_secret(environ: Mapping[str, str], name: str = "RELAY_SECRET") -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name):
        raise QualificationError("qualification_secret_env_invalid")
    value = environ.get(name, "")
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or not value.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise QualificationError("qualification_secret_missing")
    return value


def _category_from_error_payload(payload: object) -> str:
    if isinstance(payload, dict):
        for key in ("error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value in _SAFE_ERROR:
                return value
    return "qualification_remote_error"


class RelayClient:
    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        opener=None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        if not isinstance(secret, str) or not secret:
            raise QualificationError("qualification_secret_missing")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            raise QualificationError("qualification_timeout_invalid") from None
        if timeout < 1 or timeout > 120:
            raise QualificationError("qualification_timeout_invalid")
        self.timeout_seconds = timeout
        self._opener = opener or urllib.request.urlopen
        self._secret = secret

    def _url(self, path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path:
            raise QualificationError("qualification_path_invalid")
        return self.base_url + path

    def request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> dict:
        data = None
        headers = {"Authorization": f"Bearer {self._secret}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(dict(body), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(data) > 4096:
                raise QualificationError("qualification_request_too_large")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._url(path), data=data, method=method, headers=headers,
        )
        try:
            response = self._opener(request, timeout=self.timeout_seconds)
            with response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise QualificationError("qualification_response_too_large")
        except urllib.error.HTTPError as exc:
            raw = exc.read(4097)
            payload: object = {}
            if len(raw) <= 4096:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError, ValueError):
                    payload = {}
            raise QualificationError(
                _category_from_error_payload(payload), status_code=exc.code,
            ) from None
        except QualificationError:
            raise
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError):
            raise QualificationError("qualification_transport_unavailable") from None
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, json.JSONDecodeError, ValueError):
            raise QualificationError("qualification_response_invalid") from None
        if not isinstance(payload, dict):
            raise QualificationError("qualification_response_invalid")
        return payload

    def provider_status(self) -> dict[str, object]:
        payload = self.request("GET", "/provider/status")
        if payload.get("generation_provider") != "api":
            raise QualificationError("qualification_generation_authority_changed")
        connected = payload.get("connected")
        if not isinstance(connected, bool):
            raise QualificationError("qualification_provider_status_invalid")
        return {"connected": connected, "generation_provider": "api"}

    def provider_usage(self) -> dict[str, object]:
        payload = self.request("GET", "/provider/usage")
        if not isinstance(payload, dict):
            raise QualificationError("qualification_usage_invalid")
        # Preserve only structural success; never mirror arbitrary usage/account fields into receipts.
        return {"available": True}

    def login_start(self) -> dict[str, str]:
        payload = self.request("POST", "/provider/login/start")
        url = payload.get("verification_url")
        code = payload.get("user_code")
        status = payload.get("status")
        if (
            not isinstance(url, str)
            or not url.startswith("https://")
            or len(url) > 512
            or not isinstance(code, str)
            or _SAFE_USER_CODE.fullmatch(code) is None
            or status != "pending"
        ):
            raise QualificationError("qualification_login_response_invalid")
        return {"verification_url": url, "user_code": code, "status": "pending"}

    def create_canary(self, title: str = "Codex canary") -> dict[str, str]:
        if not isinstance(title, str) or len(title) > 120:
            raise QualificationError("qualification_title_invalid")
        payload = self.request("POST", "/provider/canary/create", {"title": title})
        if payload.get("ok") is not True or payload.get("provider") != "codex":
            raise QualificationError("qualification_canary_create_invalid")
        created = payload.get("created")
        if not isinstance(created, dict):
            raise QualificationError("qualification_canary_create_invalid")
        sid = _safe_session(created.get("api_session"))
        actual_title = created.get("title")
        if not isinstance(actual_title, str) or not actual_title or len(actual_title) > 120:
            raise QualificationError("qualification_canary_create_invalid")
        return {"api_session": sid, "title": actual_title}

    def canary_status(self, api_session: str) -> CanaryState:
        sid = _safe_session(api_session)
        payload = self.request("GET", f"/provider/canary/{sid}/status")
        if payload.get("ok") is not True or payload.get("provider") != "codex":
            raise QualificationError("qualification_canary_status_invalid")
        row = payload.get("session")
        if not isinstance(row, dict) or row.get("api_session") != sid:
            raise QualificationError("qualification_canary_status_invalid")
        status = row.get("status")
        thread_bound = row.get("thread_bound")
        if status not in {"active", "retired"} or not isinstance(thread_bound, bool):
            raise QualificationError("qualification_canary_status_invalid")
        return CanaryState(
            api_session=sid,
            status=str(status),
            model=_safe_model(row.get("model"), "qualification_model_invalid"),
            model_provider=_safe_model(row.get("model_provider"), "qualification_provider_invalid"),
            reasoning_effort=_safe_effort(row.get("reasoning_effort")),
            thread_bound=thread_bound,
        )

    def retire_canary(self, api_session: str) -> dict[str, str]:
        sid = _safe_session(api_session)
        payload = self.request("POST", f"/provider/canary/{sid}/retire")
        retired = payload.get("retired")
        if (
            payload.get("ok") is not True
            or payload.get("provider") != "api"
            or not isinstance(retired, dict)
            or retired.get("api_session") != sid
            or retired.get("status") != "retired"
        ):
            raise QualificationError("qualification_canary_retire_invalid")
        return {"api_session": sid, "status": "retired"}

    def health(self) -> dict[str, bool]:
        health = self.request("GET", "/healthz")
        if health.get("ok") is not True:
            raise QualificationError("qualification_health_failed")
        ready = self.request("GET", "/readyz")
        if ready.get("ready") is not True:
            raise QualificationError("qualification_readiness_failed")
        return {"healthz": True, "readyz": True}


def account_check(client: RelayClient) -> dict[str, object]:
    status = client.provider_status()
    if status["connected"] is not True:
        raise QualificationError("qualification_account_not_connected")
    client.provider_usage()
    return {"connected": True, "generation_provider": "api", "usage_available": True}


def wait_thread_bound(
    client: RelayClient,
    api_session: str,
    *,
    timeout_seconds: float = 90.0,
    poll_seconds: float = 2.0,
    sleeper=time.sleep,
) -> CanaryState:
    if timeout_seconds < 1 or timeout_seconds > 600 or poll_seconds < 0.1 or poll_seconds > 30:
        raise QualificationError("qualification_wait_invalid")
    deadline = time.monotonic() + timeout_seconds
    while True:
        state = client.canary_status(api_session)
        if state.status != "active":
            raise QualificationError("qualification_canary_not_active")
        if state.thread_bound:
            if state.model_provider == "unresolved":
                raise QualificationError("qualification_provider_unresolved_after_thread")
            return state
        if time.monotonic() >= deadline:
            raise QualificationError("qualification_thread_not_bound")
        sleeper(poll_seconds)


def build_receipt(client: RelayClient, api_session: str) -> dict[str, object]:
    account = account_check(client)
    state = client.canary_status(api_session)
    if state.status != "active" or not state.thread_bound or state.model_provider == "unresolved":
        raise QualificationError("qualification_snapshot_not_ready")
    return {
        "version": RECEIPT_VERSION,
        "account": account,
        "canary": state.public_dict(),
    }


def write_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    if not isinstance(path, Path):
        raise QualificationError("qualification_receipt_path_invalid")
    try:
        encoded = (json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if len(encoded) > 16 * 1024:
            raise QualificationError("qualification_receipt_invalid")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_bytes(encoded)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except QualificationError:
        raise
    except OSError:
        raise QualificationError("qualification_receipt_write_failed") from None


def load_receipt(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise QualificationError("qualification_receipt_read_failed") from None
    if not raw or len(raw) > 16 * 1024:
        raise QualificationError("qualification_receipt_invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise QualificationError("qualification_receipt_invalid") from None
    if not isinstance(payload, dict) or payload.get("version") != RECEIPT_VERSION:
        raise QualificationError("qualification_receipt_invalid")
    return payload


def verify_after_restart(client: RelayClient, receipt: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(receipt, dict):
        raise QualificationError("qualification_receipt_invalid")
    expected = receipt.get("canary")
    if not isinstance(expected, dict):
        raise QualificationError("qualification_receipt_invalid")
    sid = _safe_session(expected.get("api_session"))
    account = account_check(client)
    current = client.canary_status(sid)
    if current.status != "active" or not current.thread_bound:
        raise QualificationError("qualification_restart_persistence_failed")
    for field in ("model", "model_provider", "reasoning_effort"):
        if current.public_dict().get(field) != expected.get(field):
            raise QualificationError("qualification_restart_contract_changed")
    return {
        "account": account,
        "canary": current.public_dict(),
        "restart_persistence": True,
    }


def verify_retired(client: RelayClient, api_session: str) -> dict[str, object]:
    state = client.canary_status(api_session)
    if state.status != "retired":
        raise QualificationError("qualification_canary_not_retired")
    return {"api_session": state.api_session, "status": "retired"}


def rollback_check(client: RelayClient, *, expect_control_disabled: bool) -> dict[str, object]:
    health = client.health()
    if expect_control_disabled:
        try:
            client.provider_status()
        except QualificationError as exc:
            if exc.category != "codex_control_disabled":
                raise
        else:
            raise QualificationError("qualification_control_still_enabled")
        return {**health, "codex_control_disabled": True}
    status = client.provider_status()
    if status.get("generation_provider") != "api":
        raise QualificationError("qualification_generation_authority_changed")
    return {**health, "generation_provider": "api"}


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("CODEX_QUALIFICATION_BASE_URL", ""))
    parser.add_argument("--secret-env", default="RELAY_SECRET")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("plan")
    sub.add_parser("status")
    sub.add_parser("login-start")
    sub.add_parser("account-check")
    create = sub.add_parser("canary-create")
    create.add_argument("--title", default="Codex canary")
    for name in ("canary-status", "wait-bound", "snapshot", "canary-retire", "verify-retired"):
        p = sub.add_parser(name)
        p.add_argument("--session", required=True)
        if name == "wait-bound":
            p.add_argument("--wait-timeout", type=float, default=90.0)
    snapshot = sub.choices["snapshot"]
    snapshot.add_argument("--receipt", required=True)
    verify = sub.add_parser("verify-after-restart")
    verify.add_argument("--receipt", required=True)
    rollback = sub.add_parser("rollback-check")
    rollback.add_argument("--expect-control-disabled", action="store_true")
    return parser


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "plan"
    if command == "plan":
        _print_json({"network": False, "steps": list(PLAN)})
        return 0
    env = os.environ if environ is None else environ
    try:
        client = RelayClient(
            args.base_url,
            load_secret(env, args.secret_env),
            timeout_seconds=args.timeout,
        )
        if command == "status":
            result = client.provider_status()
        elif command == "login-start":
            result = client.login_start()
        elif command == "account-check":
            result = account_check(client)
        elif command == "canary-create":
            result = client.create_canary(args.title)
        elif command == "canary-status":
            result = client.canary_status(args.session).public_dict()
        elif command == "wait-bound":
            result = wait_thread_bound(client, args.session, timeout_seconds=args.wait_timeout).public_dict()
        elif command == "snapshot":
            result = build_receipt(client, args.session)
            write_receipt(Path(args.receipt), result)
        elif command == "verify-after-restart":
            result = verify_after_restart(client, load_receipt(Path(args.receipt)))
        elif command == "canary-retire":
            result = client.retire_canary(args.session)
        elif command == "verify-retired":
            result = verify_retired(client, args.session)
        elif command == "rollback-check":
            result = rollback_check(client, expect_control_disabled=args.expect_control_disabled)
        else:
            raise QualificationError("qualification_command_invalid")
    except QualificationError as exc:
        _print_json({"ok": False, "error": exc.category})
        return 2
    _print_json({"ok": True, "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
