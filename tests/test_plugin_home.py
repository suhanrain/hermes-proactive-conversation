"""The data root belongs to the plugin **instance**, and a wrong one is refused explicitly.

``composition.default_home()`` reads the process ``os.environ["HERMES_HOME"]``, which in a
multiplexing gateway — one process serving every profile, ``HERMES_HOME=~/.hermes`` — points at the
**global** ``state.db`` / ``memories`` and binds the assembly to profile ``default``. A session whose
scope says ``profile=product-manager`` then gets ``unavailable:session-scope-mismatch`` from both real
sources, and ``_silence_reason`` turns that into ``silent:source-down``: every wake stays silent while
every other gate is open.

What is real here: the plugin is the installed copy loaded by hermes' own loader, the composition value
is read through the plugin's own settings path (``ctx.get_config``), the hooks are invoked through
hermes' delivery manager, the session store is a real ``SessionStore`` with a real journal, and every
``state.db`` below is a real Hermes database written by hermes' own ``SessionDB`` writer.

**The process home is a different directory from the instance home in every test below**, which is the
failure shape these tests reproduce: ``support`` pins ``HERMES_HOME`` to the suite's throwaway home,
while the plugin instance these tests bind lives under
``<tmp>/profiles/<name>/plugins/proactive-conversation``. The assembly must follow the instance, and a
round whose scope names another profile must be refused with its own code.

Isolation: every home, store and database lives under ``tmp_path``; no live profile is read or
written, no platform is contacted, and no message is sent.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

import support  # noqa: F401  (importing it pins HERMES_HOME before any hermes module)
from proactive import composition, content
from proactive import contract as C

# ── the shared session coordinates ──────────────────────────────────────────────────────────

SESSION_KEY = "agent:product-manager:feishu:dm:oc_0000000000000000000000000000000"
FROZEN_SESSION_ID = "20260101_000000_deadbeef"
CHAT_ID = "oc_0000000000000000000000000000000"

GROUP_A = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "dnd_start": "23:00",
    "dnd_end": "09:00",
    "min_idle_seconds": 600,
    "max_idle_seconds": 1800,
    "window_max_attempts": 4,
    "window_min_visible": 1,
    "day_max_silent": 2,
    "day_max_attempts": 2,
}

#: Every window and count is written: the plugin has no built-in default for any of them
#: (时间次数不得写死), so a value that omitted one would measure the fail-closed branch instead.
COMPOSITION = {
    "recent_record_seconds": 172_800,
    "transcript_max_messages": 20,
    "transcript_window_seconds": 172_800,
    "memory_window_seconds": 172_800,
}

T0 = support.epoch("Asia/Shanghai", 2026, 10, 18, 9, 30)  # outside 23:00-09:00
TOPIC = "周五和老王吃饭"


# ── a plugin instance that lives in a *profile* home, and the data that home really holds ──


class Instance:
    """One plugin instance installed where a profile installs it: ``<home>/plugins/<id>``."""

    def __init__(self, root: Path, profile: str):
        self.profile = profile
        self.home = Path(root) / "profiles" / profile
        self.plugin_dir = self.home / "plugins" / "proactive-conversation"
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        (self.home / "memories").mkdir(exist_ok=True)
        self.settings = {"composition": dict(COMPOSITION), "session_declarations": []}
        self._ctx = None

    # -- the config surface the plugin really uses (ctx.get_config) -------------------------
    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    @property
    def ctx(self):
        if self._ctx is None:
            self._ctx = SimpleNamespace(
                manifest=SimpleNamespace(source="user", path=str(self.plugin_dir)),
                get_config=self.get_config)
        return self._ctx

    # -- real data written by hermes' own writer -------------------------------------------
    def seed(self, *, session_id, session_key, profile=None, at=T0 - 300, text=TOPIC):
        """One real session + one real user row in **this home's** ``state.db``."""
        from hermes_state import SessionDB

        db = SessionDB(self.home / "state.db")
        try:
            db.create_session(session_id, source="feishu", session_key=session_key,
                              profile_name=profile or self.profile)
            db.append_message(session_id=session_id, role="user", content=text,
                              timestamp=float(at), display_kind=None)
        finally:
            db.close()
        return session_id


def _instance(tmp_path, profile="pm") -> Instance:
    return Instance(tmp_path, profile)


def _store(tmp_path, name="p10_sessions"):
    """A real ``SessionStore`` over a throwaway directory (real journal, real metadata)."""
    return support.new_store(tmp_path, name)


def _source(profile: Optional[str] = "product-manager", chat_id=CHAT_ID):
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.FEISHU, chat_id=chat_id, user_id="ou_pm",
                         chat_type="dm", profile=profile)


