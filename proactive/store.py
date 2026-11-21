"""Per-session persistence over the gateway ``SessionStore``'s per-session metadata.

One key holds everything::

    proactive-conversation:session = {
        "version": 1,
        "dims":   {"session_key": ..., "session_id": ..., "profile": ..., "platform": ...,
                   "chat_id": ..., "thread_id": ...},
        "config": {...},          # the user's per-session configuration, verbatim
        "state":  {...},          # proactive.state.SessionState
        "origin": "declaration",  # OPTIONAL: absent = the metadata key itself authorised this
    }

Why one key: one read is one consistent view (config + state + dimension fingerprint), and
``set_session_metadata`` replaces the whole value, so an update can never half-apply.

Rules enforced here:

* **No inheritance across dimensions.** The stored ``dims`` must equal the current dimensions
  exactly; any difference (profile / platform / chat / thread / session) makes the blob
  invisible, i.e. the session is OFF. A reset or a switch therefore never inherits an
  authorisation.
* **No version guessing.** A blob whose ``version`` is not ``contract.BLOB_VERSION`` is ignored.
* **Permanent close clears everything.** :func:`clear` overwrites the key with JSON ``null``, so
  no readable enabled marker survives; readers treat ``null`` exactly like "no blob". The one
  exception is :func:`key_state` / :func:`closed`, which expose the key state so the declaration
  mirror can tell "closed" from "never written": a declaration is an authorisation *source* and must
  not re-open a session the operator closed. ``read`` keeps the "null = no blob" invariant for every
  other caller.
* **A close outranks every write of this plugin.** :func:`write_ordered` (which :func:`write` is a
  thin wrapper of) re-probes the key *inside* one process-wide lock and refuses to store anything
  when the key holds ``null`` — and also when the key cannot be read at all, because "could not
  confirm this is not a close" must never authorise a write. :func:`clear` takes the same lock, so
  a close and a mirror cannot interleave: either the close is seen and the write is refused, or the
  close lands after the write and its ``null`` stands.
* **A blob must carry an explicit authorisation stamp.** ``origin`` is REQUIRED for a session to be
  authorised: ``"declaration"`` (mirrored from ``session_declarations``) or ``"api"`` (an explicit
  ``enable_session``). A blob with the field **missing** is NOT an authorisation: only a config
  declaration (or an explicit API enable) can turn a session on. Every writer must pass the origin it
  just read back in, so a user message, a skip, a settlement or a pause never silently drops the stamp.
  A present but unusable marker (``null``, ``""``, a number, an unknown string) makes the blob
  invalid → OFF (:data:`_ORIGIN_INVALID`).
* **Nothing here logs an identifier or an untrusted exception body.** Refusals and store faults are
  logged with an irreversible short digest and the exception's *type* — never with the plaintext
  ``session_key``/``session_id`` and never with ``str(exc)``, because a store's error text is
  third-party data that may quote the key it was given.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from . import config as _config
from . import contract as C
from . import host_adapter as _adapter
from .state import SessionState

logger = logging.getLogger(__name__)

#: Salt for :func:`short_digest`. Logs must never be linkable back to a session identifier, and a
#: digest that is stable within a line (so two lines about the same session can be told apart from
#: two different sessions) is all a diagnosis needs.
_DIGEST_SALT = b"proactive-conversation/store/log-digest/v1"


def short_digest(value: Any) -> str:
    """An irreversible 12-hex-char digest of *value* — the only form of an id that may be logged."""
    text = "" if value is None else str(value)
    return hmac.new(_DIGEST_SALT, text.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:12]

#: Dimension keys whose change must never inherit an enablement.
DIM_KEYS = ("session_key", "session_id", "profile", "platform", "chat_id", "thread_id")


def dims_for(
    *,
    session_key: str,
    session_id: Optional[str] = None,
    profile: Optional[str] = None,
    platform: Optional[str] = None,
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The session's dimension fingerprint, as persisted alongside the blob."""
    return {
        "session_key": session_key,
        "session_id": session_id,
        "profile": profile,
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }


def dims_match(stored: Any, current: Any) -> bool:
    """True only when every dimension is present and identical."""
    if not isinstance(stored, dict) or not isinstance(current, dict):
        return False
    for key in DIM_KEYS:
        if key not in stored or key not in current:
            return False
        if stored[key] != current[key]:
            return False
    return True


