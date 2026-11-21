"""P2c unit tests — the plugin-host detector and the refusal it drives (no hermes import).

Card ``t_195a2515``: the plugin must never *appear* to police traffic it cannot see. Under
``plugins.isolation: host`` hermes loads the plugin in a child process where the event's
``_heartbeat_session_id`` stamp does not survive the wire codec and the live gateway / session store
arrive as ``Opaque`` placeholders, so neither the per-session policy nor the wake-vs-user distinction
is available. ``register()`` therefore refuses to arm (registers no hook at all), and ``_dispatch``
has a call-time net for the case where something else armed the callbacks anyway.

These tests are deliberately hermes-free: they import the plugin entry point from a file path in a
bare package spec and drive it with a recording context, exactly the way the plugin capability probe
does. The end-to-end behaviour under the real loader is measured in ``host_e2e/test_p2c_host_refusal.py``
(and the degraded argument shapes themselves in ``host_e2e/probe_plugin.py``).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

import support

ENTRY = support.PKG_ROOT / "__init__.py"


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _load_entry(name: str):
    """Import the plugin entry point as a package (so its relative imports resolve)."""
    spec = importlib.util.spec_from_file_location(
        name, ENTRY, submodule_search_locations=[str(support.PKG_ROOT)],
    )
    assert spec and spec.loader, f"cannot build a spec for {ENTRY}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class RecordingCtx:
    """A context that records what got registered — the shape hermes' in-process loader passes."""

    def __init__(self) -> None:
        self.hooks: list = []

    def register_hook(self, name: str, callback) -> None:
        self.hooks.append((name, callback))

    def get_config(self, *a, **kw):  # never called at registration time
        raise AssertionError("get_config must not be called while registering")


def _remote_ctx():
    """A stand-in whose *class name* is what hermes' plugin-host facade is called."""

    class RemotePluginContext(RecordingCtx):
        pass

    return RemotePluginContext()


