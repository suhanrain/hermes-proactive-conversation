# -*- coding: utf-8 -*-
"""The plugin context is reachable without the host.

The adapter owns the single host context: ``runtime.bind_plugin_context`` only forwards to it, and
``resolve_content_home()`` / the install call take no context argument at all. The guards here read
code: no host import and no environment read in ``composition``, and no second module-level context
copy in ``runtime``.

Run it the same way as the other portable file, with no Hermes on the path::

    $env:PYTHONPATH=''
    python -m pytest -q --noconftest -p no:cacheprovider tests/test_portable_context_boundary.py

What it does **not** prove: that host behaviour is unchanged (that is the host-E2E lane), or that the
*prose* in ``composition.py`` never mentions the environment variable — its docstrings explain it. The
guards here read code: no read, no import, no context parameter.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
from types import SimpleNamespace

import pytest

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from proactive import composition, host_adapter as A, runtime  # noqa: E402  (after the sys.path pin)


def _clean():
    A.reset_context()
    runtime.bind_plugin_context(None)


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
    assert importlib.util.find_spec("hermes_cli") is None
    assert importlib.util.find_spec("gateway") is None


def test_runtime_keeps_no_second_copy_of_the_context():
    """``runtime`` must keep no second copy of the host context, not even an unused one."""
    assert not hasattr(runtime, "_plugin_ctx")


def test_bind_forwards_and_plugin_context_reads_the_adapter():
    _clean()
    context = SimpleNamespace(get_config=lambda key, default=None: default,
                              manifest=SimpleNamespace(source="user", path=None, name="proactive-conversation"))
    runtime.bind_plugin_context(context)
    try:
        assert A.context() is context
        assert runtime.plugin_context() is context
        replacement = SimpleNamespace(get_config=lambda key, default=None: default)
        runtime.bind_plugin_context(replacement)
        assert runtime.plugin_context() is replacement
    finally:
        _clean()
    assert runtime.plugin_context() is None


def test_composition_names_no_host_module_or_environment_variable():
    """The process home is asked for through the adapter, not read here."""
    tree = ast.parse((PKG_ROOT / "proactive" / "composition.py").read_text(encoding="utf-8"))
    assert _composition_guard_problems(tree) == []


def test_the_composition_guard_rejects_each_host_shape():
    """Mutation cases: each host shape the guard forbids must fail it."""
    env_read = ast.parse("def default_home():\n"
                         "    import os\n"
                         "    env = os.environ.get('HERMES_HOME')\n"
                         "    return env\n")
    host_import = ast.parse("def default_home():\n"
                            "    from hermes_constants import get_hermes_home\n"
                            "    return get_hermes_home()\n")
    host_import_module = ast.parse("import hermes_constants\n")
    okay = ast.parse("def default_home():\n"
                     "    home = _adapter.process_home()\n"
                     "    return home if home is not None else Path.home() / '.hermes'\n")
    assert _composition_guard_problems(env_read)
    assert _composition_guard_problems(host_import)
    assert _composition_guard_problems(host_import_module)
    assert _composition_guard_problems(okay) == []


def test_the_home_resolution_advertises_no_context_parameter():
    """No resolution function takes a context argument — enforced on the real source, not on locals."""
    tree = ast.parse((PKG_ROOT / "proactive" / "composition.py").read_text(encoding="utf-8"))
    assert _context_parameter_problems(tree) == []


def test_the_signature_guard_rejects_the_pre_change_shape():
    """Mutation cases, one per way a context parameter could appear."""
    old_shapes = ast.parse("def instance_home(ctx: Any) -> Optional[Path]:\n"
                           "    return None\n"
                           "def resolve_home(ctx=None):\n"
                           "    return None\n"
                           "def build(config, *, home=None, ctx: Any = None):\n"
                           "    return None\n")
    clean = ast.parse("def instance_home() -> Optional[Path]:\n"
                      "    return None\n"
                      "def build(config, *, home=None, clock=None):\n"
                      "    return None\n")
    found = _context_parameter_problems(old_shapes)
    assert sorted(name for name, _ in found) == ["build", "instance_home", "resolve_home"]
    assert _context_parameter_problems(clean) == []


def _context_parameter_problems(tree):
    """``[(function, parameter)]`` for every context-shaped parameter in *tree*.

    Deliberately stricter than "named exactly ctx": a parameter called ``context`` or
    ``plugin_context`` would be the same mistake under another name.
    """
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = node.args
        names = [a.arg for a in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]
        for name in names:
            if "ctx" in name.lower() or "context" in name.lower():
                problems.append((node.name, name))
    return problems


def _composition_guard_problems(tree):
    """Every place *tree* reads the host's home or imports its module.

    Tripwire over one file's AST: it sees the literal ``HERMES_HOME`` string and an import of
    ``hermes_constants``; an environment name assembled at runtime is invisible to it.
    """
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "HERMES_HOME":
            problems.append(f"the environment variable is named at line {node.lineno}")
        elif isinstance(node, ast.Import):
            problems.extend(f"imports {alias.name} at line {node.lineno}"
                            for alias in node.names
                            if alias.name.split(".")[0] in ("hermes_cli", "hermes_constants",
                                                            "gateway", "agent"))
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in ("hermes_cli", "hermes_constants", "gateway",
                                                     "agent"):
                problems.append(f"imports from {node.module} at line {node.lineno}")
    return problems


def test_home_resolution_is_unchanged_and_needs_no_context_argument(tmp_path):
    """The instance identity still decides the data root — through the adapter, not through runtime."""
    _clean()
    home = tmp_path / "profile-home"
    plugin_dir = home / "plugins" / "proactive-conversation"
    plugin_dir.mkdir(parents=True)
    manifest = SimpleNamespace(source="user", path=str(plugin_dir), name="proactive-conversation")
    runtime.bind_plugin_context(SimpleNamespace(manifest=manifest,
                                               get_config=lambda key, default=None: default))
    try:
        assert runtime.resolve_content_home() == composition.resolve_home()
        resolved, source = runtime.resolve_content_home()
        assert resolved == home
        assert source == composition.HOME_FROM_PLUGIN_INSTANCE
    finally:
        _clean()
    # Without any identity the process view answers.
    assert runtime.resolve_content_home() == composition.resolve_home()


def test_the_source_no_longer_passes_a_context_around():
    """The runtime owns no context: no ``_plugin_ctx`` name, and no ``ctx=`` argument at a seam."""
    tree = ast.parse((PKG_ROOT / "proactive" / "runtime.py").read_text(encoding="utf-8"))
    assert _context_guard_problems(tree) == []


def test_the_guard_rejects_a_reintroduced_context():
    """Mutation case: the guard itself must fail on each way a context could come back."""
    positional = ast.parse("def resolve_content_home():\n"
                           "    return _composition.resolve_home(_plugin_ctx)\n")
    keyword = ast.parse("def refresh():\n"
                        "    _composition.install(m, c, ctx=_plugin_ctx)\n")
    renamed = ast.parse("def bind(ctx):\n    global _host_ctx\n    _host_ctx = ctx\n")
    renamed_call = ast.parse("def f():\n"
                             "    return _composition.resolve_home(_host_ctx)\n")
    assert _context_guard_problems(positional)  # a positional context argument
    assert _context_guard_problems(keyword)     # a ctx= keyword argument
    assert _context_guard_problems(renamed)     # a renamed second copy of the context
    assert _context_guard_problems(renamed_call)  # ... and its use at a seam


def _context_guard_problems(tree):
    """Every reason *tree* violates the single-owner rule; empty means it conforms.

    Two limits, stated so nobody over-trusts it: it recognises a second copy by assignment to a
    ``_*_ctx``/``*_context``-shaped global name (not by dataflow), and it reads the *source of
    ``runtime.py`` only* — a context smuggled through another module is out of its reach.
    """
    problems = []
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    if "_plugin_ctx" in names:
        problems.append("the historical _plugin_ctx name is back")

    def _context_shaped(name):
        lowered = name.lower()
        return "ctx" in lowered or "context" in lowered

    # A *second copy* can only live at module scope (or be declared ``global``): locals named
    # ``context`` inside a function are ordinary data and are not what this guard is about.
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        for target in targets:
            if _context_shaped(target.id):
                problems.append(f"a module-level context copy under {target.id}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            problems.extend(f"a global context copy under {name}"
                            for name in node.names if _context_shaped(name))
        elif isinstance(node, ast.Call):
            if any(kw.arg == "ctx" for kw in node.keywords) and not _is_binding_call(node):
                problems.append("a ctx= argument at a seam")
            if isinstance(node.func, ast.Attribute) and node.func.attr == "resolve_home" and node.args:
                problems.append("a positional argument to resolve_home")
    return problems


def _is_binding_call(node):
    """``_adapter.bind_context(ctx)``-style calls are the one legal way to pass the context on."""
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr.startswith("bind")
