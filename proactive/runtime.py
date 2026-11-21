"""The gateway hook callbacks, plus the small per-session API the control surface drives.

A heartbeat wake that the policy permits is dropped with :data:`CONSUME_LANE_RETIRED`
(``silent:consume-lane-retired``): this module neither generates a chat, settles a round nor arms a
consume ticket. A proactive round reaches the user as a normal agent turn that
``proactive/trigger.py`` opens and then brakes.

Hook contract:

* ``pre_gateway_dispatch(event, gateway, session_store)`` — runs inside the inbound path *before*
  auth and before any LLM work. A heartbeat wake arrives here as a normal (non-internal) event
  carrying ``event._heartbeat_session_id``; returning ``{"action": "skip"}`` drops the round with
  zero LLM calls and zero delivery, and the heartbeat tick is refunded so the next poll asks
  again. The hook evaluates the per-session gate and the content gates, then drops every
  permitted wake with ``silent:consume-lane-retired``.
* ``transform_llm_output(response_text, session_id, model, platform, turn_id)`` — runs once per
  turn before the final text is persisted/delivered; returning a string replaces the text. A turn
  bound by :func:`pre_llm_call` is judged by the trigger's text brake. A released round whose
  consumer did not match is silenced (``silent:consume-missed``) rather than publishing the host's
  own words. See :func:`_recheck`.
* ``pre_tool_call(tool_name, args, task_id, session_id, ...)`` — the execution-layer tool gate.
  Runs immediately before every tool execution; a ``{"action": "block", "message": ...}`` return
  becomes that call's result. It refuses non-read tools **only** inside a round this plugin
  released itself (a live release ticket for that session id) and returns ``None`` — no opinion —
  everywhere else, so an ordinary conversation is never affected. See :func:`pre_tool_call`.

The composition is optional *by construction*: ``set_content`` installs a read-only composition
(sources + tool boundary + policy). With nothing installed the hooks behave as described above.
With it installed:

* a wake that policy would release must also produce **one citable recent record** (assembly);
  when it cannot, the round is skipped with the assembly's reason code and no counter moves
  (「找不到可靠话题就静默」), unless the opener path below turns that verdict into material;
* a released wake's round uses the assembled block as the **only** material its chat is generated
  from (:func:`_generation_messages`);
* the delivery gate additionally drops a released round whose generated text pressures, blames or
  induces dependency (``silent:expression``);
* a round whose **model** answers with the gateway's own exact silence marker
  (``[SILENT]``/``SILENT``/``NO_REPLY``/``[静默]``/… — the host's list, asked at the seam, never
  copied here) is retired **uncounted** (``silent:model-choice``): the user received nothing, so the
  day's quota and the since-user counters stay where they were, and the next legitimately due window
  can open a new round. Only a round this plugin provably still owns is exempted — a user's own turn
  carries no ticket, and prose that merely mentions a marker is settled and delivered;
* a released round's tool calls run under the read-only gate (``pre_tool_call``): non-read tools are
  refused at the execution layer. This one needs no composition to be installed — it is tied to the
  release ticket, i.e. to the plugin having opened the round at all.

``None`` is the answer for every path that must let the gateway dispatch normally (an ordinary user
message, any internal error). A heartbeat wake that policy refuses returns a ``skip`` dict, and so
does a wake that passes every gate — with the consume-lane reason code, because this hook does not
release rounds itself.

Fail-open / fail-quiet:

* an unexpected error in either hook is logged and yields "no opinion" (``None``) — a broken plugin
  must not swallow traffic or silence a turn;
* a store the plugin cannot read is an *unconfigured* session: a wake is then refused (nothing is
  sent on a guess) while a user message still passes through untouched;
* the delivery gate silences only for a round *this* plugin released (a ticket in flight), so an
  ordinary user turn can never be silenced by it;
* the one deliberate exception to fail-open on that path is the settlement itself: it fails
  **closed** (no send) when the write-ahead write does not land. An unrecorded send would leave the
  release live for the next process — the duplicate window this ordering exists to close — so this
  plugin's cost direction is fixed at "at most one missed message": it never sends a round it has
  not counted and never counts the same round twice. That bound is about **this plugin's
  accounting, and nothing else**. It is *not* a claim about what reaches the recipient: every step
  after this seam (the gateway's shaping and retries, and its own delivery ledger —
  ``gateway/delivery_ledger.py`` redelivers an unconfirmed final, visibly marked as recovered)
  belongs to the host, consults no plugin again, and is not covered by this ordering. See the
  boundary note at the bottom of the write-ahead wiring end-to-end test.

Injectability: ``set_clock`` / ``set_rng`` / ``reset_seams`` are public; the pure policy in
``proactive.policy`` takes ``now`` and ``rng`` as arguments.

Plugin-level settings: ``register(ctx)`` hands this module the plugin context
(:func:`bind_plugin_context`), and the two mechanical values — ``pending_release_ttl_seconds`` and
``event_timestamp_sanity_seconds`` — are resolved from ``plugins.entries.<plugin-id>.settings`` on
every hook call, so a settings edit applies to the next call without reloading the plugin. An
absent context or key means the documented default; an unusable value means the default too,
with a warning (``proactive.settings``).
"""

from __future__ import annotations

import logging
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import config as _config
from . import composition as _composition
from . import content as _content
from . import contract as C
from . import declarations as _declarations
from . import delivery_guard as _delivery_guard
from . import host_adapter as _adapter
from . import isolation, permissions as _permissions
from . import locales as _locales
from . import policy, settings as _settings, store
from . import trigger as _trigger

logger = logging.getLogger(__name__)

#: The reason code a heartbeat wake is dropped with when it passes every gate: this plugin does not
#: deliver on that path. It is deliberately its own code rather than ``silent:off`` so an operator
#: reading the log can tell "nothing to deliver here" from "this session is not authorised".
CONSUME_LANE_RETIRED = "silent:consume-lane-retired"

# ── injectable seams ────────────────────────────────────────────────────────────────────────

_clock: Callable[[], float] = time.time
_rng: Any = random.Random()

#: The read-only content assembler, or ``None`` when no composition has been installed.
#:
#: With ``None`` an accepted wake is handed on untouched. With an assembler installed, releasing a
#: wake additionally *requires* it to produce one citable candidate — no candidate means the round
#: is skipped with the assembly's reason (and no counter moves).
_assembler: Optional[_content.Assembler] = None
_boundary: Optional[_permissions.Boundary] = None

#: In-flight release tickets, keyed by session id: {released_at, session_key, session_id, store, dims}.
#: Deliberately process-local: a ticket may only silence the turn it was created for, so it cannot
#: survive a restart and silence a later, unrelated turn. The live dispatch path does not arm one;
#: the tool gate and delivery seam still consult the map, and tests inject synthetic tickets.
_tickets: Dict[str, Dict[str, Any]] = {}

#: The host session-store handle captured from the first inbound hook, for the trigger clock
#: (:func:`arm_trigger_clock`). ``None`` until the first inbound hook has run.
_arm_store_handle: Any = None

# ── the published text is this plugin's own ──────────────────────────────────────────────────
#
# The rule: everything the plugin publishes is authored on the plugin side, never by editing the
# host.
#
# * for every round **this plugin released**, the outbound text is what this plugin generated through
#   the host's public façade (``ctx.llm.complete``). The host's ``response_text`` is reference only:
#   it is never published, so a host that writes ``(empty)``, ``No reply …``, a localised notice or any
#   other self-authored string into the turn cannot put it in front of the user. The test for "is this
#   the plugin's own text" is structural — the plugin produced the string it returns — never a match
#   on the host's wording;
# * a generation fault or an unusable text maps to **zero outbound, zero settlement, zero day count**
#   with its own reason code. There is no fallback to the host's text.
#
# The privacy goal belongs in what the generation is *told*, not in a gate after it: the material
# block's constraint line (:func:`proactive.content.render_material`) and the conversation window's
# footer (``content.CONTEXT_WINDOW_FOOTER``) both forbid citing health values, schedules and bills.
# The **source-side** rule is the other half: a ``Record.sensitive`` record is never carried as
# material (``_dispatch``), so the round has no verified health fact to cite in the first place.

#: The purpose string the host sees on its own audit trail (never a provider, model or key).

#: Tool-gate counters: what the ``pre_tool_call`` gate saw and refused, per tool / reason code.
#: Process-local diagnostics only (the same lifecycle as ``_tickets``) — nothing durable is written.
_gate_counters: Dict[str, Dict[str, int]] = {"attempts": {}, "denied": {}, "denied_reasons": {}}

# ── whose round is this seam ending? (one predicate, both sides of the TTL) ──────────────────
# The delivery seam gets ``session_id`` and the turn's text — never the round's identity. A ticket is
# process-local and one-shot, so "this seam is ending the round the ticket released" is not something
# the ticket can assert by itself; it has to be *decided*, and the decision has to be the same one on
# both sides of the ticket TTL.
#
# Two facts answer it, and only these two:
#
# * **a user message was admitted after the release** — recorded in-process on the ticket itself
#   (:func:`_record_user_takeover`). This is the fact that survives an OFF session, where the plugin
#   is forbidden to write the metadata key and therefore cannot retire the durable marker;
# * **the release is still unaccounted for** — the durable ``pending`` marker carries this ticket's
#   ``released_at`` + ``session_id``. Every event that could hand the round to someone else retires
#   it (a user message, a pause, a settlement, a later wake) *when a write is allowed at all*.
#
# A retired marker means the release is over, so the running turn is a later one. A takeover with the
# marker still intact means the opposite: a user message took the in-flight round over and the plugin
# could not record it — the deadlock this predicate exists to break (``off:undeclared`` /
# ``off:disabled`` / ``off:config-unreadable`` sessions), so the seam must deliver the user's answer.
ROUND_OURS = "ours"                 # this seam ends the round the ticket released
ROUND_TAKEN_OVER = "taken-over"     # a user message took the in-flight round over
ROUND_NOT_OURS = "not-ours"         # the release is retired, unreadable, or absent: not ours to judge

#: What the durable record says about the release behind a ticket (see :func:`_release_state`).
RELEASE_IN_FLIGHT = "in-flight"     # a readable marker carries this release's time + session id
RELEASE_RETIRED = "retired"         # readable, but no marker for this release: it is over
RELEASE_UNKNOWN = "unknown"         # the key cannot be read at all: neither of the above


def set_clock(fn: Callable[[], float]) -> None:
    """Replace the clock used by the hooks (tests, incident reproduction)."""
    global _clock
    _clock = fn


def set_rng(rng: Any) -> None:
    """Replace the RNG used to sample silence windows (tests, incident reproduction)."""
    global _rng
    _rng = rng


def set_content(assembler: Optional[_content.Assembler] = None, *,
                boundary: Optional[_permissions.Boundary] = None) -> None:
    """Install (or clear) the read-only composition.

    ``boundary`` is the execution-layer tool boundary the generation path runs with; when it is
    omitted but the assembler carries a toolbox, that toolbox's boundary is adopted so the two can
    never disagree about which tools exist.
    """
    global _assembler, _boundary
    _assembler = assembler
    # The *place* (home/profile) belongs to the configured composition: only ``refresh_content`` can
    # record it, and it does so right after this call. Any other install — a test fixture, a
    # directly built composition — names no home, so a place left over from an earlier configured
    # assembly must not be attributed to it (that would make the profile-mismatch gate refuse rounds
    # on a fixture's behalf).
    _content_state.update(wiring=None, home=None, profile=None, home_source=None)
    boundary = boundary or getattr(assembler, "boundary", None)
    if boundary is not None:
        _boundary = boundary
    elif assembler is not None and assembler.toolbox is not None:
        _boundary = _permissions.Boundary(executor=assembler.toolbox.executor,
                                          toolbox=assembler.toolbox,
                                          disabled=tuple(assembler.injected_tools))
    else:
        _boundary = None


def content_assembler() -> Optional[_content.Assembler]:
    """The installed assembler (diagnostics/tests)."""
    return _assembler


def content_boundary() -> Optional[_permissions.Boundary]:
    """The installed tool boundary (diagnostics/tests)."""
    return _boundary


#: The assembly verdicts that are **faults**, not 「no usable topic」: a missing window, a broken
#: assembly. These still refuse the round (a configuration fault must not be papered over with a
#: chat). Every other silent verdict — no recent record, only stale/health/unrelated/coercive ones, a
#: dead source — is exactly what the opener path exists for.
_OPENER_REFUSED_REASONS = frozenset({C.SILENT_POLICY, C.SILENT_INTERNAL})


def opener_material(reason: str = "", toolbox: Any = None) -> str:
    """The material a wake is opened with when the assembly found nothing to cite.

    ``""`` means "do not open a round at all" — the verdict for an assembly *fault*, and what an
    operator gets by replacing this seam. A genuine 「no usable topic」 verdict, a dead source and a
    coercive record all return material: that is the rule (「不管如何，LLM 必须发送一条聊天」). Kept as
    a module-level function on purpose: it is the one seam a test (or an incident reproduction) can
    replace.
    """
    if reason in _OPENER_REFUSED_REASONS:
        return ""
    return _content.render_opener_material(reason, toolbox=toolbox)


