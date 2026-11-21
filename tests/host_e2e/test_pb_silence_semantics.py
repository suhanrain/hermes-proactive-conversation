"""Silent-answer and tool-turn semantics.

These tests exercise the plugin boundary only: exact host silence markers are suppressed without
settlement, while an allowed tool call keeps the proactive binding until a final visible answer.
No response body, tool result, or session content is logged by the assertions.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TESTS = _HERE.parent
for _p in (str(_TESTS), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

import pytest

import support  # noqa: E402
from proactive import contract as C          # noqa: E402
from proactive import policy, runtime, trigger  # noqa: E402
from proactive import store as pstore  # noqa: E402

T0 = 1_800_000_000.0
DUE = T0 + 3600.0


def _config(**over):
    base = {"enabled": True, "timezone": "Asia/Shanghai", "dnd_start": "23:00", "dnd_end": "09:00",
            "min_idle_seconds": 3600, "max_idle_seconds": 3600,
            "window_max_attempts": 6, "window_min_visible": 1, "day_max_silent": 6, "day_max_attempts": 6}
    base.update(over)
    return base


def _setup(tmp_path):
    store = support.new_store(tmp_path)
    source = support.dm_source(chat_id="oc_silence")
    entry = support.seed_session(store, source)
    dims = support.dims_for(store, entry, source)
    runtime.set_clock(lambda: T0)
    assert runtime.enable_session(store, entry.session_key, dims, _config())
    runtime.set_clock(lambda: DUE)
    calls = []

    def inject(text, session_key=None):
        calls.append((text, session_key))
        return True

    outcome = trigger.evaluate_and_inject(
        store, session_key=entry.session_key, session_id=entry.session_id, dims=dims,
        now=DUE, inject=inject, rng=support.FixedRng(0.5))
    assert outcome.accepted
    runtime.pre_llm_call(user_message=calls[0][0], turn_id="t-silent", session_id=entry.session_id)
    return store, entry.session_key, entry.session_id, dims


@pytest.fixture(autouse=True)
def _clean():
    runtime.reset_seams()
    trigger.reset()
    # Tools are fail-closed by default; these tests measure the tool/binding paths, so bind
    # a tools-enabled context (a test that wants them blocked binds its own).
    runtime.bind_plugin_context(type("Ctx", (), {
        "get_config": lambda self, key, default=None: (
            True if key == C.SETTING_TOOLS_ENABLED else default
        ),
    })())
    yield
    runtime.reset_seams()
    trigger.reset()


@pytest.mark.parametrize("marker", ["[SILENT]", "NO_REPLY"])
def test_exact_host_silence_marker_is_not_counted(tmp_path, marker):
    store, skey, sid, dims = _setup(tmp_path)

    result = runtime.transform_llm_output(response_text=marker, session_id=sid, turn_id="t-silent")

    assert result == trigger.SILENT
    state = pstore.read(store, skey, dims).state
    assert state.visible_count == 0
    assert state.visible_since_user == 0
    assert state.pending is None


def test_allowed_tool_keeps_binding_until_visible_final_text(tmp_path):
    store, skey, sid, dims = _setup(tmp_path)

    assert runtime.pre_tool_call(tool_name="weather_lookup", turn_id="t-silent", session_id=sid) is None
    assert trigger.is_ours("t-silent")
    assert runtime.transform_llm_output(response_text="天气查询完成，今天晴。", session_id=sid,
                                        turn_id="t-silent") is None
    state = pstore.read(store, skey, dims).state
    assert state.visible_count == 1
    assert state.visible_since_user == 1
    assert state.pending is None


class _HostSurface:
    """A tiny stand-in for the host's turn surface, so "dangling progress" is observable.

    The host shows a tool-progress entry when a tool runs, covers it with the final visible text,
    and suppresses the whole turn on the silence marker. ``dangling`` is the forbidden state: a
    progress entry was shown and the turn ended with neither a visible body nor a silence marker.
    """

    def __init__(self):
        self.progress = False
        self.visible = None
        self.silenced = False
        self.dangling = False

    def tool_ran(self):
        self.progress = True

    def finish(self, result, body):
        if result == trigger.SILENT:
            self.silenced = True
            self.progress = False
            return
        text = str(body or "").strip()
        if text:
            self.visible = text
            self.progress = False  # a visible final body covers the progress entry
        elif self.progress:
            self.dangling = True  # progress with no body and no marker: the undefined path


def _drive(host, sid, turn_id, tools, body):
    """Run one proactive turn through the plugin's two brakes, then settle the host surface."""
    for name in tools:
        host.tool_ran()
        runtime.pre_tool_call(tool_name=name, turn_id=turn_id, session_id=sid)
    result = runtime.transform_llm_output(response_text=body, session_id=sid, turn_id=turn_id)
    host.finish(result, body)
    return result


