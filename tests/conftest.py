"""Fixtures for the proactive-conversation plugin tests.

Isolation, stated plainly: importing ``support`` pins a throwaway ``HERMES_HOME`` before any hermes
module loads, so plugin discovery, config lookups and session stores in this suite only ever touch
that directory. Nothing here reads or writes the live profile, its ``config.yaml``, or its
``state.db``; no platform is contacted, no token is spent, and the plugin is never installed into a
production profile.

The plugin under test is the real package: its files are copied into
``<ISO_HOME>/plugins/proactive-conversation/`` and loaded by hermes' own loader (``discover_plugins``), so
the registered callbacks are the code in this directory — not a hand-written stand-in.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


# ── live-host gate: integration tests only run where a real Hermes host exists ──────────────
#
# ``tests/host_e2e/`` exercises the *installed* plugin through a live Hermes host (its ``state.db``
# / gateway). Those cases are tagged ``integration`` automatically (by path, below) and are skipped
# on a plain checkout that has no host — a public repo must not be red the moment it is cloned.
# CI (and the release script) sets ``HERMES_HOST_REQUIRED=1`` to assert the host *is* present: in
# that mode a missing host is a hard failure, never a silent skip, so a broken pipeline cannot turn
# assertions green by accident.
#
# The probe has to read the *operator's* ``HERMES_HOME`` before ``support`` (imported a few lines
# down) repoints it at a throwaway temp dir. So it is computed here, ahead of the import.
def _host_is_reachable() -> bool:
    """True iff ``HERMES_HOME`` points at a directory that holds a ``state.db``.

    This is deliberately conservative and never raises: a missing/unreadable ``HERMES_HOME`` or a
    missing ``state.db`` simply means "no live host here".
    """
    home = os.environ.get("HERMES_HOME")
    if not home:
        return False
    try:
        return (Path(home) / "state.db").is_file()
    except OSError:
        return False


_HOST_REACHABLE = _host_is_reachable()
_HOST_REQUIRED = os.environ.get("HERMES_HOST_REQUIRED") == "1"
#: Integration tests are opt-in: a host being installed is not consent to run a suite that is
#: pinned to a particular host API. Only this flag (or the CI hard-requirement flag) runs them.
_INTEGRATION_OPT_IN = os.environ.get("PCHAT_RUN_INTEGRATION") == "1"
#: The operator's HERMES_HOME as it was *before* ``support`` repointed it (for diagnostics only).
_HOST_HOME_AT_PROBE = os.environ.get("HERMES_HOME")
_HOST_E2E_DIR = Path(__file__).resolve().parent / "host_e2e"

import support  # noqa: E402  (importing it pins HERMES_HOME)


@pytest.fixture(scope="session")
def iso_home():
    """The throwaway hermes home every test in this session resolves against."""
    return support.ISO_HOME


@pytest.fixture(scope="session")
def installed_plugin_dir(iso_home):
    """The plugin as installed under the isolated home (files copied from this package)."""
    return support.install_plugin()


@pytest.fixture
def plugin(installed_plugin_dir, monkeypatch):
    """The plugin, freshly loaded through hermes' loader, with its hooks live."""
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(support.ISO_HOME / "no-bundled-plugins"))
    loaded = support.load_plugin()
    from proactive import runtime

    runtime.reset_seams()
    yield loaded
    support.loaded_runtime().reset_seams()
    runtime.reset_seams()
    support.unload_plugins()


@pytest.fixture
def plugin_runtime(plugin):
    """The runtime module the registered callbacks live in (seams + tickets are module state).

    A released round makes exactly one model call, so there is no separate checker to script.
    """
    runtime = support.loaded_runtime()
    return runtime


@pytest.fixture
def plugin_settings():
    """Write plugin-level settings into the isolated ``config.yaml`` for one test, then restore it.

    The file is the real profile config of the throwaway home, so a test that sets a key exercises
    the same path a user's edit does (``ctx.get_config`` → ``plugins.entries.<id>.settings``).
    """
    original = support.config_text()
    yield support.write_plugin_settings
    support.CONFIG_PATH.write_text(original, encoding="utf-8")


@pytest.fixture
def store(tmp_path):
    """A real per-test ``SessionStore`` (real journal; per-session metadata is real)."""
    return support.new_store(tmp_path)


