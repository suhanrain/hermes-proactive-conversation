"""The config-file declaration as the session's authorisation and parameter source.

What is real in this file: the plugin is the installed copy loaded by hermes' own loader, the
settings are read through the very ``ctx.get_config`` the plugin was handed (the isolated profile's
real ``config.yaml``, parsed by hermes' own loader), the hooks are invoked through hermes' delivery
manager, and the per-session metadata is a real ``SessionStore`` with a real routing index/journal.

The session identity is shared across these tests: the key is produced by hermes' own
``build_session_key`` from a Feishu DM source in profile ``product-manager``, and the session id is
the value ``SessionStore`` is given at creation (``_new_session_id`` is the store's own creation
seam), so the declarations below address a session the gateway really created — nothing is
fabricated.

Nothing here touches a live profile: ``support`` pins ``HERMES_HOME`` to a throwaway directory before
any hermes module is imported, every store lives under ``tmp_path``, and no platform is contacted.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import p4_support as P
import pytest

import support
from proactive import contract as C
from proactive import declarations as D

# ── the shared configuration instances ─────────────────────────────────────────────────────

SESSION_KEY = "agent:product-manager:feishu:dm:oc_0000000000000000000000000000000"
SESSION_ID = "20260101_000000_deadbeef"
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
GROUP_B = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "dnd_start": "23:00",
    "dnd_end": "09:00",
    "min_idle_seconds": 3600,
    "max_idle_seconds": 7200,
    "window_max_attempts": 2,
    "window_min_visible": 1,
    "day_max_silent": 1,
    "day_max_attempts": 1,
}

T0 = support.epoch("Asia/Shanghai", 2026, 10, 16, 12, 0)  # noon local: outside 23:00-09:00

#: What a *released* wake returns once an assembly is installed: the released round keeps its
#: own event and only its text is filled in place, so the plugin hands the gateway this one
#: directive instead of an empty "no opinion" (``[]``). Every session harness here installs the
#: fixture assembly a configured profile's ``composition`` would produce.
RELEASED_WAKE = [{"action": "allow"}]


def declaration(session_key=SESSION_KEY, session_id=SESSION_ID, drop=None, **over):
    """One declaration entry: the frozen address plus the frozen (or overridden) parameters."""
    entry = {"session_key": session_key, "session_id": session_id, **GROUP_A}
    entry.update(over)
    for key in drop or ():
        entry.pop(key, None)
    return entry


# ── a real session, a real store, the frozen identity ──────────────────────────────────────


def _source(chat_id: str = CHAT_ID):
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.FEISHU, chat_id=chat_id, user_id="ou_pm",
                         chat_type="dm", profile="product-manager")


def _store(tmp_path, name: str = "p8_sessions"):
    """A real ``SessionStore`` with profile multiplexing on (the shape the frozen key encodes)."""
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    return SessionStore(sessions_dir=Path(tmp_path) / name,
                        config=GatewayConfig(multiplex_profiles=True))


def _seed(store, source, session_id=None):
    """Create the routing entry the gateway creates before the hook runs.

    ``session_id`` pins the store's own id generator, so a fixed session identity is reproduced
    exactly; without it the store keeps its natural id.
    """
    import gateway.session as session_mod

    original = session_mod._new_session_id
    if session_id:
        session_mod._new_session_id = lambda now: session_id  # the store's own creation seam
    try:
        entry = store.get_or_create_session(source)
    finally:
        session_mod._new_session_id = original
    if session_id:
        assert entry.session_id == session_id, (
            "the store did not take the requested session id, so the frozen identity is not reproduced")
    return entry


class DeclaredSession:
    """One real session driven through the real hooks, sharing a controllable clock."""

    def __init__(self, tmp_path, rt, *, chat_id=CHAT_ID, session_id=None, name="p8_sessions",
                 clock=None, start=T0):
        self.rt = rt
        self.store = _store(tmp_path, name)
        self.source = _source(chat_id)
        # One clock object may be shared by several sessions: the verdicts of a comparison must be
        # measured at the same moment, and the plugin's seam is process-wide.
        self.clock = clock if clock is not None else {"now": float(start)}
        rt.set_clock(lambda: self.clock["now"])
        rt.set_rng(support.FixedRng(0.0))
        # A released round needs an assembly; these tests measure the *authorisation* verdict of
        # a declared session, so every session here runs with the stand-in a configured profile's
        # ``composition`` would install. The unconfigured case is measured in its own tests.
        rt.set_content(P.release_material(lambda: self.clock["now"]))
        self.entry = _seed(self.store, self.source, session_id)
        self.dims = support.dims_for(self.store, self.entry, self.source)

    # -- observations ------------------------------------------------------------------------
    @property
    def key(self) -> str:
        return self.entry.session_key

    @property
    def session_id(self) -> str:
        return self.entry.session_id

    @property
    def now(self) -> float:
        return self.clock["now"]

    @property
    def blob(self):
        return self.store.get_session_metadata(self.key, C.META_KEY, None)

    @property
    def blob_absent(self) -> bool:
        return self.store.get_session_metadata(self.key, C.META_KEY, "MISSING") == "MISSING"

    @property
    def state(self):
        return (self.blob or {}).get("state", {})

    def blob_text(self) -> str:
        return json.dumps(self.blob, sort_keys=True, ensure_ascii=False)

    # -- driving -----------------------------------------------------------------------------
    def declare(self, *declarations):
        return support.write_plugin_settings({"session_declarations": list(declarations)})

    def wake(self, advance: float = 0.0):
        self.clock["now"] += float(advance)
        return support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_wake(self.key, self.session_id, source=self.source),
            gateway=None, session_store=self.store)

    def user_message(self, text: str = "在吗", advance: float = 0.0):
        self.clock["now"] += float(advance)
        return support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_event(text=text, session_key=self.key, source=self.source,
                                     timestamp=support.at_clock(self.clock["now"])),
            gateway=None, session_store=self.store)


@pytest.fixture
def rt(plugin_runtime):
    """The plugin's *own* runtime module: its seams, tickets and bound settings context."""
    return plugin_runtime


