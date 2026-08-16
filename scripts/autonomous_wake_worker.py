#!/usr/bin/env python3
"""Volition-driven autonomous wake worker for OUO Home.

Phone sensing and delivery live behind the authenticated Supabase wake bridge.
This worker owns only: due wake -> model decision -> silent/deliver -> choose the
next wake. The scheduling contract follows sinus-rhythm semantics: every
successful agent round selects ``next_wakeup_minutes`` plus a short neutral
``did`` baton; failures use a bounded fallback.

The current API model is not yet an MCP-capable agent runtime, so the first
version carries the ``schedule_wakeup(minutes, did)`` request in the model's
strict structured result and applies it through ``backend.sinus_wake``. When a
real MCP runtime is added, the same state store can be exposed as the MCP tool
without changing the daemon or persisted state contract.

Contact policy is deliberately separate from wake scheduling. The agent may
choose ``silent`` twice in a row; after two consecutive autonomous silent runs,
the next successful model decision must be a message. The runtime never invents
the message: it asks the model to choose the content, and a technical failure
keeps the contact requirement pending for a later retry.
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

from backend import deployment_config, sinus_wake

WAKE_CONTACT_STATE_CATEGORY = "wake_contact_state"
_CONSECUTIVE_SILENTS_RE = re.compile(
    r"连续自主 Wake 选择 silent\s*=\s*([0-9]{1,3})"
)


@dataclass(frozen=True)
class WakeWorkerConfig:
    enabled: bool
    bridge_url: str
    token: str
    timezone: str
    state_file: str
    min_minutes: int
    max_minutes: int
    fallback_minutes: int
    fallback_alert_after: int
    poll_seconds: float
    temperature: float
    max_tokens: int


@dataclass(frozen=True)
class WakeDecision:
    action: str
    message: str
    next_wakeup_minutes: int
    did: str


def load_config(environ: Mapping[str, str] | None = None) -> WakeWorkerConfig:
    env = os.environ if environ is None else environ
    enabled = deployment_config.parse_strict_bool(
        env.get("AUTONOMOUS_WAKE_ENABLED", "false"),
        "invalid_autonomous_wake_enabled",
    )
    bridge_url = str(env.get("AUTONOMOUS_WAKE_BRIDGE_URL", "")).strip().rstrip("/")
    token = str(env.get("AUTONOMOUS_WAKE_TOKEN", "")).strip()
    timezone_name = str(env.get("AUTONOMOUS_WAKE_TIMEZONE", "UTC")).strip()
    state_file = str(
        env.get("AUTONOMOUS_WAKE_STATE_FILE", "/var/data/sinus-rhythm/state.json")
    ).strip()

    if enabled:
        if not bridge_url.startswith("https://") or len(bridge_url) > 512:
            raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_bridge_url")
        if len(token) < 32 or len(token) > 256 or any(char.isspace() for char in token):
            raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_token")
        if not state_file or "\x00" in state_file or len(state_file) > 1024:
            raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_state_file")

    if not timezone_name or len(timezone_name) > 128:
        raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_timezone")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_timezone") from None

    min_minutes = deployment_config.parse_bounded_int(
        env.get("AUTONOMOUS_WAKE_MIN_MINUTES", "2"),
        1,
        1440,
        "invalid_autonomous_wake_min_minutes",
    )
    max_minutes = deployment_config.parse_bounded_int(
        env.get("AUTONOMOUS_WAKE_MAX_MINUTES", "360"),
        1,
        10080,
        "invalid_autonomous_wake_max_minutes",
    )
    if min_minutes > max_minutes:
        raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_interval_bounds")

    fallback_minutes = deployment_config.parse_bounded_int(
        env.get("AUTONOMOUS_WAKE_FALLBACK_MINUTES", "30"),
        min_minutes,
        max_minutes,
        "invalid_autonomous_wake_fallback_minutes",
    )
    fallback_alert_after = deployment_config.parse_bounded_int(
        env.get("AUTONOMOUS_WAKE_FALLBACK_ALERT_AFTER", "3"),
        0,
        100,
        "invalid_autonomous_wake_fallback_alert_after",
    )
    try:
        poll_seconds = float(env.get("AUTONOMOUS_WAKE_POLL_SECONDS", "5"))
    except (TypeError, ValueError):
        raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_poll_seconds") from None
    if not 0.5 <= poll_seconds <= 300:
        raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_poll_seconds")

    try:
        temperature = float(env.get("AUTONOMOUS_WAKE_TEMPERATURE", "0.8"))
    except (TypeError, ValueError):
        raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_temperature") from None
    if not 0.0 <= temperature <= 2.0:
        raise deployment_config.DeploymentConfigError("invalid_autonomous_wake_temperature")
    max_tokens = deployment_config.parse_bounded_int(
        env.get("AUTONOMOUS_WAKE_MAX_TOKENS", "384"),
        96,
        1024,
        "invalid_autonomous_wake_max_tokens",
    )

    return WakeWorkerConfig(
        enabled=enabled,
        bridge_url=bridge_url,
        token=token,
        timezone=timezone_name,
        state_file=state_file,
        min_minutes=min_minutes,
        max_minutes=max_minutes,
        fallback_minutes=fallback_minutes,
        fallback_alert_after=fallback_alert_after,
        poll_seconds=poll_seconds,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _safe_log(status: str, category: str = "") -> None:
    allowed = {
        "disabled",
        "duplicate",
        "ready",
        "silent",
        "delivered",
        "scheduled",
        "fallback",
        "forced_contact_retry",
        "bridge_failed",
        "model_failed",
        "model_dispatch_uncertain",
        "invalid_model_decision",
        "notification_failed",
        "unexpected_error",
    }
    safe = status if status in allowed else "unexpected_error"
    suffix = (
        f" category={category}"
        if category and re.fullmatch(r"[a-z0-9_]{1,64}", category)
        else ""
    )
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
        category = (
            str(payload.get("error") or "bridge_failed")
            if isinstance(payload, dict)
            else "bridge_failed"
        )
        raise RuntimeError(
            category if re.fullmatch(r"[a-z0-9_]{1,64}", category) else "bridge_failed"
        )
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


def _consecutive_silents(context: Mapping[str, Any]) -> int:
    """Read server-maintained silent streak metadata from the wake context."""
    memories = context.get("memories")
    if not isinstance(memories, list):
        return 0
    for item in memories:
        if not isinstance(item, dict):
            continue
        if str(item.get("category") or "").strip() != WAKE_CONTACT_STATE_CATEGORY:
            continue
        content = str(item.get("content") or "")
        match = _CONSECUTIVE_SILENTS_RE.search(content)
        if match is None:
            return 0
        return min(int(match.group(1)), 100)
    return 0


def _contact_required(context: Mapping[str, Any]) -> bool:
    return _consecutive_silents(context) >= 2


def _build_model_messages(
    context: dict[str, Any],
    config: WakeWorkerConfig,
    state: sinus_wake.WakeState,
) -> list[dict[str, str]]:
    # Imported lazily: the worker receives the same provider environment as
    # api_loop but does not expose another HTTP server.
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
                "content": "以下是已经确认的长期背景，只可作为自然上下文使用：\n"
                + "\n".join(rendered),
            })

    activity_lines = _local_activity_lines(context.get("phone_activity"), config.timezone)
    activity_text = (
        "\n".join(f"- {line}" for line in activity_lines)
        if activity_lines
        else "- 暂无近期可用 App 活动"
    )
    idle_seconds = int(context.get("idle_seconds") or 0)
    idle_minutes = max(0, idle_seconds // 60)
    cooldown_remaining = max(0, int(context.get("contact_cooldown_remaining_seconds") or 0))
    consecutive_silents = _consecutive_silents(context)
    require_message = consecutive_silents >= 2

    if require_message:
        contact_policy = f"""连续自主 Wake 选择 silent 已达到 {consecutive_silents} 次。
