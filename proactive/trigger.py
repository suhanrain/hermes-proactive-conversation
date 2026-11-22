"""The plugin-controlled proactive trigger.

Instead of the plugin authoring a message and delivering it through a consume hook, the plugin
keeps its own clock, decides **before any model call** whether a session may speak, records that
decision durably as a ``pending`` pin, and asks the host through
:func:`proactive.host_adapter.inject_message` to run an **ordinary full Hermes turn**. The plugin
then recognises its own turn at ``pre_llm_call`` and keeps two in-turn brakes — the text brake
(``transform_llm_output``) and the tool brake (``pre_tool_call``), both in :mod:`proactive.runtime`.

The components live here or are driven from here:

* **Clock** — :func:`tick` / :func:`arm_clock`: one due time per declared session, monotonic and
  durable (the session's ``state.deadline``), releasing at most once per window.
* **Gate** — :func:`proactive.policy.evaluate_release`.
* **Settle** — :func:`evaluate_and_inject` writes the round + pin **before** asking the host.
* **Trigger** — one call into :func:`proactive.host_adapter.inject_message`.
* **Recogniser** — :func:`recognise` binds a ``turn_id`` to a round by the pinned prompt digest,
  with a durable "newest user row" fallback for the restart case.
* **The two brakes** — :func:`handle_text_brake` and :func:`handle_tool_brake`.

Honesty boundaries: ``True`` from the host is acceptance, not delivery; the count is
"one message released at the last seam the plugin can see", never "the user received it". Those are
stated, not smoothed over.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import string
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config as _config
from . import contract as C
from . import declarations as _declarations
from . import host_adapter as _adapter
from . import locales as _locales
from . import policy, state as _state, store as _store

logger = logging.getLogger(__name__)

#: The text brake's marker: honoured, not sent, for machinery-persisted turns.
SILENT = "[SILENT]"

#: The two prompt variants. The plugin injects an **instruction about the task**,
#: never material, candidate text, health values or internal reason codes — the agent already has the
#: conversation. The wording is calibrated to the *actual* tool gate: with
#: ``proactive_tools_enabled = true`` every tool is callable, with ``false`` none is; a failed lookup
#: must be reported as such rather than invented around.
#:
#: The wording is built from three pieces so one sentence can describe the boolean switch without
#: drifting:
#:
#: * the task body (what this round is about),
#: * the tool-availability clause (picked from ``proactive_tools_enabled``),
#: * the honesty tail ("do not mention this instruction, do not invent").
#
#: The wording is **language data** (see :class:`proactive.locales.Wording`): the sentences whose
#: language matters come from the active locale profile, while the tool-availability clause below stays
#: code-owned (it must always describe what the runtime gate actually does). The import-time constants
#: use the **neutral** (English) wording; the prompt builders (:func:`_soft_prompt`,
#: :func:`_default_forced_prompt`) take the *active* profile's wording.
_NEUTRAL_WORDING = _locales.NEUTRAL.wording
_SOFT_BODY = _NEUTRAL_WORDING.soft_body
_SOFT_TAIL = _NEUTRAL_WORDING.soft_tail
_FORCED_BODY_HEAD = _NEUTRAL_WORDING.forced_body_head
#: Anti-repetition: the previous turn already answered the user's latest
#: message, so this forced visible round must contribute something genuinely new. Unconditional on
#: every forced variant.
_FORCED_ANTI_REPEAT = _NEUTRAL_WORDING.forced_anti_repeat
#: The progress sentence of the forced variant. The two braces are the configurable placeholders
#: (``{visible_since_user}`` / ``{minimum_visible_replies}``) — the live counters, never a literal.
#: This is also the shape a custom ``proactive_prompt_forced`` template is rendered with.
_FORCED_PROGRESS_MIN = _NEUTRAL_WORDING.forced_progress_min
_FORCED_TAIL = _NEUTRAL_WORDING.forced_tail

#: The tool-availability clause per value of ``proactive_tools_enabled``. This is **code-owned**,
#: not configurable: it must always describe what the runtime gate actually does, otherwise the model
#: is told about tools it does not have.
_TOOL_CLAUSE_SOFT = {
    True: (
        "You may use any of your tools this round (all tools are allowed). "
    ),
    False: (
        "No tools are available in this round — do not call any. "
    ),
}
_TOOL_CLAUSE_FORCED = {
    True: (
        "You may use any of your tools this round — all tools, including command-line, "
        "code-execution, browser, file-writing, payment and messaging, are allowed. If a lookup "
        "tool fails (e.g. no network), say what you can without inventing. "
    ),
    False: (
        "No tools are available in this round — do not attempt to call any. "
    ),
}

#: Built-in defaults (what an absent / unusable setting means). Always usable.
PROMPT_WITH_TOOLS = _SOFT_BODY + _TOOL_CLAUSE_SOFT[True] + _SOFT_TAIL
PROMPT_NO_TOOLS = _SOFT_BODY + _TOOL_CLAUSE_SOFT[False] + _SOFT_TAIL
#: The complete default forced template (the ``minimum > 0`` variant, tools enabled). A custom
#: ``proactive_prompt_forced`` replaces this whole string; its two placeholders are rendered with the
#: live ``visible_since_user`` and the configured ``minimum_visible_replies``.
DEFAULT_PROMPT_FORCED = (
    _FORCED_BODY_HEAD + _FORCED_ANTI_REPEAT + _FORCED_PROGRESS_MIN
    + _TOOL_CLAUSE_FORCED[True] + _FORCED_TAIL
)


def _tool_clause(access: bool, *, forced: bool = False) -> str:
    """The code-owned tool clause for the effective boolean."""
    table = _TOOL_CLAUSE_FORCED if forced else _TOOL_CLAUSE_SOFT
    return table[True if access else False]


def _active_wording() -> _locales.Wording:
    """The active locale profile's prompt wording (neutral ⇒ the English default)."""
    return _locales.active_profile().wording