@pytest.fixture
def declared(tmp_path, rt):
    session = DeclaredSession(tmp_path, rt, session_id=SESSION_ID, name="frozen")
    yield session
    rt.reset_seams()


# ══ the declaration path is alive: real loader, real ctx.get_config, hot effect ═════════════


def test_the_declaration_round_trips_through_the_real_loader_and_get_config(
        plugin, rt, plugin_settings):
    """Write the key into the profile's real config.yaml → read back exactly what was written."""
    entry = declaration()
    path = plugin_settings({"session_declarations": [entry]})

    read_back = rt.session_declarations()
    assert read_back == [entry], f"the plugin read {read_back!r} from {path}"

    # The same file through the loader the context uses — i.e. what ``ctx.get_config`` walks.
    from hermes_cli.config import load_config_readonly

    raw = load_config_readonly()
    settings = raw["plugins"]["entries"]["proactive-conversation"]["settings"]
    assert settings["session_declarations"] == [entry]
    assert rt._settings.reader_bound() is True, "register(ctx) must bind the settings context"
    print("[config] key path: plugins.entries.proactive-conversation.settings.session_declarations")
    print(f"[config] read back through ctx.get_config: {read_back!r}")


def test_the_settings_panel_write_path_accepts_the_nested_declaration_list(
        plugin, rt, plugin_settings, installed_plugin_dir):
    """The validated writer (Desktop/TUI plugin settings) accepts the key and the nested list.

    That writer refuses an undeclared key or a value of the wrong type, so this is also the evidence
    that ``plugin.yaml``'s ``config_schema`` entry (``type: list``) is what makes the declaration
    editable from the settings surface rather than only by hand-editing the file.
    """
    from hermes_cli.plugins_settings import save_plugin_settings

    entry = declaration(min_idle_seconds=900)
    plugin_id = plugin.manifest.key or plugin.manifest.name
    written = save_plugin_settings(plugin_id, installed_plugin_dir,
                                   {"session_declarations": [entry]})
    assert written == ["session_declarations"], written
    read_back = rt.session_declarations()
    assert read_back == [entry], f"the settings writer produced {read_back!r}"

    # ... and the writer really does police the shape: a scalar for this key is refused.
    with pytest.raises(ValueError):
        save_plugin_settings(plugin_id, installed_plugin_dir, {"session_declarations": "nope"})
    print(f"[config] settings-panel writer accepted and read back: {read_back!r}")


def test_ac1b_an_edited_value_reaches_the_next_hook_call(declared, plugin_settings):
    """No reload, no restart: only ``config.yaml`` changes between two hook calls."""
    declared.declare(declaration())
    assert declared.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}]
    assert declared.blob["config"]["day_max_attempts"] == 2

    declared.declare(declaration(day_max_attempts=5))
    assert declared.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}]
    assert declared.blob["config"]["day_max_attempts"] == 5, (
        "the next hook call must read the edited declaration without a plugin reload")
    print(f"[config] effective config after the edit: {declared.blob['config']!r}")


# ══ the mirror: the metadata key is what the declaration says, and nothing else ════════════


