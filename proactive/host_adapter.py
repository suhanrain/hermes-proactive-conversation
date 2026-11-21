"""Named Hermes host seams already isolated from the plugin's decision code.

This module translates plugin settings, manifest fields, completions, silence rules,
wake stamps, transcript rows, inbound identity, inbound payload, the session-metadata
protocol, the inbound source's own `to_dict()` description and enum-shaped values, the
process home, the host's isolation names and the hook-name table; it is the **single
owner of the plugin context** (``runtime`` keeps no copy). It is not the whole host
boundary: ``runtime`` parses the session-key namespace, ``__init__.py`` calls
``ctx.register_hook``, and ``proactive/sources/`` reads the host's SQLite schemas.
A predicate here cannot intercept internal-authority wakes that the host never sends to
``pre_gateway_dispatch``.

Each seam keeps its fail-safe answer; centralizing it does not
change the host's hook or delivery behavior:

======================================  ==============================  ==============================
seam                                   host surface                   unreadable host ⇒
======================================  ==============================  ==============================
:func:`config`                        ``ctx.get_config(key, default)``  the caller's default
:func:`manifest_source`               ``ctx.manifest.source``           ``""`` (no evidence)
:func:`manifest_path`                  ``ctx.manifest.path``             ``None`` (no home)
:func:`llm`                           ``ctx.llm``                       ``None`` (no façade)
:func:`host_silence_verdict`          ``gateway.response_filters``      ``False`` (not silence)
:func:`host_silence_is_legal`         ``gateway.response_filters``      ``False`` (not legal)
:func:`is_wake_event`                 ``event._heartbeat_session_id``   ``False`` (not a wake)
======================================  ==============================  ==============================

Log hygiene is unchanged and centralised too: every line here names a digest or an
exception *type*, never a session key, a chat id, a message body, or an exception body.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# ── the plugin context (bound once, at register) ──────────────────────────────────────────────

_ctx: Any = None


def bind_context(ctx: Any) -> None:
    """Bind the host plugin context. Called once from ``register()``.

    This adapter is the **single owner** of the context:
    ``runtime.bind_plugin_context`` forwards here and ``runtime.plugin_context()`` reads
    :func:`context`, so no second copy exists in the package. A context missing a surface
    leaves that seam at its fail-safe answer; nothing here raises on a recording context
    (the capability probe, ``plugins doctor``).
    """
    global _ctx
    _ctx = ctx


def context() -> Any:
    """The bound context (diagnostics/tests); ``None`` before ``register()``."""
    return _ctx


def reset_context() -> None:
    """Drop the bound context: every seam falls back to its documented default."""
    global _ctx
    _ctx = None


# ── seam 1: configuration (``ctx.get_config``) ────────────────────────────────────────────────


def config(key: str, default: Any = None) -> Any:
    """One plugin setting through the host's config surface.

    ``ctx.get_config(key, default)`` resolves ``plugins.entries.<plugin-id>.settings.<key>``
    on every call, so an operator's ``config.yaml`` edit applies to the next round with no
    reload. A context without the surface, a non-callable one, or a raising one answers
    *default* — never a guess, never a raise (the caller decides what an absent value
    means).
    """
    reader = getattr(_ctx, "get_config", None)
    if not callable(reader):
        return default
    try:
        return reader(key, default)
    except Exception as exc:  # an unreadable config is "not set", never a crash
        logger.warning("proactive-conversation: reading plugin setting %s failed (%s)", key,
                       type(exc).__name__)
        return default


def config_reader() -> Optional[Callable[[str, Any], Any]]:
    """The bound reader itself, for the settings module's sentinel-aware path.

    ``proactive.settings`` needs to tell "the key is not set" (``MISSING``) from "the key
    holds the default value", which ``config()`` alone cannot express; it therefore binds
    this reader directly. Everything else in the package goes through :func:`config`.
    """
    reader = getattr(_ctx, "get_config", None)
    return reader if callable(reader) else None


# ── seam 2: this instance's identity (``ctx.manifest``) ───────────────────────────────────────


def _manifest() -> Any:
    return getattr(_ctx, "manifest", None)


def manifest_source() -> str:
    """Hermes' own discovery-source string for this instance (``user`` / ``project`` /
    ``bundled`` / ``entrypoint``), or ``""`` when the context carries no manifest.

    ``""`` means "no evidence", never "default profile": a data root must be *proven*.
    """
    manifest = _manifest()
    if manifest is None:
        return ""
    return str(getattr(manifest, "source", "") or "").strip()


def manifest_path() -> Any:
    """The plugin's own directory (``PluginManifest.path``), or ``None``."""
    manifest = _manifest()
    return getattr(manifest, "path", None) if manifest is not None else None


