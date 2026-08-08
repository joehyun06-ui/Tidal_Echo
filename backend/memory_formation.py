"""Pure candidate formation for Automatic Memory Formation V1.

The contract is deliberately candidate-only.  It accepts exact
``AutoMemoryProposalV1`` instances, extracts only their source spans, delegates
normalization and content policy to :mod:`backend.memory_policy`, and returns
immutable candidates.  It has no persistence, history, provider, network, or
application dependency.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from backend.memory_policy import MemoryPolicy, MemoryPolicyError


MAX_PROPOSALS: Final = 3
SOURCE_MAX_CHARS: Final = 8000
DEFAULT_MAX_ITEM_CHARS: Final = 1000
TOTAL_CANDIDATE_MAX_CHARS: Final = 2000

SCOPE_TYPE: Final = "global_user"
SCOPE_REF: Final = ""
SENSITIVITY: Final = "normal"

SIGNAL_KIND_MAPPING: Final = MappingProxyType({
    "durable_preference": "user_preference",
    "stable_profile": "user_profile",
    "relationship_fact": "relationship",
    "shared_episode": "shared_episode",
    "project_fact": "project",
    "project_decision": "decision",
    "task_progress": "task_or_progress",
})

_ERROR_CATEGORIES: Final = frozenset({
    "candidate_budget_exceeded",
    "candidate_policy_rejected",
    "duplicate_proposal",
    "ineligible_proposal",
    "invalid_max_item_chars",
    "invalid_proposal",
    "invalid_proposals",
    "invalid_signal_type",
    "invalid_source_message_id",
    "invalid_source_text",
    "invalid_span",
    "memory_formation_error",
    "overlapping_proposals",
    "source_text_too_long",
    "too_many_proposals",
})

_ELIGIBILITY_WHITESPACE: Final = re.compile(r"\s+", re.UNICODE)


class MemoryFormationError(ValueError):
    """A stable, data-free candidate-formation failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe_category = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_formation_error"
        )
        self.category = safe_category
        super().__init__(safe_category)

    def __str__(self) -> str:
        try:
            category = object.__getattribute__(self, "category")
        except Exception:
            return "memory_formation_error"
        return (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_formation_error"
        )

    def __repr__(self) -> str:
        return f"MemoryFormationError({str(self)!r})"


@dataclass(frozen=True, slots=True, repr=False)
class AutoMemoryProposalV1:
    """A model- or caller-proposed source span and closed signal class."""

    signal_type: str
    start: int
    end: int

    def __repr__(self) -> str:
        return "<AutoMemoryProposalV1>"


@dataclass(frozen=True, slots=True, repr=False)
class AutoMemoryCandidateV1:
    """A policy-validated candidate; this type performs no Memory write."""

    source_message_id: int
    signal_type: str
    kind: str
    scope_type: str
    scope_ref: str
    normalized_content: str = field(repr=False)
    sensitivity: str

    def __repr__(self) -> str:
        return "<AutoMemoryCandidateV1>"