def _soft_prompt(access: bool, wording: "_locales.Wording | None" = None) -> str:
    """The ordinary (model-may-stay-silent) proactive prompt for *access*."""
    w = wording or _active_wording()
    return w.soft_body + _tool_clause(access) + w.soft_tail


def _default_forced_prompt(
    visible_since_user: int,
    minimum_visible_replies: int,
    *,
    access: bool,
    wording: "_locales.Wording | None" = None,
) -> str:
    """The built-in forced-visible prompt (no configurable template in play).

    A forced-visible round always has a positive ``window_min_visible`` (that is what makes it
    forced), so the progress sentence quotes the live ``visible_since_user`` against it. The
    sentences themselves come from the active locale profile (*wording*); the tool clause stays
    code-owned.
    """
    w = wording or _active_wording()
    progress = w.forced_progress_min.format(
        visible_since_user=visible_since_user,
        minimum_visible_replies=minimum_visible_replies,
    )
    clause = _tool_clause(access, forced=True)
    return w.forced_body_head + w.forced_anti_repeat + progress + clause + w.forced_tail


#: The **forced-visible** variant is built by :func:`forced_visible_prompt`
#: from :data:`DEFAULT_PROMPT_FORCED` (or the operator's ``proactive_prompt_forced`` template): while
#: the session's ``visible_since_user`` is below ``minimum_visible_replies`` the round must produce a
#: body the user can read, silence is not allowed, and the silence markers are refused. Both the
#: threshold and the progress come from configuration and the live counters — no literal is baked in.


def needs_forced_visible(visible_since_user: int, minimum_visible_replies: int) -> bool:
    """Whether this round must yield a visible body: a positive ``window_min_visible`` not yet met
    **since the last user message**.

    ``minimum_visible_replies == 0`` means silence is allowed, so this is ``False`` for every count.
    The comparison is entirely against the configured threshold and the session's own
    ``visible_since_user`` tally — never the calendar-day ``visible_count``, and never a hard-coded
    ``1``. Because a real user message clears ``visible_since_user``, the round that follows a fresh
    message is forced again even if the day already produced a visible reply.
    """
    return (
        int(minimum_visible_replies) > 0
        and int(visible_since_user) < int(minimum_visible_replies)
    )


def _effective_tools(allow_tools: Optional[bool]) -> bool:
    """The boolean tool availability a prompt is written for.

    ``None`` → the configured ``proactive_tools_enabled``; ``False`` → no tools at all; ``True`` →
    the configured value, because an explicit "tools on" must never promise tools the gate refuses
    when the operator turned them off.
    """
    enabled = tools_enabled()
    if allow_tools is None:
        return enabled
    return bool(allow_tools) and enabled


def _render_template(template: str, visible: int, minimum: int, access: bool) -> str:
    """Render one configured template with the live values; raises on a malformed template."""
    text = template.format(visible_since_user=visible, minimum_visible_replies=minimum)
    if not text.strip():
        raise ValueError("empty-rendered-prompt")
    if not access:
        # Never let a template claim tools a tools-disabled round does not have.
        text = text.rstrip() + " (No tools are available in this round.)"
    return text


def _configured_template(setting_key: str) -> Optional[str]:
    """A validated operator template, or ``None`` when absent or unusable (warned; default applies).

    Unusable = not a string, empty/blank, a field name outside :data:`contract.PROMPT_PLACEHOLDERS`,
    or a template ``str.format`` cannot even parse (a stray ``{``). Every one of those falls back to
    the built-in default — the plugin never sends an empty or broken prompt and never crashes.
    """
    raw = _adapter.config(setting_key, None)
    if raw is None:
        return None
    if not isinstance(raw, str):
        logger.warning("proactive-conversation: plugin setting %s is not a string (%s); using the built-in "
                       "default prompt", setting_key, type(raw).__name__)
        return None
    if not raw.strip():
        logger.warning("proactive-conversation: plugin setting %s is empty; using the built-in default "
                       "prompt", setting_key)
        return None
    try:
        fields = {name for _, name, _, _ in string.Formatter().parse(raw) if name}
    except ValueError as exc:
        logger.warning("proactive-conversation: plugin setting %s is not a valid format template (%s); "
                       "using the built-in default prompt", setting_key, type(exc).__name__)
        return None
    unknown = fields - set(C.PROMPT_PLACEHOLDERS)
    if unknown:
        logger.warning("proactive-conversation: plugin setting %s carries unknown placeholder(s) %s; using "
                       "the built-in default prompt", setting_key, sorted(unknown))
        return None
    return raw