@dataclass(frozen=True)
class SessionRead:
    """The result of one per-session read.

    ``reason`` is ``""`` only when the blob exists, matches this session's dimensions, has a known
    version and carries a readable state; otherwise it is one of the ``contract.REASON_OFF*``
    codes and the session is OFF.

    ``origin`` is the blob's authorisation source: ``contract.ORIGIN_DECLARATION`` (a declaration
    mirrored it) or ``contract.ORIGIN_API`` (an explicit ``enable_session``). A blob with no valid
    stamp is not authorised and reads as ``off:undeclared``.
    """

    config: Optional[_config.SessionConfig] = None
    state: Optional[SessionState] = None
    config_raw: Optional[Dict[str, Any]] = None
    dims: Dict[str, Any] = field(default_factory=dict)
    reason: str = C.REASON_NO_BLOB
    field_name: str = ""
    origin: str = ""

    @property
    def usable(self) -> bool:
        return self.reason == "" and self.config is not None and self.state is not None


def read_probed(store: Any, session_key: str, dims: Dict[str, Any]) -> Tuple[SessionRead, str]:
    """One probe: ``(read, key_state)`` — the validated blob *and* what the key itself turned out to be.

    The two answers come from the **same** ``get_session_metadata`` call on purpose. Deriving them
    separately means a store that fails the second read can turn an existing (say, paused) blob into
    "nothing to carry over" — the caller then invents a fresh state and overwrites a live session
    with it. One probe cannot disagree with itself, and a failed probe is reported as
    :data:`KEY_UNREADABLE` (never as "absent"), so the declaration path refuses instead of writing.
    """
    raw = _probe(store, session_key)
    kstate = _state_of(raw)
    if raw is _UNREADABLE:
        # For a reader, "could not read" is "no blob" (OFF). The key state carries
        # the diagnostic fact, and only the writers act on it.
        return SessionRead(dims=dims, reason=C.REASON_NO_BLOB), kstate
    return _validated(raw, dims), kstate


def read(store: Any, session_key: str, dims: Dict[str, Any]) -> SessionRead:
    """Read and validate this session's blob; never raises, never invents defaults."""
    return read_probed(store, session_key, dims)[0]


def _validated(raw: Any, dims: Dict[str, Any]) -> SessionRead:
    """Validate one already-probed raw value (``_ABSENT`` / ``None`` / a stored blob)."""
    if raw is _ABSENT or raw is None:
        return SessionRead(dims=dims, reason=C.REASON_NO_BLOB)
    if not isinstance(raw, dict):
        return SessionRead(dims=dims, reason=C.REASON_INVALID, field_name="blob")
    if raw.get("version") != C.BLOB_VERSION:
        return SessionRead(dims=dims, reason=C.REASON_INVALID, field_name="version")
    stored_dims = raw.get("dims")
    if not dims_match(stored_dims, dims):
        return SessionRead(dims=dims, reason=C.REASON_DIMS)
    origin = _origin_of(raw)
    if origin is _ORIGIN_INVALID:
        # A marker this version cannot trust: never guessed at, and never read as "no marker" —
        # ``origin: null`` / ``""`` / a number would then silently become an independent
        # authorisation.
        return SessionRead(dims=dims, reason=C.REASON_INVALID, field_name="origin")
    config_raw = raw.get("config")
    parsed = _config.parse(config_raw)
    if not parsed.ok:
        return SessionRead(
            config=parsed.config,
            config_raw=config_raw if isinstance(config_raw, dict) else None,
            dims=dims,
            reason=parsed.reason,
            field_name=parsed.field,
            origin=origin,
        )
    state = SessionState.from_dict(raw.get("state"))
    if state is None:
        return SessionRead(config=parsed.config, config_raw=config_raw, dims=dims,
                           reason=C.REASON_INVALID, field_name="state", origin=origin)
    if origin not in C.BLOB_ORIGINS:
        # A well-formed blob with NO recognised authorisation stamp is NOT authorised: a bare blob
        # whose ``origin`` is missing is not authorised. Only a declaration mirror
        # (origin="declaration") or an explicit enable_session (origin="api") authorise — every
        # other blob is OFF.
        return SessionRead(dims=dims, reason=C.REASON_UNDECLARED, origin=origin)
    return SessionRead(config=parsed.config, state=state,
                       config_raw=dict(config_raw), dims=dims, reason="",
                       origin=origin)


#: ``_origin_of`` answers. ``_ORIGIN_INVALID`` is a distinct sentinel: "the field is there and it is
#: not a known source" must not collapse into the blank ``""`` ("the field is not there").
_ORIGIN_INVALID = object()


def _origin_of(raw: Dict[str, Any]) -> Any:
    """The blob's authorisation source: ``""`` (no stamp → not authorised), a known origin, or ``_ORIGIN_INVALID``.

    A **missing** field returns ``""`` so the caller treats the blob as NOT authorised (only a real
    stamp authorises). A field that is present is a claim about where the authorisation came from, and
    a claim this version cannot honour (``origin: null``/``""``/``0``/``"nope"``) makes the whole blob
    invalid rather than silently downgrading it.
    """
    if "origin" not in raw:
        return ""
    value = raw["origin"]
    if isinstance(value, str) and value in C.BLOB_ORIGINS:
        return value
    return _ORIGIN_INVALID



