# -*- coding: utf-8 -*-
"""The inbound identity seam, in a process with no host.

What this file proves, and how it must be run
---------------------------------------------
The plugin's inbound identity (session key, current session id, the six session dimensions) is
translated from host-shaped objects into **plain data** by :mod:`proactive.host_adapter`, so the
decision logic can be exercised without the host. That claim is only worth anything if the test
physically cannot import Hermes, so run it like this::

    $env:PYTHONPATH=''
    python -m pytest -q --noconftest -p no:cacheprovider tests/test_portable_event_boundary.py

``--noconftest`` matters: ``tests/conftest.py`` imports ``hermes_cli`` before anything else, so a
normal run would silently put the host on the path and the test would prove nothing about
portability. ``test_the_process_really_has_no_host`` therefore asserts the condition the command is
supposed to establish — if somebody runs it the ordinary way, that test fails loudly instead of
passing on a host-loaded process.

What this file does **not** prove: that host behaviour is unchanged — that belongs to the host-E2E
lane — and it does not prove the package is host-independent overall, only that this one seam is.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:  # stand-alone run from the repository root
    sys.path.insert(0, str(PKG_ROOT))

from proactive import host_adapter as A  # noqa: E402  (after the sys.path pin)

#: The dimension keys the persisted blob carries, in the order ``store.dims_for`` builds them. The
#: seam must answer exactly these six, so a later refactor cannot silently drop or rename one.
DIM_KEYS = ("session_key", "session_id", "profile", "platform", "chat_id", "thread_id")


def test_dimension_keys_match_the_persisted_blob_contract():
    from proactive import store

    assert tuple(store.DIM_KEYS) == A.DIMENSION_KEYS == DIM_KEYS


#: True when this interpreter can import the host. The guard below is only meaningful in the
#: stand-alone ``--noconftest`` run the module docstring prescribes; in the ordinary suite the host is
#: on the path (conftest imports ``hermes_cli``), so the precondition cannot hold and the guard is an
#: environment false positive rather than a portability regression.
_HOST_ON_PATH = importlib.util.find_spec("hermes_cli") is not None


@pytest.mark.skipif(
    _HOST_ON_PATH,
    reason="portability guard: run standalone with --noconftest and no host on PYTHONPATH; the "
           "normal suite loads hermes_cli via conftest",
)
def test_the_process_really_has_no_host():
    """The precondition of every other case: no Hermes on the path, none imported (AC-PORT-001)."""
    assert importlib.util.find_spec("hermes_cli") is None
    assert importlib.util.find_spec("gateway") is None
    assert "hermes_cli" not in sys.modules
    assert "gateway" not in sys.modules


# ── the seam's own surface ───────────────────────────────────────────────────────────────────


def test_event_identity_is_plain_data():
    """The answer is a record of dimension values, not a host event or source object."""
    identity = A.event_identity(_event(), gateway=None, store=None)
    assert isinstance(identity.dims, dict)
    assert tuple(identity.dims) == DIM_KEYS
    assert not hasattr(identity.dims, "to_dict")  # a host SessionSource would carry one


def test_runtime_identity_helpers_use_only_the_adapter():
    """Keep the owning runtime helpers thin so future host-field reads fail this boundary test."""
    from proactive import runtime

    event = _event(metadata={"gateway_session_key": "k"},
                   source=SimpleNamespace(platform="feishu", profile="dev",
                                          chat_id="c", thread_id="t"))
    handle = SimpleNamespace(peek_session_id=lambda key: "s" if key == "k" else None)
    key = runtime.session_key_for(event)
    session_id = runtime._peek_session_id(handle, key)
    assert runtime._dims_for_event(event, key, session_id) == A.event_identity(
        event, store=handle).dims

    tree = ast.parse((PKG_ROOT / "proactive" / "runtime.py").read_text(encoding="utf-8"))
    target_names = {"session_key_for", "_peek_session_id", "_dims_for_event"}
    definitions = {node.name: node for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name in target_names}
    assert set(definitions) == target_names
    for name, node in definitions.items():
        expected = {
            "session_key_for": ("session_key_from", ("event", "gateway")),
            "_peek_session_id": ("session_id_from", ("store_handle", "session_key")),
            "_dims_for_event": ("dimensions_from", ("event", "session_key", "session_id")),
        }[name]
        assert _is_adapter_only_body(node, *expected), name


def test_scoped_guard_rejects_extra_host_field_reads():
    """One adapter call must not conceal a direct read beside or inside that call."""
    separate_read = ast.parse(
        "def wrapper(event, gateway):\n"
        "    ignored = event.metadata\n"
        "    return _adapter.session_key_from(event, gateway)\n"
    ).body[0]
    argument_read = ast.parse(
        "def wrapper(event, gateway):\n"
        "    return _adapter.session_key_from(event.metadata, gateway)\n"
    ).body[0]
    for node in (separate_read, argument_read):
        assert not _is_adapter_only_body(node, "session_key_from", ("event", "gateway"))


def _is_adapter_only_body(node, expected_method, expected_args):
    """Require one bare adapter return; no extra statements or field reads in its arguments."""
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):  # ignore the helper's docstring
            body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    call = body[0].value
    if not isinstance(call, ast.Call) or call.keywords:
        return False
    func = call.func
    return (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id == "_adapter" and func.attr == expected_method
            and len(call.args) == len(expected_args)
            and all(isinstance(arg, ast.Name) and arg.id == expected
                    for arg, expected in zip(call.args, expected_args)))


# ── session key: metadata first, then the private resolver, then nothing ────────────────────


def test_the_metadata_key_wins_over_the_resolver():
    """Metadata carries the gateway's own key: the resolver is never consulted."""
    calls = []
    event = _event(metadata={"gateway_session_key": "agent:main:feishu:dm:oc_1"},
                   source=SimpleNamespace(platform="feishu"))
    gateway = SimpleNamespace(_session_key_for_source=lambda source: calls.append(source) or "x")

    identity = A.event_identity(event, gateway=gateway, store=None)

    assert identity.session_key == "agent:main:feishu:dm:oc_1"
    assert calls == []


