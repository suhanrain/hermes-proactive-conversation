"""A model-chosen ``[SILENT]`` ends a released round **without** spending the day.

The defect this file measures: the wake was released and filled with assembled material, the
model answered ``[SILENT]`` at 22:59:31, the platform read **zero** app messages back — and the
plugin's durable routing state said ``visible_count=1``. The gateway's own rule suppresses that marker
(``gateway/response_filters.py::is_intentional_silence_response``), so nothing reached the user, but
the plugin settled the round at its delivery seam anyway: the day's whole quota gone for a message
nobody received.

What is real here (and therefore what a green run means), exactly as in the rest of ``host_e2e``:

* the plugin is the **installed** copy, discovered and loaded by hermes' own loader;
* the round is a **real heartbeat poll** (``_heartbeat_poll_once`` → synthetic event →
  ``adapter.handle_message`` → the real ``pre_gateway_dispatch`` → the real turn against the scripted
  loopback provider → the real ``transform_llm_output`` seam → the gateway's real shaping and
  delivery decision);
* the session store is a real ``SessionStore`` and the counters read back from it are the durable
  ones, never the plugin's in-memory copy;
* the only fake is the **transport**: ``FakeFeishuAdapter.send`` records into ``delivery_attempts``.

What each scenario pins:

1. ``…ends_the_round_uncounted`` — the model's own marker (all the host-recognised precise forms,
   including edge whitespace and case) gives **0 transport calls, 0 settlements, ``visible_count=0``,
   ``visible_since_user=0``, ``pending=None``** while the round really ran (one turn, one model call). The
   per-case tables make the exemption a named rule rather than "anything that looks quiet";
2. ``…the_next_due_window_still_opens`` — the same session may take the *next* legitimately due round
   (nothing was counted, so no cap blocks it), which is what "a silent attempt must not burn the day"
   is worth in practice;
3. ``…does_not_bypass_dnd`` — the exemption is not a way around the quiet hours: after a silent
   round, a wake inside DND still opens nothing;
4. ``…a_reply_that_is_not_an_exact_marker_is_delivered_and_counted_once`` — the positive control: the
   same strong configuration, a real candidate reply → exactly one transport call, exactly one
   settlement, one count;
5. ``…without_the_p11_rule_costs_the_day_and_delivers_nothing`` — **the control that goes red**: with
   the rule switched off, the very same journey delivers nothing and still spends the day's quota.
   That divergence is the defect, reproduced end to end;
7. ``…a_user_reply_that_mentions_the_marker_is_delivered`` — the marker is only ever exempted on this
   plugin's own released round: a user's ordinary turn (or its answer) that merely *mentions*
   ``[SILENT]`` is delivered verbatim and counts nothing against the proactive quota.

Boundary, so no reader over-reads a green run: the transport is the suite's fake adapter, and what
the host does *after* the plugin's seam (its own delivery ledger, retries) is outside every
assertion here. ``wire == []`` means "this plugin handed the round to no transport", never "the
recipient can never see a duplicate" — see the boundary note at the bottom of
the write-ahead wiring tests.
"""

from __future__ import annotations

import asyncio
import importlib
import logging

import pytest

from host_e2e import harness as H

TOPIC = "周五和老王吃饭"
#: The candidate the assembly hands the model (an ordinary, recent, user-authored record).
PROACTIVE = "上次你提到「周五和老王吃饭」，后来怎么样了？"
#: The next round's material, used by the "another window may still open" case.
NEXT = "今天杭州降温了，出门记得加件外套"

#: Every precise form the host itself recognises (``gateway/response_filters.LIVE_GATEWAY_SILENT_MARKERS``
#: plus its own normalisation: whitespace, case, and edge punctuation other than brackets).
HOST_MARKER_FORMS = ["[SILENT]", "  [SILENT]\n", "silent", "no_reply", "*NO_REPLY*", "[静默]"]

#: The default is stricter than what this module measures: a model answer that is not a chat earns
#: **one extra generation** (``generation_retry_limit``, default 1). This module pins the narrower
#: rule — 「a released round the model chose to keep silent is settled zero times and costs no quota」
#: — so it switches the retry off in the plugin's real config file
#: (``plugins.entries.proactive-conversation.settings``, read through ``ctx.get_config`` on every hook).
#: With the knob at 0 the silent-marker behaviour is unchanged; **that** is the non-regression this
#: module exists for. The default mode is exercised by the host-level tests.
SILENCE_MODE = ("  entries:\n"
            "    proactive-conversation:\n"
            "      settings:\n"
            "        generation_retry_limit: 0\n")

#: A strong configuration: one hour of idle, two proactive messages a day, two visible-since-user.
CONFIG = dict(H.P2_CONFIG)


