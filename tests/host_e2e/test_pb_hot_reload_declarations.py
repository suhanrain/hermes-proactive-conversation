"""Channel-declaration hot reload (TASK t_e4480e53 Part A).

Editing ``config.yaml``'s per-session declaration must take effect on the **next tick**, with no
gateway restart. The one invariant every test here pins:

    **counters are the facts; the config is only the gate.**

A hot reload swaps the gate and never touches a counter. The thresholds are re-measured against the
*existing* counters at the next ``policy`` decision — a change that tightens a cap below what has
already happened caps immediately, and a change never refunds/clears a day count (config change ≠
"reset the daily quota"). An invalid edit and a timezone change are refused (keep the previous good
config + a deduped warning); the timezone is refused because ``state.day`` is anchored to it.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TESTS = _HERE.parent
for _path in (str(_TESTS), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest

import support  # noqa: E402
from proactive import config as pconfig  # noqa: E402
from proactive import contract as C  # noqa: E402
from proactive import policy  # noqa: E402
from proactive import runtime  # noqa: E402
from proactive import state as pstate  # noqa: E402
from proactive import store as pstore  # noqa: E402
from proactive import trigger  # noqa: E402

T0 = 1_800_000_000.0        # 2027-01-15 16:00 Asia/Shanghai (outside DND)
DUE = T0 + 3_600.0          # 17:00 SH — one idle window later


class _Ctx:
    """A minimal host context: only ``get_config`` (the seam every plugin setting is read through)."""

    def __init__(self, settings):
        self._settings = dict(settings)

    def get_config(self, key, default=None):
        return self._settings.get(key, default)


@pytest.fixture(autouse=True)
def _clean_seams():
    runtime.reset_seams()
    trigger.reset()
    # Start each test from a clean dedupe state so the "warn once" assertions are deterministic.
    pconfig._RELOAD_WARNED.clear()
    pconfig._RELOAD_INFO_LOGGED.clear()
    yield
    runtime.reset_seams()
    trigger.reset()


def _two_layer(**over):
    """A complete two-layer config (six core keys + the three required caps)."""
    raw = {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "dnd_start": "23:00",
        "dnd_end": "09:00",
        "min_idle_seconds": 3_600,
        "max_idle_seconds": 3_600,
        "window_max_attempts": 6,
        "window_min_visible": 1,
        "day_max_silent": 5,
        "day_max_attempts": 6,
    }
    raw.update(over)
    return raw


def _cfg(**over):
    parsed = pconfig.parse(_two_layer(**over))
    assert parsed.ok, (parsed.field, parsed.reason)
    return parsed.config


# ── ① a threshold change never clears the day counters ──────────────────────────────────────

def test_reconcile_keeps_the_day_counters_and_the_tightened_cap_bites():
    old = _cfg(day_max_attempts=6)
    new_raw = _two_layer(day_max_attempts=2, day_max_silent=2)  # tighten day total 6→2 (S_d≤R_d)
    result = pconfig.reconcile_config(old, _two_layer(day_max_attempts=6), new_raw)
    assert result.kind == "adopted"

    # A state that already spent 3 attempts today (a fact) is measured against the new cap of 2.
    day = pconfig.local_day(DUE, result.config)
    state = pstate.SessionState(anchor_at=T0, deadline=T0, day=day, attempt_count=3,
                                status=C.STATUS_ACTIVE)
    state, action, reason = policy.evaluate_release(state, result.config, DUE, support.FixedRng(0.5))
    assert (action, reason) == ("skip", C.REASON_CAP_DAY)
    # The config change did NOT reset the day count (only a real rollover does).
    assert state.attempt_count == 3
    assert state.day == day


# ── ② tightening a cap below what already happened → immediate cap ─────────────────────────

def test_tightened_window_cap_caps_a_spent_window_immediately():
    old = _cfg(window_max_attempts=6, window_min_visible=1)
    new_raw = _two_layer(window_max_attempts=3, window_min_visible=1)
    result = pconfig.reconcile_config(old, _two_layer(window_max_attempts=6), new_raw)
    assert result.kind == "adopted"

    day = pconfig.local_day(DUE, result.config)
    # 4 attempts already booked into the window; the new cap is 3 → cap at once.
    state = pstate.SessionState(anchor_at=T0, deadline=T0, day=day, attempt_since_user=4,
                                attempt_count=4, status=C.STATUS_ACTIVE)
    state, action, reason = policy.evaluate_release(state, result.config, DUE, support.FixedRng(0.5))
    assert (action, reason) == ("skip", C.REASON_CAP_WINDOW)
    assert state.attempt_since_user == 4


# ── ③ an invalid config keeps the previous good config + a warning, never crashes ──────────

def test_invalid_new_config_keeps_the_good_one_and_warns_once(caplog):
    old = _cfg()
    bad_raw = _two_layer(window_min_visible=99)       # V_w > R_w → invalid
    with caplog.at_level(logging.WARNING, logger="proactive.config"):
        first = pconfig.reconcile_config(old, _two_layer(), bad_raw)
        pconfig.log_reload(first)
        second = pconfig.reconcile_config(old, _two_layer(), bad_raw)
        pconfig.log_reload(second)                    # same bad hash → no second warning

    assert first.kind == "invalid"
    assert first.config is old                        # the previous good config is kept
    assert first.config_raw == _two_layer()           # the old raw is kept, verbatim
    warnings = [r for r in caplog.records if "rejected" in r.getMessage()]
    assert len(warnings) == 1, warnings


# ── ④ a timezone change is refused (keep the old timezone) ─────────────────────────────────

def test_timezone_change_is_refused_and_old_timezone_kept(caplog):
    old = _cfg(timezone="Asia/Shanghai")
    moved_raw = _two_layer(timezone="UTC")            # state.day is bound to the old timezone
    with caplog.at_level(logging.WARNING, logger="proactive.config"):
        result = pconfig.reconcile_config(old, _two_layer(timezone="Asia/Shanghai"), moved_raw)
        pconfig.log_reload(result)

    assert result.kind == "tz-blocked"
    assert result.config.timezone == "Asia/Shanghai"  # old timezone kept
    assert result.config is old
    assert any("timezone" in r.getMessage().lower() and "rejected" in r.getMessage()
               for r in caplog.records)


# ── ⑤ a window-layer cap change is measured against the existing window counts ─────────────

def test_window_layer_change_uses_the_existing_counts():
    old = _cfg(window_max_attempts=6, window_min_visible=1)
    new_raw = _two_layer(window_max_attempts=3, window_min_visible=1)
    result = pconfig.reconcile_config(old, _two_layer(window_max_attempts=6), new_raw)
    assert result.kind == "adopted"
    day = pconfig.local_day(DUE, result.config)

    # 2 attempts so far in the window: still below the new R_w=3 → allowed (window due).
    state = pstate.SessionState(anchor_at=T0, deadline=T0, day=day, attempt_since_user=2,
                                visible_since_user=0, attempt_count=2, status=C.STATUS_ACTIVE)
    state, action, reason = policy.evaluate_release(state, result.config, DUE, support.FixedRng(0.5))
    assert (action, reason) == ("allow", C.REASON_WINDOW_DUE)


# ── integration: the change reaches a session that fires only from the clock ─────────────────

def test_a_declaration_edit_applies_on_the_next_tick_without_restart(tmp_path):
    """The clock tick reads the mirrored blob; a declaration edit must refresh it in place."""
    store = support.new_store(tmp_path)
    source = support.dm_source(chat_id="oc-hot-reload-tick")
    entry = support.seed_session(store, source)
    dims = support.dims_for(store, entry, source)

    runtime.set_clock(lambda: T0)
    assert runtime.enable_session(store, entry.session_key, dims, _two_layer())

    # Seed a day fact: 2 attempts already booked today.
    read = pstore.read(store, entry.session_key, dims)
    seeded = read.state
    seeded.attempt_count = 2
    seeded.day = pconfig.local_day(T0, read.config)
    assert pstore.write(store, entry.session_key, config_raw=read.config_raw, state=seeded,
                        dims=dims, origin=read.origin)

    # Operator edits config.yaml: tighten the day total to 2 (already reached, S_d≤R_d) and change idle.
    decl = dict(_two_layer(day_max_attempts=2, day_max_silent=2, min_idle_seconds=600,
                           max_idle_seconds=600),
                session_key=entry.session_key, session_id=entry.session_id)
    runtime.bind_plugin_context(_Ctx({C.SETTING_SESSION_DECLARATIONS: [decl]}))
    runtime.set_clock(lambda: DUE)

    out = trigger.evaluate_and_inject(
        store, session_key=entry.session_key, session_id=entry.session_id, dims=dims,
        now=DUE, inject=lambda *a, **k: True, rng=support.FixedRng(0.5),
    )
    # The tightened R_d=2 caps the day at once (2 attempts already happened).
    assert out.accepted is False
    assert out.reason == C.REASON_CAP_DAY

    after = pstore.read(store, entry.session_key, dims)
    # Counters are the facts: the two attempts are still there (not reset by the config change).
    assert after.state.attempt_count == 2
    # The edit was adopted and persisted: the day cap and the idle bounds are the new values.
    assert after.config.day_max_attempts == 2
    assert (after.config.min_idle_seconds, after.config.max_idle_seconds) == (600, 600)


def test_hot_reload_logs_once_and_names_no_identifier(caplog):
    old = _cfg()
    with caplog.at_level(logging.INFO, logger="proactive.config"):
        logged = pconfig.reconcile_config(old, _two_layer(day_max_silent=5),
                                          _two_layer(day_max_silent=3))
        pconfig.log_reload(logged, pstate.SessionState(attempt_count=2, silent_count=1))

    lines = [r.getMessage() for r in caplog.records if "hot-reloaded" in r.getMessage()]
    assert len(lines) == 1, lines
    msg = lines[0]
    assert "caps R_w/V_w/S_d/R_d" in msg
    assert "counters kept attempt=2 silent=1" in msg
