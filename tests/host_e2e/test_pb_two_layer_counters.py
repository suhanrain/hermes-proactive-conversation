"""The two-layer counter model — behaviour.

These drive the real ``proactive.policy`` / ``state`` / ``config`` / ``trigger`` code with an explicit
clock and an injected RNG. They pin these semantics:

* window layer: attempts ≤ R_w; below V_w a round is forced visible; silent ≤ R_w − V_w; the window
  is spent → stop until the next user message reopens it;
* day layer: silent rounds ≤ S_d (a forced-visible round is exempt — the "muted for a whole day" fix);
  the optional R_d hard-stops the day's total attempts;
* ``0`` = "no limit"; ``enabled`` resets (user message clears the window, midnight clears the day);
* old-key migration feeds the new model.
"""

from __future__ import annotations

import pytest

import support
from proactive import config as pconfig
from proactive import contract as C
from proactive import policy, state as pstate, trigger

T0 = 1_800_000_000.0
DUE = T0 + 7_200.0  # anything past min_idle

BASE = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "dnd_start": "23:00",
    "dnd_end": "09:00",
    "min_idle_seconds": 60,
    "max_idle_seconds": 120,
}
#: A generous default day/vv set so the window layer is what these tests exercise; individual tests
#: override the knob they care about.
LAYER = dict(BASE, window_max_attempts=4, window_min_visible=1, day_max_silent=50, day_max_attempts=0)


def _cfg(**over):
    raw = dict(LAYER)
    raw.update(over)
    parsed = pconfig.parse(raw)
    assert parsed.ok, (parsed.field, parsed.reason)
    return parsed.config


def _rng():
    return support.FixedRng(0.5)


def _state(cfg, **kw):
    kw.setdefault("day", pconfig.local_day(DUE, cfg))
    kw.setdefault("anchor_at", T0)
    kw.setdefault("deadline", T0)
    return pstate.SessionState(**kw)


def _release(state, cfg, *, visible, at=DUE):
    """Run one full round: evaluate (must allow), then commit visible/silent.

    The window is forced due first (``deadline`` just before ``at``) so these tests exercise the
    *caps*, not the silence-window timing (which has its own tests).
    """
    state.deadline = float(at) - 1.0
    state, action, reason = policy.evaluate_release(state, cfg, at, _rng())
    assert action == "allow", reason
    state.pending = {"released_at": at, "session_id": "sid-two-layer"}
    state, committed = policy.commit_release(state, cfg, at, _rng(), visible=visible)
    assert committed
    return state


# ── 1. window budget: attempts ≤ R_w, then cap:window until the user speaks ──────────────────

def test_window_budget_stops_releases_until_a_user_message():
    cfg = _cfg(window_max_attempts=3, window_min_visible=1)
    state = _state(cfg)
    # 1 forced visible + 2 silent = 3 attempts
    state = _release(state, cfg, visible=True)
    state = _release(state, cfg, visible=False)
    state = _release(state, cfg, visible=False)
    assert state.attempt_since_user == 3

    state.deadline = T0  # due again, but the window budget is spent
    state, action, reason = policy.evaluate_release(state, cfg, DUE + 5, _rng())
    assert (action, reason) == ("skip", C.REASON_CAP_WINDOW)

    # A real user message reopens the window.
    state = policy.on_user_message(state, cfg, DUE + 10, _rng())
    assert state.attempt_since_user == 0 and state.visible_since_user == 0
    state.deadline = T0
    state, action, reason = policy.evaluate_release(state, cfg, DUE + 70, _rng())
    assert (action, reason) == ("allow", C.REASON_WINDOW_DUE)


# ── 2. silent cap is DERIVED as R_w − V_w (never configured) ────────────────────────────────

def test_silent_rounds_are_capped_at_rw_minus_vw():
    cfg = _cfg(window_max_attempts=5, window_min_visible=2, day_max_silent=50)
    assert cfg.window_max_attempts - cfg.window_min_visible == 3
    state = _state(cfg)
    state = _release(state, cfg, visible=True)   # visible 1
    state = _release(state, cfg, visible=True)   # visible 2 (V_w met)
    state = _release(state, cfg, visible=False)  # silent 1
    state = _release(state, cfg, visible=False)  # silent 2
    state = _release(state, cfg, visible=False)  # silent 3 == R_w - V_w
    assert state.visible_since_user == 2
    assert state.attempt_since_user - state.visible_since_user == 3  # exactly R_w - V_w silents
    state.deadline = T0
    state, action, reason = policy.evaluate_release(state, cfg, DUE + 5, _rng())
    assert (action, reason) == ("skip", C.REASON_CAP_WINDOW)


# ── 3. below V_w a round is forced visible and exempt from the day silent cap ───────────────

