"""Deterministic per-session policy — pure functions over ``(state, config, now, rng)``.

The whole module is clock-free and RNG-free by construction: ``now`` (epoch seconds) and ``rng``
(anything with ``uniform(a, b)``) are arguments, so a decision is reproducible from its inputs and
a test can pin a window sample to an exact value.

Decision order for a heartbeat wake (``evaluate_release``) is fixed and observable. It always runs
the **two-layer** counter model:

1. drop an expired in-flight release ticket (no counters move; the TTL is the plugin-level
   ``pending_release_ttl_seconds`` setting, passed in as ``pending_ttl``),
2. paused → skip,
3. DND → skip **and void the window** (no catch-up send),
4. day rollover (clears only the daily counters),
5. the window-attempts cap (``window_max_attempts``), the optional day-attempts cap
   (``day_max_attempts``), then the day-silent cap (``day_max_silent``) only for a round that is not
   forced-visible,
6. window still open → skip; window due → allow (release).

``attempt_count`` feeds ``day_max_attempts`` and the window counter ``attempt_since_user`` feeds
``window_max_attempts``. The visible minimum (``window_min_visible``) and the silent cap are both
best-effort: the forced-visible prompt argues for the minimum, the silent cap caps the cost. The
silent cap is absolute — it is never waived because the visible minimum has not been reached yet.

The counting anchor lives in exactly one function: :func:`commit_release`. It is called when the
round passed the delivery gate (``runtime.transform_llm_output`` did not silence it), and it is
this plugin's substitute for a "the user received it" receipt that the host never issues.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from . import config as _config
from . import contract as C
from .state import SessionState


def sample_deadline(anchor_at: float, cfg: _config.SessionConfig, rng: Any) -> float:
    """``anchor + U[min_idle, max_idle]`` — one random pick inside the configured window."""
    if rng is None:
        import random as _random
        rng = _random
    return float(anchor_at) + float(rng.uniform(cfg.min_idle_seconds, cfg.max_idle_seconds))


def new_state(now: float, cfg: _config.SessionConfig, rng: Any) -> SessionState:
    """State for a session's first evaluation: the window starts at the first evaluation."""
    state = SessionState(status=C.STATUS_ACTIVE, day=_config.local_day(now, cfg))
    state.anchor_at = float(now)
    state.deadline = sample_deadline(now, cfg, rng)
    return state


def rollover_day(state: SessionState, cfg: _config.SessionConfig, now: float) -> bool:
    """Reset the daily and since-user counters when the session-local day changed; returns whether it
    did."""
    day = _config.local_day(now, cfg)
    if day == state.day:
        return False
    state.day = day
    state.attempt_count = 0
    state.visible_count = 0
    state.silent_count = 0
    state.visible_since_user = 0
    state.attempt_since_user = 0
    return True


def on_user_message(
    state: SessionState,
    cfg: _config.SessionConfig,
    now: float,
    rng: Any,
    anchor_at: Optional[float] = None,
) -> SessionState:
    """A real user message: re-anchor the silence window and clear the since-user counters.

    ``anchor_at`` lets the caller use the message's own timestamp (``event.timestamp``) instead of
    the moment the hook ran; it defaults to ``now``. The anchor never moves backwards.
    """
    anchor = float(now if anchor_at is None else anchor_at)
    state.last_user_at = max(state.last_user_at, anchor)
    state.anchor_at = anchor
    state.deadline = sample_deadline(anchor, cfg, rng)
    # ``silent_count`` is the **day** silent budget and a real user message does NOT reset it —
    # only the day rollover does (a chat cannot refill the daily token brake). The window's own
    # budget is ``attempt_since_user``, cleared just below.
    # A real user message resets the visible-since-user tally: the next
    # proactive round must speak again before silence is allowed, regardless of how many visible
    # rounds the calendar day already produced (``visible_count`` is untouched — it is the day's
    # cumulative tally, kept for observation only).
    state.visible_since_user = 0
    # The two-layer window is a fresh start: no attempt has been booked against it yet. This is
    # what reopens a spent window so the bot speaks again after the user talks — the "muted for a
    # whole day" problem.
    state.attempt_since_user = 0
    # A message that arrives while the bot owes a reply retires the in-flight release: the turn
    # it belongs to will be silenced by the delivery gate, and no counter may move for it.
    state.pending = None
    # The window is fresh from a real message, so any DND void is moot.
    state.dnd_hold = False
    rollover_day(state, cfg, now)
    return state