def _seed(store, source, session_id=None):
    """Create the routing entry the gateway creates before the hook runs."""
    import gateway.session as session_mod

    original = session_mod._new_session_id
    if session_id:
        session_mod._new_session_id = lambda now: session_id  # the store's own creation seam
    try:
        entry = store.get_or_create_session(source)
    finally:
        session_mod._new_session_id = original
    if session_id:
        assert entry.session_id == session_id, "the store did not take the requested session id"
    return entry


class Session:
    """One real session driven through the real hooks with a controllable clock."""

    def __init__(self, rt, store, source, clock, session_id=None):
        self.rt, self.store, self.source, self.clock = rt, store, source, clock
        self.entry = _seed(store, source, session_id)
        self.dims = support.dims_for(store, self.entry, source)

    @property
    def key(self):
        return self.entry.session_key

    @property
    def session_id(self):
        return self.entry.session_id

    @property
    def blob(self):
        return self.store.get_session_metadata(self.key, C.META_KEY, None)

    def declaration(self, **over):
        entry = {"session_key": self.key, "session_id": self.session_id, **GROUP_A}
        entry.update(over)
        return entry

    def wake(self, advance=0.0):
        self.clock["now"] += float(advance)
        return support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_wake(self.key, self.session_id, source=self.source),
            gateway=None, session_store=self.store)


def _clock(rt, start=T0):
    clock = {"now": float(start)}
    rt.set_clock(lambda: clock["now"])
    rt.set_rng(support.FixedRng(0.0))
    return clock


def _write_composition_into_config(composition_block=None):
    """Write the composition block into this profile's real ``config.yaml`` (one operator edit)."""
    lines = ["plugins:", "  enabled: [proactive-conversation]"]
    if composition_block:
        lines += ["  entries:", "    proactive-conversation:", "      settings:", "        composition:"]
        for key, value in composition_block.items():
            lines.append(f"          {key}: {support._yaml_scalar(value)}")
    support.CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return support.CONFIG_PATH


@pytest.fixture(autouse=True)
def _restore_profile_config():
    """Every test here may edit the one shared throwaway ``config.yaml``: put it back afterwards."""
    original = (support.CONFIG_PATH.read_text(encoding="utf-8")
                if support.CONFIG_PATH.exists() else None)
    try:
        yield
    finally:
        if original is not None:
            support.CONFIG_PATH.write_text(original, encoding="utf-8")


def _bound(ctx):
    """Bind *ctx* to the host adapter for the duration of a ``with`` block, then reset it.

    ``composition.resolve_home`` / ``instance_home`` take no context argument — the adapter owns the
    only one there is — so a test that wants a specific manifest to be the evidence binds it, exactly
    as ``register(ctx)`` does in a real gateway.
    """
    import contextlib

    from proactive import host_adapter

    @contextlib.contextmanager
    def _scope():
        host_adapter.bind_context(ctx)
        try:
            yield
        finally:
            host_adapter.reset_context()

    return _scope()


def _released(session) -> bool:
    from proactive import state as state_mod

    blob = session.blob or {}
    st = state_mod.SessionState.from_dict(blob.get("state") or {})
    return bool(st and st.pending)


# ══ the data root comes from the plugin instance, not from the process environment ═════════


def test_the_plugin_instance_home_wins_over_the_process_environment(monkeypatch, tmp_path):
    """A user plugin under ``<root>/profiles/<name>/plugins/<id>`` belongs to ``<root>/profiles/<name>``.

    Hermes' own discovery rule (``plugins_discovery.py``: ``user_dir = get_hermes_home() / "plugins"``)
    is the evidence — not a path convention this plugin invented. The process environment names a
    *different* home (the multiplexing gateway's shape) and must not win.
    """
    root = tmp_path / "root"
    plugin_dir = root / "profiles" / "product-manager" / "plugins" / "proactive-conversation"
    plugin_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))  # the process view: the global home

    ctx = SimpleNamespace(manifest=SimpleNamespace(source="user", path=str(plugin_dir)))
    with _bound(ctx):
        home, source = composition.resolve_home()

    assert home == root / "profiles" / "product-manager", (
        f"resolved {home} (source={source}); the process HERMES_HOME must not decide the data root")
    assert source == composition.HOME_FROM_PLUGIN_INSTANCE
    assert composition.profile_for_home(home) == "product-manager"
    with _bound(SimpleNamespace(manifest=None)):
        assert composition.resolve_home() == (
            root, composition.HOME_FROM_PROCESS), "no plugin identity ⇒ the process fallback, unchanged"
    print(f"[home] instance home={home} profile=product-manager source={source}; "
          f"process HERMES_HOME={root}")