_GLOBAL_INELIGIBLE_PATTERNS: Final = (
    # Uncertain language cannot establish a durable fact in any signal class.
    re.compile(r"\b(?:maybe|perhaps|might|may|possibly)\b", re.IGNORECASE),
    re.compile(r"(?:可能|也许|或许)"),
    # Explicit roleplay, fiction, or hypothetical framing.
    re.compile(
        r"\b(?:hypothetical(?:ly)?|imagine|suppose|pretend|role[ -]?play|"
        r"fiction(?:al)?|in (?:this|the) (?:story|novel)|if i were|if we were)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:假设|假如|假装|想象|如果我是|如果我们是|角色扮演|虚构|小说里|故事里)"),
    # Explicit joke markers.
    re.compile(r"\b(?:just kidding|kidding|only joking|as a joke|it(?:'| i)s a joke)\b", re.IGNORECASE),
    re.compile(r"(?:开玩笑|玩笑话|逗你的|说笑而已|只是个玩笑|这是个玩笑)"),
    # Commands that prohibit storage or ask for forgetting.
    re.compile(
        r"\b(?:do not|don't|dont|never)\s+(?:remember|store|save|retain)\b|"
        r"\b(?:do not|don't|dont|never)\s+keep\b[^.?!]{0,80}\bin\s+"
        r"(?:memory|storage)\b|"
        r"\bforget\s+(?:this|that|what|my|the|it)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:(?:不要|别|请勿)(?:记住|记|保存|存储|存|留存)|"
        r"(?:忘掉|忘记)(?:这|那|我|刚才))"
    ),
    # Present-moment state without a durable claim.
    re.compile(
        r"\b(?:right now|at the moment|currently|temporarily|for now|recently|today|tonight|"
        r"this (?:morning|afternoon|evening|week))\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:我)?(?:现在|此刻|眼下|目前|暂时|今天|今晚|今早|这周)(?:正|有点|很|在)?"),
    re.compile(r"\bi (?:am|feel|feel a bit|feel very) (?:tired|sleepy|hungry|thirsty|bored|upset)\b", re.IGNORECASE),
    re.compile(r"我(?:有点|很)?(?:困|饿|渴|累|无聊|难过)"),
    re.compile(
        r"\bmy (?:wife|husband|spouse|partner|mother|father|mom|dad|"
        r"sister|brother|daughter|son) is "
        r"(?:tired|sleepy|hungry|thirsty|bored|upset|angry|sad|happy|sick|"
        r"fine|okay|ok|well|busy|away|home|late|early|ready|ill|unwell|"
        r"asleep|awake|working|missing|dead|alive|young|old)\b",
        re.IGNORECASE,
    ),
    # Third-party-only ownership and subjects fail closed for every signal.
    # Direct user-relative spouse/kin naming remains available below.
    re.compile(
        r"\bmy (?:friend|coworker|colleague|neighbor|classmate|boss|manager|"
        r"employee|teammate|client|customer)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:a|an|our) (?:friend|coworker|colleague|neighbor|classmate|"
        r"boss|manager|employee|teammate|client|customer)(?:'s|’s)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bmy (?:wife|husband|spouse|partner|mother|father|mom|dad|"
        r"sister|brother|daughter|son)(?:'s|’s)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:his|her|their) (?:profile|preference|project|task|decision|"
        r"job|work)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b[A-Z][A-Za-z'-]{1,40}(?:'s|’s) "
        r"(?:profile|preference|project|task|decision|job|work)\b"
    ),
    re.compile(r"我(?:的)?(?:朋友|同事|邻居|同学|老板|经理|员工|队友|客户)"),
    re.compile(r"(?:朋友|同事|邻居|同学|老板|经理|员工|队友|客户)的"),
    re.compile(
        r"我的(?:妻子|丈夫|爱人|伴侣|母亲|父亲|妈妈|爸爸|姐姐|妹妹|"
        r"哥哥|弟弟|女儿|儿子)的"
    ),
    re.compile(r"(?:他|她|他们|她们)的(?:资料|偏好|项目|任务|决定|工作)"),
)


