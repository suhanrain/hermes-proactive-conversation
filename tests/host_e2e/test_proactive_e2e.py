"""Installed plugin → real heartbeat/inbound/turn → isolated Feishu transport.

Only the LLM server, clock, content source and transport are injected. Every send count
is taken from BasePlatformAdapter.send, not from an expected plugin directive.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from host_e2e import harness as H

TOPIC = "周五和老王吃饭"
PROACTIVE = "上次你提到「周五和老王吃饭」，后来怎么样了？"
#: The block a wake is opened with when the assembly found nothing to cite.
OPENER_MARK = "[proactive-conversation:opener]"


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


def _topic(host, *, available=True):
    """Opt in to the installed runtime's assembler, backed by fictional local records."""
    content = host.runtime._content
    records = (content.Record(source="session-transcript", record_id="seed-1",
                              at=host.now - 60, text=TOPIC),) if available else ()
    source = content.StaticSource("session-transcript", records)
    host.runtime.set_content(content.Assembler(
        (source,), content.ContentPolicy(recent_record_seconds=7200)))
    return source


def _host(tmp_path, srv, tag):
    host = H.IsolatedHost(tmp_path, srv, session_tag=tag,
                          transcript=(("user", TOPIC), ("assistant", "嗯，周五再说")))
    host.start()
    assert host.in_process, host.host_module
    host.at("2027-01-15T12:00")
    host.enable_session(H.P2_CONFIG)
    return host


def _counts(host, label, *, initial=0):
    attempts = len(host.adapter.delivery_attempts) - initial
    count = len(host.wire) - initial
    print(f"[P5 {label}] adapter_calls={attempts} wire={count} llm={host.llm_calls()}")
    return attempts, count






@pytest.mark.parametrize("scenario", ["window", "dnd", "paused", "other_dm"])
def test_refusals_call_adapter_zero_times(tmp_path, srv, scenario):
    host = _host(tmp_path, srv, "refusal" + scenario)
    try:
        _topic(host)
        if scenario == "other_dm":
            # The other DM has a different session entry, no session-specific grant.
            other = H.feishu_dm("ou_other_p5")
            entry = H.seed_transcript(host.store, host.db, other, [("user", "你好")])
            H.seed_heartbeat(entry.session_id, "看看X")
            asyncio.run(host.runner._heartbeat_poll_once(
                {entry.session_key: (other, entry.session_id)}))
            asyncio.run(host.settle())
        else:
            if scenario == "paused":
                assert host.pause()
            if scenario == "dnd":
                host.at("2027-01-16T02:00")
            else:
                host.advance(60 if scenario == "window" else 3601)
            asyncio.run(host.poll())
        calls, wire = _counts(host, scenario)
        assert (calls, wire) == (0, 0)
        assert host.llm_calls()["all_requests"] == 0
    finally:
        host.close()
