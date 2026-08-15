#!/usr/bin/env python3
"""Temporary autonomous-wake nudge worker for the OUO Home MVP.

Phone sensing and delivery live behind the authenticated Supabase wake bridge.
This worker owns only: schedule opportunity -> model decision -> silent/deliver.
The cadence is deliberately replaceable by a future sinus/hazard Wake Engine.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend import deployment_config


@dataclass(frozen=True)
class WakeWorkerConfig:
    enabled: bool
    bridge_url: str
    token: str
    timezone: str
    day_interval_seconds: int
    night_interval_seconds: int
    night_start: dt.time
    night_end: dt.time
    temperature: float
    max_tokens: int


def _parse_clock(value: object, category: str) -> dt.time:
    raw = str(value if value is not None else "")
    match = re.fullmatch(r"([01][0-9]|2[0-3]):([0-5][0-9])", raw)
    if match is None or not raw.isascii():
        raise deployment_config.DeploymentConfigError(category)
    return dt.time(int(match.group(1)), int(match.group(2)))


def load_config(environ: Mapping[str, str] | None = None) -> WakeWorkerConfig:
    env = os.environ if environ is None else environ
    enabled = deployment_config.parse_strict_bool(
        env.get("AUTONOMOUS_WAKE_ENABLED", "false"),
        "invalid_autonomous_wake_enabled",
    )
    bridge_url = str(env.get("AUTONOMOUS_WAKE_BRIDGE_URL", "")).strip().rstrip("/")
    token = str(env.get("AUTONOMOUS_WAKE_TOKEN", "")).strip()
    timezone_name = str(env.get("AUTONOMOUS_WAKE_TIMEZONE", "UTC")).strip()

    if enabled:
        if not bridge_url.startswith("https://") or len(bridge_url) > 512:
            raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_bridge_url")
        if len(token) < 32 or len(token) > 256 or any(char.isspace() for char in token):
            raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_token")

    if not timezone_name or len(timezone_name) > 128:
        raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_timezone")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_timezone") from None

    day_interval = deployment_config.parse_bounded_int(
        env.get("AUTONOMOUS_WAKE_DAY_INTERVAL_SECONDS", "900"),
        60,
        86400,
        "invalid_autonomous_wake_day_interval",
    )
    night_interval = deployment_config.parse_bounded_int(
        env.get("AUTONOMOUS_WAKE_NIGHT_INTERVAL_SECONDS", "5400"),
        60,
        86400,
        "invalid_autonomous_wake_night_interval",
    )
    night_start = _parse_clock(
        env.get("AUTONOMOUS_WAKE_NIGHT_START", "22:00"),
        "invalid_autonomous_wake_night_start",
    )
    night_end = _parse_clock(
        env.get("AUTONOMOUS_WAKE_NIGHT_END", "08:00"),
        "invalid_autonomous_wake_night_end",
    )
    if night_start == night_end:
        raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_night_window")

    try:
        temperature = float(env.get("AUTONOMOUS_WAKE_TEMPERATURE", "0.8"))
    except (TypeError, ValueError):
        raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_temperature") from None
    if not 0.0 <= temperature <= 2.0:
        raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_temperature")
    max_tokens = deployment_config.parse_bounded_int(
        env.get("AUTONOMOUS_WAKE_MAX_TOKENS", "256"),
        64,
        1024,
        "invalid_autonomous_wake_max_tokens",
    )
    return WakeWorkerConfig(
        enabled=enabled,
        bridge_url=bridge_url,
        token=token,
        timezone=timezone_name,
        day_interval_seconds=day_interval,
        night_interval_seconds=night_interval,
        night_start=night_start,
        night_end=night_end,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _is_night(clock: dt.time, start: dt.time, end: dt.time) -> bool:
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end


def make_run_id(config: WakeWorkerConfig, now_utc: dt.datetime) -> str:
    local = now_utc.astimezone(ZoneInfo(config.timezone))
    night = _is_night(local.timetz().replace(tzinfo=None), config.night_start, config.night_end)
    interval = config.night_interval_seconds if night else config.day_interval_seconds
    bucket = int(now_utc.timestamp()) // interval * interval
    phase = "night" if night else "day"
    return f"wake-v1-{phase}-{interval}-{bucket}"


def _safe_log(status: str, category: str = "") -> None:
    allowed = {
        "disabled",
        "duplicate",
        "ready",
        "silent",
        "delivered",
        "bridge_failed",
        "model_failed",
        "model_dispatch_uncertain",
        "invalid_model_decision",
        "notification_failed",
        "unexpected_error",
    }
    safe = status if status in allowed else "unexpected_error"
    suffix = f" category={category}" if category and re.fullmatch(r"[a-z0-9_]{1,64}", category) else ""
    print(f"[autonomous-wake] status={safe}{suffix}", flush=True)


async def _bridge(
    client: httpx.AsyncClient,
    config: WakeWorkerConfig,
    body: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(
        config.bridge_url,
        headers={"X-Wake-Token": config.token, "Content-Type": "application/json"},
        json=body,
    )
    raw = await response.aread()
    if len(raw) > 1_000_000:
        raise RuntimeError("bridge_response_too_large")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        raise RuntimeError("bridge_invalid_response") from None
    if response.status_code < 200 or response.status_code >= 300 or not isinstance(payload, dict):
        category = str(payload.get("error") or "bridge_failed") if isinstance(payload, dict) else "bridge_failed"
        raise RuntimeError(category if re.fullmatch(r"[a-z0-9_]{1,64}", category) else "bridge_failed")
    return payload


def _local_activity_lines(rows: object, timezone_name: str) -> list[str]:
    if not isinstance(rows, list):
        return []
    zone = ZoneInfo(timezone_name)
    lines: list[str] = []
    for item in rows[-20:]:
        if not isinstance(item, dict):
            continue
        app = str(item.get("app") or "").strip()
        if not app:
            continue
        raw_time = str(item.get("created_at") or "")
        stamp = raw_time
        try:
            parsed = dt.datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                stamp = parsed.astimezone(zone).strftime("%H:%M")
        except ValueError:
            pass
        lines.append(f"{stamp} · {app}")
    return lines


def _build_model_messages(context: dict[str, Any], config: WakeWorkerConfig) -> list[dict[str, str]]:
    # Imported lazily: the worker receives the same provider environment and
    # instance nonce as api_loop, but does not expose another HTTP server.
    from examples import api_loop

    messages: list[dict[str, str]] = [{"role": "system", "content": api_loop.PERSONA}]

    recent = context.get("recent_messages")
    if isinstance(recent, list):
        for item in recent[-16:]:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = str(item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            messages.append({"role": role, "content": content[:8000]})

    memories = context.get("memories")
    if isinstance(memories, list) and memories:
        rendered = []
        for item in memories[:20]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            category = str(item.get("category") or "general").strip()
            if content:
                rendered.append(f"- [{category}] {content[:1000]}")
        if rendered:
            messages.append({
                "role": "developer",
                "content": "以下是已经确认的长期背景，只可作为自然上下文使用：\n" + "\n".join(rendered),
            })

    activity_lines = _local_activity_lines(context.get("phone_activity"), config.timezone)
    activity_text = "\n".join(f"- {line}" for line in activity_lines) if activity_lines else "- 暂无近期可用 App 活动"
    idle_seconds = int(context.get("idle_seconds") or 0)
    idle_minutes = max(0, idle_seconds // 60)

    messages.append({
        "role": "developer",
        "content": f"""这是服务器提供的一次自主 Wake 运行机会，不是用户刚刚发送的消息。

