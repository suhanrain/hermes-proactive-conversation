"""Content assembly for a proactive wake — the read-only side.

What this module is for
-----------------------
A wake may only become a message when the plugin can point at **one real, recent record** it is
allowed to read. The assembly answers three questions, all decidable in code:

1. **Which sources may be read at all?** Only the frozen whitelist in ``contract.CONTENT_SOURCES``
   (this session's transcript and cross-session memory). Anything else is not
   reachable *through this module*: :class:`SourceRegistry` has no entry for it and answers
   ``denied:unknown-source``; an adapter that would fetch the network or write is refused at
   registration (``denied:network-source`` / ``denied:write-source``) and its own counter stays 0.
   Every read is bound to **one** session's :class:`Scope` — a read asking for another session or
   profile is refused with ``denied:scope-mismatch`` before any adapter is called.
2. **Is there a reliable topic?** A record only counts when it is about *this* user, inside the
   freshness window the caller configured, not sensitive-health (unless the single switch is on)
   and not part of a set of records that contradict each other. When nothing survives, the
   assembly is **silent**: no candidate text, no material, nothing that could be sent — and no
   invented topic. Unavailable sources (403/timeout) simply contribute no records; a weaker source
   is never promoted into a concrete claim.
3. **Would the candidate break the expression boundary?** A decidable pattern table (pressure /
   blame / dependency-inducing wording) is applied to the candidate before it is handed on; a
   candidate that trips it is dropped, never "softened" silently.

The output is an :class:`Assembly`: ``status``/``reason`` codes for the report, a ``candidate``
sentence and a ``material`` block for the turn, the provenance ``fingerprint``, plus an ``audit``
dict that contains **only** session digests, statuses, reasons and counts — never record text,
record ids, health values or credentials.

No clock and no RNG are read here: ``now`` and everything time-shaped (the freshness window) is
passed in. There is deliberately no default for the freshness window — an unconfigured window is
``silent:policy-incomplete``, not a guess (the user's "时间次数不得写死" rule).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import contract as C
from . import locales as _locales

logger = logging.getLogger(__name__)

#: Per-process salt for the *identifier* digests written to logs. Because it is fresh every process
#: a digest is irreversible and cannot be joined across restarts — and nothing here persists it,
#: so there is no key to leak. Record-text digests are deliberately NOT salted: a provenance
#: fingerprint must be re-derivable from the record itself (see :func:`text_digest`).
_SALT = os.urandom(16)

# ── provenance digests ──────────────────────────────────────────────────────────────────────


def short_digest(value: Any) -> str:
    """12 hex chars of a salted SHA-256: an irreversible, non-linkable short summary.

    Used for session identifiers in logs/audit. Never reversible to the identifier, and not
    comparable across processes.
    """
    text = "" if value is None else str(value)
    return hashlib.sha256(_SALT + text.encode("utf-8", "replace")).hexdigest()[:12]


def text_digest(value: Any) -> str:
    """SHA-256 of a record's text — the integrity half of a provenance fingerprint.

    Unsalted on purpose: anyone holding the record can recompute it, which is exactly what makes
    "the message carries a fingerprint that hits the original record" checkable.
    """
    text = "" if value is None else str(value)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# ── the session scope every read is bound to ────────────────────────────────────────────────


@dataclass(frozen=True)
class Scope:
    """The identity one assembly run may read for. Mirrors ``store.dims_for`` key for key."""

    session_key: str = ""
    session_id: str = ""
    profile: Optional[str] = None
    platform: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None

    @classmethod
    def from_dims(cls, dims: Any) -> "Scope":
        d = dims if isinstance(dims, dict) else {}
        return cls(
            session_key=str(d.get("session_key") or ""),
            session_id=str(d.get("session_id") or ""),
            profile=d.get("profile"),
            platform=d.get("platform"),
            chat_id=d.get("chat_id"),
            thread_id=d.get("thread_id"),
        )

    @property
    def usable(self) -> bool:
        return bool(self.session_key)

    def digest(self) -> str:
        """The only form of this scope that may reach a log line."""
        return short_digest(f"{self.session_key}\x1f{self.session_id}")


# ── records and sources ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Record:
    """One read-only record a source offers as a possible topic.

    ``subject`` — ``"user"`` when the record is about the person we are messaging; anything else
    (a note about somebody else, an unrelated archive entry) is unusable as a topic.
    ``topic_key`` — records sharing a key are about the same thing; if they disagree on ``value``
    the whole group is dropped (a contradiction is not a topic).
    ``sensitive`` — health-shaped material (HRV / sleep duration / weight). Never cited: a sensitive
    record is dropped from the citable set unconditionally.
    """

    source: str
    record_id: str
    at: float
    text: str
    subject: str = "user"
    topic_key: Optional[str] = None
    value: Optional[str] = None
    sensitive: bool = False

    def fingerprint(self) -> str:
        """``source:record_id:text-digest`` — resolvable back to this exact record."""
        return f"{self.source}:{self.record_id}:{text_digest(self.text)[:8]}"


class SourceUnavailable(Exception):
    """Raised by an adapter when the source cannot be read right now (403, timeout, offline).

    The reason is a code (``unavailable:...``), not prose: the report and the tests assert on it.
    """

    def __init__(self, reason: str = C.UNAVAILABLE_READ_FAILED, *, detail: str = "") -> None:
        super().__init__(f"{reason}{': ' + detail if detail else ''}")
        self.reason = reason or C.UNAVAILABLE_READ_FAILED


class SourceAdapter:
    """Base class for a read-only source adapter.

    Subclasses declare their own ``name`` from the frozen whitelist, keep ``read_only = True`` and
    ``network = False``, and implement :meth:`read` for the scope they are handed. A plain object
    with the same attributes works too (duck typing), which is what the injected test adapters are.
    """

    name: str = ""
    read_only: bool = True
    network: bool = False

    def read(self, scope: Scope) -> Sequence[Record]:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class StaticSource(SourceAdapter):
    """A reference adapter over a fixed record set, for tests and probes.

    ``failure`` makes it raise :class:`SourceUnavailable` (403/timeout) on every read; ``calls``
    counts the reads that actually reached it, which is how the tests show a refused request never
    touched an adapter.
    """

    name: str
    records: Sequence[Record] = ()
    network: bool = False
    read_only: bool = True
    failure: Optional[str] = None
    calls: int = 0

    def read(self, scope: Scope) -> Sequence[Record]:
        self.calls += 1
        if self.failure:
            raise SourceUnavailable(self.failure)
        return tuple(self.records)


@dataclass(frozen=True)
class SourceOutcome:
    """What one whitelisted source contributed to an assembly run (status + reason + records)."""

    name: str
    status: str
    reason: str = ""
    records: Tuple[Record, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == C.SOURCE_STATUS_OK


class SourceRegistry:
    """The assembly path's **only** door to a source.

    Bound to one :class:`Scope` at construction. Registration refuses an adapter that is not on the
    whitelist, that would use the network, or that could write; reads refuse an unknown name and a
    read aimed at another scope. Both refusals are code-level (the adapter is never called), which
    is what makes them evidence rather than prompt etiquette.
    """

    def __init__(self, scope: Scope) -> None:
        self._scope = scope
        self._adapters: Dict[str, Any] = {}
        self._refused: Dict[str, str] = {}

    @property
    def scope(self) -> Scope:
        return self._scope

    def register(self, adapter: Any) -> str:
        """Register one adapter; returns ``""`` on success, else the denial reason code."""
        name = str(getattr(adapter, "name", "") or "")
        if name not in C.CONTENT_SOURCES:
            self._refused[name] = C.DENIED_UNKNOWN_SOURCE
            return C.DENIED_UNKNOWN_SOURCE
        if bool(getattr(adapter, "network", False)):
            self._refused[name] = C.DENIED_NETWORK_SOURCE
            return C.DENIED_NETWORK_SOURCE
        if not bool(getattr(adapter, "read_only", True)):
            self._refused[name] = C.DENIED_WRITE_SOURCE
            return C.DENIED_WRITE_SOURCE
        writer = getattr(adapter, "write", None)
        if callable(writer):
            self._refused[name] = C.DENIED_WRITE_SOURCE
            return C.DENIED_WRITE_SOURCE
        self._adapters[name] = adapter
        return ""

    def register_all(self, adapters: Iterable[Any]) -> Dict[str, str]:
        """Register a batch; returns ``{name: reason}`` for the refusals only."""
        out: Dict[str, str] = {}
        for adapter in adapters or ():
            reason = self.register(adapter)
            if reason:
                out[str(getattr(adapter, "name", "") or "")] = reason
        return out

    def registered(self) -> Tuple[str, ...]:
        return tuple(name for name in C.CONTENT_SOURCES if name in self._adapters)

    def refused(self) -> Dict[str, str]:
        return dict(self._refused)

    def read(self, name: str, *, scope: Optional[Scope] = None) -> SourceOutcome:
        """Read one whitelisted source for this registry's own scope (never raises)."""
        if name not in C.CONTENT_SOURCES:
            return SourceOutcome(name, C.SOURCE_STATUS_DENIED, C.DENIED_UNKNOWN_SOURCE)
        if scope is not None and scope != self._scope:
            # Another chat / another profile / another session: unreachable by construction — the
            # adapter is not even consulted.
            return SourceOutcome(name, C.SOURCE_STATUS_DENIED, C.DENIED_SCOPE)
        adapter = self._adapters.get(name)
        if adapter is None:
            return SourceOutcome(name, C.SOURCE_STATUS_UNAVAILABLE, C.UNAVAILABLE_NOT_COMPOSED)
        try:
            records = tuple(adapter.read(self._scope) or ())
        except SourceUnavailable as exc:
            return SourceOutcome(name, C.SOURCE_STATUS_UNAVAILABLE, exc.reason)
        except Exception as exc:  # a broken adapter is an unavailable source, never a traceback path
            logger.warning("proactive-conversation: source %s read failed (%s)", name, type(exc).__name__)
            return SourceOutcome(name, C.SOURCE_STATUS_UNAVAILABLE, C.UNAVAILABLE_READ_FAILED)
        return SourceOutcome(name, C.SOURCE_STATUS_OK, "", records)


