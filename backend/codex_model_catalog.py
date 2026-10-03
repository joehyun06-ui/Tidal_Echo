"""Bounded, credential-free projection of the running CLI's model catalog."""

from __future__ import annotations

import re

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_EFFORT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,31}")


def project_model(item: object) -> dict | None:
    if not isinstance(item, dict) or item.get("hidden") is True:
        return None
    model = item.get("model")
    if not isinstance(model, str) or not _MODEL.fullmatch(model):
        return None
    name = item.get("displayName")
    if not isinstance(name, str) or not name or len(name) > 160 or any(ord(c) < 32 for c in name):
        name = model
    efforts = []
    for value in item.get("supportedReasoningEfforts", []) or []:
        effort = value.get("reasoningEffort") if isinstance(value, dict) else None
        if isinstance(effort, str) and _EFFORT.fullmatch(effort) and effort not in efforts:
            efforts.append(effort)
    default = item.get("defaultReasoningEffort")
    if not isinstance(default, str) or not _EFFORT.fullmatch(default):
        default = None
    # Older CLI fixtures may only contain the default; preserve that usable choice.
    if default and default not in efforts:
        efforts.append(default)
    return {
        "model": model, "display_name": name,
        "is_default": item.get("isDefault") is True,
        "reasoning_efforts": efforts, "default_reasoning_effort": default,
    }


async def read_models(request) -> list[dict]:
    models = {}
    cursor = None
    seen = set()
    for _ in range(8):
        params = {"limit": 100, "includeHidden": False}
        if cursor:
            params["cursor"] = cursor
        result = await request("model/list", params)
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise ValueError("invalid_model_catalog")
        if len(result["data"]) > 256:
            raise ValueError("invalid_model_catalog")
        for item in result["data"]:
            row = project_model(item)
            if row:
                models[row["model"]] = row
        if len(models) > 256:
            raise ValueError("invalid_model_catalog")
        cursor = result.get("nextCursor")
        if cursor is None:
            return list(models.values())
        if not isinstance(cursor, str) or not cursor or len(cursor) > 1024 or cursor in seen:
            raise ValueError("invalid_model_catalog")
        seen.add(cursor)
    raise ValueError("invalid_model_catalog")


class CodexModelCatalogMixin:
    """Works with both standalone P1 control and the shared control facade."""

    async def models(self) -> dict:
        try:
            return {"models": await read_models(self._request)}
        except self.model_catalog_error:
            raise
        except Exception:
            raise self.model_catalog_error("codex_models_unavailable") from None