def _manifest_hook_names():
    """The hook names ``plugin.yaml`` declares, in order.

    Read with a four-line scan rather than a YAML parser on purpose: this file must stay runnable in
    the isolation lane without adding a dependency, and the block is flat (``provides_hooks:`` followed
    by ``- name`` lines). A parse that cannot find the block raises, so a renamed key is a failure, not
    an empty list.
    """
    lines = (support.PKG_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines()
    names = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("provides_hooks:"):
            inside = True
            continue
        if inside:
            if stripped.startswith("- "):
                names.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("#"):
                break
    assert names, "plugin.yaml declares no provides_hooks entries"
    return names


class Record(dict):
    """The wire codec's dataclass stand-in (name is what the detector keys on)."""


class Opaque:
    """The wire codec's stand-in for a live object owned by the other process."""


class ExplodingGateway:
    """Any attribute read is a bug: the degraded call must decide nothing at all."""

    def __getattr__(self, item):
        raise AssertionError(f"the gate read gateway.{item} on a degraded round")


# ── STEP 1: what identifies the plugin host ──────────────────────────────────────────────────


def test_env_flag_is_the_primary_detector(monkeypatch):
    from proactive import isolation

    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    assert isolation.in_plugin_host() is True
    assert isolation.plugin_host_signature() == "env:HERMES_PLUGIN_HOST_PROCESS=1"


@pytest.mark.parametrize("value", ["", "0", "true", "yes", " 1x", "2"])
def test_only_hermes_own_value_counts(monkeypatch, value):
    """hermes sets ``=1``; a lookalike value must not make this plugin inert."""
    from proactive import isolation

    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", value)
    assert isolation.in_plugin_host() is False, value
    assert isolation.plugin_host_signature() is None, value


def test_ctx_class_is_the_secondary_detector(monkeypatch):
    from proactive import isolation

    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    assert isolation.is_host_context(_remote_ctx()) is True
    assert isolation.plugin_host_signature(_remote_ctx()) == "ctx:RemotePluginContext"
    assert isolation.is_host_context(RecordingCtx()) is False
    assert isolation.plugin_host_signature(RecordingCtx()) is None


# ── load time: refuse to arm ─────────────────────────────────────────────────────────────────


def test_register_refuses_in_the_plugin_host(monkeypatch, caplog):
    """The detector fires, the load fails, and *nothing* is registered."""
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    entry = _load_entry("p2c_entry_refuse")
    ctx = RecordingCtx()

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as excinfo:
        entry.register(ctx)

    assert type(excinfo.value).__name__ == "PluginHostIsolationRefused", type(excinfo.value)
    assert ctx.hooks == [], f"a refused load must register nothing, got {ctx.hooks}"
    message = str(excinfo.value)
    assert "degraded:plugin-host" in message, message
    assert "env:HERMES_PLUGIN_HOST_PROCESS=1" in message, message
    assert "plugins.isolation: in_process" in message, message
    assert "degraded:plugin-host" in caplog.text, caplog.text


def test_register_refuses_on_the_ctx_detector_alone(monkeypatch, caplog):
    """Defence in depth: with the env flag absent, the facade class alone still refuses."""
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    entry = _load_entry("p2c_entry_refuse_ctx")
    ctx = _remote_ctx()

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as excinfo:
        entry.register(ctx)

    assert type(excinfo.value).__name__ == "PluginHostIsolationRefused", type(excinfo.value)
    assert ctx.hooks == [], f"a refused load must register nothing, got {ctx.hooks}"
    assert "ctx:RemotePluginContext" in str(excinfo.value), str(excinfo.value)


def test_register_still_arms_in_process(monkeypatch):
    """No false positive: the in-process load arms every hook `plugin.yaml` declares, in order.

    The list is read from the manifest — the host-facing contract — instead of being written out
    here, so a manifest that drops or renames a hook fails **here** rather than agreeing with
    whichever list is read twice.
    """
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    entry = _load_entry("p2c_entry_clean")
    ctx = RecordingCtx()

    entry.register(ctx)

    from proactive import host_adapter

    declared = _manifest_hook_names()
    assert declared == ["pre_gateway_dispatch", "transform_llm_output",
                        "pre_tool_call", "pre_llm_call"], declared
    assert list(host_adapter.hook_names()) == declared, host_adapter.hook_names()
    names = [name for name, _ in ctx.hooks]
    assert names == declared, names
    assert all(callable(cb) for _, cb in ctx.hooks)
    from proactive import runtime

    runtime.reset_seams()


# ── hook time: the net, and the shape it recognises ──────────────────────────────────────────


def test_degraded_shape_names_what_crossed_the_wire():
    from proactive import isolation

    assert isolation.degraded_shape(Record({}), Opaque(), Opaque()) == (
        "event:Record,gateway:Opaque,session_store:Opaque"
    )
    assert isolation.degraded_shape({"text": "hi"}, Opaque(), None) == "event:dict,gateway:Opaque"
    assert isolation.degraded_shape(RecordingCtx(), RecordingCtx(), RecordingCtx()) is None
    assert isolation.is_wire_record(Record({})) is True
    assert isolation.is_wire_record({}) is False, "a plain dict is not the wire codec's Record"
    assert isolation.is_placeholder(Opaque()) is True
    assert isolation.is_placeholder(None) is False


def test_degraded_round_is_passed_through_untouched(monkeypatch, caplog):
    """Armed anyway (a probe, a hand-built context): decide nothing, block nothing, read nothing."""
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    entry = _load_entry("p2c_entry_net")
    ctx = RecordingCtx()
    entry.register(ctx)
    runtime = sys.modules["p2c_entry_net.proactive.runtime"]
    callback = dict(ctx.hooks)["pre_gateway_dispatch"]

    event = Record({"text": "看看X", "message_id": None, "metadata": {}})
    with caplog.at_level(logging.WARNING):
        verdict = callback(event=event, gateway=ExplodingGateway(), session_store=Opaque())

    assert verdict is None, f"a degraded round must never be blocked: {verdict}"
    assert "degraded:plugin-host" in caplog.text, caplog.text
    assert "passing the round through untouched" in caplog.text, caplog.text
    assert runtime.tickets() == {}, "a degraded round must not create state either"
    runtime.reset_seams()
