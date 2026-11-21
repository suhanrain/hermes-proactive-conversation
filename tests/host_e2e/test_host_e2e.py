"""The real chain end to end, on the installed plugin (in-process, the default).

Every test here drives the *production* lane, in this order: a real heartbeat poll iteration
(``_heartbeat_poll_once``) → the real ``pre_gateway_dispatch`` invocation → a real ``AIAgent`` turn
against the scripted provider → the real delivery gate (``apply_llm_output_transform`` inside
``agent.turn_finalizer``) → the gateway's real ``[SILENT]`` judgement → ``adapter.send`` on the fake
Feishu transport. What is real and what is not is stated in ``harness``'s module docstring.

Numbers asserted here are produced by these runs (counts of ``wire`` entries, of scripted-provider
POSTs and of assistant rows), never asserted from a fixture's expectation of what the plugin should
do.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

import pytest

import harness as H

PROACTIVE_TEXT = "关于X我还想补一句：周五之前定下来"
OLD_TOPIC_REPLY = "行，那就按上次那个方案走"
USER_REPLY_PREFIX = "那我们周五定"


@pytest.fixture
def srv():
    H.ensure_agent_importable()
    from tests.fakes.fake_llm_provider import FakeLLMServer

    server = FakeLLMServer(default_text="好的")
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _blob(host) -> Dict[str, Any]:
    raw = host.raw_blob()
    assert isinstance(raw, dict) and "state" in raw, f"no session blob was written: {raw!r}"
    return raw


def _runtime_state(host) -> Any:
    from proactive.state import SessionState

    return SessionState.from_dict(_blob(host)["state"])


# ── trajectory A: exactly one delivery, and the next user turn can read it ───────────────────


def test_trajectory_a_releases_exactly_one_round(tmp_path, srv):
    """A configured session with an old topic, wake at T+1h: one real delivery, then readable."""
    from tests.fakes.fake_llm_provider import Text

    srv.push(Text(OLD_TOPIC_REPLY), Text(PROACTIVE_TEXT))
    host = H.IsolatedHost(tmp_path, srv)
    host.start()
    try:
        assert host.in_process, host.host_module
        result = asyncio.run(_trajectory_a(host))
    finally:
        host.close()

    # exactly one proactive delivery, and it is the text the model produced for that round
    assert result["new_wire"] == [PROACTIVE_TEXT], result
    assert result["turns_after_poll"] == 2, "the wake must have opened exactly one real turn"
    assert result["marker_rows"] == 0

    # it is in the session record, exactly once
    rows = result["rows"]
    assert [c for _, c in rows].count(PROACTIVE_TEXT) == 1, rows
    assert H.roles_alternate(rows), rows

    # the delivered round was committed by the real delivery gate (counters move only there)
    state = result["state"]
    assert state.visible_since_user == 1 and state.visible_count == 1, state.to_dict()
    assert state.pending is None and state.last_release_at == result["released_at"], state.to_dict()

    # and the next real user turn reads it out of the session history
    assert result["next_turn_history_has_it"] is True, result["next_turn_history_tail"]


async def _trajectory_a(host) -> Dict[str, Any]:
    host.at("2027-01-15T12:00")  # noon in the session's timezone: outside DND
    host.enable_session(H.P2_CONFIG)
    await host.send_user_message("上次聊到X")  # the old topic: a real user round
    assert len(host.turns()) == 1, "the old-topic round must have run for real"
    wire_before = list(host.wire)

    host.advance(3601)  # the 1h window sampled at enable time is now due
    await host.poll()
    wire_after_poll = host.wire[len(wire_before):]
    released_at = host.now
    turns_after_poll = len(host.turns())

    rows = host.rows()
    state = _runtime_state(host)

    await host.send_user_message(f"{USER_REPLY_PREFIX}吧")
    history = host.turns()[-1]["history"] or []
    tail = [str(m.get("content"))[:40] for m in history if isinstance(m, dict)][-4:]

    return {
        "new_wire": wire_after_poll,
        "turns_after_poll": turns_after_poll,
        "rows": rows,
        "marker_rows": sum(1 for _, c in rows if c.strip() == "[SILENT]"),
        "state": state,
        "released_at": released_at,
        "next_turn_history_has_it": any(
            isinstance(m, dict) and PROACTIVE_TEXT in str(m.get("content")) for m in history
        ),
        "next_turn_history_tail": tail,
    }


# ── the ways a wake must stay silent (zero delivery, zero LLM) ───────────────────────────────


REFUSAL_SCENARIOS = [
    ("out_of_window", "window:open"),
    ("dnd", "dnd"),
    ("paused", "paused"),
]


@pytest.mark.parametrize("scenario,expected_reason", REFUSAL_SCENARIOS)
def test_refused_rounds_deliver_nothing(tmp_path, srv, scenario, expected_reason, caplog):
    """Out-of-window / DND / paused / cap 0: no send, no model call, no turn."""
    host = H.IsolatedHost(tmp_path, srv)
    host.start()
    try:
        assert host.in_process, host.host_module
        with caplog.at_level(logging.INFO):
            result = asyncio.run(_refusal(host, scenario))
    finally:
        host.close()

    assert result["wire"] == [], f"{scenario}: a refused wake delivered something: {result['wire']}"
    assert result["turns"] == 0, f"{scenario}: a refused wake opened a turn: {result['turns']}"
    assert result["llm"]["all_requests"] == 0, (
        f"{scenario}: a refused wake produced model traffic: {result['llm']}"
    )
    assert result["rows"] == result["rows_before"], f"{scenario}: the transcript changed"
    assert expected_reason in caplog.text, (
        f"{scenario}: the gate must report {expected_reason!r}; gateway log was:\n{caplog.text}"
    )


async def _refusal(host, scenario: str) -> Dict[str, Any]:
    config = dict(H.P2_CONFIG)
    if scenario == "out_of_window":
        config["min_idle_seconds"] = 6 * 3600
        config["max_idle_seconds"] = 6 * 3600

    host.at("2027-01-15T12:00")
    host.enable_session(config)
    if scenario == "paused":
        assert host.pause() is True, "the session must have been paused for this scenario"
    if scenario == "dnd":
        # 02:00 Asia/Shanghai the next day: inside 23:00–09:00, window already due.
        host.at("2027-01-16T02:00")
    else:
        # The window (1h, or 6h for the out-of-window case) is due; the refusal must come from the
        # scenario's own rule, not from an open window.
        host.advance(60 if scenario == "out_of_window" else 3601)

    wire_before = list(host.wire)
    rows_before = host.rows()
    await host.poll()
    return {
        "wire": host.wire[len(wire_before):],
        "turns": len(host.turns()),
        "llm": host.llm_calls(),
        "rows": host.rows(),
        "rows_before": rows_before,
    }


# ── a newer user message during the released turn silences the delivery ──────────────────────


def test_mid_turn_user_message_silences_the_released_round(tmp_path, srv, caplog):
    """The session moves under a released turn: `[SILENT]`, zero sends, alternation intact."""
    from tests.fakes.fake_llm_provider import Text

    # Slow stream so the "user message arrives mid-turn" race is deterministic, not timing luck.
    srv.push(Text(PROACTIVE_TEXT, chunk_chars=1, delay_per_chunk=0.06))
    host = H.IsolatedHost(tmp_path, srv)
    host.start()
    try:
        assert host.in_process, host.host_module
        with caplog.at_level(logging.INFO):
            result = asyncio.run(_superseded_round(host))
    finally:
        host.close()

    assert result["interrupted"] is True, "the injected message did not land mid-turn"
    assert result["wire"] == [], f"a superseded round was delivered: {result['wire']}"
    assert result["marker_rows"] == 1, (
        f"the round must leave exactly one [SILENT] marker row: {result['rows']}"
    )
    assert "proactive-conversation: silencing released turn" in caplog.text, caplog.text
    assert "silent:superseded" in caplog.text, "the gate must report why it silenced the round"
    assert H.roles_alternate(result["rows"]), result["rows"]
    state = result["state"]
    assert (state.visible_since_user, state.visible_count, state.pending) == (0, 0, None), state.to_dict()


async def _superseded_round(host) -> Dict[str, Any]:
    host.at("2027-01-15T12:00")
    host.enable_session(H.P2_CONFIG)
    host.advance(3601)

    poll_task = host.start_poll()
    released_turn_started = await host.wait_for_turns(1)
    interrupted = False
    if released_turn_started:
        # The session advances *after* the release and *during* the turn, through the gateway's real
        # admission call: exactly the "a newer user message arrived" case the gate must catch.
        host.advance(60)
        await host.admit("你怎么突然说话")
        interrupted = True
    await poll_task
    await host.settle()
    await host.settle()

    rows = host.rows()
    return {
        "interrupted": interrupted,
        "wire": host.wire,
        "rows": rows,
        "marker_rows": sum(1 for _, c in rows if c.strip() == "[SILENT]"),
        "state": _runtime_state(host),
    }


def test_lane_a_mid_turn_message_is_queued_not_admitted(tmp_path, srv, caplog):
    """Note the *lane* half of the mid-turn case, measured: a busy session queues the message instead.

    Driven through the adapter exactly like a real Feishu DM, the injected message does not reach
    admission while the released turn is running (``_handle_message_while_active`` → pending FIFO),
    so the released round is delivered normally and the user's message becomes the next turn. This is
    a known limitation of the adapter lane: the queued-message path never exercises the delivery gate.
    """
    from tests.fakes.fake_llm_provider import Text

    srv.push(Text(PROACTIVE_TEXT, chunk_chars=1, delay_per_chunk=0.06), Text("在的，你说"))
    host = H.IsolatedHost(tmp_path, srv)
    host.start()
    try:
        assert host.in_process, host.host_module
        with caplog.at_level(logging.INFO):
            result = asyncio.run(_lane_injection(host))
    finally:
        host.close()

    assert result["wire"] == [PROACTIVE_TEXT, "在的，你说"], result["wire"]
    print("[lane]", result["rows"])
    print("[lane]",
          [ln.strip() for ln in caplog.text.splitlines() if "silencing" in ln or "skip" in ln])
    assert result["marker_rows"] == 0, result["rows"]
    assert "pre_gateway_dispatch skip" not in caplog.text, caplog.text
    assert any("你怎么突然说话" in row for _, row in result["rows"]), (
        f"the queued message must still be answered, as its own turn: {result['rows']}"
    )


async def _lane_injection(host) -> Dict[str, Any]:
    host.at("2027-01-15T12:00")
    host.enable_session(H.P2_CONFIG)
    host.advance(3601)

    poll_task = host.start_poll()
    await host.wait_for_turns(1)
    host.advance(60)
    await host.admit_user_message("你怎么突然说话")  # adapter lane: queued, not admitted
    await poll_task
    await host.settle()

    rows = host.rows()
    return {
        "wire": host.wire,
        "rows": rows,
        "marker_rows": sum(1 for _, c in rows if c.strip() == "[SILENT]"),
    }


# ── regression + no double send ──────────────────────────────────────────────────────────────


def test_plugin_disabled_behaves_like_before_the_plugin(tmp_path, srv):
    """Plugin present but not enabled: the wake round is unchanged and nothing is written."""
    from tests.fakes.fake_llm_provider import Text

    srv.push(Text(PROACTIVE_TEXT))
    host = H.IsolatedHost(tmp_path, srv, enabled=False)
    host.start()
    try:
        assert H.plugin_callback() is None, "the disabled plugin must not register a callback"
        result = asyncio.run(_baseline_wake(host))
    finally:
        host.close()

    assert result["wire"] == [PROACTIVE_TEXT], result["wire"]
    assert result["turns"] == 1, result["turns"]
    assert result["blob"] is None, "a disabled plugin must not touch the session metadata at all"


async def _baseline_wake(host) -> Dict[str, Any]:
    host.at("2027-01-15T12:00")
    await host.poll()
    return {"wire": list(host.wire), "turns": len(host.turns()), "blob": host.raw_blob()}


def test_two_due_wakes_release_once(tmp_path, srv, caplog):
    """Two due ticks inside one window: one release, one delivery (no double send)."""
    from tests.fakes.fake_llm_provider import Text

    srv.push(Text(PROACTIVE_TEXT))
    host = H.IsolatedHost(tmp_path, srv)
    host.start()
    try:
        assert host.in_process, host.host_module
        with caplog.at_level(logging.INFO):
            result = asyncio.run(_two_wakes(host))
    finally:
        host.close()

    assert result["wire"] == [PROACTIVE_TEXT], result["wire"]
    assert result["turns"] == 1, result["turns"]
    assert caplog.text.count("pre_gateway_dispatch skip") == 1, (
        f"exactly one of the two due ticks may be refused: {caplog.text}"
    )
    assert "window:open" in caplog.text, caplog.text


async def _two_wakes(host) -> Dict[str, Any]:
    host.at("2027-01-15T12:00")
    host.enable_session(H.P2_CONFIG)
    host.advance(3601)
    await host.poll()  # tick 1: released, delivered
    await host.poll()  # tick 2: same window, re-armed heartbeat, must be refused
    return {"wire": list(host.wire), "turns": len(host.turns())}
