"""Under ``plugins.isolation: host`` the plugin refuses to arm.

Why: in hermes' plugin-host child the event crosses the wire as a ``Record`` (its
``_heartbeat_session_id`` stamp does not survive), and the live gateway runner / session store arrive
as ``Opaque`` placeholders. Neither the wake-vs-user distinction nor the per-session policy is
available there, so *any* arming would be a guess: refusing every round blocks ordinary user
messages, releasing every round sends proactive wakes out of sessions the user never enabled. The
plugin therefore registers no hook at all in that mode, and hermes' own loader records the reason.

Every test here runs the real loader against a real throwaway home and compares the two isolation
modes, so the refusal cannot silently spread to the installed default (``in_process``) or be dropped
from the host mode without a red test.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

import pytest

import harness as H
import support
from probe_plugin import read_records

#: The installed default arms the full hook set.
HOOKS = ("pre_gateway_dispatch", "transform_llm_output", "pre_tool_call", "pre_llm_call")


@pytest.fixture
def srv():
    H.ensure_agent_importable()
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text

    server = FakeLLMServer(default_text="好的")
    server.start()
    server.push(Text("user-reply"))
    try:
        yield server
    finally:
        server.stop()


def _host(tmp_path: Path, srv, isolation: str, *, probe_log: Path | None = None, refusal: bool = False):
    host = H.IsolatedHost(tmp_path, srv, isolation=isolation, probe_log=probe_log,
                          expect_refusal=refusal)
    host.start()
    return host


# ── load time ────────────────────────────────────────────────────────────────────────────────


def test_in_process_still_arms_three_hooks(tmp_path, srv):
    """No false positive: the installed default arms the full hook set and loads clean."""
    host = _host(tmp_path, srv, "in_process")
    try:
        assert host.loaded is not None, "the loader must expose a record per plugin"
        assert host.loaded.error is None, host.loaded.error
        assert host.loaded.enabled is True
        assert set(host.loaded.hooks_registered) == set(HOOKS), list(host.loaded.hooks_registered)
        assert len(host.loaded.hooks_registered) == len(HOOKS), list(host.loaded.hooks_registered)
        assert host.in_process is True, host.host_module
        for hook in HOOKS:
            assert H.plugin_callback(hook) is not None, hook
    finally:
        host.close()


def test_host_isolation_refuses_to_arm(tmp_path, srv, caplog):
    """The opt-in mode: the load fails, no hook is registered, and the reason names the mode."""
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        host = _host(tmp_path, srv, "host", refusal=True)
    try:
        loaded = host.loaded
        assert loaded is not None, "the loader must expose a record per plugin"
        print("[P2c host failure]", loaded.error)  # raw evidence

        assert loaded.enabled is False, "a refused load must leave the plugin disabled"
        assert loaded.error, "the loader must record why the plugin did not load"
        assert "degraded:plugin-host" in loaded.error, loaded.error
        assert "PluginHostIsolationRefused" in loaded.error, loaded.error
        assert list(loaded.hooks_registered) == [], list(loaded.hooks_registered)

        # Nothing at all is reachable in this process: not even a callback to call.
        assert H.plugin_callback(H.HOOK) is None, host.host_module
        assert H.plugin_callback("transform_llm_output") is None
        assert H.plugin_callback("pre_tool_call") is None
        assert host.in_process is False

        # hermes' loader is the operator-facing surface: it warns in the *gateway* process.
        assert "Failed to load plugin 'proactive-conversation'" in caplog.text, caplog.text
        assert "degraded:plugin-host" in caplog.text, caplog.text
    finally:
        host.close()


def test_refusal_message_reaches_the_gateway_log(tmp_path, srv, caplog):
    """The child's own ERROR line is pumped into the parent log, tagged with the host's profile.

    ``hermes_cli/plugin_host.py::_pump_stderr`` reads the child's stderr into the parent's log as
    ``plugin host [<profile>]: <line>``. That is the copy an operator can act on (it carries the
    fix), so it is asserted here rather than assumed.
    """
    import time

    caplog.clear()
    with caplog.at_level(logging.INFO):
        host = _host(tmp_path, srv, "host", refusal=True)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and "plugin host [" not in caplog.text:
            time.sleep(0.1)
    try:
        print("[P2c child stderr]", [ln for ln in caplog.text.splitlines() if "plugin host [" in ln])
        assert "plugin host [" in caplog.text, caplog.text
        assert "degraded:plugin-host" in caplog.text, caplog.text
    finally:
        host.close()


# ── the refusal is grounded in what the host actually hands the hook ─────────────────────────


def test_refusal_is_grounded_in_the_measured_degradation(tmp_path, srv):
    """Run the probe (a *separate* plugin, still armed there) and assert the four measured facts.

    These are the reasons the refusal states, measured on the same real delivery path: the event is
    a mapping with no heartbeat stamp (as attribute or payload key), the gateway and store are
    placeholders, and the two calls the plugin makes on them fail. With that in hand, arming a gate
    cannot be anything but a guess.
    """
    log = Path(tmp_path) / "probe.jsonl"
    host = _host(tmp_path, srv, "host", probe_log=log, refusal=True)
    try:
        assert host.loaded.error, "the plugin under test must have refused"
        assert H.plugin_callback(H.HOOK) is None
        host.enable_session(H.P2_CONFIG)
        host.advance(3601)
        asyncio.run(host.poll())
    finally:
        records = read_records(log)
        host.close()

    assert len(records) == 1, f"the probe plugin must still see the wake: {records}"
    rec = records[0]
    print("[P2c host probe record]", rec)  # raw evidence

    # It really is another process, and it really is the plugin host.
    assert rec["pid"] != os.getpid(), rec["pid"]
    assert rec["host_process"] == "1"
    # 1. the heartbeat stamp is gone (neither an attribute nor a payload key)
    assert rec["heartbeat_session_id"] is None, rec
    assert rec["heartbeat_attr_present"] is False, rec
    assert rec["heartbeat_id_is_a_key"] is False, rec
    # 2. the event is a mapping that came off the wire, not a MessageEvent
    assert rec["event_type"] == "Record", rec["event_type"]
    assert rec["event_is_mapping"] is True, rec
    # 3./4. the live handles the policy needs are placeholders that answer nothing
    assert rec["gateway_type"] == "Opaque" and rec["store_type"] == "Opaque", rec
    assert [c["ok"] for c in rec["calls"]] == [False, False, False], rec["calls"]


# ── traffic: a refusal must not cost the user anything ───────────────────────────────────────


def test_user_traffic_is_unaffected_while_the_plugin_is_refused(tmp_path, srv):
    """Nothing armed means nothing can block a round — the guarantee, structurally."""
    host = _host(tmp_path, srv, "host", refusal=True)
    try:
        assert H.plugin_callback(H.HOOK) is None
        asyncio.run(host.send_user_message("周五吃饭的事"))
        asyncio.run(host.settle())
        print("[P2c host user traffic]", {"wire": list(host.wire), "calls": host.llm_calls()})

        assert list(host.wire) == ["user-reply"], list(host.wire)
        assert host.llm_calls().get("main") == 1, host.llm_calls()
        assert len(host.turns()) == 1, host.turns()
    finally:
        host.close()


def test_state_is_not_touched_while_the_plugin_is_refused(tmp_path, srv):
    """A refused load must not write anything either — no blob, no partial session state."""
    host = _host(tmp_path, srv, "host", refusal=True)
    try:
        host.enable_session(H.P2_CONFIG)
        blob_before = host.raw_blob()
        assert blob_before is not None, "fixture bug: enabling must write the blob"
        asyncio.run(host.send_user_message("你好"))
        asyncio.run(host.settle())
        assert host.raw_blob() == blob_before, "the plugin is not armed; it must not write"
    finally:
        host.close()


# ── negative control: the guard is load-bearing ─────────────────────────────────────────────

#: Name/dir of the control build: this package with the guard line removed.
NO_GUARD_NAME = "p2c-no-guard"
_GUARD_LINE = "    isolation.refuse_in_plugin_host(ctx)\n"


def _install_no_guard_variant() -> Path:
    """Install a copy of this package with the one guard line deleted — the negative control.

    Everything else is byte-identical: same loader, same home, same event, same mode. The copy is
    installed beside the plugin (the loader keys plugins by directory name, so it must be renamed in
    both places) and enabled only inside the control test.
    """
    target = support.ISO_HOME / "plugins" / NO_GUARD_NAME
    if not target.exists():
        shutil.copytree(support.PKG_ROOT, target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".pytest_cache"))
        init = target / "__init__.py"
        text = init.read_text(encoding="utf-8")
        assert _GUARD_LINE in text, "the guard line moved — update the negative control"
        init.write_text(text.replace(_GUARD_LINE, "    # NEGATIVE CONTROL: guard removed\n"),
                        encoding="utf-8")
        manifest = target / "plugin.yaml"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace(
            f"name: {support.PLUGIN_DIR_NAME}", f"name: {NO_GUARD_NAME}"), encoding="utf-8")
    return target


def test_negative_control_without_the_guard_the_failure_is_silent(tmp_path, srv):
    """Delete the guard and the same host mode looks healthy while providing nothing.

    Without the guard the loader reports the plugin *enabled* with every hook registered (so
    `hermes plugins doctor`/`show` and the gateway log look fine), while a wake for a session nobody
    enabled is released and delivered — the silent guarantee failure this refusal exists to prevent.
    With the guard in place (``test_host_isolation_refuses_to_arm``) the same mode cannot reach that
    state: the load fails, no hook exists, and the reason is recorded.
    """
    variant = _install_no_guard_variant()
    print("[P2c negative control]", variant)
    host = H.IsolatedHost(tmp_path, srv, isolation="host", extra_plugins=[NO_GUARD_NAME],
                          expect_refusal=True)
    host.start()
    try:
        # the guarded plugin under test refused ...
        assert host.loaded is not None and host.loaded.error, "the plugin under test must refuse"
        # ... while the control build loaded, is reported as enabled, and armed every hook.
        manager = host.manager
        assert manager is not None, "the loader must expose a manager"
        armed = [p for p in manager._plugins.values() if p.manifest.name == NO_GUARD_NAME]
        assert len(armed) == 1, [p.manifest.name for p in manager._plugins.values()]
        control = armed[0]
        assert control.error is None, control.error
        assert control.enabled is True
        assert set(control.hooks_registered) == set(HOOKS), list(control.hooks_registered)
        assert len(control.hooks_registered) == len(HOOKS), list(control.hooks_registered)

        assert host.raw_blob() is None, "fixture bug: this session must be untouched"
        asyncio.run(host.poll())
        print("[P2c negative control result]",
              {"wire": list(host.wire), "turns": len(host.turns()), "calls": host.llm_calls()})

        assert list(host.wire) == ["user-reply"], list(host.wire)
        assert len(host.turns()) == 1, host.turns()
        assert host.llm_calls().get("main") == 1, host.llm_calls()
    finally:
        host.close()