def expire_pending(state: SessionState, now: float,
                   ttl: float = C.PENDING_RELEASE_TTL_DEFAULT_SECONDS) -> bool:
    """Drop an in-flight release whose turn never came back; returns whether it was dropped.

    ``ttl`` is the effective plugin-level setting (``proactive.settings``), defaulted here to the
    documented default so the pure policy stays callable without a plugin context.

    Dropping it moves no counter: an unfinished round may cost at most one proactive message and
    never a double send, and that bound is deliberate.
    """
    pending = state.pending
    if not pending:
        return False
    released_at = pending.get("released_at")
    if not isinstance(released_at, (int, float)) or isinstance(released_at, bool):
        state.pending = None
        return True
    if now > float(released_at) + float(ttl):
        state.pending = None
        return True
    return False


def void_pending(state: SessionState) -> SessionState:
    """Retire the in-flight release without counting it (silenced or superseded turn)."""
    state.pending = None
    return state


def evaluate_release(
    state: SessionState,
    cfg: _config.SessionConfig,
    now: float,
    rng: Any,
    session_id: str = "",
    pending_ttl: float = C.PENDING_RELEASE_TTL_DEFAULT_SECONDS,
) -> Tuple[SessionState, str, str]:
    """Decide whether this wake may open a turn. Returns ``(state, action, reason)``.

    ``action`` is ``"allow"`` or ``"skip"``; ``reason`` is one of the ``contract.REASON_*`` codes.
    ``pending_ttl`` is the effective plugin-level ticket TTL (step 1 of the documented order).
    On ``allow`` the state carries a provisional deadline (``now + min_idle``) and a pending
    release ticket, so a duplicate tick can never release twice.
    """
    expire_pending(state, now, pending_ttl)
    if state.status != C.STATUS_ACTIVE:
        return state, "skip", C.REASON_PAUSED

    if _config.in_dnd(now, cfg):
        if not state.dnd_hold:
            state.dnd_hold = True
            state.dnd_blocked_at = float(now)
        return state, "skip", C.REASON_DND

    resampled = False
    if state.dnd_hold:
        # The window sampled before the DND block is void, not deferred: re-sample it from the
        # later of the user's last message and the end of the DND window that blocked it, so DND
        # exit cannot produce an immediate catch-up send.
        blocked_end = _config.dnd_end_ts(state.dnd_blocked_at, cfg)
        anchor = max(state.last_user_at, blocked_end)
        state.dnd_hold = False
        state.anchor_at = anchor
        state.deadline = sample_deadline(anchor, cfg, rng)
        resampled = True

    rollover_day(state, cfg, now)

    return _evaluate_two_layer(state, cfg, now, session_id=session_id, resampled=resampled)


def _evaluate_two_layer(
    state: SessionState,
    cfg: _config.SessionConfig,
    now: float,
    *,
    session_id: str = "",
    resampled: bool = False,
) -> Tuple[SessionState, str, str]:
    """The two-layer release decision (window layer + day layer).

    Decision order: window-attempts cap → day-attempts cap → day-silent cap (silent rounds only) →
    window due. A round below the window's visible minimum is **forced visible** (the prompt path
    enforces it via ``visible_since_user < window_min_visible``) and is therefore exempt from the
    day silent cap — so once the silent budget is spent the bot still replies when the user talks,
    it just stops burning tokens on unprompted silent rounds. ``0`` on a knob means "no limit"; the
    all-zero case is rejected at config time, so at least one cap is always live here.
    """
    rw = getattr(cfg, "window_max_attempts", None) or 0
    vw = getattr(cfg, "window_min_visible", 0) or 0
    sd = getattr(cfg, "day_max_silent", None) or 0
    rd = getattr(cfg, "day_max_attempts", None) or 0

    # 1. Window layer: the user-message window's attempt budget is spent → stop until the next user
    #    message reopens it ("spent the window, wait for the user").
    if rw > 0 and state.attempt_since_user >= rw:
        return state, "skip", C.REASON_CAP_WINDOW

    # 2. Day layer hard cap: an explicit "save money" ceiling on the day's total attempts. It cuts
    #    even a would-be-visible round (the operator chose cost over a send).
    if rd > 0 and state.attempt_count >= rd:
        return state, "skip", C.REASON_CAP_DAY

    # 3. Forced-visible: below the window's visible minimum this round MUST be visible. A visible
    #    round does not consume the day's silent budget, so the silent brake below does not apply.
    need_visible = vw > 0 and state.visible_since_user < vw
    if not need_visible and sd > 0 and state.silent_count >= sd:
        # 4. Day silent cap: once the day's silent budget is spent, no further *silent* round is
        #    released. Only the "free to stay silent" case reaches here.
        return state, "skip", C.REASON_SILENT_CAP

    if now < state.deadline:
        return state, "skip", C.REASON_DND_RESAMPLE if resampled else C.REASON_WINDOW_OPEN

    state.deadline = float(now) + float(cfg.min_idle_seconds)
    state.pending = {"released_at": float(now), "session_id": str(session_id)}
    return state, "allow", C.REASON_WINDOW_DUE