def reset_seams() -> None:
    """Restore the real clock/RNG, drop every in-flight ticket, uninstall the composition and
    unbind the plugin context."""
    global _clock, _rng, _assembler, _boundary, _content_setting_key, _content_installed_by_config
    global _arm_store_handle
    _clock = time.time
    _rng = random.Random()
    _tickets.clear()
    _arm_store_handle = None
    _trigger.reset()
    # A reset is a fresh process for the operator-visible declaration summary too.
    global _declaration_summary_logged
    _declaration_summary_logged = False
    for counter in _gate_counters.values():
        counter.clear()
    _assembler = None
    _boundary = None
    # The next hook must re-read the configured composition from scratch, not trust a fingerprint
    # taken before the seams were reset (a test that re-installs a composition directly would
    # otherwise leave this module believing it still owns the old one).
    _content_setting_key = _UNSET
    _content_installed_by_config = False
    _content_state.clear()
    _content_state.update({"configured": False, "problems": (), "installs": 0, "present": False,
                           "refusal": ""})
    _settings.reset_reader()
    # The host context is the adapter's state: a reset is a fresh process as far as the host
    # surface is concerned (tests re-bind through the adapter).
    _adapter.reset_context()


def tickets() -> Dict[str, Dict[str, Any]]:
    """A copy of the in-flight release tickets (diagnostics/tests)."""
    return dict(_tickets)


# ── plugin-level settings ──────────────────────────────────────────────────────────────────


def bind_plugin_context(ctx: Any) -> None:
    """Hand ``ctx`` to the host adapter, which is the package's only owner of it.

    Called once from ``register()``. Nothing is read at registration time, so the capability probe
    and ``plugins doctor`` (which call ``register`` against a recording context) are unaffected, and
    a context without ``get_config`` leaves every setting at its documented default.

    The context's manifest is also the **only** evidence of which home this plugin instance belongs
    to (``composition.resolve_home`` → ``host_adapter.manifest_*``); a multiplexing gateway's
    process ``HERMES_HOME`` is the global home rather than the profile's. ``runtime`` keeps no copy —
    ``plugin_context()`` reads the adapter's.
    """
    _adapter.bind_context(ctx)
    _settings.bind_context(ctx)


def plugin_context() -> Any:
    """The context this plugin was registered with (diagnostics; ``None`` before ``register``).

    A read of the adapter's bound context: there is no second copy in this module, so a caller cannot
    mutate the plugin's view of the host by rebinding one of two globals.
    """
    return _adapter.context()


def pending_release_ttl() -> float:
    """Effective in-flight-ticket TTL in seconds (setting ``pending_release_ttl_seconds``)."""
    return _settings.value(_settings.PENDING_RELEASE_TTL)


def event_timestamp_sanity() -> float:
    """Effective timestamp sanity band in seconds (setting ``event_timestamp_sanity_seconds``)."""
    return _settings.value(_settings.EVENT_TIMESTAMP_SANITY)


def _unusable_setting_shape(raw: Any) -> str:
    """What kind of value was refused — never the value itself.

    A refused setting is operator input, and operator input is not evidence this plugin may write to
    a log: a value pasted into ``config.yaml`` from somewhere else (a URL, a token, a line of a
    private note) would be disclosed verbatim by the very warning that says it was not used, and the
    log is read by more people than the config file. The diagnosis does not need the value: the key,
    the reason code and the default are what an operator acts on, and the *shape* ("boolean",
    "string", "out of range") is what says which mistake was made.
    """
    if isinstance(raw, bool):
        return "boolean"
    if not isinstance(raw, int):
        return "string" if isinstance(raw, str) else type(raw).__name__
    return "out of range"


# ── the configured composition, installed into this plugin's own runtime module ─────────────
# ``composition.py`` is the root that wires the two real read-only sources into one assembler, and
# this module is where it is installed. Without that installation a released wake would carry the
# raw heartbeat prompt — no topic selection, no machine-text filter, no 6-character floor, no
# acknowledgement table, no 「找不到可靠话题就静默」 — so the installation is the precondition for
# releasing anything at all.
#
# The value is the ``composition`` plugin setting (``contract.SETTING_COMPOSITION``), read through
# ``ctx.get_config`` on **every inbound round** (once, at the top of ``_dispatch`` — the assembly is
# only needed where a round would be released, so ``transform_llm_output`` and ``pre_tool_call`` do
# not read it), so editing ``config.yaml`` applies to the next round with no reload. The composition
# is rebuilt only when the configured value changes (restarting the read of a settled value on every
# round would re-open the sources and reset their counters for nothing).
#
# Fail closed, and one direction only: absent, unreadable, incomplete or illegal ⇒ nothing is
# installed and **no wake is released** — whatever authorised the session (a declaration or an
# in-process ``enable_session`` blob): ``off:content-unconfigured``, zero main/aux LLM, zero
# delivery, **no write** to the session metadata key. A user message is never affected. The gate is
# keyed on the one fact that matters for the round: *is an assembly installed in this process*. A
# refresh that fails while a composition is installed withdraws that assembly first, so an edit from
# a usable value to an unusable one cannot leave the older assembly releasing rounds on numbers the
# operator has already replaced. (An assembler installed directly through ``set_content`` — a test
# fixture — is an assembly too; in a real gateway the only installer is the configured composition
# below.)
_UNSET = object()

#: Fingerprint of the configured value whose composition this module installed (``_UNSET``: never
#: looked at yet). Change detection only — the value itself is never logged.
_content_setting_key: Any = _UNSET
#: Whether *this* module installed the current assembler from the setting (so a removal may undo it).
_content_installed_by_config: bool = False
#: What the last refresh saw: problems are field *names*, never values.
#: ``refusal`` is the reason code a refused wake carries while no assembly is installed ("" once
#: one is), so the two faults — an unusable *value* and an unprovable *place* — stay
#: distinguishable at every seam instead of collapsing into one code.
_content_state: Dict[str, Any] = {"configured": False, "problems": (), "installs": 0, "present": False,
                                  "refusal": ""}


def content_ready() -> bool:
    """True when an assembly is installed, i.e. a wake may be released with material."""
    return _assembler is not None


def content_required() -> bool:
    """Diagnostics: whether the profile carries a ``composition`` value at all (usable or not).

    It is **not** the gate: the release condition is "an assembly is installed", not "the operator
    declared one" — an absent block must refuse a wake just as loudly as an unusable one, because
    the round would carry the raw heartbeat prompt either way.
    """
    return bool(_content_state.get("present"))


def content_state() -> Dict[str, Any]:
    """What the last :func:`refresh_content` saw (diagnostics/tests; no values, no record text)."""
    return dict(_content_state)


def resolve_content_home() -> Tuple[Path, str]:
    """``(home, source)`` for this plugin instance — the data root a composition would be built for.

    Delegates to :func:`proactive.composition.resolve_home`, which reads this instance's manifest
    through the host adapter, so the plugin's **own install identity** decides and the process
    environment is only the bare-interpreter fallback. No context is passed: the adapter is the
    single owner of it. Never raises: an unreadable manifest is "no
    evidence", not a fault.
    """
    try:
        return _composition.resolve_home()
    except Exception as exc:  # noqa: BLE001 - fall back, never guess from an exception body
        logger.warning("proactive-conversation: resolving this plugin instance's home failed (%s); using the "
                       "process home", type(exc).__name__)
        return _composition.default_home(), _composition.HOME_FROM_PROCESS


def content_profile() -> Optional[str]:
    """The profile the installed assembly is bound to; ``None`` when nothing is installed or the
    assembly was installed directly (``set_content``) and therefore names no profile."""
    return _content_state.get("profile")


#: What a side of the comparison is called when it could not be proven (no paths, no keys).
UNKNOWN_SIDE = "<unknown>"


def profile_from_session_key(session_key: Any) -> Optional[str]:
    """The profile a session key *proves* the round belongs to, or ``None``.

    The layout itself — the ``agent`` prefix, the four chat-type slots, the core's default ``main`` namespace
    and the ``main~`` escape — is the gateway's, so it lives in the host adapter beside the
    gateway's own inverse. This function is the package-facing name every caller uses.

    It is read from the **key** rather than from ``source.profile`` because the gateway builds a key
    for every inbound round — a heartbeat wake's source may carry no profile at all — and "the scope
    did not say" must not be read as "the default profile".
    """
    return _adapter.profile_from_session_key(session_key)


@dataclass(frozen=True)
class RoundProfile:
    """What a round proves about *its own* profile: one name, or ``None`` and why.

    ``scope`` is the source's routing profile, ``key`` the namespace-derived profile, and ``evidence``
    records what produced ``profile``: ``"scope"``, ``"session-key"`` (a named namespace, the strongest
    gateway-issued fact), ``"scope+session-key"`` (both, agreeing), ``"session-key-default"`` (only the
    core's default ``agent:main`` marker, which cannot contradict a named scope), ``""`` (nothing did) or
    ``"conflict"`` (two named proofs disagree — ``profile`` is then ``None``: two facts that contradict
    each other prove nothing, e.g. a session key frozen under one profile and routed to another).
    """

    profile: Optional[str]
    evidence: str
    scope: str
    key: str

    @property
    def proven(self) -> bool:
        """True when one profile is named and no second proof contradicts it."""
        return bool(self.profile) and self.evidence != "conflict"


def round_profile(dims: Optional[Mapping[str, Any]], session_key: Any) -> RoundProfile:
    """Collect this round's profile proofs from the two independent gateway facts.

    Precedence is by strength, not by convenience: a **named** namespace is a per-key gateway fact and
    wins over (and can refute) the scope stamp; the scope stamp is next; the core's default ``agent:main``
    marker last. A round with neither names no profile at all, and it must stay refused rather than be
    read as "the default profile".
    """
    scope = str((dims or {}).get("profile") or "").strip()
    key_profile = profile_from_session_key(session_key)
    named_key = bool(key_profile) and key_profile != "default"
    if named_key and scope:
        if scope == key_profile:
            return RoundProfile(scope, "scope+session-key", scope, str(key_profile))
        return RoundProfile(None, "conflict", scope, str(key_profile))
    if named_key:
        return RoundProfile(key_profile, "session-key", scope or UNKNOWN_SIDE, str(key_profile))
    if scope:
        return RoundProfile(scope, "scope", scope, str(key_profile or UNKNOWN_SIDE))
    if key_profile:
        return RoundProfile(key_profile, "session-key-default", UNKNOWN_SIDE, str(key_profile))
    return RoundProfile(None, "", UNKNOWN_SIDE, UNKNOWN_SIDE)


def content_round_refusal(dims: Optional[Mapping[str, Any]],
                          session_key: Any) -> Optional[Tuple[str, str, RoundProfile]]:
    """``(reason, bound_profile, proof)`` when this round may not be released from this assembly.

    The one predicate for the profile release rule, and the only place it is decided:

    * an assembly that claims **no** place (installed directly by ``set_content`` — a test fixture)
      refuses nothing: it names no data root, so no root can be the wrong one;
    * an assembly that *does* name a place is served only by a round whose profile is **proven** and
      equal to it. A round with no proof at all is refused, and two proofs that disagree are refused:
      "not disprovable" is not "allowed" once a data root is at stake: ``source.profile is None`` must
      not release a default-profile round from a named profile's home, whose memory would then feed
      the message.
    """
    bound = _content_state.get("profile")
    if not bound:
        return None
    proof = round_profile(dims, session_key)
    if proof.proven and str(proof.profile) == str(bound):
        return None
    return C.REASON_HOME_MISMATCH, str(bound), proof


def identity_evidence() -> Tuple[str, str]:
    """``(manifest source, home source)`` for this instance — the evidence, for logs (no paths).

    ``manifest source`` is hermes' own discovery-source string (``user`` for a per-home install,
    ``project``/``bundled``/``entrypoint`` for the shapes that are not homes) or ``<unknown>`` when the
    context carries no manifest at all; ``home source`` is the :func:`proactive.composition.resolve_home`
    provenance. Neither is a path, so this is safe to log.
    """
    manifest_source = _adapter.manifest_source()
    try:
        _, home_source = resolve_content_home()
    except Exception:  # noqa: BLE001 - the helper itself never raises, but stay defensive here
        home_source = _composition.HOME_FROM_PROCESS
    return (manifest_source or UNKNOWN_SIDE), str(home_source or UNKNOWN_SIDE)


def _log_home_mismatch(session_key: str, bound: str, proof: RoundProfile) -> None:
    """One line per refusal: both sides named, the evidence, no paths and no record text.

    Both profiles are printed when they are known, and ``<unknown>`` where the round could not prove
    one — "the round said nothing" and "the round said the default profile" are different verdicts and
    the operator has to be able to tell which one refused.
    """
    round_side = repr(proof.profile) if proof.profile else UNKNOWN_SIDE
    logger.warning(
        "proactive-conversation: refusing the wake for %s (%s): the installed composition is bound to "
        "profile %r but this round's profile is %s (scope=%s, session-key=%s, proof=%s; manifest "
        "source=%s, data-root source=%s). The data root cannot serve this round — fix the plugin's "
        "install location or the session's profile, this is a configuration fault, not a dead source",
        _content.short_digest(session_key), C.REASON_HOME_MISMATCH, bound, round_side,
        repr(proof.scope), repr(proof.key), proof.evidence or "none", *identity_evidence())


