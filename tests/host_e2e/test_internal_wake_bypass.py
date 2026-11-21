"""Characterisation: on this host, a heartbeat wake never reaches the plugin's gate.

This is **not** a requirement and it is **not** a test of this plugin. It pins what the host does today,
so that the day a host upgrade starts routing internal events through `pre_gateway_dispatch`, a red test
tells us instead of a silent behaviour change telling us nothing.

Measured on a live gateway (host commit `86fa9e6ca1`, 2026-10-23) while the plugin was configured to
**refuse** that session (`session_declarations[].enabled: false`): two heartbeat wakes were admitted,
two full agent turns ran, two messages reached the user, and the plugin's own log carried **zero**
lines — the dispatch hook was never called.

Why the host does this: `gateway/run_inbound.py::_hm_admit_event` decides `is_internal` early and returns
`(event, source, True)` before it reaches `_hm_pre_gateway_dispatch_hook`. That hook has **two** production
call sites at this commit: `gateway/run_inbound.py` (the `_handle_message` inbound route — the one this
test drives, and the one heartbeat wakes take) and `gateway/platforms/webhook_ingress.py` (the
producer/webhook admission route, which has no `internal` check but is not on the wake path).
`gateway/run_heartbeat_acceptance.py::admit_heartbeat` builds the wake as
`MessageEvent(..., internal=True, ...)`.

When this test goes red, read the failure before the assertion: if the host **improved** (the hook is now
called for internal events), update this file and the running record and treat the gap as closed —
do not "fix" the host back. If it goes red with an `ImportError`/`AttributeError` instead, the host
renamed something (`_hm_admit_event`, `_hm_pre_gateway_dispatch_hook`, the module path); update this file's
imports and stubs to the new names — still without touching the host.

Limit, stated rather than glossed: the cases drive one host function with stand-ins, so they pin the
ingress routing decision (does the event reach the hook?) and not the whole wake chain from timer to
message. The contrast case below is what keeps the first one honest.

Scope note: this works on an instance built with `object.__new__` and a few stubs, which is the shape the
host's own comments sanction for bare runners ("bare test runners build GatewayRunner via
``object.__new__``").
"""
from __future__ import annotations

from types import SimpleNamespace

import support  # noqa: F401  (pins a throwaway HERMES_HOME before any hermes module is imported)

from gateway.platforms.event import MessageEvent
from gateway.run_inbound import GatewayInboundMixin


def _runner():
    """A host ingress object plus a recording stand-in for the plugin's dispatch hook."""
    runner = object.__new__(GatewayInboundMixin)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    calls = []

    async def dispatch_hook(event, source):
        calls.append("plugin-dispatch")
        return event

    runner._hm_pre_gateway_dispatch_hook = dispatch_hook
    runner._scale_to_zero_note_real_inbound = lambda: calls.append("scale-to-zero")
    # Past the dispatch hook is out of scope here: stub the authorisation gates so both arms can finish
    # and the contrast stays about one property only.
    runner._is_user_authorized_for_source = lambda *a, **k: True
    runner._admit_bot_message_for_source = lambda *a, **k: True
    return runner, calls


def _event(internal: bool) -> MessageEvent:
    source = SimpleNamespace(platform=None, chat_id=None, user_id="synthetic",
                             profile_route_rejected=False)
    event = MessageEvent(text="[heartbeat] characterisation probe", source=source)
    event.internal = internal
    return event


async def _admit(internal: bool):
    runner, calls = _runner()
    result = await runner._hm_admit_event(_event(internal))
    return result, calls


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_a_heartbeat_wake_is_admitted_without_the_plugin_being_asked():
    """The wake runs (the host will answer it) and the plugin is never consulted — the gap, pinned."""
    result, calls = _run(_admit(True))

    assert result is not None, "the host admits the wake; it does not drop it"
    assert result[2] is True, "admitted as internal — that flag is what skips the hook"
    assert result[0] is not None and result[1] is not None
    assert "plugin-dispatch" not in calls, (
        "the plugin was consulted for a heartbeat wake; the host changed — see this file's docstring "
        f"before changing the assertion (calls={calls})"
    )
    assert "scale-to-zero" not in calls, (
        "the host now stamps the last-inbound clock for an internal event too; the wake is still not "
        f"routed to the plugin, but record the behaviour change (calls={calls})"
    )


def test_an_ordinary_message_does_ask_the_plugin():
    """The contrast: without the flag the same gate calls the hook, so the case above is not vacuous."""
    result, calls = _run(_admit(False))

    assert result is not None
    assert result[2] is False
    assert "plugin-dispatch" in calls, f"the hook was not called for an ordinary message (calls={calls})"
    assert "scale-to-zero" in calls, "the host's own bookkeeping still runs for real inbound"
