# -*- coding: utf-8 -*-
"""The host adapter is the package's ONLY host surface.

What is real: the adapter's own seams, driven directly with a recording fake context and
with a deliberately broken one. What is NOT here: the package's behaviour — that is the
host_e2e lane's job (AC-SEAM-002).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import support  # noqa: F401  (pins the throwaway HERMES_HOME before hermes loads)

from proactive import host_adapter as A


class FakeLLM:
    def __init__(self):
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text="ok", usage=SimpleNamespace(input_tokens=1, output_tokens=1))


class FakeManifest:
    def __init__(self, source="user", path=None, name="proactive-conversation"):
        self.source = source
        self.path = path
        self.name = name


def _ctx(*, get_config=None, manifest=None, llm=None):
    ctx = SimpleNamespace()
    if get_config is not None:
        ctx.get_config = get_config
    if manifest is not None:
        ctx.manifest = manifest
    if llm is not None:
        ctx.llm = llm
    return ctx


@pytest.fixture(autouse=True)
def _clean():
    A.reset_context()
    yield
    A.reset_context()


# ── seam 1: configuration ─────────────────────────────────────────────────────────────────────


def test_config_reads_through_the_context():
    seen = []

    def get_config(key, default=None):
        seen.append((key, default))
        return {"composition": {"a": 1}}.get(key, default)

    A.bind_context(_ctx(get_config=get_config))
    assert A.config("composition", None) == {"a": 1}
    assert seen == [("composition", None)]


def test_config_without_a_context_answers_the_default():
    A.reset_context()
    assert A.config("anything", "fallback") == "fallback"


def test_config_without_get_config_answers_the_default():
    A.bind_context(_ctx())  # no get_config attribute at all
    assert A.config("anything", "fallback") == "fallback"


def test_config_a_raising_host_answers_the_default_and_logs_the_type_only(caplog):
    import logging

    def boom(key, default=None):
        raise RuntimeError("the session key is agent:main:feishu:dm:oc_secret")

    A.bind_context(_ctx(get_config=boom))
    with caplog.at_level(logging.WARNING):
        assert A.config("composition", "fallback") == "fallback"
    # the exception TYPE is logged; its body (which quotes a session key) never is
    assert "RuntimeError" in caplog.text
    assert "oc_secret" not in caplog.text


def test_config_reader_is_the_bound_reader():
    calls = []

    def get_config(key, default=None):
        calls.append(key)
        return "value"

    A.bind_context(_ctx(get_config=get_config))
    reader = A.config_reader()
    assert callable(reader)
    assert reader("k", "d") == "value"
    assert calls == ["k"]
    A.reset_context()
    assert A.config_reader() is None


# ── seam 2: identity (``ctx.manifest``) ───────────────────────────────────────────────────────


def test_manifest_source_and_path_from_a_user_manifest():
    A.bind_context(_ctx(manifest=FakeManifest(source="user", path=r"C:\home\plugins\proactive-conversation")))
    assert A.manifest_source() == "user"
    assert A.manifest_path() == r"C:\home\plugins\proactive-conversation"
    assert A.manifest_name() == "proactive-conversation"


def test_manifest_absent_means_no_evidence_not_default_profile():
    A.bind_context(_ctx())  # no manifest
    assert A.manifest_source() == ""
    assert A.manifest_path() is None
    assert A.manifest_name() is None
    A.bind_context(_ctx(manifest=FakeManifest(source="bundled")))
    assert A.manifest_source() == "bundled"


# ── seam 3: the LLM façade ────────────────────────────────────────────────────────────────────


def test_llm_is_the_bound_facade():
    facade = FakeLLM()
    A.bind_context(_ctx(llm=facade))
    assert A.llm() is facade


def test_llm_absent_is_none():
    A.bind_context(_ctx())
    assert A.llm() is None
    A.reset_context()
    assert A.llm() is None


# ── seam 4: the host's silence rules ──────────────────────────────────────────────────────────


def test_host_silence_verdict_true_for_an_exact_marker(monkeypatch):
    # a fake host module: gateway.response_filters with the real predicate's contract
    import sys
    import types

    pkg = types.ModuleType("gateway")
    filters = types.ModuleType("gateway.response_filters")
    filters.is_intentional_silence_response = lambda text: str(text).strip() in ("[SILENT]", "SILENT")
    filters.display_kind_for_event = lambda event: "dm"
    filters.silence_allowed = lambda kind, reply_expected: kind == "dm"
    sys.modules["gateway"] = pkg
    sys.modules["gateway.response_filters"] = filters
    try:
        assert A.host_silence_verdict("[SILENT]") is True
        assert A.host_silence_verdict("a real chat") is False
        assert A.host_silence_is_legal(object()) is True
    finally:
        sys.modules.pop("gateway.response_filters", None)
        sys.modules.pop("gateway", None)


def test_host_silence_unreadable_host_fails_safe(monkeypatch, caplog):
    """No importable ``gateway.response_filters`` (a host that cannot be asked): both halves
    answer their fail-safe value and the exception TYPE is logged, never a body."""
    import logging
    import sys

    monkeypatch.delitem(sys.modules, "gateway.response_filters", raising=False)
    monkeypatch.delitem(sys.modules, "gateway", raising=False)
    monkeypatch.setattr("builtins.__import__", _no_gateway_import)
    with caplog.at_level(logging.WARNING):
        assert A.host_silence_verdict("[SILENT]") is False   # not silence → deliver
        assert A.host_silence_is_legal(object()) is False    # not legal → no exemption
    assert "ImportError" in caplog.text


def _no_gateway_import(name, *args, **kwargs):
    if name.split(".")[0] == "gateway":
        raise ImportError("no gateway module")
    return _real_import(name, *args, **kwargs)


import builtins as _builtins

_real_import = _builtins.__import__


def test_host_silence_a_raising_predicate_is_not_a_verdict(monkeypatch, caplog):
    import sys
    import types
    import logging

    filters = types.ModuleType("gateway.response_filters")

    def boom(text):
        raise ValueError("provider exploded")

    filters.is_intentional_silence_response = boom
    filters.display_kind_for_event = boom
    filters.silence_allowed = boom
    sys.modules["gateway"] = types.ModuleType("gateway")
    sys.modules["gateway.response_filters"] = filters
    try:
        with caplog.at_level(logging.WARNING):
            assert A.host_silence_verdict("[SILENT]") is False
            assert A.host_silence_is_legal(object()) is False
        assert "ValueError" in caplog.text
    finally:
        sys.modules.pop("gateway.response_filters", None)
        sys.modules.pop("gateway", None)


# ── seam 5: what a wake is ────────────────────────────────────────────────────────────────────


def test_wake_events_carry_the_host_stamp():
    event = SimpleNamespace(_heartbeat_session_id="s-1", text="anything")
    assert A.is_wake_event(event) is True
    assert A.wake_session_id(event) == "s-1"


def test_a_user_who_types_heartbeat_is_not_a_wake():
    event = SimpleNamespace(text="[Heartbeat] hello", _heartbeat_session_id=None)
    assert A.is_wake_event(event) is False
    assert A.wake_session_id(event) is None


def test_an_event_without_the_attribute_at_all_is_not_a_wake():
    assert A.is_wake_event(SimpleNamespace()) is False
    assert A.is_wake_event(None) is False


def test_internal_channel_diagnostic():
    internal = SimpleNamespace(internal=True, metadata={"gateway_session_id": "s-1"})
    plain = SimpleNamespace(internal=False, metadata={})
    user = SimpleNamespace(internal=True, metadata={"other": 1})
    assert A.wake_internal_channel(internal) is True
    assert A.wake_internal_channel(plain) is False
    assert A.wake_internal_channel(user) is False


# ── seam 9: the session-metadata protocol ───────────────────────────────────────────────────


def test_the_metadata_read_forwards_the_callers_sentinel():
    """The default is the *caller's* object: store.py relies on identity to tell absent from null."""
    absent = object()
    seen = []

    class Store:
        def get_session_metadata(self, session_key, meta_key, default):
            seen.append((session_key, meta_key, default))
            return default

    assert A.read_session_metadata(Store(), "k", "proactive-conversation:session", absent) is absent
    assert seen == [("k", "proactive-conversation:session", absent)]