def forced_visible_prompt(
    visible_since_user: int,
    minimum_visible_replies: int,
    *,
    allow_tools: Optional[bool] = None,
) -> str:
    """The prompt variant that forces a user-visible body while below the configured minimum.

    It (a) requires a readable reply this turn, (b) forbids ``[SILENT]`` / ``NO_REPLY`` / an empty
    body, (c) describes the **actual** tool access of the round (see
    :data:`_TOOL_CLAUSE_FORCED`), and (d) tells the model it has **already** answered the user's
    latest message in the previous turn, so this round must contribute something genuinely new
    rather than repeating or paraphrasing (the anti-repetition guard is prompt-only; the must-speak
    semantics are unchanged). The counts quoted are the live ``visible_since_user``
    and the configured ``minimum_visible_replies``; nothing is hard-coded.

    The wording is the operator's ``proactive_prompt_forced`` template when it is present and
    usable, otherwise :data:`DEFAULT_PROMPT_FORCED`. ``allow_tools=None``
    follows ``proactive_tools_enabled``; ``False`` writes the no-tools wording.
    """
    visible = int(visible_since_user)
    minimum = int(minimum_visible_replies)
    access = _effective_tools(allow_tools)
    template = _configured_template(C.SETTING_PROMPT_FORCED)
    if template is not None:
        try:
            return _render_template(template, visible, minimum, access)
        except Exception as exc:  # a broken template falls back, never reaches the model
            logger.warning("proactive-conversation: plugin setting %s could not be rendered (%s); using the "
                           "built-in default prompt", C.SETTING_PROMPT_FORCED, type(exc).__name__)
    return _default_forced_prompt(visible, minimum, access=access)


def proactive_prompt(state: Any, cfg: Any, *, allow_tools: Optional[bool] = None) -> str:
    """Choose the proactive-turn prompt for *state* under *cfg*.

    Below the configured ``minimum_visible_replies`` — measured on ``visible_since_user``, the
    visible rounds since the last real user message — it returns
    :func:`forced_visible_prompt`; otherwise the ordinary variant (the model freely decides whether
    to stay silent), rendered from the operator's ``proactive_prompt_soft`` template when present
    and usable, else the built-in default. A minimum of ``0`` never forces.
    ``allow_tools`` selects the wording: ``None`` follows ``proactive_tools_enabled``, ``False`` is
    the no-tools variant.
    """
    minimum = int(getattr(cfg, "window_min_visible", 0) or 0)
    visible = int(getattr(state, "visible_since_user", 0) or 0)
    access = _effective_tools(allow_tools)
    if needs_forced_visible(visible, minimum):
        return forced_visible_prompt(visible, minimum, allow_tools=allow_tools)
    template = _configured_template(C.SETTING_PROMPT_SOFT)
    if template is not None:
        try:
            return _render_template(template, visible, minimum, access)
        except Exception as exc:  # a broken template falls back, never reaches the model
            logger.warning("proactive-conversation: plugin setting %s could not be rendered (%s); using the "
                           "built-in default prompt", C.SETTING_PROMPT_SOFT, type(exc).__name__)
    return _soft_prompt(access)


#: Plugin-level settings (read through the host adapter on every call, never cached).
#: The keys are the contract's, so the manifest and this module cannot drift.
SETTING_TRIGGER_MODE = C.SETTING_TRIGGER_MODE
SETTING_TOOLS_ENABLED = C.SETTING_TOOLS_ENABLED
TRIGGER_MODE_ON = "on"
TRIGGER_MODE_OFF = "off"
TRIGGER_MODES = (TRIGGER_MODE_ON, TRIGGER_MODE_OFF)

#: Default pin lifetime (seconds). An unrecognised pin older than this is a *lost* round; the same
#: value also bounds the durable-fallback window. Read from the plugin-level ``pending_release_ttl``
#: family; a code default keeps the module usable without a bound context.
PIN_TTL_DEFAULT_SECONDS = 600.0

#: How the clock waits between scans when armed via ``ctx.spawn_task``. A code constant, not a knob.
CLOCK_INTERVAL_SECONDS = 30.0

# ── process-local recognition stores (the durable half lives in the per-session blob) ───────────

#: ``session_id -> pin dict`` for rounds this process settled and the host accepted, not yet bound to
#: a turn. Lost on restart — which is exactly why the durable pin (in the blob) exists.
_pins: Dict[str, Dict[str, Any]] = {}

#: ``turn_id -> bound dict`` for turns the plugin has recognised as its own this process.
_turns: Dict[str, Dict[str, Any]] = {}

#: ``turn_id -> number of allowed tools that ran this turn`` for our bound turns. A tool that ran
#: has (or will have) a host tool-progress entry; a turn that ends with no visible text after one
#: must end in a defined silence rather than an empty turn (no dangling progress).
_tool_used: Dict[str, int] = {}

_clock_armed = False

#: The spawned clock-loop task when the host returns a probeable object (``ctx.spawn_task`` returns
#: an ``asyncio.Task``). Lets us re-arm after the host cancels the loop (unload / force reload)
#: instead of trusting a one-way boolean that never resets — otherwise proactive firing dies for the
#: rest of the process the first time the task is retired.
_clock_task: Any = None

#: ``time.monotonic()`` of the loop's last iteration. A task object can look alive (``done()`` False)
#: while the event loop it ran on has been replaced, so it never ticks again; a stale heartbeat is the
#: only reliable "the clock is dead" signal, so ``arm_clock`` re-spawns on staleness too.
_last_tick_ts: float = 0.0


def reset() -> None:
    """Drop the process-local recognition stores and the armed-clock flag (tests, incidents)."""
    global _clock_armed, _clock_task, _last_tick_ts
    _pins.clear()
    _turns.clear()
    _tool_used.clear()
    _clock_armed = False
    _clock_task = None
    _last_tick_ts = 0.0