def test_a_declared_session_is_mirrored_into_the_metadata_key(declared, plugin_settings):
    declared.declare(declaration())
    assert declared.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}]

    blob = declared.blob
    from proactive.state import SessionState

    assert isinstance(blob, dict), f"no blob was written: {blob!r}"
    assert blob["version"] == C.BLOB_VERSION
    for key, value in GROUP_A.items():
        assert blob["config"][key] == value, f"config[{key}] is {blob['config'].get(key)!r}"
    state = SessionState.from_dict(blob["state"])
    assert state is not None, f"the mirrored state is not readable: {blob['state']!r}"
    assert state.status == C.STATUS_ACTIVE
    assert blob["dims"] == declared.dims, "the six dimensions must be this session's own"
    assert declared.dims["session_key"] == SESSION_KEY
    assert declared.dims["session_id"] == SESSION_ID
    print(f"[mirror] mirrored config: {blob['config']!r}")
    print(f"[mirror] dims: {blob['dims']!r}")


def test_an_undeclared_session_gets_no_metadata_write(tmp_path, rt, declared, plugin_settings):
    """A second, real session that the declaration does not name: 0 writes, wake still refused."""
    other = DeclaredSession(tmp_path, rt, chat_id="oc_undeclared_p8", name="other")
    declared.declare(declaration())  # declares the frozen session only
    assert other.wake() == [{"action": "skip", "reason": C.REASON_NO_BLOB}]
    assert other.blob_absent, f"an undeclared session must not be written: {other.blob!r}"
    print(f"[mirror] undeclared session {other.key}: blob absent, reason {C.REASON_NO_BLOB}")


# ══ a declaration for another session id never inherits and never rewrites ═════════════════


def test_a_session_id_mismatch_is_off_and_leaves_the_existing_key_untouched(
        declared, plugin_settings, caplog):
    declared.declare(declaration())
    assert declared.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}]
    before = declared.blob_text()

    declared.declare(declaration(session_id="20261016_111111_deadbeef"))
    with caplog.at_level(logging.WARNING):
        result = declared.wake(advance=601)
    assert result == [{"action": "skip", "reason": C.REASON_DIMS}], result
    assert declared.blob_text() == before, "a declaration that refuses must not rewrite the key"
    assert C.REASON_DIMS in caplog.text and "session_id" in caplog.text
    print(f"[verdict] verdict: {result!r}; blob unchanged ({len(before)} chars of JSON)")


# ══ incomplete / illegal declarations fail closed, one case at a time ══════════════════════

REFUSALS = [
    ("missing window_max_attempts", dict(drop=("window_max_attempts",)), "window_max_attempts"),
    ("min_idle_seconds 0", dict(min_idle_seconds=0), "min_idle_seconds"),
    ("max_idle < min_idle", dict(min_idle_seconds=3600, max_idle_seconds=1800), "max_idle_seconds"),
    ("dnd_start == dnd_end", dict(dnd_end="23:00"), "dnd_end"),
    ("unknown timezone", dict(timezone="Nowhere/Nothing"), "timezone"),
    ("enabled as a string", dict(enabled="true"), "enabled"),
]


@pytest.mark.parametrize("label,over,field", REFUSALS, ids=[case[0] for case in REFUSALS])
def test_an_unusable_declaration_is_off_and_writes_nothing(
        declared, plugin_settings, caplog, label, over, field):
    entry = declaration(**over)

    declared.declare(entry)
    with caplog.at_level(logging.WARNING):
        result = declared.wake()
    assert result == [{"action": "skip", "reason": C.REASON_INVALID}], (label, result)
    assert declared.blob_absent, f"{label}: an unusable declaration must not write a key"

    lines = [r.getMessage() for r in caplog.records if "declaration" in r.getMessage()]
    assert lines, f"{label}: the refusal was not logged"
    assert C.REASON_INVALID in caplog.text, f"{label}: no reason code in {lines}"
    assert field in lines[-1], f"{label}: the log line does not name {field!r}: {lines[-1]}"

    # the same verdict field-by-field, straight from the parser
    parsed = D.lookup([entry], SESSION_KEY, SESSION_ID)
    assert parsed.refused and parsed.reason == C.REASON_INVALID and parsed.field == field, (
        label, parsed)
    print(f"[lane] {label}: {result!r} field={parsed.field} log={lines[-1]!r}")


# ══ a permanent close outranks the declaration (both lanes, and only a new session reopens) ══


def _revoked(session):
    """Permanently close this session through the frozen writer, and prove what it left behind."""
    assert session.rt.revoke_session(session.store, session.key) is True
    assert session.blob is None and not session.blob_absent, (
        "the permanent close must leave JSON null in place, not remove the key")


def test_a_permanent_close_is_not_reopened_by_the_same_declaration(
        declared, plugin_settings, caplog):
    """The declaration is a source of authorisation, never a way to undo a permanent close."""
    declared.declare(declaration())
    assert declared.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}]
    assert declared.blob["config"] == GROUP_A
    _revoked(declared)

    with caplog.at_level(logging.WARNING):
        verdicts = [declared.wake(advance=3600) for _ in range(3)]

    assert verdicts == [[{"action": "skip", "reason": C.REASON_PERM_CLOSED}]] * 3, verdicts
    assert declared.blob is None and not declared.blob_absent, (
        "a closed session's metadata key must stay null: nothing may be re-created")
    assert caplog.text.count(C.REASON_PERM_CLOSED) >= 3, caplog.text
    print(f"[R1] wakes after revoke, same declaration: {verdicts!r}; key still null")


