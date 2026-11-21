"""The pure policy: window sampling, caps, DND, pause/resume, idempotence, config-driven.

Every test here runs the real ``proactive.policy`` / ``proactive.config`` code with an explicit
clock value and an injected RNG, so each verdict is reproducible and the boundary cases are
genuine equalities (``now == deadline``), not "close enough".
"""

from __future__ import annotations

import pytest

import support
from proactive import config as cfg_mod
from proactive import contract as C
from proactive import policy
from proactive.state import SessionState

# 2026-10-16 12:00 in Asia/Shanghai — outside both fixtures' DND windows.
T0 = support.epoch("Asia/Shanghai", 2026, 10, 16, 12, 0)
HOUR = 3600.0


def _due_state(cfg, *, now=T0, lead=HOUR, **kw) -> SessionState:
    """A configured session whose sampled window came due ``lead`` seconds before ``now``."""
    state = SessionState(
        anchor_at=kw.pop("anchor_at", now - 10 * HOUR),
        deadline=now - lead,
        visible_since_user=kw.pop("visible_since_user", 0),
        day=kw.pop("day", cfg_mod.local_day(now, cfg)),
        visible_count=kw.pop("visible_count", 0),
        last_user_at=kw.pop("last_user_at", now - 10 * HOUR),
    )
    for key, value in kw.items():
        setattr(state, key, value)
    return state


# ── window sampling and re-sampling ─────────────────────────────────────────────────────────


def test_window_sample_stays_inside_the_configured_bounds():
    cfg = support.config(support.CONFIG_A)  # 6h … 24h
    assert policy.sample_deadline(T0, cfg, support.FixedRng(0.0)) == T0 + 6 * HOUR
    assert policy.sample_deadline(T0, cfg, support.FixedRng(1.0)) == T0 + 24 * HOUR
    assert policy.sample_deadline(T0, cfg, support.FixedRng(0.5)) == T0 + 15 * HOUR


def test_new_user_message_resamples_the_window_and_resets_the_unanswered_counter():
    cfg = support.config(support.CONFIG_A)
    state = _due_state(cfg, visible_since_user=2, visible_count=1, dnd_hold=True, dnd_blocked_at=T0 - HOUR)
    now = T0 + 2 * HOUR
    after = policy.on_user_message(state, cfg, now, support.FixedRng(0.0), anchor_at=T0 + HOUR)
    assert after.anchor_at == T0 + HOUR
    assert after.deadline == T0 + HOUR + 6 * HOUR
    assert after.visible_since_user == 0
    assert after.dnd_hold is False
    assert after.visible_count == 1, "a user message must not move the daily counter"


def test_evaluation_never_moves_the_anchor_while_the_window_is_open():
    """Nothing but a user message (or a counted release / resume) re-anchors the window.

    The bot's own replies reach this plugin only through ``transform_llm_output``, which does not
    touch the anchor; evaluation itself is a pure read on the schedule.
    """
    cfg = support.config(support.CONFIG_A)
    state = _due_state(cfg, lead=-12 * HOUR, anchor_at=T0)
    for step in (1, 2, 3, 4):
        state, action, reason = policy.evaluate_release(state, cfg, T0 + step * HOUR,
                                                       support.FixedRng(0.0))
        assert action == "skip" and reason == C.REASON_WINDOW_OPEN
        assert state.anchor_at == T0 and state.deadline == T0 + 12 * HOUR


def test_a_release_resamples_the_window_from_the_release_moment():
    cfg = support.config(support.CONFIG_A)
    state = _due_state(cfg, lead=HOUR)
    state, action, reason = policy.evaluate_release(state, cfg, T0, support.FixedRng(0.5))
    assert (action, reason) == ("allow", C.REASON_WINDOW_DUE)
    assert state.pending is not None
    committed, ok = policy.commit_release(state, cfg, T0, support.FixedRng(0.0))
    assert ok is True
    assert committed.anchor_at == T0, "the next window is measured from the release"
    assert committed.deadline == T0 + 6 * HOUR
    assert committed.visible_since_user == 1
    assert committed.pending is None
    assert committed.last_release_at == T0


# ── caps and the session-local day ──────────────────────────────────────────────────────────


