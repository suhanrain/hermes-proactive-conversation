"""``LocaleProfile`` — the per-language rule data the engine reads, activated by ``proactive_locale``.

The language-dependent rules live here as data rather than as literals in the engine: the
pressure/blame/dependency regex table, the pure-acknowledgement table, the length floor, and the
built-in default prompt wording. ``proactive_locale`` selects a profile; ``neutral`` turns the
language rules off while the machine markers still apply.

1. **A data archive** — :class:`LocaleProfile` carries, per language, the *values* the engine applies
   for that language: ``expression_patterns`` (pressure / blame / dependency regex), ``acknowledgement_words``
   (the pure-acknowledgement table), a length floor (``min_chars`` for CJK scripts that do not use
   spaces, ``min_words`` for space-separated languages — ``length_unit`` says which one applies), and
   the built-in default prompt wording (:class:`Wording`). Profiles are pure data; nothing here reads a
   clock, the network or a record.
2. **A resolver** — :func:`resolve_activation` turns the ``proactive_locale`` setting into a
   :class:`LocaleActivation`: which profile is active, and whether language filtering is on at all.

The activation model (owner ruling, this card)
----------------------------------------------
``proactive_locale`` is the **only** switch that turns locale filtering on:

* **unset** → *neutral*: no language regex, no acknowledgement table, no length floor. Only the
  language-independent machine markers and the structural filters still apply. An unconfigured install
  therefore never mis-drops an ordinary message — the behaviour is predictable and safe.
* **``zh-Hans`` / ``en``** → that profile's rules apply (the zh-Hans profile carries the Chinese rule set).
* **``auto``** → the host locale decides (``zh*`` → ``zh-Hans``, ``en*`` → ``en``); when the host locale
  cannot be read, fall back to ``en`` **with a WARNING**. ``auto`` is still *active* (a profile is on).
* **anything else** → **invalid** → WARNING + neutral (fail-safe: never crash, never wrongly filter).

``machine_markers`` is deliberately **global**, not per-language: the strings Hermes injects into a
``role='user'`` row (``[CONTEXT COMPACTION``, ``Gateway message origin (``, ``[Cron delivery: `` …) are
host-owned English envelopes that have nothing to do with the user's language, so they are not a
profile field (see :data:`MACHINE_MARKERS`) and apply in every mode, neutral included.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .. import contract as C
from .. import settings as _settings

logger = logging.getLogger(__name__)


# ── the global, language-independent machine markers ─────────────────────────────────────────
#: The envelopes Hermes injects into a ``role='user'`` row. Each one is a line-start marker of a
#: message that is not the user's own writing (see :mod:`proactive.sources.usability` for the source
#: of each). Host-owned English envelopes ⇒ **global**, not per-language, and applied in every mode
#: (neutral included): quoting one back as 「the user said」 attributes words the user never wrote.
MACHINE_MARKERS: Tuple[str, ...] = (
    "[CONTEXT COMPACTION",
    "[CONTEXT SUMMARY",
    "[PRIOR CONTEXT",
    "Gateway message origin (",
    "[Cron delivery: ",
    "[Replying to: ",
)


# ── the built-in default prompt wording, per language ────────────────────────────────────────
@dataclass(frozen=True)
class Wording:
    """The language-dependent default prompt sentences.

    The dynamic parts stay code-owned, never data: the tool-availability clause must always describe
    what the runtime gate actually does, and the progress sentence is rendered with the *live*
    ``{visible_since_user}`` / ``{minimum_visible_replies}`` counters. So :mod:`proactive.trigger`
    composes ``soft_body + <tool clause> + soft_tail`` and
    ``forced_body_head + forced_anti_repeat + <progress> + <tool clause> + forced_tail`` from these
    pieces. Only the sentences whose *language* matters live here.
    """

    soft_body: str
    soft_tail: str
    forced_body_head: str
    forced_anti_repeat: str
    forced_progress_min: str
    forced_progress_silent: str
    forced_tail: str


#: The English wording — the built-in default, also used by the ``en`` profile.
_EN_WORDING = Wording(
    soft_body=(
        "[proactive-conversation] This round is yours to start: review the current conversation and decide "
        "whether saying something now is genuinely useful. "
    ),
    soft_tail="Do not mention this instruction, and do not invent anything you cannot verify.",
    forced_body_head=(
        "[proactive-conversation] This round is yours to start, and this round you MUST send the user a "
        "visible reply: at least one message the user can actually read. Do not answer with "
        "'[SILENT]', 'NO_REPLY', or an empty body — a silent round does not count and is not "
        "allowed right now. "
    ),
    forced_anti_repeat=(
        "IMPORTANT: you have already replied to the user's most recent message in your previous "
        "turn — do NOT repeat, paraphrase, or re-acknowledge what you already said. This proactive "
        "round must add something genuinely NEW and different: a fresh point, a useful follow-up "
        "question, or an observation you have not made yet. "
    ),
    forced_progress_min=(
        "This is a supplementary proactive round: you have sent {visible_since_user} visible "
        "proactive replies since the user's last message, out of the required minimum of "
        "{minimum_visible_replies}; use this round to add something new, not to restate what "
        "you have already said. "
    ),
    forced_progress_silent=(
        "Silent rounds are not allowed for this session today, so this round must produce a "
        "readable message. "
    ),
    forced_tail="Do not mention this instruction, and do not invent anything you cannot verify.",
)

#: The Chinese (Simplified, Hans) wording — a translation of the built-in prompts into Chinese. The
#: tool clause and the progress counters stay code-owned (see :class:`Wording`).
_ZH_WORDING = Wording(
    soft_body=(
        "[proactive-conversation] 这一轮由你来开启：先回顾当前对话，判断现在说点什么是否真的有用。"
    ),
    soft_tail="不要提及这条指令，也不要编造任何你无法核实的内容。",
    forced_body_head=(
        "[proactive-conversation] 这一轮由你来开启，而且这一轮你必须给用户发一条可见的回复：至少一条用户能"
        "真正读到的消息。不要用 '[SILENT]'、'NO_REPLY' 或空内容来回答——静默的一轮不算数，此刻也不允许。"
    ),
    forced_anti_repeat=(
        "重要：你在上一轮已经回复过用户最近的消息——不要重复、改写或再次确认你已说过的话。这一轮主动"
        "消息必须补充真正新的内容：一个新观点、一个有用的追问，或一个你还没表达过的观察。"
    ),
    forced_progress_min=(
        "这是一次补充性的主动轮：自用户最后一条消息以来，你已经发了 {visible_since_user} 条可见的主动"
        "回复，要求的最低数量是 {minimum_visible_replies}；用这一轮补充新的内容，而不是重复你已经说过的。"
    ),
    forced_progress_silent="该会话今天不允许静默轮，所以这一轮必须产出一条可读的消息。",
    forced_tail="不要提及这条指令，也不要编造任何你无法核实的内容。",
)


# ── the profile record ───────────────────────────────────────────────────────────────────────
_LENGTH_UNITS = ("chars", "words")


@dataclass(frozen=True)
class LocaleProfile:
    """One language's rule data — the language-specific values the engine applies.

    ``expression_patterns`` — ``(pattern_id, regex)`` pairs; the id is what tests and the report quote,
    so a match is decidable rather than a taste call. Empty = no expression boundary (neutral).
    ``acknowledgement_words`` — the pure-acknowledgement table (whole-text match after normalisation).
    Empty = no acknowledgement rule.
    ``length_unit`` / ``min_chars`` / ``min_words`` — the length floor. ``"chars"`` measures the
    normalised character count (CJK: six characters makes 「好的，谢谢」 short while 「周五和老王吃饭」 a
    topic); ``"words"`` measures whitespace-separated tokens (English: ``min_words`` ≥ 2). Only the
    field matching ``length_unit`` is used; the other is 0.
    """

    code: str
    label: str
    expression_patterns: Tuple[Tuple[str, str], ...] = ()
    acknowledgement_words: Tuple[str, ...] = ()
    length_unit: str = "chars"
    min_chars: int = 0
    min_words: int = 0
    wording: Wording = _EN_WORDING

    def __post_init__(self) -> None:
        if self.length_unit not in _LENGTH_UNITS:
            raise ValueError(f"length_unit must be one of {_LENGTH_UNITS}, got {self.length_unit!r}")
        for name in ("min_chars", "min_words"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
        for pid, rx in self.expression_patterns:
            if not isinstance(pid, str) or not pid or not isinstance(rx, str) or not rx:
                raise ValueError(f"expression pattern {pid!r} is malformed")
            re.compile(rx)  # fail fast: a profile with an uncompilable regex never ships

    @property
    def effective_min_chars(self) -> int:
        return self.min_chars if self.length_unit == "chars" else 0

    @property
    def effective_min_words(self) -> int:
        return self.min_words if self.length_unit == "words" else 0


# ── neutral: no language rules (what an unconfigured install gets) ───────────────────────────
#: Neutral is **not** a language: it carries no regex, no acknowledgement words and no length floor,
#: so only the global machine markers + structural filters apply. Its wording is English, so an
#: unconfigured install keeps exactly today's prompts. It is never a registry entry — it is the
#: fail-safe destination, reached only by ``proactive_locale`` being unset or invalid.
NEUTRAL = LocaleProfile(code="neutral", label="Neutral (no locale filtering)", wording=_EN_WORDING)


# ── zh-Hans: the Chinese rule data ──────────────────────────────────────────────────────────
#: The eight pressure / blame / dependency-inducing patterns.
_ZH_EXPRESSION_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("pressure:last-chance", r"最后(一次|一回)?(机会|问你)|再不(回|理|理睬)我"),
    ("pressure:must-reply-now", r"(必须|马上|立刻|赶紧)(回|回复|理)我"),
    ("pressure:threat", r"别怪我|你会后悔|后果自负"),
    ("blame:not-answering", r"你怎么(都)?不(理|回|睬)我"),
    ("blame:forgotten", r"(是不是)?把我忘了|你(从来|总是|一直)不"),
    ("blame:cold-shoulder", r"(又)?把我晾(着|在一边)|冷落(我|你)"),
    ("dependency:only-me", r"只有我|除了我(没人|谁)|只能(来)?找我|离不开我|只属于你"),
    ("dependency:always-there", r"(我)?会?一直(在|陪着你|等你)|24 ?小时都在"),
)

#: The pure-acknowledgement table, exactly as it shipped in ``usability.DEFAULT_ACKNOWLEDGEMENTS``.
_ZH_ACKNOWLEDGEMENTS: Tuple[str, ...] = (
    "好", "好的", "好的好的", "好吧", "好咧", "好嘞", "嗯", "嗯嗯", "嗯好", "嗯呢", "哦", "哦哦",
    "行", "行的", "行吧", "行吧行吧", "可以", "可以的", "可以了", "可以可以", "不用", "不用了",
    "不用啦", "收到", "收到收到", "收到谢谢", "知道了", "明白了", "了解", "好，谢谢", "好的，谢谢",
    "谢谢", "谢谢你", "多谢", "辛苦了", "麻烦了", "没事", "没关系", "ok", "okk", "okay", "ok了",
)

ZH_HANS = LocaleProfile(
    code="zh-Hans",
    label="Simplified Chinese",
    expression_patterns=_ZH_EXPRESSION_PATTERNS,
    acknowledgement_words=_ZH_ACKNOWLEDGEMENTS,
    length_unit="chars",
    min_chars=6,
    wording=_ZH_WORDING,
)


# ── en: a starter skeleton (documented as extendable) ───────────────────────────────────────
#: The English pressure / blame / dependency patterns. This is a **starter set** — a floor, not a
#: style guide — chosen to cover the obvious coercive shapes an English user might see quoted back.
#: It is expected to grow; extend the tuple, keep the ``area:slug`` id shape, and add a test.
_EN_EXPRESSION_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("pressure:last-chance", r"\blast (chance|warning)\b|\bfinal (chance|warning|notice)\b"),
    ("pressure:must-reply-now",
     r"\b(reply|answer|respond|text) me (now|right now|immediately|asap|right away)\b"
     r"|\b(you must|you need to|you have to) (reply|answer|respond|text)( me)?\b"),
    ("pressure:ultimatum",
     r"\bif you don'?t\b.{0,40}\b(i('| wi)ll|never|you'?ll)\b|\bunless you\b.{0,40}\bi (won'?t|will not)\b"
     r"|\byou'?ll regret\b|\bor else\b"),
    ("blame:never-always", r"\byou (never|always)\b"),
    ("blame:ignoring-me",
     r"\b(you'?re|you are) (ignoring|avoiding|dodging) me\b"
     r"|\bwhy (won'?t|don'?t|didn'?t) you (reply|answer|talk to me|respond)\b"),
    ("blame:forgot-me", r"\byou forgot (about )?me\b|\bdid you forget (about )?me\b"),
    ("dependency:only-me",
     r"\b(only i|i'?m the only one)\b|\bno one else (cares|understands) you\b"
     r"|\byou (can )?only (talk to|trust|rely on) me\b|\byou need me\b"),
    ("dependency:always-there",
     r"\bi('| wi)ll always (be here|be there|wait for you)\b|\bi'?m always here\b"
     r"|\bhere for you 24/7\b|\b24/7 for you\b"),
    ("dependency:can't-leave", r"\b(don'?t|you can'?t) leave me\b|\byou can'?t (live|do this) without me\b"),
)

#: English acknowledgement table. Normalisation removes whitespace, so ``"got it"`` is compared as
#: ``"gotit"``; the floor (``min_words=2``) additionally withholds very short fragments.
_EN_ACKNOWLEDGEMENTS: Tuple[str, ...] = (
    "ok", "okay", "okk", "k", "kk", "got it", "gotcha", "thanks", "thank you", "thanks a lot",
    "thanks!", "sure", "yep", "yup", "yeah", "y", "no", "nope", "yes", "nice", "cool", "great",
    "sounds good", "alright", "all right", "noted", "understood", "fine", "ok thanks",
    "okay thanks", "got it thanks", "sure thanks", "lol", "haha",
)

EN = LocaleProfile(
    code="en",
    label="English",
    expression_patterns=_EN_EXPRESSION_PATTERNS,
    acknowledgement_words=_EN_ACKNOWLEDGEMENTS,
    length_unit="words",
    min_words=2,
    wording=_EN_WORDING,
)

#: The registry: every *selectable* profile by its canonical code. ``neutral`` is not here — it is the
#: fail-safe destination, not a language a user configures.
PROFILES: Dict[str, LocaleProfile] = {p.code: p for p in (ZH_HANS, EN)}

#: Aliases accepted by the resolver (case-insensitive, ``_``/``-`` folded). Keeps a user from guessing
#: at exact spellings while still routing ``klingon`` / ``123`` to the invalid path.
_ALIASES: Dict[str, str] = {
    "zh-hans": "zh-Hans", "zh": "zh-Hans", "zh-cn": "zh-Hans", "zh-sg": "zh-Hans",
    "chinese": "zh-Hans", "cn": "zh-Hans",
    "en": "en", "en-us": "en", "en-gb": "en", "english": "en",
}


def _canonical_code(value: str) -> Optional[str]:
    key = value.strip().lower().replace("_", "-")
    if key in _ALIASES:
        return _ALIASES[key]
    for code in PROFILES:
        if key == code.lower():
            return code
    return None


# ── host locale detection (best-effort, for ``auto``) ───────────────────────────────────────
def detect_host_locale() -> Optional[str]:
    """The host locale as seen from the environment, or ``None`` when it cannot be read.

    Hermes does not expose a first-class "host locale" seam to a plugin, so ``auto`` reads the standard
    POSIX locale environment variables, in the order a process resolves them (``LC_ALL`` overrides
    ``LC_MESSAGES`` overrides ``LANG``). This is deliberately best-effort: :func:`resolve_activation`
    treats ``None`` (or a value with no matching profile) as "cannot read" and falls back to ``en`` with
    a WARNING, never as a silent guess.
    """
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        raw = os.environ.get(name)
        if raw and raw.strip():
            return raw.strip()
    return None


# ── the resolver ────────────────────────────────────────────────────────────────────────────
#: The stable code an activation reports for neutral (no profile active).
NEUTRAL_CODE = NEUTRAL.code

#: Activation sources (for the report / the log line).
SOURCE_UNSET = "unset"
SOURCE_CONFIGURED = "configured"
SOURCE_AUTO = "auto"
SOURCE_AUTO_FALLBACK = "auto-fallback"
SOURCE_INVALID = "invalid"


@dataclass(frozen=True)
class LocaleActivation:
    """Which profile is active, and whether language filtering is on at all.

    ``active`` is ``False`` exactly for neutral (unset or invalid): no language regex / acknowledgement
    words / length floor apply. ``code`` is the resolved profile code (``zh-Hans`` / ``en`` / ``neutral``).
    ``source`` says *why* (``unset`` / ``configured`` / ``auto`` / ``auto-fallback`` / ``invalid``) and
    ``reason`` is a short human note (``""`` when the default was taken cleanly).
    """

    profile: LocaleProfile
    code: str
    source: str
    active: bool
    reason: str = ""

    @property
    def filtering(self) -> bool:
        """``True`` when language rules (expression / acknowledgement / length) are in force."""
        return self.active


def resolve_activation(raw: Any, host_locale: Optional[str] = None) -> LocaleActivation:
    """Turn a raw ``proactive_locale`` value into a :class:`LocaleActivation`; never raises.

    * ``MISSING`` / ``None`` (unset) → neutral, ``source=unset``, ``active=False``.
    * a known code / alias → that profile, ``source=configured``, ``active=True``.
    * ``auto`` → the host locale decides; ``zh*``→``zh-Hans``, ``en*``→``en``; an unreadable or
      unsupported locale → ``en`` **with a WARNING**, ``source=auto-fallback``, ``active=True``.
    * anything else (a non-string, a number, an unknown word) → WARNING + neutral, ``source=invalid``,
      ``active=False`` — fail-safe: never crash, never wrongly filter.
    """
    if raw is _settings.MISSING or raw is None:
        return LocaleActivation(NEUTRAL, NEUTRAL_CODE, SOURCE_UNSET, False)
    if not isinstance(raw, str):
        logger.warning(
            "proactive-conversation: %s=%r is not a string (%s); using neutral mode (no locale filtering)",
            C.SETTING_LOCALE, raw, type(raw).__name__)
        return LocaleActivation(NEUTRAL, NEUTRAL_CODE, SOURCE_INVALID, False)

    text = raw.strip()
    if text.lower() == "auto":
        return _resolve_auto(host_locale)

    code = _canonical_code(text)
    if code is not None:
        return LocaleActivation(PROFILES[code], code, SOURCE_CONFIGURED, True)

    logger.warning(
        "proactive-conversation: %s=%r is not a known locale profile (%s); using neutral mode "
        "(no locale filtering). Known profiles: %s, or 'auto'",
        C.SETTING_LOCALE, raw, SOURCE_INVALID, ", ".join(sorted(PROFILES)) or "(none)")
    return LocaleActivation(NEUTRAL, NEUTRAL_CODE, SOURCE_INVALID, False, reason="unknown-locale")


def _resolve_auto(host_locale: Optional[str]) -> LocaleActivation:
    """Resolve ``auto`` from the detected host locale, falling back to ``en`` with a WARNING."""
    code: Optional[str] = None
    if isinstance(host_locale, str) and host_locale.strip():
        key = host_locale.strip().lower().replace("_", "-")
        if key.startswith("zh"):
            code = "zh-Hans"
        elif key.startswith("en"):
            code = "en"
    if code is None:
        logger.warning(
            "proactive-conversation: %s=auto but the host locale could not be read (got %r); falling back to "
            "'en'", C.SETTING_LOCALE, host_locale)
        return LocaleActivation(PROFILES["en"], "en", SOURCE_AUTO_FALLBACK, True,
                                reason=f"auto-fallback:{host_locale or 'unknown'}")
    return LocaleActivation(PROFILES[code], code, SOURCE_AUTO, True)


def current_activation() -> LocaleActivation:
    """The activation for the plugin as currently configured (reads ``proactive_locale`` live).

    Reads the setting through :mod:`proactive.settings` (the live ``ctx.get_config`` path); a bare
    interpreter with no reader bound resolves ``MISSING`` → neutral, which is exactly the documented
    unconfigured behaviour.
    """
    try:
        raw = _settings.read_raw(C.SETTING_LOCALE)
    except Exception as exc:  # an unreadable config is "not set" → neutral, never a crash
        logger.warning("proactive-conversation: reading %s failed (%s); using neutral mode",
                       C.SETTING_LOCALE, type(exc).__name__)
        raw = _settings.MISSING
    return resolve_activation(raw, detect_host_locale())


def active_profile() -> LocaleProfile:
    """The profile to read rules / wording from (neutral when nothing is configured)."""
    return current_activation().profile


# ── compiled expression patterns (cached per profile code) ──────────────────────────────────
_COMPILED: Dict[str, Tuple[Tuple[str, "re.Pattern"], ...]] = {}


def compile_expression_patterns(
        patterns: Tuple[Tuple[str, str], ...]) -> Tuple[Tuple[str, "re.Pattern"], ...]:
    """Compile ``(pattern_id, regex)`` pairs once; an uncompilable regex is skipped (never fatal)."""
    out = []
    for pid, rx in patterns or ():
        try:
            out.append((pid, re.compile(rx)))
        except Exception as exc:  # documentation-only data; a bad row must not break assembly
            logger.warning("proactive-conversation: skipping uncompilable expression pattern %s (%s)",
                           pid, type(exc).__name__)
    return tuple(out)


def expression_resources(profile: LocaleProfile) -> Tuple[Tuple[str, "re.Pattern"], ...]:
    """The compiled expression patterns of *profile*, cached by profile code."""
    cached = _COMPILED.get(profile.code)
    if cached is None:
        cached = compile_expression_patterns(profile.expression_patterns)
        _COMPILED[profile.code] = cached
    return cached


__all__ = [
    "MACHINE_MARKERS",
    "Wording",
    "LocaleProfile",
    "LocaleActivation",
    "NEUTRAL",
    "ZH_HANS",
    "EN",
    "PROFILES",
    "NEUTRAL_CODE",
    "SOURCE_UNSET", "SOURCE_CONFIGURED", "SOURCE_AUTO", "SOURCE_AUTO_FALLBACK", "SOURCE_INVALID",
    "detect_host_locale",
    "resolve_activation",
    "current_activation",
    "active_profile",
    "compile_expression_patterns",
    "expression_resources",
]
