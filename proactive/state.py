"""The durable per-session runtime state (the ``state`` subtree of the plugin-owned blob).

Field contract (stable — callers may read these keys directly):

``anchor_at``        epoch seconds the silence window is measured from: the last real user
                     message, or else the last release.
``deadline``         epoch seconds of the sampled release moment for the current window.
``day``              session-local day (``YYYY-MM-DD``) that the daily counters belong to.
``status``           ``active`` | ``paused``.
``last_user_at``     epoch seconds of the last real user message seen.
``last_release_at``  epoch seconds of the last committed release.
``dnd_hold``         a sampled window was voided by DND; re-sample on the next evaluation
                     outside DND (never a catch-up send).
``dnd_blocked_at``   when the DND block was recorded (used to compute the DND end).
``pending``          release ticket in flight (``{"released_at", "session_id"}``) or ``None``.
``attempt_count``    releases committed on ``day`` (visible or not).
``visible_count``    releases that produced a user-visible body on ``day`` (observability only).
``silent_count``     releases that ended silent on ``day`` (``day_max_silent`` budget).
``visible_since_user``  visible proactive rounds since the last real user message. Drives the
                     forced-visible decision: ``+1`` on a visible round,
                     cleared by a real user message and by the day rollover. Independent of
                     ``visible_count`` (the day's cumulative tally, kept only for observation).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from . import contract as C

STATE_KEYS = (
    "anchor_at",
    "deadline",
    "day",
    "status",
    "last_user_at",
    "last_release_at",
    "dnd_hold",
    "dnd_blocked_at",
    "pending",
    "attempt_count",
    "visible_count",
    "silent_count",
    "visible_since_user",
    "attempt_since_user",
)

_STR_FIELDS = ("day", "status")
_NUM_FIELDS = ("anchor_at", "deadline", "last_user_at", "last_release_at", "dnd_blocked_at")
_INT_FIELDS = ("attempt_count", "visible_count", "silent_count",
               "visible_since_user", "attempt_since_user")

#: Counters an older blob may not carry: ``from_dict`` tolerates all of these at ``0`` instead of
#: declaring the whole session unusable. None of them is required for correctness — only
#: ``attempt_count``/``attempt_since_user``/``silent_count``/``visible_since_user`` drive policy, and
#: those are always written, so tolerating a missing one here can never change a count.
_TOLERATED_ABSENT_INT_FIELDS = ("attempt_count", "visible_count",
                                "silent_count", "visible_since_user", "attempt_since_user")

# ── the durable ``pending`` pin ───────────────────────────────────────────────────────────────
# ``pending`` is the release pin (``{"released_at", "session_id"}``). A plugin-controlled
# injection trigger writes it as a richer, durable *pin* before the host is asked to run a turn, so
# a restarted process can still recognise the round. The pin adds four fields on top of the release
# ticket; ``from_dict``/``to_dict`` pass a ``pending`` mapping through unchanged, so the extra keys
# survive a round-trip with no schema change:
#
#   ``prompt_digest``  digest of the exact prompt the plugin injected (normalised with ``str.strip()``)
#   ``injected_at``    epoch seconds the injection was accepted by the host (``None`` until ``True``)
#   ``round_marker``   a random per-round token, so a stale write cannot clear a newer round's pin
#   ``deadline``       epoch seconds after which an unrecognised pin is a *lost* round (retired, never counted)
#
# Retiring the pin is :func:`retire_pin` (settle / silence / expire / refusal). The pin carries no
# chat text, no session identifier and no health value — only a digest and three timestamps/tokens.
PIN_FIELDS = ("prompt_digest", "injected_at", "round_marker", "deadline")


def pin_fields(pending: Any) -> Dict[str, Any]:
    """The four pin fields of a ``pending`` mapping (empty when none of them is present)."""
    if not isinstance(pending, dict):
        return {}
    return {key: pending.get(key) for key in PIN_FIELDS if key in pending}


def is_pin(pending: Any) -> bool:
    """True when ``pending`` is a B-lane pin (carries a usable ``prompt_digest`` + ``round_marker``)."""
    if not isinstance(pending, dict):
        return False
    digest = pending.get("prompt_digest")
    marker = pending.get("round_marker")
    return isinstance(digest, str) and bool(digest) and isinstance(marker, str) and bool(marker)


@dataclass
class SessionState:
    """Mutable per-session state; every transition returns the object it mutated."""

    anchor_at: float = 0.0
    deadline: float = 0.0
    day: str = ""
    status: str = C.STATUS_ACTIVE
    last_user_at: float = 0.0
    last_release_at: float = 0.0
    dnd_hold: bool = False
    dnd_blocked_at: float = 0.0
    pending: Optional[Dict[str, Any]] = None
    attempt_count: int = 0
    visible_count: int = 0
    #: Proactive rounds that ended silent on ``day``. Cleared by a real user
    #: message and by the session-local day rollover, never by a visible round.
    silent_count: int = 0
    #: Visible proactive rounds since the last real user message. This — not
    #: ``visible_count`` — drives the forced-visible decision: a fresh user message clears it, so the
    #: next round must speak again. Cleared by ``on_user_message`` and ``rollover_day``; ``+1`` on
    #: every visible round.
    visible_since_user: int = 0
    #: Attempts committed since the last real user message: the two-layer
    #: window counter (visible **and** silent). ``window_max_attempts`` caps it; a real user message
    #: reopens the window (clears it). The window's silent total is ``attempt_since_user −
    #: visible_since_user`` — derived, never stored. Cleared by ``on_user_message`` and rollover.
    attempt_since_user: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {key: getattr(self, key) for key in STATE_KEYS}

    def copy(self) -> "SessionState":
        return SessionState.from_dict(copy.deepcopy(self.to_dict())) or SessionState()

    @classmethod
    def from_dict(cls, data: Any) -> Optional["SessionState"]:
        """Rebuild state from persisted JSON; ``None`` when the value is not usable as-is.

        An unusable state is *ignored*, never repaired with defaults: the caller then treats the
        session as unconfigured (OFF) instead of guessing what the numbers mean.
        """
        if not isinstance(data, dict):
            return None
        required_keys = tuple(key for key in STATE_KEYS if key not in _TOLERATED_ABSENT_INT_FIELDS)
        if any(key not in data for key in required_keys):
            return None
        for key in _NUM_FIELDS:
            if isinstance(data[key], bool) or not isinstance(data[key], (int, float)):
                return None
        for key in _INT_FIELDS:
            if key not in data:
                if key in _TOLERATED_ABSENT_INT_FIELDS:
                    continue
                return None
            if isinstance(data[key], bool) or not isinstance(data[key], int):
                return None
        for key in _STR_FIELDS:
            if not isinstance(data[key], str):
                return None
        if not isinstance(data["dnd_hold"], bool):
            return None
        pending = data["pending"]
        if pending is not None and not isinstance(pending, dict):
            return None
        state = cls(
            anchor_at=float(data["anchor_at"]),
            deadline=float(data["deadline"]),
            day=str(data["day"]),
            status=str(data["status"]),
            last_user_at=float(data["last_user_at"]),
            last_release_at=float(data["last_release_at"]),
            dnd_hold=bool(data["dnd_hold"]),
            dnd_blocked_at=float(data["dnd_blocked_at"]),
            pending=dict(pending) if pending is not None else None,
            attempt_count=int(data.get("attempt_count", 0)),
            visible_count=int(data.get("visible_count", 0)),
            silent_count=int(data.get("silent_count", 0)),
            visible_since_user=int(data.get("visible_since_user", 0)),
            attempt_since_user=int(data.get("attempt_since_user", 0)),
        )
        if state.status not in (C.STATUS_ACTIVE, C.STATUS_PAUSED):
            return None
        return state