def manifest_name() -> Any:
    """The plugin's own manifest name, or ``None``."""
    manifest = _manifest()
    return getattr(manifest, "name", None) if manifest is not None else None


# ── seam 3: the host's LLM façade (``ctx.llm``) ───────────────────────────────────────────────


def llm() -> Any:
    """The host-owned completion façade (``ctx.llm.complete``), or ``None``.

    The façade owns routing, auth, timeouts and fallback; this plugin passes messages and
    a purpose, never a provider, a model, or anything resembling a credential. A context
    without the façade (a recording context, a broken install) answers ``None`` and the
    caller's own "no generator" path decides the round.
    """
    return getattr(_ctx, "llm", None)


# ── seam 4: the host's silence rules (``gateway.response_filters``) ───────────────────────────
#
# The exemption below spends nothing (no delivery, no counter), so it may only be claimed
# where the host's own delivery decision is the same silence. Both halves are therefore read
# from the host module instead of copied into this package: a private list of markers, or a
# private copy of the machinery-lane rule, would drift from the gateway and then quietly
# exempt rounds the gateway sends.


def host_silence_verdict(text: Any) -> bool:
    """True only when the host's own rule calls this **whole** response an intentional silence.

    ``gateway.response_filters.is_intentional_silence_response`` is the exact-response rule
    the DM lane uses (``is_intentional_silence_agent_result``); it normalises edge
    whitespace/punctuation and case, and it deliberately does not match prose that merely
    mentions a marker, nor a blank response. Asking the host means the plugin cannot exempt
    a text the host would deliver.

    A host that cannot be asked answers "not silence": the round is settled and delivered,
    never a free round.
    """
    try:
        from gateway.response_filters import is_intentional_silence_response
    except Exception as exc:  # only reachable on a broken host install
        logger.warning("proactive-conversation: cannot ask the host about silence (%s); treating the "
                       "response as a normal one", type(exc).__name__)
        return False
    try:
        return bool(is_intentional_silence_response(text))
    except Exception as exc:  # a raising predicate is not a verdict
        logger.warning("proactive-conversation: the host's silence predicate raised (%s); treating the "
                       "response as a normal one", type(exc).__name__)
        return False


def host_silence_is_legal(event: Any) -> bool:
    """Whether the host's rules would honour a bare marker on **this** released round's lane.

    The gateway decides that from the persisted user row's display kind and the event's
    ``reply_expected`` (``gateway/run_turn_persistence.py``: ``silence_allowed(...)``); a
    round that fails it is answered with the host's own "silence marker rejected" text,
    i.e. a message the user *does* receive. Exempting such a round would leave a delivered
    message uncounted, so it is not exempted. Unreadable host → not legal (conservative).
    """
    try:
        from gateway.response_filters import display_kind_for_event, silence_allowed
    except Exception as exc:  # only reachable on a broken host install
        logger.warning("proactive-conversation: cannot ask the host about this round's silence rules "
                       "(%s); its silence will not be exempted", type(exc).__name__)
        return False
    try:
        return bool(silence_allowed(display_kind_for_event(event),
                                    getattr(event, "reply_expected", None)))
    except Exception as exc:  # a raising host helper is not a verdict
        logger.warning("proactive-conversation: the host's display-kind helper raised (%s); this "
                       "round's silence will not be exempted", type(exc).__name__)
        return False


