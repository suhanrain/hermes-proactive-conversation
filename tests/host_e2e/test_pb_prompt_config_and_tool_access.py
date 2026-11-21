"""Configurable proactive prompts + the boolean ``proactive_tools_enabled`` switch.

Two properties are measured here, both through the **real** seams:

* **wording** — the built-in soft / forced prompts state the tool reality of the *active* boolean:
  ``true`` = every tool is allowed; ``false`` = no tools are available this round (there is no
  read-only middle ground);
* **configuration** — ``proactive_prompt_soft`` / ``proactive_prompt_forced`` are operator templates
  rendered with ``{visible_since_user}`` / ``{minimum_visible_replies}``, with a fail-closed
  validation that falls back to the built-in default (and a WARNING) for anything unusable; and
  ``proactive_tools_enabled`` (bool, default ``true``) is enforced in ``runtime.pre_tool_call`` for a
  recognised proactive round only. Only ``proactive_tools_enabled`` is read;
  ``proactive_tool_access`` is ignored.

The tests drive ``trigger.evaluate_and_inject`` / ``runtime.pre_llm_call`` / ``runtime.pre_tool_call``
against a real ``SessionStore``, so a green run is a measurement of the plugin's own decisions.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TESTS = _HERE.parent
for _path in (str(_TESTS), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest

import support  # noqa: E402
from proactive import contract as C  # noqa: E402
from proactive import runtime  # noqa: E402
from proactive import store as pstore  # noqa: E402
from proactive import trigger  # noqa: E402

T0 = 1_800_000_000.0
DUE = T0 + 3_600.0


class _Ctx:
    """A minimal host context: only ``get_config`` (the seam every plugin setting is read through)."""

    def __init__(self, settings):
        self._settings = dict(settings)

    def get_config(self, key, default=None):
        return self._settings.get(key, default)


@pytest.fixture(autouse=True)
def _clean_seams():
    runtime.reset_seams()
    trigger.reset()
    yield
    runtime.reset_seams()
    trigger.reset()


def _raw_config(**over):
    raw = {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "dnd_start": "23:00",
        "dnd_end": "09:00",
        "min_idle_seconds": 3_600,
        "max_idle_seconds": 3_600,
        "window_max_attempts": 4,
        "window_min_visible": 1,
        "day_max_silent": 6,
        "day_max_attempts": 6,
    }
    raw.update(over)
    return raw


def _cfg(**over):
    from proactive import config as pconfig

    parsed = pconfig.parse(_raw_config(**over))
    assert parsed.ok is True, (parsed.field, parsed.reason)
    return parsed.config


def _round(tmp_path, *, settings=None, minimum=0, seed_visible=0):
    """Enable a session, bind the plugin context, release one round, capture the injected text."""
    store = support.new_store(tmp_path)
    source = support.dm_source(chat_id="oc-prompt-config")
    entry = support.seed_session(store, source)
    dims = support.dims_for(store, entry, source)

    runtime.set_clock(lambda: T0)
    assert runtime.enable_session(
        store, entry.session_key, dims,
        _raw_config(window_min_visible=minimum),
    )
    if seed_visible:
        read = pstore.read(store, entry.session_key, dims)
        seeded = read.state
        seeded.visible_count = seed_visible
        seeded.visible_since_user = seed_visible
        assert pstore.write(store, entry.session_key, config_raw=read.config_raw, state=seeded,
                            dims=dims, origin=read.origin)

    if settings is not None or True:
        # Wording tests measure the tool-ALLOWED prompt unless they explicitly override the switch;
        # tools are fail-closed by default, so default the round's context to enabled.
        ctx_settings = {C.SETTING_TOOLS_ENABLED: True}
        if settings:
            ctx_settings.update(settings)
        runtime.bind_plugin_context(_Ctx(ctx_settings))

    calls = []

    def inject(text, session_key=None):
        calls.append(text)
        return True

    runtime.set_clock(lambda: DUE)
    outcome = trigger.evaluate_and_inject(
        store, session_key=entry.session_key, session_id=entry.session_id, dims=dims,
        now=DUE, inject=inject, rng=support.FixedRng(0.5),
    )
    assert outcome.accepted is True, outcome.reason
    assert len(calls) == 1
    return store, entry, dims, calls[0]


# ── 1. the default wording matches the boolean switch ───────────────────────────────────────


def test_default_soft_prompt_with_tools_enabled_allows_every_tool():
    prompt = trigger.PROMPT_WITH_TOOLS
    assert "You may use any of your tools this round (all tools are allowed)." in prompt
    # The read-only-only middle ground must not appear in the default wording.
    assert "low-risk read-only tools" not in prompt
    assert "No tools are available" not in prompt


def test_default_soft_prompt_with_tools_disabled_refuses_every_tool():
    prompt = trigger.PROMPT_NO_TOOLS
    assert "No tools are available in this round" in prompt
    assert "do not call any" in prompt
    assert "all tools are allowed" not in prompt


def test_default_forced_prompt_allows_every_tool_and_keeps_the_honesty_tail():
    prompt = trigger.DEFAULT_PROMPT_FORCED
    assert ("all tools, including command-line, code-execution, browser, file-writing, payment "
            "and messaging, are allowed") in prompt
    assert "If a lookup tool fails (e.g. no network), say what you can without inventing" in prompt
    # anti-repetition + must-speak semantics are unchanged (they are just now configurable text)
    for marker in ("already replied", "do NOT repeat", "paraphrase", "genuinely NEW",
                   "MUST send the user a visible reply", "[SILENT]", "NO_REPLY", "empty body"):
        assert marker in prompt, marker
    assert "{visible_since_user}" in prompt and "{minimum_visible_replies}" in prompt


def test_soft_round_injects_the_tools_enabled_wording_by_default(tmp_path):
    _store, _entry, _dims, injected = _round(tmp_path, minimum=0)
    assert injected == trigger.PROMPT_WITH_TOOLS
    assert "all tools are allowed" in injected


# ── 2. configurable templates ────────────────────────────────────────────────────────────────


def test_custom_forced_template_renders_live_values(tmp_path):
    template = "FORCED n={visible_since_user} min={minimum_visible_replies} — say something new."
    _store, _entry, _dims, injected = _round(
        tmp_path, settings={C.SETTING_PROMPT_FORCED: template}, minimum=2, seed_visible=1
    )
    assert injected == "FORCED n=1 min=2 — say something new."


def test_custom_soft_template_is_used_and_rendered():
    runtime.bind_plugin_context(_Ctx({C.SETTING_PROMPT_SOFT: "SOFT n={visible_since_user}",
                                      C.SETTING_TOOLS_ENABLED: True}))
    from proactive import state as pstate

    state = pstate.SessionState(visible_since_user=0)
    assert trigger.proactive_prompt(state, _cfg()) == "SOFT n=0"


@pytest.mark.parametrize(
    ("value", "label"),
    [
        ("", "empty"),
        ("   ", "blank"),
        (123, "not-a-string"),
        (["a"], "list"),
        ("hello {unknown_placeholder}", "unknown-placeholder"),
        ("hello {visible_since_user} {0}", "positional-placeholder"),
        ("stray { brace", "unformattable"),
    ],
)
def test_unusable_forced_template_falls_back_to_default_with_a_warning(caplog, value, label):
    runtime.bind_plugin_context(_Ctx({C.SETTING_PROMPT_FORCED: value,
                                      C.SETTING_TOOLS_ENABLED: True}))
    with caplog.at_level(logging.WARNING):
        prompt = trigger.forced_visible_prompt(0, 2)
    assert prompt == trigger._default_forced_prompt(0, 2, access=True)
    assert "MUST send the user a visible reply" in prompt
    assert any("proactive_prompt_forced" in rec.getMessage() for rec in caplog.records), label


def test_unusable_soft_template_falls_back_to_default_with_a_warning(caplog):
    runtime.bind_plugin_context(_Ctx({C.SETTING_PROMPT_SOFT: "boom {",
                                      C.SETTING_TOOLS_ENABLED: True}))
    from proactive import state as pstate

    with caplog.at_level(logging.WARNING):
        prompt = trigger.proactive_prompt(pstate.SessionState(visible_since_user=0), _cfg())
    assert prompt == trigger.PROMPT_WITH_TOOLS
    assert any("proactive_prompt_soft" in rec.getMessage() for rec in caplog.records)


# ── 3. the boolean proactive_tools_enabled switch ───────────────────────────────────────────


def _bound_round(tmp_path, settings):
    """Release one round under *settings* and bind the turn, returning the session identity."""
    store, entry, dims, injected = _round(tmp_path, settings=settings, minimum=0)
    recognised = runtime.pre_llm_call(
        user_message=injected, turn_id="t-access", session_id=entry.session_id
    )
    assert recognised is not False
    return store, entry, dims, injected


def test_tools_enabled_true_allows_every_tool(tmp_path):
    """``true`` allows every tool."""
    settings = {C.SETTING_TOOLS_ENABLED: True}
    _store, entry, _dims, injected = _bound_round(tmp_path, settings)
    assert injected == trigger.PROMPT_WITH_TOOLS
    assert "all tools are allowed" in injected
    for name in ("payment_charge", "terminal", "terminal_run", "execute_code", "write_file"):
        assert runtime.pre_tool_call(tool_name=name, turn_id="t-access",
                                     session_id=entry.session_id) is None, name


def test_tools_enabled_false_refuses_every_tool_and_switches_the_prompt(tmp_path):
    settings = {C.SETTING_TOOLS_ENABLED: False}
    _store, entry, _dims, injected = _bound_round(tmp_path, settings)
    assert injected == trigger.PROMPT_NO_TOOLS
    assert "No tools are available in this round" in injected
    for name in ("weather_lookup", "terminal", "execute_code", "payment_charge"):
        blocked = runtime.pre_tool_call(tool_name=name, turn_id="t-access",
                                        session_id=entry.session_id)
        assert isinstance(blocked, dict) and blocked["action"] == "block", name
        assert "denied:proactive-tool" in blocked["message"]


def test_an_ordinary_user_turn_is_never_braked_whatever_the_switch_says(tmp_path):
    settings = {C.SETTING_TOOLS_ENABLED: False}
    _store, entry, _dims, _injected = _bound_round(tmp_path, settings)
    # A different turn_id was never bound by pre_llm_call: it is the user's own turn.
    assert runtime.pre_tool_call(tool_name="payment_charge", turn_id="t-user",
                                 session_id=entry.session_id) is None


def test_the_switch_defaults_to_false_when_unset():
    runtime.bind_plugin_context(_Ctx({}))
    assert trigger.tools_enabled() is False


@pytest.mark.parametrize("value", ["true", "false", "yes", 1, 0, [], {}])
def test_a_non_boolean_switch_falls_back_to_false_with_a_warning(caplog, value):
    runtime.bind_plugin_context(_Ctx({C.SETTING_TOOLS_ENABLED: value}))
    with caplog.at_level(logging.WARNING):
        assert trigger.tools_enabled() is False
    assert any(C.SETTING_TOOLS_ENABLED in rec.getMessage() and "not a boolean" in rec.getMessage()
               for rec in caplog.records), value


def test_missing_key_is_fail_closed_no_tools_and_blocks(tmp_path):
    """No ``proactive_tools_enabled`` key → no tools, no-tools prompt, pre_tool_call blocks."""
    store = support.new_store(tmp_path)
    source = support.dm_source(chat_id="oc-no-tools-key")
    entry = support.seed_session(store, source)
    dims = support.dims_for(store, entry, source)

    runtime.set_clock(lambda: T0)
    assert runtime.enable_session(
        store, entry.session_key, dims,
        _raw_config(window_min_visible=0),
    )
    runtime.bind_plugin_context(_Ctx({}))  # proactive_tools_enabled deliberately absent

    calls = []

    def inject(text, session_key=None):
        calls.append(text)
        return True

    runtime.set_clock(lambda: DUE)
    outcome = trigger.evaluate_and_inject(
        store, session_key=entry.session_key, session_id=entry.session_id, dims=dims,
        now=DUE, inject=inject, rng=support.FixedRng(0.5),
    )
    assert outcome.accepted is True, outcome.reason
    assert trigger.tools_enabled() is False
    assert calls[0] == trigger.PROMPT_NO_TOOLS
    assert "No tools are available in this round" in calls[0]

    assert runtime.pre_llm_call(
        user_message=calls[0], turn_id="t-no-tools", session_id=entry.session_id) is not False
    blocked = runtime.pre_tool_call(tool_name="terminal", turn_id="t-no-tools",
                                    session_id=entry.session_id)
    assert isinstance(blocked, dict) and blocked["action"] == "block"
    assert "denied:proactive-tool" in blocked["message"]


def test_a_custom_template_never_claims_tools_when_the_switch_is_false():
    """A tools-disabled round has no tools, so the code appends the no-tools sentence."""
    runtime.bind_plugin_context(
        _Ctx({C.SETTING_PROMPT_SOFT: "SOFT custom body", C.SETTING_TOOLS_ENABLED: False})
    )
    from proactive import state as pstate

    prompt = trigger.proactive_prompt(pstate.SessionState(visible_since_user=0), _cfg())
    assert prompt.startswith("SOFT custom body")
    assert "(No tools are available in this round.)" in prompt


def test_soft_template_with_an_unknown_placeholder_falls_back(caplog):
    runtime.bind_plugin_context(_Ctx({C.SETTING_PROMPT_SOFT: "hi {nope}",
                                      C.SETTING_TOOLS_ENABLED: True}))
    from proactive import state as pstate

    with caplog.at_level(logging.WARNING):
        prompt = trigger.proactive_prompt(pstate.SessionState(visible_since_user=0), _cfg())
    assert prompt == trigger.PROMPT_WITH_TOOLS
    assert any("proactive_prompt_soft" in rec.getMessage() for rec in caplog.records)
