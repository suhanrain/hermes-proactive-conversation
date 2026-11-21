"""``session-transcript`` — this session's own recent messages, read from the real session store.

What is real here
-----------------
The records come from Hermes' own session database (``<HERMES_HOME>/state.db``): the ``messages``
table the agent persists every turn into, joined to the ``sessions`` row that owns it. Nothing is
cached, nothing is synthesized: a record's ``text`` is the exact ``messages.content`` string of one
row, and ``Record.fingerprint()`` therefore resolves back to that row
(``sha256(content)[:8]``, recomputable by anyone holding the database).

The three boundaries this adapter enforces — all of them *before* a row is read
---------------------------------------------------------------------------------
1. **Profile.** The adapter is built for exactly one profile (the profile whose ``HERMES_HOME`` it
   was given). A scope naming another profile is refused
   (``unavailable:session-scope-mismatch``): reading it would mean reading another profile's chat.
2. **This session only.** The scope's ``session_id`` must exist in the ``sessions`` table *and* the
   row's ``session_key`` must equal the scope's — otherwise the scope does not describe this
   session's transcript and the adapter answers ``unavailable:session-unknown`` (no row) or
   ``unavailable:session-scope-mismatch`` (row belongs to another key). A second session's text is
   unreachable, not merely unwanted.
3. **The user's own words.** Only ``role='user'`` rows are offered, and machinery rows are dropped:
   the single transcript template (``content._TEMPLATES``) says 上次你提到「…」, so quoting an
   assistant turn, a tool row, a hidden/failed row or the plugin's own previous proactive round
   (persisted as ``display_kind='internal_notification'``, the gateway's ``MACHINERY_DISPLAY_KINDS``
   member) would attribute to the user words the user never wrote. This is a correctness rule of the
   one template, not a taste filter — and it is *not* a claim that assistant turns are unsuitable
   material in general (citing them would simply be a different template's choice).
   ``display_kind='steer'`` is deliberately **kept**: a steering turn is the user's own words, merely
   delivered mid-turn.
4. **The rule table.** ``display_kind`` is not enough: the platform also files *injected* turns as
   ``role='user'`` with ``display_kind IS NULL`` — a compaction handoff (``_compressed_summary = 1``),
   the gateway's origin preamble, a cron delivery envelope, a reply pointer. Those are withheld as
   machine text (``unusable:machine-text`` / ``unusable:compaction-summary``), and so is a row that
   cannot carry a topic (``unusable:too-short`` / ``unusable:acknowledgement``). The table is
   :class:`proactive.sources.usability.TopicRules` — configured here as ``rules``, never inlined —
   and this adapter is built with all of its rules by default (``TRANSCRIPT_TOPIC_RULES``).

What is withheld is *counted*, never rewritten
----------------------------------------------
A row the rules refuse is dropped whole: ``Record.text`` stays the exact stored string, so a
fingerprint still resolves to the row (a trimmed quote would break that).
The adapter keeps ``window_rows`` (how many rows the window returned) and ``withheld_counts()``
(``{unusable:*: n}``) so a report can say *why* a window produced fewer topics than rows — with
counts only, never row text.

One schema assumption, checked: the query reads ``messages._compressed_summary`` (Hermes' own
compaction flag, the one signal that cannot be recovered from the text alone). If a store lacks the
column the read raises ``unavailable:read-failed`` rather than
guessing: a source that cannot be read is reported unreadable, never silently treated as topic-less.

Windows are configuration, never constants
------------------------------------------
``max_messages`` (how many rows) and ``window_seconds`` (how far back) have **no defaults** in this
class: a caller must supply both, because the user's rule is that every time and every count is
configurable (the same rule ``contract`` puts on ``ContentPolicy.recent_record_seconds``).
The effective window is the **smaller** of the two: rows older than ``window_seconds`` and rows
beyond the newest ``max_messages`` are both excluded. The clock is injectable for the same reason
the plugin's own clock is (tests replay a moment; production passes the real clock).

What this adapter never does: open the database writable, write anywhere, touch the network, or
raise anything except :class:`proactive.content.SourceUnavailable` (the registry converts an
unexpected exception into the same "unavailable" outcome, but a coded refusal is the evidence a
test can quote).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple

from .. import contract as C
from .. import host_adapter as _adapter
from ..content import Record, SourceAdapter, SourceUnavailable
from .codes import (UNAVAILABLE_SCOPE_MISMATCH, UNAVAILABLE_SESSION_UNKNOWN,
                    UNUSABLE_COMPACTION_SUMMARY)
from .sqlite_readonly import open_read_only, read_only_uri
from .usability import TRANSCRIPT_TOPIC_RULES, TopicRules

logger = logging.getLogger(__name__)

#: The host's schema, in the one module allowed to know it: the two
#: statements and the ``messages.display_kind`` values that are not the user's own words. They are
#: imported rather than copied so a schema change is a one-file edit, and they are used verbatim in
#: :meth:`SessionTranscriptSource.read`.
EXCLUDED_DISPLAY_KINDS: Tuple[str, ...] = _adapter.EXCLUDED_DISPLAY_KINDS

#: Tolerance for a message whose stored timestamp is marginally ahead of the reader's clock
#: (sub-second stamping skew). It is the same 1-second slack ``content._stale`` applies, kept in
#: step deliberately: the adapter must not offer a row the assembly would then throw away.
FUTURE_TOLERANCE_SECONDS = 1.0

_SESSION_SQL = _adapter.TRANSCRIPT_SESSION_SQL

_MESSAGES_SQL = _adapter.TRANSCRIPT_MESSAGES_SQL


class SessionTranscriptSource(SourceAdapter):
    """Read-only adapter over one profile's ``state.db``, scoped to one session.

    ``db_path`` — the profile's session database (``<HERMES_HOME>/state.db``).
    ``max_messages`` / ``window_seconds`` — the two configured window values (no defaults).
    ``profile`` — the profile this database belongs to; a scope naming another one is refused.
    ``clock`` — injectable "now" (the window's upper end and the future guard).
    ``rules`` — the :class:`TopicRules` table (machine text, the length floor, the acknowledgement
    table); defaults to all of them, and every field is a value a caller may replace.
    """

    name: str = C.SOURCE_SESSION
    read_only: bool = True
    network: bool = False

    def __init__(self, db_path: Any, *, max_messages: Any, window_seconds: Any,
                 profile: Optional[str] = None, clock: Callable[[], float] = time.time,
                 rules: TopicRules = TRANSCRIPT_TOPIC_RULES) -> None:
        self.db_path = Path(db_path)
        self.max_messages = _positive_int(max_messages, "max_messages")
        self.window_seconds = _positive_number(window_seconds, "window_seconds")
        self.profile = profile or None
        self._clock = clock
        self.rules = rules if isinstance(rules, TopicRules) else TopicRules(**dict(rules))
        self.calls = 0
        #: Rows the last read's window returned, and how many of those the rules withheld, by code.
        self.window_rows = 0
        self.withheld: dict = {}

    # ── diagnostics ─────────────────────────────────────────────────────────────────────────

    @property
    def uri(self) -> str:
        """The read-only URI this adapter opens (quoted by the tests and the report)."""
        return read_only_uri(self.db_path)

    def windows(self) -> dict:
        """The configured windows, as the report prints them (no values are invented here)."""
        return {"max_messages": self.max_messages, "window_seconds": self.window_seconds,
                "profile": self.profile}

    def withheld_counts(self) -> dict:
        """``{unusable:...: n}`` over every read so far (counts only, never row text)."""
        return dict(self.withheld)

    def rules_dict(self) -> dict:
        """The rule table this adapter applies (report data: markers, floor, table size)."""
        return self.rules.as_dict()

    # ── the read ────────────────────────────────────────────────────────────────────────────

    def read(self, scope: Any) -> Sequence[Record]:
        """This session's newest usable user messages, newest first. Never writes, never guesses."""
        self.calls += 1
        scope_profile = _text(getattr(scope, "profile", None))
        if self.profile and scope_profile and scope_profile != self.profile:
            raise SourceUnavailable(UNAVAILABLE_SCOPE_MISMATCH)
        session_id = _text(getattr(scope, "session_id", None))
        session_key = _text(getattr(scope, "session_key", None))
        if not session_id:
            # Without a session id there is no transcript to read — and picking "the newest
            # session" instead would read a chat this scope does not name.
            raise SourceUnavailable(UNAVAILABLE_SESSION_UNKNOWN)

        now = float(self._clock())
        try:
            conn = open_read_only(self.db_path)
        except sqlite3.Error as exc:
            logger.warning("proactive-conversation: session store %s could not be opened read-only (%s)",
                           self.db_path.name, type(exc).__name__)
            raise SourceUnavailable(C.UNAVAILABLE_READ_FAILED, detail=type(exc).__name__)

        try:
            row = conn.execute(_SESSION_SQL, (session_id,)).fetchone()
            if row is None:
                raise SourceUnavailable(UNAVAILABLE_SESSION_UNKNOWN)
            stored_key, stored_profile = _text(row[0]), _text(row[1])
            if session_key and stored_key and stored_key != session_key:
                raise SourceUnavailable(UNAVAILABLE_SCOPE_MISMATCH)
            if self.profile and stored_profile and stored_profile != self.profile:
                raise SourceUnavailable(UNAVAILABLE_SCOPE_MISMATCH)
            params = (session_id, *EXCLUDED_DISPLAY_KINDS, now - self.window_seconds,
                      now + FUTURE_TOLERANCE_SECONDS, self.max_messages)
            rows = conn.execute(_MESSAGES_SQL, params).fetchall()
        except sqlite3.Error as exc:
            logger.warning("proactive-conversation: session transcript read failed (%s)", type(exc).__name__)
            raise SourceUnavailable(C.UNAVAILABLE_READ_FAILED, detail=type(exc).__name__)
        finally:
            conn.close()

        self.window_rows = len(rows)
        records = []
        for row_id, content, timestamp, compressed_summary in rows:
            reason = self._unusable(content, compressed_summary)
            if reason:
                self.withheld[reason] = self.withheld.get(reason, 0) + 1
                continue
            at = _number(timestamp)
            if at is None:
                continue  # an undated row cannot be shown to be recent; content keeps it out
            records.append(Record(source=self.name, record_id=f"messages#{int(row_id)}", at=at,
                                  text=str(content), subject="user"))
        if self.withheld:
            logger.info("proactive-conversation: session transcript offered %d of %d window rows (%s)",
                        len(records), self.window_rows, sorted(self.withheld.items()))
        return tuple(records)

    def _unusable(self, content: Any, compressed_summary: Any) -> str:
        """The code that keeps this row out of the candidate set, or ``""`` when it may be offered.

        Two independent checks, both deterministic: the structural flag Hermes sets on a compaction
        handoff (only this adapter can apply it — the column lives on ``messages``), then the
        configured :class:`TopicRules` table over the text.
        """
        if self.rules.exclude_compaction_summaries and _truthy(compressed_summary):
            return UNUSABLE_COMPACTION_SUMMARY
        return self.rules.why_unusable(content)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer, got {value!r}")
    return int(value)


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0.0:
        raise ValueError(f"{field} must be a positive number, got {value!r}")
    return float(value)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _truthy(value: Any) -> bool:
    """SQLite hands back ``0``/``1`` for a flag column; anything non-zero means "set"."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) != 0.0
    return str(value).strip() not in ("", "0")