# ── small helpers ────────────────────────────────────────────────────────────────────────────


def prompt_digest(text: Any) -> str:
    """The normalised digest of *text* — the exact-match key the recogniser binds on.

    Normalised the same way the transcript seam normalises (``str.strip()``), so a turn started with
    the injected prompt matches the pin regardless of leading/trailing whitespace the host may add.
    """
    return hashlib.sha256(str(text or "").strip().encode("utf-8")).hexdigest()


def trigger_mode() -> str:
    """The configured trigger mode: ``on`` | ``off``.

    Default ``off``: the plugin stays silent until an operator turns it on. An unusable value falls
    back to ``off`` and never invents a mode.
    """
    raw = _adapter.config(SETTING_TRIGGER_MODE, TRIGGER_MODE_OFF)
    if isinstance(raw, bool):
        # YAML 1.1 coerces bare ``on``/``off``/``yes``/``no`` to booleans, so a config written
        # ``trigger_mode: on`` arrives here as ``True`` — honour it instead of silently going OFF.
        return TRIGGER_MODE_ON if raw else TRIGGER_MODE_OFF
    value = str(raw).strip().lower() if isinstance(raw, str) else TRIGGER_MODE_OFF
    return value if value in TRIGGER_MODES else TRIGGER_MODE_OFF


def tools_enabled() -> bool:
    """Whether the proactive round may call tools at all: ``proactive_tools_enabled``.

    ``True`` means **every** tool is callable.
    ``False`` means **no** tool is callable.

    Fail-closed: the documented default is ``False``, so only an explicit
    boolean ``true`` opens tools. An absent key or an explicit ``null`` returns ``False``; an
    unusable value (anything that is not a real boolean: a string, a number, ...) also returns
    ``False`` with a WARNING.

    Read through the host adapter on every call, so an operator edit applies to the
    next round with no reload.
    """
    raw = _adapter.config(SETTING_TOOLS_ENABLED, C.TOOLS_ENABLED_DEFAULT)
    if raw is None:  # an absent key or an explicit null is "not enabled" (fail closed)
        return C.TOOLS_ENABLED_DEFAULT
    if isinstance(raw, bool):
        return raw
    logger.warning("proactive-conversation: plugin setting %s=%r is not a boolean; treating it as the "
                   "default %r (no tools)", SETTING_TOOLS_ENABLED, raw, C.TOOLS_ENABLED_DEFAULT)
    return C.TOOLS_ENABLED_DEFAULT


def pin_ttl() -> float:
    """The effective pin lifetime in seconds (a positive number; else the documented default)."""
    raw = _adapter.config(C.SETTING_PENDING_RELEASE_TTL_SECONDS, PIN_TTL_DEFAULT_SECONDS)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return PIN_TTL_DEFAULT_SECONDS
    value = float(raw)
    return value if value > 0.0 else PIN_TTL_DEFAULT_SECONDS


def _now() -> float:
    import time

    return time.time()


# ── settle + inject (the gate + trigger, one round) ──────────────────────────────────────────


@dataclass(frozen=True)
class InjectOutcome:
    """What one evaluated round did. ``accepted`` is the host boolean, never a delivery claim.

    ``reason`` is ``""`` on ``accepted``; otherwise a contract/trigger code (an ``off:*``/``cap:*``/
    ``window:*`` gate reason, ``inject:refused`` for a ``False``, ``off:write-failed`` when the pin
    could not be written). ``prompt_digest``/``round_marker`` identify the pin for a settled round.
    """

    accepted: bool
    reason: str = ""
    prompt_digest: str = ""
    round_marker: str = ""


def _hot_reload_config(read: "_store.SessionRead", session_key: str,
                       session_id: str) -> "_store.SessionRead":
    """Pick up a live edit to this session's channel declaration at decision time.

    The clock tick reads the **mirrored** blob, which only an inbound hook refreshes; without this a
    caps / idle-window change in ``config.yaml`` would not reach a session firing from the clock until
    the next real user message. We compare the *current* declaration's raw config with the mirrored
    one and — if it changed, parses, and keeps the timezone — swap ``config``/``config_raw`` while
    leaving every counter in ``read.state`` untouched. A no-op fast path (content hash equal) costs
    one declaration read and no parse. An invalid edit or a timezone change keeps the old config,
    exactly like the mirror path (:func:`proactive.config.reconcile_config`).
    """
    lookup = _declarations.lookup_setting(session_key, session_id)
    if not lookup.usable:
        return read
    result = _config.reconcile_config(read.config, read.config_raw, lookup.declaration.config_raw)
    if result.kind == "unchanged":
        return read
    _config.log_reload(result, read.state)
    if result.kind == "adopted":
        # Same state (counters) — only the gate is replaced. The next write persists the new
        # config_raw into the blob, so subsequent ticks take the unchanged fast path.
        return replace(read, config=result.config, config_raw=result.config_raw)
    return read