# ── seam 5: what a wake IS (the event's own stamp) ────────────────────────────────────────────
#
# The gateway-injected heartbeat wake carries ``event._heartbeat_session_id`` (set by
# ``gateway/run_goals.py::_heartbeat_poll_watch``) and is a NON-internal event, so it
# reaches ``pre_gateway_dispatch`` and this plugin's gate. A gateway with a live
# ``session_authority`` instead admits the wake through
# ``gateway/run_heartbeat_acceptance.py::admit_heartbeat`` as an **internal** event, and
# ``gateway/run_inbound.py`` answers an internal event *before* the
# ``pre_gateway_dispatch`` hook — on such a gateway a wake never reaches this plugin at all.
#
# Therefore: the predicate below is the package's ONLY definition of a wake. Extending it
# for the internal channel (recognising the automation identity the authority path commits,
# ``session_automation.py``: ``native_text_v1.automation.heartbeat``) is a one-place change
# this module does not make.


def is_wake_event(event: Any) -> bool:
    """True for a gateway-injected heartbeat wake — the plugin's only wake definition.

    ``_heartbeat_session_id`` is only ever set by the heartbeat poller; it is never
    inferred from message text, so a user who types "[Heartbeat]" is not a wake. A missing
    or falsy stamp is not a wake: guessing would either block ordinary user traffic or
    release wakes the user never enabled.
    """
    return bool(getattr(event, "_heartbeat_session_id", None))


def wake_session_id(event: Any) -> Optional[str]:
    """The session id stamped on a wake event, or ``None`` when it carries no stamp."""
    value = getattr(event, "_heartbeat_session_id", None)
    return str(value) if value else None


def wake_internal_channel(event: Any) -> bool:
    """Whether *event* arrived on the host's internal automation channel.

    Diagnostic seam: the authority path admits wakes as ``internal=True`` events whose
    payload carries the automation identity (``gateway_session_id`` in metadata). This
    answers a fact, not a verdict — the package's gate only acts on :func:`is_wake_event`.
    """
    if not bool(getattr(event, "internal", False)):
        return False
    metadata = getattr(event, "metadata", None) or {}
    return bool(isinstance(metadata, dict) and metadata.get("gateway_session_id"))


# ── seam 6: the session transcript ───────────────────────────────────────────────────────────
#
# The generation request carries the conversation, as a bounded window (N rounds, N configurable)
# rather than a full dump, so consecutive proactive messages do not repeat themselves for want of
# context. The privacy constraints ride inside the frame.
#
# The read is the adapter's job because the transcript is host-owned data: the plugin's own
# ``proactive.sources.session_transcript`` already opens the session database read-only for the
# *candidate* path, and this seam reuses exactly that machinery (same read-only helper, same
# never-writes, never-raises contract) for the *context* path.

#: Both roles, oldest-first. A row's role is whatever the store says; the frame in
# ``content.render_context_block`` labels them for the model.
TranscriptRow = Tuple[str, str]

_CONTEXT_SQL = """
SELECT role, content
FROM messages
WHERE session_id = ?
  AND role IN ('user', 'assistant')
  AND content IS NOT NULL
  AND TRIM(content) <> ''
  AND active = 1
  AND compacted = 0
ORDER BY id DESC
LIMIT ?
"""


def recent_transcript(session_id: Any, limit: Any = 10, *, db_path: Any = None) -> List[TranscriptRow]:
    """The last ``limit`` rounds of this session's transcript, oldest-first.

    ``session_id`` — the session whose rows are wanted. An empty or unknown id answers ``[]``.
    ``limit`` — how many rows; coerced through the same rules as every numeric setting
    (a bool, a fraction, a non-positive or an over-bound value falls back to the documented
    default, :data:`proactive.contract.SESSION_CONTEXT_ROUNDS_DEFAULT`).
    ``db_path`` — the profile's session database. Without one the seam answers ``[]``: the
    caller passes the path the composition resolved (the adapter never guesses a data root).

    Never raises, never writes, never opens the database writable: an unreadable store answers
    ``[]`` with one warning naming the exception type (a store's error text is third-party
    data), and the generation then runs with the material block alone, never a broken round.
    """
    from . import contract as C

    key = str(session_id or "").strip()
    if not key or db_path is None:
        return []
    rounds = _coerce_rounds(limit)
    if rounds <= 0:
        return []
    try:
        from .sources.sqlite_readonly import open_read_only

        conn = open_read_only(db_path)
    except Exception as exc:  # a store that cannot be opened is an empty window, not a fault
        logger.warning("proactive-conversation: the session store for the context window could not be "
                       "opened read-only (%s)", type(exc).__name__)
        return []
    try:
        rows = conn.execute(_CONTEXT_SQL, (key, rounds)).fetchall()
    except Exception as exc:  # same rule: an unreadable window is an empty one
        logger.warning("proactive-conversation: the session context window could not be read (%s)",
                       type(exc).__name__)
        return []
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - a close failure must not mask the read's answer
            pass
    out: List[TranscriptRow] = []
    for role, content in reversed(rows):  # newest-first from SQL → oldest-first for the model
        text = str(content or "").strip()
        if text:
            out.append((str(role or ""), text))
    return out