def test_the_resolver_is_the_fallback_when_metadata_has_no_key():
    event = _event(metadata={}, source=SimpleNamespace(platform="feishu"))
    gateway = SimpleNamespace(_session_key_for_source=lambda source: "agent:dev:feishu:dm:oc_2")

    identity = A.event_identity(event, gateway=gateway, store=None)

    assert identity.session_key == "agent:dev:feishu:dm:oc_2"


def test_no_key_anywhere_is_none_not_an_invented_default():
    assert A.event_identity(_event(metadata={}), gateway=None, store=None).session_key is None
    assert A.event_identity(_event(metadata={}), gateway=SimpleNamespace(),
                            store=None).session_key is None


def test_a_keyless_event_never_probes_the_store():
    """The inbound hook returns before probing when the host supplies no session key."""
    calls = []
    handle = SimpleNamespace(peek_session_id=lambda key: calls.append(key) or "s")
    identity = A.event_identity(_event(metadata={}), store=handle)
    assert identity.session_key is None
    assert identity.session_id is None
    assert calls == []


def test_a_raising_resolver_is_not_a_key():
    def boom(source):
        raise RuntimeError("the session key is agent:main:feishu:dm:oc_secret")

    identity = A.event_identity(_event(metadata={}, source=object()),
                                gateway=SimpleNamespace(_session_key_for_source=boom),
                                store=None)

    assert identity.session_key is None
    assert identity.session_id is None


# ── current session id: the store's probe, or nothing ───────────────────────────────────────


def test_the_session_id_comes_from_the_store_probe():
    store = SimpleNamespace(peek_session_id=lambda key: "s-42")
    identity = A.event_identity(_event(metadata={"gateway_session_key": "k"}), gateway=None,
                                store=store)
    assert identity.session_id == "s-42"