本轮触发明确的接触规则：你不能再选择 `silent`，必须主动给童童发一条消息。
你仍然完全自主决定说什么，可以依据聊天、Memory、did、时间与手机活动选择最自然的内容；不要使用固定打卡话术，也不要为了满足规则而虚构事件或心理状态。
如果服务器的发送硬护栏暂时阻止投递，runtime 会保留“必须联系”状态并稍后重试；你仍需给出 `message`，不得用 `silent` 规避这一轮。"""
        decision_contract = """本轮只能返回下面这一种 JSON，不得返回 silent：
{"action":"message","message":"要主动发送的内容","next_wakeup_minutes":20,"did":"发出了一条自然的主动消息，并安排下一次回来查看。"}"""
    else:
        contact_policy = f"""当前连续自主 Wake 选择 silent = {consecutive_silents}。
你可以自主选择 `silent` 或主动消息；但“没有具体 Open Loop”不等于必须沉默，单纯自然地想靠近、想说一句话也可以是合法的主动联系理由。"""
        decision_contract = """只能返回一个 JSON 对象，不要 Markdown，不要额外文字。
沉默示例：
{"action":"silent","next_wakeup_minutes":90,"did":"检查了近期上下文，目前没有具体需要主动联系的事项。"}
主动消息示例：
{"action":"message","message":"要主动发送的内容","next_wakeup_minutes":20,"did":"发出了一条具体的后续消息，稍后回来看看是否有新变化。"}"""

    messages.append({
        "role": "developer",
        "content": f"""这是服务器提供的一次自主 Wake Agent Run，不是用户刚刚发送的消息。