def _log_unproven_identity(session_key: Any = None, proof: Optional[RoundProfile] = None, *,
                           wake: bool = True) -> None:
    """A refusal caused by the *instance's* unprovable home: reason, unprovable side, evidence.

    The other refusals can name both profiles; this one cannot name the instance's side at all, and
    saying ``<unknown>`` out loud is the point — the operator's action ("install the plugin in the
    profile's own ``plugins/`` dir") follows from it, while a line that silently omitted the side would
    read like a dead data source.
    """
    manifest_source, home_source = identity_evidence()
    round_side = (repr(proof.profile) if proof is not None and proof.profile else UNKNOWN_SIDE)
    digest = _content.short_digest(session_key) if session_key else "(no session yet)"
    message = (
        "proactive-conversation: refusing %s (%s): this plugin instance's profile home cannot be proven, so "
        "no composition is installed and no data root may serve a round (instance profile=%s, round "
        "profile=%s, manifest source=%s, data-root source=%s). A fallback home belongs to whichever "
        "profile the process serves — in a gateway that is not necessarily this one — so releasing "
        "from it would read another profile's memory; install the plugin under the profile's own "
        "plugins/ directory")
    args = (digest, C.REASON_HOME_MISMATCH, UNKNOWN_SIDE, round_side, manifest_source, home_source)
    if wake:
        logger.warning(message, *args)
    else:
        logger.debug(message, *args)


def content_refusal_reason() -> str:
    """Why the last :func:`refresh_content` left no assembly to release from.

    Two faults, two codes, because they send the operator to different places: ``off:home-mismatch``
    when the *place* is unprovable (fix the install location) and ``off:content-unconfigured`` when the
    ``composition`` block is absent, unusable or unreadable (fix the value). A usable configuration
    that installed an assembly answers ``""``.
    """
    if _assembler is not None:
        return ""
    return str(_content_state.get("refusal") or C.REASON_CONTENT_UNCONFIGURED)


def content_gate_applies(prepared: bool) -> bool:
    """Whether this wake must be refused for want of an assembly (fail closed).

    One rule for **every** authorisation source: with no assembly installed, a wake is refused.
    There is no exemption for a session authorised by the in-process ``enable_session`` API: that
    path runs a wake the same way, the model receives the raw heartbeat prompt, and the adapter
    delivers one message.

    A user message never reaches this function: only a round that
    :func:`proactive.policy.evaluate_release` said "allow" for is refused here.
    """
    return not prepared


def refresh_content() -> bool:
    """Read the ``composition`` setting live, install/rebuild/withdraw, and answer "may we release?".

    Never raises: an unexpected error here is logged by type only, and it takes the same fail-closed
    exit as an unusable value — withdraw whatever assembly is installed, forget the fingerprint, and
    answer ``False`` (no assembly ⇒ no release). Answering ``False`` alone would be enough for *this*
    round, but it would leave the previous assembly installed, and the round after it would release
    from numbers the operator has already replaced.
    """
    try:
        return _refresh_content()
    except Exception as exc:  # fail closed: a broken refresh must not release anything
        return _withdraw_and_refuse(exc)


def _refresh_content() -> bool:
    global _content_setting_key, _content_installed_by_config
    raw, read_ok = _settings.read_raw_checked(C.SETTING_COMPOSITION)
    if not read_ok:
        fingerprint, problems, present = "unreadable", ("settings",), True
    elif raw is _settings.MISSING or raw is None:
        fingerprint, problems, present = "absent", (), False
    else:
        try:
            fingerprint, problems, present = _composition.settings_fingerprint(raw), (), True
        except Exception as exc:  # noqa: BLE001 - cannot even describe the value ⇒ cannot use it
            # Change detection is the first thing that touches the operator's value, so a value that
            # cannot be fingerprinted (an integer whose ``repr`` exceeds Python's digit limit, a
            # hostile mapping) aborts the refresh *here* — before any comparison. Answering "refuse
            # this round" while leaving the previous assembly installed would keep
            # ``content_ready()`` true, so it takes the one fail-closed exit.
            return _withdraw_and_refuse(exc)
    # The change key carries the *place* the assembly would be built for, not only the value. A
    # configured value that has not changed is still the wrong assembly once this plugin instance is
    # serving a different home (profile switch, a copied plugin directory, a test that binds another
    # profile), and comparing the value alone would keep releasing from the old data root forever.
    # The *provenance* is part of the key for the same reason: the same path reached through the
    # process fallback is not the same evidence as one read off this instance's manifest, and a
    # context that loses its manifest must re-decide instead of keeping the earlier assembly.
    home, home_source = resolve_content_home()
    # i18n: which locale profile is active decides the language rules the assembly applies. Resolved
    # from the ``proactive_locale`` setting on every refresh (unset ⇒ neutral: no language filtering).
    # It is part of the change key, so editing ``proactive_locale`` alone rebuilds the assembly.
    locale_activation = _locales.current_activation()
    install_key = (fingerprint, str(home), home_source, locale_activation.code,
                   locale_activation.source)
    if install_key == _content_setting_key:
        return _assembler is not None  # unchanged value and place: no rebuild, no re-read of the sources

    _content_setting_key = install_key
    if fingerprint in ("absent", "unreadable"):
        _withdraw_configured_assembly()
        _content_state.update(configured=False, problems=problems, present=present, wiring=None,
                              home=None, profile=None, home_source=home_source,
                              refusal=C.REASON_CONTENT_UNCONFIGURED)
        logger.warning(
            "proactive-conversation: no composition is configured (%s: %s), so no wake is released (%s); a "
            "released round without an assembly would carry the raw heartbeat prompt instead of "
            "assembled read-only material", C.SETTING_COMPOSITION, fingerprint,
            C.REASON_CONTENT_UNCONFIGURED)
        return _assembler is not None

    # The *place* has to be proven before the value can mean anything. ``HOME_FROM_PROCESS``
    # is the absence of evidence — in a multiplexing gateway the process home is the global one while
    # the round belongs to a profile — so a composition built over it would read another profile's
    # ``state.db``/``memories``. Refuse the whole instance: nothing installed, the refusal carries its
    # own code, and a context that has a manifest again rebuilds (the key above includes the source).
    if not _composition.identity_proven(home_source):
        _withdraw_configured_assembly()
        _content_state.update(configured=False, problems=(), present=present, wiring=None,
                              home=None, profile=None, home_source=home_source,
                              refusal=C.REASON_HOME_MISMATCH)
        _log_unproven_identity()
        return False

    profile = _composition.profile_for_home(home)
    try:
        config, config_problems = _composition.try_from_settings(raw)
        if config is None:
            _withdraw_configured_assembly()
            _content_state.update(configured=False, problems=config_problems, present=True,
                                  wiring=None, home=None, profile=None, home_source=home_source,
                                  refusal=C.REASON_CONTENT_UNCONFIGURED)
            logger.warning(
                "proactive-conversation: the configured composition is unusable (%s: %s), so no wake is "
                "released (%s)", C.SETTING_COMPOSITION, ", ".join(config_problems) or "unknown",
                C.REASON_CONTENT_UNCONFIGURED)
            return False
        composition = _composition.install(_this_module(), config, clock=lambda: _clock(),
                                           locale_activation=locale_activation)
    except Exception as exc:  # noqa: BLE001 - fail closed, and the type only
        # A refresh that cannot finish must not leave the older assembly in place: the operator has
        # already replaced the numbers it was built from, so releasing from it would be releasing
        # from a value nobody configured (and, on the fingerprint path, it would keep doing so on
        # every later round). Withdraw it, forget the fingerprint so the next round re-decides from
        # scratch, and refuse this wake.
        return _withdraw_and_refuse(exc)
    _content_installed_by_config = True
    _content_state.update(configured=True, problems=(), present=True,
                          installs=int(_content_state.get("installs") or 0) + 1,
                          wiring=composition.wiring(), home=str(composition.home),
                          profile=composition.profile, home_source=composition.home_source,
                          refusal="")
    return True


def _withdraw_configured_assembly() -> None:
    """Take out the assembly *this module* installed from the setting, if it is the current one."""
    global _content_installed_by_config
    if _content_installed_by_config:
        _composition.uninstall(_this_module())
        _content_installed_by_config = False


def _withdraw_and_refuse(exc: BaseException) -> bool:
    """The one fail-closed exit for a refresh that could not finish: withdraw, forget, refuse.

    Every failure path that happens *after* an assembly may already be installed funnels through
    here — an unparseable/unbuildable value, a value that cannot even be fingerprinted, and any
    unexpected error escaping the refresh. Doing it in one place is the point: a fingerprint
    computation outside the guarded region would answer "refuse this round" while leaving the old
    assembly installed and ``content_ready()`` true, and the next round would release from it.

    The fingerprint is forgotten (``_UNSET``) so the next round decides from scratch instead of
    treating a value it never fingerprinted as unchanged. Logs carry the exception *type* only —
    never its body, never the configured value.
    """
    global _content_setting_key, _content_installed_by_config
    try:
        _withdraw_configured_assembly()
    except Exception as uninstall_exc:  # noqa: BLE001 - the seam refused; forget it anyway
        _content_installed_by_config = False
        logger.warning(
            "proactive-conversation: withdrawing the previously installed composition failed (%s); it is no "
            "longer treated as installed (%s)", type(uninstall_exc).__name__,
            C.REASON_CONTENT_UNCONFIGURED)
    _content_setting_key = _UNSET
    _content_state.update(configured=False, problems=("settings",), present=True, wiring=None,
                          home=None, profile=None, home_source=None,
                          refusal=C.REASON_CONTENT_UNCONFIGURED)
    logger.warning(
        "proactive-conversation: the configured composition could not be read or built (%s; %s), so the "
        "previous assembly was withdrawn and no wake is released (%s)", type(exc).__name__,
        C.SETTING_COMPOSITION, C.REASON_CONTENT_UNCONFIGURED)
    return False


def _this_module() -> Any:
    """This runtime module — the only module ``composition.install`` may ever install into."""
    return sys.modules[__name__]


# ── event helpers ───────────────────────────────────────────────────────────────────────────


def is_heartbeat_wake(event: Any) -> bool:
    """True for a gateway-injected heartbeat wake.

    The definition lives in the host adapter — the one place that knows what the
    host stamps on a wake, and the one place the next architecture change will be handled.
    """
    return _adapter.is_wake_event(event)


def session_key_for(event: Any, gateway: Any = None) -> Optional[str]:
    """The gateway session key this event belongs to, or ``None`` when it cannot be resolved.

    The translation lives in the host adapter: metadata first, then the gateway's private
    resolver. ``None`` means "nothing in this event proves a key", never "the default session".
    """
    return _adapter.session_key_from(event, gateway)


def _peek_session_id(store_handle: Any, session_key: str) -> Optional[str]:
    """The session's current id through the store probe (the adapter's translation)."""
    return _adapter.session_id_from(store_handle, session_key)


def _dims_for_event(event: Any, session_key: str, session_id: Optional[str]) -> Dict[str, Any]:
    """The six session dimensions of this event, translated to plain data by the adapter."""
    return _adapter.dimensions_from(event, session_key, session_id)


def _store_handle(session_store: Any, gateway: Any) -> Any:
    """The session store for this round — the hook argument, else the gateway's (via the adapter)."""
    return _adapter.store_handle_from(session_store, gateway)


def _event_timestamp(event: Any, now: float) -> Optional[float]:
    """The message's own time when it is usable, else ``None`` (caller falls back to ``now``).

    The host-shape half — a naive ``datetime`` is host-local, resolved the way the gateway's own
    ``gateway.message_timestamps._localize`` does — lives in the adapter. What stays here
    is the *plugin's* decision: a value further from ``now`` than the effective
    ``event_timestamp_sanity_seconds`` setting is treated as unusable.
    """
    epoch = _adapter.event_timestamp_epoch(event)
    if epoch is None:
        return None
    sanity = event_timestamp_sanity()
    return epoch if abs(epoch - now) <= sanity else None


# ── the config-file declaration as an authorisation + parameter source ──────────────────────
# The declaration list is *another* place the per-session configuration may come from. It is read
# on every hook call (``ctx.get_config`` → ``plugins.entries.proactive-conversation.settings``), matched on
# ``(session_key, session_id)``, and then mirrored into the plugin's own per-session metadata key so
# that everything downstream keeps reading exactly one thing (``store.read``). Nothing here changes
# what a release, a cap, a DND window or a pause *means*.


def session_declarations() -> Any:
    """The raw ``session_declarations`` setting, read live (``settings.MISSING`` when unset)."""
    return _declarations.raw_value()


