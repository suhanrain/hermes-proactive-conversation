"""``cross-session-memory`` — this profile's own memory files, read as the curated text they are.

What is real here
-----------------
Hermes keeps a profile's curated memory in two files under ``<HERMES_HOME>/memories/``
(``MEMORY.md`` — the agent's notes — and ``USER.md`` — who the user is; both written by the memory
tool, entries joined by ``\\n§\\n``: see ``tools/memory_tool_store.ENTRY_DELIMITER``). This adapter
reads those files and nothing else: each entry becomes one :class:`Record` whose ``text`` is the
entry verbatim, so ``Record.fingerprint()`` (``sha256(text)[:8]``) resolves by recomputing the
digest over the entry a reader can see in the file.

Recency: the file's ``mtime``, and why that is the only honest anchor
--------------------------------------------------------------------
Memory entries carry **no timestamp of their own** — the file is a delimiter-joined list of prose
entries, and a rewrite (which is what every ``memory(add=...)`` does) replaces the whole file. So
"最近 30 天" can only be judged at file granularity: the adapter treats a file whose ``mtime`` is
inside the configured window as *this profile's recent memory* and stamps every entry it contains
with that ``mtime`` (the moment the file last changed, i.e. the moment the newest entry landed). A
file older than the window contributes nothing — its entries would otherwise claim a freshness
nobody can support. The delivery report prints path + mtime + char count for exactly this reason,
and the window itself is configuration (``window_seconds``, no default).

Refusals, and what stays silent
-------------------------------
* another profile's scope → ``unavailable:session-scope-mismatch`` (this adapter is bound to one
  profile; ``HERMES_HOME`` is not a hint, it is the binding);
* a file that exists but cannot be read (permissions, encoding, an unresolved symlink out of the
  memory directory) → ``unavailable:read-failed``: a real failure, never "no memory";
* a file that does not exist at all → contributes nothing, and the *source* is still readable: a
  profile with no memory yet is not a broken source (an empty source is
  ``ok`` with 0 records, and if nothing else has a topic the round is silent).

What is withheld, and why almost nothing is
-------------------------------------------
An entry that opens a line with an injected envelope (``Gateway message origin (…``, a compaction
handoff, a cron label) is withheld as ``unusable:machine-text``: quoting it with 「我记得你之前说过…」
would put machine text in the user's mouth. That is the **only** rule this adapter applies by
default (``MEMORY_TOPIC_RULES``): a memory entry is curated prose — an entry is in the file because
the memory tool decided it was worth keeping — so the chat-row floor and the acknowledgement table
do not belong here (「周五面试」 is a good thing to remember, not a "too short" row). Both are values
a caller can change; see :class:`proactive.sources.usability.TopicRules`. Measured on this machine's
live memory files — 112 entries across three roots (``MEMORY.md`` + ``USER.md``: 19 + 15 top-level,
25 + 16 in ``dev``, 22 + 15 in ``product-manager``) — **0 entries** are withheld by either rule set,
and the tests pin that normal entries survive.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

from .. import contract as C
from ..content import Record, SourceAdapter, SourceUnavailable
from .codes import UNAVAILABLE_SCOPE_MISMATCH
from .usability import MEMORY_TOPIC_RULES, TopicRules

logger = logging.getLogger(__name__)

#: The delimiter Hermes joins memory entries with (``tools.memory_tool_store.ENTRY_DELIMITER``).
#: Kept as a literal so this package imports no hermes module; ``tests/test_sources_real.py`` pins
#: the two together, so a change on either side fails a test instead of silently halving a file.
ENTRY_DELIMITER = "\n§\n"

#: The two memory files of a profile, in the order a reader should prefer them: what the agent
#: noted about the user's life first, then the profile of who the user is.
MEMORY_FILES: Tuple[str, ...] = ("MEMORY.md", "USER.md")


def parse_entries(text: Any) -> List[str]:
    """Split a memory file into its entries exactly as Hermes does (drop empties, strip edges)."""
    raw = "" if text is None else str(text)
    return [entry for entry in (part.strip() for part in raw.split(ENTRY_DELIMITER)) if entry]


class CrossSessionMemorySource(SourceAdapter):
    """Read-only adapter over one profile's ``memories/`` directory.

    ``memory_dir`` — the profile's memory directory (``<HERMES_HOME>/memories``).
    ``window_seconds`` — how old a file may be and still count as recent memory (no default).
    ``profile`` — the profile these files belong to; a scope naming another one is refused.
    ``files`` — which files to read (defaults to ``MEMORY.md`` + ``USER.md``).
    ``clock`` — injectable "now".
    ``rules`` — the rule table; defaults to ``MEMORY_TOPIC_RULES`` (machine markers only, see the
    module docstring), and every field is a value a caller may replace.
    """

    name: str = C.SOURCE_MEMORY
    read_only: bool = True
    network: bool = False

    def __init__(self, memory_dir: Any, *, window_seconds: Any, profile: Optional[str] = None,
                 files: Sequence[str] = MEMORY_FILES,
                 clock: Callable[[], float] = time.time,
                 rules: TopicRules = MEMORY_TOPIC_RULES) -> None:
        self.memory_dir = Path(memory_dir)
        self.window_seconds = _positive_number(window_seconds, "window_seconds")
        self.profile = profile or None
        self.files = tuple(str(name) for name in files) or MEMORY_FILES
        self._clock = clock
        self.rules = rules if isinstance(rules, TopicRules) else TopicRules(**dict(rules))
        self.calls = 0
        #: Entries this adapter withheld across all reads, by code (counts only, never text).
        self.withheld: dict = {}

    # ── diagnostics ─────────────────────────────────────────────────────────────────────────

    def windows(self) -> dict:
        """The configured window and the files this adapter reads (printed by the report)."""
        return {"window_seconds": self.window_seconds, "profile": self.profile,
                "files": list(self.files), "memory_dir": str(self.memory_dir)}

    def withheld_counts(self) -> dict:
        """``{unusable:...: n}`` over every read so far (counts only, never entry text)."""
        return dict(self.withheld)

    def rules_dict(self) -> dict:
        """The rule table this adapter applies (report data: markers, floor, table size)."""
        return self.rules.as_dict()

    # ── the read ────────────────────────────────────────────────────────────────────────────

    def read(self, scope: Any) -> Sequence[Record]:
        """Every entry of a recent memory file, oldest file first, newest entry first inside it."""
        self.calls += 1
        scope_profile = "" if getattr(scope, "profile", None) is None else str(scope.profile)
        if self.profile and scope_profile and scope_profile != self.profile:
            raise SourceUnavailable(UNAVAILABLE_SCOPE_MISMATCH)

        now = float(self._clock())
        oldest = now - self.window_seconds
        records: List[Record] = []
        for name in self.files:
            path = self._resolve(name)
            if path is None:
                continue  # no such file: this profile simply has no memory here yet
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            except OSError as exc:  # a file that exists but cannot be stat'ed is unreadable
                logger.warning("proactive-conversation: memory file %s could not be inspected (%s)",
                               name, type(exc).__name__)
                raise SourceUnavailable(C.UNAVAILABLE_READ_FAILED, detail=type(exc).__name__)
            if float(stat.st_mtime) < oldest or float(stat.st_mtime) > now + 1.0:
                # Too old to be recent memory (or stamped in the future, which is a broken clock,
                # not a fresher memory): contributes nothing.
                continue
            try:
                # ``utf-8-sig`` mirrors Hermes' own memory reader (``MemoryStore._read_raw_checked``):
                # a BOM is stripped rather than glued to the first entry, so the entry text — and
                # therefore its fingerprint — is exactly what Hermes would show for the same file.
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                logger.warning("proactive-conversation: memory file %s could not be read (%s)",
                               name, type(exc).__name__)
                raise SourceUnavailable(C.UNAVAILABLE_READ_FAILED, detail=type(exc).__name__)
            for index, entry in enumerate(parse_entries(text), start=1):
                reason = self.rules.why_unusable(entry)
                if reason:
                    self.withheld[reason] = self.withheld.get(reason, 0) + 1
                    continue
                records.append(Record(source=self.name, record_id=f"{name}#{index}",
                                      at=float(stat.st_mtime), text=entry, subject="user"))
        return tuple(records)

    def _resolve(self, name: str) -> Optional[Path]:
        """The file's path when it stays inside the memory directory, else ``None`` / a refusal.

        A name this profile simply does not have yet returns ``None`` (contributes nothing, the
        source stays readable). A file that *exists* but resolves outside the profile's
        ``memories/`` directory is refused as ``unavailable:read-failed``: a crafted symlink or a
        path built from data is not memory this profile owns, and reading it anyway would be the
        one thing this adapter must never do.
        """
        candidate = self.memory_dir / name
        try:
            real = Path(os.path.realpath(candidate))
            root = Path(os.path.realpath(self.memory_dir))
        except OSError:  # pragma: no cover - defensive; realpath only fails on absurd paths
            return None
        if real == root / name:
            return candidate
        if not candidate.exists() and not os.path.islink(candidate):
            return None
        logger.warning("proactive-conversation: memory file %s resolves outside the memory directory", name)
        raise SourceUnavailable(C.UNAVAILABLE_READ_FAILED, detail="outside-memory-dir")


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0.0:
        raise ValueError(f"{field} must be a positive number, got {value!r}")
    return float(value)