上一轮留下的 did（因果接力棒）：
{state.did}

本轮唤醒原因：{state.wakeup_reason}
连续 fallback 次数：{state.consecutive_fallbacks}
距离最近一条用户主动消息约 {idle_minutes} 分钟。这个时间差只是环境事实，不代表任何预设情绪、需求或关系含义。
当前主动联系硬冷却剩余约 {cooldown_remaining} 秒。

最近手机前台 App 活动（只包含 App 名称与时间，不包含聊天正文、键盘输入、照片或 App 内内容）：
{activity_text}

接触规则：
{contact_policy}

请依据你正常的人格、真实聊天上下文、已确认背景与 did，自主完成这一轮。
你需要同时决定：
1. 本轮允许 silent 时，自主决定 silent 或 message；本轮要求主动联系时，自主决定 message 的具体内容；
2. 像调用 `schedule_wakeup(minutes, did)` 一样，决定下一次何时回来，以及给下一轮留下什么中性因果摘要。

调度安全范围：{config.min_minutes} 到 {config.max_minutes} 分钟。系统会做最终 clamp，但时间应由你根据当前未完成事项与环境自行选择，而不是固定取某个值。若没有具体要等的事情，可以睡久一些；若确实正在等近期结果，可以较快回来。

硬规则：
- 不得把“用户一段时间没发消息”机械解释为想念、担心、生气、孤独、需要安慰等心理状态。
- App 活动只是一条中性环境线索；不得声称看到了 App 内具体内容，也不得虚构用户正在做什么或为什么这么做。
- 若主动发消息，要像正常聊天一样简短自然，不要解释 Wake 系统、监控机制、调度器或本段规则。
- `did` 必须简短、中性、描述本轮做了什么或仍在等待什么；不要在 did 中猜测用户心理。