#: Set once per process the first time the declaration list is consulted for a hook. The point is
#: the operator-visible aggregate: list EVERY invalid declaration once, up front,
#: instead of only learning about one when its session happens to be evaluated — and never spam it on
#: every hook. Reset by ``reset_seams`` so a test sees a fresh process.
_declaration_summary_logged = False


def _log_declaration_summary_once() -> None:
    """Log every invalid ``session_declarations`` entry once per process (field name + reason only).

    Uses the same ``declarations.validate_declarations`` → ``config.validate`` the dry-run script and
    the runtime OFF decision use, so the WARNING summary and the dry-run report agree. Fully guarded:
    a fault in this diagnostic must never break a hook. Privacy: field names, reason codes
    and our own detail strings only — never a plaintext session key / chat id.
    """
    global _declaration_summary_logged
    if _declaration_summary_logged:
        return
    _declaration_summary_logged = True
    try:
        problems = _declarations.validate_declarations(session_declarations())
    except Exception as exc:  # noqa: BLE001 - diagnostics only, never break the hook over a summary
        logger.warning("proactive-conversation: could not summarise session_declarations (%s)",
                       type(exc).__name__)
        return
    for problem in problems:
        logger.warning("proactive-conversation: %s", problem.line())


def _log_declared_refusal(session_key: str, declared: Any, *, wake: bool) -> None:
    """One line per refusal: reason code + the declaration field, never a config value."""
    message = ("proactive-conversation: the session declaration for %s does not authorise this session "
               "(%s%s); it stays OFF and its metadata key is left untouched")
    args = (_content.short_digest(session_key), declared.reason or C.REASON_INVALID,
            f", field {declared.field}" if declared.field else "")
    if wake:
        logger.warning(message, *args)
    else:
        logger.debug(message, *args)


def _log_permanent_close(session_key: str, *, wake: bool) -> None:
    """One line per closed session: the reason code only, no config values."""
    message = ("proactive-conversation: %s was permanently closed (%s); the declaration does not re-open it "
               "and its metadata key is left untouched")
    if wake:
        logger.warning(message, _content.short_digest(session_key), C.REASON_PERM_CLOSED)
    else:
        logger.debug(message, _content.short_digest(session_key), C.REASON_PERM_CLOSED)


def _log_unverified(session_key: str, *, wake: bool) -> None:
    """One line per unreadable key: the session is OFF because a close cannot be ruled out."""
    message = ("proactive-conversation: the session metadata key for %s could not be read (%s), so a "
               "permanent close cannot be ruled out; the declaration is not mirrored and nothing "
               "is written")
    if wake:
        logger.warning(message, _content.short_digest(session_key), C.REASON_UNVERIFIED)
    else:
        logger.debug(message, _content.short_digest(session_key), C.REASON_UNVERIFIED)


def _log_undeclared(session_key: str, *, wake: bool) -> None:
    """The declaration that authorised this session is gone: OFF, and nothing is written."""
    message = ("proactive-conversation: the declaration that authorised %s is no longer configured (%s); "
               "the session is OFF and its mirrored metadata is left untouched")
    if wake:
        logger.warning(message, _content.short_digest(session_key), C.REASON_UNDECLARED)
    else:
        logger.debug(message, _content.short_digest(session_key), C.REASON_UNDECLARED)


def _log_config_unreadable(session_key: str, *, wake: bool) -> None:
    """The declaration source itself could not be read: fail closed for every session."""
    message = ("proactive-conversation: the session declarations could not be read (%s), so no session's "
               "authorisation can be confirmed (%s); %s is OFF and nothing is written")
    if wake:
        logger.warning(message, C.SETTING_SESSION_DECLARATIONS, C.REASON_CONFIG_UNREADABLE,
                       _content.short_digest(session_key))
    else:
        logger.debug(message, C.SETTING_SESSION_DECLARATIONS, C.REASON_CONFIG_UNREADABLE,
                     _content.short_digest(session_key))


def _log_content_unconfigured(session_key: str, *, wake: bool, refused: str = "") -> None:
    """No usable composition is configured: no wake is opened and nothing is written.

    Logged with the setting *name*, the reason code and a fixed phrase — never the configured value
    and never the problem values, only field names, so a typo in ``config.yaml`` cannot leak into the
    log through this line.
    """
    message = ("proactive-conversation: no usable composition is configured (%s; problems=%s), so %s is "
               "refused (%s); a round without assembled read-only material would carry the raw "
               "heartbeat prompt, so nothing is written for %s")
    problems = ", ".join(_content_state.get("problems") or ()) or "none"
    digest = _content.short_digest(session_key)
    if wake:
        logger.warning(message, C.SETTING_COMPOSITION, problems, refused or "the wake",
                       C.REASON_CONTENT_UNCONFIGURED, digest)
    else:
        logger.debug(message, C.SETTING_COMPOSITION, problems, refused or "the wake",
                     C.REASON_CONTENT_UNCONFIGURED, digest)


def _log_damaged_marker(session_key: str, *, wake: bool) -> None:
    """The blob's ``origin`` field is present but unusable: OFF, and the key is left as it was."""
    message = ("proactive-conversation: the authorisation marker of %s is not one this version can honour "
               "(%s, field origin); the session is OFF and its metadata key is left as it was")
    if wake:
        logger.warning(message, _content.short_digest(session_key), C.REASON_INVALID)
    else:
        logger.debug(message, _content.short_digest(session_key), C.REASON_INVALID)


def _session_under_declaration(store_handle: Any, session_key: str, session_id: Optional[str],
                               dims: Dict[str, Any], now: float, *, wake: bool,
                               allow_mirror: bool = True):
    """``(read, refusal_reason)`` — what the declaration says about this session.

    * **the declaration source cannot be read** (``ctx.get_config`` raised): ``(None,
      off:config-unreadable)`` for *every* session — "I could not read the declarations" is not
      "the operator declared nothing", and must never fall back to a stored authorisation;
    * **no declaration for this ``session_key``** (key absent, list empty, or another session is
      declared) and the stored blob is **not** declaration-owned: this source is silent → the
      per-session metadata key decides;
    * **no declaration for this ``session_key``** but the stored blob *is* declaration-owned
      (``origin == "declaration"``): ``(None, off:undeclared)`` — the source that authorised this
      session is gone, so its leftover mirror is not an authorisation. Nothing is written, and a
      user message still passes;
    * **a permanently closed session**: ``(None, perm:closed)`` — the operator's close is terminal
      for this session, so a declaration that still names it neither re-enables it
      nor writes anything. Re-enabling is a **new** authorisation, i.e. a new ``session_id``;
    * **a key whose state cannot be read**: ``(None, off:unverified)`` — the plugin cannot rule out
      a close, so it neither mirrors nor writes. A transient store fault must never be able to
      overwrite a close marker that it simply could not see (the write path re-checks this too);
    * **a declaration that refuses this session**: ``(None, reason)`` — no decision is built on a
      half-trusted blob and, above all, **nothing is written** for it;
    * **a usable declaration**: mirrored into the metadata key (marked with its origin), then read
      back, so the rest of the hook keeps reading one thing (``store.read``) and cannot diverge
      from what is durable.
    """
    declared = _declarations.lookup_setting(session_key, session_id)
    _log_declaration_summary_once()
    if declared.unreadable:
        _log_config_unreadable(session_key, wake=wake)
        return store.SessionRead(dims=dims, reason=C.REASON_CONFIG_UNREADABLE), \
            C.REASON_CONFIG_UNREADABLE
    # One probe for both answers: a read fault must not be able to present an existing (paused)
    # blob as "nothing to carry over" (see store.read_probed).
    existing, kstate = store.read_probed(store_handle, session_key, dims)
    if not declared.found:
        if kstate == store.KEY_UNREADABLE:
            _log_unverified(session_key, wake=wake)
            return store.SessionRead(dims=dims, reason=C.REASON_UNVERIFIED), C.REASON_UNVERIFIED
        if existing.origin == C.ORIGIN_DECLARATION:
            # The declaration owned this authorisation and is gone: OFF, no write, no revival of
            # the leftover mirror. Re-declaring the session is what turns it back on.
            _log_undeclared(session_key, wake=wake)
            return store.SessionRead(dims=dims, reason=C.REASON_UNDECLARED), C.REASON_UNDECLARED
        return existing, ""
    if kstate == store.KEY_CLOSED:
        # A permanent close outranks every declaration source: the close is the operator's
        # last word on *this* session, so nothing here may write the metadata key back into life.
        _log_permanent_close(session_key, wake=wake)
        return store.SessionRead(dims=dims, reason=C.REASON_PERM_CLOSED), C.REASON_PERM_CLOSED
    if kstate == store.KEY_UNREADABLE:
        # Fail closed: without a readable key state the plugin cannot tell "never written" (a
        # declaration may mirror) from "closed", and guessing would let a store fault undo a
        # permanent close. No read is built on an unverifiable key either.
        _log_unverified(session_key, wake=wake)
        return store.SessionRead(dims=dims, reason=C.REASON_UNVERIFIED), C.REASON_UNVERIFIED
    if existing.reason == C.REASON_INVALID and existing.field_name == "origin":
        # The blob's authorisation marker is present but unusable. Its ownership is therefore
        # unknown, and this blob is the only evidence of that: mirroring over it would erase the
        # evidence and let a declaration silently take over an authorisation nobody can identify
        # Refuse, write nothing — the session stays OFF until it is closed and a new
        # session id is authorised.
        _log_damaged_marker(session_key, wake=wake)
        return store.SessionRead(dims=dims, reason=C.REASON_INVALID, field_name="origin"), \
            C.REASON_INVALID
    if declared.refused:
        _log_declared_refusal(session_key, declared, wake=wake)
        reason = declared.reason or C.REASON_INVALID
        return store.SessionRead(dims=dims, reason=reason, field_name=declared.field), reason
    if not allow_mirror:
        # Fail closed, and the only place where "no assembly" outranks an *authorisation*: a
        # declaration that would authorise this session is not mirrored into the metadata key when
        # the round could not be opened anyway. Without an assembly a wake is refused, so mirroring
        # would write an authorisation for a round that never happens — the round must be refused
        # with zero writes. Every refusal above keeps its own reason code, so only an otherwise
        # authorisable wake reaches here.
        #
        # *Why* there is no assembly decides the code. An unusable value and an unprovable
        # place are different faults, and the second one must not be reported as the first — the
        # operator would go looking for a typo in ``config.yaml`` while the plugin sits in a
        # directory that belongs to no profile.
        reason = content_refusal_reason()
        if reason == C.REASON_HOME_MISMATCH:
            _log_unproven_identity(session_key, round_profile(dims, session_key), wake=wake)
        else:
            _log_content_unconfigured(session_key, wake=wake, refused="declaration not mirrored")
        return store.SessionRead(dims=dims, reason=reason), reason
    return _mirror_declaration(store_handle, session_key, dims, declared.declaration, now,
                               existing=existing, kstate=kstate), ""


def _mirror_declaration(store_handle: Any, session_key: str, dims: Dict[str, Any],
                        declaration: _declarations.Declaration, now: float,
                        existing: Optional[store.SessionRead] = None,
                        kstate: str = "") -> store.SessionRead:
    """Write the declaration's config into the metadata key, preserving any usable state.

    Only the config values are the declaration's; the ``state`` belongs to the session (counters,
    window, pause, in-flight ticket) and is carried over untouched when it is readable. A fresh
    state is created only when there is provably nothing usable to carry over — never when the
    existing blob merely could not be read: that would turn a transient fault into "this session had
    no state", and the write that followed would erase a paused/counted session. ``existing`` is
    therefore the caller's *probed* read — the read and the key state from one
    ``get_session_metadata`` call; a read fault reaches us as ``KEY_UNREADABLE`` and refuses.

    The blob is written with ``origin="declaration"``: that marker is what lets the next hook tell
    "a declaration owns this authorisation" from "the metadata key itself authorised it", so
    removing the declaration turns the session OFF instead of leaving a phantom authorisation.

    A mirror that does not land is a refusal, not a stale decision: the blob that comes back is
    compared with the declaration, and a session whose durable configuration is not what the
    declaration says is treated as OFF rather than run on the old numbers.

    The write itself is conditional (``store.write_ordered``): it re-establishes the key's state
    inside the same lock a close takes, so a close that happened after the caller's probe refuses
    this write, and a close that happened *during* it leaves a ``null`` the post-write probe sees —
    which is reported as ``perm:closed`` too, so the round is skipped either way. That is what makes
    "permanent close outranks the declaration" an ordering, not a race.
    """
    if existing is None or not kstate:
        existing, kstate = store.read_probed(store_handle, session_key, dims)
    if kstate == store.KEY_CLOSED:
        _log_permanent_close(session_key, wake=True)
        return store.SessionRead(dims=dims, reason=C.REASON_PERM_CLOSED)
    if kstate == store.KEY_UNREADABLE:
        _log_unverified(session_key, wake=True)
        return store.SessionRead(dims=dims, reason=C.REASON_UNVERIFIED)
    config_raw = dict(declaration.config_raw)
    if (existing.usable and existing.origin == C.ORIGIN_DECLARATION
            and store.same_config(existing.config_raw, config_raw)):
        return existing  # already mirrored: no write, and the state (counters) is not churned
    if existing.usable:
        # Hot-reload guard: a live edit may only swap the *gate*. It never touches the
        # counters, and an invalid edit or a timezone change is refused (keep the previous good
        # config, one deduped warning) so the mirrored blob keeps running on its old numbers.
        reload = _config.reconcile_config(existing.config, existing.config_raw, config_raw)
        _config.log_reload(reload, existing.state)
        if reload.kind in ("invalid", "tz-blocked", "unchanged"):
            return existing
        config_raw = reload.config_raw
    state = existing.state if existing.usable else policy.new_state(now, declaration.config, _rng)
    result = store.write_ordered(store_handle, session_key, config_raw=config_raw, state=state,
                                 dims=dims, origin=C.ORIGIN_DECLARATION)
    if result.blocked == store.WRITE_BLOCKED_CLOSED:
        # A close landed between the caller's gate and this write: the close wins, the round is OFF.
        _log_permanent_close(session_key, wake=True)
        return store.SessionRead(dims=dims, reason=C.REASON_PERM_CLOSED)
    if result.blocked == store.WRITE_BLOCKED_UNREADABLE:
        _log_unverified(session_key, wake=True)
        return store.SessionRead(dims=dims, reason=C.REASON_UNVERIFIED)
    if result.blocked == store.WRITE_BLOCKED_REFUSED:
        logger.warning("proactive-conversation: the declaration for %s could not be written into the "
                       "session metadata key (%s); the session is OFF",
                       _content.short_digest(session_key), C.REASON_WRITE_FAILED)
        return store.SessionRead(dims=dims, reason=C.REASON_WRITE_FAILED)
    mirrored = store.read(store_handle, session_key, dims)
    if (not mirrored.usable or mirrored.origin != C.ORIGIN_DECLARATION
            or not store.same_config(mirrored.config_raw, config_raw)):
        logger.warning("proactive-conversation: the declaration for %s could not be mirrored into the "
                       "session metadata key (written=%r); the session is OFF",
                       _content.short_digest(session_key), bool(result.written))
        return store.SessionRead(config=declaration.config, state=state, config_raw=config_raw,
                                 dims=dims, reason=C.REASON_INVALID, field_name="mirror")
    return mirrored


