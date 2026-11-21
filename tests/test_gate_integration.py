"""The dispatch gate, driven through the real plugin and the real finalizer.

The live invariants: the installed copy and hook manifest, a wake for an unconfigured session is
refused, a real user message is never blocked and re-anchors the window, the window anchors on the
message's own timestamp, and the timestamp-sanity setting decides whether that timestamp is usable.

Nothing in this file re-implements the hook path. The plugin is copied into an isolated hermes home
and loaded by hermes' own loader; the dispatch hook is invoked through the lifecycle delivery
manager; the delivery gate is the real ``agent.turn_finalizer.apply_llm_output_transform`` (the
entry point the gateway uses before the assistant row is persisted); and the resulting text is
judged by the gateway's own silence predicates (``gateway.response_filters``).

``rt`` (fixture ``plugin_runtime``) is the plugin's *own* runtime module — the one the registered
callbacks live in. The seams and the in-flight tickets are module state, so driving the real hooks
requires that object, not a second ``import proactive.runtime`` in this process.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import p4_support as P
import support
from proactive import contract as C

T0 = support.epoch("Asia/Shanghai", 2026, 10, 16, 12, 0)
HOUR = 3600.0


def installed_path() -> Path:
    return (support.ISO_HOME / "plugins" / "proactive-conversation").resolve()


class SessionHarness:
    """One configured session: real store entry, fixed seams, hooks invoked for real."""

    def __init__(self, store, rt, source=None, raw=None, start=T0):
        self.rt = rt
        self.store = store
        self.source = source or support.dm_source()
        self.clock = {"now": float(start)}
        rt.set_clock(lambda: self.clock["now"])
        rt.set_rng(support.FixedRng(0.0))
        self.entry = support.seed_session(store, self.source)
        self.dims = support.dims_for(store, self.entry, self.source)
        assert rt.enable_session(
            store, self.entry.session_key, self.dims, dict(raw or support.CONFIG_A)) is True
        # An installed assembly is the precondition of a release. This harness measures the
        # gate/ticket, so it installs the same stand-in a configured profile would have.
        rt.set_content(P.release_material(lambda: self.clock["now"]))

    @property
    def blob(self):
        return self.store.get_session_metadata(self.entry.session_key, C.META_KEY, None)

    @property
    def state(self):
        return (self.blob or {}).get("state", {})

    def wake(self):
        """Run one heartbeat wake through the registered dispatch hook (``[]`` = released)."""
        return support.released(support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_wake(self.entry.session_key, self.entry.session_id,
                                    source=self.source),
            gateway=None, session_store=self.store))

    def user_message(self, text="在吗", advance=60.0):
        """A real user message through the registered hook; the clock moves on by ``advance``.

        The message carries its own timestamp (what the gateway stamps on an inbound message), so
        the anchor comes from the message, not from the moment the hook ran.
        """
        self.clock["now"] += float(advance)
        return support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_event(text=text, session_key=self.entry.session_key,
                                     source=self.source,
                                     timestamp=support.at_clock(self.clock["now"])),
            gateway=None, session_store=self.store)

    def due(self, seconds=None):
        """Move the clock past the stored window."""
        self.clock["now"] = float(seconds if seconds is not None else self.state["deadline"] + 1)


class FakeAgent:
    """The attributes ``apply_llm_output_transform`` reads off a live agent."""

    def __init__(self, session_id, platform="telegram", model="fake-model"):
        self.session_id = session_id
        self.platform = platform
        self.model = model
        self._llm_output_transform = None


def _deliver(agent, text="早上好，昨晚睡得好吗？", turn_id="turn-1"):
    """Run the real delivery gate: returns (final_response, transformed, pre_transform)."""
    from agent.turn_finalizer import apply_llm_output_transform

    return apply_llm_output_transform(agent, text, turn_id=turn_id)


@pytest.fixture
def rt(plugin_runtime):
    return plugin_runtime


@pytest.fixture
def harness(store, rt):
    h = SessionHarness(store, rt)
    yield h
    rt.reset_seams()


# ── the plugin as loaded ────────────────────────────────────────────────────────────────────


def test_the_installed_copy_is_this_package(installed_plugin_dir):
    """The loaded plugin is this repository's code, byte for byte (no drift)."""
    for rel in ("plugin.yaml", "__init__.py", "proactive/runtime.py", "proactive/policy.py",
                "proactive/config.py", "proactive/state.py", "proactive/store.py",
                "proactive/settings.py", "proactive/contract.py"):
        here = (support.PKG_ROOT / rel).read_bytes()
        there = (installed_plugin_dir / rel).read_bytes()
        assert hashlib.sha256(here).hexdigest() == hashlib.sha256(there).hexdigest(), rel


