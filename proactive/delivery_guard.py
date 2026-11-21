"""The delivery gate: the one point that decides whether a released round may actually be sent.

The gate sits on the **plugin** side: the plugin never reshapes an outbound
message and never touches the platform adapter — it owns a guard object whose ``deliver`` is the
only way a proactive message reaches a transport. Everything observable in this module is therefore
about *counts*: how many messages a fake (or real) adapter was asked to send, and what the plugin
recorded about them.

**The send sequence is write-ahead, and that order is the guarantee**: the gate
judges the permission, then consumes the in-flight release ticket *and* moves the counters durably,
and only then calls the transport. A process that dies anywhere after the judgement — before the
send, during it, or before any receipt is recorded — therefore leaves a durable record that the
round is spent: a restarted gate finds no ticket and refuses. **At most one message per round, never
a duplicate**; the cost of every failure branch is *at most one missed message*. The counters are
never rolled back because of what the transport answers: the round that passed the gate is counted
whether the transport confirmed it, rejected it, or left the outcome unknown. A settlement that
cannot be persisted is a refusal, not a send.

The gate answers one question with four possible outcomes, and it answers it **per attempt**, from
the live session read — nothing is cached across rounds:

``sent``      the transport returned a receipt that identifies *this* delivery (see
              :func:`classify_receipt`); this is the only state that may be *reported* as delivered
              (the round was already counted, before the send).
``failed``    the transport asserted non-delivery.
``uncertain`` no usable receipt: possibly delivered, possibly not. Never reported as a success,
              never retried.
``refused``   the gate said no and the transport was never called.

Design rules:

* **Target + permission must match together.** A delivery is allowed only for a Feishu direct
  message whose route (profile / platform / chat / thread / session key / session id) equals the
  grant's dimensions *and* whose grant is still live in the store. Every other case — a second DM,
  a group, another profile, another platform, a stale route — costs zero sends.
* **The grant lives in the same per-session blob as everything else**, plus the in-flight
  release ticket: a permission is valid only for the round that was released, so "the previous
  round was allowed" can never authorise this one.
* **At most one attempt per round, across processes.** The ticket is consumed durably *before* the
  transport is called (:func:`settle_release`), and the round id is remembered in-process as well,
  so neither a failure nor a restart can produce a second send of that round.
* **The counters move in exactly one function** (``policy.commit_release``), called once per
  attempt by this gate, ahead of the send: nothing the transport says can move them again.
* **Logs and bookkeeping are digest-only**: the message body, chat ids, session ids, health
  numbers and tokens never appear in a log line or in what this module keeps.

Nothing calls into the gateway's egress path from here: the gate is handed an adapter by whatever
component owns the delivery lane. The in-flight ticket is one-shot, so exactly one component may
settle it — whichever component sits immediately before the transport must be the one that does.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import math
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, NamedTuple, Optional, Tuple

from . import contract as C
from . import host_adapter as _adapter
from . import policy, settings as _settings, store

logger = logging.getLogger(__name__)

#: Length of the short, irreversible digest used for every session/round identifier in logs and in
#: this module's own bookkeeping. Mechanical value (a log field width), not a user setting.
DIGEST_CHARS = 12

_ABSENT = object()
"""Sentinel for "this session has no metadata entry for this key" — it is how a **permanent close**
(JSON ``null`` under the key) is told apart from **never configured** (no value at all)."""


# ── redaction ───────────────────────────────────────────────────────────────────────────────


def short_digest(*parts: Any) -> str:
    """An irreversible short digest of the given parts (logs, bookkeeping, test assertions).

    Irreversible in the only sense that matters here: the digest cannot be turned back into a chat
    id, and a digest is useless to whoever reads the log. It is stable within a delivery attempt, so
    the same target is still recognisably *the same* target across lines.
    """
    blob = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def _text(value: Any) -> Optional[str]:
    """A non-empty string, or ``None`` — never a stringified number/object masquerading as an id."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def platform_name(platform: Any) -> Optional[str]:
    """The platform's own name: ``Platform.FEISHU`` and ``"feishu"`` both mean ``"feishu"``."""
    value = getattr(platform, "value", None) or platform
    return _text(value.lower() if isinstance(value, str) else value)