def test_a_store_without_the_probe_answers_no_session_id():
    assert A.event_identity(_event(metadata={"gateway_session_key": "k"}), gateway=None,
                            store=SimpleNamespace()).session_id is None
    assert A.event_identity(_event(metadata={"gateway_session_key": "k"}), gateway=None,
                            store=None).session_id is None


def test_a_raising_probe_is_no_session_id():
    def boom(key):
        raise RuntimeError("store exploded")

    identity = A.event_identity(_event(metadata={"gateway_session_key": "k"}), gateway=None,
                                store=SimpleNamespace(peek_session_id=boom))
    assert identity.session_id is None


# ── the six dimensions ──────────────────────────────────────────────────────────────────────


def test_an_enum_platform_is_normalised_to_its_value():
    platform = SimpleNamespace(value="feishu")
    source = SimpleNamespace(platform=platform, profile="default", chat_id="oc_1", thread_id=None)
    identity = A.event_identity(_event(metadata={"gateway_session_key": "k"}, source=source),
                                gateway=None, store=SimpleNamespace(peek_session_id=lambda k: "s-1"))
    assert identity.dims == {
        "session_key": "k", "session_id": "s-1", "profile": "default",
        "platform": "feishu", "chat_id": "oc_1", "thread_id": None,
    }


def test_a_string_platform_is_kept_as_is():
    source = SimpleNamespace(platform="feishu", profile="dev", chat_id="oc_9", thread_id="t-1")
    identity = A.event_identity(_event(metadata={"gateway_session_key": "k"}, source=source),
                                gateway=None, store=None)
    assert identity.dims["platform"] == "feishu"
    assert identity.dims["profile"] == "dev"
    assert identity.dims["thread_id"] == "t-1"


def test_a_missing_source_leaves_every_dimension_none():
    identity = A.event_identity(_event(metadata={"gateway_session_key": "k"}, source=None),
                                gateway=None, store=None)
    assert identity.dims == {"session_key": "k", "session_id": None, "profile": None,
                             "platform": None, "chat_id": None, "thread_id": None}


def test_the_event_carries_no_wake_stamp_in_any_case():
    """Identity translation must not classify: a text-identical user message is still not a wake."""
    user = _event(metadata={"gateway_session_key": "k"},
                  text="[Heartbeat] please look at the deploy")
    assert A.is_wake_event(user) is False
    # and the identity seam does not smuggle in a verdict of its own
    assert A.event_identity(user, gateway=None, store=None).session_key == "k"


# ── the payload seam: the fields the release path reads ─────────────────────────────────────


def test_the_store_handle_prefers_the_hook_argument():
    """The hook's own ``session_store`` wins; the gateway's attribute is only the fallback."""
    hook_store = SimpleNamespace(name="hook")
    gateway = SimpleNamespace(session_store=SimpleNamespace(name="gateway"))
    assert A.store_handle_from(hook_store, gateway) is hook_store
    assert A.store_handle_from(None, gateway) is gateway.session_store
    assert A.store_handle_from(None, SimpleNamespace()) is None
    assert A.store_handle_from(None, None) is None


def test_the_source_is_handed_over_untouched():
    """The caller canonicalises ``source`` itself, so the seam must not copy or coerce it."""
    source = SimpleNamespace(platform="feishu", chat_id="oc_1")
    event = _event(metadata={"gateway_session_key": "k"}, source=source)
    assert A.event_source(event) is source
    assert A.event_source(_event(source=None)) is None


def test_message_id_and_text_keep_their_exact_values():
    event = _event(metadata={"gateway_session_key": "k"}, text="  two  spaces  ", message_id="m-1")
    assert A.event_message_id(event) == "m-1"
    assert A.event_text(event) == "  two  spaces  "  # no trimming: the fingerprint is exact
    plain = _event(metadata={"gateway_session_key": "k"}, text=None, message_id=None)
    assert A.event_message_id(plain) is None
    assert A.event_text(plain) is None
    assert A.event_message_id(SimpleNamespace()) is None