def write(
    store: Any,
    session_key: str,
    *,
    config_raw: Dict[str, Any],
    state: SessionState,
    dims: Dict[str, Any],
    origin: str = "",
) -> bool:
    """Persist config + state + dims (*and the origin marker*) as one value.

    ``False`` when the store refused the write. A permanent close (or a key that cannot be read)
    makes this return ``False`` without writing — see :func:`write_ordered`, which carries the
    ordering every write depends on.
    """
    return write_ordered(store, session_key, config_raw=config_raw, state=state, dims=dims,
                         origin=origin).written


#: Outcomes of :func:`write_ordered`'s ``blocked`` field. ``""`` means the write landed.
WRITE_BLOCKED_CLOSED = "closed"
WRITE_BLOCKED_UNREADABLE = "unreadable"
WRITE_BLOCKED_REFUSED = "refused"


@dataclass(frozen=True)
class WriteResult:
    """``written`` plus *why not*, when it was not.

    ``blocked`` is ``""`` for a landed write, :data:`WRITE_BLOCKED_CLOSED` when the key holds JSON
    ``null`` (a permanent close), :data:`WRITE_BLOCKED_UNREADABLE` when the key's
    state could not be confirmed, and :data:`WRITE_BLOCKED_REFUSED` when the store itself refused or
    raised. Only ``""`` means "a reader will now see this value".
    """

    written: bool
    blocked: str = ""

    @property
    def closed(self) -> bool:
        return self.blocked == WRITE_BLOCKED_CLOSED


#: One lock for the plugin's single key. It serialises "probe the key state, then write" against
#: the close (:func:`clear`), which is the ordering a close requires: a close that happened
#: before a write is seen by that write, and a close that happens after it wins. Reentrant so that a
#: caller already inside the critical section (or an operator callback invoked from it) cannot
#: deadlock; the lock is process-local, which is the scope of a close in this plugin (the key is
#: plugin-owned and only this module writes it).
_KEY_LOCK = threading.RLock()


def write_ordered(
    store: Any,
    session_key: str,
    *,
    config_raw: Dict[str, Any],
    state: SessionState,
    dims: Dict[str, Any],
    origin: str = "",
) -> WriteResult:
    """Write the blob unless the key is closed (or unreadable); atomic w.r.t. :func:`clear`.

    The probe and the write happen inside one lock, so no close can slip between them, and a
    *stale* probe taken by the caller before this call cannot authorise an overwrite: the fact is
    re-established here, last, immediately before the value is stored. "Cannot read the key" is a
    refusal too — a store whose reads fail must never be able to overwrite a close marker it cannot
    see, which is what a caller would otherwise do on the strength of a stale ``read``.

    ``origin`` is the authorisation stamp to record (``"declaration"`` or ``"api"``); a blank/omitted
    origin produces an **unauthorised** blob (reads back as OFF). Callers must pass the origin they
    just read, so a write never drops it.
    """
    blob = {
        "version": C.BLOB_VERSION,
        "dims": dict(dims),
        "config": dict(config_raw),
        "state": state.to_dict(),
    }
    if origin:
        blob["origin"] = origin
    digest = short_digest(session_key)
    with _KEY_LOCK:
        kstate = key_state(store, session_key)
        if kstate == KEY_CLOSED:
            logger.warning("proactive-conversation: refusing to write the session metadata for %s: the "
                           "session is permanently closed", digest)
            return WriteResult(False, WRITE_BLOCKED_CLOSED)
        if kstate == KEY_UNREADABLE:
            logger.warning("proactive-conversation: refusing to write the session metadata for %s: the key "
                           "state could not be read, so a permanent close cannot be ruled out",
                           digest)
            return WriteResult(False, WRITE_BLOCKED_UNREADABLE)
        ok = _set(store, session_key, blob)
    return WriteResult(bool(ok), "" if ok else WRITE_BLOCKED_REFUSED)


def clear(store: Any, session_key: str) -> bool:
    """Permanent close: overwrite the plugin's per-session value with JSON ``null``.

    Takes the same lock as every write, so the close is either seen by an in-flight write (which is
    then refused) or lands after it (in which case the ``null`` stands and the next reader is OFF).
    """
    with _KEY_LOCK:
        return _set(store, session_key, None)


_MISSING = object()

#: Distinguishes "the key is not there at all" from a value of JSON ``null`` — which is exactly what
#: a permanent close leaves behind. ``read`` treats both as "no blob" on purpose;
#: the declaration mirror must not, because a declaration is a source, not a way to re-open a closed
#: session.
_ABSENT = object()