# ── policy (the assembly's knobs) ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContentPolicy:
    """Assembly knobs.

    ``recent_record_seconds`` — what "recent" means for the greeting requirement. **There is no
    default**: the caller (composition) must supply it, so no time boundary is hard-coded in this
    package (user rule: 时间次数不得写死). An unset/invalid window means the assembly cannot decide
    "recent" and stays silent (``silent:policy-incomplete``) instead of guessing.

    Health values are never configurable: a ``Record.sensitive`` record (HRV, sleep duration,
    weight) is always dropped from the citable set, so a wake whose only material is a health value
    is silent. There is no switch that could make one citable.
    """

    recent_record_seconds: Optional[float] = None

    @property
    def usable(self) -> bool:
        value = self.recent_record_seconds
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


# ── expression boundary ─────────────────────────────────────────────────────────────────────

#: Pressure / blame / dependency-inducing wording. Each entry is ``(pattern_id, regex)``; the
#: pattern id is what tests and the report quote, so a match is decidable rather than a judgement
#: call. Kept deliberately small and explicit: this is a floor, not a style guide.
#:
#: The data lives in :mod:`proactive.locales` (the ``zh-Hans`` profile); this name is the shipped
#: Chinese table. The *active* profile — selected by
#: ``proactive_locale`` and resolved at composition time — supplies the patterns an assembly actually
#: uses (see :class:`Assembler` and :func:`expression_violation`). An unset ``proactive_locale`` is
#: neutral: no patterns, no expression filtering.
EXPRESSION_PATTERNS: Tuple[Tuple[str, str], ...] = _locales.ZH_HANS.expression_patterns