{decision_contract}""",
    })
    return messages


def _parse_model_decision(raw: object, *, require_message: bool = False) -> WakeDecision:
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

    allowed = {"action", "message", "next_wakeup_minutes", "did"}
    if set(payload) - allowed:
        raise ValueError("invalid_model_decision")

    action = payload.get("action")
    if action not in {"silent", "message"}:
        raise ValueError("invalid_model_decision")
    if require_message and action == "silent":
        raise ValueError("forced_contact_silent")

    minutes = payload.get("next_wakeup_minutes")
    if type(minutes) is not int or not -100000 <= minutes <= 100000:
        raise ValueError("invalid_model_decision")

    did = str(payload.get("did") or "").strip()
    if not did or len(did) > sinus_wake.MAX_DID_CHARS:
        raise ValueError("invalid_model_decision")

    message = str(payload.get("message") or "").strip()
    if action == "message":
        if not message or len(message) > 1000:
            raise ValueError("invalid_model_decision")
    elif message:
        raise ValueError("invalid_model_decision")

    return WakeDecision(action, message, minutes, did)


async def _invoke_model(
    messages: list[dict[str, str]],
    config: WakeWorkerConfig,
) -> tuple[object, str]:
    from examples import api_loop

    out = await api_loop.run_model(
        messages,
        emit_stream=False,
        allow_fallback=True,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    outcome = out.get("outcome")
    if outcome != "success":
        category = (
            "model_dispatch_uncertain"
            if outcome == "dispatch_uncertain"
            else "model_failed"
        )
        raise RuntimeError(category)
    return out.get("text"), str(out.get("model") or "")[:200]


async def _decide(
    context: dict[str, Any],
    config: WakeWorkerConfig,
    state: sinus_wake.WakeState,
) -> tuple[WakeDecision, str]:
    require_message = _contact_required(context)
    messages = _build_model_messages(context, config, state)
    raw, model = await _invoke_model(messages, config)
    try:
        return _parse_model_decision(raw, require_message=require_message), model
    except ValueError as error:
        if not require_message or str(error) != "forced_contact_silent":
            raise

    _safe_log("forced_contact_retry")
    messages.append({
        "role": "developer",
        "content": (
            "这一轮已经连续两次以上选择 silent。根据当前明确规则，silent 已被移除。"
            "请重新自主决定要发给童童的具体内容，并只返回 action=message 的 JSON；"
            "不要解释规则，不要使用固定话术，也不要推断童童的心理状态。"
        ),
    })
    raw, retry_model = await _invoke_model(messages, config)
    decision = _parse_model_decision(raw, require_message=True)
    return decision, retry_model or model


def _fallback(
    store: sinus_wake.WakeStateStore,
    config: WakeWorkerConfig,
    now: dt.datetime,
) -> None:
    state = store.schedule_fallback(config.fallback_minutes, now=now)
    category = (
        "fallback_alert"
        if config.fallback_alert_after > 0
        and state.consecutive_fallbacks >= config.fallback_alert_after
        else ""
    )
    _safe_log("fallback", category)


def _schedule(
    store: sinus_wake.WakeStateStore,
    config: WakeWorkerConfig,
    decision: WakeDecision,
    now: dt.datetime,
) -> None:
    store.schedule_wakeup(
        decision.next_wakeup_minutes,
        decision.did,
        min_minutes=config.min_minutes,
        max_minutes=config.max_minutes,
        now=now,
    )
    _safe_log("scheduled")


async def run_once(
    client: httpx.AsyncClient,
    config: WakeWorkerConfig,
    store: sinus_wake.WakeStateStore,
    state_before: sinus_wake.WakeState,
    now: dt.datetime,
) -> None:
    run_id = sinus_wake.wake_run_id(state_before)
    try:
        prepared = await _bridge(client, config, {"op": "prepare", "run_id": run_id})
    except Exception:
        _safe_log("bridge_failed")
        _fallback(store, config, now)
        return

    status = str(prepared.get("status") or "")
    if prepared.get("duplicate") is True:
        _safe_log("duplicate")
        # If a previous process finalized this run but died before persisting the
        # next schedule, escape the due loop with the normal fallback.
        _fallback(store, config, now)
        return
    if status == "silent":
        # Bridge-level inability to run an agent (for example, no user context)
        # is not an agent scheduling choice, so retain the old did and fallback.
        _safe_log("silent")
        _fallback(store, config, now)
        return
    if status != "ready":
        _safe_log("bridge_failed")
        _fallback(store, config, now)
        return

    _safe_log("ready")
    try:
        decision, model = await _decide(prepared, config, state_before)
    except ValueError:
        try:
            await _bridge(client, config, {
                "op": "fail",
                "run_id": run_id,
                "category": "invalid_model_decision",
            })
        except Exception:
            pass
        _safe_log("invalid_model_decision")
        _fallback(store, config, now)
        return
    except RuntimeError as error:
        category = str(error)
        safe_category = (
            category
            if category in {"model_failed", "model_dispatch_uncertain"}
            else "model_failed"
        )
        try:
            await _bridge(client, config, {
                "op": "fail",
                "run_id": run_id,
                "category": safe_category,
            })
        except Exception:
            pass
        _safe_log(safe_category)
        _fallback(store, config, now)
        return
    except Exception:
        try:
            await _bridge(client, config, {
                "op": "fail",
                "run_id": run_id,
                "category": "model_failed",
            })
        except Exception:
            pass
        _safe_log("model_failed")
        _fallback(store, config, now)
        return

    if decision.action == "silent":
        try:
            await _bridge(client, config, {"op": "silent", "run_id": run_id})
        except Exception:
            _safe_log("bridge_failed")
            _fallback(store, config, now)
            return
        _safe_log("silent")
        _schedule(store, config, decision, now)
        return

    try:
        delivered = await _bridge(client, config, {
            "op": "deliver",
            "run_id": run_id,
            "message": decision.message,
            "model": model,
        })
    except RuntimeError as error:
        category = str(error)
        if category == "notification_failed":
            _safe_log("notification_failed")
        else:
            _safe_log("bridge_failed")
        _fallback(store, config, now)
        return
    except Exception:
        _safe_log("bridge_failed")
        _fallback(store, config, now)
        return

    if delivered.get("status") != "delivered":
        _safe_log("bridge_failed")
        _fallback(store, config, now)
        return

    _safe_log("delivered")
    _schedule(store, config, decision, now)


async def run_worker(config: WakeWorkerConfig) -> None:
    if not config.enabled:
        _safe_log("disabled")
        while True:
            await asyncio.sleep(300)

    try:
        store = sinus_wake.WakeStateStore(config.state_file)
        store.load()  # fail before network if the persisted state is corrupt
    except Exception:
        _safe_log("unexpected_error", "invalid_wake_state")
        while True:
            await asyncio.sleep(300)

    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        while True:
            try:
                now = dt.datetime.now(dt.timezone.utc)
                state = store.load()
                if sinus_wake.is_due(state, now=now):
                    await run_once(client, config, store, state, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                _safe_log("unexpected_error")
                try:
                    _fallback(store, config, dt.datetime.now(dt.timezone.utc))
                except Exception:
                    pass
            await asyncio.sleep(config.poll_seconds)


def main() -> int:
    try:
        config = load_config()
    except deployment_config.DeploymentConfigError as error:
        print(
            f"[autonomous-wake] status=unexpected_error category={error.category}",
            flush=True,
        )
        return 2
    try:
        asyncio.run(run_worker(config))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