def _coerce_rounds(limit: Any) -> int:
    """``limit`` as a positive int, or the documented default when it is unusable.

    The same resolution order as :mod:`proactive.settings`: a bool is not a number here, a
    fraction is not a round, and an over-bound value is a configuration fault rather than a
    licence to dump a whole session.
    """
    from . import contract as C

    if isinstance(limit, bool) or not isinstance(limit, (int, float)):
        return C.SESSION_CONTEXT_ROUNDS_DEFAULT
    if isinstance(limit, float) and not float(limit).is_integer():
        # a fraction is not a round: the same refusal settings.resolve_number applies to a
        # fractional TTL, kept in step deliberately
        return C.SESSION_CONTEXT_ROUNDS_DEFAULT
    value = int(limit)
    if value < 1 or value > C.SESSION_CONTEXT_ROUNDS_MAX:
        return C.SESSION_CONTEXT_ROUNDS_DEFAULT
    return value


# ── seam 7: the inbound event's identity ─────────────────────────────────────────────────────
#
# `runtime._dispatch` reads no host event, gateway or session store itself: the host field names
# (`event.metadata["gateway_session_key"]`, a private `gateway._session_key_for_source` resolver,
# `store.peek_session_id`, and `source.platform.value` / `profile` / `chat_id` / `thread_id`) live
# here, and the caller receives plain data: the key, the optional session id, and the six
# dimensions the blob stores.
#
# Parity is the whole point: metadata first, the private resolver only as a fallback, a raising
# resolver or probe treated as "no answer", an enum platform normalised to its value, every other
# field passed through untouched. Nothing here classifies an event — the wake verdict is seam 5's
# job, and a user message whose text copies the heartbeat prompt stays a user message.

#: The dimension keys the persisted blob carries, in ``proactive.store.dims_for``'s order.
DIMENSION_KEYS = ("session_key", "session_id", "profile", "platform", "chat_id", "thread_id")


@dataclass(frozen=True)
class EventIdentity:
    """One inbound identity in a plain Python record, rather than the host event itself.

    ``session_key`` and ``session_id`` are strings or ``None``. ``dims`` has the six dimension keys
    in :data:`DIMENSION_KEYS` order; its field values are preserved from the host unchanged to keep
    the existing fingerprint semantics, rather than guessed or coerced into new defaults.
    """

    session_key: Optional[str] = None
    session_id: Optional[str] = None
    dims: Dict[str, Any] = field(default_factory=dict)


def session_key_from(event: Any, gateway: Any = None) -> Optional[str]:
    """The event's session key: its own metadata first, then the gateway's private resolver."""
    metadata = getattr(event, "metadata", None) or {}
    key = metadata.get("gateway_session_key") if isinstance(metadata, dict) else None
    if key:
        return str(key)
    source = getattr(event, "source", None)
    resolver = getattr(gateway, "_session_key_for_source", None)
    if source is not None and callable(resolver):
        try:
            resolved = resolver(source)
        except Exception:  # a resolver that raises answers nothing; it is not a verdict
            return None
        if resolved:
            return str(resolved)
    return None


def session_id_from(store: Any, session_key: Any) -> Optional[str]:
    """The session's current id through the store's ``peek_session_id`` probe, or ``None``.

    The probe is read-only by contract; a store without it (a recording fake, a broken install) or
    one that raises answers "unknown", which the caller already treats as "cannot account for the
    round" rather than as a reason to guess.
    """
    peek = getattr(store, "peek_session_id", None)
    if not callable(peek):
        return None
    try:
        session_id = peek(session_key)
    except Exception:
        return None
    return str(session_id) if session_id else None