def test_a_naive_timestamp_is_localised_like_the_host_does():
    naive = datetime(2026, 10, 23, 9, 30, tzinfo=None)
    aware = datetime(2026, 10, 23, 9, 30, tzinfo=timezone.utc)
    assert A.event_timestamp_epoch(_event(timestamp=naive)) == naive.astimezone().timestamp()
    assert A.event_timestamp_epoch(_event(timestamp=aware)) == aware.timestamp()


def test_an_unusable_timestamp_answers_none():
    assert A.event_timestamp_epoch(_event(timestamp="2026-10-23T09:30:00")) is None
    assert A.event_timestamp_epoch(_event(timestamp=None)) is None
    assert A.event_timestamp_epoch(SimpleNamespace()) is None


def test_the_sanity_band_stays_a_plugin_decision():
    """The band is a setting, not a host shape: runtime keeps it, and it still rejects a wild value."""
    from proactive import runtime

    stale = datetime(2001, 1, 1, tzinfo=timezone.utc)
    assert runtime._event_timestamp(_event(timestamp=stale), time.time()) is None
    fresh = datetime.now(timezone.utc)
    assert runtime._event_timestamp(_event(timestamp=fresh), time.time()) is not None


def test_release_path_delegates_do_not_read_host_fields():
    """AC-PORT-002 ownership: the payload delegates and the two release-path functions stay clean."""
    tree = ast.parse((PKG_ROOT / "proactive" / "runtime.py").read_text(encoding="utf-8"))
    bodies = {node.name: node for node in ast.walk(tree)
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert _is_adapter_only_body(bodies["_store_handle"], "store_handle_from",
                                 ("session_store", "gateway"))
    for name, expected_method in (("_event_timestamp", "event_timestamp_epoch"),
                                  ("_dispatch", None)):
        assert not _direct_host_reads(bodies[name]), f"{name}: {_direct_host_reads(bodies[name])}"
    timestamp_calls = [item for item in ast.walk(bodies["_event_timestamp"])
                       if isinstance(item, ast.Call)
                       and isinstance(item.func, ast.Attribute)
                       and isinstance(item.func.value, ast.Name)
                       and item.func.value.id == "_adapter"]
    assert [call.func.attr for call in timestamp_calls] == ["event_timestamp_epoch"]


def test_the_payload_guard_rejects_a_direct_read():
    """Mutation case: a call-count-only predicate would accept this body; the field check must not."""
    read = ast.parse(
        "def wrapper(event, gateway):\n"
        "    event.timestamp\n"
        "    return _adapter.event_timestamp_epoch(event)\n"
    ).body[0]
    calls = [item for item in ast.walk(read) if isinstance(item, ast.Call)]
    assert len(calls) == 1  # one adapter call — a call-count-only predicate would pass
    assert _direct_host_reads(read) == ["timestamp"]


def _direct_host_reads(node):
    """Every ``event.<x>`` / ``gateway.<x>`` / ``getattr(event|gateway, ...)`` read in *node*."""
    found = []
    for item in ast.walk(node):
        if not isinstance(item, ast.Attribute):
            continue
        if isinstance(item.value, ast.Name) and item.value.id in ("event", "gateway"):
            found.append(item.attr)
        elif (isinstance(item.value, ast.Call) and isinstance(item.value.func, ast.Name)
              and item.value.func.id == "getattr" and item.value.args
              and isinstance(item.value.args[0], ast.Name)
              and item.value.args[0].id in ("event", "gateway")):
            found.append(item.attr)
    return found


# ── the session-metadata protocol seam: store.py names it nowhere ───────────────────────────

#: The two host methods ``proactive/store.py`` must not call by name.
METADATA_METHODS = ("get_session_metadata", "set_session_metadata")


def test_store_module_does_not_name_the_metadata_protocol():
    """AC-PORT-004 ownership, host-free: the protocol calls live in the adapter only."""
    tree = ast.parse((PKG_ROOT / "proactive" / "store.py").read_text(encoding="utf-8"))
    assert _metadata_guard_problems(tree) == []


def test_the_metadata_guard_rejects_every_way_of_bypassing_it():
    """Mutation cases: a direct call, a ``getattr`` lookup, an alias, and an indirect lookup.

    The last case is the one the guard cannot see — asserted as *not* detected on purpose, so nobody
    reads this guard as a proof.
    """
    direct = ast.parse("def _probe(store, key):\n"
                       "    return store.get_session_metadata(key, 'm', None)\n")
    getattr_lookup = ast.parse("def _probe(store, key):\n"
                               "    return getattr(store, 'get_session_metadata')(key, 'm', None)\n")
    indirect = ast.parse("def _probe(store, key):\n"
                         "    method = getattr(store, 'set_session_metadata')\n"
                         "    return method(key, 'm', None)\n")
    alias = ast.parse("def _set(store, key, v):\n"
                      "    target = store\n"
                      "    return target.set_session_metadata(key, 'm', v)\n")
    runtime_name = ast.parse("def _probe(store, key):\n"
                             "    name = 'get_' + 'session_metadata'\n"
                             "    return getattr(store, name)(key, 'm', None)\n")
    assert _metadata_guard_problems(direct)
    assert _metadata_guard_problems(getattr_lookup)
    assert _metadata_guard_problems(indirect)
    assert _metadata_guard_problems(alias)  # the receiver does not matter: the name does
    # documented blind spot: a name computed at runtime is invisible to an AST tripwire
    assert _metadata_guard_problems(runtime_name) == []


def _metadata_guard_problems(tree):
    """Every place *tree* still names one of the host's two metadata methods.

    Limits, stated so it is not over-trusted: this is a **tripwire over the AST of one file**, not a
    proof. It sees an attribute access by name (whatever the receiver — so ``target = store;
    target.set_…`` is caught) and a ``getattr``/``setattr`` with a literal name. It cannot see a
    method name held in a variable or computed at runtime, nor ``__getattribute__``/``vars()``.
    """
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in METADATA_METHODS:
            problems.append(f"{node.attr} attribute call at line {node.lineno}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("getattr", "setattr") and len(node.args) >= 2:
            name = node.args[1]
            if isinstance(name, ast.Constant) and name.value in METADATA_METHODS:
                problems.append(f"{node.func.id}(…, {name.value!r}) at line {node.lineno}")
    return problems


# ── the delivery path names no host shape ───────────────────────────────────────────────────


def test_delivery_guard_neither_names_the_protocol_nor_logs_an_exception_body():
    """AC-PORT-005: no metadata call, and no log call handed an exception object."""
    tree = ast.parse((PKG_ROOT / "proactive" / "delivery_guard.py").read_text(encoding="utf-8"))
    assert _delivery_guard_problems(tree, "delivery_guard") == []


def test_the_delivery_guard_rejects_each_shape_it_must():
    """Mutation cases: they must fail on the pre-adapter code."""
    to_dict_read = ast.parse("def _canonical(v):\n"
                             "    method = getattr(v, 'to_dict', None)\n"
                             "    return method() if callable(method) else {}\n")
    protocol_read = ast.parse("def _closed_probe(store, key):\n"
                              "    return store.get_session_metadata(key, 'm', None) is None\n")
    logged_body = ast.parse("def _closed_probe(store, key):\n"
                            "    try:\n"
                            "        return store.read(key)\n"
                            "    except Exception as exc:\n"
                            "        logger.warning('probe failed (%s: %s)', type(exc).__name__, exc)\n"
                            "        return False\n")
    logged_body_via_fstring = ast.parse("def _closed_probe(store, key):\n"
                                        "    try:\n"
                                        "        return store.read(key)\n"
                                        "    except Exception as exc:\n"
                                        "        logger.warning(f'probe failed: {exc}')\n"
                                        "        return False\n")
    logged_body_via_args = ast.parse("def _closed_probe(store, key):\n"
                                     "    try:\n"
                                     "        return store.read(key)\n"
                                     "    except Exception as exc:\n"
                                     "        logger.warning('probe failed: %s', exc.args)\n"
                                     "        return False\n")
    logged_type_only = ast.parse("def _closed_probe(store, key):\n"
                                 "    try:\n"
                                 "        return store.read(key)\n"
                                 "    except Exception as exc:\n"
                                 "        logger.warning('probe failed (%s)', type(exc).__name__)\n"
                                 "        return False\n")
    assert _delivery_guard_problems(to_dict_read, "consume")
    assert _delivery_guard_problems(protocol_read, "delivery_guard")
    assert _delivery_guard_problems(logged_body, "delivery_guard")
    assert _delivery_guard_problems(logged_body_via_fstring, "delivery_guard")
    assert _delivery_guard_problems(logged_body_via_args, "delivery_guard")
    assert _delivery_guard_problems(logged_type_only, "delivery_guard") == []


def _delivery_guard_problems(tree, module):
    """Every remaining host shape in the delivery path.

    Tripwire, not proof, and the limits are the same family as the sibling guards: it matches names
    in the AST of one file, so a method name held in a variable, built at runtime, or reached through
    ``__getattribute__``/``vars()`` is invisible to it.
    """
    problems = []
    for node in ast.walk(tree):
        if module == "consume" and isinstance(node, ast.Constant) and node.value == "to_dict":
            problems.append(f"the source's own to_dict is named at line {node.lineno}")
        elif module == "consume" and isinstance(node, ast.Attribute) and node.attr == "to_dict":
            problems.append(f"a to_dict attribute access at line {node.lineno}")
        elif module == "delivery_guard":
            if isinstance(node, ast.Attribute) and node.attr in METADATA_METHODS:
                problems.append(f"{node.attr} attribute access at line {node.lineno}")
            elif isinstance(node, ast.Call) and _logs_an_exception_object(node):
                problems.append(f"an exception body handed to a log call at line {node.lineno}")
    return problems


def _logs_an_exception_object(node):
    """True when a ``logger.*(...)`` call passes a bare exception rather than its type or a digest.

    Blind spots, measured rather than assumed: ``str(exc)``, an f-string that interpolates ``exc``, and
    a differently named or post-processed variable all pass this check. It is a tripwire over the AST of
    one file, not a proof — the behavioural guarantee for the probe lives in
    ``tests/test_host_adapter.py`` (a raising store's message must not appear in the log record).
    """
    func = node.func
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id in ("logger", "log", "logging")):
        return False
    for argument in list(node.args) + [kw.value for kw in node.keywords]:
        if isinstance(argument, ast.Name) and argument.id in ("exc", "e", "err", "error", "exception"):
            return True
        if isinstance(argument, ast.Attribute) and argument.attr == "args":
            return True
        if isinstance(argument, ast.JoinedStr):
            names = {child.id for child in ast.walk(argument) if isinstance(child, ast.Name)}
            if names & {"exc", "e", "err", "error", "exception"}:
                return True
    return False


# ── the isolation detector and the queue name no host shape ─────────────────────────────────


def test_isolation_keeps_the_decisions_and_the_adapter_keeps_the_names():
    """AC-PORT-007: no flag/class literal and no environment read left in the detector."""
    tree = ast.parse((PKG_ROOT / "proactive" / "isolation.py").read_text(encoding="utf-8"))
    assert _isolation_guard_problems(tree) == []


def test_the_isolation_and_platform_guards_reject_the_pre_change_shapes():
    """Mutation cases built from the pre-adapter code."""
    old_isolation = ast.parse("HOST_PROCESS_ENV = 'HERMES_PLUGIN_HOST_PROCESS'\n"
                              "HOST_CTX_TYPE = 'RemotePluginContext'\n"
                              "def _entry(environ, name):\n"
                              "    source = os.environ if environ is None else environ\n"
                              "    return source.get(name)\n")
    old_platform = ast.parse("def route_key(source):\n"
                             "    platform = source.get('platform')\n"
                             "    return getattr(platform, 'value', platform)\n")
    current_isolation = ast.parse("HOST_PROCESS_ENV = _adapter.HOST_PROCESS_ENV\n"
                                  "def in_plugin_host(environ=None):\n"
                                  "    return _adapter.host_process_flag(environ)\n")
    current_platform = ast.parse("def route_key(source):\n"
                                 "    return _adapter.platform_value(source.get('platform'))\n")
    assert _isolation_guard_problems(old_isolation)
    assert _platform_guard_problems(old_platform)
    assert _isolation_guard_problems(current_isolation) == []
    assert _platform_guard_problems(current_platform) == []


def _isolation_guard_problems(tree):
    """Host names or an environment read left in the detector.

    Tripwire over one file's AST: it matches the four literals, an ``os`` import and an
    ``<anything>.environ`` attribute. Blind spots, measured rather than assumed: ``from os import
    environ`` plus a bare ``environ.get(...)``, a literal held in a variable first, an alias such as
    ``builtins.getattr``, and a name built at runtime all pass. It is a tripwire, not a proof.
    """
    literals = {"HERMES_PLUGIN_HOST_PROCESS", "RemotePluginContext", "Record", "Opaque"}
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in literals:
            problems.append(f"{node.value!r} is named at line {node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr == "environ":
            problems.append(f"the process environment is read at line {node.lineno}")
        elif isinstance(node, ast.Import):
            problems.extend(f"imports {alias.name} at line {node.lineno}"
                            for alias in node.names if alias.name == "os")
    return problems


def _platform_guard_problems(tree):
    """A hand-rolled enum unwrap (``getattr(<x>, "value", <x>)``) left in the queue."""
    problems = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) >= 2):
            continue
        name = node.args[1]
        if isinstance(name, ast.Constant) and name.value == "value":
            problems.append(f"a hand-rolled enum unwrap at line {node.lineno}")
    return problems