@pytest.mark.parametrize("source_name", ["bundled", "entrypoint"])
def test_a_manifest_that_is_not_home_rooted_never_becomes_a_data_home(
        monkeypatch, tmp_path, source_name):
    """``bundled``/``entrypoint`` manifests carry no home; their directory is the install tree."""
    root = tmp_path / "root"
    install_tree = tmp_path / "hermes-agent" / "plugins" / "proactive-conversation"
    install_tree.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    ctx = SimpleNamespace(manifest=SimpleNamespace(source=source_name, path=str(install_tree)))
    with _bound(ctx):
        home, source = composition.resolve_home()
    assert home == root and source == composition.HOME_FROM_PROCESS, (
        f"a {source_name} manifest resolved a data home: {home} ({source})")
    print(f"[home] {source_name} manifest → no instance home; fallback {home}")


def test_a_project_plugin_directory_is_never_a_data_home(monkeypatch, tmp_path):
    """``source=project`` is ``Path.cwd()/.hermes/plugins`` — a workspace, not a Hermes home.

    Hermes' project discovery (``plugins_discovery.py::collect_directory_manifests``, guarded by
    ``HERMES_ENABLE_PROJECT_PLUGINS``) scans ``Path.cwd() / ".hermes" / "plugins"``. That directory
    *looks* like ``<home>/plugins`` and is exactly the disguise this rule exists for: it holds no
    profile's ``state.db`` or ``memories``, so reading it as a data root would be reading whatever a
    project happens to keep in its workspace. The instance identity is therefore **unproven**, and
    the module answers with the process fallback, marked as such (the release path refuses it).
    """
    root = tmp_path / "root"
    monkeypatch.setenv("HERMES_HOME", str(root))
    plugin_dir = tmp_path / "workspace" / ".hermes" / "plugins" / "proactive-conversation"
    plugin_dir.mkdir(parents=True)

    ctx = SimpleNamespace(manifest=SimpleNamespace(source="project", path=str(plugin_dir),
                                                  name="proactive-conversation"))
    with _bound(ctx):
        assert composition.instance_home() is None, (
            f"a project workspace became a data home: {composition.instance_home()}")
        home, source = composition.resolve_home()
    assert (home, source) == (root, composition.HOME_FROM_PROCESS), (home, source)
    print(f"[home] project plugin dir → no instance identity; process fallback {home} ({source})")


@pytest.mark.parametrize("label,relative", [
    ("relative", "plugins/proactive-conversation"),
    ("traversal", "x/plugins/../../plugins/proactive-conversation"),
    ("no-plugins-segment", "install-tree/proactive-conversation"),
    ("leaf-is-not-this-plugin", "root/plugins/someone-else"),
    ("leaf-is-the-plugins-segment", "root/plugins"),
])
def test_a_malformed_plugin_directory_proves_nothing(monkeypatch, tmp_path, label, relative):
    """A path that is not exactly ``<absolute home>/plugins/<this plugin's name>`` proves nothing.

    Every case below *contains* a ``plugins`` segment (or is the shape of one), which is why a bare
    segment scan is not evidence: a relative path, a traversal, an install tree, a directory named
    after another plugin, and the ``plugins`` directory itself must all be refused as a "home".
    """
    root = tmp_path / "root"
    monkeypatch.setenv("HERMES_HOME", str(root))
    path = relative if label == "relative" else str(tmp_path / relative)
    if label == "leaf-is-the-plugins-segment":
        (tmp_path / "root" / "plugins").mkdir(parents=True)

    ctx = SimpleNamespace(manifest=SimpleNamespace(source="user", path=path,
                                                  name="proactive-conversation"))
    with _bound(ctx):
        assert composition.instance_home() is None, (
            f"{label}: {path!r} was accepted as a data home: {composition.instance_home()}")
        home, source = composition.resolve_home()
    assert (home, source) == (root, composition.HOME_FROM_PROCESS), (label, home, source)
    print(f"[home] {label} → not an identity; fallback {home}")


def test_a_missing_or_empty_manifest_path_proves_nothing(monkeypatch, tmp_path):
    """No path at all (and a path object that cannot be built) is "no evidence", never a guess."""
    root = tmp_path / "root"
    monkeypatch.setenv("HERMES_HOME", str(root))
    for manifest in (SimpleNamespace(source="user", path=None, name="proactive-conversation"),
                     SimpleNamespace(source="user", path="", name="proactive-conversation"),
                     SimpleNamespace(source="", path=None, name="proactive-conversation")):
        with _bound(SimpleNamespace(manifest=manifest)):
            assert composition.instance_home() is None, manifest
    print("[home] empty/absent manifest path → no identity")