def test_the_manifest_declares_exactly_the_hooks_that_are_registered(plugin):
    import hermes_cli.plugins as plugins_mod

    manager = plugins_mod.get_plugin_manager()
    assert plugin.manifest.name == "proactive-conversation"
    assert plugin.enabled is True and plugin.error is None
    # Declaring ``pre_tool_call`` is what lets hermes dispatch the gate, so this assertion is
    # load-bearing.
    declared = ["pre_gateway_dispatch", "transform_llm_output", "pre_tool_call", "pre_llm_call"]
    assert list(plugin.manifest.provides_hooks) == declared

    live = {name: callbacks for name, callbacks in manager._hooks.items() if callbacks}
    assert set(live) == set(declared), (
        f"the plugin registered hooks it does not declare (or lost one): {sorted(live)}"
    )
    for name, callbacks in live.items():
        assert len(callbacks) == 1, f"{name} has {len(callbacks)} callbacks"
        module = sys.modules[callbacks[0].__module__]
        assert str(installed_path()) in str(Path(module.__file__).resolve()), name


# ── the dispatch gate ───────────────────────────────────────────────────────────────────────


def test_a_wake_for_an_unconfigured_session_is_refused(plugin, store, rt):
    source = support.dm_source()
    entry = support.seed_session(store, source)  # exists, but nothing was ever enabled
    rt.set_clock(lambda: T0)
    try:
        result = support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_wake(entry.session_key, entry.session_id, source=source),
            gateway=None, session_store=store)
    finally:
        rt.reset_seams()
    assert result == [{"action": "skip", "reason": C.REASON_NO_BLOB}]






def test_a_real_user_message_is_never_blocked_and_re_anchors_the_window(harness):
    harness.clock["now"] = T0 + 30 * HOUR  # past the sampled window, but no wake is run
    assert harness.user_message(advance=0) == []
    assert harness.state["last_user_at"] == T0 + 30 * HOUR
    assert harness.state["deadline"] == T0 + 30 * HOUR + 6 * HOUR
    assert harness.state["visible_since_user"] == 0


def test_the_window_is_anchored_on_the_messages_own_timestamp(harness):
    """The user's message time is the anchor, not the moment the hook happened to run."""
    from datetime import datetime

    harness.clock["now"] = T0 + 2 * HOUR + 90
    naive = datetime.fromtimestamp(T0 + 2 * HOUR)  # how MessageEvent builds it: datetime.now()
    event = support.make_event(text="刚开完会", session_key=harness.entry.session_key,
                               source=harness.source, timestamp=naive)
    assert support.invoke_hook("pre_gateway_dispatch", event=event, gateway=None,
                               session_store=harness.store) == []
    assert harness.state["last_user_at"] == pytest.approx(T0 + 2 * HOUR, abs=1)
    assert harness.state["deadline"] == pytest.approx(T0 + 2 * HOUR + 6 * HOUR, abs=1)


def test_the_window_is_anchored_on_an_aware_timestamp_too(harness):
    from datetime import datetime, timezone

    stamp = datetime.fromtimestamp(T0 + 3 * HOUR, tz=timezone.utc)
    harness.clock["now"] = T0 + 3 * HOUR + 5
    event = support.make_event(session_key=harness.entry.session_key, source=harness.source,
                               timestamp=stamp)
    assert support.invoke_hook("pre_gateway_dispatch", event=event, gateway=None,
                               session_store=harness.store) == []
    assert harness.state["anchor_at"] == pytest.approx(stamp.timestamp(), abs=1)


def test_an_implausible_message_timestamp_falls_back_to_now(harness):
    """A timestamp outside the sanity band is ignored; the hook uses its own clock."""
    from datetime import datetime

    stale = datetime.fromtimestamp(T0 - 30 * 24 * HOUR)  # a month old: not usable
    event = support.make_event(session_key=harness.entry.session_key, source=harness.source,
                               timestamp=stale)
    assert support.invoke_hook("pre_gateway_dispatch", event=event, gateway=None,
                               session_store=harness.store) == []
    assert harness.state["anchor_at"] == harness.clock["now"]


# ── the delivery gate ───────────────────────────────────────────────────────────────────────


















def test_an_ordinary_user_turn_is_never_silenced_or_touched(harness):
    """No ticket: the delivery gate must be a no-op for a normal turn, and write nothing."""
    before = harness.blob
    agent = FakeAgent(harness.entry.session_id)
    text, transformed, pre = _deliver(agent, "这是正常的回复")
    assert (text, transformed, pre) == ("这是正常的回复", False, None)
    assert harness.blob == before, "an unreleased turn must not touch this session's state"






# ── a store whose key is absent or unreadable ───────────────────────────────────────────────






class _UnreadableStore:
    """The real store with one switch: the plugin's own key vanishes, or the read fails outright.

    Both are *injections against a live round*, not fixtures: the wake writes the key through this
    same handle, and only the later probe is affected — exactly the shape of a store fault or an
    externally cleared key that appears while a released round is still running.
    """

    def __init__(self, inner, mode="ok"):
        self.inner = inner
        self.mode = mode  # "ok" | "absent" | "unreadable"

    def get_session_metadata(self, session_key, key, default=None):
        if key == C.META_KEY and self.mode != "ok":
            if self.mode == "absent":
                return default
            raise RuntimeError("store read failed")
        return self.inner.get_session_metadata(session_key, key, default)

    def __getattr__(self, name):
        return getattr(self.inner, name)