def evaluate_and_inject(
    store_handle: Any,
    *,
    session_key: Any,
    session_id: Any,
    dims: Dict[str, Any],
    now: float,
    inject: Optional[Callable[..., bool]] = None,
    rng: Any = None,
    prompt: Optional[str] = None,
    ttl: Optional[float] = None,
) -> InjectOutcome:
    """Evaluate one session's gate, and if allowed settle the pin durably then ask the host.

    Order: the gate and the write happen **before** ``inject``, so a refused round costs zero model
    calls and zero tool calls. On ``False`` from the host the pin is retired and nothing is counted —
    the plugin has not sent anything.
    """
    skey = str(session_key or "")
    sid = str(session_id or "")
    read = _store.read(store_handle, skey, dims)
    if not read.usable:
        return InjectOutcome(False, read.reason or C.REASON_NO_BLOB)
    # A live edit to the channel declaration applies on this tick, without a restart. The
    # counters are kept; only the gate may change.
    read = _hot_reload_config(read, skey, sid)

    state, cfg = read.state, read.config
    state, action, reason = policy.evaluate_release(
        state, cfg, now, rng, session_id=sid, pending_ttl=(ttl if ttl is not None else pin_ttl()))
    if action != "allow":
        # Persist the (possibly re-sampled) window so the due time stays monotonic and durable.
        _store.write(store_handle, skey, config_raw=read.config_raw, state=state, dims=dims,
                     origin=read.origin)
        return InjectOutcome(False, reason)

    text = prompt if prompt is not None else proactive_prompt(state, cfg)
    digest = prompt_digest(text)
    marker = uuid.uuid4().hex
    released_at = float(state.pending.get("released_at")) if state.pending else float(now)
    pin_deadline = float(now) + float(ttl if ttl is not None else pin_ttl())
    state.pending = {
        # keep the ticket shape (released_at/session_id) so the same slot is reusable
        "released_at": released_at,
        "session_id": sid,
        # the four pin fields (state.PIN_FIELDS)
        "prompt_digest": digest,
        "injected_at": None,
        "round_marker": marker,
        "deadline": pin_deadline,
    }
    if not _store.write(store_handle, skey, config_raw=read.config_raw, state=state, dims=dims,
                        origin=read.origin):
        return InjectOutcome(False, C.REASON_WRITE_FAILED, digest, marker)

    pin = {
        "session_key": skey,
        "session_id": sid,
        "dims": dict(dims),
        "store": store_handle,
        "origin": read.origin,
        "config_raw": read.config_raw,
        "prompt_digest": digest,
        "round_marker": marker,
        "released_at": released_at,
        "deadline": pin_deadline,
    }

    accepted = _do_inject(inject, text, skey)

    if not accepted:
        _retire_pin(store_handle, skey, dims, round_marker=marker)
        logger.warning(
            "proactive-conversation: injection refused for %s (reason=%s); the round is not counted",
            _store.short_digest(skey), "inject:refused")
        return InjectOutcome(False, "inject:refused", digest, marker)

    _pins[sid] = pin
    # Record the acceptance time on the pin for a faithful "injected_at" (best effort; the pin is
    # already durable and the recognition only needs the digest).
    _record_injected_at(store_handle, skey, dims, marker, now)
    return InjectOutcome(True, "", digest, marker)


def _do_inject(inject: Optional[Callable[..., bool]], text: str, session_key: str) -> bool:
    if inject is not None:
        try:
            return bool(inject(text, session_key=session_key))
        except Exception as exc:  # a refusing seam is a refusal, never a crash
            logger.warning("proactive-conversation: the inject seam raised (%s); treating the round as "
                           "refused", type(exc).__name__)
            return False
    return _adapter.inject_message(text, session_key=session_key)


def _record_injected_at(store_handle: Any, session_key: str, dims: Dict[str, Any],
                        marker: str, injected_at: float) -> None:
    read = _store.read(store_handle, session_key, dims)
    if not read.usable or not isinstance(read.state.pending, dict):
        return
    if read.state.pending.get("round_marker") != marker:
        return
    read.state.pending["injected_at"] = float(injected_at)
    _store.write(store_handle, session_key, config_raw=read.config_raw, state=read.state, dims=dims,
                 origin=read.origin)


def _retire_pin(store_handle: Any, session_key: str, dims: Dict[str, Any], *,
                round_marker: Optional[str] = None) -> None:
    """Clear the durable pin, but only if *round_marker* (when given) still owns it."""
    if not session_key:
        return
    read = _store.read(store_handle, session_key, dims)
    if not read.usable or not isinstance(read.state.pending, dict):
        return
    if round_marker is not None and read.state.pending.get("round_marker") not in (None, round_marker):
        return  # a newer round owns the pin; leave it alone
    new_state = read.state.copy()
    new_state.pending = None
    _store.write(store_handle, session_key, config_raw=read.config_raw, state=new_state, dims=dims,
                 origin=read.origin)


# ── the clock ────────────────────────────────────────────────────────────────────────────────


#: Routine per-scan skip reasons stay at DEBUG so a 30s loop does not flood the log; every other
#: reason (caps, DND, pause, off) is INFO. Reason codes only — no session content, no values.
_SKIP_LOG_LEVEL = {
    C.REASON_WINDOW_OPEN: logging.DEBUG,
    C.REASON_DND_RESAMPLE: logging.DEBUG,
    # The silent-round budget being spent is an operator-relevant brake, not routine loop noise:
    # it must show up in the per-scan log at INFO.
    C.REASON_SILENT_CAP: logging.INFO,
}


def _log_outcome(session_key: str, outcome: "InjectOutcome") -> None:
    """Emit one privacy-safe line per scanned session (digest + reason code only)."""
    digest = _store.short_digest(session_key)
    if outcome.accepted:
        logger.info("proactive-conversation: proactive round released for %s", digest)
        return
    level = _SKIP_LOG_LEVEL.get(outcome.reason, logging.INFO)
    logger.log(level, "proactive-conversation: proactive round skipped for %s (reason=%s)",
               digest, outcome.reason or "unknown")