def test_a_user_message_after_a_permanent_close_re_creates_nothing(declared, plugin_settings):
    """The user-message lane mirrors too, so it must not re-open a closed session either."""
    declared.declare(declaration())
    assert declared.user_message("授权后的一句") == []
    assert declared.blob["config"] == GROUP_A
    _revoked(declared)

    assert declared.user_message("关闭后的一句") == [], "user traffic must still never be blocked"
    assert declared.blob is None and not declared.blob_absent, (
        "the user-message lane re-created the metadata key of a permanently closed session")
    assert declared.wake(advance=3600) == [{"action": "skip", "reason": C.REASON_PERM_CLOSED}]
    print("[R2] user message after revoke: admitted, key untouched (null), next wake perm:closed")


def test_only_a_new_session_can_be_authorised_after_a_close(declared, plugin_settings):
    """Re-enabling is a judgeable NEW authorisation: a new session id, declared afresh.

    The permanent-close marker lives on the gateway's routing entry for that session, and a reset
    replaces that entry — so the new ``session_id`` starts from "never written" and can be authorised
    by a declaration naming it, while the old id is never the live session again.
    """
    declared.declare(declaration())
    assert declared.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}]
    _revoked(declared)

    entry = declared.store.get_or_create_session(declared.source, force_new=True)
    assert entry.session_id != SESSION_ID
    assert declared.store.get_session_metadata(declared.key, C.META_KEY, "MISSING") == "MISSING", (
        "a reset must not carry the closed marker into the new session")
    declared.entry = entry
    declared.dims = support.dims_for(declared.store, entry, declared.source)

    # The old declaration (same session_id) is now simply another session: OFF, no write, no effect.
    assert declared.wake() == [{"action": "skip", "reason": C.REASON_DIMS}]
    assert declared.blob_absent

    # A declaration for the NEW id authorises the new session: the mirror lands with the new dims.
    declared.declare(declaration(session_id=entry.session_id))
    assert declared.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}]
    assert declared.blob["dims"]["session_id"] == entry.session_id
    assert declared.blob["config"] == GROUP_A
    print(f"[R1] after reset: new session {entry.session_id} authorised, old {SESSION_ID} is OFF")


# ══ a store fault and a close racing the mirror must both leave the close standing ═════════


def _flaky_metadata_reads(session, monkeypatch, *, failures=1, skip=0):
    """Make the next ``failures`` metadata reads of THIS session's key raise, then read normally.

    A store whose read raises once is the transient fault that must stay distinguishable from "the
    key is not there". ``skip`` lets a test fail a *later* read than the first one: the key-state
    probe succeeds and the read the mirror needs is the one that fails.
    """
    real = session.store.get_session_metadata
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        index = calls["n"]
        if skip <= index < skip + failures:
            calls["n"] += 1
            raise OSError("transient session-metadata read failure")
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(session.store, "get_session_metadata", flaky)
    return calls


def test_a_transient_read_failure_cannot_resurrect_a_closed_session(
        declared, plugin_settings, monkeypatch, caplog):
    """'Could not read the key' is not 'the key is not there': a close must survive a read fault."""
    declared.declare(declaration())
    assert declared.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}]
    _revoked(declared)

    calls = _flaky_metadata_reads(declared, monkeypatch, failures=1)
    with caplog.at_level(logging.WARNING):
        verdict = declared.wake(advance=3600)
    assert calls["n"] == 1, "the probe did not inject the read failure"
    assert verdict == [{"action": "skip", "reason": C.REASON_UNVERIFIED}], verdict
    assert C.REASON_UNVERIFIED in caplog.text, caplog.text
    assert declared.blob is None and not declared.blob_absent, (
        "a read fault let the mirror overwrite the permanent-close marker")

    monkeypatch.undo()  # the store reads normally again
    verdicts = [declared.wake(advance=3600) for _ in range(3)]
    assert verdicts == [[{"action": "skip", "reason": C.REASON_PERM_CLOSED}]] * 3, verdicts
    assert declared.blob is None and not declared.blob_absent
    print(f"[R5] read fault → {verdict!r}; then 3 wakes → {C.REASON_PERM_CLOSED} ×3; key still null")


