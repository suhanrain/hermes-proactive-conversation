"""Trigger clock wiring — arm the tick loop and feed it the declared sessions.

These cases pin the wiring around the trigger *clock* (`tick`/`arm_clock`) on a live host loop:

* the provider enumerates the declared, usable sessions and reuses each blob's stored dims verbatim
  (so `store.read`'s exact dims match holds);
* a single `trigger.tick` over that provider injects exactly once;
* `arm_trigger_clock` is idempotent and needs a bound context with `spawn_task` and a store handle.

Real `SessionStore` (support.new_store); no live gateway, no network.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TESTS = _HERE.parent
for _p in (str(_TESTS), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

import support  # noqa: E402  (pins a throwaway HERMES_HOME before any hermes module import)

from proactive import contract as C          # noqa: E402
from proactive import host_adapter, runtime, trigger  # noqa: E402

T0 = 1_800_000_000.0
DUE = T0 + 3600.0


def _config(**over):
    base = {"enabled": True, "timezone": "Asia/Shanghai", "dnd_start": "23:00", "dnd_end": "09:00",
            "min_idle_seconds": 3600, "max_idle_seconds": 3600,
            "window_max_attempts": 4, "window_min_visible": 1, "day_max_silent": 2, "day_max_attempts": 2}
    base.update(over)
    return base


def _declaration(skey, sid, **over):
    decl = {"session_key": skey, "session_id": sid}
    decl.update(_config(**over))
    return decl


class _Ctx:
    """A host context: get_config (settings), and optionally spawn_task (the clock seam)."""

    def __init__(self, settings, *, with_spawn=True):
        self._settings = dict(settings)
        self.spawned = []
        self._with_spawn = with_spawn
        if with_spawn:
            self.spawn_task = self._spawn_task

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def _spawn_task(self, coro, name=None):
        self.spawned.append(coro)
        coro.close()  # don't run the loop in the test; just prove it was spawned
        return object()


@pytest.fixture(autouse=True)
def _clean():
    runtime.reset_seams()
    trigger.reset()
    yield
    runtime.reset_seams()
    trigger.reset()
    host_adapter.reset_context()


def _make(tmp_path, *, config=None, now=T0):
    """Seed + enable a real session blob (mirror) and return ``(store, skey, sid, dims)``."""
    store = support.new_store(tmp_path)
    source = support.dm_source(chat_id="oc_pb")
    entry = support.seed_session(store, source)
    dims = support.dims_for(store, entry, source)
    runtime.set_clock(lambda: now)
    assert runtime.enable_session(store, entry.session_key, dims, config or _config())
    runtime.set_clock(lambda: DUE)
    return store, entry.session_key, entry.session_id, dims


def _recorder(result=True):
    calls = []
    def inject(text, session_key=None):
        calls.append((text, session_key))
        return result
    return inject, calls


def test_provider_lists_declared_usable_session_with_stored_dims(tmp_path):
    store, skey, sid, dims = _make(tmp_path)
    runtime.bind_plugin_context(_Ctx({C.SETTING_SESSION_DECLARATIONS: [_declaration(skey, sid)]}))

    sessions = runtime._clock_sessions(store)

    assert len(sessions) == 1
    assert sessions[0]["session_key"] == skey
    assert sessions[0]["session_id"] == sid
    # The dims reused are exactly the ones the blob stored (so store.dims_match holds).
    assert sessions[0]["dims"] == dims


def test_provider_skips_session_without_a_blob(tmp_path):
    store, skey, sid, dims = _make(tmp_path)
    runtime.bind_plugin_context(_Ctx({C.SETTING_SESSION_DECLARATIONS: [_declaration(skey, sid)]}))
    assert runtime.revoke_session(store, skey)  # clears the metadata key → no stored dims

    assert runtime._clock_sessions(store) == []


def test_provider_skips_undeclared_session(tmp_path):
    store, skey, sid, dims = _make(tmp_path)
    # A declaration for a *different* session key: this one is not authorised.
    runtime.bind_plugin_context(_Ctx({C.SETTING_SESSION_DECLARATIONS: [_declaration("agent:main:feishu:dm:oc_other", sid)]}))

    assert runtime._clock_sessions(store) == []


def test_one_tick_over_the_provider_injects_exactly_once(tmp_path):
    store, skey, sid, dims = _make(tmp_path)
    runtime.bind_plugin_context(_Ctx({C.SETTING_SESSION_DECLARATIONS: [_declaration(skey, sid)]}))
    sessions = runtime._clock_sessions(store)
    inject, calls = _recorder(True)

    outcomes = trigger.tick(store, sessions, DUE, inject=inject, rng=support.FixedRng(0.5))

    assert outcomes[0][1].accepted is True
    assert len(calls) == 1
    assert calls[0][1] == skey
    assert "[proactive-conversation]" in calls[0][0]  # the injected prompt, not model-author material


def test_arm_captures_handle_and_spawns_once_idempotently(tmp_path):
    store, skey, sid, dims = _make(tmp_path)
    ctx = _Ctx({C.SETTING_SESSION_DECLARATIONS: [_declaration(skey, sid)], C.SETTING_TRIGGER_MODE: "on"})
    runtime.bind_plugin_context(ctx)

    assert runtime.arm_trigger_clock(store) is True
    assert len(ctx.spawned) == 1, "the clock loop is spawned exactly once"
    assert runtime.arm_trigger_clock(store) is True
    assert len(ctx.spawned) == 1, "arming again is a no-op (the _clock_armed guard)"


def test_arm_without_a_store_handle_is_false(tmp_path):
    ctx = _Ctx({})
    runtime.bind_plugin_context(ctx)
    assert runtime.arm_trigger_clock() is False
    assert ctx.spawned == []


def test_arm_without_spawn_task_is_false(tmp_path):
    store, skey, sid, dims = _make(tmp_path)
    ctx = _Ctx({C.SETTING_SESSION_DECLARATIONS: [_declaration(skey, sid)], C.SETTING_TRIGGER_MODE: "on"}, with_spawn=False)
    runtime.bind_plugin_context(ctx)
    assert runtime.arm_trigger_clock(store) is False


def test_arm_is_false_when_trigger_mode_is_off(tmp_path):
    store, skey, sid, dims = _make(tmp_path)
    ctx = _Ctx({C.SETTING_SESSION_DECLARATIONS: [_declaration(skey, sid)]})  # no trigger_mode ⇒ off
    runtime.bind_plugin_context(ctx)
    assert runtime.arm_trigger_clock(store) is False
    assert ctx.spawned == []
    # and the provider releases nothing when mode is off
    assert runtime._clock_provider()[1] == []


class _FakeTask:
    """Minimal stand-in for the asyncio.Task ``ctx.spawn_task`` really returns."""

    def __init__(self):
        self._done = False

    def done(self):
        return self._done

    def cancel(self):
        self._done = True


def test_arm_rebuilds_the_loop_after_the_task_is_cancelled(tmp_path):
    # The host cancels spawn_task tasks on unload/reload. A cancelled loop must be re-spawned on the
    # next arm, not latched dead by the one-way boolean (the "one message then silent for an hour"
    # failure). A still-live task stays idempotent.
    store, skey, sid, dims = _make(tmp_path)
    ctx = _Ctx({C.SETTING_SESSION_DECLARATIONS: [_declaration(skey, sid)], C.SETTING_TRIGGER_MODE: "on"})
    tasks = []

    def _spawn(coro, name=None):
        ctx.spawned.append(coro)
        coro.close()
        t = _FakeTask()
        tasks.append(t)
        return t

    ctx.spawn_task = _spawn
    runtime.bind_plugin_context(ctx)

    assert runtime.arm_trigger_clock(store) is True
    assert len(ctx.spawned) == 1
    # A live task is not re-spawned (idempotent).
    assert runtime.arm_trigger_clock(store) is True
    assert len(ctx.spawned) == 1, "a live loop task is not duplicated"
    # The host retires the loop → the next arm must rebuild it.
    tasks[0].cancel()
    assert runtime.arm_trigger_clock(store) is True
    assert len(ctx.spawned) == 2, "a cancelled loop is re-spawned on the next arm"


def test_arm_rebuilds_a_stuck_loop_with_a_stale_heartbeat(tmp_path):
    # A task can report done()==False (looks alive) while the event loop it ran on was replaced, so it
    # never ticks again. The heartbeat is the only honest liveness signal: a stale one must trigger a
    # re-spawn, else the clock is dead for the rest of the process ("one message then silent for
    # hours"). This is the case the plain done() check missed.
    store, skey, sid, dims = _make(tmp_path)
    ctx = _Ctx({C.SETTING_SESSION_DECLARATIONS: [_declaration(skey, sid)], C.SETTING_TRIGGER_MODE: "on"})
    tasks = []
    events = []

    class _OrderedTask(_FakeTask):
        def cancel(self):
            events.append("cancel-old")
            super().cancel()

    def _spawn(coro, name=None):
        events.append("spawn-new")
        ctx.spawned.append(coro)
        coro.close()
        t = _OrderedTask()  # never done(): the loop simply stopped ticking
        tasks.append(t)
        return t

    ctx.spawn_task = _spawn
    runtime.bind_plugin_context(ctx)

    assert runtime.arm_trigger_clock(store) is True
    assert len(ctx.spawned) == 1
    # The loop has not heartbeated for far longer than a few intervals, yet the task looks alive.
    assert tasks[0].done() is False
    trigger._last_tick_ts = time.monotonic() - 10_000.0
    assert runtime.arm_trigger_clock(store) is True
    assert len(ctx.spawned) == 2, "a stuck-but-not-done loop is re-spawned when its heartbeat is stale"
    assert events == ["spawn-new", "cancel-old", "spawn-new"], \
        "the stale loop is cancelled before the replacement is spawned"


def test_old_loop_cancellation_cannot_clear_replacement_or_trigger_third_spawn():
    """An old task may observe cancellation after arm_clock has installed its replacement."""
    events = []

    class _Ctx:
        def spawn_task(self, coro, name=None):
            events.append("spawn")
            return asyncio.create_task(coro)

    async def scenario():
        host_adapter.bind_context(_Ctx())
        provider = lambda: (None, [])
        assert trigger.arm_clock(provider, interval=3600.0) is True
        old = trigger._clock_task
        trigger._last_tick_ts = time.monotonic() - 20_000.0
        assert trigger.arm_clock(provider, interval=3600.0) is True
        replacement = trigger._clock_task
        assert replacement is not old
        assert old.cancelling() == 1
        await asyncio.sleep(0)
        assert trigger._clock_task is replacement
        assert trigger._clock_armed is True
        assert trigger.arm_clock(provider, interval=3600.0) is True
        assert events == ["spawn", "spawn"]
        replacement.cancel()
        await asyncio.gather(old, replacement, return_exceptions=True)

    asyncio.run(scenario())


def test_arm_judges_a_non_probeable_task_by_its_heartbeat(tmp_path):
    # Some hosts return None from spawn_task (no probeable task object). That arm must still be
    # re-armed once its heartbeat goes stale: liveness is judged by the heartbeat, not by whatever
    # the spawn seam happened to return.
    store, skey, sid, dims = _make(tmp_path)
    ctx = _Ctx({C.SETTING_SESSION_DECLARATIONS: [_declaration(skey, sid)], C.SETTING_TRIGGER_MODE: "on"})

    def _spawn(coro, name=None):
        ctx.spawned.append(coro)
        coro.close()
        return None  # not probeable

    ctx.spawn_task = _spawn
    runtime.bind_plugin_context(ctx)

    assert runtime.arm_trigger_clock(store) is True
    assert len(ctx.spawned) == 1
    # A fresh heartbeat keeps the same arm (no duplicate clock).
    assert runtime.arm_trigger_clock(store) is True
    assert len(ctx.spawned) == 1, "a live non-probeable clock is not duplicated"
    # A stale heartbeat means the loop is dead even though no task object can say so.
    trigger._last_tick_ts = 0.0
    assert runtime.arm_trigger_clock(store) is True
    assert len(ctx.spawned) == 2, "a stale non-probeable clock is re-armed"