# ── hook: pre_gateway_dispatch ──────────────────────────────────────────────────────────────


# ── the trigger clock: arm the tick loop and feed it the declared sessions ────────────────────
# The clock decides *when* a session is considered. It is armed once, on the host's running loop,
# from the first inbound hook (which already holds the session-store handle). Every wake re-reads
# the declarations and reuses each session blob's stored dims verbatim, because ``store.read``
# requires the dims to equal the blob's exactly (``store.dims_match``). Nothing is cached.


def _clock_sessions(store_handle: Any) -> List[Dict[str, Any]]:
    """The declared, currently-usable sessions, each with the dims its blob already stored.

    A declaration that is absent / invalid / disabled, or a session with no mirrored blob yet (no
    dims, nothing to release), is skipped — the gate would refuse it ``no-blob`` anyway.
    """
    raw = _declarations.raw_value()
    if not isinstance(raw, list):
        return []
    sessions: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        skey = item.get("session_key")
        raw_sid = item.get("session_id")
        if not isinstance(skey, str) or not skey.strip():
            continue
        skey = skey.strip()
        # ``session_id`` is optional: absent (``None``) is a channel-level declaration, present
        # must be a non-empty string (a malformed value makes the whole list unusable elsewhere).
        # A channel-level declaration must still be enumerated here, or its clock scan finds no
        # session and proactive firing dies silently (the /reset self-heal would never run).
        if raw_sid is None:
            sid: Optional[str] = None
        elif isinstance(raw_sid, str) and raw_sid.strip():
            sid = raw_sid.strip()
        else:
            continue
        dims = store.stored_dims(store_handle, skey)
        if not dims:
            continue
        # A channel-level declaration follows the *current* session: take the id the blob was
        # mirrored with (the mirror writes the current dims on every hook). A pinned declaration
        # must match that id exactly.
        eff_sid = sid if sid is not None else dims.get("session_id")
        if not isinstance(eff_sid, str) or not eff_sid.strip():
            continue
        if not _declarations.lookup(raw, skey, eff_sid).usable:
            continue
        # The stored dims must address *this* declared session, or reusing them would release a
        # different session's state; skip a stale/reset blob.
        if dims.get("session_key") not in (None, skey) or dims.get("session_id") not in (None, eff_sid):
            continue
        sessions.append({"session_key": skey, "session_id": eff_sid, "dims": dict(dims)})
    return sessions


def _clock_provider() -> Tuple[Any, List[Dict[str, Any]]]:
    """``(store_handle, sessions)`` for one clock wake; ``(None, [])`` before the first hook."""
    handle = _arm_store_handle
    if handle is None:
        return None, []
    # Re-checked every wake: an operator flipping trigger_mode to 'off' stops releases without a restart.
    if _trigger.trigger_mode() != "on":
        return handle, []
    return handle, _clock_sessions(handle)


def arm_trigger_clock(store_handle: Any = None) -> bool:
    """Arm the trigger clock on the host loop (idempotent; safe to call every hook).

    ``store_handle`` is captured the first time it is seen (the inbound hook has it). ``arm_clock``
    uses ``ctx.spawn_task`` and needs a running loop, so this must run from inside a hook the host
    invokes on its loop (``ainvoke_hook`` runs sync callbacks inline there). Returns whether the
    clock is armed; ``False`` before any store handle is known or when no loop is available.
    """
    # plugin.yaml: trigger_mode 'off' "arms no proactive turn". Honour it before doing anything.
    if _trigger.trigger_mode() != "on":
        return False
    global _arm_store_handle
    if store_handle is not None:
        _arm_store_handle = store_handle
    if _arm_store_handle is None:
        return False
    try:
        return _trigger.arm_clock(_clock_provider)
    except Exception as exc:  # arming is best-effort; never break the hook over it
        logger.warning("proactive-conversation: could not arm the trigger clock (%s)", type(exc).__name__)
        return False


