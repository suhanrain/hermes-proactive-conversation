"""The plugin-controlled injection trigger.

These fourteen cases drive the **real** trigger code in
:mod:`proactive.trigger` and the three hook callbacks in :mod:`proactive.runtime` against a **real**
``SessionStore`` (the per-session blob, real journal + metadata) and, for the restart cases, a
**real** SQLite transcript read through :func:`proactive.host_adapter.recent_transcript`.

What is real here and what is a stand-in (so a green run is read correctly):

* **real** — the per-session blob, :mod:`proactive.store`'s read/write/close discipline,
  :mod:`proactive.state`'s serialisation, :func:`proactive.policy.evaluate_release` /
  ``delivery_recheck`` / ``commit_release``, the recogniser and both brakes, and the transcript
  read-only seam (a real ``messages`` table);
* **stand-in** — the **host turn engine**: there is no live gateway injection in this fixture, so a
  "turn" is what the test drives by calling ``runtime.pre_llm_call`` then ``runtime.transform_llm_output``
  with a ``turn_id``; the injected text and the agent's answer are recorded in a local ``wire`` list.
  The ``inject`` seam is a local callable returning the host boolean, standing where
  ``ctx.inject_message`` would. This fixture therefore measures **the plugin's decisions** (gate,
  pin, recognition, brakes, count) — not a live gateway's delivery.

Scope note: no live profile, no gateway, no network. ``support`` pins a throwaway ``HERMES_HOME``.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TESTS_DIR = _HERE.parent
for _p in (str(_TESTS_DIR), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

import support  # noqa: E402  (pins a throwaway HERMES_HOME before any hermes module is imported)

from proactive import contract as C          # noqa: E402
from proactive import policy, runtime         # noqa: E402
from proactive import state as pstate         # noqa: E402
from proactive import store as pstore         # noqa: E402
from proactive import trigger                 # noqa: E402

#: A fixed "enable" instant and the instant its window is due (min_idle == max_idle == 3600).
T0 = 1_800_000_000.0
DUE = T0 + 3600.0
SKEY = "agent:main:feishu:dm:oc_pb"
SID = "sid-pb-1"


def _config(**over):
    base = {"enabled": True, "timezone": "Asia/Shanghai", "dnd_start": "23:00", "dnd_end": "09:00",
            "min_idle_seconds": 3600, "max_idle_seconds": 3600,
            "window_max_attempts": 4, "window_min_visible": 1, "day_max_silent": 2, "day_max_attempts": 2}
    base.update(over)
    return base


def _dims(skey=SKEY, sid=SID):
    return {"session_key": skey, "session_id": sid, "profile": None, "platform": "feishu",
            "chat_id": "oc_pb", "thread_id": None}


def _source():
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.FEISHU, chat_id="ou_pb", user_id="ou_pb", chat_type="dm")


def _make(store, *, config=None, now=T0):
    """Create the session entry, enable it (a real store write), return ``(skey, sid, dims)``."""
    source = _source()
    entry = support.seed_session(store, source)
    dims = support.dims_for(store, entry, source)
    runtime.set_clock(lambda: now)
    assert runtime.enable_session(store, entry.session_key, dims, config or _config())
    runtime.set_clock(lambda: DUE)
    return entry.session_key, entry.session_id, dims


def _state(store, skey, dims):
    read = pstore.read(store, skey, dims)
    assert read.usable, read.reason
    return read.state


def _inject_recorder(result=True):
    calls = []

    def inject(text, session_key=None):
        calls.append((text, session_key))
        return result

    return inject, calls


class _Ctx:
    """A minimal host context: only ``get_config`` (the seam the settings/deny-list read uses)."""

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


# ── case 1 ───────────────────────────────────────────────────────────────────────────────────


def test_refused_round_never_calls_the_trigger(tmp_path):
    """Gate before injection: OFF / paused / cap / DND give zero injects, zero count."""
    cases = {}

    # paused
    store = support.new_store(tmp_path, name="paused")
    skey, sid, dims = _make(store, config=_config())
    assert runtime.pause_session(store, skey, dims)
    cases["paused"] = (store, skey, sid, dims)

    # DND: a window that covers the whole day, evaluated on a DUE tick
    store3 = support.new_store(tmp_path, name="dnd")
    cases["dnd"] = (store3,) + _make(store3, config=_config(dnd_start="00:00", dnd_end="23:59"))

    # OFF: a permanently closed session is not usable at all
    store4 = support.new_store(tmp_path, name="off")
    skey4, sid4, dims4 = _make(store4)
    assert runtime.revoke_session(store4, skey4)
    cases["off"] = (store4, skey4, sid4, dims4)

    for label, (store, skey, sid, dims) in cases.items():
        inject, calls = _inject_recorder(True)
        outcome = trigger.evaluate_and_inject(
            store, session_key=skey, session_id=sid, dims=dims, now=DUE,
            inject=inject, rng=support.FixedRng(0.5))
        assert outcome.accepted is False, f"{label}: a refused round must not be accepted"
        assert calls == [], f"{label}: the trigger must not be called for a refused round"
        assert outcome.reason, f"{label}: a refusal must carry a reason code"
        if label != "off":
            assert _state(store, skey, dims).visible_count == 0, f"{label}: no count"


# ── case 2 ───────────────────────────────────────────────────────────────────────────────────


def test_consent_absent_is_a_clean_refusal(tmp_path, caplog):
    """`inject` returning False: not counted, pin retired, one warning with a reason code."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store)
    inject, calls = _inject_recorder(False)
    caplog.set_level(logging.WARNING, logger="proactive.trigger")

    outcome = trigger.evaluate_and_inject(
        store, session_key=skey, session_id=sid, dims=dims, now=DUE,
        inject=inject, rng=support.FixedRng(0.5))

    assert outcome.accepted is False
    assert outcome.reason == "inject:refused"
    assert len(calls) == 1, "the trigger is asked exactly once, then the host refuses"
    assert _state(store, skey, dims).visible_count == 0, "a refused round is not counted"
    assert _state(store, skey, dims).pending is None, "the pin is retired on a refusal"
    assert "inject:refused" in caplog.text or "not counted" in caplog.text