def clock_status() -> Dict[str, Any]:
    """A process-local, content-free snapshot of the trigger clock's liveness (diagnostics/tests).

    Distinguishes the three states a caller cares about: ``not-running`` (never armed, or reset),
    ``ticking`` (a live loop heartbeated within a few intervals), and ``stale`` (the loop object
    looks alive but its heartbeat is old — a replaced event loop). This is what lets an operator or a
    test tell "the clock is not running" apart from "the clock ran and found nothing to do".
    """
    now_m = time.monotonic()
    task = _clock_task
    task_alive = None
    if task is not None:
        try:
            task_alive = not bool(task.done())
        except Exception:  # noqa: BLE001 - a non-probeable handle is judged by its heartbeat
            task_alive = None
    age = (now_m - _last_tick_ts) if _last_tick_ts else None
    stale_after = max(CLOCK_INTERVAL_SECONDS * 4.0, 120.0)
    stale = age is not None and age > stale_after
    if not _clock_armed and task is None:
        state = "not-running"
    elif stale:
        state = "stale"
    else:
        state = "ticking"
    return {"state": state, "armed": _clock_armed, "task_alive": task_alive,
            "last_tick_age": age}


def tick(
    store_handle: Any,
    sessions: List[Dict[str, Any]],
    now: float,
    *,
    inject: Optional[Callable[..., bool]] = None,
    rng: Any = None,
    prompt: Optional[str] = None,
    ttl: Optional[float] = None,
) -> List[Tuple[str, InjectOutcome]]:
    """One clock scan over *sessions* (each a dict with session_key/session_id/dims).

    Returns ``[(session_key, InjectOutcome), ...]`` in input order. A session whose gate refuses is
    not injected (its outcome reason is the gate reason). This is the deterministic core the
    ``spawn_task`` loop and the tests both drive; the loop itself only decides *when* to call it.
    """
    outcomes: List[Tuple[str, InjectOutcome]] = []
    for entry in sessions:
        skey = entry.get("session_key")
        outcome = evaluate_and_inject(
            store_handle, session_key=skey, session_id=entry.get("session_id"),
            dims=entry.get("dims") or {}, now=now, inject=inject, rng=rng, prompt=prompt, ttl=ttl)
        _log_outcome(str(skey), outcome)
        outcomes.append((str(skey), outcome))
    return outcomes


async def _tick_loop(
    store_provider: Callable[[], Tuple[Any, List[Dict[str, Any]]]],
    interval: float = CLOCK_INTERVAL_SECONDS,
) -> None:
    """The ``spawn_task`` clock: scan every *interval* seconds; return quietly when cancelled.

    ``store_provider`` returns ``(store_handle, sessions)`` afresh each wake (the plugin never caches
    configuration). A scan that raises is logged and survived — a broken provider must not kill the
    clock, but it also must not pretend a round happened. Cleanup is ownership-protected: an old loop
    finishing after re-arm cannot clear the replacement's global state.
    """
    owner = asyncio.current_task()
    while True:
        try:
            await asyncio.sleep(float(interval))
            global _last_tick_ts
            _last_tick_ts = time.monotonic()  # heartbeat: this loop is alive this iteration
            store_handle, sessions = store_provider()
            tick(store_handle, sessions, _now(), rng=_default_rng())
        except asyncio.CancelledError:  # unload / force reload: not an error
            # A stale replacement can be installed before the loop observes cancellation. Only
            # the current owner may clear the global arm; an obsolete loop must not retire its
            # replacement.
            global _clock_armed, _clock_task
            if _clock_task is owner:
                _clock_armed = False
                _clock_task = None
            return
        except Exception as exc:  # a failed scan is skipped, not retried into the ground
            logger.warning("proactive-conversation: a trigger clock scan failed (%s); skipping it",
                           type(exc).__name__)


def arm_clock(store_provider: Callable[[], Tuple[Any, List[Dict[str, Any]]]],
              interval: float = CLOCK_INTERVAL_SECONDS) -> bool:
    """Arm the clock on the host loop via ``ctx.spawn_task``; ``False`` when no loop is available.

    The ``spawn_task`` coroutine is preferred; when the context has no ``spawn_task``
    (a CLI-side load, a recording context) this returns ``False`` and the caller may fall back to a
    first-hook-call arm. The task is cancelled on unload by the host.
    """
    global _clock_armed, _clock_task, _last_tick_ts
    # Re-arm if a previous loop task died **or merely stopped ticking**. The host cancels
    # ``ctx.spawn_task`` tasks on unload/force reload, and a task can also look alive (``done()``
    # False) sitting on an event loop that has since been replaced — it then never ticks again. Only
    # a fresh heartbeat proves liveness: re-spawn when the task is done, or the heartbeat is older
    # than a few intervals. Otherwise proactive firing dies for the rest of the process until a
    # manual restart.
    now_m = time.monotonic()
    stale_after = max(float(interval) * 4.0, 120.0)
    if _clock_task is not None:
        try:
            finished = bool(_clock_task.done())
        except Exception:  # noqa: BLE001 - a non-probeable handle is judged by its heartbeat
            finished = False
        if not finished and (now_m - _last_tick_ts) < stale_after:
            return True
        if not finished:
            # Retire a stale task before spawning its replacement so two scanners cannot run.
            try:
                _clock_task.cancel()
            except Exception as exc:  # pragma: no cover - defensive host task seam
                logger.warning("proactive-conversation: could not cancel stale trigger clock (%s)", type(exc).__name__)
        _clock_task = None
    elif _clock_armed and (now_m - _last_tick_ts) < stale_after:
        return True
    spawn = getattr(_adapter.context(), "spawn_task", None)
    if not callable(spawn):
        return False
    try:
        _clock_task = spawn(_tick_loop(store_provider, interval))
    except Exception as exc:  # pragma: no cover - a host that cannot spawn is not a crash here
        logger.warning("proactive-conversation: could not arm the trigger clock (%s)", type(exc).__name__)
        return False
    _last_tick_ts = now_m  # assume the fresh loop will heartbeat; refreshed each iteration
    _clock_armed = True
    return True