def test_the_metadata_write_answers_the_stores_truthiness():
    calls = []

    class Store:
        def __init__(self, answer):
            self.answer = answer

        def set_session_metadata(self, session_key, meta_key, value):
            calls.append((session_key, meta_key, value))
            return self.answer

    assert A.write_session_metadata(Store(True), "k", "m", {"a": 1}) is True
    assert A.write_session_metadata(Store(1), "k", "m", None) is True
    assert A.write_session_metadata(Store(False), "k", "m", None) is False
    assert A.write_session_metadata(Store(None), "k", "m", None) is False
    assert calls[1] == ("k", "m", None)  # a close (JSON null) is written as ``None``, untouched


def test_a_raising_store_reaches_the_caller_unchanged():
    """The adapter must not swallow: store.py owns the verdict and the log line for a fault."""

    class Store:
        def get_session_metadata(self, *args):
            raise RuntimeError("the session key is agent:main:feishu:dm:oc_secret")

        def set_session_metadata(self, *args):
            raise RuntimeError("write refused")

    with pytest.raises(RuntimeError):
        A.read_session_metadata(Store(), "k", "m", None)
    with pytest.raises(RuntimeError):
        A.write_session_metadata(Store(), "k", "m", None)


# The AST guard over ``proactive/store.py`` lives in ``tests/test_portable_event_boundary.py``
# (``_metadata_guard_problems``), where it also runs host-free and carries its mutation cases. A
# second, weaker copy here would only invite drift: this file keeps the seam's forwarding contract.