def commit_release(
    state: SessionState,
    cfg: _config.SessionConfig,
    now: float,
    rng: Any,
    *,
    visible: bool = True,
) -> Tuple[SessionState, bool]:
    """THE counting anchor: the round passed the delivery gate, so it counts and re-samples.

    Returns ``(state, committed)``; ``committed`` is False when there was no ticket to commit
    (a turn this plugin never released, or a ticket already retired). Counters move here and
    nowhere else.
    """
    pending = state.pending
    if not pending:
        return state, False
    released_at = pending.get("released_at")
    if isinstance(released_at, bool) or not isinstance(released_at, (int, float)):
        state.pending = None
        return state, False
    released_at = float(released_at)
    rollover_day(state, cfg, now)
    state.attempt_count += 1
    state.attempt_since_user += 1
    if visible:
        state.visible_count += 1
        # A visible round advances the since-user tally that drives the forced-visible decision.
        # A silent round deliberately leaves it unchanged.
        state.visible_since_user += 1
    else:
        # A settled silent round spends one unit of the day's silent budget; a visible round leaves
        # it alone (only a real user message or the day rollover clears it).
        state.silent_count += 1
    state.last_release_at = released_at
    state.anchor_at = released_at
    state.deadline = sample_deadline(released_at, cfg, rng)
    state.pending = None
    return state, True


def delivery_recheck(
    state: SessionState,
    cfg: _config.SessionConfig,
    now: float,
    released_at: float,
) -> str:
    """The delivery-gate re-check: ``""`` means deliver, else the silence reason.

    Covers exactly what happened *during* the released turn: a newer user message, DND, pause.
    It does not cover messages that were already queued before the release.
    """
    if state.status != C.STATUS_ACTIVE:
        return C.REASON_SILENT_PAUSED
    if _config.in_dnd(now, cfg):
        return C.REASON_SILENT_DND
    if state.last_user_at > released_at:
        return C.REASON_SILENT_SUPERSEDED
    return ""


def pause(state: SessionState, now: float) -> SessionState:
    """Pause: zero releases until resume; the in-flight release is retired uncounted."""
    state.status = C.STATUS_PAUSED
    state.pending = None
    return state


def resume(state: SessionState, cfg: _config.SessionConfig, now: float, rng: Any) -> SessionState:
    """Resume: re-sample the window from the user's most recent message."""
    state.status = C.STATUS_ACTIVE
    state.pending = None
    state.dnd_hold = False
    anchor = state.last_user_at if state.last_user_at > 0 else float(now)
    state.anchor_at = anchor
    state.deadline = sample_deadline(anchor, cfg, rng)
    return state


# ── the proactive turn's tool deny list ───────────────────────────────────────────────────────
# A proactive turn uses the host's **normal** tool set minus an explicit deny list, not a
# read-only cap. This module owns only the *decision* ("is this
# tool name on the list?"); the list itself is a plugin setting read by ``proactive.trigger`` and
# the enforcement is the ``pre_tool_call`` brake in ``proactive.runtime``. A name matches when it is
# equal to an entry, or when an entry ends with ``*`` and the name starts with that prefix — so an
# operator can deny one tool (``payment_charge``) or a family (``payment_*``). Matching is
# case-sensitive and on the tool *name*, never on the call's arguments (which are not ours to read).

