"""The proactive tool gate on Hermes' **real dispatch path**.

The live gate is the boolean switch ``proactive_tools_enabled``, enforced in
:func:`proactive.trigger.handle_tool_brake` for a turn this plugin bound (section 7 below). The pure
classification tables (``proactive.permissions``) and the installed-copy drift check are also real
code and are covered here.

What the blocking assertions below never do is call a classification function as the *only* check:
the plugin is the **installed** copy, loaded by hermes' own loader (fixture ``plugin`` /
``plugin_runtime``), and the switch is read through the same ``ctx.get_config`` path an operator
edits.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import p4_support as P
import pytest
import support
from proactive import contract as C
from proactive import permissions as perm

T0 = P.NOW

#: The forbidden classes: write memory / send a message.
WRITE_TOOLS = (
    (C.TOOL_WRITE_MEMORY, C.DENIED_WRITE_TOOL),
    (C.TOOL_SEND_MESSAGE, C.DENIED_SEND_TOOL),
)


# ── harness ─────────────────────────────────────────────────────────────────────────────────


class Harness:
    """One configured session plus the real dispatch hook that releases a wake for it.

    Same shape as the other integration harnesses: a real ``SessionStore``, real session metadata, the
    plugin's own seams for clock/RNG. ``release_wake()`` is the production path that creates the
    in-flight release ticket.
    """

    def __init__(self, store, rt, source=None, raw=None, start=T0):
        self.rt = rt
        self.store = store
        self.source = source or support.dm_source()
        self.clock = {"now": float(start)}
        rt.set_clock(lambda: self.clock["now"])
        rt.set_rng(support.FixedRng(0.0))
        self.entry = support.seed_session(store, self.source)
        self.dims = support.dims_for(store, self.entry, self.source)
        assert rt.enable_session(store, self.entry.session_key, self.dims,
                                 dict(raw or support.CONFIG_A)) is True
        # A wake is only released when an assembly is installed; the harness installs the stand-in
        # a configured profile would have.
        rt.set_content(P.release_material(lambda: self.clock["now"]))

    @property
    def state(self):
        return (self.store.get_session_metadata(self.entry.session_key, C.META_KEY, None)
                or {}).get("state", {})

    @property
    def session_id(self) -> str:
        return self.entry.session_id

    def due(self) -> None:
        self.clock["now"] = float(self.state["deadline"] + 1)

    def release_wake(self):
        """Fire the real eligibility hook for a heartbeat wake and return its directives."""
        return support.released(support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_wake(self.entry.session_key, self.entry.session_id,
                                    source=self.source),
            gateway=None, session_store=self.store))

    def user_message(self, text: str = "在吗"):
        self.clock["now"] += 60.0
        return support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_event(text=text, session_key=self.entry.session_key,
                                     source=self.source,
                                     timestamp=support.at_clock(self.clock["now"])),
            gateway=None, session_store=self.store)


class ProbeHandler:
    """A real callable with its own counter — the proof that a refused call never executed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.seen: list = []

    def __call__(self, args: Any = None, **_kwargs: Any) -> str:
        self.calls += 1
        self.seen.append(args)
        return json.dumps({"ok": True, "handler": self.name})

    def reset(self) -> None:
        self.calls = 0
        self.seen.clear()


#: Registered once per test process (hermes' registry has no public removal); counters are reset
#: before every test by ``_clean_probes``.
_PROBES: Dict[str, ProbeHandler] = {}