def test_window_cap_blocks_after_the_attempt_budget():
    cfg = support.config(support.CONFIG_A)  # window_max_attempts R_w = 6
    state = _due_state(cfg, attempt_since_user=6)
    state, action, reason = policy.evaluate_release(state, cfg, T0, support.FixedRng(0.0))
    assert (action, reason) == ("skip", C.REASON_CAP_WINDOW)
    assert state.pending is None and state.attempt_count == 0


def test_window_cap_reopens_on_a_user_message():
    cfg = support.config(support.CONFIG_A)  # R_w = 6
    state = _due_state(cfg, attempt_since_user=6)
    state, action, reason = policy.evaluate_release(state, cfg, T0, support.FixedRng(0.0))
    assert (action, reason) == ("skip", C.REASON_CAP_WINDOW)

    state = policy.on_user_message(state, cfg, T0, support.FixedRng(0.0))
    assert state.attempt_since_user == 0, "a user message reopens the window"
    state.deadline = T0  # the user message re-sampled it; make it due again
    _, action, reason = policy.evaluate_release(state, cfg, T0, support.FixedRng(0.0))
    assert (action, reason) == ("allow", C.REASON_WINDOW_DUE)


def test_daily_cap_blocks_without_a_user_message():
    cfg = support.config(support.CONFIG_A)  # day_max_attempts R_d = 2
    # The day cap is measured on ``attempt_count`` (every released round, visible or silent).
    state = _due_state(cfg, attempt_count=2)
    state, action, reason = policy.evaluate_release(state, cfg, T0, support.FixedRng(0.0))
    assert (action, reason) == ("skip", C.REASON_CAP_DAY)
    assert state.attempt_count == 2


def test_crossing_midnight_clears_the_daily_counters():
    """The day layer is a calendar fact: rollover clears the day attempt/silent counters."""
    cfg = support.config(support.CONFIG_A)
    state = _due_state(cfg, day="2026-10-15", attempt_count=2, silent_count=2)
    state, action, reason = policy.evaluate_release(state, cfg, T0, support.FixedRng(0.0))
    assert state.day == "2026-10-16"
    assert state.attempt_count == 0 and state.silent_count == 0
    assert (action, reason) == ("allow", C.REASON_WINDOW_DUE), (
        "the cleared day budget no longer blocks the round"
    )

    fresh = _due_state(cfg, day="2026-10-15", attempt_count=2)
    fresh, action, reason = policy.evaluate_release(fresh, cfg, T0, support.FixedRng(0.0))
    assert (action, reason) == ("allow", C.REASON_WINDOW_DUE)
    fresh, ok = policy.commit_release(fresh, cfg, T0, support.FixedRng(0.0))
    assert ok and fresh.day == "2026-10-16" and fresh.attempt_count == 1


def test_day_boundary_follows_the_configured_timezone():
    """One instant, two configured timezones: only the session whose local day rolled over may fire."""
    instant = support.epoch("Asia/Shanghai", 2026, 10, 16, 23, 30)  # 2026-10-17 00:30 in Tokyo
    sh = support.config(dict(support.CONFIG_A, dnd_start="06:00", dnd_end="06:15"))
    tk = support.config(dict(support.CONFIG_B, dnd_start="06:00", dnd_end="06:15"))
    assert cfg_mod.local_day(instant, sh) == "2026-10-16"
    assert cfg_mod.local_day(instant, tk) == "2026-10-17"

    sh_state = _due_state(sh, now=instant, day="2026-10-16", attempt_count=2)
    sh_state, sh_action, sh_reason = policy.evaluate_release(
        sh_state, sh, instant, support.FixedRng(0.0))
    tk_state = _due_state(tk, now=instant, day="2026-10-16", attempt_count=1)
    tk_state, tk_action, tk_reason = policy.evaluate_release(
        tk_state, tk, instant, support.FixedRng(0.0))

    # The daily cap is measured on ``attempt_count`` (every released round), see ``policy`` docstring.
    assert (sh_action, sh_reason) == ("skip", C.REASON_CAP_DAY)
    assert (tk_action, tk_reason) == ("allow", C.REASON_WINDOW_DUE)
    assert tk_state.attempt_count == 0 and tk_state.day == "2026-10-17"


# ── DND ─────────────────────────────────────────────────────────────────────────────────────