def test_tools_then_final_text_is_visible_and_covers_progress(tmp_path):
    store, skey, sid, dims = _setup(tmp_path)
    host = _HostSurface()

    result = _drive(host, sid, "t-silent", ["weather_lookup"], "查过了，今天多云。")

    assert result is None, "the final text must pass through, not be silenced"
    assert host.visible == "查过了，今天多云。"
    assert host.dangling is False, "a visible final body must cover the tool progress"
    assert pstore.read(store, skey, dims).state.visible_count == 1


def test_tools_without_final_text_takes_the_defined_silence_path(tmp_path):
    store, skey, sid, dims = _setup(tmp_path)
    host = _HostSurface()

    result = _drive(host, sid, "t-silent", ["weather_lookup"], "")

    assert result == trigger.SILENT, "tools + no body must end in a defined silence, not an empty turn"
    assert host.dangling is False, "never leave progress dangling with no body and no marker"
    state = pstore.read(store, skey, dims).state
    assert state.visible_count == 0, "an invisible round must not consume the proactive quota"
    assert state.pending is None


def test_no_tools_and_no_text_is_still_a_quiet_skip(tmp_path):
    store, skey, sid, dims = _setup(tmp_path)
    host = _HostSurface()

    result = _drive(host, sid, "t-silent", [], "")

    assert result is None, "nothing was shown, so there is nothing to suppress"
    assert host.dangling is False
    assert pstore.read(store, skey, dims).state.visible_count == 0



def test_silence_is_uncounted_but_still_holds_one_window(tmp_path):
    # Documented consequence, measured: an uncounted silence leaves the provisional deadline
    # (release + min_idle) in place, so the clock neither re-fires immediately nor counts the round.
    store, skey, sid, dims = _setup(tmp_path)
    session = [{"session_key": skey, "session_id": sid, "dims": dims}]

    assert runtime.transform_llm_output(response_text="[SILENT]", session_id=sid,
                                        turn_id="t-silent") == trigger.SILENT
    assert pstore.read(store, skey, dims).state.visible_count == 0

    def _rec(text, session_key=None):
        return True

    early = trigger.tick(store, session, DUE + 1, inject=_rec, rng=support.FixedRng(0.5))
    assert early[0][1].accepted is False, "the next tick cannot be inside the same window"
    assert early[0][1].reason == C.REASON_WINDOW_OPEN

    due = trigger.tick(store, session, DUE + 3600, inject=_rec, rng=support.FixedRng(0.5))
    assert due[0][1].accepted is True, "one full window later the clock may try again"


def test_prose_that_mentions_the_marker_is_still_delivered(tmp_path):
    # No over-blocking: only the host's exact-marker rule counts as silence. A real message that
    # merely mentions the marker must be delivered and counted like any other.
    store, skey, sid, dims = _setup(tmp_path)
    text = "顺带一提，[SILENT] 只是系统标记，不影响我们正常聊天。"

    result = runtime.transform_llm_output(response_text=text, session_id=sid, turn_id="t-silent")

    assert result is None, "prose mentioning the marker is a normal message, not a silence"
    state = pstore.read(store, skey, dims).state
    assert state.visible_count == 1
    assert state.visible_since_user == 1