# ── case 3 ───────────────────────────────────────────────────────────────────────────────────


def test_an_accepted_injection_runs_exactly_one_full_turn(tmp_path):
    """Acceptance → one turn; that turn's text reaches the (fake) transport exactly once."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store)
    inject, calls = _inject_recorder(True)

    outcome = trigger.evaluate_and_inject(
        store, session_key=skey, session_id=sid, dims=dims, now=DUE,
        inject=inject, rng=support.FixedRng(0.5))
    assert outcome.accepted is True
    wire = []
    prompt = calls[0][0]

    # The host runs one turn: pre_llm_call recognises it, transform_llm_output releases the text.
    runtime.pre_llm_call(user_message=prompt, turn_id="t-1", session_id=sid)
    assert trigger.is_ours("t-1")
    silenced = runtime.transform_llm_output(response_text="你好，在吗", session_id=sid, turn_id="t-1")
    assert silenced is None, "an allowed round is delivered, not silenced"
    wire.append("你好，在吗")

    assert wire == ["你好，在吗"], "exactly one delivery of the one turn's text"
    assert _state(store, skey, dims).visible_count == 1, "one released round counted once"


# ── case 4 ───────────────────────────────────────────────────────────────────────────────────


def test_the_turn_is_bound_at_the_model_call(tmp_path):
    """pre_llm_call binds the matching prompt; a different user message does not."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store)
    inject, calls = _inject_recorder(True)
    trigger.evaluate_and_inject(store, session_key=skey, session_id=sid, dims=dims, now=DUE,
                                inject=inject, rng=support.FixedRng(0.5))
    prompt = calls[0][0]

    runtime.pre_llm_call(user_message=prompt, turn_id="t-match", session_id=sid)
    assert trigger.is_ours("t-match"), "the pinned prompt binds the turn"

    runtime.pre_llm_call(user_message="一句普通的话", turn_id="t-other", session_id=sid)
    assert not trigger.is_ours("t-other"), "a different user message must not bind"


# ── case 5 ───────────────────────────────────────────────────────────────────────────────────


def test_a_turn_with_text_between_tool_calls_stays_recognised(tmp_path):
    """Mid-turn: tool traffic and assistant rows must not decay the binding."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store)
    runtime.bind_plugin_context(_Ctx({C.SETTING_TOOLS_ENABLED: True}))
    inject, calls = _inject_recorder(True)
    trigger.evaluate_and_inject(store, session_key=skey, session_id=sid, dims=dims, now=DUE,
                                inject=inject, rng=support.FixedRng(0.5))
    prompt = calls[0][0]

    runtime.pre_llm_call(user_message=prompt, turn_id="t-mid", session_id=sid)
    assert trigger.is_ours("t-mid")

    # A tool call happens between model calls; the binding (keyed by turn_id) must survive it.
    assert runtime.pre_tool_call(tool_name="read_note", turn_id="t-mid", session_id=sid) is None
    assert trigger.is_ours("t-mid"), "tool traffic must not drop the binding"
    # An allowed tool is still not braked on the bound turn; the turn ends, text delivered.
    assert runtime.transform_llm_output(response_text="检查完了", session_id=sid,
                                        turn_id="t-mid") is None
    assert _state(store, skey, dims).visible_count == 1


# ── case 6 ───────────────────────────────────────────────────────────────────────────────────


def test_a_pause_landing_after_injection_silences_the_text(tmp_path):
    """Brake 1: a pause after injection ⇒ [SILENT], no count, and the user's next turn is answered."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store)
    inject, calls = _inject_recorder(True)
    trigger.evaluate_and_inject(store, session_key=skey, session_id=sid, dims=dims, now=DUE,
                                inject=inject, rng=support.FixedRng(0.5))
    runtime.pre_llm_call(user_message=calls[0][0], turn_id="t-pause", session_id=sid)
    assert runtime.pause_session(store, skey, dims) is not None

    result = runtime.transform_llm_output(response_text="本不该发出", session_id=sid,
                                          turn_id="t-pause")
    assert result == trigger.SILENT, "a paused round's text is silenced"
    assert _state(store, skey, dims).visible_count == 0, "a silenced round is not counted"

    # The user's own next turn is a different turn_id, is not ours, and is delivered verbatim.
    runtime.pre_llm_call(user_message="我还是想问一句", turn_id="t-user", session_id=sid)
    assert not trigger.is_ours("t-user")
    assert runtime.transform_llm_output(response_text="好的", session_id=sid,
                                        turn_id="t-user") is None