_ELIGIBILITY_PATTERNS: Final = {
    "durable_preference": (
        re.compile(
            r"\bi\s+(?:always|usually|generally|typically|consistently)\s+"
            r"(?:really\s+)?(?:prefer|like|love|choose|favor)\b",
            re.IGNORECASE,
        ),
        re.compile(r"\bi\s+prefer\b.+\b(?:over|rather than)\b", re.IGNORECASE),
        re.compile(r"\bmy\s+(?:usual|default|long[ -]?term|go[ -]?to)\s+(?:choice|preference)\b", re.IGNORECASE),
        re.compile(r"我(?:一直|通常|一向|总是|一般|长期)(?:都)?(?:更)?(?:喜欢|偏好|爱|会选|选择)"),
        re.compile(r"我(?:更)?(?:喜欢|偏好).+(?:而不是|胜过|优先于)"),
    ),
    "stable_profile": (
        re.compile(r"\bi (?:work|serve) as (?:an? |the )?[^.?!]+", re.IGNORECASE),
        re.compile(r"\bmy (?:profession|occupation|job|major|field|degree) is\b", re.IGNORECASE),
        re.compile(
            r"\bi(?:'m| am) (?:an?|the) "
            r"(?:(?:product|ux|ui|software|hardware|data|systems?|graphic|web) )?"
            r"(?:designer|engineer|developer|architect|researcher|scientist|"
            r"teacher|professor|doctor|nurse|lawyer|accountant|manager|writer|"
            r"artist|student)\b",
            re.IGNORECASE,
        ),
        re.compile(r"\bi (?:live|reside) in\b", re.IGNORECASE),
        re.compile(r"\bi speak [a-z]", re.IGNORECASE),
        re.compile(r"我的(?:专业|职业|工作|母语|家乡)是"),
        re.compile(r"我是(?:一名|一个|一位|名|位)?[^，。！？,.!?]{1,40}(?:师|员|家|生|者|工程师|设计师)"),
        re.compile(r"我(?:从事|任职于|就职于|居住在|住在)"),
    ),
    "relationship_fact": (
        re.compile(
            r"\b[Mm]y (?:wife|husband|spouse|partner|mother|father|mom|dad|"
            r"sister|brother|daughter|son) (?:is named|is called) "
            r"[A-Z][a-z]{1,39}(?:[-'][A-Z]?[a-z]{1,39})?\b"
        ),
        re.compile(
            r"\bMy (?:wife|husband|spouse|partner|mother|father|mom|dad|"
            r"sister|brother|daughter|son) is "
            r"[A-Z][a-z]{1,39}(?:[-'][A-Z]?[a-z]{1,39})?\b"
        ),
        re.compile(
            r"\bmy (?:wife|husband|spouse|partner|mother|father|mom|dad|"
            r"sister|brother|daughter|son) and i (?:are|have been) "
            r"(?:married|partners|friends|roommates|classmates)\b",
            re.IGNORECASE,
        ),
        re.compile(r"\bi (?:am married to|have been (?:friends|partners) with)\b", re.IGNORECASE),
        re.compile(r"\bwe (?:are|have been) (?:married|friends|partners|roommates|classmates)\b", re.IGNORECASE),
        re.compile(r"我的(?:妻子|丈夫|爱人|伴侣|母亲|父亲|妈妈|爸爸|姐姐|妹妹|哥哥|弟弟|女儿|儿子)(?:叫|名叫)"),
        re.compile(r"我和[^，。！？,.!?]{1,40}是(?:夫妻|伴侣|朋友|(?:大学|高中|中学)?同学|室友|同事)"),
    ),
    "shared_episode": (
        re.compile(
            r"\bwe (?:once |first |finally |last (?:year|month|week) )?"
            r"(?:visited|traveled|went|met|celebrated|built|launched|finished|completed)\b"
            r"[^.?!]*(?:\btogether\b|\blast (?:year|month|week)\b|\bin 20\d{2}\b)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bi remember when we "
            r"(?:visited|traveled|went|met|celebrated|built|launched|finished|completed)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"我们(?:(?:曾经|去年|上个月|上周|第一次)(?:一起)?|一起)"
            r"(?:去|去了|旅行|见面|庆祝|经历|完成|做了)"
        ),
        re.compile(
            r"(?:去年|上个月|上周)我们(?:一起)?"
            r"(?:去|去了|旅行|见面|庆祝|经历|完成|做了)"
        ),
        re.compile(
            r"我记得我们(?:曾经|一起|第一次)"
            r"(?:去|去了|旅行|见面|庆祝|经历|完成|做了)"
        ),
    ),
    "project_fact": (
        re.compile(
            r"\b(?:(?:the|this|our) project|project [a-z0-9._-]+)\s+"
            r"(?:uses|runs on|is built with|depends on|has)\b",
            re.IGNORECASE,
        ),
        re.compile(r"\bour (?:api|service|application|app|backend|frontend) (?:uses|runs on|depends on|is built with)\b", re.IGNORECASE),
        re.compile(r"(?:这个|该|我们的)?项目(?:使用|采用|基于|依赖|运行在)"),
        re.compile(r"项目[^，。！？,.!?]{1,40}(?:使用|采用|基于|依赖|运行在)"),
    ),
    "project_decision": (
        re.compile(
            r"\bwe (?:decided|agreed|chose) to "
            r"(?:keep|make|use|adopt|switch to|migrate to|standardize on|"
            r"implement|deploy|configure|design|build) "
            r"(?:(?:the|this|our|a|an) )?"
            r"(?:project|api|service|application|app|backend|frontend|database|"
            r"schema|migration|repository|repo|codebase|server|protocol|endpoint|"
            r"framework|workflow|pipeline|architecture|interface|postgresql|"
            r"postgres|mysql|sqlite|python|fastapi)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bwe (?:decided|agreed|chose|standardized) on "
            r"(?:(?:the|this|our|a|an) )?"
            r"(?:project|api|service|application|app|backend|frontend|database|"
            r"schema|migration|repository|repo|codebase|server|protocol|endpoint|"
            r"framework|workflow|pipeline|architecture|interface|postgresql|"
            r"postgres|mysql|sqlite|python|fastapi)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:we decided|it was decided) that (?:the|this|our) "
            r"(?:project|api|service|application|app|backend|frontend|database|"
            r"schema|migration|repository|codebase|server|protocol|endpoint|"
            r"framework|workflow|pipeline|architecture|interface)\b",
            re.IGNORECASE,
        ),
        re.compile(r"\bfrom now on,? (?:this|the|our) project (?:will|uses?|is going to)\b", re.IGNORECASE),
        re.compile(
            r"我们(?:决定|商定|选定)(?:这个|该|我们的)项目(?:统一)?"
            r"(?:使用|采用|改用|保持|部署|基于)"
        ),
        re.compile(
            r"(?:这个|该|我们的)?项目(?:以后|今后)(?:统一)?"
            r"(?:使用|采用|改用|保持|部署|基于)"
        ),
    ),
    "task_progress": (
        re.compile(
            r"\b(?:(?:the|this|our) )?"
            r"(?:project|task|ticket|issue|milestone|sprint|workflow|pipeline|"
            r"deployment|migration|database|schema|api|service|backend|frontend|"
            r"feature|bug|test suite|documentation|implementation|integration|"
            r"repository|codebase)"
            r"(?:[ -][a-z0-9._-]+){0,4} "
            r"(?:has been|have been|is|are) "
            r"(?:completed|finished|done|deployed|migrated|shipped)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bwe (?:completed|finished|deployed|migrated|shipped) "
            r"(?:(?:the|this|our) )?"
            r"(?:project|task|ticket|issue|milestone|workflow|pipeline|deployment|"
            r"migration|database|schema|api|service|backend|frontend|feature|"
            r"test suite|documentation|implementation|integration|repository|"
            r"codebase)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:项目|任务|工单|问题|里程碑|冲刺|工作流|工作流程|管线|构建|"
            r"部署|发布|迁移|数据库|数据模式|接口|服务|后端|前端|功能|缺陷|"
            r"测试套件|文档|实现|集成|代码库|代码)"
            r"[^，。！？,.!?]{0,32}(?:已经|已)(?:完成|做完|上线|部署|迁移|交付)"
        ),
    ),
}


