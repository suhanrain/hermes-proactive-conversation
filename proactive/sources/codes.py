"""Reason codes added by the real read-only sources.

They live here rather than in ``proactive/contract.py`` because they belong to the read-only
sources: the two ``unavailable:*`` codes below are the adapters' own vocabulary. The shapes follow
the existing codes (``contract.UNAVAILABLE_*`` / ``contract.DENIED_*``): lowercase,
``prefix:reason``, one literal quoted by the code, the tests and the report.

The two codes here describe a source that is *readable but must contribute nothing*:

* ``unavailable:session-unknown`` — the scope names a session that does not exist in this profile's
  session store (a reset, a switched profile, a hand-built scope): there is nothing to read, and
  guessing a nearby session would read another chat's text.
* ``unavailable:session-scope-mismatch`` — the scope's own fields do not agree with the stored row
  (different session key, different profile): the same refusal as the registry's
  ``denied:scope-mismatch``, but raised by the adapter that can see the stored row.

Read *failures* (a missing/unopenable database, an unreadable file, a timeout) deliberately reuse
``contract.UNAVAILABLE_READ_FAILED``: "the source could not be read" already has a code, and a
second one would just be a synonym.

The ``unusable:*`` codes are a different axis: the source read
fine, the row exists, and it still may not become a topic — citing it would attribute to the user
words the user never wrote (machine text), or quote a fragment that cannot carry a topic. They are
*withheld rows*, so they appear in an adapter's own ``withheld_counts()`` rather than in an
assembly's source status: an assembly is never told about a row its source already refused.

* ``unusable:machine-text`` — a line of the row opens with an injected envelope (compaction handoff,
  gateway origin preamble, cron delivery label, reply pointer): not the user's writing.
* ``unusable:compaction-summary`` — the row carries ``messages._compressed_summary = 1``, Hermes' own
  structural marker for a synthesized compaction handoff.
* ``unusable:too-short`` — normalised text shorter than the configured floor.
* ``unusable:acknowledgement`` — the whole row is a pure acknowledgement (「嗯」「好的」「不用了」).
"""

from __future__ import annotations

UNAVAILABLE_SESSION_UNKNOWN = "unavailable:session-unknown"
UNAVAILABLE_SCOPE_MISMATCH = "unavailable:session-scope-mismatch"

UNUSABLE_MACHINE_TEXT = "unusable:machine-text"
UNUSABLE_COMPACTION_SUMMARY = "unusable:compaction-summary"
UNUSABLE_TOO_SHORT = "unusable:too-short"
UNUSABLE_ACKNOWLEDGEMENT = "unusable:acknowledgement"

#: Every code this package adds, for tests and reports that want to enumerate them.
NEW_REASON_CODES = (
    UNAVAILABLE_SESSION_UNKNOWN,
    UNAVAILABLE_SCOPE_MISMATCH,
    UNUSABLE_MACHINE_TEXT,
    UNUSABLE_COMPACTION_SUMMARY,
    UNUSABLE_TOO_SHORT,
    UNUSABLE_ACKNOWLEDGEMENT,
)
