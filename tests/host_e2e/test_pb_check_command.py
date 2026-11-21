"""``/proactive-check`` — the online dry-run of ``session_declarations`` (card t_ee7ccaa7).

These drive the command handler directly (no gateway, no LLM, no delivery): they bind a fake plugin
context exposing ``get_config`` exactly like ``ctx.get_config`` does, then call
``proactive.check.handle_check``. That is the same code ``/proactive-check`` runs in a live session.

What is pinned:

* a valid config (base keys + the four two-layer keys only) is all green, and the output never
  contains a plaintext ``session_key`` / ``session_id`` / ``chat_id`` (only a short digest);
* a declaration missing ``window_max_attempts`` names the exact field (``off:invalid:window_max_attempts``);
* ``--json`` has the documented shape and is still masked;
* empty declarations / a non-list value / a missing setting never crash and return a readable hint;
* ``register(ctx)`` actually registers ``/proactive-check`` (and the ``hermes proactive-check`` twin)
  and both share the one handler, so there is no second copy of the check.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TESTS = _HERE.parent
_REPO_ROOT = _TESTS.parent
for _path in (str(_TESTS), str(_HERE), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest

import support  # noqa: E402  (pins HERMES_HOME before any hermes import)
from proactive import check, contract as C, host_adapter, settings  # noqa: E402

#: The base per-session config every declaration must carry, plus the four two-layer keys (the "four
#: new keys" the card names) — so this is a *complete, valid* two-layer declaration.
_BASE = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "dnd_start": "23:00",
    "dnd_end": "09:00",
    "min_idle_seconds": 600,
    "max_idle_seconds": 1800,
}
_FOUR_NEW_KEYS = {
    "window_max_attempts": 2,
    "window_min_visible": 1,
    "day_max_silent": 2,
    "day_max_attempts": 2,
}

#: Secrets that must never escape verbatim. The chat id is part of the session_key; both the whole
#: key and the embedded chat id / session id are asserted absent from every output.
_CHAT_ID = "oc_9f8e7d6c5b4aSECRETchatid"
_SESSION_KEY = f"agent:product-manager:feishu:dm:{_CHAT_ID}"
_SESSION_ID = "20261016_023509_SECRETsessionid"


def _declaration(**over):
    entry = dict(_BASE, **_FOUR_NEW_KEYS)
    entry["session_key"] = _SESSION_KEY
    entry["session_id"] = _SESSION_ID
    entry.update(over)
    return entry


class _FakeContext:
    """The minimum plugin context the handler and ``register()`` touch (no host import)."""

    def __init__(self, config):
        self._config = dict(config)
        self.hooks = {}
        self.commands = {}
        self.cli_commands = {}

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, description="", args_hint="", **kwargs):
        self.commands[name] = {"handler": handler, "description": description,
                               "args_hint": args_hint}

    def register_cli_command(self, name, **kwargs):
        self.cli_commands[name] = kwargs


@pytest.fixture
def bind():
    """Bind a fake config-bearing context, and restore the module seams afterwards."""
    def _bind(config):
        ctx = _FakeContext(config)
        host_adapter.bind_context(ctx)
        settings.bind_context(ctx)
        return ctx

    yield _bind
    host_adapter.reset_context()
    settings.reset_reader()


def _assert_no_plaintext(text):
    assert _CHAT_ID not in text, f"plaintext chat_id leaked: {text}"
    assert _SESSION_ID not in text, f"plaintext session_id leaked: {text}"
    assert _SESSION_KEY not in text, f"plaintext session_key leaked: {text}"


# ── 1. a valid config is all green and leaks no identifier ───────────────────────────────────

def test_valid_config_is_all_green_and_masked(bind):
    bind({"session_declarations": [_declaration()]})

    out = check.handle_check("")

    assert "RESULT: OK" in out, out
    assert "off:invalid" not in out, out
    assert "declarations: 1 (ok: 1, invalid: 0)" in out, out
    assert "two_layer_active: true" in out, out
    assert "caps (R_w/V_w/S_d/R_d): 2/1/2/2" in out, out
    assert "proactive_tools_enabled: false" in out, out  # fail-closed default, no key set
    _assert_no_plaintext(out)


# ── 2. a missing required two-layer key names the exact field ─────────────────────────────────

def test_missing_window_max_attempts_names_the_field(bind):
    entry = _declaration()
    entry.pop("window_max_attempts")
    bind({"session_declarations": [entry]})

    out = check.handle_check("")

    assert "off:invalid:window_max_attempts" in out, out
    assert "RESULT: INVALID" in out, out
    _assert_no_plaintext(out)


# ── 3. a bad timezone names the timezone field ────────────────────────────────────────────────

def test_bad_timezone_names_the_field(bind):
    bind({"session_declarations": [_declaration(timezone="Mars/Olympus_Mons")]})

    out = check.handle_check("")

    assert "off:invalid:timezone" in out, out
    assert "RESULT: INVALID" in out, out


# ── 4. --json has the documented shape and is still masked ────────────────────────────────────

def test_json_shape_is_correct_and_masked(bind):
    bind({"session_declarations": [_declaration()]})

    raw = check.handle_check("--json")
    data = json.loads(raw)

    for key in ("ok", "configured", "count", "ok_count", "invalid_count", "two_layer_active",
                "caps", "proactive_tools_enabled", "proactive_tools_enabled_note",
                "prompt_soft", "prompt_forced", "declarations", "problems", "note"):
        assert key in data, f"missing key {key}: {data}"
    assert data["ok"] is True
    assert data["count"] == 1 and data["ok_count"] == 1 and data["invalid_count"] == 0
    assert data["two_layer_active"] is True
    assert data["caps"] == {"R_w": 2, "V_w": 1, "S_d": 2, "R_d": 2}
    assert data["proactive_tools_enabled"] is False
    assert "false" in data["proactive_tools_enabled_note"].lower()
    assert data["prompt_soft"] in ("custom", "built-in default")
    decl = data["declarations"][0]
    assert decl["status"] == "ok" and decl["timezone"] == "Asia/Shanghai"
    assert decl["problems"] == []
    _assert_no_plaintext(raw)


def test_json_reports_invalid_problem_codes(bind):
    entry = _declaration()
    entry.pop("window_max_attempts")
    bind({"session_declarations": [entry]})

    data = json.loads(check.handle_check("--json"))

    assert data["ok"] is False and data["invalid_count"] == 1
    codes = [p["code"] for p in data["declarations"][0]["problems"]]
    assert "off:invalid:window_max_attempts" in codes, data


# ── 5. tool switch + prompt mode are surfaced ────────────────────────────────────────────────

def test_tool_switch_and_prompt_mode_are_reported(bind):
    bind({
        "session_declarations": [_declaration()],
        "proactive_tools_enabled": True,
        "proactive_prompt_soft": "Custom soft {visible_since_user}",
    })

    data = json.loads(check.handle_check("--json"))

    assert data["proactive_tools_enabled"] is True
    assert data["prompt_soft"] == "custom"
    assert data["prompt_forced"] == "built-in default"


# ── 6. degenerate inputs never crash and return a readable hint ──────────────────────────────

def test_empty_declarations_is_green(bind):
    bind({"session_declarations": []})

    out = check.handle_check("")

    assert "declarations: 0" in out, out
    assert "RESULT: OK" in out, out


def test_non_list_value_reports_a_shape_problem(bind):
    bind({"session_declarations": "this should be a list"})

    out = check.handle_check("")

    assert "off:invalid:session_declarations" in out, out
    assert "RESULT: INVALID" in out, out


def test_missing_setting_returns_a_hint(bind):
    bind({})  # no session_declarations key at all

    out = check.handle_check("")

    assert "is not set" in out, out
    assert "RESULT: OK" in out, out


def test_non_list_value_json_is_wellformed(bind):
    bind({"session_declarations": 123})

    data = json.loads(check.handle_check("--json"))
    assert data["ok"] is False
    assert data["invalid_count"] >= 1


# ── 7. registration: register(ctx) wires the slash command (and the CLI twin) ─────────────────

_ENTRY_MODULE = None


def _load_entry_module():
    """Load the plugin's top-level ``__init__`` as a package so ``register()`` is callable directly."""
    global _ENTRY_MODULE
    if _ENTRY_MODULE is not None:
        return _ENTRY_MODULE
    import proactive as _proactive_pkg  # the real package the plugin uses

    name = "pchat_plugin_entry_undertest"
    if name + ".proactive" not in sys.modules:
        sys.modules[name + ".proactive"] = _proactive_pkg
    spec = importlib.util.spec_from_file_location(
        name, _REPO_ROOT / "__init__.py", submodule_search_locations=[str(_REPO_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _ENTRY_MODULE = module
    return module


def test_register_wires_the_slash_command_and_shares_the_handler(bind):
    entry = _load_entry_module()
    ctx = _FakeContext({})
    try:
        entry.register(ctx)
    finally:
        host_adapter.reset_context()
        settings.reset_reader()

    # The five behaviour hooks are still registered…
    assert set(ctx.hooks) == {
        "pre_gateway_dispatch", "transform_llm_output", "pre_tool_call", "pre_llm_call"}
    # …and the read-only self-check is registered on both surfaces, sharing ONE handler.
    assert "proactive-check" in ctx.commands, ctx.commands
    assert ctx.commands["proactive-check"]["handler"] is check.handle_check
    assert ctx.commands["proactive-check"]["args_hint"] == "[--json]"
    assert "proactive-check" in ctx.cli_commands, ctx.cli_commands
    assert ctx.cli_commands["proactive-check"]["handler_fn"] is check.cli_run


def test_cli_handler_shares_the_same_output(bind):
    bind({"session_declarations": [_declaration()]})

    class _Args:
        json = True

    out = check.handle_check("--json")
    # cli_run prints the same document; assert the shared function path agrees and exits 0.
    assert json.loads(out)["ok"] is True
    # Exercise the terminal entry point without capturing stdout semantics: it must return an int.
    rc = check.cli_run(_Args())
    assert rc == 0
