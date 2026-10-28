#!/usr/bin/env python3
"""Operator-local, explicitly enabled, read-only evidence collection.

No writes, deployment, rollback, Telegram setWebhook or snapshot restore.
No secrets, raw URLs, response bodies or exception details are printed.
Run only in an operator-controlled terminal on an approved network.
"""
from __future__ import annotations

import argparse
import getpass
import hmac
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Mapping

APPROVAL_FLAG = "RENDER_OPERATOR_READONLY_GET_EXPLICITLY_APPROVED"
TELEGRAM_ORIGIN = "https://api.telegram.org"
RENDER_ORIGIN = "https://api.render.com"
RENDER_DISK_ID = "dsk-d9dk0gbrjlhs73aph4ig"
EXPECTED_WEBHOOK = "https://ouou-home-telegram.onrender.com/integrations/telegram/webhook"
MAX_RESPONSE_BYTES = 16384

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _result(source: str, status: str, *, http_status: int | None = None, **data):
    return {
        "source": source,
        "status": status,
        "http_status": http_status,
        **data,
        "may_deploy": False,
        "may_rollback": False,
        "may_change_telegram": False,
        "may_restore_disk": False,
        "may_enable_oidc": False,
        "may_write_memory": False,
        "may_use_paid_api": False,
    }

def _valid_telegram_token(token: object) -> bool:
    return type(token) is str and re.fullmatch(r"[0-9]{4,18}:[A-Za-z0-9_-]{20,160}", token) is not None

def _valid_render_key(key: object) -> bool:
    return type(key) is str and 10 <= len(key) <= 512 and re.fullmatch(r"[A-Za-z0-9_-]+", key) is not None

def _as_utc(value: object):
    if type(value) is not str or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None

def _classify_telegram(payload: object):
    if type(payload) is not dict or payload.get("ok") is not True or type(payload.get("result")) is not dict:
        return _result("telegram", "invalid_response", http_status=200)
    data = payload["result"]
    url = data.get("url")
    if type(url) is not str or len(url) > 2048:
        return _result("telegram", "invalid_response", http_status=200)
    if not url:
        return _result("telegram", "not_registered", http_status=200)
    if not hmac.compare_digest(url.encode("utf-8"), EXPECTED_WEBHOOK.encode("utf-8")):
        return _result("telegram", "target_mismatch", http_status=200)
    pending = data.get("pending_update_count")
    if type(pending) is not int or pending < 0:
        return _result("telegram", "invalid_response", http_status=200)
    for field in ("last_error_date", "last_synchronization_error_date"):
        if field in data and (type(data[field]) is not int or data[field] < 0):
            return _result("telegram", "invalid_response", http_status=200)
    if "last_error_message" in data and type(data["last_error_message"]) is not str:
        return _result("telegram", "invalid_response", http_status=200)
    allowed = data.get("allowed_updates")
    if allowed is not None and (type(allowed) is not list or any(type(x) is not str for x in allowed)):
        return _result("telegram", "invalid_response", http_status=200)
    return _result(
        "telegram", "registered_claim_only", http_status=200,
        pending_updates=pending,
        historical_delivery_error=bool(data.get("last_error_date")),
        historical_sync_error=bool(data.get("last_synchronization_error_date")),
        message_update_allowed=(None if allowed is None else "message" in allowed),
    )

def _classify_snapshots(payload: object, *, now: datetime):
    if type(payload) is not list or len(payload) > 100:
        return _result("snapshots", "invalid_response", http_status=200)
    if not payload:
        return _result("snapshots", "no_snapshot_records", http_status=200, snapshot_count=0)
    created = []
    for item in payload:
        if type(item) is not dict:
            return _result("snapshots", "invalid_response", http_status=200)
        stamp = _as_utc(item.get("createdAt"))
        key = item.get("snapshotKey")
        if stamp is None or type(key) is not str or not (1 <= len(key) <= 2048):
            return _result("snapshots", "invalid_response", http_status=200)
        created.append(stamp)
    latest = max(created)
    age = (now - latest).total_seconds()
    if age < -300:
        return _result("snapshots", "clock_or_timestamp_inconsistent", http_status=200)
    return _result(
        "snapshots",
        "recent_metadata_restore_unverified" if age <= 48 * 3600 else "old_metadata_restore_unverified",
        http_status=200,
        snapshot_count=len(created),
        latest_created_at_utc=latest.isoformat().replace("+00:00", "Z"),
    )

def collect(
    source: str, *, approved: bool = False,
    environ: Mapping[str, str] | None = None,
    secret_reader: Callable[[str], str] | None = None,
    opener: Callable | None = None,
    now: datetime | None = None,
):
    """Never perform a request without both explicit gates. All outputs redacted."""
    if type(source) is not str or source not in ("telegram", "snapshots"):
        return _result("unknown", "blocked")
    env = os.environ if environ is None else environ
    if not approved or env.get(APPROVAL_FLAG) != "true":
        return _result(source, "disabled")
    read_secret = secret_reader or getpass.getpass
    try:
        secret = read_secret("Bot Token (hidden): " if source == "telegram" else "Render API Key (hidden): ")
    except Exception:
        return _result(source, "secret_unavailable")
    if source == "telegram":
        if not _valid_telegram_token(secret):
            return _result(source, "invalid_local_credential")
        url = TELEGRAM_ORIGIN + "/bot" + secret + "/getWebhookInfo"
        headers = {"Accept": "application/json"}
    else:
        if not _valid_render_key(secret):
            return _result(source, "invalid_local_credential")
        url = RENDER_ORIGIN + "/v1/disks/" + RENDER_DISK_ID + "/snapshots"
        headers = {"Accept": "application/json", "Authorization": "Bearer " + secret}
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        if opener is None:
            transport = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                _NoRedirect(),
            )
            opener = transport.open
        with opener(request, timeout=5) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if type(status) is not int or status not in ((200,) if source == "telegram" else (200, 201)):
            return _result(source, "http_unexpected_status", http_status=status if type(status) is int else None)
        if type(content_type) is not str or content_type.lower().split(";", 1)[0].strip() != "application/json":
            return _result(source, "invalid_response", http_status=status)
        if type(body) is not bytes or len(body) > MAX_RESPONSE_BYTES:
            return _result(source, "invalid_response", http_status=status)
        try:
            payload = json.loads(body.decode("utf-8", "strict"))
        except (ValueError, UnicodeError):
            return _result(source, "invalid_response", http_status=status)
        if source == "telegram":
            return _classify_telegram(payload)
        timestamp = now or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            return _result(source, "invalid_local_clock")
        result = _classify_snapshots(payload, now=timestamp.astimezone(timezone.utc))
        result["http_status"] = status
        return result
    except urllib.error.HTTPError as exc:
        code = exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else None
        return _result(source, "http_error", http_status=code)
    except Exception:
        # Includes DNS, CA/hostname TLS failures, redirects and timeouts.
        # No exception text (which can contain the token-bearing URL) escapes.
        return _result(source, "unavailable")

def main(argv=None):
    parser = argparse.ArgumentParser(description="Operator-local non-mutating evidence GET; OFF by default")
    parser.add_argument("--source", required=True, choices=("telegram", "snapshots"))
    parser.add_argument("--perform-read-only", action="store_true")
    args = parser.parse_args(argv)
    report = collect(args.source, approved=args.perform_read_only)
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    # Never turn this diagnostic command into a production release gate.
    return 2

if __name__ == "__main__":
    sys.exit(main())