def test_forced_visible_below_vw_is_not_blocked_by_spent_day_silent_budget():
    cfg = _cfg(window_max_attempts=6, window_min_visible=1, day_max_silent=1)
    # The day's silent budget is already spent, and no visible round yet this window.
    state = _state(cfg, silent_count=1, visible_since_user=0, attempt_since_user=0, attempt_count=1)
    assert trigger.needs_forced_visible(state.visible_since_user, cfg.window_min_visible) is True
    state, action, reason = policy.evaluate_release(state, cfg, DUE, _rng())
    assert (action, reason) == ("allow", C.REASON_WINDOW_DUE)

    # Once the visible minimum is met, a silent round IS refused (S_d spent) — "no more silent today".
    state = _state(cfg, silent_count=1, visible_since_user=1, attempt_since_user=1, attempt_count=2)
    assert trigger.needs_forced_visible(state.visible_since_user, cfg.window_min_visible) is False
    state, action, reason = policy.evaluate_release(state, cfg, DUE, _rng())
    assert (action, reason) == ("skip", C.REASON_SILENT_CAP)


# ── 4. R_d hard-stops the day's total attempts (even a would-be-visible round) ──────────────

def test_day_attempt_cap_hard_stops():
    cfg = _cfg(window_max_attempts=10, window_min_visible=0, day_max_silent=2, day_max_attempts=3)
    state = _state(cfg, attempt_count=3, visible_since_user=0)
    state, action, reason = policy.evaluate_release(state, cfg, DUE, _rng())
    assert (action, reason) == ("skip", C.REASON_CAP_DAY)


# ── 5. "0" = no limit ────────────────────────────────────────────────────────────────────────

def test_zero_knobs_mean_no_limit():
    # R_w = 0 (no window cap), S_d = 0 (no day silent cap), only R_d is live.
    cfg = _cfg(window_max_attempts=0, window_min_visible=0, day_max_silent=0, day_max_attempts=4)
    state = _state(cfg)
    # No window cap and no silent cap: many silent rounds are allowed, up to the day attempts cap.
    for _ in range(4):
        state = _release(state, cfg, visible=False)
    assert state.silent_count == 4 and state.attempt_count == 4
    state.deadline = T0
    state, action, reason = policy.evaluate_release(state, cfg, DUE + 5, _rng())
    assert (action, reason) == ("skip", C.REASON_CAP_DAY)


# ── 6. resets ────────────────────────────────────────────────────────────────────────────────

def test_user_message_clears_the_window_not_the_day():
    cfg = _cfg(window_max_attempts=4, window_min_visible=1, day_max_silent=5)
    state = _state(cfg)
    state = _release(state, cfg, visible=True)
    state = _release(state, cfg, visible=False)
    att_su, vis_su, silent_day, att_day = (
        state.attempt_since_user, state.visible_since_user, state.silent_count, state.attempt_count)

    state = policy.on_user_message(state, cfg, DUE + 30, _rng())
    # Window counters are cleared…
    assert state.attempt_since_user == 0
    assert state.visible_since_user == 0
    # …but the day counters are NOT (a chat cannot refill the daily silent brake).
    assert state.silent_count == silent_day
    assert state.attempt_count == att_day


def test_day_rollover_clears_the_daily_counters():
    cfg = _cfg()
    yesterday = pconfig.local_day(T0 - 86_400.0, cfg)
    state = _state(cfg, day=yesterday, silent_count=3, attempt_count=5,
                   visible_since_user=2, attempt_since_user=4)
    assert policy.rollover_day(state, cfg, DUE) is True
    assert state.day == pconfig.local_day(DUE, cfg)
    assert state.silent_count == 0 and state.attempt_count == 0
    assert state.visible_since_user == 0 and state.attempt_since_user == 0


# ── 7. old-key migration behaves in the runtime policy ──────────────────────────────────────

def test_old_keys_are_rejected_under_the_two_layer_model():
    # With any two-layer key present, the old keys are not a fallback, so the
    # missing required two-layer keys make the declaration off:invalid — no silent derivation.
    raw = dict(BASE, minimum_visible_replies=1, max_silent_per_day=2,
               max_unanswered=3, max_per_day=20, window_max_attempts=3)
    parsed = pconfig.parse(raw)
    assert parsed.ok is False
    assert parsed.field in ("window_min_visible", "day_max_silent")

    # A complete two-layer block (the three required keys, no legacy keys needed) parses, and its
    # caps actually bite: after 2 silent rounds (S_d) a non-forced round is refused.
    full = dict(BASE, window_max_attempts=3, window_min_visible=1, day_max_silent=2)
    parsed = pconfig.parse(full)
    assert parsed.ok, (parsed.field, parsed.reason)
    cfg = parsed.config
    assert (cfg.window_max_attempts, cfg.window_min_visible, cfg.day_max_silent) == (3, 1, 2)

    state = _state(cfg, visible_since_user=1, attempt_since_user=1, silent_count=2, attempt_count=3)
    state, action, reason = policy.evaluate_release(state, cfg, DUE, _rng())
    assert (action, reason) == ("skip", C.REASON_SILENT_CAP)


# ── 8. a settled silent round books the window attempt, not just the day ────────────────────

def test_silent_commit_advances_both_window_and_day_attempts():
    cfg = _cfg()
    state = _state(cfg)
    before = (state.attempt_since_user, state.attempt_count, state.silent_count)
    state = _release(state, cfg, visible=False)
    assert (state.attempt_since_user, state.attempt_count, state.silent_count) == (1, 1, 1)
    assert before == (0, 0, 0)