def dimensions_from(event: Any, session_key: Any, session_id: Any) -> Dict[str, Any]:
    """The six dimensions of one event, in blob order, with the platform normalised to its value.

    ``source.platform`` is an enum in the host and a string in a recording fake; both are accepted,
    and a falsy platform (no source, or one without it) stays ``None``.
    """
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    normalised = getattr(platform, "value", None) or (str(platform) if platform else None)
    return {
        "session_key": session_key,
        "session_id": session_id,
        "profile": getattr(source, "profile", None),
        "platform": normalised,
        "chat_id": getattr(source, "chat_id", None),
        "thread_id": getattr(source, "thread_id", None),
    }


def event_identity(event: Any, gateway: Any = None, store: Any = None) -> EventIdentity:
    """Translate one inbound event into :class:`EventIdentity` — the package's identity seam."""
    key = session_key_from(event, gateway)
    # The inbound hook returns before probing the store when no session key can be proven.
    session_id = session_id_from(store, key) if key else None
    return EventIdentity(session_key=key, session_id=session_id,
                         dims=dimensions_from(event, key, session_id))


# ── seam 8: the inbound payload ──────────────────────────────────────────────────────────────
#
# The release path needs five things from the inbound round: the session store to read and write,
# the event's ``source`` (handed to the consume queue, which canonicalises it itself), its
# ``message_id`` and ``text`` (the fingerprint's exact inputs), and the message's own timestamp.
# ``runtime`` reads none of them directly: the two ``getattr(event, "source", None)`` sites, the
# fingerprint's ``getattr(event, "message_id"/"text", ...)``, the ``getattr(gateway,
# "session_store", None)`` fallback and the ``event.timestamp`` localisation all read through
# this seam.
#
# Parity rules, deliberately strict:
#
# * the hook's ``session_store`` argument wins; the gateway attribute is only the fallback;
# * ``source`` is returned **by identity** — the queue's canonicalisation and the ticket fingerprint
#   must see exactly the object the host presented;
# * ``message_id`` and ``text`` are returned untouched, ``None`` included: trimming or stringifying
#   here would change the fingerprint and could match the wrong ticket;
# * a timestamp is converted to epoch seconds the way the gateway does it (a naive datetime is
#   host-local, resolved with ``astimezone()``), and anything that is not a datetime answers
#   ``None``. The *sanity band* is a plugin setting and stays in ``runtime``.


def store_handle_from(session_store: Any, gateway: Any) -> Any:
    """The session store this round reads and writes: the hook argument, else the gateway's."""
    if session_store is not None:
        return session_store
    return getattr(gateway, "session_store", None)


def event_source(event: Any) -> Any:
    """The event's source object, by identity (``None`` when the event carries none)."""
    return getattr(event, "source", None)


def event_message_id(event: Any) -> Any:
    """The event's platform message id, untouched (``None`` for a heartbeat wake)."""
    return getattr(event, "message_id", None)


def event_text(event: Any) -> Any:
    """The event's text, untouched — it is a fingerprint input, so no trimming or coercion."""
    return getattr(event, "text", None)


def event_timestamp_epoch(event: Any) -> Optional[float]:
    """The message's own time as epoch seconds, or ``None`` when it is not a usable datetime.

    A naive ``datetime`` is host-local — exactly how ``MessageEvent`` builds it — so it is resolved
    with ``astimezone()``, which is the same rule the gateway's ``message_timestamps._localize``
    applies. The caller decides whether the value is within its sanity band.
    """
    value = getattr(event, "timestamp", None)
    if not isinstance(value, datetime):
        return None
    try:
        return float(value.astimezone().timestamp() if value.tzinfo is None else value.timestamp())
    except Exception:  # an exotic datetime subclass is "no timestamp", never a failed round
        return None