#: The default (zh-Hans) compiled patterns — what :func:`expression_violation` uses when a caller does
#: not pass an explicit profile's patterns.
_EXPRESSION_RES = _locales.expression_resources(_locales.ZH_HANS)


def expression_violation(text: Any,
                         patterns: Optional[Tuple[Tuple[str, Any], ...]] = None) -> Optional[str]:
    """The pattern id this text trips, or ``None`` when it is inside the boundary.

    ``patterns`` is a compiled ``(pattern_id, re.Pattern)`` table (as returned by
    :func:`proactive.locales.expression_resources`). When it is ``None`` the zh-Hans table
    is used. An assembly passes the **active profile's**
    compiled patterns, which may be empty (neutral: no expression filtering at all).
    """
    if not isinstance(text, str) or not text:
        return None
    table = _EXPRESSION_RES if patterns is None else patterns
    for pattern_id, regex in table:
        if regex.search(text):
            return pattern_id
    return None


def inside_expression_boundary(text: Any,
                               patterns: Optional[Tuple[Tuple[str, Any], ...]] = None) -> bool:
    """``True`` when the text carries no pressure / blame / dependency-inducing wording."""
    return expression_violation(text, patterns) is None


# ── rendering ───────────────────────────────────────────────────────────────────────────────

#: One short line of the record's own words is quoted; longer records are clipped. A rendering
#: bound on the quoted fragment (not a throttle): it only limits how much of a record is echoed.
_TEMPLATES: Dict[str, str] = {
    C.SOURCE_SESSION: "上次你提到「{t}」，后来怎么样了？",
    C.SOURCE_MEMORY: "我记得你之前说过「{t}」，最近有变化吗？",
}
_DEFAULT_TEMPLATE = "你之前提到「{t}」，后来怎么样了？"