def _raise(category: str) -> None:
    raise MemoryFormationError(category)


def _validate_source(source_message_id: object, source_text: object) -> str:
    if (
        type(source_message_id) is not int
        or source_message_id <= 0
    ):
        _raise("invalid_source_message_id")
    if type(source_text) is not str:
        _raise("invalid_source_text")
    if len(source_text) > SOURCE_MAX_CHARS:
        _raise("source_text_too_long")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in source_text):
        _raise("invalid_source_text")
    return source_text


def _validate_max_item_chars(max_item_chars: object) -> int:
    if (
        type(max_item_chars) is not int
        or max_item_chars < 64
        or max_item_chars > 4096
    ):
        _raise("invalid_max_item_chars")
    return max_item_chars


def _validated_sorted_proposals(
    proposals: object,
    *,
    source_length: int,
) -> tuple[AutoMemoryProposalV1, ...]:
    if type(proposals) not in (list, tuple):
        _raise("invalid_proposals")
    if len(proposals) > MAX_PROPOSALS:
        _raise("too_many_proposals")

    validated: list[AutoMemoryProposalV1] = []
    seen: set[tuple[str, int, int]] = set()
    for proposal in proposals:
        if type(proposal) is not AutoMemoryProposalV1:
            _raise("invalid_proposal")
        signal_type = proposal.signal_type
        start = proposal.start
        end = proposal.end
        if type(signal_type) is not str or signal_type not in SIGNAL_KIND_MAPPING:
            _raise("invalid_signal_type")
        if type(start) is not int or type(end) is not int:
            _raise("invalid_span")
        if not 0 <= start < end <= source_length:
            _raise("invalid_span")
        identity = (signal_type, start, end)
        if identity in seen:
            _raise("duplicate_proposal")
        seen.add(identity)
        validated.append(proposal)

    result = tuple(sorted(validated, key=lambda item: (item.start, item.end, item.signal_type)))
    previous_end = -1
    for proposal in result:
        if proposal.start < previous_end:
            _raise("overlapping_proposals")
        previous_end = proposal.end
    return result


