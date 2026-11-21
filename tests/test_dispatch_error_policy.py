# -*- coding: utf-8 -*-
"""The delivery hooks swallow exactly what they say they swallow.

Two host hooks run their own error policy, and both are **fail open**: an error of ours must never eat
something the user was going to get. `runtime.pre_gateway_dispatch` runs before every ordinary inbound
round, and `runtime.transform_llm_output` decides whether a released round's text is replaced by the
silence marker. The cases here cover:

1. the dispatch gate's fail-open answer, plus the log-hygiene half (the gate sees per-session data, so
   the exception's *body* may quote a session key and must never reach the log);
2. the delivery re-check's fail-open answer: an error delivers the text instead of silencing it;
3. the handlers' boundary: ``except Exception`` does not catch ``BaseException``, so a
   ``KeyboardInterrupt`` still propagates and the process is not made un-interruptible by either hook.

These cases pin the behaviour; they do not endorse the policy.
"""
from __future__ import annotations

import logging

import pytest

import support  # noqa: F401  (pins the throwaway HERMES_HOME before hermes loads)

from proactive import runtime

#: A marker that must not survive into the log: it is the shape of a real session key.
SECRET_MARKER = "agent:main:feishu:dm:oc_must_not_be_logged"


def test_an_error_in_the_gate_passes_the_round_through_and_hides_the_body(caplog, monkeypatch):
    def exploding_dispatch(*args, **kwargs):
        raise RuntimeError(f"the gate blew up while reading {SECRET_MARKER}")

    monkeypatch.setattr(runtime, "_dispatch", exploding_dispatch)

    with caplog.at_level(logging.WARNING):
        answer = runtime.pre_gateway_dispatch(event=object(), gateway=None, session_store=None)

    assert answer is None, "a gate fault must express no opinion, so the round proceeds"
    logged = caplog.text
    assert "passing the round through" in logged, logged
    assert "RuntimeError" in logged, f"the exception type is the useful part: {logged}"
    assert SECRET_MARKER not in logged, f"the exception body reached the log: {logged}"


def test_the_gate_does_not_swallow_a_base_exception(monkeypatch):
    def interrupting_dispatch(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "_dispatch", interrupting_dispatch)

    with pytest.raises(KeyboardInterrupt):
        runtime.pre_gateway_dispatch(event=object(), gateway=None, session_store=None)


def test_an_error_in_the_delivery_recheck_delivers_instead_of_silencing(caplog, monkeypatch):
    """The sibling fail-open: a fault in the re-check must not replace the text with the marker."""

    def exploding_recheck(*args, **kwargs):
        raise RuntimeError(f"the re-check blew up while reading {SECRET_MARKER}")

    monkeypatch.setitem(runtime._tickets, "synthetic-session",
                        {"released_at": 0.0, "store": None, "session_key": "", "dims": {}})
    monkeypatch.setattr(runtime, "_recheck", exploding_recheck)

    with caplog.at_level(logging.WARNING):
        answer = runtime.transform_llm_output(response_text="the answer", session_id="synthetic-session")

    assert answer is None, "None means 'no opinion', so the host delivers the text"
    logged = caplog.text
    assert "delivering" in logged, logged
    assert "RuntimeError" in logged, logged
    assert SECRET_MARKER not in logged, f"the exception body reached the log: {logged}"


def test_the_recheck_does_not_swallow_a_base_exception(monkeypatch):
    def interrupting_recheck(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setitem(runtime._tickets, "synthetic-session",
                        {"released_at": 0.0, "store": None, "session_key": "", "dims": {}})
    monkeypatch.setattr(runtime, "_recheck", interrupting_recheck)

    with pytest.raises(KeyboardInterrupt):
        runtime.transform_llm_output(response_text="the answer", session_id="synthetic-session")