def clip(text: Any, limit: int = C.CANDIDATE_MAX_CHARS) -> str:
    """First line of a record's text, clipped to ``limit`` characters (deterministic)."""
    raw = "" if text is None else str(text)
    first = raw.splitlines()[0].strip() if raw.strip() else ""
    first = re.split(r"(?<=[。！？!?])", first)[0].strip() or first
    return first if len(first) <= limit else first[: limit - 1] + "…"


def render_candidate(record: Record) -> str:
    """The one sentence this record supports — always derived from the record, never invented."""
    template = _TEMPLATES.get(record.source, _DEFAULT_TEMPLATE)
    return template.format(t=clip(record.text))


def _stamp(at: float) -> str:
    try:
        return datetime.fromtimestamp(float(at), tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:  # pragma: no cover - defensive
        return "unknown"


# ── the must-reply materials (one chat per released round) ──────────────────────────────────
#
# A released round must produce one chat: 「不管如何，LLM 必须发送一条聊天」. The topic rules stay
# exactly as they are; what a verdict costs the user is what the opener path changes.

#: The block marker of the assembled material (candidate + provenance). The seam must recognise it: a
#: model that sends this block back instead of a sentence has produced no chat (the ``echo`` case).
MATERIAL_ASSEMBLED = "[proactive-conversation:assembled]"

#: The block marker of the opener material — everything an opener round may carry. Also how the seam
#: recognises "the model echoed the material back instead of talking".
MATERIAL_OPENER = "[proactive-conversation:opener]"

#: Prefix of the log reason a wake earns when it is opened *without* a citable topic. The verdict is
#: not rewritten (``opener:silent:health`` states both facts): the topic rules stay as they are, and
#: only what a verdict is allowed to cost the user changes.
OPENER_PREFIX = "opener:"


def opener_reason(reason: str) -> str:
    """The log reason for a round opened by the opener path: ``opener:<the verdict it earned>``."""
    return f"{OPENER_PREFIX}{(reason or C.SILENT_NO_TOPIC)}"


#: The header that frames the carried conversation window in the generation request. The window is
#: bounded (N rounds, N configurable) so the request never becomes a whole-session dump.
CONTEXT_WINDOW_HEADER = "下面是这个会话最近的一段对话（从旧到新）："

#: The instruction that frames the window: the reply must be *about* the conversation, and the
#: privacy rules still apply **inside** the window — a health value, a bill, a schedule that
#: appears in a row is still not a fact this round may cite.
CONTEXT_WINDOW_FOOTER = (
    "请根据这段对话自然地接话或开启相关话题；"
    "不得引用其中的健康数值、作息、账单这类私人信息，也不得编造对话里没有的事。")


def render_context_block(rows: Sequence) -> str:
    """The framed conversation window for the generation request, or ``""`` when empty.

    ``rows`` — ``[(role, text), …]`` oldest-first from the host adapter. The roles are
    labelled in Chinese for the model (``用户`` / ``助手``); anything else keeps its own role
    string rather than being silently relabelled. Machine rows never reach this function: the
    seam filters to ``user`` / ``assistant`` with non-empty content.
    """
    if not rows:
        return ""
    labels = {"user": "用户", "assistant": "助手"}
    lines = [CONTEXT_WINDOW_HEADER]
    for row in rows:
        try:
            role, text = row[0], row[1]
        except Exception:  # pragma: no cover - a malformed row is skipped, never fatal
            continue
        label = labels.get(str(role or ""), str(role or "?"))
        body = str(text or "").strip()
        if not body:
            continue
        lines.append(f"{label}: {body}")
    lines.append(CONTEXT_WINDOW_FOOTER)
    return "\n".join(lines)


def render_material(candidate: str, fingerprint: str, record: Record,
                    toolbox: Any = None) -> str:
    """The block the plugin injects into the released turn (the LLM's only allowed material)."""
    tools = ", ".join(toolbox.allowed_names()) if toolbox is not None and hasattr(toolbox, "allowed_names") else ""
    return "\n".join([
        f"{MATERIAL_ASSEMBLED} 只读来源装配（插件生成）",
        f"候选消息: {candidate}",
        f"线索指纹: {fingerprint}（来源 {record.source}，记录时间 {_stamp(record.at)}）",
        "约束: 只可引用上面的候选消息与指纹；不得引入健康数值；不得编造其它旧话题；"
        "必须给用户发一条自然的聊天消息，不得只回复 [SILENT]。",
        f"可用工具: 只读（{tools or '无'}）；写与外部发送工具在本回合不可用。",
    ])


def render_opener_material(reason: str = "", toolbox: Any = None) -> str:
    """The block for a wake opened with **nothing to cite**.

    The topic rules do not move: the assembly still refuses to promote a stale, health, unrelated
    or coercive record, and still reports ``silent:*`` per source. What that verdict is allowed to
    cost the user is what changes (「不管如何，LLM 必须发送一条聊天」): instead of retiring the wake, the
    round opens with this block, and the block says the one thing that is still true — there is
    nothing to continue, so open a conversation instead of pretending to remember one.

    What it must never carry: a candidate, a fingerprint, a record, or the plugin's own reason code.
    ``reason`` is accepted for the caller's symmetry and is deliberately **not** rendered — the model
    has no business seeing ``silent:health``, and a leak here would be a data disclosure with no
    upside.

    The sensitive-fact rule is spelled out rather than implied (「须保证无可引用素材时不会将
    旧会话误当已核实线索」). "No citable record" is not the same as "the turn knows nothing": the host
    still hands the session's own transcript to the model, and a round that says only 「不要续写旧话题」
    invites it to treat that transcript as a verified lead — the health kind especially, where the very
    reason there is nothing citable is that the only records were private measurements. So the block
    names what may not be brought up (past conversations, health figures, itineraries, money) and says
    plainly that appearing in the history does not make any of it citable here.
    """
    tools = ", ".join(toolbox.allowed_names()) if toolbox is not None and hasattr(toolbox, "allowed_names") else ""
    return "\n".join([
        f"{MATERIAL_OPENER} 无可用线索（插件生成）",
        "情况: 这一轮没有找到可以引用的近期记录，因此不要续写任何旧话题。",
        "要求: 用一两句自然的日常口吻，主动开启一个轻松的话题，或者关心一下用户此刻的状态；"
        "不要声称记得任何具体的事，也不要复述这段说明。",
        "禁止: 不得回复 [SILENT] 或任何其它静默标记；本条消息必须是一条真正发给用户看的聊天。",
        "隐私: 不得引用、总结或暗示过去对话里的任何内容，也不得引用健康数值、作息、账单这类"
        "私人信息；这些内容即使出现在对话历史里，也不属于本轮已核实的事实，不要提及。",
        f"可用工具: 只读（{tools or '无'}）；写与外部发送工具在本回合不可用。",
    ])


def resolve_fingerprint(outcomes: Iterable[SourceOutcome], fingerprint: str) -> Optional[Record]:
    """The record a fingerprint points at, or ``None`` when it does not resolve.

    Only records that were actually read (status ``ok``) are considered: a fingerprint can never
    be resolved against a source that was unavailable.
    """
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    for outcome in outcomes or ():
        if not outcome.ok:
            continue
        for record in outcome.records:
            if record.fingerprint() == fingerprint and record.source == outcome.name:
                return record
    return None


# ── the assembly ────────────────────────────────────────────────────────────────────────────


@dataclass
class Assembly:
    """The result of one assembly run.

    ``status`` is ``"ready"`` (a candidate + material exist) or ``"silent"`` (nothing sendable was
    produced — ``candidate`` and ``material`` are empty strings and ``topic`` is ``None``).
    ``tools`` are the injected (disabled) write tools the generation path may touch — callable, and
    harmless: see :mod:`proactive.permissions`.
    """

    status: str = "silent"
    reason: str = ""
    candidate: str = ""
    material: str = ""
    fingerprint: str = ""
    topic: Optional[Record] = None
    sources: Tuple[Tuple[str, str, str, int], ...] = ()
    audit: Dict[str, Any] = field(default_factory=dict)
    tools: Tuple[Any, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status == C.ASSEMBLY_READY

    @property
    def silent(self) -> bool:
        return self.status == C.ASSEMBLY_SILENT


def _empty(status: str, reason: str, *, scope: Scope, outcomes: Sequence[SourceOutcome],
           counts: Optional[Dict[str, int]] = None, tools: Sequence[Any] = (),
           topic: Optional[Record] = None) -> Assembly:
    """A silent (or ready) assembly with a digest-only audit trail."""
    return Assembly(
        status=status,
        reason=reason,
        topic=topic,
        sources=tuple((o.name, o.status, o.reason, len(o.records)) for o in outcomes),
        audit={
            "session": scope.digest(),
            "status": status,
            "reason": reason,
            "sources": [[o.name, o.status, o.reason, len(o.records)] for o in outcomes],
            "counts": dict(counts or {}),
            "topic": ({"source": topic.source, "record": short_digest(f"{topic.source}/{topic.record_id}")}
                      if topic is not None else None),
        },
        tools=tuple(tools),
    )


class Assembler:
    """Turns read-only sources into at most one citable candidate — or into silence."""

    def __init__(self, sources: Sequence[Any] = (), policy: Optional[ContentPolicy] = None, *,
                 toolbox: Any = None, injected_tools: Sequence[Any] = (),
                 expression_patterns: Optional[Tuple[Tuple[str, Any], ...]] = None) -> None:
        self._sources = tuple(sources or ())
        self.policy = policy if policy is not None else ContentPolicy()
        self._toolbox = toolbox
        self._injected = tuple(injected_tools or ())
        # The expression boundary the active locale profile supplies. ``None`` keeps the zh-Hans
        # default (a directly-built assembler behaves the same); the configured composition passes
        # the active profile's compiled patterns, which may be **empty**
        # (neutral / unset locale ⇒ no expression filtering at all).
        self._expression_res = (_EXPRESSION_RES if expression_patterns is None
                                else tuple(expression_patterns))

    def _violation(self, text: Any) -> Optional[str]:
        """The expression pattern id *text* trips under this assembler's profile, or ``None``."""
        return expression_violation(text, self._expression_res)

    @property
    def toolbox(self) -> Any:
        """The read-only tool door this assembly path runs with (``None`` when none was injected)."""
        return self._toolbox

    @property
    def injected_tools(self) -> Tuple[Any, ...]:
        """The callable-but-disabled write tools handed to the generation path."""
        return self._injected

    def assemble(self, scope: Scope, now: float) -> Assembly:
        """Run one assembly. Never raises: an internal error is silence, never a broken turn."""
        try:
            return self._assemble(scope, now)
        except Exception as exc:  # fail quiet: no candidate, no material, no invented topic
            logger.warning("proactive-conversation: content assembly failed (%s) for %s",
                           type(exc).__name__, getattr(scope, "digest", lambda: "?")())
            return _empty(C.ASSEMBLY_SILENT, C.SILENT_INTERNAL, scope=scope, outcomes=(),
                          tools=self._injected)

    # ── internals ───────────────────────────────────────────────────────────────────────────

    def _assemble(self, scope: Scope, now: float) -> Assembly:
        if not getattr(scope, "usable", False):
            return _empty(C.ASSEMBLY_SILENT, C.SILENT_POLICY, scope=scope, outcomes=(),
                          tools=self._injected)
        if not self.policy.usable:
            # No configured "recent" boundary: refuse to decide what is recent instead of guessing.
            return _empty(C.ASSEMBLY_SILENT, C.SILENT_POLICY, scope=scope, outcomes=(),
                          tools=self._injected)

        registry = SourceRegistry(scope)
        refusals = registry.register_all(self._sources)
        if refusals:
            logger.warning("proactive-conversation: refused source adapters for %s: %s",
                           scope.digest(), sorted(refusals.items()))
        outcomes = tuple(registry.read(name) for name in C.CONTENT_SOURCES)

        window = float(self.policy.recent_record_seconds)
        counts = {"records": 0, "unrelated": 0, "health": 0, "stale": 0, "empty": 0, "accepted": 0}
        accepted: List[Record] = []
        for outcome in outcomes:
            for record in outcome.records:
                counts["records"] += 1
                if not isinstance(record, Record):
                    counts["empty"] += 1
                    continue
                if record.subject != "user":
                    counts["unrelated"] += 1
                    continue
                if not str(record.text or "").strip():
                    counts["empty"] += 1
                    continue
                if record.sensitive:
                    counts["health"] += 1
                    continue
                if _stale(record, now, window):
                    counts["stale"] += 1
                    continue
                accepted.append(record)
        accepted, conflicts = _drop_contradictions(accepted)
        counts["conflict"] = conflicts
        counts["accepted"] = len(accepted)

        if not accepted:
            return _empty(C.ASSEMBLY_SILENT, _silence_reason(outcomes, counts), scope=scope,
                          outcomes=outcomes, counts=counts, tools=self._injected)

        for record in _ordered(accepted):
            candidate = render_candidate(record)
            violation = self._violation(candidate)
            if violation:
                # The record's own wording is inside the boundary? Then it is not usable — dropped,
                # never softened into a different claim.
                logger.info("proactive-conversation: dropped a candidate for %s (%s)",
                            scope.digest(), C.SILENT_EXPRESSION)
                counts["expression"] = counts.get("expression", 0) + 1
                continue
            fingerprint = record.fingerprint()
            material = render_material(candidate, fingerprint, record, self._toolbox)
            if self._violation(material):
                counts["expression"] = counts.get("expression", 0) + 1
                continue
            assembly = _empty(C.ASSEMBLY_READY, C.ASSEMBLY_READY, scope=scope, outcomes=outcomes,
                              counts=counts, tools=self._injected, topic=record)
            assembly.candidate = candidate
            assembly.material = material
            assembly.fingerprint = fingerprint
            return assembly

        return _empty(C.ASSEMBLY_SILENT, C.SILENT_EXPRESSION, scope=scope, outcomes=outcomes,
                      counts=counts, tools=self._injected)


def _stale(record: Record, now: float, window: float) -> bool:
    at = record.at
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return True  # an undated record cannot be shown to be recent
    return float(at) > float(now) + 1.0 or (float(now) - float(at)) > window


def _drop_contradictions(records: Sequence[Record]) -> Tuple[List[Record], int]:
    """Drop every group of records that share a ``topic_key`` but disagree on ``value``.

    A contradiction is not a topic: picking one side would be inventing a fact, so the whole group
    goes. Returns ``(kept, number_of_dropped_records)``.
    """
    groups: Dict[str, List[Record]] = {}
    for record in records:
        if record.topic_key:
            groups.setdefault(record.topic_key, []).append(record)
    bad: set = set()
    dropped = 0
    for key, group in groups.items():
        values = {r.value for r in group if r.value is not None}
        if len(values) > 1:
            bad.add(key)
            dropped += len(group)
    kept = [r for r in records if not (r.topic_key and r.topic_key in bad)]
    return kept, dropped


def _ordered(records: Sequence[Record]) -> List[Record]:
    """Newest first, ties broken deterministically (source order on the whitelist, then id)."""
    order = {name: index for index, name in enumerate(C.CONTENT_SOURCES)}
    return sorted(records, key=lambda r: (-float(r.at), order.get(r.source, len(order)), r.record_id))


def _silence_reason(outcomes: Sequence[SourceOutcome], counts: Mapping[str, int]) -> str:
    """The most specific silence code the run earned (deterministic priority)."""
    if not any(o.ok for o in outcomes):
        return C.SILENT_SOURCE_DOWN
    if counts.get("accepted", 0) == 0:
        for key, code in (("unrelated", C.SILENT_UNRELATED),
                          ("health", C.SILENT_HEALTH),
                          ("stale", C.SILENT_STALE),
                          ("conflict", C.SILENT_CONFLICT)):
            if counts.get(key, 0) > 0:
                return code
    return C.SILENT_NO_TOPIC