def test_dnd_bounds_are_half_open_and_cross_midnight():
    cfg = support.config(support.CONFIG_A)  # 23:00–09:00, wraps midnight
    at = lambda h, m: support.epoch("Asia/Shanghai", 2026, 10, 16, h, m)
    assert cfg_mod.in_dnd(at(22, 59), cfg) is False
    assert cfg_mod.in_dnd(at(23, 0), cfg) is True
    assert cfg_mod.in_dnd(at(0, 0), cfg) is True
    assert cfg_mod.in_dnd(at(8, 59), cfg) is True
    assert cfg_mod.in_dnd(at(9, 0), cfg) is False, "the end minute is already outside DND"

    day = support.config(dict(support.CONFIG_A, dnd_start="06:00", dnd_end="06:15"))
    assert cfg_mod.in_dnd(at(5, 59), day) is False
    assert cfg_mod.in_dnd(at(6, 0), day) is True
    assert cfg_mod.in_dnd(at(6, 14), day) is True
    assert cfg_mod.in_dnd(at(6, 15), day) is False


def test_dnd_blocks_a_due_window_and_records_a_hold():
    cfg = support.config(support.CONFIG_A)
    now = support.epoch("Asia/Shanghai", 2026, 10, 16, 23, 30)
    state = _due_state(cfg, now=now, lead=HOUR)
    before = state.deadline
    state, action, reason = policy.evaluate_release(state, cfg, now, support.FixedRng(0.0))
    assert (action, reason) == ("skip", C.REASON_DND)
    assert state.dnd_hold is True and state.dnd_blocked_at == now
    assert state.deadline == before, "DND must not move the window"
    assert state.visible_since_user == 0 and state.visible_count == 0


def test_dnd_voids_the_window_instead_of_queueing_a_catch_up_send():
    """A window that came due inside DND is re-sampled, never fired late."""
    cfg = support.config(support.CONFIG_A)
    blocked_at = support.epoch("Asia/Shanghai", 2026, 10, 16, 23, 30)
    last_user = support.epoch("Asia/Shanghai", 2026, 10, 16, 22, 0)
    dnd_end = support.epoch("Asia/Shanghai", 2026, 10, 17, 9, 0)
    assert cfg_mod.dnd_end_ts(blocked_at, cfg) == dnd_end

    state = _due_state(cfg, now=blocked_at, lead=HOUR, last_user_at=last_user, anchor_at=last_user)
    state, action, reason = policy.evaluate_release(state, cfg, blocked_at, support.FixedRng(0.0))
    assert (action, reason) == ("skip", C.REASON_DND)

    # First evaluation after DND: the window is re-sampled from the later of the last user message
    # and the DND end — nothing is delivered on the spot.
    state, action, reason = policy.evaluate_release(state, cfg, dnd_end, support.FixedRng(0.0))
    assert (action, reason) == ("skip", C.REASON_DND_RESAMPLE)
    assert state.anchor_at == dnd_end
    assert state.deadline == dnd_end + 6 * HOUR
    assert state.dnd_hold is False

    # The new window fires once, after it comes due — no backlog of two sends.
    state, action, reason = policy.evaluate_release(
        state, cfg, dnd_end + 6 * HOUR, support.FixedRng(0.0))
    assert (action, reason) == ("allow", C.REASON_WINDOW_DUE)
    state, ok = policy.commit_release(state, cfg, dnd_end + 6 * HOUR, support.FixedRng(0.0))
    assert ok and state.visible_since_user == 1


def test_dnd_hold_uses_the_interval_containing_the_block():
    cfg = support.config(support.CONFIG_A)
    early = support.epoch("Asia/Shanghai", 2026, 10, 16, 3, 0)  # morning leg of 23:00–09:00
    state = _due_state(cfg, now=early, lead=HOUR)
    state, _, _ = policy.evaluate_release(state, cfg, early, support.FixedRng(0.0))
    assert cfg_mod.dnd_end_ts(state.dnd_blocked_at, cfg) == support.epoch(
        "Asia/Shanghai", 2026, 10, 16, 9, 0)


# ── pause / resume ──────────────────────────────────────────────────────────────────────────