# ── seam 9: the session-metadata protocol ────────────────────────────────────────────────────
#
# The host's `SessionStore` exposes one JSON value per (session_key, meta_key) pair:
# `get_session_metadata(key, meta_key, default)` answers the caller's default when the key is absent,
# `None` when it holds JSON `null` (a permanent close), and the value otherwise;
# `set_session_metadata(key, meta_key, value)` replaces the whole value and answers truthiness.
# Both calls live here, so the *protocol shape* is named in one place, while every decision stays
# in `store.py`: the sentinels that tell absent from closed from unreadable, the `KEY_*` verdicts,
# the close-before-write ordering under its lock, and the "exception type only" log line for a
# fault. Nothing here catches an exception — a store that raises must reach `store.py`, which is
# the module that decides what a fault means.
#
# Both `store.py` and `delivery_guard.py` reach the protocol through here, so the shape is named in
# exactly one place; what `delivery_guard` keeps is its own private sentinel, because "closed" and
# "never configured" are different refusals for the gate.


def read_session_metadata(store: Any, session_key: Any, meta_key: str, default: Any) -> Any:
    """Read the plugin's value for one session, answering *default* when the key is absent."""
    return store.get_session_metadata(session_key, meta_key, default)


def write_session_metadata(store: Any, session_key: Any, meta_key: str, value: Any) -> bool:
    """Replace the plugin's value for one session; ``True`` means the store accepted it."""
    return bool(store.set_session_metadata(session_key, meta_key, value))


# ── seam 10: the inbound source as a plain dict ──────────────────────────────────────────────
#
# The host's `SessionSource` describes itself with `to_dict()`; the consumer side of the queue only
# ever has that dict. The adapter translates through this seam, so the host's shape is named once.
# dict passes through **by identity** (fingerprints hash it, so a copy would still hash the same,
# but callers compare objects), `None` is `{}`, and anything unusable — no `to_dict`, a `to_dict()`
# that is not a dict, or one that raises — is `{}` rather than a route.


def source_as_dict(source: Any) -> Dict[str, Any]:
    """``SessionSource`` | ``dict`` | ``None`` → a plain dict, the same way on both hooks."""
    if source is None:
        return {}
    if isinstance(source, dict):
        return source
    try:
        as_dict = getattr(source, "to_dict", None)
        if callable(as_dict):
            described = as_dict()
            return described if isinstance(described, dict) else {}
    except Exception:  # noqa: BLE001 - a source that cannot describe itself is not a route
        return {}
    return {}


# ── seam 11: the process home ───────────────────────────────────────────────────────────────
#
# Hermes anchors a process on one home: `HERMES_HOME` when the environment names one, otherwise its own
# resolution (`hermes_constants.get_hermes_home()`, lazily imported here and nowhere else). The plugin
# reads this only as the **bare-interpreter fallback** for a data root: a process that serves several
# profiles answers with the global home, which would attribute a round to the wrong data root, so an
# instance's own manifest always wins (`composition.resolve_home`). The environment branch is *not*
# protected, so a value `Path` rejects still raises to the caller; only the host lookup is guarded,
# and its failure means "no answer" (`None`).


def process_home() -> Optional[Path]:
    """The home this *process* is anchored on, or ``None`` when no host module can answer."""
    env = str(os.environ.get("HERMES_HOME", "") or "").strip()
    if env:
        return Path(env)
    try:  # the host's own resolution; exercised by the seam's cases when the host is on the path
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - no host, no answer; the caller has its own last resort
        return None


# ── seam 12: the isolation detector's host names ────────────────────────────────────────────
#
# Hermes decides *where* a plugin runs, and the two shapes that matter are named here rather than
# inside `proactive/isolation.py`: the environment flag its plugin-host child sets, the context class
# it hands `register()` in that child, and the two wire-codec class names a value crosses the process
# boundary as (`hermes_cli/plugin_host_wire.py`). The detector's *decisions* stay in `isolation.py` —
# this module only knows what the host calls things.

#: Environment variable ``hermes_cli.plugin_host.PluginHost._env`` sets for the plugin-host child.
HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"

#: The concrete ``ctx`` class the host hands ``register()`` (``hermes_cli.plugin_host_child``).
HOST_CONTEXT_TYPE = "RemotePluginContext"

