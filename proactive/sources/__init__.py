"""Real, local, read-only source adapters for the two sources this package connects.

The assembly path can be fed by injected adapters (``content.StaticSource``), but a real install
needs local material to cite; without it every wake ends in "no reliable topic → silent". This
package provides two adapters that are local and need neither credentials nor network:

* :class:`~proactive.sources.session_transcript.SessionTranscriptSource` — this session's own
  recent messages, from the profile's ``state.db`` (``SELECT`` on a read-only connection);
* :class:`~proactive.sources.memory.CrossSessionMemorySource` — this profile's own memory files
  (``MEMORY.md`` / ``USER.md``), read as text.

:mod:`proactive.composition` is the root that wires them into
one :class:`proactive.content.Assembler`.

Which rows may become a topic at all is a *table*, not a literal in a query:
:mod:`proactive.sources.usability` holds :class:`TopicRules` — the machine-text markers Hermes
injects into ``role='user'`` rows, the length floor and the pure-acknowledgement table — and each
adapter is built with one (``TRANSCRIPT_TOPIC_RULES`` / ``MEMORY_TOPIC_RULES``). A row the table
refuses is withheld whole and counted (``withheld_counts()``); its text is never trimmed, because a
record's fingerprint has to resolve back to the stored row.

Neither adapter writes, uses the network, or invents a record: an unreadable source raises
``SourceUnavailable`` with a code, and the registry turns that into "this source contributed
nothing" — the P4 semantics, unchanged.
"""

from __future__ import annotations

from .codes import (NEW_REASON_CODES, UNAVAILABLE_SCOPE_MISMATCH,
                    UNAVAILABLE_SESSION_UNKNOWN, UNUSABLE_ACKNOWLEDGEMENT,
                    UNUSABLE_COMPACTION_SUMMARY, UNUSABLE_MACHINE_TEXT, UNUSABLE_TOO_SHORT)
from .memory import ENTRY_DELIMITER, MEMORY_FILES, CrossSessionMemorySource, parse_entries
from .session_transcript import EXCLUDED_DISPLAY_KINDS, SessionTranscriptSource
from .sqlite_readonly import is_read_only, open_read_only, read_only_uri
from .usability import (DEFAULT_ACKNOWLEDGEMENTS, DEFAULT_MIN_CHARS, MACHINE_MARKERS,
                        MEMORY_TOPIC_RULES, TRANSCRIPT_TOPIC_RULES, TopicRules, has_machine_marker,
                        normalize, topic_rules_from_profile)

__all__ = [
    "CrossSessionMemorySource",
    "DEFAULT_ACKNOWLEDGEMENTS",
    "DEFAULT_MIN_CHARS",
    "ENTRY_DELIMITER",
    "EXCLUDED_DISPLAY_KINDS",
    "MACHINE_MARKERS",
    "MEMORY_FILES",
    "MEMORY_TOPIC_RULES",
    "NEW_REASON_CODES",
    "SessionTranscriptSource",
    "TRANSCRIPT_TOPIC_RULES",
    "TopicRules",
    "UNAVAILABLE_SCOPE_MISMATCH",
    "UNAVAILABLE_SESSION_UNKNOWN",
    "UNUSABLE_ACKNOWLEDGEMENT",
    "UNUSABLE_COMPACTION_SUMMARY",
    "UNUSABLE_MACHINE_TEXT",
    "UNUSABLE_TOO_SHORT",
    "has_machine_marker",
    "is_read_only",
    "normalize",
    "open_read_only",
    "parse_entries",
    "read_only_uri",
    "topic_rules_from_profile",
]
