"""A **failed** round: what the marker exempts, and what the host still sends.

The failure ordering here is *measured* rather than argued from two predicates: "can a round reach this plugin's last seam with the model's
exact ``[SILENT]`` and still end as a failed turn — and if so, what do the platform and the durable
counters show?"

Measured answer, on the same real host path as the rest of ``host_e2e`` (installed plugin, real
heartbeat poll, real turn against the scripted loopback provider, real ``transform_llm_output``
dispatch, real gateway shaping/delivery, real ``SessionStore``; only the transport is the fake
adapter):

* **the seam carries no failure bit.** The real dispatch hands ``transform_llm_output`` only
  ``response_text`` / ``session_id`` / ``model`` / ``platform`` / ``turn_id`` (recorded from the live
  callback in ``test_a_failure_stamped_after_the_marker_is_a_host_disclosure_and_stays_uncounted``),
  and it runs *inside* the turn, before the gateway classifies the result. No plugin can decide
  "this round failed" there;
* **a failure stamped after the text** — the gateway's own shape: ``_run_agent_timeout_result``
  (``gateway/run_turn.py``) substitutes a synthetic ``failed=True`` result at this same seam when it
  gives up on an idle turn — leaves the exemption standing (0 settlements, ``visible_count=0``,
  ``visible_since_user=0``, ``pending=None``), and the gateway then does **not** swallow the marker:
  ``is_intentional_silence_agent_result`` requires ``failed=False``, so it appends its failed-turn
  notice and sends it once. The round's visible cost is one *host failure disclosure*, not one
  proactive message, and the next legitimate window can still open;
* **a hard agent failure** (a real provider 401) never reaches the seam at all: the error path
  returns before finalisation, so the plugin neither exempts nor settles — 0 counts — and the same
  single host failure disclosure is delivered;
* **the control**: the same failure stamp on a round whose text is *not* the marker is delivered
  **and** settled once (``visible_count=1``). The exemption is the marker, never "the round failed".

The bound this pins, stated so no reader over-reads it: after a failure the plugin under-counts by at
most the host's own failure disclosure (one message), and it never swallows a message the host
delivered (the marker is exempted only on this plugin's own released round, only where the host
honours that lane, and only in the host's own exact marker forms). What the plugin cannot do is
*count* that disclosure as a proactive round — ``failed`` is not knowable at any seam it has. The
reverse case (a platform failure **after** the write-ahead settlement can still cost the day) is
out of scope here.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from host_e2e import harness as H
from host_e2e.test_model_silence_no_count import (NEXT, SILENCE_MODE, PROACTIVE, TOPIC,
                                                     _state, settlements)

#: The gateway's own failed-turn boundary copy (``agent/turn_failure_copy.py``), imported rather than
#: retyped: the assertion is "the host sent its disclosure", not "the test knows the English string".
#: Loaded lazily like every other host import in this package (``harness.ensure_agent_importable``),
#: so this module still *collects* under an interpreter that has no Hermes on its path.
def _failed_turn_notice() -> str:
    H.ensure_agent_importable()
    from agent.turn_failure_copy import FAILED_TURN_NOTICE

    return FAILED_TURN_NOTICE


#: The one strong configuration this module uses (1h idle, 2 a day, 2 visible-since-user).
CONFIG = dict(H.P2_CONFIG)

#: Production's timeout shape at the agent seam (``_run_agent_timeout_result``): a synthetic failed
#: result whose ``final_response`` is the gateway's own diagnostic, not the model's text. Built from
#: the host's i18n keys rather than retyped, so the test never asserts against invented copy.
def _timeout_diag() -> str:
    H.ensure_agent_importable()
    from agent.i18n import t
    from agent.session_activity import format_iteration_progress

    return "\n".join([
        t("gateway.progress.timeout_header", minutes=10),
        t("gateway.progress.timeout_last_activity", activity="idle", seconds="600",
          progress=format_iteration_progress(0, 0)),
        t("gateway.progress.timeout_hint"),
    ])


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


def _host(tmp_path, srv, tag, *, result_patch=None):
    """One isolated host whose seam is the production one, with an optional failure stamp."""
    host = H.IsolatedHost(tmp_path, srv, session_tag=tag, result_patch=result_patch,
                          extra_config=SILENCE_MODE,
                          transcript=(("user", TOPIC), ("assistant", "嗯，周五再说")))
    host.start()
    assert host.in_process, host.host_module
    host.at("2027-01-15T12:00")
    host.enable_session(dict(CONFIG))
    return host


class seam_calls:
    """Record exactly what the real dispatch hands the plugin's ``transform_llm_output`` callback."""

    HOOK = "transform_llm_output"

    def __init__(self):
        self.calls = []
        self._callback = H.plugin_callback(self.HOOK)
        assert self._callback is not None, "the installed plugin registered no transform seam"
        # The dispatcher exposes no observation point, so the manager's live hook list is wrapped
        # in place (and restored on exit). If the host changes that structure, this fails loudly at
        # construction rather than silently recording nothing.
        import hermes_cli.plugins as plugins_mod

        self._hooks = plugins_mod.get_plugin_manager()._hooks[self.HOOK]
        self._original = list(self._hooks)

    def __enter__(self):
        callback, calls = self._callback, self.calls

        def recording(*args, **kwargs):
            calls.append({"args": args, "kwargs": dict(kwargs)})
            return callback(*args, **kwargs)

        self._hooks[:] = [recording if cb is callback else cb for cb in self._original]
        return self

    def __exit__(self, *exc):
        self._hooks[:] = self._original
        return False