# ── what a delivery attempt is about ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Route:
    """Where a message would go, as the live gateway knows it right now.

    The six identity fields are exactly the ones the per-session blob fingerprints, so a route
    can be compared to a grant without a second mapping: :meth:`dims` *is* the dimension
    fingerprint the store validates against.
    """

    session_key: str
    session_id: Optional[str] = None
    profile: Optional[str] = None
    platform: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    chat_type: Optional[str] = None

    @classmethod
    def from_source(cls, source: Any, *, session_key: Any, session_id: Any = None) -> "Route":
        """The route a gateway ``SessionSource`` describes (the shape the dispatch hook sees)."""
        return cls(
            session_key=_text(session_key) or "",
            session_id=_text(session_id),
            profile=_text(getattr(source, "profile", None)),
            platform=platform_name(getattr(source, "platform", None)),
            chat_id=_text(getattr(source, "chat_id", None)),
            thread_id=_text(getattr(source, "thread_id", None)),
            chat_type=_text(getattr(source, "chat_type", None)),
        )

    def dims(self) -> Dict[str, Any]:
        """The dimension fingerprint to read/write this session with."""
        return store.dims_for(
            session_key=self.session_key,
            session_id=self.session_id,
            profile=self.profile,
            platform=self.platform,
            chat_id=self.chat_id,
            thread_id=self.thread_id,
        )

    @property
    def is_dm(self) -> bool:
        """Whether this is a direct message on the one platform a proactive message may reach."""
        return self.platform == C.PLATFORM_FEISHU and self.chat_type == C.CHAT_TYPE_DM

    @property
    def digest(self) -> str:
        """The redacted identity of this target (all six dimensions, one short digest)."""
        return short_digest(self.profile, self.platform, self.chat_id, self.thread_id,
                            self.session_key, self.session_id)

    def __repr__(self) -> str:  # a repr can end up in a log line or a traceback: digests only
        return (f"Route(digest={self.digest!r}, platform={self.platform!r}, "
                f"dm={self.is_dm}, thread={bool(self.thread_id)})")


@dataclass(frozen=True)
class Permission:
    """The outcome of asking the store "may this route be sent a proactive message *now*?"."""

    status: str
    reason: str
    released_at: Optional[float] = None

    @property
    def granted(self) -> bool:
        return self.status == C.PERM_ACTIVE

    def __repr__(self) -> str:
        return f"Permission(status={self.status!r}, reason={self.reason!r})"


class DeliveryRequest:
    """One delivery attempt: what to send, where, and which released round it belongs to."""

    __slots__ = ("route", "text", "round_id", "store", "pending_ttl")

    def __init__(self, *, route: Route, text: str, round_id: str, store_handle: Any,
                 pending_ttl: Optional[float] = None) -> None:
        self.route = route
        self.text = text
        self.round_id = str(round_id)
        self.store = store_handle
        self.pending_ttl = pending_ttl

    def __repr__(self) -> str:  # never the body, never the ids
        return (f"DeliveryRequest(route={self.route!r}, round={short_digest(self.round_id)!r}, "
                f"body_chars={len(self.text or '')})")


def round_id_for(released_at: Any, session_id: Any) -> str:
    """The round id of a release: the ticket's identity (``released_at`` + ``session_id``).

    Two releases of the same session are different rounds (a later ``released_at``), and a reset
    session is a different round (a new ``session_id``) — which is exactly the granularity the
    "at most one message per round" rule needs.
    """
    stamp = float(released_at) if isinstance(released_at, (int, float)) and not isinstance(released_at, bool) else 0.0
    return f"{stamp:.6f}:{session_id}"