@pytest.fixture
def srv():
    H.ensure_agent_importable()
    from tests.fakes.fake_llm_provider import FakeLLMServer

    server = FakeLLMServer(default_text="好，我们周五再定")
    server.start()
    try:
        yield server
    finally:
        server.stop()


# ── helpers ─────────────────────────────────────────────────────────────────────────────────


def _host(tmp_path, srv, tag, config=CONFIG):
    host = H.IsolatedHost(tmp_path, srv, session_tag=tag, extra_config=SILENCE_MODE,
                          transcript=(("user", TOPIC), ("assistant", "嗯，周五再说")))
    host.start()
    assert host.in_process, host.host_module
    host.at("2027-01-15T12:00")
    host.enable_session(dict(config))
    return host


def _state(raw):
    state = (raw or {}).get("state") if isinstance(raw, dict) else {}
    state = state or {}
    return {key: state.get(key) for key in
            ("visible_since_user", "visible_count", "last_release_at", "deadline", "pending")}


def _guard_module(host):
    """The live ``delivery_guard`` module the released round's settlement would go through."""
    module = host.host_module
    assert module and module.endswith(".proactive.runtime"), module
    return importlib.import_module(module[: -len(".runtime")] + ".delivery_guard")


class settlements:
    """Count the guard's single settlement point for one host (restored on exit)."""

    def __init__(self, host):
        self.guard = _guard_module(host)
        self.real = self.guard.settle_release
        self.calls = []

    def __enter__(self):
        real = self.real
        calls = self.calls

        def counting(store_handle, route, read, *, now, rng, round_digest=""):
            calls.append(round_digest)
            return real(store_handle, route, read, now=now, rng=rng, round_digest=round_digest)

        self.guard.settle_release = counting
        return self

    def __exit__(self, *exc):
        self.guard.settle_release = self.real
        return False


# ── 1. the model's own silence ends the round uncounted ─────────────────────────────────────


@pytest.mark.parametrize("marker", HOST_MARKER_FORMS)
def test_a_model_silence_marker_ends_the_round_uncounted(tmp_path, srv, marker, caplog):
    """0 transport calls, 0 settlements, 0 counts — for every precise form the host honours."""
    from tests.fakes.fake_llm_provider import Text

    caplog.set_level(logging.INFO)
    srv.push(Text(marker, chunk_chars=1))
    host = _host(tmp_path, srv, "p11" + "".join(ch for ch in marker if ch.isalnum()))
    try:
        with settlements(host) as settled:
            host.advance(3601)  # from 12:00 to 13:00:01 local: the 1h window is due
            asyncio.run(host.poll())
            asyncio.run(host.settle())

        # the round really ran: this is not a "never started" pass
        assert len(host.turns()) == 1, "the released round must run a real turn"
        assert host.llm_calls()["main"] == 1

        # ... the user received nothing ...
        assert host.adapter.delivery_attempts == [], f"wire={host.wire!r}"
        assert host.wire == []

        # ... nothing was settled, and nothing was counted ...
        assert settled.calls == [], (
            f"a round the model kept silent must not be settled; settle_release ran "
            f"{len(settled.calls)} time(s): {settled.calls}")
        state = _state(host.raw_blob())
        assert state["visible_count"] == 0, state
        assert state["visible_since_user"] == 0, state
        assert state["pending"] is None, "the in-flight release must be retired, not left live"
        assert state["last_release_at"] == 0.0, "no round reached the user, so none is dated"

        # ... the round's durable row is the gateway's own silence decision ...
        assert any(content == "[SILENT]" for _, content in host.rows()), (
            f"the round's row must be the marker the gateway honourned, not the candidate: "
            f"{host.rows()}")

        # ... and the plugin says so, in one named reason code
        assert "silent:model-choice" in caplog.text, caplog.text
    finally:
        host.close()


def test_after_a_silent_round_the_next_due_window_still_opens(tmp_path, srv, caplog):
    """A silent attempt costs no quota, so the session's next due window can still open."""
    from tests.fakes.fake_llm_provider import Text

    caplog.set_level(logging.INFO)
    srv.push(Text("[SILENT]"), Text(NEXT))
    host = _host(tmp_path, srv, "p11next")
    try:
        with settlements(host) as settled:
            host.advance(3601)
            asyncio.run(host.poll())
            asyncio.run(host.settle())
            assert host.wire == [] and settled.calls == []

            host.advance(3601)  # the provisional deadline the release left behind (min_idle)
            asyncio.run(host.poll())
            asyncio.run(host.settle())

        assert host.wire == [NEXT], host.wire
        assert len(host.adapter.delivery_attempts) == 1
        assert len(host.turns()) == 2, "the second, non-silent round really ran"
        assert len(settled.calls) == 1, settled.calls
        state = _state(host.raw_blob())
        assert state["visible_count"] == 1 and state["visible_since_user"] == 1, state
        assert state["pending"] is None
    finally:
        host.close()