def _failure_stamp(*, final_response=None, times=1):
    """A post-turn failure verdict at the gateway's agent seam (``result_patch``).

    ``times`` makes the stamp one-shot, so a later round in the same host can be an ordinary round
    (the fault-injection seam is the gateway's, and the gateway stops failing when the fault ends).
    """
    remaining = [int(times)]

    def patch(result):
        if remaining[0] <= 0:
            return result
        remaining[0] -= 1
        # Key-for-key what production hands the gateway on an inactivity timeout
        # (``_run_agent_timeout_result``): a ``failed`` verdict, and — when the gateway owns the text
        # — a replaced ``final_response``. No invented ``error``/``completed`` keys.
        result["failed"] = True
        if final_response is not None:
            result["final_response"] = final_response
        return result

    return patch


# ── 1. failure stamped AFTER the marker: the host's disclosure, not a counted round ─────────


@pytest.mark.parametrize("shape", ["marker-kept", "gateway-timeout-shape"])
def test_a_failure_stamped_after_the_marker_is_a_host_disclosure_and_stays_uncounted(
        tmp_path, srv, caplog, shape):
    """Real marker turn → the gateway fails the round → 1 host disclosure, 0 counted rounds.

    The two shapes are the two ways the gateway can own the verdict after the text exists: keeping
    the agent's own final response (``marker-kept``) and substituting its timeout diagnostic
    (``gateway-timeout-shape``, what ``_run_agent_timeout_result`` really returns). In both, the
    plugin's exemption was already decided *inside* the turn and cannot be un-decided afterwards.
    """
    from tests.fakes.fake_llm_provider import Text

    final_response = None if shape == "marker-kept" else _timeout_diag()
    caplog.set_level(logging.INFO)
    notice = _failed_turn_notice()
    srv.push(Text("[SILENT]"))
    host = _host(tmp_path, srv, "p11fail" + ("tmo" if final_response else "marker"),
                 result_patch=_failure_stamp(final_response=final_response))
    try:
        with settlements(host) as settled, seam_calls() as seam:
            host.advance(3601)  # 12:00 → 13:00:01: the 1h window is due
            asyncio.run(host.poll())
            asyncio.run(host.settle())

        # the round really ran, and the plugin's last seam really saw the model's marker …
        assert len(host.turns()) == 1
        assert host.llm_calls()["main"] == 1
        assert len(seam.calls) == 1, "the transform seam must really have run for this round"
        assert seam.calls[0]["kwargs"]["response_text"] == "[SILENT]"
        assert "silent:model-choice" in caplog.text, caplog.text

        # … and the seam was handed no failure information at all: this is why the plugin cannot
        # decide the failure verdict itself (the recorded payload IS the production dispatch). The
        # dispatch's only extra key is the host's telemetry tag; no verdict field is sent.
        payload = seam.calls[0]["kwargs"]
        assert {"response_text", "session_id", "model", "platform", "turn_id"} <= set(payload)
        assert not (set(payload) & {"failed", "agent_result", "interrupted", "partial", "error"}), (
            "the transform seam must carry no failure bit: " + repr(payload))

        # the gateway did NOT swallow the marker on a failed turn: it sent its own disclosure once.
        assert len(host.adapter.delivery_attempts) == 1, host.adapter.delivery_attempts
        delivered = host.wire[0]
        assert notice in delivered, (
            "a failed turn must carry the host's own failure disclosure: " + delivered)
        assert delivered != "[SILENT]", "the bare marker must NOT be what the user receives"
        assert PROACTIVE not in delivered, (
            "no proactive candidate content reaches the user on a failed round: " + delivered)

        # the plugin counted nothing and left nothing in flight — the round cost no quota.
        assert settled.calls == [], f"a silent round must never be settled: {settled.calls}"
        state = _state(host.raw_blob())
        assert state["visible_count"] == 0 and state["visible_since_user"] == 0, state
        assert state["pending"] is None, "the exemption must retire the in-flight release"
        assert state["last_release_at"] == 0.0, "no round reached the user, so none is dated"

        # the durable row is the text the seam produced (the marker), not the disclosure text.
        assert ("assistant", "[SILENT]") in host.rows(), host.rows()
    finally:
        host.close()