@dataclass(frozen=True)
class DeliveryResult:
    """What happened in one attempt — the value every caller and every test reads.

    The three transport columns of the observation table are all here: ``attempts`` (calls the gate
    made), ``accepted`` (what the *transport itself* claimed — a receipt with ``success is True``),
    and ``delivered`` (what the gate could confirm as this delivery). ``counted`` is settled before
    the transport is called and never depends on those three.
    """

    state: str
    reason: str
    target: str                       # route digest
    permission: Permission
    round: str = ""                   # round digest
    attempts: int = 0                 # transport calls made by this attempt (0 or 1)
    accepted: int = 0                 # receipts in which the transport claimed success (0 or 1)
    delivered: int = 0                 # messages this attempt confirmed as delivered (0 or 1)
    counted: bool = False             # the round's release was counted (write-ahead, before the send)
    duplicate: bool = False            # a repeated round was refused before the transport
    receipt_id: Optional[str] = field(default=None, repr=False)
    route: Optional[Route] = field(default=None, repr=False)
    store: Any = field(default=None, repr=False, compare=False)
    read: Any = field(default=None, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        """True only for a confirmed send. Failed and uncertain are *not* successes."""
        return self.state == C.SEND_SENT

    @property
    def receipt_digest(self) -> str:
        """The platform receipt id, redacted (empty when there is no receipt)."""
        return short_digest(self.receipt_id) if self.receipt_id else ""

    def __repr__(self) -> str:
        return (f"DeliveryResult(state={self.state!r}, reason={self.reason!r}, "
                f"target={self.target!r}, round={self.round!r}, "
                f"permission={self.permission.status!r}, attempts={self.attempts}, "
                f"accepted={self.accepted}, delivered={self.delivered}, "
                f"counted={self.counted}, duplicate={self.duplicate})")


# ── the gate's inputs: the store, and the two things that can refuse ────────────────────────


def _closed_probe(store_handle: Any, session_key: str) -> bool:
    """Whether the session's plugin value is an explicit JSON ``null`` (a permanent close).

    Probed with a private sentinel instead of the public ``None`` default: "the value is gone on
    purpose" and "there was never a value" are different refusals, and that difference is what tells
    a permanently closed session apart from one that was never configured. Nothing else is read
    here — the value itself is unused.

    The host call goes through :func:`proactive.host_adapter.read_session_metadata`,
    so this module names no metadata method. A fault is still "no grant": the
    handler logs the exception **type** plus a session digest and never the exception body, which
    would be third-party text that may quote the key it was handed (the rule `store.py`
    already follows).
    """
    if store_handle is None:
        return False
    try:
        return _adapter.read_session_metadata(store_handle, session_key, C.META_KEY, _ABSENT) is None
    except Exception as exc:  # an unreadable store is "no grant", never a crash and never an allow
        logger.warning("proactive-conversation: session metadata probe failed for %s (%s)",
                       short_digest(session_key), exc.__class__.__name__)
        return False


def _target_refusal(route: Route) -> str:
    """Why this route can never receive a proactive message, whatever the store says."""
    if not route.session_key:
        return C.REASON_TARGET_NO_SESSION_KEY
    if not route.chat_id:
        return C.REASON_TARGET_NO_CHAT
    if route.platform != C.PLATFORM_FEISHU:
        return C.REASON_TARGET_PLATFORM
    if route.chat_type != C.CHAT_TYPE_DM:
        return C.REASON_TARGET_NOT_DM
    if not route.session_id:
        return C.REASON_TARGET_NO_SESSION_ID
    return ""


def _permission_from_read(read: Any, route: Route, *, closed: bool, now: float,
                          ttl: float) -> Permission:
    """Map one live session read + the in-flight ticket onto the permission state."""
    if closed:
        return Permission(C.PERM_CLOSED, C.REASON_PERM_CLOSED)
    if not read.usable:
        if read.reason == C.REASON_DIMS:
            # The grant belongs to other dimensions (another chat/thread/profile/session): a stale
            # route never inherits an authorisation.
            return Permission(C.PERM_EXPIRED, C.REASON_PERM_STALE_ROUTE)
        if read.reason in (C.REASON_DISABLED,):
            return Permission(C.PERM_REVOKED, C.REASON_PERM_REVOKED)
        if read.reason in (C.REASON_INVALID,):
            return Permission(C.PERM_ABSENT, C.REASON_PERM_ABSENT)
        return Permission(C.PERM_ABSENT, C.REASON_PERM_ABSENT)
    if read.state.status != C.STATUS_ACTIVE:
        return Permission(C.PERM_PAUSED, C.REASON_PERM_PAUSED)
    pending = read.state.pending or {}
    released_at = pending.get("released_at")
    if not isinstance(released_at, (int, float)) or isinstance(released_at, bool):
        # No release is in flight: this round was never granted, and the previous round's grant is
        # spent (the ticket is single-use). Nothing is inherited.
        return Permission(C.PERM_EXPIRED, C.REASON_PERM_NO_TICKET)
    released_at = float(released_at)
    if pending.get("session_id") not in (None, route.session_id):
        return Permission(C.PERM_EXPIRED, C.REASON_PERM_STALE_ROUTE)
    if now > released_at + ttl:
        # Bound to a wall-clock deadline (the plugin-level ``pending_release_ttl_seconds`` setting):
        # a round whose turn never came back cannot authorise a later delivery.
        return Permission(C.PERM_EXPIRED, C.REASON_PERM_TTL, released_at)
    return Permission(C.PERM_ACTIVE, C.REASON_PERM_ACTIVE, released_at)


def permission_for(store_handle: Any, route: Route, *, now: float,
                   pending_ttl: Optional[float] = None, read: Any = None) -> Permission:
    """The current permission for ``route``: always re-derived, never cached."""
    ttl = _effective_ttl(pending_ttl)
    closed = _closed_probe(store_handle, route.session_key)
    if read is None:
        read = store.read(store_handle, route.session_key, route.dims())
    return _permission_from_read(read, route, closed=closed, now=now, ttl=ttl)


def _effective_ttl(value: Optional[float]) -> float:
    """The in-flight-ticket TTL: the explicit argument, else the plugin-level setting."""
    if value is None:
        return _settings.value(_settings.PENDING_RELEASE_TTL)
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError("pending_ttl must be a finite positive number of seconds")
    return number


# ── the receipt: success is only what the platform confirmed ────────────────────────────────


def _has_field(receipt: Any, name: str) -> bool:
    if isinstance(receipt, Mapping):
        return name in receipt
    return hasattr(receipt, name)


def _receipt_field(receipt: Any, name: str) -> Any:
    if isinstance(receipt, Mapping):
        return receipt.get(name)
    return getattr(receipt, name, None)


def _receipt_target(receipt: Any) -> Optional[str]:
    """The chat a receipt names, when it names one (adapter-specific metadata, optional)."""
    raw = _receipt_field(receipt, "raw_response")
    value = raw.get("chat_id") if isinstance(raw, Mapping) else getattr(raw, "chat_id", None)
    return _text(value)


def classify_receipt(receipt: Any, route: Route) -> Tuple[str, str, Optional[str]]:
    """Interpret one transport return value as ``(state, reason, receipt_id)``.

    ``sent`` requires a receipt that identifies *this* delivery: an explicit ``success is True``, a
    non-empty platform message id, and — when the receipt names a chat at all — the chat we sent to.
    A receipt for a different chat is not a receipt for this message: that is ``uncertain``, not
    success. ``failed`` is only what the transport itself asserted (``success is False``, or a slice
    of transport-declared error kinds); everything else is ``uncertain``, which is never
    upgraded to success and never retried.
    """
    if receipt is None:
        return C.SEND_UNCERTAIN, C.REASON_SEND_NO_RECEIPT, None
    if not _has_field(receipt, "success"):
        return C.SEND_UNCERTAIN, C.REASON_SEND_UNRECOGNIZED, None
    success = _receipt_field(receipt, "success")
    if success is False:
        return C.SEND_FAILED, C.REASON_SEND_REJECTED, None
    if success is not True:
        return C.SEND_UNCERTAIN, C.REASON_SEND_UNRECOGNIZED, None
    receipt_id = _text(_receipt_field(receipt, "message_id"))
    if not receipt_id:
        return C.SEND_UNCERTAIN, C.REASON_SEND_NO_RECEIPT_ID, None
    named = _receipt_target(receipt)
    if named is not None and named != route.chat_id:
        return C.SEND_UNCERTAIN, C.REASON_SEND_WRONG_TARGET, receipt_id
    return C.SEND_SENT, C.REASON_OK, receipt_id


# ── the gate itself ─────────────────────────────────────────────────────────────────────────


class DeliveryGuard:
    """The plugin-side delivery gate, bound to one transport handle (a real adapter or a fake).

    The transport is called in the platform adapter's own shape — ``send(chat_id, content)``, with
    an awaitable accepted for the real (async) adapter — and it is called **at most once per round**
    no matter how the call returns. One guard instance is one process's memory of attempted
    rounds; the durable half of the guarantee is the write-ahead settlement, which is what
    survives this process dying.

    ``clock`` and ``rng`` are the injectable seams the pure policy needs (:func:`settle_release`);
    the RNG is only ever used to re-sample the window of a round that was just counted.
    """

    def __init__(self, sender: Any, *, clock: Optional[Callable[[], float]] = None,
                 rng: Any = None) -> None:
        self._sender = sender
        self._clock = clock or time.time
        self._rng = rng if rng is not None else random.Random()
        self._rounds: "OrderedDict[str, Tuple[float, str]]" = OrderedDict()

    # -- diagnostics ---------------------------------------------------------------------

    def rounds(self) -> Dict[str, Dict[str, Any]]:
        """This process's attempted rounds, digest-keyed: ``{round-digest: {at, state}}``.

        Deliberately digest-only: the guard's own bookkeeping must never be a plaintext store
        of session ids. ``known`` is the count of rounds it still remembers.
        """
        return {short_digest(key): {"at": at, "state": state}
                for key, (at, state) in self._rounds.items()}

    def known_rounds(self) -> int:
        return len(self._rounds)

    # -- the gate ------------------------------------------------------------------------

    def deliver(self, request: DeliveryRequest) -> DeliveryResult:
        """Synchronous transport (a fake, or any non-coroutine sender).

        A transport problem never raises: it comes back as ``failed``/``uncertain``. A transport
        that is *shaped* wrong for this entry point (one whose ``send`` returns a coroutine) is a
        programming error and raises ``TypeError`` — the round is already settled and remembered, so
        the mistake cannot turn into a duplicate send either.
        """
        prepared = self._gate(request)
        if isinstance(prepared, DeliveryResult):
            return prepared
        try:
            receipt = self._sender.send(prepared[1].chat_id, request.text)
        except Exception as exc:
            return self._raised(prepared, exc)
        if inspect.isawaitable(receipt):
            close = getattr(receipt, "close", None)  # never leave it un-awaited
            if callable(close):
                close()
            raise TypeError("the transport is a coroutine: use deliver_async() for it")
        return self._finish(prepared, receipt)

    async def deliver_async(self, request: DeliveryRequest) -> DeliveryResult:
        """The production shape: a real platform adapter's ``send`` is a coroutine."""
        prepared = self._gate(request)
        if isinstance(prepared, DeliveryResult):
            return prepared
        try:
            receipt = self._sender.send(prepared[1].chat_id, request.text)
            while inspect.isawaitable(receipt):  # a plain value is accepted as-is
                receipt = await receipt
        except Exception as exc:
            return self._raised(prepared, exc)
        return self._finish(prepared, receipt)

    # -- internals -----------------------------------------------------------------------

    def _gate(self, request: DeliveryRequest) -> Any:
        """Everything that happens *before* the transport is touched: the refusals, then the
        write-ahead settlement of the round that is about to be sent.

        Returns a :class:`DeliveryResult` (refused) or an ``_Allowed`` bundle.
        """
        now = float(self._clock())
        ttl = _effective_ttl(request.pending_ttl)
        route = request.route
        target = route.digest
        round_digest = short_digest(request.round_id)

        # ── the anti-duplicate rule comes first: a round this process already attempted ──
        # It is checked before anything is read or decided, because it is a fact about the *round*,
        # not about the session's current permissions — and a repeated round must be refused for
        # that reason (not for whatever the session now looks like). This branch is what tells
        # "I already sent this round" apart from "this round has no live release at all"; the
        # durable settlement below is the backstop that keeps holding after a restart.
        self._prune(now, ttl)
        if request.round_id in self._rounds:
            return self._refuse(
                state=C.SEND_REFUSED, reason=C.REASON_SEND_DUPLICATE, request=request,
                # The round's release is spent (the settlement ran before its attempt), which is
                # exactly what "expired, no ticket" says — asserted from memory, no read needed.
                permission=Permission(C.PERM_EXPIRED, C.REASON_PERM_NO_TICKET),
                target=target, round_digest=round_digest, duplicate=True)

        refusal = _target_refusal(route)
        if refusal:
            return self._refuse(state=C.SEND_REFUSED, reason=refusal, request=request,
                                permission=Permission(C.PERM_ABSENT, refusal),
                                target=target, round_digest=round_digest)

        # One read, one consistent view: the same read is what the settlement below commits.
        read = store.read(request.store, route.session_key, route.dims())
        permission = _permission_from_read(
            read, route, closed=_closed_probe(request.store, route.session_key), now=now, ttl=ttl)
        if not permission.granted:
            return self._refuse(state=C.SEND_REFUSED, reason=permission.reason, request=request,
                                permission=permission, target=target, round_digest=round_digest,
                                read=read)

        # ── write-ahead: the ticket and the counters are settled durably *before* the send ──
        # This is what a process death cannot undo, and it is why no restart can send this round
        # twice. A settlement that does not land is a refusal: an unrecorded send would reopen the
        # very window this ordering closes.
        if not settle_release(request.store, route, read, now=now, rng=self._rng,
                              round_digest=round_digest):
            return self._refuse(state=C.SEND_REFUSED, reason=C.REASON_SETTLE_FAILED,
                                request=request, permission=permission, target=target,
                                round_digest=round_digest, read=read)

        # Remembered BEFORE the call: a crash, a raise or an uncertain receipt must not make the
        # round look unattempted to a later attempt in this process (the durable record above
        # covers every other process).
        self._rounds[request.round_id] = (now, "attempting")
        return _Allowed(request=request, route=route, permission=permission, read=read, now=now,
                        target=target, round_digest=round_digest)

    def _raised(self, allowed: "_Allowed", exc: BaseException) -> DeliveryResult:
        """A transport that raised: possibly delivered, definitely not confirmed."""
        self._rounds[allowed.request.round_id] = (allowed.now, C.SEND_UNCERTAIN)
        result = DeliveryResult(state=C.SEND_UNCERTAIN, reason=C.REASON_SEND_RAISED,
                                target=allowed.target, permission=allowed.permission,
                                round=allowed.round_digest, attempts=1, delivered=0,
                                counted=True,  # settled before the call; a raise cannot uncount it
                                route=allowed.route, store=allowed.request.store, read=allowed.read)
        logger.warning("proactive-conversation: delivery raised %s for round %s to %s (%s) — the round "
                       "stays counted (at most one missed message, never a repeat)",
                       exc.__class__.__name__, allowed.round_digest, allowed.target, result.reason)
        return result

    def _finish(self, allowed: "_Allowed", receipt: Any) -> DeliveryResult:
        state, reason, receipt_id = classify_receipt(receipt, allowed.route)
        self._rounds[allowed.request.round_id] = (allowed.now, state)
        result = DeliveryResult(
            state=state, reason=reason, target=allowed.target, permission=allowed.permission,
            round=allowed.round_digest, attempts=1, accepted=_claimed_accepted(receipt),
            delivered=1 if state == C.SEND_SENT else 0,
            counted=True,  # settled before the call, whatever this receipt says
            receipt_id=receipt_id, route=allowed.route, store=allowed.request.store,
            read=allowed.read,
        )
        if state == C.SEND_SENT:
            logger.info("proactive-conversation: delivered round %s to %s (receipt %s)",
                        allowed.round_digest, allowed.target, result.receipt_digest)
        else:
            logger.warning("proactive-conversation: delivery %s for round %s to %s (%s) — the round stays "
                           "counted (at most one missed message, never a repeat)", state,
                           allowed.round_digest, allowed.target, reason)
        return result

    def _refuse(self, *, state: str, reason: str, request: DeliveryRequest, permission: Permission,
                target: str, round_digest: str, duplicate: bool = False,
                read: Any = None) -> DeliveryResult:
        result = DeliveryResult(state=state, reason=reason, target=target, permission=permission,
                                round=round_digest, attempts=0, accepted=0, delivered=0,
                                counted=False, duplicate=duplicate,
                                route=request.route, store=request.store, read=read)
        if duplicate:
            logger.warning("proactive-conversation: refused a repeated delivery of round %s to %s (%s)",
                           round_digest, target, reason)
        else:
            logger.info("proactive-conversation: delivery refused for %s (round %s): %s %s", target,
                        round_digest, permission.status, reason)
        return result

    def _prune(self, now: float, ttl: float) -> None:
        """Forget rounds older than the ticket TTL: past it a round can no longer be live, so its
        memory is dead weight (and a stale round id can never authorise anything: the permission has
        expired by then)."""
        for key, (at, _state) in list(self._rounds.items()):
            if at + ttl < now:
                self._rounds.pop(key, None)


class _Allowed(NamedTuple):
    """Everything the gate settled *before* an attempt — one transport call's inputs."""

    request: DeliveryRequest
    route: Route
    permission: Permission
    read: Any
    now: float
    target: str
    round_digest: str


# ── the write-ahead settlement ──────────────────────────────────────────────────────────────


def settle_release(store_handle: Any, route: Route, read: Any, *, now: float, rng: Any,
                   round_digest: str = "") -> bool:
    """Consume the round's ticket and move the counters durably — **before** anything is sent.

    ``True`` when the round is settled: the ticket is spent, the counters moved and
    the session is persisted, all of it ahead of the transport call. ``False`` when it could not be
    settled at all (no ticket left to commit, or the store refused/failed the write) — the caller
    must then **refuse**: an unrecorded send would leave the ticket authorising that same round to a
    restarted process, which is exactly the duplicate window this ordering closes.

    ``policy.commit_release`` stays *the* counting anchor and is called from here only: this
    function decides *when* it runs (ahead of the transport), never *what* it means. The ticket is
    single-use, so exactly one component may settle a round — the one immediately before the
    transport.

    Nothing here depends on a transport outcome: at this point there is no outcome yet, and there
    will never be one to react to — a failed or uncertain send keeps its count, which is the
    documented cost of the ordering (at most one missed message, never a duplicate).
    """
    state, committed = policy.commit_release(read.state, read.config, now, rng)
    if not committed:
        logger.warning("proactive-conversation: round %s has no ticket left to settle; not sending",
                       round_digest)
        return False
    written = store.write(store_handle, route.session_key, config_raw=read.config_raw, state=state,
                          dims=route.dims(), origin=getattr(read, "origin", ""))
    if not written:
        logger.warning("proactive-conversation: settlement of round %s could not be persisted; not sending",
                       round_digest)
        return False
    logger.info("proactive-conversation: settled round %s before the send (attempts=%d, silent=%d)",
                round_digest, state.attempt_count, state.silent_count)
    return True


def _claimed_accepted(receipt: Any) -> int:
    """Whether the *transport* claimed success (``success is True``), whatever the gate concluded.

    This is the middle column of the observation table: an ``uncertain`` row can legitimately show
    ``accepted=1, delivered=0`` — the transport said "ok" without giving a receipt that identifies
    this delivery, so the gate refuses to call it a delivery. It is recorded, never trusted.
    """
    if receipt is None or not _has_field(receipt, "success"):
        return 0
    return 1 if _receipt_field(receipt, "success") is True else 0