def test_paused_never_releases_and_retires_an_inflight_release():
    cfg = support.config(support.CONFIG_A)
    state = _due_state(cfg, pending={"released_at": T0 - 1, "session_id": "s1"})
    state = policy.pause(state, T0)
    assert state.status == C.STATUS_PAUSED and state.pending is None
    state, action, reason = policy.evaluate_release(state, cfg, T0, support.FixedRng(0.0))
    assert (action, reason) == ("skip", C.REASON_PAUSED)
    assert state.visible_since_user == 0


def test_resume_resamples_from_the_users_most_recent_message():
    cfg = support.config(support.CONFIG_A)
    last_user = T0 - 3 * HOUR
    state = _due_state(cfg, lead=-5 * HOUR, last_user_at=last_user)
    state = policy.pause(state, T0)
    state = policy.resume(state, cfg, T0, support.FixedRng(0.0))
    assert state.status == C.STATUS_ACTIVE
    assert state.deadline == last_user + 6 * HOUR, "resume re-samples from the user message"
    assert state.anchor_at == last_user


# ── idempotence and crash recovery ──────────────────────────────────────────────────────────


def test_duplicate_tick_cannot_release_twice():
    cfg = support.config(support.CONFIG_A)
    state, action, reason = policy.evaluate_release(_due_state(cfg), cfg, T0, support.FixedRng(0.0))
    assert (action, reason) == ("allow", C.REASON_WINDOW_DUE)
    assert state.deadline == T0 + cfg.min_idle_seconds  # provisional hold

    same, action2, reason2 = policy.evaluate_release(state, cfg, T0, support.FixedRng(0.0))
    assert (action2, reason2) == ("skip", C.REASON_WINDOW_OPEN)
    assert same.visible_since_user == 0, "the duplicate tick must not count anything"
    same, action3, reason3 = policy.evaluate_release(state, cfg, T0 + 60, support.FixedRng(0.0))
    assert (action3, reason3) == ("skip", C.REASON_WINDOW_OPEN)


def test_expired_inflight_release_moves_no_counter_and_frees_the_next_attempt():
    """A stale ticket (a turn that never came back) is dropped; nothing is counted for it."""
    cfg = support.config(support.CONFIG_A)
    state = _due_state(cfg, lead=HOUR)
    state.pending = {"released_at": T0 - C.PENDING_RELEASE_TTL_DEFAULT_SECONDS - 1, "session_id": "s1"}
    state, action, reason = policy.evaluate_release(state, cfg, T0, support.FixedRng(0.0))
    assert state.visible_since_user == 0 and state.visible_count == 0
    assert (action, reason) == ("allow", C.REASON_WINDOW_DUE)
    committed, ok = policy.commit_release(state, cfg, T0, support.FixedRng(0.0))
    assert ok is True and committed.visible_since_user == 1
    assert committed.last_release_at == T0, "only the round that reached the gate counts"


def test_a_round_that_never_returns_costs_at_most_one_message():
    """Crash simulation: state written, process gone. On restart the release is dropped, no counter
    moves, and the provisional window still holds — never a second send for one release."""
    cfg = support.config(support.CONFIG_A)
    state, action, _ = policy.evaluate_release(_due_state(cfg), cfg, T0, support.FixedRng(0.0))
    assert action == "allow"
    restored = SessionState.from_dict(state.to_dict())  # what a restart reads back
    assert restored.pending == state.pending

    # The turn never came back; the ticket expires.
    dropped = policy.expire_pending(restored, T0 + C.PENDING_RELEASE_TTL_DEFAULT_SECONDS + 1)
    assert dropped is True
    assert restored.pending is None
    assert restored.visible_since_user == 0 and restored.visible_count == 0
    _, action2, reason2 = policy.evaluate_release(
        restored, cfg, T0 + C.PENDING_RELEASE_TTL_DEFAULT_SECONDS + 1, support.FixedRng(0.0))
    assert (action2, reason2) == ("skip", C.REASON_WINDOW_OPEN)


def test_unreadable_state_is_ignored_not_repaired():
    assert SessionState.from_dict(None) is None
    assert SessionState.from_dict({"anchor_at": 1.0}) is None
    good = SessionState.from_dict(_due_state(support.config(support.CONFIG_A)).to_dict())
    assert good is not None
    broken = good.to_dict()
    broken["status"] = "whatever"
    assert SessionState.from_dict(broken) is None
    broken = good.to_dict()
    broken["visible_since_user"] = "2"
    assert SessionState.from_dict(broken) is None