def pre_gateway_dispatch(event: Any = None, gateway: Any = None, session_store: Any = None,
                         **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Eligibility gate: ``{"action": "skip"}`` for a refused wake, ``None`` (pass) otherwise."""
    try:
        return _dispatch(event, gateway, session_store)
    except Exception as exc:  # fail open
        # The type only: an exception body may quote a session identifier, and this hook sees
        # per-session data.
        logger.warning("proactive-conversation: dispatch gate failed, passing the round through: %s",
                       type(exc).__name__)
        return None


def _dispatch(event: Any, gateway: Any, session_store: Any) -> Optional[Dict[str, Any]]:
    degraded = isolation.degraded_shape(event, gateway, session_store)
    if degraded:
        # Call-time net: the load should have failed (``register()`` refuses in the plugin host), so
        # reaching here means the plugin was armed outside hermes' loader — a
        # capability probe, a hand-built context, a future loader that ignores the raise. Decide
        # nothing and pass the round through: guessing here would either block ordinary user traffic
        # or release wakes the user never enabled, and neither is recoverable by the user.
        logger.warning(
            "proactive-conversation: refusing to decide on degraded:plugin-host arguments (%s); passing the "
            "round through untouched. This plugin must not be armed in hermes' plugin host — set "
            "plugins.isolation: in_process.", degraded,
        )
        return None
    store_handle = _store_handle(session_store, gateway)
    if event is None or store_handle is None:
        return None
    arm_trigger_clock(store_handle)
    session_key = session_key_for(event, gateway)
    if not session_key:
        return None
    session_id = _peek_session_id(store_handle, session_key)
    dims = _dims_for_event(event, session_key, session_id)
    now = _clock()
    wake = is_heartbeat_wake(event)
    if not wake:
        # A real user message takes the in-flight round over, and that has to be a fact in
        # this process — for an OFF session the plugin may not write the metadata key, so the durable
        # ``pending`` marker cannot be retired and the last seam would otherwise mistake the user's
        # own turn for this plugin's released round. Recorded before anything can
        # refuse or skip: the admission itself is what supersedes the round.
        _record_user_takeover(session_key, session_id, now)

    # The composition is read once per inbound round, before the authorisation source is consulted —
    # a wake may neither be released nor *mirrored* without an assembly. A user
    # message is never affected. ``content_prepared`` is only the decision input; the refusal itself
    # happens where a round would otherwise open, so every authorisation verdict keeps its own reason
    # code (permanently closed, undeclared, dims mismatch, paused, DND, caps, window) — only an
    # otherwise releasable round is refused with ``off:content-unconfigured``.
    content_prepared = refresh_content()

    read, declared_refusal = _session_under_declaration(
        store_handle, session_key, session_id, dims, now, wake=wake,
        allow_mirror=bool(content_prepared or not wake))
    if declared_refusal:
        # A declaration that names this session and refuses it: the session is OFF and nothing is
        # written for it. A user message still passes through.
        return {"action": "skip", "reason": declared_refusal} if wake else None

    if not wake:
        # A real user message: never blocked, never silently dropped. It re-anchors the silence
        # window and clears the since-user counters — for a configured session only; an OFF
        # session is left completely untouched (no state is created for it).
        if read.usable:
            state = policy.on_user_message(
                read.state, read.config, now, _rng,
                anchor_at=_event_timestamp(event, now),
            )
            store.write(store_handle, session_key,
                        config_raw=read.config_raw, state=state, dims=dims,
                        origin=read.origin)
        return None

    # A heartbeat wake: this is the round the plugin exists to decide.
    if not read.usable:
        return {"action": "skip", "reason": read.reason}
    if not session_id:
        # Without a session id the delivery gate could not recognise this round later, so the
        # release would never be counted: refuse instead of releasing something unaccountable.
        return {"action": "skip", "reason": C.REASON_NO_SESSION}
    state, action, reason = policy.evaluate_release(
        read.state, read.config, now, _rng, session_id=session_id,
        pending_ttl=pending_release_ttl(),
    )
    if action != "allow":
        store.write(store_handle, session_key, config_raw=read.config_raw, state=state, dims=dims,
                    origin=read.origin)
        return {"action": "skip", "reason": reason}

    # Fail closed, release point: with no assembly installed the round must not open at all. It
    # would be a round whose only material is the raw heartbeat prompt — no candidate, no provenance,
    # no 「找不到可靠话题就静默」— and the counters must not be charged for it: nothing is written
    # (the policy decision above is discarded) and no ticket exists, so the next wake re-decides from
    # the same numbers. This is the same rule for every authorisation source: a declaration and an
    # in-process ``enable_session`` blob are both refused.
    if content_gate_applies(content_prepared):
        # Two faults reach this point: the composition value is unusable, or the instance's
        # place could not be proven. They keep separate codes — the operator's next action differs.
        reason = content_refusal_reason()
        if reason == C.REASON_HOME_MISMATCH:
            _log_unproven_identity(session_key, round_profile(dims, session_key))
        else:
            _log_content_unconfigured(session_key, wake=True, refused="wake not released")
        return {"action": "skip", "reason": reason}

    # An installed assembly that is bound to another profile cannot serve this round. Its data root
    # belongs to a different profile, so both real sources refuse every read
    # (``unavailable:session-scope-mismatch``); reporting that as ``silent:source-down`` would blame
    # a dead source for a configuration fault and retry it on every poll forever. Refuse it here,
    # before the release is recorded: no ticket, no counter moved, no turn. (The declaration mirror
    # above is an authorisation fact and still happens.)
    #
    # The rule is symmetric: a round *without* a provable profile is refused too.
    # ``source.profile is None`` must not mean "not disprovable" — that would release a
    # default-profile round from a named profile's home, whose memory then feeds the message.
    verdict = content_round_refusal(dims, session_key)
    if verdict is not None:
        reason, bound_profile, proof = verdict
        _log_home_mismatch(session_key, bound_profile, proof)
        return {"action": "skip", "reason": reason}

    # A release is only a *candidate*. With a read-only composition installed it must also produce
    # one citable, recent record; otherwise the round is retired uncounted and skipped —
    # 「找不到可靠话题就静默」is enforced here, not in a prompt.
    #
    # The cost of that verdict: the topic rules are unchanged and still logged per source, but a wake
    # with nothing to cite is **opened** with the opener material instead of being retired —
    # 「不管如何，LLM 必须发送一条聊天」. The verdict still travels, as ``opener:<verdict>``, so the
    # log/CLI keep telling the truth about the sources.
    assembly: Optional[_content.Assembly] = None
    opener = ""
    if _assembler is not None:
        assembly = _assembler.assemble(_content.Scope.from_dims(dims), now)
        if assembly.ready and getattr(assembly.topic, "sensitive", False):
            # Source side: a proactive round's material never carries a ``Record.sensitive``
            # record. A health value is only ever an unverified personal fact, and the ruling forbids
            # publishing one. The record is dropped the same way an unusable one always was: the round
            # opener material (which cites nothing), and the verdict is logged as its own reason so
            # the report still says what happened (`opener:silent:health`).
            logger.info("proactive-conversation: wake for %s will not cite a sensitive record; opening with "
                        "no citation instead", _content.short_digest(session_key))
            assembly = None
        if assembly is None or assembly.silent:
            reason = assembly.reason if assembly is not None else C.SILENT_HEALTH
            opener = opener_material(reason, toolbox=getattr(assembly, "tools", None))
            if not opener:
                # The rule is off (or an operator replaced this seam): the plain skip verdict.
                state = policy.void_pending(state)
                store.write(store_handle, session_key, config_raw=read.config_raw, state=state,
                            dims=dims, origin=read.origin)
                logger.info("proactive-conversation: wake for %s stayed silent (%s)",
                            _content.short_digest(session_key), reason)
                return {"action": "skip", "reason": reason}
            logger.info("proactive-conversation: wake for %s opened without a citable topic (%s)",
                        _content.short_digest(session_key), _content.opener_reason(reason))

    # ── the release point ───────────────────────────────────────────────────────────────────────
    # Everything above this line is the gate (per-session authorisation, composition presence,
    # profile binding, topic/opener rules). A permitted wake is dropped here with its own reason
    # code — zero generation calls, zero settlement, zero daily count, zero delivery — so an
    # operator reading the log can tell it apart from an authorisation refusal. Proactive delivery
    # belongs to the trigger lane, which opens a full agent turn and then brakes it.
    logger.info("proactive-conversation: the consume lane is not used; the wake for "
                "%s is dropped (%s: 0 sends, 0 settlement, 0 day count)",
                _content.short_digest(session_key), CONSUME_LANE_RETIRED)
    return {"action": "skip", "reason": CONSUME_LANE_RETIRED}


def pre_llm_call(user_message: Any = None, turn_id: Any = None, session_id: Any = None,
                 conversation_history: Any = None, is_first_turn: Any = False,
                 **kwargs: Any) -> Optional[str]:
    """Recognise our own proactive turn and bind its ``turn_id``; return no injected context.

    The host fires this once per turn, immediately before the first model call, with the
    ``user_message`` and ``turn_id``. If the message matches a pinned prompt digest, the turn is
    bound (:func:`proactive.trigger.recognise`) so the two brakes can act on *this* turn and no other.
    An ordinary user turn never matches, is never bound, and is left entirely to the host.

    This hook returns ``None`` on purpose: the trigger injects no extra context into the user
    message. It is also the **first-hook-call** clock fallback point: if the clock was not armed at
    registration (a CLI-side load with no loop), the first turn tries to arm it.
    """
    try:
        arm_trigger_clock()  # first-hook-call arm (idempotent; needs the host loop)
    except Exception:
        pass
    try:
        _trigger.recognise(session_id, turn_id, user_message)
    except Exception as exc:  # never let a recogniser fault disturb a real turn
        logger.warning("proactive-conversation: pre_llm_call recognition failed (%s)", type(exc).__name__)
    return None


# ── hook: transform_llm_output ──────────────────────────────────────────────────────────────


def transform_llm_output(response_text: Any = None, session_id: Any = None, platform: Any = None,
                         turn_id: Any = None, **kwargs: Any) -> Optional[str]:
    """Delivery-gate re-check: ``[SILENT]`` when the session moved under a released turn.

    Two paths answer here, in order:

    1. **a bound turn** — a ``turn_id`` bound by :func:`pre_llm_call` is judged by the text brake
       (:func:`proactive.trigger.handle_text_brake`): ``[SILENT]`` when the policy no longer allows
       the round, else the host's text is delivered and the round is settled once;
    2. **a released-round ticket** — the ticket for ``session_id``, popped on first use.
    """
    if turn_id is not None:
        try:
            handled, b_result = _trigger.handle_text_brake(str(turn_id), response_text, _clock())
        except Exception as exc:  # the text brake must not, by its own fault, silence a real turn
            logger.warning("proactive-conversation: the B-lane text brake failed, delivering: %s",
                           type(exc).__name__)
            handled, b_result = False, None
        if handled:
            return b_result

    ticket_key = str(session_id) if session_id else ""
    if not ticket_key:
        return None
    ticket = _tickets.pop(ticket_key, None)  # pop: at most one re-check per released round
    if ticket is None:
        return None
    try:
        return _recheck(ticket, response_text)
    except Exception as exc:  # fail open: never silence because of our own error
        logger.warning("proactive-conversation: delivery re-check failed, delivering: %s",
                       type(exc).__name__)
        return None


def _recheck(ticket: Dict[str, Any], response_text: Any = None) -> Optional[str]:
    now = _clock()
    released_at = float(ticket.get("released_at") or 0.0)
    store_handle = ticket.get("store")
    session_key = str(ticket.get("session_key") or "")
    dims = ticket.get("dims") or {}
    read, kstate = store.read_probed(store_handle, session_key, dims)

    # ── whose round is this seam ending? one predicate, used on both sides of the TTL ──────────
    owner = _whose_round_is_this_seam(ticket, read)
    if owner == ROUND_TAKEN_OVER:
        # A user message was admitted after the release and the durable marker was not retired, so
        # the plugin was forbidden to write it (the session is OFF): the turn ending here is that
        # message's, not this ticket's. Deliver it — no silence, no settlement, no write. Silencing
        # it is the one thing this seam must never do: the user would lose their answer and receive
        # the gateway's silence-marker warning text instead.
        logger.info("proactive-conversation: a user message took the in-flight round of %s over; letting the "
                    "turn through untouched (no re-check, no settlement)",
                    _content.short_digest(session_key))
        return None

    # The ticket is process-local and the first re-check consumes it, so a ticket older than the
    # effective TTL belongs to a round that ended without ever reaching this seam (a crash, an abort,
    # a wedged process): it must not be allowed to silence a *later* round of the same session, which
    # may well be the user's own turn. That is the TTL's whole purpose.
    if now > released_at + pending_release_ttl():
        # The TTL bounds how long a ticket may *judge* a turn it may not own — it is not an exemption
        # from the authorisation *source*. A round that is provably still this session's own in-flight
        # release and whose authorisation came from a declaration is judged by that declaration on
        # both sides of the boundary: withdrawing it must not become a licence to send merely because
        # the turn outlived the ticket TTL. The proof of "still ours" is
        # :func:`_whose_round_is_this_seam` — the same answer the in-TTL branch below uses.
        if owner == ROUND_OURS:
            refused = _in_flight_declaration_refusal(ticket, read)
            if refused:
                logger.warning("proactive-conversation: not sending the released round for %s (%s): the "
                               "declaration that authorised it no longer does (the ticket had "
                               "outlived its TTL, and that is not an exemption)",
                               _content.short_digest(session_key), refused)
                return _silence(session_key, refused)
        elif kstate == store.KEY_CLOSED:
            # A permanent close is terminal: it outranks the stale-deliver default, so a round
            # whose session was closed while the ticket was in flight is never sent — however long
            # that ticket lived. The close and the round are both *this* session's, which is why this
            # is the one NOT_OURS case that refuses rather than passes.
            logger.warning("proactive-conversation: not sending the released round for %s (%s): the session "
                           "was permanently closed while the round was in flight (the key is JSON "
                           "null, and a close is terminal)", _content.short_digest(session_key),
                           C.REASON_PERM_CLOSED)
            return _silence(session_key, C.REASON_PERM_CLOSED)
        return _stale_ticket_verdict(session_key, read, now, released_at)

    # A session reset / switch gives the same session key a new session id. That is knowable here
    # without reading anything, and it means this released message belongs to a session that no
    # longer exists — so it is dropped rather than delivered into the new one.
    current_session_id = _peek_session_id(store_handle, session_key)
    if current_session_id != str(ticket.get("session_id") or ""):
        return _silence(session_key, C.REASON_SILENT_SESSION)

    # ── the session-side verdict: may this round be sent at all? ────────────────────────────────
    # The session rules, in order (off/dims → declaration → delivery re-check → expression
    # boundary). Pulled into a function because the same verdict has to be taken **again**
    # once a generation has returned: see :func:`_read_after_the_generation`.
    refused = _refusal_for_the_session(ticket, read, owner, now=now, released_at=released_at,
                                       session_key=session_key, dims=dims,
                                       store_handle=store_handle, response_text=response_text)
    if refused:
        return _silence(session_key, refused)

    # ── a released round has no turn left to publish — the consume gate already answered it ─────
    # Under the consume shape the text that reaches the user was decided and settled at admission:
    # the consumer answers the wake before ``_handle_message_with_agent`` is even called. So a turn
    # ending here for a round this plugin owns means the consumer did **not** match the wake, and
    # the host's ``response_text`` — its own notice, an empty retry, a localised error, or a real
    # answer — is the only text this seam could still put on the wire. That is exactly what the rule
    # forbids: the round publishes nothing rather than the host's own words, and the reason says
    # "the consumer missed this one" instead of folding it into a generic silence.
    #
    # The generation is not repeated here either: doing it twice would make this seam a second
    # delivery path, so the invariant is one decision, one settlement, one ticket.
    if owner == ROUND_OURS:
        return _silence(session_key, C.SILENT_CONSUME_MISSED)

    # ── a round this seam does not own: nothing here may judge it ────────────────────────────────
    # ``ROUND_TAKEN_OVER`` returned above (that turn is the user's), and a round this plugin still
    # owns was silenced above. What is left is a release that somebody else already retired: the
    # turn is passed through, and the settlement below is the only thing taken from it.

    # ── the write-ahead settlement: this turn's last chance to record the round before it is sent ──
    # The gateway sends whatever this seam returns; nothing after this point consults a plugin again.
    # So the round is settled here — the ticket consumed and the counters moved durably — through the
    # same single settlement point the guard uses, and a settlement that does not
    # land refuses the send. Delivering anyway would put a message on the wire that no durable state
    # accounts for, which keeps this release live for the next process: exactly the duplicate window
    # this ordering closes. The refusal costs at most one missed message (the round stays deliverable,
    # uncounted) — a bound on *this plugin's* bookkeeping: no send without a recorded count, and no
    # round counted twice. Host-side duplicates after this seam (shaping, retries, the gateway's own
    # delivery ledger, which redelivers an unconfirmed final) are outside the plugin and are **not**
    # ruled out by this ordering; nothing here supports a "the recipient never sees a duplicate"
    # claim.
    if not _settle(store_handle, read, dims, session_key=session_key, released_at=released_at,
                   now=now):
        return _silence(session_key, C.REASON_SETTLE_FAILED)
    return None


def _refusal_for_the_session(ticket: Dict[str, Any], read: store.SessionRead, owner: str, *,
                             now: float, released_at: float, session_key: str,
                             dims: Dict[str, Any], store_handle: Any,
                             response_text: Any = None) -> str:
    """May this released round be sent into this session as it stands? ``""`` means yes.

    The rules that are about the *session* (not about the ticket), in order and with their effects:

    * **gone** (turned off / revoked / cleared while the turn was running) → ``silent:off``
      (``silent:dims`` when the reason is a dimension mismatch). This is also where a permanent
      close (``null``) is honoured, and it outranks the declaration re-check: a closed session is
      closed whatever the source now says;
    * **the declaration no longer authorises it** → that refusal's own reason code. The source is
      re-read here because this is the plugin's **last** seam before the outbound: an operator who
      deletes or disables the declaration while the round is already in flight must still get zero
      sends. Only a round that is still provably ours is judged by it, and only
      declaration-owned ones — an ``api``-enabled round is not re-judged by a declaration it never had;
    * **the delivery re-check** (:func:`policy.delivery_recheck`) → pause / DND / a newer user
      message. The in-flight marker is voided on the way out, as it always was;
    * **the expression boundary**, on the text that would actually reach the user: a proactive
      message that pressures, blames or induces dependency is dropped rather than delivered. Only a
      round this plugin released has a ticket, so an ordinary user turn can never be silenced here.
      The pattern id is logged; the text itself never is. ``response_text=None`` skips it — the
      caller has already judged that text (the same boundary applies to the text
      it returns).

    Two callers, one verdict: the seam calls this before the generation, and again (through
    :func:`_read_after_the_generation`) when a generated text exists but the session may have moved
    on since. Keeping it one function is what makes the second call evidence about the same rules,
    not a second, drifting copy of them.
    """
    if not read.usable:
        return C.REASON_SILENT_DIMS if read.reason == C.REASON_DIMS else C.REASON_SILENT_OFF

    state, cfg = read.state, read.config
    if state is None or cfg is None:  # a usable read carries both; anything else is not judged
        return C.REASON_SILENT_OFF

    if owner == ROUND_OURS:
        declared_refusal = _in_flight_declaration_refusal(ticket, read)
        if declared_refusal:
            logger.warning("proactive-conversation: not sending the released round for %s (%s): the "
                           "declaration that authorised it no longer does",
                           _content.short_digest(session_key), declared_refusal)
            return declared_refusal

    reason = policy.delivery_recheck(state, cfg, now, released_at)
    if reason:
        _void_the_marker(store_handle, session_key, read, dims)
        return reason

    if _assembler is not None and response_text is not None:
        if _content.expression_violation(response_text):
            _void_the_marker(store_handle, session_key, read, dims)
            return C.SILENT_EXPRESSION
    return ""


def _void_the_marker(store_handle: Any, session_key: str, read: store.SessionRead,
                     dims: Dict[str, Any]) -> None:
    """Clear the in-flight marker on the record that was just read (best effort, as before)."""
    state, cfg_raw = read.state, read.config_raw
    if state is None or cfg_raw is None:
        return  # nothing this seam may write; the marker lapses with the ticket TTL
    store.write(store_handle, session_key, config_raw=cfg_raw, state=policy.void_pending(state),
                dims=dims, origin=read.origin)


#: What the gate can be told after a generation, in one value: a **fresh** snapshot of the session,
#: the clock that snapshot was judged at, and the refusal it implies (``""`` = the round may still be
#: sent). ``read`` is ``None`` only when the session cannot be read at all — and then ``refusal`` is
#: never ``""``, so a caller never has to handle "readable and refused" as two separate cases.
@dataclass(frozen=True)
class _LateVerdict:
    read: Optional[store.SessionRead]
    now: float
    refusal: str = ""


def _read_after_the_generation(ticket: Dict[str, Any]) -> _LateVerdict:
    """Take the gate's verdict **again**, on a fresh read and a fresh clock, after a generation.

    ``_complete_once`` hands the turn to the host for up to :data:`GENERATION_TIMEOUT_SECONDS`. The
    ticket is not touched while that call is out, so by the time a text exists the session may have
    been paused, revoked, permanently closed, or taken over by a user message, and the DND boundary
    may have been crossed. The first verdict is therefore **stale evidence** about the moment of the
    send, and settling against the snapshot it read would not merely be late — it would write that
    snapshot back over the change (``settle_release`` persists the state it is handed, so a paused
    session would come back ``active``, its pending marker cleared and its counters moved, for a
    message the user was never supposed to receive).

    What is re-decided here is exactly the session-side verdict
    (:func:`_refusal_for_the_session`). The ticket-side decisions — the TTL, the session-id reset,
    and "a user message took this round over" — were already taken for this turn
    (:func:`_recheck`), and re-taking the TTL after a slow provider would turn a slow model into an
    expired round: the TTL bounds *how long a ticket may judge later turns*, not how long a provider
    may take inside the turn it already judged. The one ticket-side fact worth
    re-reading is the takeover stamp, because a user who wrote while we were waiting really did
    supersede the round — and there ``policy.delivery_recheck`` cannot see it on an OFF session
    (the plugin may not write the blob, so ``last_user_at`` never moved): ``ROUND_TAKEN_OVER`` is
    reported as ``silent:superseded``, which is the same verdict the durable path reaches and the
    one the stamp exists to carry.

    Cost: one ``get_session_metadata`` and one config read per *generated* round — i.e. only when the
    model already sent no chat. Nothing is written here; the caller settles or retires with
    :attr:`_LateVerdict.read`, so every write in the generation path lands on current state.
    """
    now = _clock()
    store_handle = ticket.get("store")
    session_key = str(ticket.get("session_key") or "")
    dims = ticket.get("dims") or {}
    read = store.read(store_handle, session_key, dims)
    # A reset during the generation means the text was written for a session that no longer exists —
    # the same verdict the seam takes when the reset happened before it, and unlike the TTL it
    # cannot misfire on a slow provider: a session id changes only on an explicit reset or switch.
    if _peek_session_id(store_handle, session_key) != str(ticket.get("session_id") or ""):
        return _LateVerdict(read=read, now=now, refusal=C.REASON_SILENT_SESSION)
    owner = _whose_round_is_this_seam(ticket, read)
    if owner == ROUND_TAKEN_OVER:
        return _LateVerdict(read=read, now=now, refusal=C.REASON_SILENT_SUPERSEDED)
    refusal = _refusal_for_the_session(
        ticket, read, owner, now=now, released_at=float(ticket.get("released_at") or 0.0),
        session_key=session_key, dims=dims, store_handle=store_handle)
    return _LateVerdict(read=read, now=now, refusal=refusal)


def _retire_the_marker(store_handle: Any, session_key: str, current: Optional[store.SessionRead],
                       dims: Dict[str, Any], *, stale: store.SessionRead) -> bool:
    """Void the release marker without moving any counter, on the record as it stands **now**.

    The retirement of an unusable generation has the same write-back hazard as its settlement: the
    snapshot read before the generation carries the session as it was then, so retiring against it
    would undo a pause (or a resume) that landed while the host had the turn — a round that reached
    nobody would leave its fingerprints on the session anyway. ``current`` is the snapshot taken
    after the generation (:func:`_read_after_the_generation`); ``stale`` is the pre-generation one,
    used only when the fresh read carries no writable state at all (a close that landed in between)
    — and there the store refuses the write on its own, which is what the caller's warning says.
    """
    state, cfg_raw = (current.state, current.config_raw) if current is not None else (None, None)
    if state is None or cfg_raw is None:
        state, cfg_raw = stale.state, stale.config_raw
    if state is None or cfg_raw is None:
        return False
    return bool(store.write(store_handle, session_key, config_raw=cfg_raw,
                            state=policy.void_pending(state), dims=dims,
                            origin=(current or stale).origin))


def _record_user_takeover(session_key: str, session_id: Optional[str], now: float) -> None:
    """Stamp every in-flight ticket of *session_key* with the moment a user message was admitted.

    This is the process-local half of the ownership decision, and it exists
    because the durable half is unavailable exactly where it matters: in an OFF session the plugin
    may not write the metadata key at all (the zero-write rule), so ``policy.on_user_message`` never
    runs and the release marker stays in place forever. Without this stamp the last seam would read
    that intact marker as "the round is still ours" and silence the user's own answer.

    Scope, deliberately narrow:

    * only tickets whose ``session_key`` is this session's (and whose ``session_id`` still matches,
      when both are known — a reset makes the old ticket unreachable rather than taken over);
    * only the **first** admission is recorded (``setdefault``): the stamp means "a user message took
      the round over", not "when the user last spoke";
    * nothing durable is written, nothing is logged: the stamp dies with the process, like the
      ticket it is attached to.
    """
    for ticket in _tickets.values():
        if str(ticket.get("session_key") or "") != session_key:
            continue
        ticket_session = str(ticket.get("session_id") or "")
        if session_id and ticket_session and session_id != ticket_session:
            continue
        ticket.setdefault("taken_over_at", float(now))


def _release_state(ticket: Dict[str, Any], read: store.SessionRead) -> str:
    """Whether the durable record still shows this release in flight.

    Three answers, because "I cannot read the key" is not "the release is over":

    * :data:`RELEASE_IN_FLIGHT` — a readable blob whose ``pending`` marker carries *this* ticket's
      ``released_at`` **and** ``session_id``: nothing has retired the release;
    * :data:`RELEASE_RETIRED` — a readable blob with no matching marker (settled, voided, superseded
      by a user message / pause / later wake, or a blob that never carried one): the release is
      over, so a turn running now is a later one;
    * :data:`RELEASE_UNKNOWN` — the plugin cannot read the key at all (a store fault, or a permanent
      close that erased the blob): neither of the above can be asserted.
    """
    if not read.usable or read.state is None:
        return RELEASE_UNKNOWN
    pending = read.state.pending
    if not isinstance(pending, dict):
        return RELEASE_RETIRED
    pending_at = pending.get("released_at")
    if isinstance(pending_at, bool) or not isinstance(pending_at, (int, float)):
        return RELEASE_RETIRED
    try:
        ticket_at = float(ticket.get("released_at") or 0.0)
    except (TypeError, ValueError):
        return RELEASE_RETIRED
    if float(pending_at) != ticket_at:
        return RELEASE_RETIRED
    ticket_session = str(ticket.get("session_id") or "")
    pending_session = pending.get("session_id")
    if not ticket_session or not isinstance(pending_session, str):
        return RELEASE_RETIRED
    if pending_session != ticket_session:
        return RELEASE_RETIRED
    return RELEASE_IN_FLIGHT


def _whose_round_is_this_seam(ticket: Dict[str, Any], read: store.SessionRead) -> str:
    """**The** ownership decision: is the turn at this seam the round the ticket released?

    One predicate for the whole delivery seam — the TTL boundary on both sides *and* the in-TTL
    declaration re-check use this and only this. Answers:

    * :data:`ROUND_OURS` — no user message since the release, and the durable record still shows the
      release in flight. The round is this plugin's own: the declaration source may judge it, and it
      may be silenced and settled exactly as before;
    * :data:`ROUND_TAKEN_OVER` — a user message was admitted after the release and the release is
      **not provably retired** (``in-flight``, or ``unknown`` because the key cannot be read). The
      turn at this seam belongs to that message: it is delivered, never silenced, never settled, and
      the plugin writes nothing;
    * :data:`ROUND_NOT_OURS` — the release is provably retired, or there is no takeover, no proof and
      no readable state: the old rules decide (the stale-ticket verdict, the delivery re-check, or
      the OFF verdicts).

    Invalidation: the ticket is dropped when the seam consumes it, and the whole map is cleared by
    :func:`reset_seams`; nothing here outlives the process that released the round. A takeover whose
    marker *was* retired stays ``ROUND_NOT_OURS`` on purpose — that release is over for real, and the
    old verdicts (``silent:superseded`` and friends) own the turn, which is what keeps the silence
    rule intact for a round the user's message genuinely superseded.
    """
    state = _release_state(ticket, read)
    if ticket.get("taken_over_at") is not None:
        return ROUND_NOT_OURS if state == RELEASE_RETIRED else ROUND_TAKEN_OVER
    return ROUND_OURS if state == RELEASE_IN_FLIGHT else ROUND_NOT_OURS


def _in_flight_declaration_refusal(ticket: Dict[str, Any], read: store.SessionRead) -> str:
    """Why a declaration-owned round must not be sent now (``""`` = it may be).

    The declaration is an authorisation *source*, so it has to be consulted as long as the
    round it authorised is still deliverable — not only at the wake that released it. Three answers
    make the round unsendable, and all of them are the same fail-closed verdicts the wake path uses:

    * **the source cannot be read** (``ctx.get_config`` raised) → ``off:config-unreadable``: an
      unreadable source is not "the operator declared nothing", and a round whose authorisation
      cannot be confirmed must not go out;
    * **the declaration is gone** (entry removed, list emptied, setting key deleted) →
      ``off:undeclared``: the mirror left behind is not an independent authorisation;
    * **the declaration refuses the session** (``enabled: false``, a hot-edited invalid value, a
      different ``session_id``) → that reason code.

    Ownership is decided by the **current** blob when it says ``declaration``, falling back to the
    ticket's release-time marker: a blob taken over by a declaration between the release and
    this seam is declaration-owned now, and must not be deliverable on the strength of the ticket.
    A non-declaration session (an ``api``-enabled one, or one no declaration names) is never touched
    here — the declaration re-check only governs declaration-owned rounds.

    Nothing is written on any of these paths: the metadata key of an undeclared session must not be
    rebuilt, and a re-check is not a write opportunity. The round stays uncounted, as for every other
    silenced round; ``policy.expire_pending`` (the ticket TTL) is what eventually retires the marker.
    """
    origin = (getattr(read, "origin", "") or str(ticket.get("origin") or ""))
    if origin != C.ORIGIN_DECLARATION:
        return ""
    declared = _declarations.lookup_setting(ticket.get("session_key"), ticket.get("session_id"))
    if declared.unreadable:
        return C.REASON_CONFIG_UNREADABLE
    if not declared.found:
        return C.REASON_UNDECLARED
    if declared.refused:
        return declared.reason or C.REASON_INVALID
    return ""


def _settle(store_handle: Any, read: store.SessionRead, dims: Dict[str, Any], *,
            session_key: str, released_at: float, now: float) -> bool:
    """Settle this round ahead of the send; ``False`` means "refuse, do not send".

    Anything that goes wrong here — no ticket left to commit, a store that refuses or fails the
    write, an unexpected error — is a settlement that did not land, and a settlement that did not
    land may not be followed by a send.
    """
    route = _delivery_guard.Route(
        session_key=session_key,
        session_id=dims.get("session_id"),
        profile=dims.get("profile"),
        platform=dims.get("platform"),
        chat_id=dims.get("chat_id"),
        thread_id=dims.get("thread_id"),
    )
    digest = _delivery_guard.short_digest(
        _delivery_guard.round_id_for(released_at, route.session_id))
    try:
        settled = _delivery_guard.settle_release(store_handle, route, read, now=now, rng=_rng,
                                                 round_digest=digest)
    except Exception as exc:  # a settlement that raises is a settlement that did not land
        logger.warning("proactive-conversation: settling round %s raised (%s); not sending", digest,
                       type(exc).__name__)
        return False
    if not settled:
        logger.warning("proactive-conversation: round %s could not be settled (%s); not sending",
                       digest, C.REASON_SETTLE_FAILED)
    return bool(settled)


def _stale_ticket_verdict(session_key: Any, read: store.SessionRead, now: float,
                          released_at: float) -> Optional[str]:
    """What to do at the delivery gate with a ticket that outlived its round.

    Default: deliver unchanged. The ticket can no longer be trusted as "this round is mine", and
    silencing a round that is not ours is the one thing this seam must never do — a later
    user-initiated turn would lose its answer and the user would instead receive the gateway's
    "silence marker on a user turn" warning text (the very symptom this seam prevents).

    One exception, and only one: **the user's quiet hours**. DND is a property of the *delivery
    moment*, not of the round's age, and 「不要在免打扰时段打扰」 must hold for a proactive message
    however long its round took — a round that crossed into DND after a slow model call is still a
    round this plugin opened, and sending it is the disturbance DND exists to prevent.

    The guard that keeps this exception safe is ``last_user_at <= released_at``: a real user message
    that arrived after the release is evidence that the round under this ticket has been superseded,
    and — for a stale ticket — that the running turn is very likely the user's own. In that case the
    verdict is "deliver", because guessing "ours" there would silence the user's answer. The same
    guard means a session whose blob is unreadable now (revoked while the ticket was in flight) is
    never silenced from here either: an unreadable blob cannot prove the round is ours.
    """
    if not read.usable or read.config is None or read.state is None:
        return None
    if not _config.in_dnd(now, read.config):
        return None
    if read.state.last_user_at > released_at:
        return None
    return _silence(session_key, C.REASON_SILENT_DND)


def _silence(session_key: Any, reason: str) -> str:
    """Log one silencing decision (digest + reason code only) and return the gateway marker."""
    logger.info("proactive-conversation: silencing released turn for %s (%s)",
                _content.short_digest(session_key), reason)
    return C.SILENCE_MARKER


# ── the host's own silence verdict ──────────────────────────────────────────────────────────
#
# The exemption below spends nothing (no delivery, no counter), so it may only be claimed where the
# host's own delivery decision is the same silence. Both halves are therefore read from the host
# module instead of copied into this package: a private list of markers, or a private copy of the
# machinery lane rule, would drift from the gateway and then quietly exempt rounds the gateway sends.


def _host_silence_verdict(text: Any) -> bool:
    """True only when the host's own rule calls this **whole** response an intentional silence.

    The predicate lives in the host adapter (the package's only ``gateway.*`` import);
    the semantics here are unchanged: a host that cannot be asked answers "not silence".
    """
    return _adapter.host_silence_verdict(text)


def _host_silence_is_legal(event: Any) -> bool:
    """Whether the host's rules would honour a bare marker on **this** released round's lane.

    The rule lives in the host adapter; an unreadable host is "not legal".
    """
    return _adapter.host_silence_is_legal(event)


def _model_kept_the_round_silent(ticket: Dict[str, Any], owner: str, text: Any) -> bool:
    """Whether *text* is the host's own silence answer **for this round**, spoken by the model.

    Three facts, all of them this plugin's own at the seam, and all of them required:

    * ``owner == ROUND_OURS`` — :func:`_whose_round_is_this_seam` proved the durable marker is this
      round's and no user message took it over. A ticket a user message took over, or one whose
      release is provably retired, is **never** exempted: that turn may be the user's own (the one
      thing this seam must never swallow);
    * ``ticket["silence_legal"]`` — the host would honour a bare marker on the lane this round was
      opened on, so the exemption and the host's delivery decision agree;
    * the exact-marker rule of the host (:func:`_host_silence_verdict`).

    A round that fails any of these is settled and delivered.
    """
    if owner != ROUND_OURS:
        return False
    if not ticket.get("silence_legal"):
        return False
    return _host_silence_verdict(text)


# ── the must-reply rule (one chat per released round) ───────────────────────────────────────


# ── hook: pre_tool_call (the execution-layer tool gate) ─────────────────────────────────────


def pre_tool_call(tool_name: Any = "", args: Any = None, task_id: Any = "", session_id: Any = "",
                  tool_call_id: Any = "", turn_id: Any = "", api_request_id: Any = "",
                  middleware_trace: Any = None, **kwargs: Any) -> Optional[Dict[str, str]]:
    """Refuse non-read tools **inside a round this plugin released**; ``None`` (no opinion) otherwise.

    Hermes fires this hook immediately before every tool execution, built-in or plugin, and turns
    ``{"action": "block", "message": ...}`` into that call's result — so this is the point where
    「不主动调用会写入 / 支付 / 外发的工具」is code rather than a sentence in a prompt (the sentence in
    ``content.render_material`` is a courtesy to the model, not the boundary).

    Scope, and it is the whole reason this hook is safe to register:

    * the call is refused **only** when its ``session_id`` carries a *live* release ticket
      (``pre_gateway_dispatch`` put one there for a wake this plugin released, and the delivery gate
      removes it when that turn ends). An ordinary user turn, another session, a sub-agent turn, or a
      turn whose ticket outlived the effective ticket TTL (``pending_release_ttl()``) has no ticket
      and is therefore never touched;
    * decided in :func:`proactive.permissions.released_round_denial`: ``read`` tools pass, everything
      else is refused with its capability's reason code (``denied:write-tool`` / ``denied:send-tool``
      / ``denied:payment-tool``), and a name this package cannot classify is refused as
      ``denied:unknown-tool`` rather than assumed harmless.

    Failure modes are deliberately asymmetric:

    * deciding *which* round a call belongs to is guarded — if that check itself breaks, the answer is
      "no opinion", because guessing "released" would start refusing tools in ordinary conversations;
    * once the round is known, the decision is **not** wrapped: if the gate raises, the exception
      leaves this callback and Hermes' own fail-closed path blocks the tool
      (``hermes_cli/plugins_dispatch.py``: a raising or timed-out ``pre_tool_call`` callback blocks).
      Swallowing it here would turn a broken boundary into an open one. Same for a hang: the tool is
      blocked, not executed.

    Untouched by this hook: nothing is logged about the call's arguments, and no counter other than
    the process-local gate counters below moves.
    """
    if not _in_released_round(session_id):
        # A turn bound by pre_llm_call is subject to the boolean ``proactive_tools_enabled`` switch:
        #   * ``true``  -> every tool callable (the deny list is not consulted);
        #   * ``false`` -> every tool refused with ``denied:proactive-tool``.
        # The decision lives in ``trigger.handle_tool_brake`` (it owns the turn binding and the
        # blocked-round accounting); an unrecognised turn is left entirely to the host — neither
        # counted nor braked, so a user turn never sees any of this.
        if turn_id:
            block = _trigger.handle_tool_brake(str(turn_id), str(tool_name or ""))
            if block is not None:
                return block
        return None
    name = str(tool_name or "")
    _bump(_gate_counters["attempts"], name)
    result = _permissions.released_round_denial(name, specs=_boundary_specs())
    if result is None:
        return None
    _bump(_gate_counters["denied"], name)
    _bump(_gate_counters["denied_reasons"], result.reason)
    # The tool name and the reason code are safe to log; the call's arguments are not, and never are.
    logger.info("proactive-conversation: tool %s refused in a released round (%s)", name, result.reason)
    return _permissions.block_directive(result)


def _in_released_round(session_id: Any) -> bool:
    """True when *session_id* holds a live release ticket, i.e. we are inside our own released turn.

    Guarded on purpose: this is the only check that decides whether the plugin may interfere at all,
    so an unexpected error here must answer "no" (leave the round alone) instead of blocking tools in
    a conversation the plugin has no business in.
    """
    try:
        ticket = _live_ticket(session_id)
        return ticket is not None and not _superseded(ticket)
    except Exception as exc:  # pragma: no cover - defensive: a broken lookup is not a release
        logger.warning("proactive-conversation: tool gate could not resolve the round, leaving it alone: %s",
                       type(exc).__name__)
        return False


def _superseded(ticket: Dict[str, Any]) -> bool:
    """True only when the round this ticket belongs to has been **explicitly retired**.

    ``policy.on_user_message`` sets ``state.pending = None``: a real user message that arrives while a
    released turn is in flight retires it, and the delivery gate silences that turn's output. The
    process-local ticket, however, lives on until the delivery gate pops it or the TTL expires — so
    without this
    check the user's *own* next turn would inherit the released round's read-only boundary for up to
    ``pending_release_ttl()``. Refusing tools in an ordinary conversation is the one thing this
    hook must never do, so a retired round stands the gate down.

    The same answer uses the same predicate as the delivery seam: a ticket a user message took
    over (``ROUND_TAKEN_OVER``) stands the gate down too, even though an OFF session could not write
    the retirement — otherwise the user's own turn would inherit the boundary of a round the plugin
    was never allowed to record.

    Fail-closed the other way: a blob that cannot be read is **not** treated as retired — the live
    ticket is the primary evidence that this plugin released the round, and only an affirmative
    "the release is gone" lets the boundary go.
    """
    store_handle = ticket.get("store")
    session_key = ticket.get("session_key")
    if store_handle is None or not session_key:
        return False
    try:
        read = store.read(store_handle, str(session_key), ticket.get("dims") or {})
    except Exception as exc:  # pragma: no cover - defensive: an unreadable blob is not a retirement
        logger.warning("proactive-conversation: could not confirm the release is retired, keeping the gate: %s",
                       type(exc).__name__)
        return False
    if _whose_round_is_this_seam(ticket, read) == ROUND_TAKEN_OVER:
        return True
    if not read.usable:
        return False
    return getattr(read.state, "pending", None) is None


def _live_ticket(session_id: Any) -> Optional[Dict[str, Any]]:
    """The ticket for *session_id* when it is still inside its mechanical TTL, else ``None``.

    The TTL is the same one the delivery gate uses: a turn that died before its ``transform_llm_output``
    must not keep refusing tools in a later, unrelated turn of the same session.
    """
    key = str(session_id) if session_id else ""
    if not key:
        return None
    ticket = _tickets.get(key)
    if ticket is None:
        return None
    released_at = float(ticket.get("released_at") or 0.0)
    if _clock() > released_at + pending_release_ttl():
        logger.info("proactive-conversation: release ticket for %s outlived its turn, gate stands down",
                    _content.short_digest(key))
        return None
    return ticket


def _boundary_specs() -> Optional[Dict[str, Any]]:
    """This package's own tool registry when a boundary is installed (else ``None``).

    Read live rather than captured: the composition root may install the boundary after the plugin
    was registered, and a stale copy would classify the plugin's own read tools as unknown.
    """
    boundary = _boundary
    return dict(boundary.executor.specs()) if boundary is not None else None


def _bump(counter: Dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def tool_gate_counters() -> Dict[str, Dict[str, int]]:
    """A copy of the dispatch-gate counters (diagnostics/tests; process-local, never durable)."""
    return {name: dict(counts) for name, counts in _gate_counters.items()}


# ── per-session API for enable / control ────────────────────────────────────────────────────


def read_session(store_handle: Any, session_key: str, dims: Dict[str, Any]) -> store.SessionRead:
    """Read + validate this session's blob (never raises)."""
    return store.read(store_handle, session_key, dims)