def test_a_user_message_under_a_read_fault_is_admitted_and_writes_nothing(
        declared, plugin_settings, monkeypatch):
    """On the other lane: no read → no mirror, but the user's message is never blocked."""
    declared.declare(declaration())
    assert declared.user_message("授权后的一句") == []
    assert declared.blob["config"] == GROUP_A
    _revoked(declared)

    calls = _flaky_metadata_reads(declared, monkeypatch, failures=1)
    assert declared.user_message("读故障期间的一句") == [], "user traffic must never be blocked"
    assert calls["n"] == 1
    assert declared.blob is None and not declared.blob_absent, (
        "a read fault let the user-message lane re-create a closed session's key")
    print("[R5] user message under a read fault: admitted, key untouched (null)")


def test_an_unreadable_key_is_never_mirrored_and_recovers_on_the_next_hook(
        declared, plugin_settings, monkeypatch):
    """Fail closed: with no readable key state the plugin cannot tell "never written" from "closed"."""
    declared.declare(declaration())
    calls = _flaky_metadata_reads(declared, monkeypatch, failures=1)
    assert declared.wake() == [{"action": "skip", "reason": C.REASON_UNVERIFIED}]
    assert calls["n"] == 1
    assert declared.blob_absent, "an unverifiable key must not be written"

    # ... and the fault is transient, not a dead end: the next hook mirrors normally.
    monkeypatch.undo()
    assert declared.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}]
    assert declared.blob["config"] == GROUP_A
    print(f"[R5] unreadable key → off, no write; next hook mirrored {declared.blob['config']!r}")


def test_a_close_landing_between_the_gate_and_the_mirror_still_wins(
        declared, plugin_settings, monkeypatch, caplog):
    """The mirror re-probes the key inside the close's own lock, so a stale gate cannot reopen.

    The interleaving: the gate's probe answers *before* the operator's close, the close then lands
    (a legitimate in-process control action), and the mirror is the next thing to run. The write must
    refuse — after which the blob is still ``null`` and the next wake is OFF.

    The injection point is the gate's one probe (``store.read_probed``): the gate takes the blob and
    the key state from the **same** ``get_session_metadata`` call, so "the key was not closed" is
    answered there, not by a separate ``key_state`` read.
    """
    declared.declare(declaration())
    assert declared.blob_absent

    store_mod = declared.rt.store
    real_read_probed = store_mod.read_probed
    raced = {"done": False}

    def racy(store_handle, key, dims):
        answer = real_read_probed(store_handle, key, dims)  # honest verdict, taken before the close
        if not raced["done"]:
            raced["done"] = True
            assert declared.rt.revoke_session(store_handle, key) is True, "the close did not land"
        return answer  # the stale answer the gate is about to act on

    monkeypatch.setattr(store_mod, "read_probed", racy)
    with caplog.at_level(logging.WARNING):
        verdict = declared.wake()
    assert raced["done"] is True, "the interleaving was never driven"
    assert verdict == [{"action": "skip", "reason": C.REASON_PERM_CLOSED}], verdict
    assert C.REASON_PERM_CLOSED in caplog.text, caplog.text
    assert declared.blob is None and not declared.blob_absent, (
        f"the mirror overwrote the close that landed before it: {declared.blob!r}")

    assert declared.wake(advance=3600) == [{"action": "skip", "reason": C.REASON_PERM_CLOSED}]
    print("[R6] close injected between gate and mirror → skip perm:closed, key still null")


# ══ two shared configurations must produce two different verdicts ══════════════════════════


# ══ a user message is never blocked, declared or not ═══════════════════════════════════════


def test_a_user_message_is_admitted_with_and_without_a_declaration(
        declared, plugin_settings):
    assert declared.user_message("没授权的第一句") == [], "user traffic must never be blocked"
    assert declared.blob_absent, "an OFF session is left completely untouched"

    declared.declare(declaration())
    assert declared.user_message("授权之后的一句") == [], "user traffic must never be blocked"
    assert declared.blob["config"]["day_max_attempts"] == 2, "the declared session is mirrored"
    assert declared.state["last_user_at"] == pytest.approx(declared.now, abs=1)
    print("[privacy] user-message verdicts: [] (undeclared) and [] (declared); state last_user_at="
          f"{declared.state['last_user_at']:.0f}")


# ══ the declaration list itself: shape failures are global, absence is silence ═════════════


SHAPE_FAILURES = [
    ("list missing", 'session_declarations: {"not": "a list"}'),
    ("string", 'session_declarations: "session_declarations"'),
]


@pytest.mark.parametrize("label,line", SHAPE_FAILURES, ids=[case[0] for case in SHAPE_FAILURES])
def test_an_unreadable_declaration_value_is_off_for_every_session(
        declared, plugin_settings, label, line):
    support.CONFIG_PATH.write_text(
        "plugins:\n  enabled: [proactive-conversation]\n  entries:\n    proactive-conversation:\n"
        f"      settings:\n        {line}\n", encoding="utf-8")
    result = declared.wake()
    assert result == [{"action": "skip", "reason": C.REASON_INVALID}], (label, result)
    assert declared.blob_absent, f"{label}: nothing may be written for an unreadable declaration"
    print(f"[shape:{label}] {result!r}, blob absent")