# ── seam 10: the inbound source as a plain dict ─────────────────────────────────────────────


def test_the_source_dict_passes_a_dict_through_by_identity():
    source = {"platform": "feishu", "chat_id": "oc_1"}
    assert A.source_as_dict(source) is source
    assert A.source_as_dict(None) == {}


def test_the_source_dict_calls_to_dict_once():
    calls = []

    class Source:
        def to_dict(self):
            calls.append(True)
            return {"platform": "feishu", "chat_id": "oc_2"}

    assert A.source_as_dict(Source()) == {"platform": "feishu", "chat_id": "oc_2"}
    assert calls == [True]


def test_an_unusable_source_answers_an_empty_dict():
    class NoToDict:
        pass

    class NotADict:
        def to_dict(self):
            return ["feishu"]

    class Raising:
        def to_dict(self):
            raise RuntimeError("the session key is agent:main:feishu:dm:oc_secret")

    assert A.source_as_dict(NoToDict()) == {}
    assert A.source_as_dict(NotADict()) == {}
    assert A.source_as_dict(Raising()) == {}


def test_a_raising_store_never_prints_its_message_or_the_key():
    """A fault is "no grant", and the log line carries neither the key nor the store's text.

    A store's exception message is third-party text that may quote the key it was handed, so the probe
    must log a digest and the exception *type* only.
    """
    import logging

    from proactive import delivery_guard

    secret_key = "agent:main:feishu:dm:oc_super_secret_chat"
    marker = "cannot read the metadata for " + secret_key

    class Store:
        def get_session_metadata(self, *args):
            raise RuntimeError(marker)

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture()
    logger = logging.getLogger(delivery_guard.__name__)
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        assert delivery_guard._closed_probe(Store(), secret_key) is False
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert records, "the probe must say something when the store cannot be read"
    joined = "\n".join(records)
    assert secret_key not in joined, "the session key must never be logged"
    assert marker not in joined, "the store's exception body must never be logged"
    assert "RuntimeError" in joined, "the exception type must survive — it is the useful part"


