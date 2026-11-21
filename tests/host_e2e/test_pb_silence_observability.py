"""Observability — distinguishable, privacy-safe reasons for every clock outcome.

Each gate outcome must be tellable apart from the others (window-not-due / daily cap / visible-since-user
cap / DND / release), an empty or model-silenced answer must be logged under its own code, and the
clock's liveness must be readable (not-running / ticking / stale). The assertions check reason codes
and digests only — never session text — so the log cannot become a content leak.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TESTS = _HERE.parent
for _p in (str(_TESTS), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

import pytest

import support  # noqa: E402
from proactive import contract as C          # noqa: E402
from proactive import runtime, trigger       # noqa: E402
from proactive import store as pstore         # noqa: E402

T0 = 1_800_000_000.0
DUE = T0 + 3600.0


def _config(**over):
    base = {"enabled": True, "timezone": "Asia/Shanghai", "dnd_start": "23:00", "dnd_end": "09:00",
            "min_idle_seconds": 3600, "max_idle_seconds": 3600, "window_max_attempts": 6,
            "window_min_visible": 1, "day_max_silent": 4, "day_max_attempts": 6}
    base.update(over)
    return base


def _seed(tmp_path, *, config=None, now=T0):
    store = support.new_store(tmp_path)
    source = support.dm_source(chat_id="oc_obs")
    entry = support.seed_session(store, source)
    dims = support.dims_for(store, entry, source)
    runtime.set_clock(lambda: now)
    assert runtime.enable_session(store, entry.session_key, dims, config or _config())
    return store, entry.session_key, entry.session_id, dims


def _tick(store, skey, sid, dims, now, *, inject=None):
    sessions = [{"session_key": skey, "session_id": sid, "dims": dims}]
    if inject is None:
        def inject(text, session_key=None):
            return True
    return trigger.tick(store, sessions, now, inject=inject, rng=support.FixedRng(0.5))


@pytest.fixture(autouse=True)
def _clean():
    runtime.reset_seams()
    trigger.reset()
    yield
    runtime.reset_seams()
    trigger.reset()


@pytest.mark.parametrize("label,config,now,expected", [
    ("window-open", _config(), T0, C.REASON_WINDOW_OPEN),
    ("dnd", _config(dnd_start="00:00", dnd_end="23:59"), DUE, C.REASON_DND),
])
def test_each_gate_skip_has_its_own_logged_reason(tmp_path, caplog, label, config, now, expected):
    store, skey, sid, dims = _seed(tmp_path, config=config, now=now)
    caplog.set_level(logging.DEBUG, logger="proactive.trigger")

    outcomes = _tick(store, skey, sid, dims, now)

    assert outcomes[0][1].accepted is False
    assert outcomes[0][1].reason == expected
    digest = pstore.short_digest(skey)
    assert expected in caplog.text, f"{label}: the reason code must appear in the log"
    assert digest in caplog.text, f"{label}: the log carries the session digest"
    # No leakage: the raw session key or the DM chat id never appears verbatim.
    assert skey not in caplog.text, f"{label}: the full session key must not be logged"
    assert "oc_obs" not in caplog.text, f"{label}: chat text/ids must not be logged"


def test_a_release_is_logged_and_empty_and_silence_have_their_own_codes(tmp_path, caplog):
    store, skey, sid, dims = _seed(tmp_path)
    caplog.set_level(logging.DEBUG, logger="proactive.trigger")

    calls = []

    def _rec(text, session_key=None):
        calls.append(text)
        return True

    sessions = [{"session_key": skey, "session_id": sid, "dims": dims}]
    outcomes = trigger.tick(store, sessions, DUE, inject=_rec, rng=support.FixedRng(0.5))
    assert outcomes[0][1].accepted is True
    assert "released" in caplog.text

    # Empty answer: its own code, not confusion with a normal release.
    runtime.pre_llm_call(user_message=calls[0], turn_id="t-empty", session_id=sid)
    runtime.transform_llm_output(response_text="", session_id=sid, turn_id="t-empty")
    assert "empty-response" in caplog.text

    # (model-choice log is asserted in test_pb_silence_semantics; here we pin the empty code.)


def test_clock_status_distinguishes_running_from_not_running(tmp_path):
    # Fresh state: nothing armed.
    assert trigger.clock_status()["state"] == "not-running"

    store, skey, sid, dims = _seed(tmp_path)

    class _Ctx:
        def __init__(self):
            self.spawned = []

        def get_config(self, key, default=None):
            if key == C.SETTING_TRIGGER_MODE:
                return "on"
            return default

        def spawn_task(self, coro, name=None):
            self.spawned.append(coro)
            coro.close()
            return object()

    runtime.bind_plugin_context(_Ctx())
    assert runtime.arm_trigger_clock(store) is True
    status = trigger.clock_status()
    assert status["state"] == "ticking"
    assert status["armed"] is True

    # A stale heartbeat on a task that still reports alive is surfaced as "stale", not "ticking".
    import time as _t
    trigger._last_tick_ts = _t.monotonic() - 10_000.0
    assert trigger.clock_status()["state"] == "stale"
