"""What the installed loader actually hands ``pre_gateway_dispatch``.

The question is one fact, in one configuration: after the real loader discovers and registers the
plugin, does the hook receive the **real** gateway runner and session store, or the placeholder
objects that ``hermes plugins validate`` warns about for host-isolated plugins?

Both configurations are measured with the same probe (see ``probe_plugin``), driven by the same real
poll, so the two records are directly comparable:

* ``plugins.isolation: in_process`` — the installed default;
* ``plugins.isolation: host``     — the operator opt-in for host isolation.

Each test asserts what was measured, so a future Hermes that changes this contract fails loudly here
instead of silently making the plugin's policy inert.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import harness as H
import support
from probe_plugin import read_records


@pytest.fixture
def srv():
    H.ensure_agent_importable()
    from tests.fakes.fake_llm_provider import FakeLLMServer

    server = FakeLLMServer(default_text="好的")
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _drive_one_wake(tmp_path: Path, srv, isolation: str) -> list:
    """Install + discover + enable + one real wake, in ``isolation`` mode; return probe records.

    Under ``plugins.isolation: host`` the plugin under test *refuses to arm* (its load fails with
    ``degraded:plugin-host``), so this helper expects that failure in host mode. The records still
    arrive because the probe is a separate plugin that stays armed — which is what makes the
    degradation measurable at all. In-process the plugin arms normally.
    """
    log = Path(tmp_path) / f"probe-{isolation}.jsonl"
    host = H.IsolatedHost(tmp_path, srv, isolation=isolation, probe_log=log,
                          expect_refusal=(isolation == "host"))
    host.start()
    try:
        assert host.in_process is (isolation == "in_process"), (
            f"isolation={isolation} but the live hook callback lives in {host.host_module!r}"
        )
        assert (H.plugin_callback(H.HOOK) is None) is (isolation == "host"), (
            f"isolation={isolation}: a refused load must leave no callback at all"
        )
        host.enable_session(H.P2_CONFIG)
        host.advance(3601)  # the 1h window sampled at enable time is now due
        asyncio.run(host.poll())
        return read_records(log)
    finally:
        host.close()


def test_in_process_hook_receives_real_objects(tmp_path, srv):
    """The installed default: the hook runs in this process and gets the live gateway + store."""
    records = _drive_one_wake(tmp_path, srv, "in_process")

    assert len(records) == 1, f"one real wake must invoke the hook exactly once: {records}"
    rec = records[0]
    print("[isolation in_process]", rec)  # raw evidence for the report

    assert rec["pid"] == os.getpid(), "in-process hook must run in this process"
    assert rec["host_process"] is None, "no plugin-host child may be involved"
    assert rec["mode_seen_by_callback"] == "in_process"
    assert rec["gateway_type"] == "GatewayRunner", rec["gateway_type"]
    assert rec["store_type"] == "SessionStore", rec["store_type"]
    assert rec["event_type"] == "MessageEvent"
    assert rec["event_is_mapping"] is False
    assert rec["heartbeat_session_id"] and rec["session_key"], rec
    for call in rec["calls"]:
        assert call["ok"] is True, f"a real object must answer a real call: {call}"
    assert "feishu_dm" not in rec["gateway_repr"]  # sanity: it is the runner, not the fixture


def test_host_mode_hook_receives_placeholders(tmp_path, srv):
    """The operator opt-in (``plugins.isolation: host``): the documented degradation, measured.

    This test pins the negative result rather than failing on it: the hook contract cannot be met
    for host isolation, and that must stay visible instead of being lost. The plugin under test refuses to arm
    in this mode, so the record below is the *probe* plugin's — the measurements are of the host
    boundary itself, which is exactly what the refusal is based on.
    """
    records = _drive_one_wake(tmp_path, srv, "host")

    assert len(records) == 1, f"one real wake must invoke the host-side hook once: {records}"
    rec = records[0]
    print("[isolation host]", rec)  # raw evidence for the report

    assert rec["pid"] != os.getpid(), "the host-isolated hook must run in another process"
    assert rec["host_process"] == "1"
    assert rec["event_type"] != "MessageEvent", rec["event_type"]
    assert rec["event_is_mapping"] is True, "the event crosses the wire as a mapping, not an object"
    assert rec["gateway_type"] != "GatewayRunner", rec["gateway_type"]
    assert rec["store_type"] != "SessionStore", rec["store_type"]
    assert rec["calls"][0]["ok"] is False, "a placeholder cannot answer peek_session_id"
    assert rec["heartbeat_session_id"] is None, "dynamic attributes do not survive the wire"


def test_host_mode_makes_the_gate_inert(tmp_path, srv):
    """Consequence of that negative result, measured on the real plugin in both modes.

    One scenario, two modes: a session nobody enabled, woken once. In-process the gate refuses the
    wake (``off:no-blob``: no turn, no model call). In host mode the same wake is delivered because
    the plugin **refuses to arm at all** there (its load fails with ``degraded:plugin-host``: the hook
    cannot tell a heartbeat wake from a user message and cannot read the session blob through a
    placeholder), so nothing can intercept the round.
    """
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text

    results = {}
    for mode in ("in_process", "host"):
        # A fresh server per mode: the refusing case never consumes its queued reply, so a shared
        # queue would leak that text into the next case.
        server = FakeLLMServer(default_text="好的")
        server.start()
        server.push(Text(f"{mode}-wake-delivered"))
        case_dir = tmp_path / mode
        case_dir.mkdir(parents=True, exist_ok=True)
        host = H.IsolatedHost(case_dir, server, isolation=mode, expect_refusal=(mode == "host"))
        try:
            host.start()
            try:
                assert host.loaded is not None
                results[mode] = {"refused": bool(host.loaded.error),
                                 "error": host.loaded.error,
                                 "hooks": list(host.loaded.hooks_registered),
                                 "callback": H.plugin_callback(H.HOOK) is not None,
                                 **asyncio.run(_wake_on_unconfigured_session(host))}
            finally:
                host.close()
        finally:
            server.stop()

    print("[host inertness]", results)
    assert results["in_process"]["refused"] is False, results["in_process"]
    assert results["in_process"]["callback"] is True, results["in_process"]
    assert results["in_process"]["wire"] == [], results["in_process"]
    assert results["in_process"]["turns"] == 0, results["in_process"]
    assert results["in_process"]["calls"]["post_total"] == 0, results["in_process"]
    # host mode: the plugin refused to arm, and therefore cannot intercept anything.
    assert results["host"]["refused"] is True, results["host"]
    assert "degraded:plugin-host" in (results["host"]["error"] or ""), results["host"]
    assert results["host"]["hooks"] == [], results["host"]
    assert results["host"]["callback"] is False, results["host"]
    assert results["host"]["wire"] == ["host-wake-delivered"], results["host"]
    assert results["host"]["turns"] == 1, results["host"]
    assert results["host"]["calls"]["main"] == 1, (
        f"the host-mode wake must run exactly one real model turn: {results['host']}"
    )


async def _wake_on_unconfigured_session(host) -> dict:
    """Drive one real poll for a session that was never enabled (no blob in the store)."""
    assert host.raw_blob() is None, "fixture bug: this session must be untouched"
    await host.poll()
    return {
        "wire": list(host.wire),
        "turns": len(host.turns()),
        "calls": host.llm_calls(),
        "paths": [str(r.get("path")) for r in host.requests()],
    }


def test_cli_validate_note_is_static_not_mode_dependent(tmp_path):
    """The ``hermes plugins validate`` hint, run for real — and pinned to what it means.

    The note comes from the *static* capability audit, so it prints for a standalone plugin
    regardless of the profile's ``plugins.isolation``; the tests above measure what actually
    happens in each mode. Recording both here is the point: the hint is advisory, not a runtime fact.

    This test builds its **own** throwaway home (``tmp_path/cli-home``) instead of reusing the shared
    ``support.ISO_HOME``: ``validate`` reads ``plugins.isolation`` from the home it is given, and
    writing ``host`` into the session-wide config would silently flip the mode for every other test
    in the process.
    """
    cli = H.agent_repo_root() / ".venv" / "bin" / "hermes"
    assert cli.exists(), f"the host CLI is not installed at {cli}"

    home = Path(tmp_path) / "cli-home"
    plugin_dir = home / "plugins" / support.PLUGIN_DIR_NAME
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(support.PKG_ROOT, plugin_dir,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".pytest_cache"))

    seen = {}
    for mode in ("in_process", "host"):
        (home / "config.yaml").write_text(
            f"plugins:\n  enabled: [{support.PLUGIN_DIR_NAME}]\n  isolation: {mode}\n",
            encoding="utf-8",
        )
        env = {
            "HERMES_HOME": str(home),
            "HERMES_BUNDLED_PLUGINS": str(home / "no-bundled-plugins"),
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
        }
        proc = subprocess.run([str(cli), "plugins", "validate", str(plugin_dir)],
                              capture_output=True, text=True, env=env, timeout=240)
        seen[mode] = proc.stdout + proc.stderr
        assert proc.returncode == 0, f"validate failed in {mode}:\n{seen[mode]}"
        assert "Validation passed." in seen[mode], seen[mode]

    print("[isolation validate in_process]", [ln for ln in seen["in_process"].splitlines()
                                        if "Isolation" in ln or "placeholder" in ln])
    print("[isolation validate host]", [ln for ln in seen["host"].splitlines()
                                  if "Isolation" in ln or "placeholder" in ln])
    for mode, text in seen.items():
        assert "runs in the plugin host" in text, (mode, text)
        assert "placeholder" in text, (mode, text)


def test_hermes_ran_from_the_throwaway_profile(tmp_path, srv):
    """Containment, measured: the live profile is never the home hermes resolved.

    ``tests/conftest.py`` pins ``HERMES_HOME`` before any hermes import; this test proves the pin
    held through a real plugin load — the discovered plugin's own module file is inside the copy
    under the throwaway home, not in this repo and not in any installed profile.
    """
    from hermes_constants import get_hermes_home

    assert Path(get_hermes_home()) == support.ISO_HOME, get_hermes_home()
    assert os.environ["HERMES_HOME"] == str(support.ISO_HOME)

    host = H.IsolatedHost(tmp_path, srv)
    host.start()
    try:
        installed = support.install_plugin()
        assert installed.parent == support.ISO_HOME / "plugins", installed
        cb = H.plugin_callback(H.HOOK)
        mod = sys.modules[cb.__module__]
        assert mod.__file__, f"the registered callback lives in a fileless module: {cb.__module__}"
        module_file = Path(mod.__file__).resolve()
        print("[isolation plugin module]", module_file)
        assert installed.resolve() in module_file.parents, module_file
        assert not str(module_file).startswith(str(support.PKG_ROOT.resolve())), module_file
        live_dirs = [Path.home() / ".hermes" / "plugins"] + sorted(
            (Path.home() / ".hermes" / "profiles").glob("*/plugins")
        )
        for live in live_dirs:
            assert live.resolve() not in module_file.parents, f"ran from the live profile: {live}"
    finally:
        host.close()