def enable_session(store_handle: Any, session_key: str, dims: Dict[str, Any],
                   config_raw: Any) -> bool:
    """Enable/update a session. Refuses (no write, session stays OFF) when the config is incomplete
    or invalid — the OFF judgement is never softened by the writer.

    This is an **explicit programmatic** enable, so the blob it writes carries ``origin="api"`` —
    an auditable stamp, not a bare/un-marked blob (a missing stamp is now NO authorisation).
    """
    parsed = _config.parse(config_raw)
    if not parsed.ok:
        logger.warning("proactive-conversation: refusing to enable %s: %s%s",
                       _content.short_digest(session_key), parsed.reason,
                       f" ({parsed.field})" if parsed.field else "")
        return False
    now = _clock()
    state = policy.new_state(now, parsed.config, _rng)
    return store.write(store_handle, session_key, config_raw=dict(config_raw),
                       state=state, dims=dims, origin=C.ORIGIN_API)


def pause_session(store_handle: Any, session_key: str, dims: Dict[str, Any]) -> bool:
    """Pause a configured session (zero releases; state and counters are kept)."""
    read = store.read(store_handle, session_key, dims)
    if not read.usable:
        return False
    state = policy.pause(read.state, _clock())
    return store.write(store_handle, session_key, config_raw=read.config_raw, state=state, dims=dims,
                       origin=read.origin)


def resume_session(store_handle: Any, session_key: str, dims: Dict[str, Any]) -> bool:
    """Resume a paused session: the window is re-sampled from the user's most recent message."""
    read = store.read(store_handle, session_key, dims)
    if not read.usable:
        return False
    state = policy.resume(read.state, read.config, _clock(), _rng)
    return store.write(store_handle, session_key, config_raw=read.config_raw, state=state, dims=dims,
                       origin=read.origin)


def revoke_session(store_handle: Any, session_key: str) -> bool:
    """Permanently close a session: the plugin's per-session value is cleared to ``null``.

    The in-flight release ticket is deliberately left alone: a released turn must still be able to
    *see* the closure at the delivery gate and silence itself. Dropping the ticket here would make
    the round indistinguishable from an ordinary turn and let the message through.
    """
    return store.clear(store_handle, session_key)