def probe_tool(name: str) -> ProbeHandler:
    """Register (once) a probe tool in hermes' own registry under *name*."""
    handler = _PROBES.get(name)
    if handler is None:
        from tools.registry import registry

        handler = ProbeHandler(name)
        registry.register(
            name, "proactive-probe",
            {"name": name, "description": "proactive-conversation test probe",
             "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}},
            handler,
        )
        _PROBES[name] = handler
    return handler


def call_tool(name: str, args: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    """One tool call through hermes' real dispatch entry point; returns the parsed tool result."""
    from model_tools import handle_function_call

    return json.loads(handle_function_call(
        name, dict(args), task_id="task-1", session_id=session_id,
        tool_call_id="call-1", turn_id="turn-1"))


def hook_directives(tool_name: str, *, session_id: str, args: Optional[Dict[str, Any]] = None):
    """The ``pre_tool_call`` directives hermes' delivery manager returns for one call."""
    return support.invoke_hook("pre_tool_call", tool_name=tool_name, args=dict(args or {}),
                               task_id="task-1", session_id=session_id, tool_call_id="call-1",
                               turn_id="turn-1", api_request_id="req-1", middleware_trace=[])


@pytest.fixture(autouse=True)
def _clean_probes():
    for handler in _PROBES.values():
        handler.reset()
    yield


@pytest.fixture
def rt(plugin_runtime):
    return plugin_runtime


@pytest.fixture
def hs(store, rt):
    h = Harness(store, rt)
    yield h
    rt.reset_seams()


# ── 1. the hook exists on the real plugin path ──────────────────────────────────────────────


def test_the_manifest_declares_the_tool_gate_and_the_callback_is_registered(plugin):
    import hermes_cli.plugins as plugins_mod

    assert "pre_tool_call" in (plugin.manifest.provides_hooks or []), \
        "plugin.yaml must declare pre_tool_call"
    assert plugins_mod.has_hook("pre_tool_call"), "no live subscriber for pre_tool_call"
    callbacks = plugins_mod.get_plugin_manager().iter_hook_callbacks("pre_tool_call")
    assert any(cb.__name__ == "pre_tool_call" and str(cb.__module__).endswith("proactive.runtime")
               for cb in callbacks), f"the registered callback is not the plugin's gate: {callbacks}"


def test_without_a_released_round_the_gate_has_no_opinion(hs, rt):
    """No ticket → no directive at all, for a write tool and a read tool alike."""
    assert rt.tickets() == {}
    assert hook_directives("memory", session_id=hs.session_id) == []
    assert hook_directives("read_file", session_id=hs.session_id) == []
    assert rt.tool_gate_counters() == {"attempts": {}, "denied": {}, "denied_reasons": {}}


# ── 2. inside a released round: the write tools are refused before they run ─────────────────








# ── 3. the gate's scope: an ordinary turn is never affected ─────────────────────────────────


def test_an_ordinary_user_turn_has_no_ticket_and_runs_its_tools(hs, rt):
    """The whole safety argument: only a round this plugin released carries a ticket."""
    assert hs.user_message() == []
    assert rt.tickets() == {}, "a user message must not create a release ticket"

    name = "pchat_probe_ordinary"          # a name the gate cannot vouch for: refused when released
    handler = probe_tool(name)
    assert hook_directives(name, session_id=hs.session_id) == []
    out = call_tool(name, {"text": "ordinary turn"}, hs.session_id)

    assert handler.calls == 1, "an ordinary conversation must not be interfered with"
    assert out == {"ok": True, "handler": name}
    assert rt.tool_gate_counters()["attempts"] == {}












# ── 4. the failure modes are closed, not open ───────────────────────────────────────────────






# ── 5. the classification itself (unit; the end-to-end tests above use the same table) ──────


def test_classification_says_read_only_when_code_says_read():
    for name in ("read_file", "search_files", "web_search", "session_search", "skill_view"):
        assert perm.classify_tool(name) == perm.CAP_READ, name
    for name in ("memory", "write_file", "terminal", "delegate_task"):
        assert perm.classify_tool(name) == perm.CAP_WRITE, name
    for name in (C.TOOL_SEND_MESSAGE, "text_to_speech", "react_to_message"):
        assert perm.classify_tool(name) == perm.CAP_EGRESS, name
    assert perm.classify_tool("a_tool_from_the_future") == perm.CAP_UNKNOWN
    assert perm.classify_tool("") == perm.CAP_UNKNOWN

    # A refused call never claims a capability of its own: the request is not consulted at all.
    assert perm.released_round_denial(C.TOOL_WRITE_MEMORY).capability == perm.CAP_WRITE
    assert perm.released_round_denial("a_tool_from_the_future").reason == C.DENIED_UNKNOWN_TOOL
    assert perm.released_round_denial("read_file") is None


def test_the_plugin_registry_wins_over_the_name_tables():
    """The composition root's classification is reused: a read adapter this plugin injected is read."""
    boundary = perm.make_read_only_boundary([
        perm.ToolSpec(name="calendar_recent", capability=perm.CAP_READ, handler=lambda: []),
        perm.ToolSpec(name="calendar_write", capability=perm.CAP_WRITE, handler=lambda **_: None),
    ])
    specs = boundary.executor.specs()

    assert perm.classify_tool("calendar_recent", specs=specs) == perm.CAP_READ
    assert perm.classify_tool("calendar_write", specs=specs) == perm.CAP_WRITE
    assert perm.released_round_denial("calendar_recent", specs=specs) is None
    assert perm.released_round_denial("calendar_write", specs=specs).reason == C.DENIED_WRITE_TOOL


def test_the_read_list_cannot_overlap_the_dangerous_classes():
    assert not (perm.READ_TOOL_NAMES & perm.WRITE_TOOL_NAMES)
    assert not (perm.READ_TOOL_NAMES & perm.EGRESS_TOOL_NAMES)
    assert not (perm.WRITE_TOOL_NAMES & perm.EGRESS_TOOL_NAMES)
    for name in perm.READ_TOOL_NAMES:
        assert name and name == name.strip(), name


def test_the_block_message_carries_the_reason_code():
    for name, reason in WRITE_TOOLS:
        directive = perm.block_directive(perm.released_round_denial(name))
        assert directive["action"] == "block"
        assert directive["message"].strip(), "a block directive requires a non-empty message"
        assert reason in directive["message"]
        assert directive["message"].startswith(C.ROUND_GATE_MESSAGE_PREFIX)


# ── 6. no drift between the files under test and the installed copy ─────────────────────────


def test_the_installed_copy_carries_the_same_gate_files(installed_plugin_dir):
    import hashlib

    for rel in ("__init__.py", "plugin.yaml", "proactive/permissions.py", "proactive/runtime.py"):
        here = (support.PKG_ROOT / rel).read_bytes()
        there = (installed_plugin_dir / rel).read_bytes()
        assert hashlib.sha256(here).hexdigest() == hashlib.sha256(there).hexdigest(), rel


# ── 7. the live boolean switch: proactive_tools_enabled ─────────────────────────────────────
#
# The boolean switch is enforced in ``trigger.handle_tool_brake`` for a turn this plugin bound at
# ``pre_llm_call``. These unit cases pin the decision itself, with the turn binding seeded directly
# and the round-accounting step stubbed: the end-to-end shape (pre_llm_call → pre_tool_call) lives in
# ``tests/host_e2e/test_pb_prompt_config_and_tool_access.py``.


def _bind_tools_switch(monkeypatch, enabled):
    """Point the adapter at a context that answers only ``proactive_tools_enabled``."""
    from types import SimpleNamespace

    from proactive import runtime, trigger

    runtime.bind_plugin_context(SimpleNamespace(
        get_config=lambda key, default=None: enabled if key == C.SETTING_TOOLS_ENABLED else default
    ))
    # A refused tool also settles the round as non-visible; that write is not what this section
    # measures, so keep it out of the decision unit.
    monkeypatch.setattr(trigger, "_settle_nonvisible", lambda *a, **k: None)


def test_the_boolean_switch_refuses_every_tool_in_a_bound_round(monkeypatch):
    from proactive import runtime, trigger

    _bind_tools_switch(monkeypatch, False)
    trigger._turns["t-tools-off"] = {"bound": True}
    try:
        for name in ("terminal", "write_file", "text_to_speech", "some_future_tool"):
            blocked = runtime.pre_tool_call(tool_name=name, turn_id="t-tools-off", session_id="s")
            assert blocked is not None and blocked["action"] == "block", name
            assert "denied:proactive-tool" in blocked["message"], name
    finally:
        trigger._turns.pop("t-tools-off", None)


def test_the_boolean_switch_lets_every_tool_through_when_true(monkeypatch):
    from proactive import runtime, trigger

    _bind_tools_switch(monkeypatch, True)
    trigger._turns["t-tools-on"] = {"bound": True}
    try:
        for name in ("terminal", "write_file", "payment_charge"):
            assert runtime.pre_tool_call(tool_name=name, turn_id="t-tools-on",
                                         session_id="s") is None, name
    finally:
        trigger._turns.pop("t-tools-on", None)


def test_an_unbound_turn_is_never_braked_whatever_the_switch_says(monkeypatch):
    from proactive import runtime

    _bind_tools_switch(monkeypatch, False)
    assert runtime.pre_tool_call(tool_name="terminal", turn_id="t-user",
                                 session_id="s") is None
