"""Bounded OpenAI-compatible embedding adapter for Phase 4D-D3B2.

The adapter is intentionally narrow: callers provide plaintext strings only by
ordinal plus a server-fixed model/dimension identity.  It never accepts Memory
keys, provenance, hierarchy identifiers, or retrieval authority.  Network and
response failures collapse to fixed data-free categories.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Final

import httpx

from backend import memory_retrieval_vector as vector


EMBEDDING_ADAPTER_CONTRACT_VERSION: Final = "memory-retrieval-openai-embedding-v1"
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
MAX_TOTAL_INPUT_CHARS: Final = 32 * 4096
DEFAULT_TIMEOUT_SECONDS: Final = 30.0
_MODEL_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")

_ERROR_CATEGORIES: Final = frozenset({
    "embedding_adapter_configuration_invalid",
    "embedding_adapter_request_failed",
    "embedding_adapter_response_invalid",
    "memory_retrieval_embedding_adapter_error",
})


class MemoryRetrievalEmbeddingAdapterError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_retrieval_embedding_adapter_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except BaseException:
            return "memory_retrieval_embedding_adapter_error"

    def __repr__(self) -> str:
        return f"MemoryRetrievalEmbeddingAdapterError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalEmbeddingAdapterError(category)


def _validated_api_base(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > 2048:
        _raise("embedding_adapter_configuration_invalid")
    try:
        value.encode("ascii", errors="strict")
        parsed = urllib.parse.urlsplit(value)
    except (UnicodeError, ValueError):
        _raise("embedding_adapter_configuration_invalid")
    host = (parsed.hostname or "").casefold()
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not host
        or parsed.scheme not in {"https", "http"}
        or (parsed.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"})
    ):
        _raise("embedding_adapter_configuration_invalid")
    path = parsed.path.rstrip("/")
    if any(part == ".." for part in path.split("/")):
        _raise("embedding_adapter_configuration_invalid")
    return urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        path,
        "",
        "",
    ))


def _validated_api_key(value: object) -> str:
    if (
        type(value) is not str
        or value != value.strip()
        or not 16 <= len(value) <= 512
        or not value.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        _raise("embedding_adapter_configuration_invalid")
    return value


def _validated_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _raise("embedding_adapter_configuration_invalid")
    parsed = float(value)
    if not math.isfinite(parsed) or not 1.0 <= parsed <= 40.0:
        _raise("embedding_adapter_configuration_invalid")
    return parsed


def _validated_request(
    texts: object,
    model: object,
    dimensions: object,
) -> tuple[tuple[str, ...], str, int]:
    if (
        type(texts) is not tuple
        or not 1 <= len(texts) <= vector.MAX_EMBEDDING_BATCH
        or any(type(text) is not str or not text for text in texts)
        or type(model) is not str
        or _MODEL_PATTERN.fullmatch(model) is None
        or type(dimensions) is not int
        or isinstance(dimensions, bool)
        or not vector.MIN_VECTOR_DIMENSIONS <= dimensions <= vector.MAX_VECTOR_DIMENSIONS
    ):
        _raise("embedding_adapter_configuration_invalid")
    if sum(len(text) for text in texts) > MAX_TOTAL_INPUT_CHARS:
        _raise("embedding_adapter_configuration_invalid")
    try:
        for text in texts:
            text.encode("utf-8", errors="strict")
    except UnicodeError:
        _raise("embedding_adapter_configuration_invalid")
    return texts, model, dimensions


def _decode_response(raw: bytes, expected_count: int) -> tuple[object, ...]:
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        _raise("embedding_adapter_response_invalid")
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _raise("embedding_adapter_response_invalid")
    if type(payload) is not dict or type(payload.get("data")) is not list:
        _raise("embedding_adapter_response_invalid")
    data = payload["data"]
    if len(data) != expected_count:
        _raise("embedding_adapter_response_invalid")
    ordered: list[object | None] = [None] * expected_count
    for item in data:
        if type(item) is not dict:
            _raise("embedding_adapter_response_invalid")
        index = item.get("index")
        embedding = item.get("embedding")
        if (
            type(index) is not int
            or isinstance(index, bool)
            or not 0 <= index < expected_count
            or ordered[index] is not None
            or type(embedding) not in (list, tuple)
        ):
            _raise("embedding_adapter_response_invalid")
        ordered[index] = embedding
    if any(item is None for item in ordered):
        _raise("embedding_adapter_response_invalid")
    return tuple(ordered)


@dataclass(frozen=True, slots=True, repr=False)
class OpenAICompatibleEmbeddingAdapterV1:
    api_base: str = field(repr=False)
    api_key: str = field(repr=False)
    timeout_seconds: float

    def __init__(
        self,
        api_base: object,
        api_key: object,
        *,
        timeout_seconds: object = DEFAULT_TIMEOUT_SECONDS,
    ):
        object.__setattr__(self, "api_base", _validated_api_base(api_base))
        object.__setattr__(self, "api_key", _validated_api_key(api_key))
        object.__setattr__(self, "timeout_seconds", _validated_timeout(timeout_seconds))

    def __repr__(self) -> str:
        return "<OpenAICompatibleEmbeddingAdapterV1>"

    async def __call__(
        self,
        texts: tuple[str, ...],
        model: str,
        dimensions: int,
    ) -> object:
        inputs, model_id, dims = _validated_request(texts, model, dimensions)
        endpoint = self.api_base.rstrip("/") + "/embeddings"
        payload = {
            "model": model_id,
            "input": list(inputs),
            "dimensions": dims,
            "encoding_format": "float",
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    endpoint,
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        _raise("embedding_adapter_request_failed")
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        if (
                            not declared.isascii()
                            or not declared.isdecimal()
                            or int(declared) > MAX_RESPONSE_BYTES
                        ):
                            _raise("embedding_adapter_response_invalid")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            _raise("embedding_adapter_response_invalid")
        except asyncio.CancelledError:
            raise
        except MemoryRetrievalEmbeddingAdapterError:
            raise
        except (httpx.HTTPError, OSError, TimeoutError):
            _raise("embedding_adapter_request_failed")
        except Exception:
            _raise("embedding_adapter_request_failed")
        return _decode_response(bytes(raw), len(inputs))


__all__ = (
    "EMBEDDING_ADAPTER_CONTRACT_VERSION",
    "MemoryRetrievalEmbeddingAdapterError",
    "OpenAICompatibleEmbeddingAdapterV1",
)