# ── seam 11: the process home ───────────────────────────────────────────────────────────────


def test_the_process_home_prefers_the_environment_value(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "env-home"))
    assert A.process_home() == tmp_path / "env-home"
    monkeypatch.setenv("HERMES_HOME", f"  {tmp_path / 'padded'}  ")
    assert A.process_home() == tmp_path / "padded"  # stripped, as before


def test_the_process_home_falls_back_to_the_hosts_own_resolution(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    try:
        from hermes_constants import get_hermes_home
    except Exception:  # no host on the path: the seam must answer "no answer"
        assert A.process_home() is None
    else:
        assert A.process_home() == Path(get_hermes_home())


def test_a_broken_environment_value_still_raises(monkeypatch):
    """Parity: the environment branch was never inside the fallback's ``try``, so it still raises.

    Windows refuses a NUL inside an environment value at ``setenv`` time, so the failure is produced
    where it matters: the path construction the seam performs on the environment value.
    """
    monkeypatch.setenv("HERMES_HOME", "unbuildable-home")
    real_path = A.Path

    def picky(value):
        if str(value) == "unbuildable-home":
            raise ValueError("this value cannot be a path")
        return real_path(value)

    monkeypatch.setattr(A, "Path", picky)
    with pytest.raises(ValueError):
        A.process_home()


def test_composition_no_longer_takes_a_context(monkeypatch, tmp_path):
    """The composition seams take no caller context: the process home is the only answer."""
    import inspect

    from proactive import composition

    for function in (composition.instance_home, composition.resolve_home, composition.build):
        parameters = inspect.signature(function).parameters
        assert "ctx" not in parameters, f"{function.__name__} still advertises a context"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "process-home"))
    assert composition.default_home() == tmp_path / "process-home"
    assert composition.resolve_home()[0] == tmp_path / "process-home"


# ── seam 12: the isolation detector's host names ────────────────────────────────────────────


def test_the_host_names_have_their_documented_values():
    assert A.HOST_PROCESS_ENV == "HERMES_PLUGIN_HOST_PROCESS"
    assert A.HOST_CONTEXT_TYPE == "RemotePluginContext"
    assert A.WIRE_RECORD_TYPE == "Record"
    assert A.WIRE_PLACEHOLDER_TYPE == "Opaque"


def test_the_process_flag_is_exactly_one(monkeypatch):
    for value, expected in (("1", True), (" 1 ", True), ("0", False), ("yes", False),
                            ("", False), ("true", False)):
        monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", value)
        assert A.host_process_flag() is expected, value
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    assert A.host_process_flag() is False


def test_the_process_flag_prefers_an_injected_mapping(monkeypatch):
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    assert A.host_process_flag({}) is False
    assert A.host_process_flag({"HERMES_PLUGIN_HOST_PROCESS": "1"}) is True

    class Broken:
        def get(self, name, default=None):
            raise RuntimeError("this mapping cannot be read")

    assert A.host_process_flag(Broken()) is False  # a detector must not become an exception


def test_the_platform_value_unwraps_an_enum_and_leaves_anything_else_alone():
    import enum

    class Platform(enum.Enum):
        FEISHU = "feishu"

    assert A.platform_value(Platform.FEISHU) == "feishu"
    assert A.platform_value("feishu") == "feishu"
    assert A.platform_value(None) is None

    class ValueRaises:
        @property
        def value(self):
            raise RuntimeError("no value here")

    # Parity: ``getattr(obj, "value", obj)`` only falls back on AttributeError, so a property that
    # raises still reaches the caller — the seam must not invent a default here.
    with pytest.raises(RuntimeError):
        A.platform_value(ValueRaises())