def test_after_a_failed_silent_round_the_next_window_can_still_open(tmp_path, srv):
    """A failure after the marker must not burn the day: the next due window still opens."""
    from tests.fakes.fake_llm_provider import Text

    srv.push(Text("[SILENT]"), Text(NEXT))
    host = _host(tmp_path, srv, "p11failnext",
                 result_patch=_failure_stamp(final_response=_timeout_diag()))
    try:
        with settlements(host) as settled:
            host.advance(3601)
            asyncio.run(host.poll())
            asyncio.run(host.settle())
            assert len(host.adapter.delivery_attempts) == 1  # the host's disclosure
            assert settled.calls == [] and _state(host.raw_blob())["visible_count"] == 0

            # The failed round cleared its own release, so the window re-opens after min_idle —
            # a failure is retryable, which is the whole point of not counting it.
            host.advance(3601)
            asyncio.run(host.poll())
            asyncio.run(host.settle())

        assert host.wire[-1] == NEXT, host.wire
        assert len(settled.calls) == 1, settled.calls
        state = _state(host.raw_blob())
        assert state["visible_count"] == 1 and state["visible_since_user"] == 1, state
        assert state["pending"] is None
    finally:
        host.close()


# ── 2. a hard agent failure never reaches the seam, and is not counted either ─────────────────


def test_a_hard_agent_failure_never_reaches_the_seam_and_costs_nothing(tmp_path, srv, caplog):
    """A real provider 401: the turn dies before finalisation, so this plugin sees nothing.

    This is the other half of the failure verdict measured on the real path: an agent-core failure does
    not hand the seam a marker to exempt (nor anything to settle) — it returns before the transform.
    What the user receives is again the host's own failure disclosure.
    """
    from tests.fakes.fake_llm_provider import Error

    caplog.set_level(logging.INFO)
    notice = _failed_turn_notice()
    srv.push(Error(status=401, message="invalid api key"))
    host = _host(tmp_path, srv, "p11failhard")
    try:
        with settlements(host) as settled, seam_calls() as seam:
            host.advance(3601)
            asyncio.run(host.poll())
            asyncio.run(host.settle())

        assert len(host.turns()) == 1
        assert seam.calls == [], (
            "a failed turn that never finalises must not reach the plugin's transform seam")
        assert "silent:model-choice" not in caplog.text, (
            "the plugin cannot have exempted a round it never saw: " + caplog.text)
        assert len(host.adapter.delivery_attempts) == 1, host.adapter.delivery_attempts
        assert notice in host.wire[0], host.wire[0]
        assert PROACTIVE not in host.wire[0]
        assert settled.calls == [], settled.calls
        state = _state(host.raw_blob())
        assert state["visible_count"] == 0 and state["visible_since_user"] == 0, state
        # The release marker is NOT retired here: nothing this plugin owns runs on a turn that never
        # reached the seam, so it lapses through the existing release TTL, moving no counter either way.
        assert state["pending"] is not None and state["pending"]["released_at"] > 0, state
    finally:
        host.close()


# ── 3. the control: the exemption is the marker, never "the round failed" ─────────────────────


def test_the_same_failure_on_a_non_marker_round_is_delivered_and_counted_once(tmp_path, srv):
    """Same failure stamp, a normal candidate reply: delivered once, settled once, counted once."""
    from tests.fakes.fake_llm_provider import Text

    notice = _failed_turn_notice()
    srv.push(Text(PROACTIVE))
    host = _host(tmp_path, srv, "p11failctl", result_patch=_failure_stamp())
    try:
        with settlements(host) as settled:
            host.advance(3601)
            asyncio.run(host.poll())
            asyncio.run(host.settle())

        assert len(host.adapter.delivery_attempts) == 1
        assert PROACTIVE in host.wire[0] and notice in host.wire[0], host.wire[0]
        assert len(settled.calls) == 1, (
            f"without the marker the round is settled exactly once: {settled.calls}")
        state = _state(host.raw_blob())
        assert state["visible_count"] == 1 and state["visible_since_user"] == 1, state
        assert state["pending"] is None
    finally:
        host.close()