# ── the recogniser (pre_llm_call) ────────────────────────────────────────────────────────────


def recognise(
    session_id: Any,
    turn_id: Any,
    user_message: Any,
    *,
    store_handle: Any = None,
    declarations: Optional[List[Tuple[str, str, Dict[str, Any]]]] = None,
    db_path: Any = None,
    limit: Any = None,
) -> bool:
    """Bind *turn_id* to a round when *user_message* is the injected prompt; ``True`` if ours.

    Two rules, in order:

    1. **process-local pin**: an armed pin for *session_id* whose ``prompt_digest`` equals the digest
       of *user_message* (normalised with ``str.strip()``). This is the live case.
    2. **durable fallback (restart)**: no in-process pin, so an optional *declarations* list (of
       ``(session_key, session_id, dims)``) is scanned for a blob whose pin covers this
       ``session_id``, and the digest is compared with the **newest user row** of the transcript seam.
       A window with no user row, or an unreadable transcript, answers "not ours".

    Neither rule is ever "brake every turn": an unrecognised turn is left entirely to the host, and a
    warning records that a round could not be recognised (the chosen failure direction).
    """
    sid = str(session_id or "")
    tid = str(turn_id or "")
    if not sid or not tid:
        return False
    if tid in _turns:
        return True

    digest = prompt_digest(user_message)
    pin = _pins.get(sid)
    if pin is not None and pin.get("prompt_digest") == digest:
        _turns[tid] = dict(pin, turn_id=tid)
        return True

    bound = _durable_recognise(sid, digest, store_handle, declarations, db_path, limit)
    if bound is not None:
        _turns[tid] = dict(bound, turn_id=tid)
        return True

    # Only worth a warning when we actually expected a round to be recogniseable: a session with an
    # armed pin, or a supplied durable store. Otherwise this is an ordinary user turn and saying
    # nothing is correct (it would drown every conversation in warnings).
    if pin is not None or store_handle is not None:
        logger.warning(
            "proactive-conversation: a turn for %s could not be recognised as a proactive round (digest "
            "mismatch, no pin, or unreadable transcript); leaving it to the host",
            _store.short_digest(sid))
    return False


def _durable_recognise(sid: str, digest: str, store_handle: Any, declarations: Any,
                       db_path: Any, limit: Any) -> Optional[Dict[str, Any]]:
    if store_handle is None or not declarations:
        return None
    for decl in declarations:
        try:
            skey, ssid, dims = decl
        except Exception:
            continue
        if str(ssid or "") != sid:
            continue
        read = _store.read(store_handle, str(skey), dims or {})
        if not read.usable or not _state.is_pin(read.state.pending):
            continue
        pin_pending = read.state.pending
        pin_digest = pin_pending.get("prompt_digest")
        if pin_digest != digest:
            # The user_message in a replay should equal the injected prompt; if it does not, still
            # apply the newest-user-row rule below rather than trusting the message text alone.
            pass
        newest = _newest_user_row(sid, db_path, limit)
        if newest is None:
            continue  # unreadable / empty window: cannot confirm, so "not ours"
        if prompt_digest(newest) == pin_digest:
            return {
                "session_key": str(skey),
                "session_id": sid,
                "dims": dict(dims or {}),
                "store": store_handle,
                "origin": read.origin,
                "config_raw": read.config_raw,
                "prompt_digest": pin_digest,
                "round_marker": pin_pending.get("round_marker"),
                "released_at": pin_pending.get("released_at"),
                "deadline": pin_pending.get("deadline"),
            }
    return None


def _newest_user_row(session_id: str, db_path: Any, limit: Any) -> Optional[str]:
    """The newest ``user`` row's content inside the transcript window, or ``None``."""
    rounds = 10 if limit is None else limit
    rows = _adapter.recent_transcript(session_id, rounds, db_path=db_path)
    for role, content in reversed(rows):
        if str(role) == "user":
            text = str(content or "").strip()
            if text:
                return text
    return None


def is_ours(turn_id: Any) -> bool:
    """Whether *turn_id* is currently bound to a proactive round (diagnostics/tests)."""
    return str(turn_id or "") in _turns


# ── brake 1: the text brake (transform_llm_output) ──────────────────────────────────────────