def test_an_entry_without_an_address_is_off_for_every_session(declared, plugin_settings):
    """A half-written entry must not silently leave the well-formed ones authorised."""
    entry = {key: value for key, value in declaration().items() if key != "session_key"}
    declared.declare(entry)
    result = declared.wake()
    assert result == [{"action": "skip", "reason": C.REASON_INVALID}], result
    assert declared.blob_absent
    print(f"[shape:no-address] {result!r}, blob absent")


# ══ the origin marker: a declaration owns what it mirrored ═════════════════════════════════

ORIGIN = C.ORIGIN_DECLARATION


def _mirrored(session, *entries):
    """Declare, drive one wake so the declaration mirrors, and prove the marker it left behind."""
    session.declare(*(entries or (declaration(),)))
    assert session.wake() == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}]
    assert session.blob["origin"] == ORIGIN, session.blob
    return session.blob_text()


def test_a_declaration_mirror_marks_the_authorisation_it_owns(declared, plugin_settings):
    """The marker is written by the mirror and survives every path that rewrites the blob."""
    _mirrored(declared)

    # an ordinary user message (the other mirroring lane)
    assert declared.user_message("在吗") == [], "user traffic must never be blocked"
    assert declared.blob["origin"] == ORIGIN, "the user-message write dropped the origin marker"

    # a policy skip (a wake inside the window rewrites the state)
    assert declared.wake(advance=1) == [{"action": "skip", "reason": C.REASON_WINDOW_OPEN}]
    assert declared.blob["origin"] == ORIGIN, "the skip write dropped the origin marker"

    # pause / resume (the session-control writers)
    assert declared.rt.pause_session(declared.store, declared.key, declared.dims) is True
    assert declared.blob["origin"] == ORIGIN, "the pause write dropped the origin marker"
    assert declared.state["status"] == C.STATUS_PAUSED
    assert declared.rt.resume_session(declared.store, declared.key, declared.dims) is True
    assert declared.blob["origin"] == ORIGIN, "the resume write dropped the origin marker"
    assert declared.state["status"] == C.STATUS_ACTIVE
    print(f"[R7] origin={declared.blob['origin']!r} after user message / skip / pause / resume")


def test_a_blocked_declaration_leaves_an_undeclared_off_session_off(declared, plugin_settings):
    """``enabled: false`` still closes; it never hands the session back to a stale mirror."""
    _mirrored(declared)
    declared.declare(declaration(enabled=False))
    verdict = declared.wake(advance=601)
    assert verdict == [{"action": "skip", "reason": C.REASON_DISABLED}], verdict
    assert declared.blob["config"]["enabled"] is True, "a refusing declaration must not write"
    print(f"[R7] enabled:false → {verdict!r}; mirrored config untouched")


# ══ an unreadable declaration source is not "no declaration" (fail closed) ═════════════════


# ══ a fault while the mirror reads must not overwrite a live state ═════════════════════════


def test_a_read_fault_while_mirroring_keeps_the_paused_state(
        declared, plugin_settings, monkeypatch, caplog):
    """The counterexample: probe #1 succeeds, the mirror's read fails, and the pause must survive."""
    _mirrored(declared)
    assert declared.rt.pause_session(declared.store, declared.key, declared.dims) is True
    assert declared.state["status"] == C.STATUS_PAUSED
    before = declared.blob_text()

    declared.declare(declaration(day_max_attempts=5))  # the declaration is edited at the same time
    calls = _flaky_metadata_reads(declared, monkeypatch, failures=1, skip=1)
    with caplog.at_level(logging.WARNING):
        verdict = declared.wake(advance=601)
    assert calls["n"] >= 2, "the mirror's metadata read was never reached"
    assert verdict == [{"action": "skip", "reason": C.REASON_UNVERIFIED}], verdict
    assert declared.blob_text() == before, "a read fault overwrote the paused blob"
    assert declared.state["status"] == C.STATUS_PAUSED
    assert declared.rt._tickets == {}, "a round was released while the blob could not be read"
    assert C.REASON_UNVERIFIED in caplog.text, caplog.text

    monkeypatch.undo()  # the store reads normally again
    verdicts = [declared.wake(advance=601) for _ in range(2)]
    assert verdicts == [[{"action": "skip", "reason": C.REASON_PAUSED}]] * 2, verdicts
    assert declared.state["status"] == C.STATUS_PAUSED, "reads recovering silently resumed a paused session"
    assert declared.blob["config"]["day_max_attempts"] == 5, (
        "once readable again, the edited declaration is mirrored — with the pause intact")
    print(f"[R9] mirror read fault → {C.REASON_UNVERIFIED}, pause kept; then {verdicts!r}")