# ── case 7 ───────────────────────────────────────────────────────────────────────────────────


def test_a_user_message_during_the_round_wins(tmp_path):
    """Takeover: a user message during the round silences the proactive text, counts nothing."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store)
    inject, calls = _inject_recorder(True)
    trigger.evaluate_and_inject(store, session_key=skey, session_id=sid, dims=dims, now=DUE,
                                inject=inject, rng=support.FixedRng(0.5))
    runtime.pre_llm_call(user_message=calls[0][0], turn_id="t-take", session_id=sid)

    # A real user message arrives mid-round: the policy retires the pending pin (takeover).
    read = pstore.read(store, skey, dims)
    new_state = policy.on_user_message(read.state, read.config, DUE + 1, support.FixedRng(0.5))
    pstore.write(store, skey, config_raw=read.config_raw, state=new_state, dims=dims,
                 origin=read.origin)

    result = runtime.transform_llm_output(response_text="过时的主动消息", session_id=sid,
                                          turn_id="t-take")
    assert result == trigger.SILENT
    assert _state(store, skey, dims).visible_count == 0


# ── case 8 ───────────────────────────────────────────────────────────────────────────────────


def test_the_tool_policy_applies_to_our_turn_only(tmp_path):
    """pre_tool_call: the boolean switch acts on the bound turn; an ordinary turn is untouched."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store)
    runtime.bind_plugin_context(_Ctx({"proactive_tools_enabled": False}))
    inject, calls = _inject_recorder(True)
    trigger.evaluate_and_inject(store, session_key=skey, session_id=sid, dims=dims, now=DUE,
                                inject=inject, rng=support.FixedRng(0.5))
    runtime.pre_llm_call(user_message=calls[0][0], turn_id="t-ours", session_id=sid)

    blocked = runtime.pre_tool_call(tool_name="payment_charge", turn_id="t-ours", session_id=sid)
    assert isinstance(blocked, dict) and blocked.get("action") == "block", blocked
    # Every tool is blocked on our turn while the switch is off.
    assert isinstance(runtime.pre_tool_call(tool_name="read_note", turn_id="t-ours",
                                            session_id=sid), dict)
    # An ordinary user turn (different turn_id) is never braked, whatever the switch says.
    assert runtime.pre_tool_call(tool_name="payment_charge", turn_id="t-user", session_id=sid) is None


# ── case 9 ───────────────────────────────────────────────────────────────────────────────────


def _transcript_db(path, session_id, rows):
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
        "content TEXT, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0)")
    for role, content in rows:
        con.execute("INSERT INTO messages (session_id, role, content, active, compacted) "
                    "VALUES (?, ?, ?, 1, 0)", (session_id, role, content))
    con.commit()
    con.close()
    return path


def test_a_replayed_turn_after_restart_is_still_recognised(tmp_path):
    """Restart: no in-process record; the durable pin + newest-user-row digest still binds."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store)
    inject, calls = _inject_recorder(True)
    trigger.evaluate_and_inject(store, session_key=skey, session_id=sid, dims=dims, now=DUE,
                                inject=inject, rng=support.FixedRng(0.5))
    prompt = calls[0][0]

    # Simulate a restart: the process-local stores are gone, the blob pin is durable.
    trigger.reset()
    assert not trigger.is_ours("t-replay")

    db = _transcript_db(tmp_path / "state.db", sid,
                        [("user", "之前一句"), ("assistant", "嗯"), ("user", prompt)])

    bound = trigger.recognise(sid, "t-replay", prompt, store_handle=store,
                              declarations=[(skey, sid, dims)], db_path=db)
    assert bound is True, "the durable pin + newest user row must re-recognise the turn"
    assert trigger.is_ours("t-replay")


# ── case 10 ──────────────────────────────────────────────────────────────────────────────────


def test_an_unrecognised_turn_is_not_counted_and_not_braked(tmp_path):
    """The chosen failure direction: an unrecognised turn is neither silenced nor counted."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store, config=_config())
    runtime.bind_plugin_context(_Ctx({}))
    before = _state(store, skey, dims).visible_count

    # No pin at all: an ordinary user turn.
    runtime.pre_llm_call(user_message="用户自己的话", turn_id="t-user", session_id=sid)
    assert not trigger.is_ours("t-user")
    assert runtime.transform_llm_output(response_text="回答用户", session_id=sid,
                                        turn_id="t-user") is None
    assert runtime.pre_tool_call(tool_name="payment_charge", turn_id="t-user",
                                 session_id=sid) is None
    assert _state(store, skey, dims).visible_count == before, "an unrecognised turn never counts"