# ── the entry point and the runtime name no host shape ──────────────────────────────────────


def test_the_entry_point_does_not_call_register_hook_itself():
    """AC-PORT-008: the registration call is the adapter's (`register_hooks`)."""
    tree = ast.parse((PKG_ROOT / "__init__.py").read_text(encoding="utf-8"))
    assert _registration_guard_problems(tree) == []


def test_the_runtime_does_not_parse_the_session_key_itself():
    """The key layout (`agent:<ns>:<platform>:<chat_type>`) belongs beside the gateway's inverse."""
    tree = ast.parse((PKG_ROOT / "proactive" / "runtime.py").read_text(encoding="utf-8"))
    assert _session_key_guard_problems(tree) == []


def test_the_registration_and_key_guards_reject_the_pre_change_shapes():
    """Mutation cases built from the pre-adapter code."""
    old_registration = ast.parse("def register(ctx):\n"
                                 "    for name in names:\n"
                                 "        ctx.register_hook(name, callbacks[name])\n")
    old_key_parse = ast.parse("def profile_from_session_key(key):\n"
                              "    parts = str(key).split(':')\n"
                              "    if parts[0] != 'agent':\n"
                              "        return None\n"
                              "    return parts[1]\n")
    current_registration = ast.parse("def register(ctx):\n"
                                     "    host_adapter.register_hooks(ctx, table())\n")
    current_key_parse = ast.parse("def profile_from_session_key(key):\n"
                                  "    return _adapter.profile_from_session_key(key)\n")
    assert _registration_guard_problems(old_registration)
    assert _session_key_guard_problems(old_key_parse)
    assert _registration_guard_problems(current_registration) == []
    assert _session_key_guard_problems(current_key_parse) == []