@pytest.fixture(autouse=True)
def _no_seam_leak():
    """Never let a test's clock/RNG seam outlive the test."""
    yield
    import sys

    if "proactive" in sys.modules or any(m.startswith("proactive.") for m in sys.modules):
        from proactive import runtime

        runtime.reset_seams()


@pytest.fixture(scope="session", autouse=True)
def _route_the_plugins_aux_question():
    """Prepare every scripted provider for a test run.

    A proactive round is a normal agent turn, so the plugin makes no generation request of its own:
    :mod:`tests.fake_provider` only initialises the scripted-answer attributes those tests use, and
    every auxiliary call goes to whatever the fake server already had. Session-scoped and installed
    on the class, i.e. before any per-test fixture builds a server; the patch is removed at teardown.
    """
    import importlib.util
    import sys
    from pathlib import Path

    import hermes_cli

    # The scripted provider lives in the hermes checkout's own ``tests/fakes``. It is importable only
    # once that checkout's root is on ``sys.path`` (the host's host_e2e harness does the same); appended
    # rather than inserted so nothing installed is shadowed, and only if it is not there already.
    agent_root = str(Path(hermes_cli.__file__).resolve().parents[1])
    if agent_root not in sys.path:
        sys.path.append(agent_root)
    from tests.fakes.fake_llm_provider import FakeLLMServer

    # The router helper is loaded from this directory by path: ``tests`` is a regular package of a
    # *different* checkout in this interpreter (the host's own test tree), so ``from tests import ...``
    # would resolve there. Loading it by path keeps this suite's helper unambiguous.
    helper = Path(__file__).resolve().parent / "fake_provider.py"
    spec = importlib.util.spec_from_file_location("p12_fake_provider", helper)
    assert spec and spec.loader
    fake_provider = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fake_provider)

    original = FakeLLMServer.__init__

    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        fake_provider.install(self)

    FakeLLMServer.__init__ = __init__
    try:
        yield
    finally:
        FakeLLMServer.__init__ = original


# ── integration marker + no-host skip / CI fail-loud ───────────────────────────────────────

def pytest_configure(config):
    """Declare the ``integration`` marker (also mirrored in ``pytest.ini`` so ``-m`` works)."""
    config.addinivalue_line(
        "markers",
        "integration: opt-in host-level tests. Auto-applied to tests/host_e2e/; skipped unless "
        "PCHAT_RUN_INTEGRATION=1 (or HERMES_HOST_REQUIRED=1) is set.",
    )


def pytest_collection_modifyitems(config, items):
    """Auto-tag ``tests/host_e2e/`` as ``integration`` and gate it on explicit opt-in.

    * no ``PCHAT_RUN_INTEGRATION=1`` → every integration case is *skipped*, even on a machine that
      happens to have a host (a normal user's Hermes install is not consent to run a version-pinned
      suite);
    * opted in and host reachable → the integration cases run for real;
    * opted in, no host → skipped with a reason; with ``HERMES_HOST_REQUIRED=1`` instead → hard
      ``pytest.exit`` so a missing host in CI is a loud red, never a silent green.
    """
    integration_items = []
    for item in items:
        try:
            here = Path(str(item.fspath)).resolve()
        except (OSError, ValueError):
            continue
        if _HOST_E2E_DIR in here.parents:
            item.add_marker(pytest.mark.integration)
            integration_items.append(item)

    if not integration_items:
        return

    if not (_INTEGRATION_OPT_IN or _HOST_REQUIRED):
        skip_marker = pytest.mark.skip(
            reason="integration tests are opt-in; set PCHAT_RUN_INTEGRATION=1 to run them"
        )
        for item in integration_items:
            item.add_marker(skip_marker)
        return

    if _HOST_REACHABLE:
        return

    if _HOST_REQUIRED:
        pytest.exit(
            "HERMES_HOST_REQUIRED=1 is set but no live Hermes host was found "
            f"(HERMES_HOME={_HOST_HOME_AT_PROBE!r} has no state.db). "
            "Refusing to silently skip the integration suite: provide a real host or unset "
            "HERMES_HOST_REQUIRED.",
            returncode=1,
        )

    skip_marker = pytest.mark.skip(
        reason="PCHAT_RUN_INTEGRATION=1 is set but no live Hermes host was found"
    )
    for item in integration_items:
        item.add_marker(skip_marker)