def test_an_unconfigured_session_has_no_delivery_gate(plugin, store):
    """Without configuration there is no state, so nothing can be silenced by mistake."""
    source = support.dm_source("88")
    entry = support.seed_session(store, source)
    agent = FakeAgent(entry.session_id)
    text, transformed, _ = _deliver(agent, "随便一句话")
    assert (text, transformed) == ("随便一句话", False)
    assert store.get_session_metadata(entry.session_key, C.META_KEY, "MISSING") == "MISSING"


def test_a_broken_store_refuses_wakes_but_never_blocks_a_user_message(plugin):
    """A store the plugin cannot read is an unconfigured session: no proactive guess, no dropped
    user traffic.

    The reason code is ``off:unverified`` rather than ``off:no-blob``: the plugin cannot tell an
    unreadable key from a permanently closed one (the origin marker lives in the blob it cannot
    read), so "the store could not be read" must not be reported as "there is no session here". The
    invariant is: the wake is skipped and the user message is admitted.
    """

    class BrokenStore:
        def peek_session_id(self, key):
            raise RuntimeError("boom")

        def get_session_metadata(self, *a, **kw):
            raise RuntimeError("boom")

        def set_session_metadata(self, *a, **kw):
            raise RuntimeError("boom")

    wake = support.make_wake("telegram:dm:whatever", "sess-x")
    assert support.invoke_hook("pre_gateway_dispatch", event=wake, gateway=None,
                               session_store=BrokenStore()) == [
        {"action": "skip", "reason": C.REASON_UNVERIFIED}]

    user = support.make_event(text="你好", session_key="telegram:dm:whatever")
    assert support.invoke_hook("pre_gateway_dispatch", event=user, gateway=None,
                               session_store=BrokenStore()) == [], "user traffic is never blocked"




# ── plugin-level settings change behaviour ─────────────────────────────────────────────────
# The values come from the isolated profile's real config.yaml through ``ctx.get_config``, written
# by the ``plugin_settings`` fixture — nothing here stubs the settings module.






def test_the_timestamp_sanity_setting_decides_whether_a_stale_timestamp_is_used(plugin_settings,
                                                                                store, rt):
    """Same message time, two configured bands, two anchors — and the anchor is observable state."""
    from datetime import datetime

    stale_by = 10 * 60.0  # the message claims to be ten minutes old

    # Default band (7 days): the message's own time is usable, so the window is anchored on it.
    wide = SessionHarness(store, rt, source=support.dm_source("104"))
    wide.clock["now"] = T0 + HOUR
    stamp = datetime.fromtimestamp(wide.clock["now"] - stale_by)
    assert support.invoke_hook(
        "pre_gateway_dispatch",
        event=support.make_event(text="刚在忙", session_key=wide.entry.session_key,
                                 source=wide.source, timestamp=stamp),
        gateway=None, session_store=store) == []
    assert wide.state["anchor_at"] == pytest.approx(stamp.timestamp(), abs=1)

    plugin_settings({"event_timestamp_sanity_seconds": 60})
    assert rt.event_timestamp_sanity() == 60.0, "the configured band must reach the hook"
    narrow = SessionHarness(store, rt, source=support.dm_source("105"))
    narrow.clock["now"] = T0 + HOUR
    stamp = datetime.fromtimestamp(narrow.clock["now"] - stale_by)
    assert support.invoke_hook(
        "pre_gateway_dispatch",
        event=support.make_event(text="刚在忙", session_key=narrow.entry.session_key,
                                 source=narrow.source, timestamp=stamp),
        gateway=None, session_store=store) == []
    assert narrow.state["anchor_at"] == narrow.clock["now"], (
        "outside the configured band the hook anchors on its own clock")


def test_an_unusable_sanity_setting_cannot_turn_a_stale_timestamp_into_an_early_release(
        plugin_settings, store, rt):
    """Fail closed, the direction that matters: a band that would accept anything must not make the
    plugin release a round it should not release.

    A month-old timestamp anchors the window in the past, from where the sampled deadline is
    already due — i.e. it turns the next wake into an immediate proactive message. With the band
    collapsed to its default the timestamp is ignored, the window is anchored on "now", and the
    next wake is refused exactly as it would be without any timestamp at all.
    """
    from datetime import datetime

    plugin_settings({"event_timestamp_sanity_seconds": 10 ** 12})  # "never stale": a unit slip
    assert rt.event_timestamp_sanity() == C.EVENT_TIMESTAMP_SANITY_DEFAULT_SECONDS

    harness = SessionHarness(store, rt, source=support.dm_source("106"))
    month_old = datetime.fromtimestamp(harness.clock["now"] - 30 * 24 * HOUR)
    assert support.invoke_hook(
        "pre_gateway_dispatch",
        event=support.make_event(text="上个月的消息", session_key=harness.entry.session_key,
                                 source=harness.source, timestamp=month_old),
        gateway=None, session_store=store) == []
    assert harness.state["anchor_at"] == harness.clock["now"], "the month-old stamp is ignored"
    assert harness.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}], (
        "no release can be manufactured out of a refused setting")