# ── configuration drives the verdict ────────────────────────────────────────────────────────


def test_two_configurations_produce_different_verdicts_at_the_same_instant():
    a = support.config(support.CONFIG_A)  # 6h…24h, 2 visible-since-user, 2/day, Shanghai 23:00–09:00
    b = support.config(support.CONFIG_B)  # 1h…3h,  1 visible-since-user, 1/day, Tokyo 21:00–07:00
    now = T0 + 2 * HOUR
    anchor = T0

    # Both sessions were windows from the same user message; only the configuration differs.
    a_state = policy.new_state(anchor, a, support.FixedRng(0.0))
    b_state = policy.new_state(anchor, b, support.FixedRng(0.0))
    a_state.last_user_at = anchor
    b_state.last_user_at = anchor
    a_window, b_window = a_state.deadline, b_state.deadline
    a_state, a_action, a_reason = policy.evaluate_release(a_state, a, now, support.FixedRng(0.0))
    b_state, b_action, b_reason = policy.evaluate_release(b_state, b, now, support.FixedRng(0.0))

    assert (a_action, a_reason) == ("skip", C.REASON_WINDOW_OPEN), "A's 6h floor has not elapsed"
    assert a_window == anchor + 6 * HOUR and a_state.deadline == a_window
    assert (b_action, b_reason) == ("allow", C.REASON_WINDOW_DUE)
    assert b_window == anchor + 1 * HOUR, "B's 1h floor has elapsed"


def test_caps_differ_between_configurations():
    a = support.config(support.CONFIG_A)  # window_max_attempts R_w = 6
    b = support.config(support.CONFIG_B)  # window_max_attempts R_w = 2
    a_state = _due_state(a, attempt_since_user=2)
    b_state = _due_state(b, attempt_since_user=2)
    _, a_action, a_reason = policy.evaluate_release(a_state, a, T0, support.FixedRng(0.0))
    _, b_action, b_reason = policy.evaluate_release(b_state, b, T0, support.FixedRng(0.0))
    assert (a_action, a_reason) == ("allow", C.REASON_WINDOW_DUE)
    assert (b_action, b_reason) == ("skip", C.REASON_CAP_WINDOW)


def test_window_boundary_is_inclusive():
    cfg = support.config(support.CONFIG_A)
    deadline = T0 + 10 * HOUR
    state = _due_state(cfg, lead=-10 * HOUR, anchor_at=T0)
    assert state.deadline == deadline
    _, action, reason = policy.evaluate_release(state, cfg, deadline - 1, support.FixedRng(0.0))
    assert (action, reason) == ("skip", C.REASON_WINDOW_OPEN)
    _, action, reason = policy.evaluate_release(state, cfg, deadline, support.FixedRng(0.0))
    assert (action, reason) == ("allow", C.REASON_WINDOW_DUE)


def test_injected_rng_makes_the_schedule_reproducible():
    cfg = support.config(support.CONFIG_A)
    first = policy.sample_deadline(T0, cfg, support.seeded_rng(7))
    second = policy.sample_deadline(T0, cfg, support.seeded_rng(7))
    other = policy.sample_deadline(T0, cfg, support.seeded_rng(8))
    assert first == second
    assert first != other
    assert cfg.min_idle_seconds <= first - T0 <= cfg.max_idle_seconds


@pytest.mark.parametrize("reason", [C.REASON_DND, C.REASON_PAUSED])
def test_a_skip_never_touches_the_counters(reason):
    """Whatever the skip reason, a refused round moves no counter (it never reached the user)."""
    cfg = support.config(support.CONFIG_A)
    state = _due_state(cfg, visible_since_user=0, visible_count=0)
    if reason == C.REASON_PAUSED:
        state.status = C.STATUS_PAUSED
        now = T0
    else:
        now = support.epoch("Asia/Shanghai", 2026, 10, 16, 23, 30)
        state.deadline = now - HOUR
    state, action, got = policy.evaluate_release(state, cfg, now, support.FixedRng(0.0))
    assert action == "skip"
    assert state.visible_since_user == 0 and state.visible_count == 0