#: Wire-codec class names: a dataclass becomes ``Record``, any live handle becomes ``Opaque``.
WIRE_RECORD_TYPE = "Record"
WIRE_PLACEHOLDER_TYPE = "Opaque"


def environ_value(name: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """One environment entry, stripped; ``""`` when absent or when the mapping cannot be read."""
    source: Mapping[str, str] = os.environ if environ is None else environ
    try:
        return str(source.get(name) or "").strip()
    except Exception:  # noqa: BLE001 - an exotic mapping must not turn a detector into an exception
        return ""


def host_process_flag(environ: Optional[Mapping[str, str]] = None) -> bool:
    """True when the env flag only hermes' plugin-host child carries is exactly ``1``."""
    return environ_value(HOST_PROCESS_ENV, environ) == "1"


def platform_value(platform: Any) -> Any:
    """An enum-shaped platform's ``value``, else the object itself.

    ``getattr(platform, "value", platform)`` only falls back on ``AttributeError`` — a ``value``
    property that raises still reaches the caller, and that is intended.
    """
    return getattr(platform, "value", platform)


# ── seam 13: hook registration ──────────────────────────────────────────────────────────────
#
# `register()` hands the host one callback per declared hook. That call is the package's only direct
# `ctx.<method>` invocation outside this module, so it lives here: the names come from
# `hook_names()`, the callbacks from `runtime`, and this seam only performs the registration. It never
# catches — a context that refuses a hook fails loudly at load time.


def register_hooks(ctx: Any, hooks: Iterable[Tuple[str, Callable[..., Any]]]) -> None:
    """Register each ``(name, callback)`` pair with the host, in order."""
    for name, callback in hooks:
        ctx.register_hook(name, callback)


# ── seam 14: the session-key layout ──────────────────────────────────────────────────────────
#
# `gateway/session.py::build_session_key` joins the namespace (`agent:main` for the default profile,
# `agent:<name>` otherwise, `agent:main~` for a profile literally named `main`), the platform value and
# the chat type into `<ns>:<platform>:<chat_type>…`. Its inverse,
# `profile_from_session_key_namespace`, is what core uses to route notifications and recovery, so the
# slot is a gateway-issued fact — which is why the shape is parsed here, next to the other host names,
# and not in `runtime`.

#: The namespace prefix a gateway-built session key carries.
_SESSION_KEY_PREFIX = "agent"

#: The chat-type slot the gateway writes at index 3 (`SessionSource.chat_type` plus the thread/topic
#: renames). A *closed set*, used as a shape check: a key in an older or unexpected layout must not be
#: read as if its second slot were a profile.
_SESSION_KEY_CHAT_TYPES = frozenset({"dm", "group", "thread", "channel"})

#: What the core's default (non-multiplex) namespace means: ``agent:main`` is byte-identical for the default
#: profile, so it is evidence for "the default profile" and nothing more.
_SESSION_KEY_DEFAULT_NAMESPACE = "main"

#: The namespace written for a profile literally named ``main``.
_SESSION_KEY_ESCAPED_MAIN = "main~"


def profile_from_session_key(session_key: Any) -> Optional[str]:
    """The profile a session key *proves* the round belongs to, or ``None``.

    ``agent:main`` → ``"default"`` (core's own reading of its default namespace); ``agent:<name>`` →
    ``"<name>"``; ``agent:main~`` → ``"main"``. Anything whose shape cannot be checked — an
    unnamespaced, hand-built or unprintable key — answers ``None``: the scope did not say, and guessing
    a profile from an unknown layout is exactly the inference this rule forbids.
    """
    try:
        parts = str(session_key or "").split(":")
    except Exception:  # noqa: BLE001 - a key that cannot be printed proves nothing
        return None
    if len(parts) < 4 or parts[0] != _SESSION_KEY_PREFIX:
        return None
    if parts[2] in _SESSION_KEY_CHAT_TYPES or parts[3] not in _SESSION_KEY_CHAT_TYPES:
        return None  # not ``agent:<slot>:<platform>:<chat_type>``: no profile slot to read
    namespace = parts[1]
    if not namespace:
        return None
    if namespace == _SESSION_KEY_DEFAULT_NAMESPACE:
        return "default"
    return "main" if namespace == _SESSION_KEY_ESCAPED_MAIN else namespace


# ── seam 15: the transcript schema ──────────────────────────────────────────────────────────
#
# Hermes keeps each profile's session history in `state.db`: a `sessions` row naming the session key and
# profile, and `messages` rows for the turns. The schema is the host's, so it lives here: which tables,
# which columns, which `display_kind` values are *not* the user's own words, the order, and the limit.
# The source keeps the decisions that are the plugin's (scope/profile refusals, windows, the future
# guard, the usability rules, the refusal codes) and the read-only connection.

#: ``messages.display_kind`` values the gateway writes for turns that are *not* the user's own words:
#: ``internal_notification`` is Hermes' own ``MACHINERY_DISPLAY_KINDS`` member (a self-injected /
#: heartbeat turn), ``hidden`` and ``failed_turn`` are rows the platform never showed the user. A row
#: carrying one of these may not become a candidate sentence.
EXCLUDED_DISPLAY_KINDS: Tuple[str, ...] = ("internal_notification", "hidden", "failed_turn")

#: The session row: the key it is stored under, and the profile the database belongs to.
TRANSCRIPT_SESSION_SQL = "SELECT session_key, profile_name FROM sessions WHERE id = ?"

#: The user's own usable turns in the window, newest first. The parameters, in order: the session id,
#: then :data:`EXCLUDED_DISPLAY_KINDS`, then the window's lower and upper bounds, then the row limit.
TRANSCRIPT_MESSAGES_SQL = f"""
SELECT id, content, timestamp, _compressed_summary
FROM messages
WHERE session_id = ?
  AND role = 'user'
  AND content IS NOT NULL
  AND TRIM(content) <> ''
  AND active = 1
  AND compacted = 0
  AND (display_kind IS NULL OR display_kind NOT IN ({", ".join("?" * len(EXCLUDED_DISPLAY_KINDS))}))
  AND timestamp >= ?
  AND timestamp <= ?
ORDER BY timestamp DESC, id DESC
LIMIT ?
"""


# ── the hook-shaped surface ──────────────────────────────────────────────────────────────────


def hook_names() -> Tuple[str, ...]:
    """The hooks this plugin registers, in registration order (``plugin.yaml`` mirrors it).

    Kept here so a host-side rename or a new seam has one obvious home; ``__init__.py``
    reads the names through this tuple.
    """
    return ("pre_gateway_dispatch", "transform_llm_output",
            "pre_tool_call", "pre_llm_call")


# ── seam 16: the injection trigger ──────────────────────────────────────────────────────────
#
# The plugin asks the host to run an **ordinary full Hermes turn** in a session through the
# documented ``ctx.inject_message`` API. ``True`` means *accepted for asynchronous dispatch* — not
# turn completion and not delivery — so the caller must never read it as "sent". The host's own
# consent gate applies on the gateway path (``allow_gateway_injection``); a ``False`` from the host,
# a missing surface, or a raising call is a **clean refusal** the caller counts as zero. The
# failure direction is deliberately the conservative one: a plugin that cannot ask the host does
# not speak, and never raises into the host's dispatch.

INJECT_ROLE_USER = "user"


def inject_message(text: Any, session_key: Any = None, *, role: str = INJECT_ROLE_USER) -> bool:
    """Ask the host to start an ordinary turn carrying *text* in *session_key*; returns the host bool.

    This is the **only** host call the plugin makes to trigger a proactive turn; the decision (gate,
    settle, retire) stays in ``proactive.trigger``. A context without ``inject_message`` (a recording
    context, an older host), a non-callable one, or one that raises answers ``False`` — the round is
    not counted and one warning names the exception *type* (never a body).
    """
    fn = getattr(_ctx, "inject_message", None)
    if not callable(fn):
        return False
    try:
        return bool(fn(str(text), role=role, session_key=(str(session_key) if session_key else None)))
    except Exception as exc:  # a raising host call is a refusal, never a crash in dispatch
        logger.warning("proactive-conversation: ctx.inject_message raised (%s); treating the round as refused",
                       type(exc).__name__)
        return False