def _registration_guard_problems(tree):
    """A ``<something>.register_hook(...)`` attribute call left in the entry point.

    Tripwire over one file's AST. Blind spots: a call reached through
    ``getattr(ctx, "register_hook")``, a bound method stored in a variable first, and a name assembled
    at runtime are all invisible to it.
    """
    return [f"a register_hook call at line {node.lineno}" for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "register_hook"]


def _session_key_guard_problems(tree):
    """The session-key layout (an ``"agent"`` literal or a ``split(":")``) left in the runtime.

    Blind spots: a prefix assembled at runtime, a delimiter held in a variable, and ``partition`` /
    ``rsplit`` / ``re.split`` instead of ``split`` are not seen.
    """
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "agent":
            problems.append(f"the key namespace prefix is named at line {node.lineno}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "split" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == ":":
                problems.append(f"a session key is split at line {node.lineno}")
    return problems


# ── the transcript source names no schema ───────────────────────────────────────────────────


def test_the_transcript_source_holds_no_schema_knowledge():
    """AC-PORT-009: the statements and the display_kind vocabulary come from the adapter."""
    tree = ast.parse((PKG_ROOT / "proactive" / "sources" / "session_transcript.py")
                     .read_text(encoding="utf-8"))
    assert _schema_guard_problems(tree) == []


def test_the_schema_guard_rejects_the_pre_change_shape():
    """Mutation case built from the pre-adapter code."""
    old = ast.parse("_MESSAGES_SQL = '''\n"
                    "SELECT id, content FROM messages\n"
                    "WHERE role = 'user' AND display_kind NOT IN (?, ?, ?)\n"
                    "'''\n"
                    "EXCLUDED = ('internal_notification', 'hidden', 'failed_turn')\n")
    current = ast.parse("from .. import host_adapter as _adapter\n"
                        "_MESSAGES_SQL = _adapter.TRANSCRIPT_MESSAGES_SQL\n"
                        "EXCLUDED = _adapter.EXCLUDED_DISPLAY_KINDS\n")
    assert _schema_guard_problems(old)
    assert _schema_guard_problems(current) == []


def _schema_guard_problems(tree):
    """A schema fragment left in the source **code**: a SQL keyword literal, a table/column name, or a
    ``display_kind`` value.

    Prose is exempt on purpose — this module's docstrings explain the schema to a reader, and naming it
    there is documentation, not a read. The marker set is exactly: ``select``, ``from messages``,
    ``from sessions``, ``display_kind``, ``compacted``, ``_compressed_summary``,
    ``internal_notification``, ``hidden``, ``failed_turn``. Bare ``messages``/``sessions`` are **not**
    markers, because ordinary strings such as ``"max_messages"`` contain them. Tripwire over one file's
    AST, with the same family of blind spots as its siblings: SQL assembled from pieces at runtime, a
    table name held in a variable, a keyword split across literals, and a query built by a helper in
    another module are not seen.
    """
    markers = ("select", "from messages", "from sessions", "display_kind", "compacted",
               "_compressed_summary", "internal_notification", "hidden", "failed_turn")
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        text = node.value.lower()
        for marker in markers:
            if marker in text:
                problems.append(f"{marker!r} appears in a literal at line {node.lineno}")
                break
    return problems


# ── helpers ─────────────────────────────────────────────────────────────────────────────────


def _event(*, metadata=None, source=None, text="", **extra):
    """A recording stand-in for the host's inbound event: attribute access only, no host import."""
    event = SimpleNamespace(metadata=metadata if metadata is not None else {}, source=source,
                            text=text, message_id=None, timestamp=None)
    for name, value in extra.items():
        setattr(event, name, value)
    return event