def test_the_isolation_detectors_still_answer_through_the_seam():
    from proactive import isolation

    assert isolation.in_plugin_host({"HERMES_PLUGIN_HOST_PROCESS": "1"}) is True
    assert isolation.in_plugin_host({"HERMES_PLUGIN_HOST_PROCESS": "0"}) is False
    assert isolation.plugin_host_signature(None, {"HERMES_PLUGIN_HOST_PROCESS": "1"}) == \
        "env:HERMES_PLUGIN_HOST_PROCESS=1"
    assert isolation.plugin_host_signature(object(), {}) is None


# ── seam 13/14: hook registration and the session-key layout ────────────────────────────────


def test_registration_forwards_each_hook_in_order():
    """The package's only host call, behind the seam: same names, same order, same arguments."""
    calls = []

    class Ctx:
        def register_hook(self, name, callback):
            calls.append((name, callback))

    def first():
        return None

    def second():
        return None

    A.register_hooks(Ctx(), [("pre_gateway_dispatch", first), ("transform_llm_output", second)])
    assert calls == [("pre_gateway_dispatch", first), ("transform_llm_output", second)]


def test_registration_does_not_swallow_the_hosts_own_error():
    class Ctx:
        def register_hook(self, name, callback):
            raise RuntimeError("the host refused this hook")

    with pytest.raises(RuntimeError):
        A.register_hooks(Ctx(), [("pre_gateway_dispatch", lambda: None)])


def test_the_session_key_layout_is_read_in_the_adapter():
    """Parity with the gateway's own inverse, including the legacy and the ``main~`` namespaces."""
    assert A.profile_from_session_key("agent:main:feishu:dm:oc_1") == "default"
    assert A.profile_from_session_key("agent:pm:feishu:dm:oc_1") == "pm"
    assert A.profile_from_session_key("agent:main~:feishu:dm:oc_1") == "main"
    assert A.profile_from_session_key("agent:pm:feishu:group:oc_1") == "pm"
    # nothing provable
    assert A.profile_from_session_key("feishu:dm:oc_1") is None
    assert A.profile_from_session_key("agent:pm:feishu") is None
    assert A.profile_from_session_key("agent::feishu:dm:oc_1") is None
    assert A.profile_from_session_key("agent:pm:feishu:dm"[:0]) is None
    assert A.profile_from_session_key(None) is None

    class Unprintable:
        def __str__(self):
            raise RuntimeError("no")

    assert A.profile_from_session_key(Unprintable()) is None


def test_runtime_still_answers_through_the_seam():
    from proactive import runtime

    for key in ("agent:main:feishu:dm:oc_1", "agent:pm:feishu:dm:oc_1", "nonsense", None):
        assert runtime.profile_from_session_key(key) == A.profile_from_session_key(key)


# ── seam 15: the transcript schema ──────────────────────────────────────────────────────────


def test_the_transcript_statements_are_the_documented_text():
    """Byte-identical to the text the source holds: the read must not shift by one column."""
    assert A.EXCLUDED_DISPLAY_KINDS == ("internal_notification", "hidden", "failed_turn")
    assert A.TRANSCRIPT_SESSION_SQL == "SELECT session_key, profile_name FROM sessions WHERE id = ?"
    messages = A.TRANSCRIPT_MESSAGES_SQL
    assert "FROM messages" in messages
    assert "role = 'user'" in messages and "active = 1" in messages and "compacted = 0" in messages
    assert "_compressed_summary" in messages
    assert "display_kind NOT IN (?, ?, ?)" in messages
    assert "ORDER BY timestamp DESC, id DESC" in messages and "LIMIT ?" in messages


