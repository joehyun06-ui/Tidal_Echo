"""Typed server-side contract for the isolated authorization ledger."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from uuid import uuid4

from backend.session_lifecycle import SourceCursor, WindowReceipt
from backend.session_window_api_consumption import ApiWindowConsumptionObservation

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX = 2**63 - 1


class LedgerError(Exception):
    """A stable ledger error code suitable for an adapter."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class OutcomeUnknown(LedgerError):
    def __init__(self, request_id):
        self.request_id = request_id
        super().__init__("activation_outcome_unknown")


def require_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise LedgerError("invalid_identifier")


def require_digest(value):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise LedgerError("invalid_digest")


def require_counter(value, minimum=0):
    if type(value) is not int or not minimum <= value <= _MAX:
        raise LedgerError("invalid_counter")


def canonical(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()


def sha256(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def new_operation_id(incarnation):
    require_counter(incarnation, 1)
    return f"{incarnation}.{uuid4().hex}"


def require_operation_id(operation_id, incarnation):
    if not isinstance(operation_id, str) or not re.fullmatch(r"[1-9][0-9]{0,18}\.[0-9a-f]{32}", operation_id):
        raise LedgerError("invalid_operation_id")
    if operation_id.split(".", 1)[0] != str(incarnation):
        raise LedgerError("stale_incarnation")


@dataclass(frozen=True)
class Scope:
    """Established by the trusted host, never copied from browser fields."""

    principal_id: str
    login_id: str
    conversation_id: str
    dataset_id: str
    source_ids: tuple[str, ...]

    def __post_init__(self):
        for value in (self.principal_id, self.login_id, self.conversation_id, self.dataset_id):
            require_id(value)
        if type(self.source_ids) is not tuple or not 1 <= len(self.source_ids) <= 128:
            raise LedgerError("invalid_source_set")
        for value in self.source_ids:
            require_id(value)
        if tuple(sorted(set(self.source_ids))) != self.source_ids:
            raise LedgerError("invalid_source_set")


@dataclass(frozen=True)
class SourceFence:
    source_id: str
    event_id: str
    revision: int
    position: int
    content_sha256: str
    grant_epoch: int


@dataclass(frozen=True)
class AuthorizationFence:
    scope: Scope
    incarnation: int
    principal_epoch: int
    identity_revision: int
    login_epoch: int
    conversation_revision: int
    version_id: str
    version_revision: int
    conversation_grant_epoch: int
    provider: str
    generation: str
    generation_revision: int
    authorization_epoch: int
    cursor_upper: int
    dataset_grant_epoch: int
    sources: tuple[SourceFence, ...]


@dataclass(frozen=True)
class Credential:
    fence: AuthorizationFence
    valid_until_us: int
    seal: str


@dataclass(frozen=True)
class ConsumptionClaim:
    attempt_request_id: str
    packet_sha256: str
    input_sha256: str
    from_window_id: str
    target_window_id: str
    model_run_id: str
    context_epoch: int
    message_cursor: int

    def __post_init__(self):
        for value in (self.attempt_request_id, self.from_window_id, self.target_window_id, self.model_run_id):
            require_id(value)
        require_digest(self.packet_sha256)
        require_digest(self.input_sha256)
        require_counter(self.context_epoch, 1)
        require_counter(self.message_cursor)
        if self.from_window_id == self.target_window_id:
            raise LedgerError("invalid_consumption_binding")

    def matches(self, fence, observation):
        if type(observation) is not ApiWindowConsumptionObservation:
            return False
        receipt = observation.receipt
        return (
            type(receipt) is WindowReceipt
            and type(receipt.source_cursor) is SourceCursor
            and type(receipt.context_epoch) is int
            and type(receipt.source_cursor.position) is int
            and type(receipt.message_cursor) is int
            and receipt.status == "confirmed"
            and receipt.conversation_id == fence.scope.conversation_id
            and receipt.version_id == fence.version_id
            and receipt.provider == fence.provider
            and receipt.from_window_id == self.from_window_id
            and receipt.target_window_id == self.target_window_id
            and receipt.model_run_id == self.model_run_id
            and receipt.context_epoch == self.context_epoch
            and receipt.packet_sha256 == self.packet_sha256
            and observation.input_sha256 == self.input_sha256
            and receipt.source_cursor.generation == fence.generation
            and receipt.source_cursor.position == fence.cursor_upper
            and receipt.message_cursor == self.message_cursor
        )


@dataclass(frozen=True)
class PreparedActivation:
    credential: Credential
    claim: ConsumptionClaim
    seal: str

    @classmethod
    def from_payload(cls, payload):
        credential = payload["credential"].copy()
        fence = credential["fence"].copy()
        scope = fence["scope"].copy()
        scope["source_ids"] = tuple(scope["source_ids"])
        fence["scope"] = Scope(**scope)
        fence["sources"] = tuple(SourceFence(**source) for source in fence["sources"])
        credential["fence"] = AuthorizationFence(**fence)
        return cls(Credential(**credential), ConsumptionClaim(**payload["claim"]), payload["seal"])

    @property
    def receipt_id(self):
        return sha256(asdict(self))


@dataclass(frozen=True)
class ActivationStatus:
    request_id: str
    state: str
    reason: str | None = None
    effective_now: bool = False
    projection_revision: int | None = None