def test_the_bare_interpreter_fallback_still_answers_without_any_hermes_environment(
        monkeypatch, tmp_path):
    """The fallback survives — it just never wins over a plugin identity (capability probe path)."""
    import sys

    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    with _bound(None):
        home, source = composition.resolve_home()
    assert home == Path.home() / ".hermes" and source == composition.HOME_FROM_PROCESS, home
    print(f"[home] no hermes environment at all → {home} ({source})")


# ══ the real-shape positive, and the control ═══════════════════════════════════════════════


def test_the_assembly_reads_the_instance_home_while_the_process_home_is_a_different_one(
        plugin, plugin_runtime, tmp_path):
    """The instance lives in ``profiles/pm``; the process home is the suite's own.

    The assembly must be built over the *instance* home and its transcript source must read rows out
    of that home — while ``support.ISO_HOME`` (what ``default_home()`` answers in this process) is a
    different directory that holds no row this scope could use.
    """
    rt = plugin_runtime
    _clock(rt)
    pm = _instance(tmp_path, "pm")
    session_id = "20261018_120000_deadbeef"
    pm.seed(session_id=session_id, session_key="agent:pm:feishu:dm:oc_pm")

    rt.bind_plugin_context(pm.ctx)  # what register(ctx) does with the real loader's context
    assert rt.refresh_content() is True, rt.content_state()
    state = rt.content_state()
    print(f"[home] state home={state['home']} profile={state['profile']} "
          f"source={state['home_source']} installs={state['installs']} "
          f"(process home {support.ISO_HOME})")
    assert state["home"] == str(pm.home), (
        f"the assembly was built over {state['home']}; the instance lives in {pm.home}")
    assert state["home"] != str(support.ISO_HOME), "the process home must not be the data root"
    assert state["profile"] == "pm", state
    assert state["home_source"] == composition.HOME_FROM_PLUGIN_INSTANCE, state
    assert state["wiring"]["db_path"] == str(pm.home / "state.db"), state["wiring"]
    assert state["wiring"]["memory_dir"] == str(pm.home / "memories"), state["wiring"]

    scope = content.Scope(session_key="agent:pm:feishu:dm:oc_pm", session_id=session_id,
                          profile="pm", platform="feishu", chat_id="oc_pm")
    assembly = rt.content_assembler().assemble(scope, T0)
    audit = {name: (status, reason, count) for name, status, reason, count in assembly.sources}
    print(f"[home] status={'silent' if assembly.silent else 'ready'} "
          f"reason={assembly.reason!r} audit={audit}")
    assert not assembly.silent, f"the assembly stayed silent: {assembly.reason}"
    assert audit[C.SOURCE_SESSION][0] == "ok", audit
    assert audit[C.SOURCE_SESSION][2] > 0, audit
    assert TOPIC in assembly.material, assembly.material


def test_the_same_profile_home_as_the_process_home_behaves_identically(
        plugin, plugin_runtime, tmp_path, monkeypatch):
    """The control: env == instance home must not be a different code path.

    The suite's own home is the plugin instance's home; pointing the process environment at it (as a
    standalone per-profile gateway does) must produce the same place, the same profile and a usable
    assembly — otherwise the fix would have traded one shape for the other.
    """
    rt = plugin_runtime
    _clock(rt)
    monkeypatch.setenv("HERMES_HOME", str(support.ISO_HOME))
    _write_composition_into_config(COMPOSITION)
    assert rt.refresh_content() is True, rt.content_state()
    state = rt.content_state()
    print(f"[home] control state home={state['home']} profile={state['profile']} "
          f"source={state['home_source']} installs={state['installs']}")
    assert os.path.normcase(state["home"]) == os.path.normcase(str(support.ISO_HOME)), state
    assert composition.default_home() == support.ISO_HOME
    assert state["home_source"] == composition.HOME_FROM_PLUGIN_INSTANCE, state
    assert state["profile"] == composition.profile_for_home(support.ISO_HOME)

    # ... and the assembly really reads that home: one real row, seeded through hermes' own writer.
    import p6a_support as S

    session_id = "20261018_130000_feedface"
    key = "agent:feishu:dm:oc_p10_control"
    S.seed_transcript(S.session_db(), session_id=session_id, session_key=key, profile=None,
                      rows=[("user", TOPIC, {"timestamp": T0 - 120})])
    assembly = rt.content_assembler().assemble(
        content.Scope(session_key=key, session_id=session_id, profile=None, platform="feishu",
                      chat_id="oc_p10_control"), T0)
    audit = {name: (status, reason, count) for name, status, reason, count in assembly.sources}
    print(f"[home] control assembly status={'silent' if assembly.silent else 'ready'} "
          f"reason={assembly.reason!r} audit={audit}")
    assert not assembly.silent and TOPIC in assembly.material, (assembly.reason, audit)