def test_the_transcript_statements_run_against_a_fixture_database(tmp_path):
    """The statements plus the source's parameter order produce rows — the schema is really theirs."""
    import sqlite3

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE sessions (id TEXT, session_key TEXT, profile_name TEXT)")
        conn.execute("CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT,"
                     " timestamp REAL, _compressed_summary TEXT, active INTEGER, compacted INTEGER,"
                     " display_kind TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('s1', 'agent:pm:feishu:dm:oc_1', 'pm')")
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, NULL, 1, 0, ?)",
            [(1, "s1", "user", "hello there", 1000.0, None),
             (2, "s1", "user", "a heartbeat", 1000.0, "internal_notification"),
             (3, "s1", "assistant", "reply", 1000.0, None)])
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute(A.TRANSCRIPT_SESSION_SQL, ("s1",)).fetchone() == (
            "agent:pm:feishu:dm:oc_1", "pm")
        params = ("s1", *A.EXCLUDED_DISPLAY_KINDS, 500.0, 2000.0, 10)
        rows = conn.execute(A.TRANSCRIPT_MESSAGES_SQL, params).fetchall()
    finally:
        conn.close()
    assert [row[0] for row in rows] == [1], rows  # the heartbeat row and the assistant turn are out


def test_the_source_reads_through_the_adapter_statements():
    from proactive.sources import session_transcript as source_module

    assert source_module._MESSAGES_SQL is A.TRANSCRIPT_MESSAGES_SQL
    assert source_module._SESSION_SQL == A.TRANSCRIPT_SESSION_SQL
    assert source_module.EXCLUDED_DISPLAY_KINDS == A.EXCLUDED_DISPLAY_KINDS


# ── the hook table ────────────────────────────────────────────────────────────────────────────


def test_hook_names_are_the_four_declared_in_the_manifest():
    assert A.hook_names() == ("pre_gateway_dispatch", "transform_llm_output",
                              "pre_tool_call", "pre_llm_call")


def test_no_other_module_names_the_host():
    """AC-SEAM-001, enforced as a test: every proactive module except the adapter is
    import-clean of gateway/hermes_cli/agent and of direct ctx attribute access.

    Comments and docstrings may *name* the host surface (that is where the knowledge lives);
    what must not exist outside the adapter is CODE that touches it. The check therefore
    parses each module's AST and inspects import statements and attribute chains only.
    """
    import ast
    import pathlib

    pkg = pathlib.Path(__file__).resolve().parents[1] / "proactive"
    offenders = []
    for path in sorted(pkg.glob("*.py")):
        if path.name == "host_adapter.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in ("gateway", "hermes_cli", "agent"):
                        offenders.append(f"{path.name}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in ("gateway", "hermes_cli", "agent"):
                    offenders.append(f"{path.name}:{node.lineno}: from {node.module} import ...")
            elif isinstance(node, ast.Attribute):
                # ctx.get_config / ctx.manifest / ctx.llm / getattr(ctx, ...)
                if isinstance(node.value, ast.Name) and node.value.id == "ctx":
                    if node.attr in ("get_config", "manifest", "llm"):
                        offenders.append(f"{path.name}:{node.lineno}: ctx.{node.attr}")
    assert not offenders, "host surface outside the adapter:\n" + "\n".join(offenders)


# ── the outbound health-claim check is absent ───────────────────────────────────────────────


#: Names of the second, health-grading model call. None may be importable, configurable or matched
#: by an audit string.
_REMOVED_CHECK_NAMES = (
    "health_check",
    "HEALTH_CHECK",
    "_health_claim_is_cleared",
    "PURPOSE_HEALTH_CHECK",
    "silent:health-claim-unverified",
    "set_health_checker",
)


def test_no_module_or_manifest_names_the_removed_health_claim_check():
    """The absence of the health-claim check is verified against the package and its manifest.

    The same check is available from the shell; this test keeps it true for the next edit, on any
    platform's tooling.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    files = sorted((root / "proactive").rglob("*.py")) + [root / "plugin.yaml"]
    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for name in _REMOVED_CHECK_NAMES:
            if name in text:
                offenders.append(f"{path.relative_to(root).as_posix()}: {name}")
    assert not offenders, "the removed health-claim check is still named:\n" + "\n".join(offenders)


def test_the_runtime_exposes_no_checker_seam():
    """No seam exists to inject a verdict, and no stored checker either."""
    from proactive import runtime

    assert not hasattr(runtime, "set_health_checker")
    assert not hasattr(runtime, "_health_checker")
