#!/usr/bin/env python3
"""Interactively configure Telegram webhook without secrets in shell arguments."""

from __future__ import annotations

import getpass
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def configure_webhook(
    bot_token: str,
    webhook_secret: str,
    webhook_url: str,
    *,
    opener=urllib.request.urlopen,
) -> None:
    if not bot_token or any(character.isspace() for character in bot_token):
        raise ValueError("invalid_bot_token")
    if not (1 <= len(webhook_secret) <= 256) or re.fullmatch(r"[A-Za-z0-9_-]+", webhook_secret) is None:
        raise ValueError("invalid_webhook_secret")
    parsed = urllib.parse.urlsplit(webhook_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("invalid_webhook_url")
    payload = json.dumps(
        {"url": webhook_url, "secret_token": webhook_secret, "allowed_updates": ["message"]}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/setWebhook",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener(request, timeout=15) as response:
            raw = response.read(65537)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        raise RuntimeError("telegram_webhook_request_failed") from None
    if len(raw) > 65536:
        raise RuntimeError("telegram_webhook_response_invalid")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError("telegram_webhook_response_invalid") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("telegram_webhook_rejected")


def main() -> int:
    print("Secrets are read interactively and are never printed.", file=sys.stderr)
    webhook_url = input("Public HTTPS webhook URL: ").strip()
    bot_token = getpass.getpass("Telegram Bot Token: ").strip()
    webhook_secret = getpass.getpass("Telegram webhook secret: ").strip()
    try:
        configure_webhook(bot_token, webhook_secret, webhook_url)
    except (ValueError, RuntimeError) as exc:
        print(f"Webhook configuration failed: {exc}", file=sys.stderr)
        return 1
    print("Webhook configured successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