def _source_eligibility_view(validated_source: str) -> str:
    """Return a bounded transient view used only for formation vetoes."""

    normalized = unicodedata.normalize(
        "NFC",
        validated_source.replace("\r\n", "\n").replace("\r", "\n"),
    )
    return _ELIGIBILITY_WHITESPACE.sub(" ", normalized).strip()


def _source_has_formation_veto(source_eligibility_view: str) -> bool:
    return any(
        pattern.search(source_eligibility_view)
        for pattern in _GLOBAL_INELIGIBLE_PATTERNS
    )


def _is_eligible(signal_type: str, normalized_content: str) -> bool:
    return any(
        pattern.search(normalized_content)
        for pattern in _ELIGIBILITY_PATTERNS[signal_type]
    )


def build_auto_memory_candidates(
    source_message_id: object,
    source_text: object,
    proposals: object,
    *,
    max_item_chars: object = DEFAULT_MAX_ITEM_CHARS,
) -> tuple[AutoMemoryCandidateV1, ...]:
    """Build deterministic V1 candidates from exact source spans.

    The public proposal representation is ``AutoMemoryProposalV1`` only.
    Exact ``list`` and ``tuple`` proposal containers are accepted; mappings,
    iterators, subclasses, and duck-typed objects fail closed.
    """

    validated_source = _validate_source(source_message_id, source_text)
    validated_max_item_chars = _validate_max_item_chars(max_item_chars)
    validated_proposals = _validated_sorted_proposals(
        proposals,
        source_length=len(validated_source),
    )
    if not validated_proposals:
        return ()

    source_eligibility_view = _source_eligibility_view(validated_source)
    if _source_has_formation_veto(source_eligibility_view):
        raise MemoryFormationError("ineligible_proposal")

    try:
        policy = MemoryPolicy(
            max_item_chars=validated_max_item_chars,
            sensitive_storage_enabled=False,
        )
        policy.validate_scope(SCOPE_TYPE, SCOPE_REF)
    except MemoryPolicyError:
        raise MemoryFormationError("candidate_policy_rejected") from None

    candidates: list[AutoMemoryCandidateV1] = []
    total_chars = 0
    for proposal in validated_proposals:
        kind = SIGNAL_KIND_MAPPING[proposal.signal_type]
        source_span = validated_source[proposal.start:proposal.end]
        try:
            policy.validate_kind(kind)
            normalized_content = policy.validate_content(source_span, SENSITIVITY)
        except MemoryPolicyError:
            raise MemoryFormationError("candidate_policy_rejected") from None
        if not _is_eligible(proposal.signal_type, normalized_content):
            raise MemoryFormationError("ineligible_proposal")
        total_chars += len(normalized_content)
        if total_chars > TOTAL_CANDIDATE_MAX_CHARS:
            raise MemoryFormationError("candidate_budget_exceeded")
        candidates.append(
            AutoMemoryCandidateV1(
                source_message_id=source_message_id,
                signal_type=proposal.signal_type,
                kind=kind,
                scope_type=SCOPE_TYPE,
                scope_ref=SCOPE_REF,
                normalized_content=normalized_content,
                sensitivity=SENSITIVITY,
            )
        )
    return tuple(candidates)
