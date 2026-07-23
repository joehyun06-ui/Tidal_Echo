"""Deterministic policy, normalization, and fingerprinting for Memory Core."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable


NORMALIZATION_VERSION = 1
FINGERPRINT_VERSION = 1
FINGERPRINT_DOMAIN = "memory-core/fingerprint/v1"
HMAC_DIGEST_BYTES = 32

KINDS = frozenset({
    "user_preference", "user_profile", "relationship", "shared_episode",
    "project", "decision", "task_or_progress", "assistant_experience",
})
SCOPE_TYPES = frozenset({"global_user", "channel", "session", "project"})
SENSITIVITIES = frozenset({"normal", "sensitive", "restricted"})
EVIDENCE_ROLES = frozenset({"user", "assistant"})
USER_EVIDENCE_TYPES = frozenset({
    "user_explicit_remember", "user_explicit_statement", "user_confirmed_decision",
})
FORBIDDEN_EVIDENCE_TYPES = frozenset({
    "roleplay", "fiction", "third_party", "connection_test", "error_log", "raw_request",
})
ALL_EVIDENCE_TYPES = USER_EVIDENCE_TYPES | FORBIDDEN_EVIDENCE_TYPES | {
    "assistant_experience",
}
KNOWN_CHANNELS = frozenset({
    "web", "relay", "telegram", "kelivo", "operit_share", "galatea", "mobile_executor",
})

_SCOPE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_PROVENANCE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_WHITESPACE = re.compile(r"\s+", re.UNICODE)

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"""["']?\bauthorization["']?\s*:\s*["']?(?:bearer|basic)\s+\S+""",
        re.IGNORECASE,
    ),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(
        r"""["']?\b(?:api[_ -]?key|access[_ -]?token|secret[_ -]?key|client[_ -]?secret)"""
        r"""["']?\s*[:=]\s*["']?[A-Za-z0-9._~+/=-]{8,}""",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:sk|gh[pousr])[-_][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:xox[baprs]-[A-Za-z0-9-]{16,}|AIza[A-Za-z0-9_-]{20,}|ya29\.[A-Za-z0-9_-]{16,})\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(
        r"""["']?\b(?:cookie|set-cookie|session(?:id|_token|_secret)?)"""
        r"""["']?\s*[:=]\s*["']?\S+""",
        re.IGNORECASE,
    ),
    re.compile(r"(?:^|[?&])(?:token|api_key|secret)=[A-Za-z0-9._~+/%=-]{8,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
_TEST_PATTERNS = (
    re.compile(r"\b(?:OPERIT|MEMORY|KELIVO|TELEGRAM)[-_][A-Z0-9_-]*E2E[A-Z0-9_-]*\b", re.IGNORECASE),
    re.compile(r"\bconnection[ _-]?test\b", re.IGNORECASE),
    re.compile(r"\bE2E[ _-]?(?:marker|test)\b", re.IGNORECASE),
)
_ERROR_LOG_PATTERNS = (
    re.compile(r"Traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"\b(?:stack trace|uncaught exception)\b", re.IGNORECASE),
)
_TECHNICAL_ID_PATTERNS = (
    re.compile(
        r"\b(?:telegram|device|android|database|operit[ _-]?conversation)"
        r"[ _-]?(?:id|identifier)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
)
_FORBIDDEN_SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:card number|cvv|bank account|routing number|wallet seed)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(
        r"\b(?:latitude|lat)\s*[:=]\s*-?\d{1,3}(?:\.\d+)?\s*[,; ]+"
        r"(?:longitude|lon|lng)\s*[:=]\s*-?\d{1,3}(?:\.\d+)?\b",
        re.IGNORECASE,
    ),
)
_HIGH_SENSITIVITY_PATTERNS = (
    re.compile(
        r"\b(?:diagnosed|diagnosis|medical condition|mental health|therapy|pregnan(?:t|cy)|"
        r"hiv|sexual health|sex life)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:诊断|病史|心理健康|治疗记录|怀孕|性健康|性生活)"),
)


class MemoryPolicyError(ValueError):
    """A fixed, content-free policy error."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class ProvenanceInput:
    canonical_message_id: int = field(repr=False)
    channel: str
    source: str
    evidence_role: str
    evidence_type: str


def normalize_content(content: str, *, max_chars: int) -> str:
    if not isinstance(content, str):
        raise MemoryPolicyError("invalid_content")
    if len(content) > max_chars * 4:
        raise MemoryPolicyError("content_too_long")
    normalized = unicodedata.normalize("NFC", content.replace("\r\n", "\n").replace("\r", "\n"))
    if any(unicodedata.category(char) == "Cc" and not char.isspace() for char in normalized):
        raise MemoryPolicyError("invalid_content")
    normalized = _WHITESPACE.sub(" ", normalized).strip()
    if not normalized:
        raise MemoryPolicyError("empty_content")
    if len(normalized) > max_chars:
        raise MemoryPolicyError("content_too_long")
    return normalized


def fingerprint_content(
    secret: str,
    *,
    scope_type: str,
    scope_ref: str,
    kind: str,
    normalized_content: str,
) -> bytes:
    if not isinstance(secret, str) or len(secret) < 32:
        raise MemoryPolicyError("memory_configuration_invalid")
    payload = json.dumps(
        {
            "domain": FINGERPRINT_DOMAIN,
            "kind": kind,
            "normalization_version": NORMALIZATION_VERSION,
            "normalized_content": normalized_content,
            "scope_ref": scope_ref,
            "scope_type": scope_type,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(secret.encode("ascii"), payload, hashlib.sha256).digest()


def secure_digest_equal(left: bytes, right: bytes) -> bool:
    return (
        isinstance(left, bytes)
        and isinstance(right, bytes)
        and len(left) == HMAC_DIGEST_BYTES
        and len(right) == HMAC_DIGEST_BYTES
        and hmac.compare_digest(left, right)
    )


class MemoryPolicy:
    """Pure deterministic validation; no database or provider access."""

    def __init__(self, *, max_item_chars: int, sensitive_storage_enabled: bool):
        if max_item_chars < 64 or max_item_chars > 4096:
            raise MemoryPolicyError("invalid_memory_policy")
        self.max_item_chars = max_item_chars
        self.sensitive_storage_enabled = bool(sensitive_storage_enabled)

    def validate_scope(self, scope_type: str, scope_ref: str) -> tuple[str, str]:
        if scope_type not in SCOPE_TYPES or not isinstance(scope_ref, str):
            raise MemoryPolicyError("invalid_scope")
        if scope_type == "global_user":
            if scope_ref != "":
                raise MemoryPolicyError("invalid_scope")
        elif _SCOPE_REF.fullmatch(scope_ref) is None:
            raise MemoryPolicyError("invalid_scope")
        if scope_type == "channel" and scope_ref not in KNOWN_CHANNELS:
            raise MemoryPolicyError("invalid_scope")
        return scope_type, scope_ref

    def validate_kind(self, kind: str) -> str:
        if kind not in KINDS:
            raise MemoryPolicyError("invalid_kind")
        return kind

    def validate_sensitivity(self, sensitivity: str, content: str) -> str:
        if sensitivity not in SENSITIVITIES:
            raise MemoryPolicyError("invalid_sensitivity")
        if any(pattern.search(content) for pattern in _FORBIDDEN_SENSITIVE_PATTERNS):
            raise MemoryPolicyError("secret_detected")
        high_sensitivity = any(pattern.search(content) for pattern in _HIGH_SENSITIVITY_PATTERNS)
        if high_sensitivity and sensitivity == "normal":
            raise MemoryPolicyError("sensitivity_downgrade")
        if sensitivity != "normal" and not self.sensitive_storage_enabled:
            raise MemoryPolicyError("sensitive_storage_disabled")
        if high_sensitivity and not self.sensitive_storage_enabled:
            raise MemoryPolicyError("sensitive_storage_disabled")
        return sensitivity

    def validate_content(self, content: str, sensitivity: str) -> str:
        normalized = normalize_content(content, max_chars=self.max_item_chars)
        if any(pattern.search(normalized) for pattern in _SECRET_PATTERNS):
            raise MemoryPolicyError("secret_detected")
        if any(pattern.search(normalized) for pattern in _TEST_PATTERNS):
            raise MemoryPolicyError("forbidden_test_content")
        if any(pattern.search(normalized) for pattern in _ERROR_LOG_PATTERNS):
            raise MemoryPolicyError("forbidden_log_content")
        if any(pattern.search(normalized) for pattern in _TECHNICAL_ID_PATTERNS):
            raise MemoryPolicyError("technical_identifier_forbidden")
        self.validate_sensitivity(sensitivity, normalized)
        return normalized

    def validate_provenance_inputs(
        self, kind: str, sources: Iterable[ProvenanceInput],
    ) -> tuple[ProvenanceInput, ...]:
        result = tuple(sources)
        if not result:
            raise MemoryPolicyError("invalid_provenance")
        for source in result:
            if not isinstance(source, ProvenanceInput):
                raise MemoryPolicyError("invalid_provenance")
            if (
                not isinstance(source.canonical_message_id, int)
                or isinstance(source.canonical_message_id, bool)
                or source.canonical_message_id <= 0
                or source.channel not in KNOWN_CHANNELS
                or (source.source and _SAFE_PROVENANCE_VALUE.fullmatch(source.source) is None)
                or source.evidence_role not in EVIDENCE_ROLES
                or source.evidence_type not in ALL_EVIDENCE_TYPES
            ):
                raise MemoryPolicyError("invalid_provenance")
            if source.evidence_type in FORBIDDEN_EVIDENCE_TYPES:
                raise MemoryPolicyError("unsupported_evidence")

        if kind == "assistant_experience":
            if any(
                source.evidence_role != "assistant"
                or source.evidence_type != "assistant_experience"
                for source in result
            ):
                raise MemoryPolicyError("unsupported_evidence")
        elif any(
            source.evidence_role != "user"
            or source.evidence_type not in USER_EVIDENCE_TYPES
            for source in result
        ):
            raise MemoryPolicyError("unsupported_evidence")
        return result

    def validate_explicit_create(
        self,
        *,
        kind: str,
        scope_type: str,
        scope_ref: str,
        content: str,
        sensitivity: str,
        sources: Iterable[ProvenanceInput],
    ) -> tuple[str, tuple[ProvenanceInput, ...]]:
        self.validate_kind(kind)
        self.validate_scope(scope_type, scope_ref)
        normalized = self.validate_content(content, sensitivity)
        validated_sources = self.validate_provenance_inputs(kind, sources)
        return normalized, validated_sources