def test_a_silent_round_does_not_bypass_dnd(tmp_path, srv, caplog):
    """The exemption is not a route around the quiet hours."""
    from tests.fakes.fake_llm_provider import Text

    caplog.set_level(logging.INFO)
    srv.push(Text("[SILENT]"), Text(NEXT))
    host = _host(tmp_path, srv, "p11dnd")
    try:
        host.advance(3601)
        asyncio.run(host.poll())
        asyncio.run(host.settle())
        before = _state(host.raw_blob())
        assert host.wire == [] and before["pending"] is None

        host.at("2027-01-15T23:01")  # 23:00-09:00 quiet hours
        asyncio.run(host.poll())
        asyncio.run(host.settle())

        assert host.wire == [], "a wake inside DND must open nothing"
        assert len(host.adapter.delivery_attempts) == 0
        assert len(host.turns()) == 1, "the DND wake must not open a turn"
        assert _state(host.raw_blob())["visible_count"] == 0
        assert "reason=silent:dnd" in caplog.text or "reason=dnd" in caplog.text, caplog.text
    finally:
        host.close()


# ── 2. the positive control: a real reply is delivered and counted exactly once ──────────────


def test_a_reply_that_is_not_an_exact_marker_is_delivered_and_counted_once(tmp_path, srv):
    """The same strong configuration, a candidate answer: exactly one send, one settlement."""
    from tests.fakes.fake_llm_provider import Text

    srv.push(Text(PROACTIVE))
    host = _host(tmp_path, srv, "p11deliver")
    try:
        with settlements(host) as settled:
            host.advance(3601)
            asyncio.run(host.poll())
            asyncio.run(host.settle())

        assert host.wire == [PROACTIVE], host.wire
        assert len(host.adapter.delivery_attempts) == 1
        assert len(settled.calls) == 1, (
            f"a delivered round is settled exactly once: {settled.calls}")
        state = _state(host.raw_blob())
        assert state["visible_count"] == 1 and state["visible_since_user"] == 1, state
        assert state["pending"] is None
        assert state["last_release_at"] > 0
    finally:
        host.close()


# ── 3. the control: switch the rule off and the day is spent for nothing ─────────────────────


def test_control_without_the_p11_rule_costs_the_day_and_delivers_nothing(tmp_path, srv, caplog):
    """CONTROL — the behaviour without the marker rule, reproduced end to end.

    The plugin's own marker verdict is replaced by "never silence". Everything else stays real — the
    installed plugin, the real hooks, the real turn, the
    real store, the real gateway shaping. The measured outcome is the incident: the gateway delivers
    nothing, and the plugin has counted a message nobody received.
    """
    from tests.fakes.fake_llm_provider import Text

    caplog.set_level(logging.INFO)
    srv.push(Text("[SILENT]"))
    host = _host(tmp_path, srv, "p11control")
    runtime = host.runtime
    original = runtime._host_silence_verdict
    try:
        runtime._host_silence_verdict = lambda text: False
        with settlements(host) as settled:
            host.advance(3601)
            asyncio.run(host.poll())
            asyncio.run(host.settle())

        assert host.wire == [], "the control still delivers nothing: the host silences the marker"
        assert len(host.adapter.delivery_attempts) == 0
        assert len(settled.calls) == 1, (
            "without this card's rule the round is settled anyway — that is the defect")
        state = _state(host.raw_blob())
        assert state["visible_count"] == 1 and state["visible_since_user"] == 1, (
            "the day's quota is spent for a message nobody received: " + str(state))
        assert state["pending"] is None
    finally:
        runtime._host_silence_verdict = original
        host.close()


# ── 4. the exemption is scoped to this plugin's own rounds ───────────────────────────────────


def test_a_user_reply_that_mentions_the_marker_is_delivered(tmp_path, srv):
    """A user's own turn is never the plugin's round: its answer is delivered verbatim."""
    from tests.fakes.fake_llm_provider import Text

    replied = "你说 [SILENT] 是给模型的暗号，我记住了"
    srv.push(Text(replied))
    host = _host(tmp_path, srv, "p11user")
    try:
        asyncio.run(host.send_user_message("刚才那句 [SILENT] 是什么意思"))
        asyncio.run(host.settle())

        assert host.wire == [replied], host.wire
        assert len(host.adapter.delivery_attempts) == 1
        state = _state(host.raw_blob())
        assert state["visible_count"] == 0, "an ordinary conversation is not proactive traffic"
        assert state["pending"] is None
    finally:
        host.close()
