"""Deterministic policy, normalization, and fingerprinting for Memory Core."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Iterable


NORMALIZATION_VERSION = 1
FINGERPRINT_VERSION = 1
FINGERPRINT_DOMAIN = "memory-core/fingerprint/v1"
FINGERPRINT_PROFILE_DOMAIN = "memory-core/profile-check/v1"
HMAC_DIGEST_BYTES = 32
CREDENTIAL_DETECTION_MAX_CHARS = 4096
CREDENTIAL_PERCENT_DECODE_ROUNDS = 2

KINDS = frozenset({
    "user_preference", "user_profile", "relationship", "shared_episode",
    "project", "decision", "task_or_progress", "assistant_experience",
})
SCOPE_TYPES = frozenset({"global_user", "channel", "session", "project"})
SENSITIVITIES = frozenset({"normal", "sensitive", "restricted"})
USER_EVIDENCE_TYPES = frozenset({
    "explicit_user_memory", "confirmed_user_fact", "confirmed_project_decision",
})
CORRECTION_EVIDENCE_TYPES = frozenset({"explicit_user_correction"})
ASSISTANT_EVIDENCE_TYPES = frozenset({"assistant_experience"})
ALL_EVIDENCE_TYPES = (
    USER_EVIDENCE_TYPES | CORRECTION_EVIDENCE_TYPES | ASSISTANT_EVIDENCE_TYPES
)
REALITY_SCOPES = frozenset({"real", "roleplay", "joke", "fiction", "third_party"})
SUBJECT_SCOPES = frozenset({"user", "project", "assistant", "third_party"})
EVIDENCE_COMPONENTS = frozenset({
    "memory_admin", "web_adapter", "telegram_adapter", "kelivo_adapter",
    "operit_adapter", "galatea_adapter", "assistant_runtime",
})
KNOWN_CHANNELS = frozenset({
    "web", "relay", "telegram", "kelivo", "operit_share", "galatea", "mobile_executor",
})

_SCOPE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_WHITESPACE = re.compile(r"\s+", re.UNICODE)
_JSON_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")

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


def fingerprint_profile_check(secret: str) -> bytes:
    if not isinstance(secret, str) or len(secret) < 32 or not secret.isascii():
        raise MemoryPolicyError("memory_configuration_invalid")
    payload = json.dumps(
        {
            "fingerprint_domain": FINGERPRINT_DOMAIN,
            "fingerprint_version": FINGERPRINT_VERSION,
            "normalization_version": NORMALIZATION_VERSION,
            "profile_domain": FINGERPRINT_PROFILE_DOMAIN,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hmac.new(
        secret.encode("ascii"),
        payload,
        hashlib.sha256,
    ).digest()


def secure_digest_equal(left: bytes, right: bytes) -> bool:
    return (
        isinstance(left, bytes)
        and isinstance(right, bytes)
        and len(left) == HMAC_DIGEST_BYTES
        and len(right) == HMAC_DIGEST_BYTES
        and hmac.compare_digest(left, right)
    )


def _decode_json_unicode_escapes(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        codepoint = int(match.group(1), 16)
        if 0xD800 <= codepoint <= 0xDFFF:
            return match.group(0)
        return chr(codepoint)

    return _JSON_UNICODE_ESCAPE.sub(replace, value)


def credential_detection_views(normalized_content: str) -> tuple[str, ...]:
    """Return bounded, non-persisted views used only for credential detection."""
    if (
        not isinstance(normalized_content, str)
        or len(normalized_content) > CREDENTIAL_DETECTION_MAX_CHARS
    ):
        raise MemoryPolicyError("content_too_long")
    candidates = [normalized_content]
    current = normalized_content
    for _round in range(CREDENTIAL_PERCENT_DECODE_ROUNDS):
        decoded = urllib.parse.unquote(current, encoding="utf-8", errors="replace")
        if decoded == current:
            break
        if len(decoded) > CREDENTIAL_DETECTION_MAX_CHARS:
            raise MemoryPolicyError("content_too_long")
        candidates.append(decoded)
        current = decoded
    for candidate in tuple(candidates):
        decoded = _decode_json_unicode_escapes(candidate)
        if (
            decoded != candidate
            and len(decoded) <= CREDENTIAL_DETECTION_MAX_CHARS
        ):
            candidates.append(decoded)
    return tuple(dict.fromkeys(candidates))


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

    def validate_sensitivity(
        self,
        sensitivity: str,
        content: str,
        *,
        allow_existing_reclassification: bool = False,
    ) -> str:
        if sensitivity not in SENSITIVITIES:
            raise MemoryPolicyError("invalid_sensitivity")
        if any(pattern.search(content) for pattern in _FORBIDDEN_SENSITIVE_PATTERNS):
            raise MemoryPolicyError("secret_detected")
        high_sensitivity = any(pattern.search(content) for pattern in _HIGH_SENSITIVITY_PATTERNS)
        if high_sensitivity and sensitivity == "normal":
            raise MemoryPolicyError("sensitivity_downgrade")
        if (
            sensitivity != "normal"
            and not self.sensitive_storage_enabled
            and not allow_existing_reclassification
        ):
            raise MemoryPolicyError("sensitive_storage_disabled")
        if (
            high_sensitivity
            and not self.sensitive_storage_enabled
            and not allow_existing_reclassification
        ):
            raise MemoryPolicyError("sensitive_storage_disabled")
        return sensitivity

    def validate_content(
        self,
        content: str,
        sensitivity: str,
        *,
        allow_existing_reclassification: bool = False,
    ) -> str:
        normalized = normalize_content(content, max_chars=self.max_item_chars)
        detection_views = credential_detection_views(normalized)
        if any(
            pattern.search(view)
            for view in detection_views
            for pattern in _SECRET_PATTERNS
        ):
            raise MemoryPolicyError("secret_detected")
        if any(pattern.search(normalized) for pattern in _TEST_PATTERNS):
            raise MemoryPolicyError("forbidden_test_content")
        if any(pattern.search(normalized) for pattern in _ERROR_LOG_PATTERNS):
            raise MemoryPolicyError("forbidden_log_content")
        if any(pattern.search(normalized) for pattern in _TECHNICAL_ID_PATTERNS):
            raise MemoryPolicyError("technical_identifier_forbidden")
        self.validate_sensitivity(
            sensitivity,
            normalized,
            allow_existing_reclassification=allow_existing_reclassification,
        )
        return normalized

    def validate_provenance_inputs(
        self, kind: str, sources: Iterable[ProvenanceInput],
    ) -> tuple[ProvenanceInput, ...]:
        self.validate_kind(kind)
        result: dict[int, ProvenanceInput] = {}
        for source in sources:
            if (
                not isinstance(source, ProvenanceInput)
                or not isinstance(source.canonical_message_id, int)
                or isinstance(source.canonical_message_id, bool)
                or source.canonical_message_id <= 0
            ):
                raise MemoryPolicyError("invalid_provenance")
            result.setdefault(source.canonical_message_id, source)
        if not result:
            raise MemoryPolicyError("invalid_provenance")
        return tuple(result.values())

    def validate_explicit_create(
        self,
        *,
        kind: str,
        scope_type: str,
        scope_ref: str,
        content: str,
        sensitivity: str,
        sources: Iterable[ProvenanceInput],
        allow_existing_reclassification: bool = False,
    ) -> tuple[str, tuple[ProvenanceInput, ...]]:
        self.validate_kind(kind)
        self.validate_scope(scope_type, scope_ref)
        normalized = self.validate_content(
            content,
            sensitivity,
            allow_existing_reclassification=allow_existing_reclassification,
        )
        validated_sources = self.validate_provenance_inputs(kind, sources)
        return normalized, validated_sources
