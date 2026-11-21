"""Fixtures for the delivery-gate tests. Not a test module.

What is real here: the per-session store (a real ``SessionStore``), the plugin's own per-session API
(``runtime.enable_session`` / ``pause_session`` / ``revoke_session``), the release decision (the
plugin's real ``pre_gateway_dispatch`` hook, invoked through hermes' own lifecycle delivery manager)
and the gateway's own ``SendResult``/``SessionSource`` shapes. What is fake is the **transport
only**: :class:`FakeFeishuAdapter` records instead of calling the Feishu API, so every "send" count in
these tests is a count over a local list. No network, no token, no real profile.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import p4_support as P
import support
from proactive import contract as C
from proactive.delivery_guard import (DeliveryGuard, DeliveryRequest, Route,
                                      platform_name, round_id_for)

T0 = support.epoch("Asia/Shanghai", 2026, 10, 16, 12, 0)
HOUR = 3600.0

#: A deliberately tight per-session config: one proactive message per day, two before the cap.
CONFIG_TIGHT: Dict[str, Any] = dict(support.CONFIG_A, day_max_attempts=1)

#: A body that carries every kind of content the privacy rules forbid in a log line.
SENSITIVE_BODY = ("体重 72.5kg、睡眠 5h20m，昨晚 HRV 41；token=SECRET-9f2a；"
                  "chat_id=oc_dm_42；session=feishu:dm:oc_dm_42")


# ── sources ─────────────────────────────────────────────────────────────────────────────────


def feishu_dm_source(chat_id: str = "oc_dm_42", *, profile: Optional[str] = None,
                     thread_id: Optional[str] = None):
    """The shape the Feishu adapter builds for a ``p2p`` message."""
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.FEISHU, chat_id=chat_id, user_id="ou_7", chat_type="dm",
                         profile=profile, thread_id=thread_id)


def feishu_group_source(chat_id: str = "oc_group_9", *, thread_id: Optional[str] = None,
                        profile: Optional[str] = None):
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.FEISHU, chat_id=chat_id, user_id="ou_7",
                         chat_type="group", profile=profile, thread_id=thread_id)


# ── the fake transport ──────────────────────────────────────────────────────────────────────


def send_result(success: bool = True, message_id: Optional[str] = "om_1", *,
                chat_id: Optional[str] = None, error: Optional[str] = None):
    """A real ``gateway.platforms.base.SendResult`` — the receipt shape a platform adapter returns."""
    from gateway.platforms.base import SendResult

    raw = {"chat_id": chat_id} if chat_id is not None else None
    return SendResult(success=success, message_id=message_id, error=error, raw_response=raw)


_DEFAULT = object()  # "no receipt configured" ≠ "the transport answered nothing"


class FakeFeishuAdapter:
    """A local stand-in for the Feishu adapter's ``send``: it records, it never dials out.

    ``wire`` is every call the guard made (attempts); ``sent`` is what the platform "accepted" — the
    distinction is what makes "zero sends on a refused round" and "one visible message per round"
    checkable without trusting the plugin's own bookkeeping.
    """

    def __init__(self, *, connected: bool = True, receipt: Any = _DEFAULT,
                 raise_error: Optional[BaseException] = None,
                 answer: Optional[Callable[[str, str], Any]] = None) -> None:
        self.connected = connected
        self.receipt = receipt
        self.raise_error = raise_error
        self.answer = answer
        self.wire: List[Dict[str, Any]] = []
        self.sent: List[Dict[str, Any]] = []

    # the adapter's own shape: send(chat_id, content, reply_to=None, metadata=None)
    def send(self, chat_id: str, content: str, reply_to: Any = None, metadata: Any = None) -> Any:
        return self._deliver(chat_id, content)

    def _deliver(self, chat_id: str, content: str) -> Any:
        self.wire.append({"chat_id": chat_id, "text": content})
        if self.raise_error is not None:
            raise self.raise_error
        if not self.connected:
            return send_result(False, None, error="Not connected")
        if self.answer is not None:
            receipt = self.answer(chat_id, content)
        elif self.receipt is _DEFAULT:
            receipt = send_result(True, f"om_{len(self.sent) + 1}", chat_id=chat_id)
        else:
            receipt = self.receipt
        if getattr(receipt, "success", False) is True:
            self.sent.append({"chat_id": chat_id, "text": content,
                              "message_id": getattr(receipt, "message_id", None)})
        return receipt

    # observability
    def texts(self) -> List[str]:
        return [message["text"] for message in self.sent]

    def sent_to(self, chat_id: str) -> List[str]:
        return [message["text"] for message in self.sent if message["chat_id"] == chat_id]

    def __repr__(self) -> str:  # keep test output readable (and body-free)
        return f"FakeFeishuAdapter(wire={len(self.wire)}, sent={len(self.sent)})"


class AsyncFakeFeishuAdapter(FakeFeishuAdapter):
    """The same fake in the shape the real adapter has: ``send`` is a coroutine."""

    async def send(self, chat_id: str, content: str, reply_to: Any = None,  # type: ignore[override]
                   metadata: Any = None) -> Any:
        return self._deliver(chat_id, content)


# ── the crash window: dying at a chosen point, in-process ────────────────────────────────────


class ProcessDeath(BaseException):
    """A death the gate must not catch — the shape of the thing that actually happens.

    A signal kill or a machine reset is not an ``Exception``: nothing gets a chance to run a handler
    or a rollback. Deriving from ``BaseException`` is exactly that property, which is why the gate's
    ``except Exception`` around the transport must not swallow it.
    """


class DyingBeforeTheCall(FakeFeishuAdapter):
    """Death *between* the settlement and the transport call: the send call is never entered.

    ``send`` is a property that dies on attribute lookup — the last thing the process does is
    resolve the method it was about to call — so ``wire`` stays empty, which is how the test proves
    the transport body never ran.
    """

    @property
    def send(self):  # type: ignore[override]
        raise ProcessDeath("died before the transport was called")


class DyingInFlight(FakeFeishuAdapter):
    """Death *after* the transport was called, before any receipt could be recorded."""

    def _deliver(self, chat_id: str, content: str) -> Any:
        self.wire.append({"chat_id": chat_id, "text": content})
        raise ProcessDeath("died while the transport was sending")


# ── a configured session with a real released round ─────────────────────────────────────────


class GuardedSession:
    """One real configured session, driven through the plugin's own hooks.

    ``release()`` moves the clock past the sampled window and runs a real heartbeat wake through the
    registered dispatch hook; ``[]`` back means the wake was released (the gateway would dispatch the
    turn). The in-flight ticket that release leaves behind is what a delivery permission is derived
    from — nothing in this harness hands the guard a grant directly.
    """

    def __init__(self, store: Any, rt: Any, *, source: Any = None, raw: Optional[Dict[str, Any]] = None,
                 start: float = T0, released: bool = True) -> None:
        self.rt = rt
        self.store = store
        self.source = source if source is not None else feishu_dm_source()
        self.clock = {"now": float(start)}
        self.released_at: Optional[float] = None
        self._guards: Dict[int, DeliveryGuard] = {}
        rt.set_clock(lambda: self.clock["now"])
        rt.set_rng(support.FixedRng(0.0))
        self.entry = support.seed_session(store, self.source)
        self.dims = support.dims_for(store, self.entry, self.source)
        assert rt.enable_session(store, self.entry.session_key, self.dims,
                                 dict(raw if raw is not None else support.CONFIG_A)) is True
        # A released round needs an installed assembly, so a harness whose subject is the
        # release/delivery layer installs the stand-in a configured profile's ``composition`` would
        # produce (one ordinary recent record). Without it the wake is refused
        # (``off:content-unconfigured``) and nothing here would be reachable.
        rt.set_content(P.release_material(lambda: self.clock["now"]))
        if released:
            self.release()

    # -- state --------------------------------------------------------------------------

    @property
    def blob(self) -> Any:
        return self.store.get_session_metadata(self.entry.session_key, C.META_KEY, None)

    @property
    def state(self) -> Dict[str, Any]:
        return (self.blob or {}).get("state", {})

    @property
    def pending(self) -> Optional[Dict[str, Any]]:
        return self.state.get("pending")

    def guard(self, sender: Any) -> DeliveryGuard:
        """The gate for this transport and this session's clock.

        Memoised per transport object, because that is what a process really has: one gate whose
        memory of attempted rounds is what makes "at most one message per round" hold across calls.
        A test that needs a *fresh* gate (to show what a restart loses) uses :meth:`new_guard`.
        """
        key = id(sender)
        gate = self._guards.get(key)
        if gate is None:
            gate = self.new_guard(sender)
            self._guards[key] = gate
        return gate

    def new_guard(self, sender: Any) -> DeliveryGuard:
        """A *fresh* guard on this session's clock — a restarted process, with empty round memory.

        It reads the same store, so whatever still protects the round after the "restart" is the
        durable settlement, not this process's memory.
        """
        return DeliveryGuard(sender, clock=lambda: self.clock["now"], rng=support.FixedRng(0.0))

    # -- the release --------------------------------------------------------------------

    def release(self, seconds: Optional[float] = None) -> float:
        self.clock["now"] = float(seconds if seconds is not None
                                  else self.state["deadline"] + 1.0)
        assert support.released(support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_wake(self.entry.session_key, self.entry.session_id,
                                    source=self.source),
            gateway=None, session_store=self.store)) == [], "the wake must be released"
        released_at = float(self.state["pending"]["released_at"])
        self.released_at = released_at
        return released_at

    def wake(self) -> List[Any]:
        """Run one more real wake and report what the plugin decided (``[]`` = released).

        A release replaces the ticket, so the harness's idea of "the round this session is in"
        follows it — exactly as the production caller would, because it is the wake it just
        handled: the released round's ``released_at`` is what the delivery request is built from.
        """
        decision = support.released(support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_wake(self.entry.session_key, self.entry.session_id,
                                    source=self.source),
            gateway=None, session_store=self.store))
        if decision == [] and self.pending is not None:
            self.released_at = float(self.pending["released_at"])
        return decision

    # -- the delivery request -----------------------------------------------------------

    def route(self, **overrides: Any) -> Route:
        """This session's route, or the same route with one dimension changed (the negative cases)."""
        dims: Dict[str, Any] = {
            "session_key": self.entry.session_key,
            "session_id": self.entry.session_id,
            "profile": self.source.profile,
            "platform": platform_name(self.source.platform),
            "chat_id": self.source.chat_id,
            "thread_id": self.source.thread_id,
            "chat_type": self.source.chat_type,
        }
        dims.update(overrides)
        return Route(**dims)

    def make_request(self, text: str = "早上好，昨晚睡得好吗？", *, round_id: Optional[str] = None,
                     ttl: Optional[float] = None, store_handle: Any = None,
                     **route_overrides: Any) -> DeliveryRequest:
        """A delivery request for **the round this session was released for**.

        The round id is taken from the release the harness itself made (``self.released_at``), not
        from the store's in-flight ticket: the ticket is consumed by the settlement *before* the
        transport is called, so re-reading it after an attempt would invent a *different*
        round id for the same round. That is a real property of the design, and the caller in
        production has the release in hand (it is the wake it just handled) — which is why the
        durable refusal does not depend on it: a spent ticket refuses the round whatever id the
        caller passes, and the gate's own memory catches a repeated id on top of that.

        ``store_handle`` overrides the store the gate reads and writes (a store that refuses writes,
        for instance); the harness's own read-outs keep using the real one.
        """
        released_at = self.released_at if self.released_at is not None \
            else (self.pending or {}).get("released_at")
        return DeliveryRequest(
            route=self.route(**route_overrides),
            text=text,
            round_id=round_id or round_id_for(released_at, self.entry.session_id),
            store_handle=self.store if store_handle is None else store_handle,
            pending_ttl=ttl,
        )

    def deliver(self, sender: Any, text: str = "早上好，昨晚睡得好吗？", *,
                round_id: Optional[str] = None, ttl: Optional[float] = None,
                guard: Optional[DeliveryGuard] = None, store_handle: Any = None,
                **route_overrides: Any):
        """One guarded delivery attempt on this session's clock."""
        gate = guard if guard is not None else self.guard(sender)
        return gate.deliver(self.make_request(text, round_id=round_id, ttl=ttl,
                                              store_handle=store_handle, **route_overrides))

    def count(self, result: Any) -> bool:
        """Whether this round moved the counters — settled by the gate, before the transport.

        The read-out is a *report* of what the gate already did. Kept as a named helper because
        every assertion in the delivery suite is written in terms of "was this round counted", and
        that question must be answerable without reaching into the store.
        """
        return bool(result.counted)

    def counters(self) -> Dict[str, Any]:
        """The durable counting fields of this session, as the next round will read them."""
        state = self.state
        return {"visible_since_user": state.get("visible_since_user"), "visible_count": state.get("visible_count"),
                "last_release_at": state.get("last_release_at"), "pending": state.get("pending")}


def assert_no_send(result: Any, fake: FakeFeishuAdapter, reason: str, *, wire: int = 0,
                   sent: int = 0) -> None:
    """The two halves of every refusal: the verdict, and the transport that was not touched again.

    ``wire``/``sent`` are how many attempts and messages this transport had *already* legitimately
    seen before the refused attempt, so the assertion is about the refused attempt adding nothing.
    """
    assert result.state == C.SEND_REFUSED, result
    assert result.reason == reason, result
    assert result.attempts == 0 and result.delivered == 0 and not result.ok, result
    assert len(fake.wire) == wire, f"a refused delivery must not reach the transport: {fake.wire}"
    assert len(fake.sent) == sent, f"a refused delivery must not deliver: {fake.sent}"