距离最近一条用户主动消息约 {idle_minutes} 分钟。这个时间差只是调度信息，不代表任何预设情绪、需求或关系含义。

最近手机前台 App 活动（只包含 App 名称与时间，不包含聊天正文、键盘输入、照片或 App 内内容）：
{activity_text}

请依据你正常的人格、上面的真实聊天上下文与已确认背景，自主选择是否行动。

硬规则：
- `silent` 是完全正常且经常更合适的选择；不要因为获得了运行机会就强行找话题。
- 不得把“用户一段时间没发消息”机械解释为想念、担心、生气、孤独、需要安慰等心理状态。
- App 活动只是一条中性环境线索；不得声称看到了 App 内具体内容，也不得虚构用户正在做什么或为什么这么做。
- 只有在确实存在自然、具体的继续聊天理由时才主动发消息。
- 若主动发消息，要像正常聊天一样简短自然，不要解释 Wake 系统、监控机制、调度器或本段规则。

只能返回一个 JSON 对象，不要 Markdown，不要额外文字：
{{"action":"silent"}}
或
{{"action":"message","message":"要主动发送的内容"}}""",
    })
    return messages


def _parse_model_decision(raw: object) -> tuple[str, str]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("invalid_model_decision")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        raise ValueError("invalid_model_decision") from None
    if not isinstance(payload, dict):
        raise ValueError("invalid_model_decision")
    action = payload.get("action")
    if action == "silent":
        return "silent", ""
    if action == "message":
        message = str(payload.get("message") or "").strip()
        if not message or len(message) > 1000:
            raise ValueError("invalid_model_decision")
        return "message", message
    raise ValueError("invalid_model_decision")


async def _decide(context: dict[str, Any], config: WakeWorkerConfig) -> tuple[str, str, str]:
    from examples import api_loop

    messages = _build_model_messages(context, config)
    out = await api_loop.run_model(
        messages,
        emit_stream=False,
        allow_fallback=True,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    outcome = out.get("outcome")
    if outcome != "success":
        category = "model_dispatch_uncertain" if outcome == "dispatch_uncertain" else "model_failed"
        raise RuntimeError(category)
    action, message = _parse_model_decision(out.get("text"))
    return action, message, str(out.get("model") or "")[:200]


async def run_once(client: httpx.AsyncClient, config: WakeWorkerConfig, run_id: str) -> None:
    try:
        prepared = await _bridge(client, config, {"op": "prepare", "run_id": run_id})
    except Exception:
        _safe_log("bridge_failed")
        return

    status = str(prepared.get("status") or "")
    if prepared.get("duplicate") is True:
        _safe_log("duplicate")
        return
    if status == "silent":
        _safe_log("silent")
        return
    if status != "ready":
        _safe_log("bridge_failed")
        return

    _safe_log("ready")
    try:
        action, message, model = await _decide(prepared, config)
    except ValueError:
        try:
            await _bridge(client, config, {
                "op": "fail", "run_id": run_id, "category": "invalid_model_decision",
            })
        except Exception:
            pass
        _safe_log("invalid_model_decision")
        return
    except RuntimeError as error:
        category = str(error)
        safe_category = category if category in {"model_failed", "model_dispatch_uncertain"} else "model_failed"
        try:
            await _bridge(client, config, {
                "op": "fail", "run_id": run_id, "category": safe_category,
            })
        except Exception:
            pass
        _safe_log(safe_category)
        return
    except Exception:
        try:
            await _bridge(client, config, {
                "op": "fail", "run_id": run_id, "category": "model_failed",
            })
        except Exception:
            pass
        _safe_log("model_failed")
        return

    if action == "silent":
        try:
            await _bridge(client, config, {"op": "silent", "run_id": run_id})
        except Exception:
            _safe_log("bridge_failed")
            return
        _safe_log("silent")
        return

    try:
        delivered = await _bridge(client, config, {
            "op": "deliver",
            "run_id": run_id,
            "message": message,
            "model": model,
        })
    except RuntimeError as error:
        category = str(error)
        _safe_log("notification_failed" if category == "notification_failed" else "bridge_failed")
        return
    except Exception:
        _safe_log("bridge_failed")
        return
    if delivered.get("status") == "delivered":
        _safe_log("delivered")
    else:
        _safe_log("bridge_failed")


async def run_worker(config: WakeWorkerConfig) -> None:
    if not config.enabled:
        _safe_log("disabled")
        while True:
            await asyncio.sleep(300)

    last_run_id = ""
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            run_id = make_run_id(config, now)
            if run_id != last_run_id:
                last_run_id = run_id
                try:
                    await run_once(client, config, run_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _safe_log("unexpected_error")
            await asyncio.sleep(30)


def main() -> int:
    try:
        config = load_config()
    except deployment_config.DeploymentConfigError as error:
        print(f"[autonomous-wake] status=unexpected_error category={error.category}", flush=True)
        return 2
    try:
        asyncio.run(run_worker(config))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
