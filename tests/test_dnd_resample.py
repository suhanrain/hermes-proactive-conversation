"""DND exit must re-sample even when the caller has no RNG to inject.

The tick loop is the production caller: it has a wall clock but no RNG. A ``dnd_hold`` session
must still leave DND without raising ``AttributeError`` and without leaving the hold set.
"""

from __future__ import annotations

import support
from proactive import contract as C
from proactive import trigger

TZ = "Asia/Shanghai"
SK = "agent:main:feishu:dm:TESTONLY"
SID = "test-session"
DIMS = {
    "session_key": SK,
    "session_id": SID,
    "profile": None,
    "platform": "feishu",
    "chat_id": "TESTONLY",
    "thread_id": None,
}
CFG = {
    "enabled": True,
    "timezone": TZ,
    "dnd_start": "23:00",
    "dnd_end": "09:00",
    "min_idle_seconds": 600,
    "max_idle_seconds": 1800,
    "window_max_attempts": 4,
    "window_min_visible": 2,
    "day_max_silent": 4,
    "day_max_attempts": 6,
}
STATE = {
    "anchor_at": 0.0,
    "deadline": 0.0,
    "day": "2026-11-22",
    "status": "active",
    "last_user_at": support.epoch(TZ, 2026, 11, 22, 21, 46) + 37,
    "last_release_at": 0.0,
    "dnd_hold": True,
    "dnd_blocked_at": support.epoch(TZ, 2026, 11, 22, 23, 52) + 13,
    "pending": None,
    "attempt_count": 0,
    "visible_count": 0,
    "silent_count": 0,
    "visible_since_user": 0,
    "attempt_since_user": 0,
}
BLOB = {
    "version": 1,
    "dims": DIMS,
    "config": CFG,
    "state": STATE,
    "origin": "declaration",
}


class _Store:
    def __init__(self) -> None:
        self.written = None

    def get_session_metadata(self, sk, key, default=None):
        return (self.written or BLOB) if key == "proactive-conversation:session" else default

    def set_session_metadata(self, sk, key, value):
        self.written = value
        return True


def test_dnd_exit_resamples_without_injected_rng():
    now = support.epoch(TZ, 2026, 11, 23, 9, 0) + 29
    store = _Store()

    outcomes = trigger.tick(
        store,
        [{"session_key": SK, "session_id": SID, "dims": DIMS}],
        now,
    )

    assert len(outcomes) == 1
    outcome = outcomes[0][1]
    assert outcome.reason == C.REASON_DND_RESAMPLE
    assert store.written is not None
    assert store.written["state"]["dnd_hold"] is False

    deadline = float(store.written["state"]["deadline"])
    dnd_end = support.epoch(TZ, 2026, 11, 23, 9, 0)
    assert dnd_end + CFG["min_idle_seconds"] <= deadline <= dnd_end + CFG["max_idle_seconds"]