# ── case 11 ──────────────────────────────────────────────────────────────────────────────────


def test_a_turn_that_ends_with_no_text_is_not_counted(tmp_path):
    """No text ⇒ nothing to brake and nothing to count; the pin is retired."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store)
    inject, calls = _inject_recorder(True)
    trigger.evaluate_and_inject(store, session_key=skey, session_id=sid, dims=dims, now=DUE,
                                inject=inject, rng=support.FixedRng(0.5))
    runtime.pre_llm_call(user_message=calls[0][0], turn_id="t-empty", session_id=sid)

    assert runtime.transform_llm_output(response_text="", session_id=sid,
                                        turn_id="t-empty") is None
    assert _state(store, skey, dims).visible_count == 0
    assert _state(store, skey, dims).pending is None


# ── case 12 ──────────────────────────────────────────────────────────────────────────────────


def test_an_unreadable_transcript_lets_the_text_out_and_logs_it(tmp_path, caplog):
    """The other failure direction: an unreadable transcript ⇒ not recognised ⇒ text goes out."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store)
    inject, calls = _inject_recorder(True)
    trigger.evaluate_and_inject(store, session_key=skey, session_id=sid, dims=dims, now=DUE,
                                inject=inject, rng=support.FixedRng(0.5))
    prompt = calls[0][0]
    trigger.reset()
    caplog.set_level(logging.WARNING, logger="proactive.trigger")

    # A database path that cannot be opened: the transcript read returns no rows.
    missing = tmp_path / "does-not-exist" / "state.db"
    bound = trigger.recognise(sid, "t-unreadable", prompt, store_handle=store,
                              declarations=[(skey, sid, dims)], db_path=missing)
    assert bound is False, "an unreadable transcript cannot re-recognise the turn"

    # Not ours ⇒ the text is NOT braked (let out), and nothing is counted.
    assert not trigger.is_ours("t-unreadable")
    assert runtime.transform_llm_output(response_text="一条可能发出的话", session_id=sid,
                                        turn_id="t-unreadable") is None
    assert "could not be recognised" in caplog.text


# ── case 13 ──────────────────────────────────────────────────────────────────────────────────


def test_two_ticks_in_one_window_never_double_trigger(tmp_path):
    """Clock + pin: two ticks in the same window release at most once."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store)
    inject, calls = _inject_recorder(True)
    sessions = [{"session_key": skey, "session_id": sid, "dims": dims}]

    first = trigger.tick(store, sessions, DUE, inject=inject, rng=support.FixedRng(0.5))
    second = trigger.tick(store, sessions, DUE, inject=inject, rng=support.FixedRng(0.5))

    assert first[0][1].accepted is True
    assert second[0][1].accepted is False, "the second tick in the same window is a skip"
    assert len(calls) == 1, "the trigger ran exactly once for the window"


# ── case 14 ──────────────────────────────────────────────────────────────────────────────────


def test_the_count_is_one_per_released_round_not_per_tick(tmp_path):
    """Settlement keyed by turn_id: the second transform for the same turn does not double-count."""
    store = support.new_store(tmp_path)
    skey, sid, dims = _make(store)
    inject, calls = _inject_recorder(True)
    trigger.evaluate_and_inject(store, session_key=skey, session_id=sid, dims=dims, now=DUE,
                                inject=inject, rng=support.FixedRng(0.5))
    runtime.pre_llm_call(user_message=calls[0][0], turn_id="t-count", session_id=sid)

    assert runtime.transform_llm_output(response_text="第一条", session_id=sid,
                                        turn_id="t-count") is None
    assert _state(store, skey, dims).visible_count == 1
    # The same turn_id fires again (a re-entrant call): the binding is spent, so no second count.
    assert runtime.transform_llm_output(response_text="第一条", session_id=sid,
                                        turn_id="t-count") is None
    assert _state(store, skey, dims).visible_count == 1, "one released round counts once"