# ══ a release whose write did not land is not released ═════════════════════════════════════


# ══ no log line may carry a session identifier ═════════════════════════════════════════════


def test_no_refusal_or_store_fault_logs_a_plaintext_identifier(
        declared, plugin_settings, monkeypatch, caplog):
    """Every refusal path logs a reason code and a digest — never the identifier."""
    with caplog.at_level(logging.DEBUG):
        _mirrored(declared)                                   # mirror / write
        declared.declare(declaration(session_id="20261016_111111_deadbeef"))
        declared.wake(advance=1)                              # off:dims-mismatch
        declared.declare(declaration())
        _flaky_metadata_reads(declared, monkeypatch, failures=1, skip=1)
        declared.wake(advance=601)                            # store read fault
        monkeypatch.undo()
        declared.declare()
        declared.wake(advance=601)                            # off:undeclared
        declared.rt.revoke_session(declared.store, declared.key)
        declared.declare(declaration())
        declared.wake(advance=601)                            # perm:closed (write refused)
        declared.user_message("最后一句")                      # the user lane

    text = caplog.text
    for identifier in (SESSION_KEY, SESSION_ID, CHAT_ID, "oc_0000000000000000000000000000000"):
        assert identifier not in text, (
            f"a log line leaked the session identifier {identifier!r}:\n{text}")
    assert C.REASON_PERM_CLOSED in text and C.REASON_DIMS in text, text
    print(f"[R11] {len(caplog.records)} log records, 0 plaintext session identifiers")


def test_store_refusals_log_a_digest_not_the_key(caplog):
    """``_probe``/``_set``/``write_ordered`` are the lowest layer: they must not leak either."""
    from proactive import store as store_mod

    marker = "oc_SECRET_MARKER_9f3a7c"
    dims = store_mod.dims_for(session_key=marker, session_id="sid_SECRET_1a2b")

    class BrokenStore:
        def get_session_metadata(self, key, meta_key, default=None):
            raise OSError("read fault")

        def set_session_metadata(self, key, meta_key, value):
            raise OSError("write fault")

    broken = BrokenStore()
    with caplog.at_level(logging.DEBUG):
        assert store_mod.key_state(broken, marker) == store_mod.KEY_UNREADABLE
        result = store_mod.write_ordered(
            broken, marker, config_raw=dict(GROUP_A),
            state=_new_state(), dims=dims)
        assert result.blocked == store_mod.WRITE_BLOCKED_UNREADABLE
        assert store_mod.write(broken, marker, config_raw=dict(GROUP_A), state=_new_state(),
                               dims=dims) is False

    text = caplog.text
    assert marker not in text and "sid_SECRET_1a2b" not in text, text
    assert "could not be read" in text, text
    print(f"[R11] store-level refusals logged without the key: {len(caplog.records)} records")


def test_a_store_fault_never_logs_the_untrusted_exception_body(caplog):
    """A store's error *text* is third-party data and may quote the key.

    The store is handed the session key, so any backend that puts its arguments in an exception
    message would smuggle the plaintext identifier into the plugin's log through ``str(exc)`` (and
    a traceback is no safer). Only the exception *type* may be logged.
    """
    from proactive import store as store_mod

    marker = "oc_SECRET_MARKER_9f3a7c"
    dims = store_mod.dims_for(session_key=marker, session_id="sid_SECRET_1a2b")

    class LeakyRead:
        def get_session_metadata(self, key, meta_key, default=None):
            raise OSError("backend could not load " + marker)

    class LeakyWrite:
        def get_session_metadata(self, key, meta_key, default=None):
            return default  # the key is absent: the write is allowed to try

        def set_session_metadata(self, key, meta_key, value):
            raise OSError("backend could not store " + marker)

    with caplog.at_level(logging.DEBUG):
        assert store_mod.key_state(LeakyRead(), marker) == store_mod.KEY_UNREADABLE
        assert store_mod.write_ordered(LeakyWrite(), marker, config_raw=dict(GROUP_A),
                                       state=_new_state(), dims=dims).blocked == \
            store_mod.WRITE_BLOCKED_REFUSED

    text = caplog.text
    assert marker not in text, f"the exception body was logged verbatim:\n{text}"
    assert "OSError" in text, f"the exception type is the diagnostic that must survive:\n{text}"
    assert "Traceback" not in text, f"a traceback is not safer than the message:\n{text}"
    print(f"[R11] exception bodies never logged: {len(caplog.records)} records, "
          f"types only ({'OSError' in text})")


# ══ a declaration-owned round already in flight respects the live declaration ═══════════════
# The wake is not the last word: the plugin is handed ``transform_llm_output`` immediately before the
# outbound, so a declaration removed / disabled / unreadable *while the turn runs* must still cost
# zero sends. These are the unit halves of that seam; the real lane is in
# the host-level declared-session tests.