def handle_text_brake(turn_id: Any, response_text: Any, now: float, rng: Any = None
                      ) -> Tuple[bool, Optional[str]]:
    """Decide the text of a recognised round; ``(handled, result)``.

    ``handled`` is ``False`` when this ``turn_id`` is not ours (the caller then leaves the turn to the
    host). When it is ours:

    * **no text** — nothing to brake and nothing to count; the pin is retired (a text-less turn is
      neither braked nor counted), result ``None``;
    * **policy forbids the round** (paused / DND / a newer user message) — result ``SILENT``,
      the pin is retired, and nothing is counted (a silenced round is a skip, not a refund);
    * **still allowed** — settle **once** (``commit_release`` moves the counters), result ``None`` so
      the host's text is delivered.

    One settlement per ``turn_id`` because the binding is popped on the first call (the host fires
    ``transform_llm_output`` once per turn; the pop makes the count test prove that).
    """
    tid = str(turn_id or "")
    binding = _turns.pop(tid, None)
    if binding is None:
        return False, None
    tools_used = _tool_used.pop(tid, 0)

    if not str(response_text or "").strip():
        _settle_nonvisible(binding, now)
        _retire_binding(binding)
        digest = _store.short_digest(binding.get("session_id") or "")
        if tools_used:
            # A tool progressed in the host UI but the turn produced no visible body. Returning
            # an empty turn would leave that progress dangling with no defined outcome, so end
            # it as an explicit silence: nothing is delivered and nothing is counted.
            logger.info("proactive-conversation: tool progress without final text for %s "
                        "(reason=tool-no-text, tools=%d)", digest, tools_used)
            return True, SILENT
        logger.info("proactive-conversation: empty response skipped for %s (reason=empty-response)",
                    digest)
        return True, None

    if _adapter.host_silence_verdict(response_text):
        _settle_nonvisible(binding, now)
        _retire_binding(binding)
        logger.info("proactive-conversation: intentional silence skipped for %s (reason=model-choice)",
                    _store.short_digest(binding.get("session_id") or ""))
        return True, SILENT

    read = _store.read(binding["store"], binding["session_key"], binding["dims"])
    if not read.usable:
        # Cannot judge the session: the other failure direction — let the text out, do not count.
        _retire_binding(binding)
        logger.warning(
            "proactive-conversation: could not read %s to brake its proactive text; letting the text out "
            "uncounted", _store.short_digest(binding.get("session_id") or ""))
        return True, None

    released_at = float(binding.get("released_at") or 0.0)
    reason = policy.delivery_recheck(read.state, read.config, now, released_at)
    if reason:
        _retire_binding(binding)
        logger.info("proactive-conversation: silencing the proactive text for %s (%s)",
                    _store.short_digest(binding.get("session_id") or ""), reason)
        return True, SILENT

    effective_rng = rng if rng is not None else _default_rng()
    new_state, committed = policy.commit_release(read.state, read.config, now, effective_rng)
    if committed:
        _store.write(binding["store"], binding["session_key"], config_raw=read.config_raw,
                     state=new_state, dims=binding["dims"], origin=read.origin)
        _retire_binding(binding, already_read=new_state)
    else:
        _retire_binding(binding)
    return True, None


def _settle_nonvisible(binding: Dict[str, Any], now: float) -> None:
    """Count an accepted proactive round without crediting a visible reply."""
    read = _store.read(binding["store"], binding["session_key"], binding["dims"])
    if not read.usable:
        return
    updated, committed = policy.commit_release(
        read.state, read.config, now, _default_rng(), visible=False)
    if committed:
        _store.write(binding["store"], binding["session_key"], config_raw=read.config_raw,
                     state=updated, dims=binding["dims"], origin=read.origin)



def _retire_binding(binding: Dict[str, Any], *, already_read: Any = None) -> None:
    """Retire a binding's durable pin (and its process-local pin) once the round is over."""
    sid = binding.get("session_id")
    marker = binding.get("round_marker")
    if sid and _pins.get(str(sid), {}).get("round_marker") == marker:
        _pins.pop(str(sid), None)
    if already_read is None:
        _retire_pin(binding.get("store"), str(binding.get("session_key") or ""),
                    binding.get("dims") or {}, round_marker=marker)


def _default_rng() -> Any:
    import random

    return random.Random()


# ── brake 2: the tool brake (pre_tool_call) ──────────────────────────────────────────────────


def handle_tool_brake(turn_id: Any, tool_name: Any) -> Optional[Dict[str, str]]:
    """The proactive tool policy for a recognised turn; ``None`` (no opinion) otherwise.

    Applies ``proactive_tools_enabled`` **only** to a turn this plugin bound:

    * ``true``  — every tool runs;
    * ``false`` — **nothing** runs; every tool is refused with ``denied:proactive-tool``.

    An unrecognised turn (a user's own turn, a sub-agent, another session) is left entirely to the
    host — neither counted nor braked. The returned directive is the host's ``pre_tool_call``
    block shape; a refused name is logged by its name and the reason only (never the arguments).
    """
    tid = str(turn_id or "")
    if tid not in _turns:
        return None
    if tools_enabled():
        name = str(tool_name or "")
        _tool_used[tid] = _tool_used.get(tid, 0) + 1  # this tool runs and shows progress
        return None
    name = str(tool_name or "")
    message = (f"proactive-conversation: no tools are available in this proactive round — the tool "
               f"'{name}' was not run (denied:proactive-tool).")
    binding = _turns.get(tid)
    if binding is not None:
        _settle_nonvisible(binding, time.time())
    logger.info("proactive-conversation: tool %s refused in a proactive round (denied:proactive-tool)", name)
    return {"action": "block", "message": message}
