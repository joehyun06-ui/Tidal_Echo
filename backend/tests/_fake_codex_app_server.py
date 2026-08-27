"""Deterministic, offline JSONL fake for Codex control-plane tests."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def emit(value: object) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def record(path: Path | None, value: object) -> None:
    if path is not None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, separators=(",", ":")) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="normal")
    parser.add_argument("--transcript")
    args = parser.parse_args()
    transcript = Path(args.transcript) if args.transcript else None
    login_id = "internal-login-id"
    server_request_sent = False
    initialized = False

    for raw in sys.stdin:
        request = json.loads(raw)
        record(transcript, request)
        if "method" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            if args.scenario == "delayed_initialize":
                time.sleep(0.25)
            request_id = request["id"]
            emit({"id": request_id, "result": {"serverInfo": {"name": "fake"}}})
            continue
        if method == "initialized" and "id" not in request:
            initialized = True
            continue
        if "id" not in request:
            continue
        request_id = request["id"]
        if method.startswith("account/") and not initialized:
            emit({
                "id": request_id,
                "error": {"code": -32002, "message": "not initialized"},
            })
            continue
        if args.scenario == "exit":
            return 7
        if args.scenario == "malformed":
            sys.stdout.write("{malformed\n")
            sys.stdout.flush()
            continue
        if args.scenario == "oversized":
            sys.stdout.write("{" + ("x" * (1024 * 1024 + 8)) + "\n")
            sys.stdout.flush()
            continue
        if args.scenario == "delay":
            time.sleep(2)
        if args.scenario == "short_delay":
            time.sleep(0.2)
        if args.scenario == "server_request" and not server_request_sent:
            server_request_sent = True
            emit({
                "id": 990, "method": "tool/approval",
                "params": {"secret": "must-not-be-approved"},
            })

        if method == "account/read":
            result = {
                "account": {
                    "type": "chatgpt", "planType": "plus",
                    "accountId": "must-not-return", "accessToken": "must-not-return",
                },
                "requiresOpenaiAuth": False,
                "refreshToken": "must-not-return",
            }
        elif method == "account/login/start":
            result = {
                "type": "chatgptDeviceCode",
                "loginId": login_id,
                "verificationUrl": "https://example.invalid/device",
                "userCode": "ABCD-EFGH",
                "accessToken": "must-not-return",
            }
        elif method == "account/login/cancel":
            if args.scenario == "cancel_not_found":
                cancel_status = "notFound"
            elif args.scenario == "cancel_legacy_spelling":
                cancel_status = "cancelled"
            elif args.scenario == "cancel_secret_status":
                cancel_status = "PRIVATE-CANCEL-STATUS-SENTINEL"
            else:
                cancel_status = (
                    "canceled"
                    if request.get("params", {}).get("loginId") == login_id
                    else "notFound"
                )
            result = {
                "status": cancel_status
            }
        elif method == "account/logout":
            result = {}
        elif method == "account/rateLimits/read":
            primary = {
                "limitId": "primary", "limitName": "Primary",
                "primary": {"usedPercent": 12.5, "windowDurationMins": 300,
                            "resetsAt": 1800000000},
                "credits": {
                    "hasCredits": True,
                    "unlimited": False,
                    "balance": "PRIVATE-BALANCE-SENTINEL",
                },
                "rateLimitReachedType": None,
                "unknownSecret": "must-not-return",
            }
            result = {
                "rateLimits": primary,
                "rateLimitsByLimitId": {
                    "primary": primary,
                    "secondary": {
                        "limitId": "secondary", "limitName": "Secondary",
                        "primary": {"usedPercent": 3, "windowDurationMins": 60,
                                    "resetsAt": 1800000300},
                        "credits": {
                            "hasCredits": False,
                            "unlimited": True,
                            "balance": None,
                        },
                    },
                },
                "rateLimitResetCredits": {"availableCount": 2, "credits": None},
                "unknown": "must-not-return",
            }
        elif method == "account/usage/read":
            result = {"summary": {
                "lifetimeTokens": 1234,
                "peakDailyTokens": 200,
                "longestRunningTurnSec": 42,
                "currentStreakDays": 2,
                "longestStreakDays": 4,
                "rawAccount": "must-not-return",
            }, "dailyUsageBuckets": [
                {"startDate": "2026-08-26", "tokens": 25, "secret": "drop"}
            ]}
        else:
            emit({"id": request_id, "error": {"code": -32601, "message": "unsupported"}})
            continue
        emit({"id": request_id, "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