# ══ the mismatch is refused with its own code, never as a dead source ═════════════════════


def test_a_round_of_another_profile_is_refused_with_the_home_mismatch_code(
        plugin, plugin_runtime, tmp_path):
    """The frozen session (profile ``product-manager``) against an assembly bound to ``pm``.

    This verdict must be *distinguishable*: ``off:home-mismatch``, not ``silent:source-down``. The
    inequality is the assertion this test exists for; the release must not have been recorded either
    (no ticket, no counter).
    """
    rt = plugin_runtime
    clock = _clock(rt)
    pm = _instance(tmp_path, "pm")
    pm.seed(session_id=FROZEN_SESSION_ID, session_key="agent:pm:feishu:dm:oc_pm")
    rt.bind_plugin_context(pm.ctx)
    assert rt.refresh_content() is True, rt.content_state()

    store = _store(tmp_path)
    frozen = Session(rt, store, _source(profile="product-manager"), clock)
    pm.settings["session_declarations"] = [frozen.declaration()]

    assert frozen.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}], (
        "the first wake of a declared session only samples the window")
    assert rt.content_ready() is True, rt.content_state()

    verdict = frozen.wake(advance=1800)  # past the window: every other gate is open
    print(f"[home] frozen session verdict: {verdict!r}")
    assert verdict == [{"action": "skip", "reason": C.REASON_HOME_MISMATCH}], verdict
    assert verdict[0]["reason"] != C.SILENT_SOURCE_DOWN, (
        "a wrong data root must not be reported as a dead source — that is the home-resolution defect")
    assert C.REASON_HOME_MISMATCH.startswith("off:"), C.REASON_HOME_MISMATCH
    assert not _released(frozen), "a mismatched round must not record a release"
    assert not rt.tickets(), f"a mismatched round must not create a ticket: {rt.tickets()}"


# ══ the same content resolved without a plugin identity ═════════════════════════════════════


def test_the_old_process_home_path_is_what_produced_silent_source_down(plugin_runtime, tmp_path):
    """Build with no plugin identity and watch the process home produce ``silent:source-down``.

    ``composition.build(config)`` without ``ctx`` lets the process home decide. In this process that
    is the suite's home, bound to profile ``default``; the scope names ``product-manager``, so both
    real sources answer ``unavailable:session-scope-mismatch`` and the assembly is
    ``silent:source-down``. Resolved through the instance, the same config and the same scope are
    ``ready``.
    """
    rt = plugin_runtime
    _clock(rt)
    pm = _instance(tmp_path, "pm")
    pm.seed(session_id=FROZEN_SESSION_ID, session_key="agent:pm:feishu:dm:oc_pm")
    config = composition.from_settings(COMPOSITION)
    scope = content.Scope(session_key=SESSION_KEY, session_id=FROZEN_SESSION_ID,
                          profile="product-manager", platform="feishu", chat_id=CHAT_ID)

    old = composition.build(config, clock=lambda: T0)  # no ctx: no plugin identity
    assert old.home == support.ISO_HOME, old.home
    old_assembly = old.assembler.assemble(scope, T0)
    old_audit = {name: (status, reason, count)
                 for name, status, reason, count in old_assembly.sources}
    print(f"[home old path] home={old.home} profile={old.profile} "
          f"status={'silent' if old_assembly.silent else 'ready'} "
          f"reason={old_assembly.reason!r} audit={old_audit}")
    assert old_assembly.silent is True and old_assembly.reason == C.SILENT_SOURCE_DOWN, (
        f"the old path no longer reproduces the incident: {old_assembly.reason!r}")
    assert old_audit[C.SOURCE_SESSION][1].endswith("session-scope-mismatch"), old_audit
    assert old_audit[C.SOURCE_MEMORY][1].endswith("session-scope-mismatch"), old_audit

    with _bound(pm.ctx):  # the identity the gateway's own `register(ctx)` binds
        new = composition.build(config, clock=lambda: T0)
    assert new.home == pm.home and new.profile == "pm", (new.home, new.profile)
    new_assembly = new.assembler.assemble(
        content.Scope(session_key="agent:pm:feishu:dm:oc_pm", session_id=FROZEN_SESSION_ID,
                      profile="pm", platform="feishu", chat_id="oc_pm"), T0)
    new_audit = {name: (status, reason, count)
                 for name, status, reason, count in new_assembly.sources}
    print(f"[home new path] home={new.home} profile={new.profile} "
          f"status={'silent' if new_assembly.silent else 'ready'} "
          f"reason={new_assembly.reason!r} audit={new_audit}")
    assert not new_assembly.silent, new_assembly.reason
    assert new_audit[C.SOURCE_SESSION][0] == "ok" and new_audit[C.SOURCE_SESSION][2] > 0