def _release(session, advance: float = 601) -> None:
    """Drive one real wake until the plugin releases it (a durable pending marker + a ticket)."""
    assert session.wake(advance=advance) == RELEASED_WAKE, "the fixture expected a release"
    assert session.rt._tickets, "a released round must carry a ticket"


def _deliver(session, text: str = "该发的话"):
    """The plugin's own delivery seam — its last chance to silence the round before the send."""
    return session.rt.transform_llm_output(
        response_text=text, session_id=session.session_id, platform="feishu",
        turn_id="t-recheck")


# ══ a damaged origin marker is never read as "no marker" ════════════════════════════════════

DAMAGED_MARKERS = [None, "", 0, 7, 1.5, True, "nope"]


@pytest.mark.parametrize("marker", DAMAGED_MARKERS, ids=[repr(m) for m in DAMAGED_MARKERS])
def test_a_damaged_origin_marker_is_off_and_is_never_promoted_to_a_legacy_grant(
        declared, plugin_settings, marker):
    """Only a *missing* field is legacy; any other non-``declaration`` value is ``off:invalid``."""
    _mirrored(declared)
    blob = dict(declared.blob)
    blob["origin"] = marker
    declared.store.set_session_metadata(declared.key, C.META_KEY, blob)
    before = declared.blob_text()

    read = declared.rt.read_session(declared.store, declared.key, declared.dims)
    assert (read.reason, read.field_name) == (C.REASON_INVALID, "origin"), (read.reason, read.field_name)
    assert read.origin == "", "a damaged marker must never surface as the legacy origin"

    plugin_settings(None)  # no declaration at all: the blob alone must not authorise
    assert declared.wake(advance=601) == [{"action": "skip", "reason": C.REASON_INVALID}]
    assert declared.rt._tickets == {}, "a damaged marker released a round"
    assert declared.blob_text() == before, "the damaged blob was rewritten"

    # …and a declaration may not quietly mirror over it either: the damage is the only evidence.
    declared.declare(declaration())
    assert declared.wake(advance=601) == [{"action": "skip", "reason": C.REASON_INVALID}]
    assert declared.blob_text() == before, "the declaration erased a damaged marker"
    print(f"[R13] origin={marker!r} → {C.REASON_INVALID} (field origin), blob untouched, no write")


# ══ the ticket TTL is not an exemption from the declaration ════════════════════════════════
# The TTL bounds how long a *stale* ticket may judge a turn it may not own (it must never silence a
# later user turn). It is not a licence to send a round whose authorisation was withdrawn while that
# round was still the session's own in-flight release. The ownership proof is the durable pending
# marker: it is written by the release, and cleared by every event that could make the running turn
# someone else's (a user message, a pause, a settlement, a later wake). So the TTL boundary — and
# both sides of it — must reach the same verdict for a declaration-owned round.


def _age(session, seconds: float) -> None:
    """Move the shared clock forward *after* the release (the round outliving the ticket)."""
    session.clock["now"] += float(seconds)


def _past_ttl(session) -> None:
    _age(session, C.PENDING_RELEASE_TTL_DEFAULT_SECONDS + 1)


# ══ whose round is this seam ending? (process-local ownership) ═════════════════════════════
# The durable pending marker is unavailable exactly where the hard cases live: an OFF session may
# not be written at all (0 writes), so a user message cannot retire the marker, and the release
# keeps looking "in flight" forever. The process-local stamp ``_record_user_takeover`` (set by the
# admission of a user message) closes that hole. One predicate answers the question for the TTL
# boundary *and* the in-TTL declaration re-check: ``runtime._whose_round_is_this_seam``.


def _taken_over_off_session(declared, plugin_settings):
    """The counterexample setup: release, then the declaration goes away, then the user writes.

    Order matters: the declaration must be gone *before* the message is admitted, because that is what
    makes the session OFF and forbids the plugin to write the metadata key. The plugin therefore
    cannot retire the pending marker, and the last seam must not silence the user.
    """
    _mirrored(declared)
    _release(declared)
    plugin_settings(None)                       # the operator withdraws the declaration
    before = declared.blob_text()
    declared.user_message("在吗", advance=5.0)   # a real user message, admitted, nothing written
    assert declared.blob_text() == before, "an OFF session must not be written on admission"
    assert (declared.state["pending"] or {}).get("released_at") is not None, (
        "the deadlock this case is about: the marker cannot be retired without a write")
    return before


def _new_state():
    """A fresh, valid session state for the declaration's group-A config (no store involved)."""
    from proactive import config as _config
    from proactive import policy

    parsed = _config.parse(dict(GROUP_A))
    assert parsed.ok and parsed.config is not None
    return policy.new_state(T0, parsed.config, support.FixedRng(0.0))
