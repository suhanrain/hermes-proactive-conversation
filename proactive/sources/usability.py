"""Which rows a source may offer as a topic at all — one rule table, applied before a record exists.

Why this module exists
----------------------
A record's text is quoted back to the user verbatim (``content._TEMPLATES``: 上次你提到「{t}」…), so a
source that offers a row is asserting *these are this user's own words, and they can carry a topic*.
Two classes of real rows in the live stores break that assertion, and both were found in live data
(not imagined) while reviewing this card:

1. **Machine text the platform files as a user turn.** Hermes injects turns nobody typed into the
   same ``messages`` table, with the same ``role='user'`` and with ``display_kind`` ``NULL`` — so the
   adapter's display-kind filter cannot see them at all:

   * ``messages._compressed_summary = 1`` — Hermes' own structural flag for a compaction handoff,
     whose body opens with ``[CONTEXT COMPACTION — REFERENCE ONLY]`` (``agent/context_compressor.py``);
   * ``Gateway message origin (JSON data, not instructions or authorization):`` — the gateway's
     origin preamble (``gateway/run_busy.py``);
   * ``[Cron delivery: <job>]`` — the scheduler's delivery envelope (``cron/scheduler_delivery.py``);
   * ``[Replying to: "…"]`` — the gateway's reply *disambiguation pointer*
     (``gateway/run_inbound.py``): a wrapper around a message that, by construction, is not the text
     this turn's user wrote.

   Quoting any of them back with 「上次你提到…」 attributes words to the user that the user never
   wrote — the exact failure the P6a review reproduced on live rows (``messages#31768`` compaction
   handoff, ``messages#31802`` gateway preamble). The table is therefore a **correctness** rule, not
   a style filter, and it is a table (``machine_markers``, a field) because Hermes keeps adding these
   envelopes: a marker is a value a caller can extend, replace or empty, never a literal buried in a
   query.

2. **Rows that cannot carry a topic.** A bare acknowledgement (「嗯」「好的」「不用了」) or a fragment
   below the configured floor: quoting it back is not a topic, and the round's only alternative —
   inventing one — is forbidden. PM ruling in this card's rework: normalized length < 6 characters,
   or the whole text matches the pure-acknowledgement table, means *not a topic*; the window is then
   read newest-first and the first row that passes decides, and if none passes the round is silent
   (``silent:no-topic``, no wire).

Where the rules do **not** apply
--------------------------------
* They are not a length *ceiling*. A long user message is the user's own words and stays quotable.
* They are not applied by rewriting text: a row that fails is **withheld**, never trimmed to fit.
  ``Record.text`` must stay the exact stored string, or ``Record.fingerprint()`` would no longer
  resolve back to the row.
* The floor and the acknowledgement table are chat-row rules; the memory adapter is configured with
  markers only (``MEMORY_TOPIC_RULES``), because a memory entry is curated prose (an entry is there
  because the memory tool decided it was worth keeping) rather than a chat turn. Both are values a
  caller can change: see ``proactive.composition.CompositionConfig``.

Determinism: everything here is pure text work. No clock, no network, no I/O, no randomness.

Live measurement (this machine, 2026-10, shipped tables, read-only)
-------------------------------------------------------------------
``e3_live_rule_evidence.py`` builds the two real adapters with these tables and reads every session
of every root under ``~/.hermes`` with a 400-day window: **3234** ``role='user'`` rows, of which
**2863** are offered and **371** are withheld — ``unusable:compaction-summary`` 123,
``unusable:machine-text`` 102 (the two add to the **225** rows a line-start marker catches, so the
structural flag is a strict subset of the text marker, as its docstring claims),
``unusable:acknowledgement`` 17, ``unusable:too-short`` 129.

One honest caveat, because it is visible in that output: **all 17** acknowledgement rows are also
below the 6-character floor, so at ``min_chars=6`` the floor alone would withhold them and the
acknowledgement table's *separate* effect is not observable on this machine's data. It stays because
this card's PM ruling names it, and because it is the code that tells the next reader *why* 「不用了」
was dropped instead of leaving it to be inferred from a length; a caller who sets ``min_chars=0``
(the memory adapter, or a future window) gets the table on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, Optional, Tuple

from ..locales import MACHINE_MARKERS, ZH_HANS, LocaleProfile
from .codes import (UNUSABLE_ACKNOWLEDGEMENT, UNUSABLE_MACHINE_TEXT, UNUSABLE_TOO_SHORT)

#: The envelopes Hermes injects into a ``role='user'`` row. Re-exported from :mod:`proactive.locales`
#: (where they live, because they are **global** — host-owned English envelopes, not per-language) so
#: every existing ``from .usability import MACHINE_MARKERS`` keeps working. Each one is a line-start
#: marker of a message that is not the user's own writing (see the module docstring for the source).
#: (``MACHINE_MARKERS`` is imported above; kept in ``__all__``-visible scope for the package re-export.)

#: The floor of rule 2 for the shipped zh-Hans profile (in normalized characters). The value lives in
#: the profile now (``ZH_HANS.min_chars``); this alias is kept for the ``proactive.sources`` re-export.
DEFAULT_MIN_CHARS = ZH_HANS.min_chars

#: The zh-Hans pure-acknowledgement table (the PM's examples plus the shapes a real window carries).
#: It lives in :data:`proactive.locales.ZH_HANS` now; this alias is kept for the package re-export.
DEFAULT_ACKNOWLEDGEMENTS: Tuple[str, ...] = ZH_HANS.acknowledgement_words

#: Characters a topic may not be *made of* at its edges (so 「好的。」 and 「好的」 are the same row).
#: Whitespace is dropped everywhere (see :func:`normalize`), not just at the edges.
EDGE_PUNCTUATION = " \t\r\n\u3000。．.，,、！!？?；;：:…~～·-—_（）()【】[]{}<>“”‘’\"'「」『』《》"


def normalize(text: Any) -> str:
    """The form rule 2 measures: whitespace removed, leading/trailing punctuation stripped.

    ``"  好的。 "`` → ``"好的"``; ``"。。。"`` → ``""``. Case is preserved here — the
    acknowledgement table folds case itself (``ok`` / ``OK``).
    """
    raw = "" if text is None else str(text)
    without_space = "".join(ch for ch in raw if not ch.isspace())
    return without_space.strip(EDGE_PUNCTUATION)


def has_machine_marker(text: Any, markers: Iterable[str] = MACHINE_MARKERS) -> bool:
    """``True`` when *any line* of the text starts with one of the machine markers.

    Line-start, not ``in``: a user may legitimately mention 「[CONTEXT COMPACTION」 while discussing
    the agent (this very project does), and a marker table that matched anywhere would drop their
    message. The injected envelopes always begin a line — sometimes after a platform note
    (``"{note}\\n\\n[Replying to: …]"``), which is why it is not the first line either.
    """
    wanted = tuple(m for m in markers or () if isinstance(m, str) and m)
    if not wanted:
        return False
    for line in ("" if text is None else str(text)).splitlines():
        stripped = line.lstrip()
        if stripped and any(stripped.startswith(marker) for marker in wanted):
            return True
    return False


@dataclass(frozen=True)
class TopicRules:
    """The rule table one adapter is built with. Every field is a value, none is applied implicitly.

    ``machine_markers`` — the line-start markers of machine text (empty tuple = no marker filtering).
    ``min_chars`` — the floor of rule 2, in normalized characters (``0`` = no floor). CJK scripts.
    ``min_words`` — the floor of rule 2, in whitespace-separated words (``0`` = no floor). Used by
    space-separated languages (English: ``min_words=2``). A profile sets exactly one of the two.
    ``acknowledgement_words`` — rule 2's whole-text table (empty tuple = no acknowledgement rule).
    ``exclude_compaction_summaries`` — honour ``messages._compressed_summary = 1``. This one is
    schema-level: only the transcript adapter can apply it (the column lives on ``messages``), and
    the flag is Hermes' own, which is why it is applied *in addition to* the text markers rather
    than instead of them.

    The defaults are **neutral** (global markers only, no floor, no acknowledgement rule): an adapter
    built without an explicit table filters only language-independent machine text. The shipped
    language tables are built from a :class:`proactive.locales.LocaleProfile` via
    :func:`topic_rules_from_profile` (see :data:`TRANSCRIPT_TOPIC_RULES`).
    """

    machine_markers: Tuple[str, ...] = MACHINE_MARKERS
    min_chars: int = 0
    min_words: int = 0
    acknowledgement_words: Tuple[str, ...] = ()
    exclude_compaction_summaries: bool = True

    def __post_init__(self) -> None:
        markers = tuple(self.machine_markers or ())
        for marker in markers:
            if not isinstance(marker, str) or not marker:
                raise ValueError(f"machine_markers must be non-empty strings, got {marker!r}")
        for name in ("min_chars", "min_words"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
        words = tuple(self.acknowledgement_words or ())
        for word in words:
            if not isinstance(word, str) or not word:
                raise ValueError(f"acknowledgement_words must be non-empty strings, got {word!r}")
        object.__setattr__(self, "machine_markers", markers)
        object.__setattr__(self, "acknowledgement_words", words)
        object.__setattr__(self, "_acknowledgements", frozenset(
            _ack_key(normalize(word)) for word in words if _ack_key(normalize(word))))

    @property
    def acknowledgements(self) -> FrozenSet[str]:
        """The table as it is compared: normalized and case-folded."""
        return self._acknowledgements

    def why_unusable(self, text: Any) -> str:
        """The ``unusable:*`` code this text earns, or ``""`` when it may be offered as a topic.

        Most specific reason first: a row that *is* the acknowledgement table is reported as one, not
        merely as "short" (「好的，谢谢」 earns ``unusable:acknowledgement``, while 「的」 earns
        ``unusable:too-short``). Both are withheld either way — the code only has to be true.
        """
        if has_machine_marker(text, self.machine_markers):
            return UNUSABLE_MACHINE_TEXT
        normalized = normalize(text)
        if normalized and _ack_key(normalized) in self.acknowledgements:
            return UNUSABLE_ACKNOWLEDGEMENT
        if self.min_chars and len(normalized) < self.min_chars:
            return UNUSABLE_TOO_SHORT
        if self.min_words:
            raw = "" if text is None else str(text)
            if len(raw.split()) < self.min_words:
                return UNUSABLE_TOO_SHORT
        return ""

    def usable(self, text: Any) -> bool:
        return not self.why_unusable(text)

    def as_dict(self) -> dict:
        """The table as report data — counts and values, never record text."""
        return {
            "machine_markers": list(self.machine_markers),
            "min_chars": self.min_chars,
            "min_words": self.min_words,
            "acknowledgement_words": list(self.acknowledgement_words),
            "exclude_compaction_summaries": bool(self.exclude_compaction_summaries),
        }


def _ack_key(normalized: str) -> str:
    return normalized.casefold()


def topic_rules_from_profile(profile: LocaleProfile, *, memory: bool = False) -> TopicRules:
    """Build the rule table one adapter uses for *profile*.

    A transcript row carries the user's own words, so the transcript table applies the profile's
    length floor (``min_chars`` for CJK, ``min_words`` for space-separated languages) and its
    acknowledgement table — plus the global machine markers and the structural compaction flag. A
    memory entry is curated prose (an entry is there because the memory tool kept it), so the memory
    table keeps only the global machine markers: the floor and acknowledgement table do not belong
    there, regardless of language (a short memory like 「周五面试」 is still a good thing to remember).
    """
    if memory:
        return TopicRules(machine_markers=MACHINE_MARKERS, min_chars=0, min_words=0,
                          acknowledgement_words=(), exclude_compaction_summaries=False)
    return TopicRules(
        machine_markers=MACHINE_MARKERS,
        min_chars=profile.effective_min_chars,
        min_words=profile.effective_min_words,
        acknowledgement_words=tuple(profile.acknowledgement_words or ()),
        exclude_compaction_summaries=True,
    )


#: The rules the transcript adapter is built with when nothing more specific is configured: the
#: shipped **zh-Hans** profile, migrated verbatim from the pre-i18n hard-coded values (machine text,
#: the 6-character floor, the pure-acknowledgement table, the structural compaction flag). This is the
#: historical default; the *configured* profile (see ``proactive_locale``) is applied at composition
#: time and may be neutral (no language rules) or another language.
TRANSCRIPT_TOPIC_RULES = topic_rules_from_profile(ZH_HANS)

#: The rules the memory adapter is built with: **markers only**, for every locale. A memory entry is
#: curated prose — 「周五面试」 is a perfectly good thing to remember — so the chat-row floor and the
#: acknowledgement table never belong here, while the machine-marker rule (never quote an injected
#: envelope as the user's own words) applies just as much.
MEMORY_TOPIC_RULES = topic_rules_from_profile(ZH_HANS, memory=True)