# ══ the change key carries the place, and a fixture assembly claims no place ═══════════════


def test_a_different_plugin_home_rebuilds_the_assembly_even_with_the_same_value(
        plugin, plugin_runtime, tmp_path):
    """Two homes in one process: the second must not keep releasing from the first one's data root.

    The configured value does not change, so a value-only change key would answer "unchanged" and
    leave the old assembly installed for the rest of the process — the same defect through another
    door.
    """
    rt = plugin_runtime
    _clock(rt)
    alpha = _instance(tmp_path / "a", "alpha")
    beta = _instance(tmp_path / "b", "beta")

    rt.bind_plugin_context(alpha.ctx)
    assert rt.refresh_content() is True, rt.content_state()
    first = rt.content_state()
    assert (first["profile"], first["installs"], first["home"]) == (
        "alpha", 1, str(alpha.home)), first

    assert rt.refresh_content() is True
    assert rt.content_state()["installs"] == 1, "an unchanged value *and* place must not rebuild"

    rt.bind_plugin_context(beta.ctx)
    assert rt.refresh_content() is True, rt.content_state()
    second = rt.content_state()
    print(f"[home place key] alpha→beta: installs={second['installs']} home={second['home']} "
          f"profile={second['profile']}")
    assert second["installs"] == 2, f"a new plugin home did not rebuild the assembly: {second}"
    assert second["home"] == str(beta.home) and second["profile"] == "beta"
    assert rt.content_assembler() is not None


def test_an_assembly_installed_directly_claims_no_profile(plugin, plugin_runtime, tmp_path):
    """``set_content`` (a test fixture) names no home, so it can never be mismatched."""
    import p4_support as P

    rt = plugin_runtime
    rt.set_content(P.release_material(lambda: 0.0))
    assert rt.content_profile() is None
    assert rt.content_round_refusal({"profile": "product-manager"},
                                    "agent:product-manager:feishu:dm:oc_x") is None
    assert rt.content_state()["home"] is None
    print("[home] a directly installed assembly claims no place")


# ══ the refusal is decidable from the log, and the log carries no paths or text ═════════


def test_the_mismatch_refusal_names_both_profiles_and_no_paths(
        plugin, plugin_runtime, tmp_path, caplog):
    """The operator must be able to tell *which* side is wrong from the log line alone.

    The line carries the two profile names, the reason code and the digest; it must not carry the
    data-root path or any record text.
    """
    rt = plugin_runtime
    clock = _clock(rt)
    pm = _instance(tmp_path, "pm")
    pm.seed(session_id=FROZEN_SESSION_ID, session_key="agent:pm:feishu:dm:oc_pm")
    rt.bind_plugin_context(pm.ctx)
    assert rt.refresh_content() is True, rt.content_state()

    store = _store(tmp_path, "p10_log_sessions")
    frozen = Session(rt, store, _source(profile="product-manager"), clock)
    pm.settings["session_declarations"] = [frozen.declaration()]

    with caplog.at_level(logging.WARNING, logger="proactive.runtime"):
        assert frozen.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}]
        caplog.clear()
        verdict = frozen.wake(advance=1800)

    assert verdict == [{"action": "skip", "reason": C.REASON_HOME_MISMATCH}], verdict
    line = "\n".join(rec.getMessage() for rec in caplog.records
                     if C.REASON_HOME_MISMATCH in rec.getMessage())
    print(f"[home] refusal log: {line!r}")
    assert line, "the refusal left no log line at all"
    assert "product-manager" in line and "'pm'" in line, line
    assert str(pm.home) not in line, f"the log leaked a data-root path: {line}"
    assert TOPIC not in line, f"the log leaked record text: {line}"
    assert frozen.key not in line and frozen.session_id not in line, (
        f"the log leaked the session identity: {line}")


# ══ an unproven identity may not name a data root, and neither may an unproven round: ═══════
# ══ the fallback home is never releasable, whatever the process says ═══════════════════════
#
# ``ctx.manifest=None`` (or ``source=project``) leaves the process-home fallback in place, and
# because a profile-less round is *declared* and its composition is *usable*, that round must not be
# released from the process home — a different profile's data root. The rule: the only releasable
# root is the one the plugin instance provably belongs to, and the round's profile must be provably
# that root's profile. ``HOME_FROM_PROCESS`` therefore means "no identity", never "guess the process
# home".