#: The key's state could not be established (the store's read raised): *not* the same as absent.
#: A caller that mirrors a declaration must treat it as "refuse", since it cannot rule out a close.
_UNREADABLE = object()

#: :func:`key_state` answers. ``absent`` = never written (a declaration may mirror);
#: ``closed`` = the key exists and holds JSON ``null`` (terminal); ``present`` = it holds a
#: value; ``unreadable`` = the store's read failed, so nothing about the key is known.
KEY_ABSENT = "absent"
KEY_CLOSED = "closed"
KEY_PRESENT = "present"
KEY_UNREADABLE = "unreadable"


def key_state(store: Any, session_key: str) -> str:
    """Which of the four states this session's metadata key is in (never raises)."""
    return _state_of(_probe(store, session_key))


def _state_of(raw: Any) -> str:
    """The :data:`KEY_*` answer for one already-probed raw value."""
    if raw is _ABSENT:
        return KEY_ABSENT
    if raw is _UNREADABLE:
        return KEY_UNREADABLE
    if raw is None:
        return KEY_CLOSED
    return KEY_PRESENT


def closed(store: Any, session_key: str) -> bool:
    """True only when this session's key exists and holds JSON ``null`` (a permanent close).

    The store answers ``None`` both for an absent key and for a ``null`` value, so only a sentinel
    read can tell them apart; a store that cannot be read is *not* a closure — but it is not
    "absent" either (:func:`key_state` distinguishes it, and every writer refuses it).
    """
    return key_state(store, session_key) == KEY_CLOSED


def same_config(stored: Any, configured: Any) -> bool:
    """True when both raw configs carry the same value for **every** per-session key.

    Used by the declaration mirror (``runtime._mirror_declaration``) to decide whether the
    declaration is already in the metadata key: comparing every per-session key, required and optional, keeps an
    unchanged declaration from rewriting (and re-journalling) the blob on every single hook call,
    and makes "the stored config is the declared config" a per-field fact rather than a byte
    comparison of two mappings. The optional keys matter as much as the required ones: a declared
    ``day_max_attempts`` that differs from the stored blob (including "absent vs present")
    must count as a change, or the mirror would keep a stale blob and the runtime would read the
    parser's default. Both sides missing a key compare equal (the ``_MISSING`` sentinel), so a
    stored blob and a declaration that also omits the key are still "the same config".
    """
    if not isinstance(stored, dict) or not isinstance(configured, dict):
        return False
    return all(stored.get(key, _MISSING) == configured.get(key, _MISSING)
               for key in C.CONFIG_KEYS + C.OPTIONAL_CONFIG_KEYS)


def _probe(store: Any, session_key: str) -> Any:
    """The key's raw value: ``_ABSENT`` / ``_UNREADABLE`` sentinel, ``None`` for ``null``, else value.

    The host call goes through :func:`proactive.host_adapter.read_session_metadata`; the sentinels
    and the verdicts they feed stay here, because they are the plugin's contract, not the host's.
    The adapter does not catch, so this handler still decides what a fault means.
    """
    if store is None:
        return _ABSENT
    try:
        return _adapter.read_session_metadata(store, session_key, C.META_KEY, _ABSENT)
    except Exception as exc:  # a broken store must read as "no configuration", never as OFF-by-crash
        # The exception's *type* only: a store's message is third-party text that may quote the
        # session key it was handed, and a traceback is no safer.
        logger.warning("proactive-conversation: session metadata read failed for %s (%s); the key state is "
                       "unknown", short_digest(session_key), type(exc).__name__)
        return _UNREADABLE


def _set(store: Any, session_key: str, value: Any) -> bool:
    if store is None:
        return False
    try:
        return _adapter.write_session_metadata(store, session_key, C.META_KEY, value)
    except Exception as exc:
        logger.warning("proactive-conversation: session metadata write failed for %s (%s); nothing was "
                       "recorded", short_digest(session_key), type(exc).__name__)
        return False


def stored_dims(store: Any, session_key: str) -> Optional[Dict[str, Any]]:
    """The dimension fingerprint a stored blob carries, or ``None``.

    The proactive clock scans *declared* sessions without an inbound event, but :func:`read` requires
    the dims to equal the blob's exactly (:func:`dims_match`). The blob already records its dims, so
    the clock reuses them verbatim instead of re-deriving (and possibly drifting from) what the
    declaration mirror wrote. An absent/unreadable key, or a blob whose ``dims`` is not a dict,
    answers ``None`` — that session is simply skipped this scan (the gate refuses ``no-blob``).
    """
    raw = _probe(store, session_key)
    if not isinstance(raw, dict):
        return None
    dims = raw.get("dims")
    return dims if isinstance(dims, dict) else None