def test_the_session_key_namespace_proof_is_the_gateway_s_own_inverse():
    """The plugin recovers a round's profile from the key the way core itself does — pinned here.

    ``gateway/session.py::_session_key_namespace`` encodes the routing profile into ``agent:<ns>:…``
    and ``profile_from_session_key_namespace`` inverts it. Core uses that inverse to route
    notifications, relay traffic and session recovery, so a session key is a gateway-issued fact and
    not a plugin convention. This copy must agree with it on every profile name — including the two
    awkward ones (``None``→``default`` and the marked ``main~``) — or the plugin's proof would name a
    different profile than the gateway's own answers do.
    """
    from gateway.session import _session_key_namespace, profile_from_session_key_namespace
    from proactive import runtime as runtime_mod

    seen = {}
    for name in (None, "default", "main", "pm", "product-manager"):
        namespace = _session_key_namespace(name)          # e.g. "agent:main", "agent:main~"
        slot = namespace.split(":", 1)[1]                 # the profile slot core's inverse reads
        key = f"{namespace}:feishu:dm:oc_x"               # the layout build_session_key produces
        expected = profile_from_session_key_namespace(slot)
        assert expected, f"core's own inverse named no profile for {name!r}"
        actual = runtime_mod.profile_from_session_key(key)
        seen[repr(name)] = (key, actual)
        assert actual == expected, (name, key, actual, expected)
    for key in ("", None, "not-a-key", "telegram:42", "feishu:dm:oc_x",
                "agent:feishu:dm:oc_x", "agent:pm:oc_x"):
        assert runtime_mod.profile_from_session_key(key) is None, key
    print(f"[home] session-key proof pinned to gateway.session: {seen}")


def test_an_unprovable_instance_identity_installs_nothing_and_refuses_the_wake(
        plugin, plugin_runtime, tmp_path, caplog):
    """``ctx.manifest=None`` with a usable config: no assembly, no ticket, no key write.

    The config surface still answers, so a composition *could* be built — over the process home,
    which is a different profile's data root. It must not be: nothing is
    installed, the refusal is ``off:home-mismatch`` (not ``content-unconfigured``, which would send
    the operator to the wrong file), and a declared wake gets no release, no ticket and no state key.
    """
    rt = plugin_runtime
    clock = _clock(rt)
    pm = _instance(tmp_path, "pm")
    pm.seed(session_id=FROZEN_SESSION_ID, session_key="agent:pm:feishu:dm:oc_pm")
    rt.bind_plugin_context(SimpleNamespace(manifest=None, get_config=pm.get_config))

    assert rt.refresh_content() is False, rt.content_state()
    state = rt.content_state()
    print(f"[home] identity-less refresh → ready={rt.content_ready()} state={state} "
          f"reason={rt.content_refusal_reason()!r}")
    assert rt.content_ready() is False, (
        f"an assembly was installed without an instance identity: {state}")
    assert rt.content_assembler() is None, state
    assert state["home"] is None and state["profile"] is None, state
    assert state["home_source"] == composition.HOME_FROM_PROCESS, state
    assert rt.content_refusal_reason() == C.REASON_HOME_MISMATCH, state
    assert C.SILENT_SOURCE_DOWN not in caplog.text, caplog.text

    store = _store(tmp_path, "p10_identity_less")
    frozen = Session(rt, store, _source(profile="product-manager"), clock)
    pm.settings["session_declarations"] = [frozen.declaration()]
    verdict = frozen.wake()
    print(f"[home] identity-less wake → {verdict!r}; blob={frozen.blob!r}")
    assert verdict == [{"action": "skip", "reason": C.REASON_HOME_MISMATCH}], verdict
    assert frozen.blob is None, "a refused wake wrote the session metadata key"
    assert not _released(frozen), "a refused wake opened a release"
    assert not rt.tickets(), rt.tickets()


def test_a_missing_composition_keeps_its_reason_code_even_without_an_identity(
        plugin, plugin_runtime, tmp_path):
    """Two different faults, two different codes: nothing configured is *not* a home mismatch."""
    rt = plugin_runtime
    pm = _instance(tmp_path, "pm")
    pm.settings = {"session_declarations": []}  # the operator never wrote a composition block
    rt.bind_plugin_context(SimpleNamespace(manifest=None, get_config=pm.get_config))

    assert rt.refresh_content() is False
    assert rt.content_refusal_reason() == C.REASON_CONTENT_UNCONFIGURED, rt.content_state()
    print(f"[home] absent composition → {rt.content_refusal_reason()!r}")


def test_the_unproven_identity_refusal_is_decidable_from_the_log_and_leaks_nothing(
        plugin, plugin_runtime, tmp_path, caplog):
    """Whoever reads the log must see *why* (and on which side), with no path, text or key."""
    rt = plugin_runtime
    clock = _clock(rt)
    pm = _instance(tmp_path, "pm")
    pm.seed(session_id=FROZEN_SESSION_ID, session_key="agent:pm:feishu:dm:oc_pm")

    with caplog.at_level(logging.WARNING, logger="proactive.runtime"):
        rt.bind_plugin_context(SimpleNamespace(manifest=None, get_config=pm.get_config))
        assert rt.refresh_content() is False
        store = _store(tmp_path, "p10_identity_log")
        frozen = Session(rt, store, _source(profile="product-manager"), clock)
        pm.settings["session_declarations"] = [frozen.declaration()]
        assert frozen.wake() == [{"action": "skip", "reason": C.REASON_HOME_MISMATCH}]

    lines = [rec.getMessage() for rec in caplog.records
             if C.REASON_HOME_MISMATCH in rec.getMessage()]
    print(f"[home] unproven-identity log lines: {lines}")
    assert lines, "the refusal left no log line at all"
    for line in lines:
        assert "<unknown>" in line, f"the log never names the unprovable side: {line}"
        assert "source=" in line, f"the log never names its evidence: {line}"
        for leak in (str(pm.home), str(support.ISO_HOME), TOPIC, frozen.key, frozen.session_id):
            assert leak not in line, f"the log leaked {leak!r}: {line}"


def test_a_round_with_no_profile_proof_is_refused_even_on_a_proven_instance(
        plugin, plugin_runtime, tmp_path, caplog):
    """The symmetric half of the rule: a *proven* home does not make an unproven round releasable.

    Here the instance side is fine — ``source=user`` at ``<home>/plugins/proactive-conversation``, bound to
    ``pm``, so an assembly really is installed — and the round has ``profile=None`` *plus* a session
    key in the pre-namespace layout (``feishu:dm:<chat>``), i.e. nothing in this round names a profile
    at all. "Not disprovable" is not "allowed" once a data root is at stake: the round is refused.

    This case exists because the guard can otherwise be weakened back to one-directional ("only a
    *named* mismatch refuses") with every other test still green — the named-mismatch and
    identity-less cases do not exercise it.
    """
    rt = plugin_runtime
    clock = _clock(rt)
    pm = _instance(tmp_path, "pm")
    pm.seed(session_id=FROZEN_SESSION_ID, session_key="agent:pm:feishu:dm:oc_pm")
    rt.bind_plugin_context(pm.ctx)
    assert rt.refresh_content() is True, rt.content_state()
    assert rt.content_profile() == "pm", rt.content_state()
    assert rt.content_assembler() is not None, rt.content_state()

    store = _store(tmp_path, "p10_unprovable_round")
    source = _source(profile=None)  # the scope does not say which profile this is
    key = "feishu:dm:oc_unproven"   # ... and neither does the key: no profile slot
    assert rt.profile_from_session_key(key) is None, key
    # A real store entry *under that key* (the store's own creation seam, as ``_seed`` does for the
    # generated key), so the round is a real one: the store knows its session id, the plugin reads it
    # back with ``_peek_session_id``, and only the profile proof is missing.
    generate = getattr(store, "_generate_session_key")
    store._generate_session_key = lambda _source, *_a, **_k: key
    try:
        entry = store.get_or_create_session(source)
    finally:
        store._generate_session_key = generate
    assert entry.session_key == key, entry.session_key
    pm.settings["session_declarations"] = [
        {"session_key": key, "session_id": entry.session_id, **GROUP_A}]

    def wake(advance=0.0):
        clock["now"] += float(advance)
        return support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_wake(key, entry.session_id, source=source),
            gateway=None, session_store=store)

    with caplog.at_level(logging.WARNING, logger="proactive.runtime"):
        assert wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}], (
            "the first wake of a declared session only samples the window")
        caplog.clear()
        verdict = wake(advance=1800)

    line = "\n".join(rec.getMessage() for rec in caplog.records
                     if C.REASON_HOME_MISMATCH in rec.getMessage())
    print(f"[home] unprovable round on a proven instance → {verdict!r}; log={line!r}")
    assert verdict == [{"action": "skip", "reason": C.REASON_HOME_MISMATCH}], verdict
    assert C.SILENT_SOURCE_DOWN not in caplog.text, (
        "an unprovable round must not be reported as a dead data source")
    assert "'pm'" in line and "<unknown>" in line, line
    assert not rt.tickets(), f"an unprovable round must not create a ticket: {rt.tickets()}"
