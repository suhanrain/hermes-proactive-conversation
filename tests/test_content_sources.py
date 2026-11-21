"""The source whitelist, and a failing source never degrades into an invention.

The claim under test is *not* "the prompt says so". It is that the assembly path has no way to
reach anything but the five whitelisted sources: a non-whitelisted name has no entry in the
registry (``denied:unknown-source``), another session/profile cannot be addressed
(``denied:scope-mismatch``), an adapter that would use the network or write is refused **at
registration** (``denied:network-source`` / ``denied:write-source``), and in every one of those
cases the adapter's own call counter stays 0 because the adapter was never called.

Sources are injected adapters over fictional records (no real API is touched).
"""

from __future__ import annotations

import pytest

import p4_support as P
import support  # noqa: F401
from proactive import content
from proactive import contract as C


# ── the whitelist is the whole surface ──────────────────────────────────────────────────────


def test_the_whitelist_is_exactly_the_two_approved_sources():
    assert C.CONTENT_SOURCES == ("session-transcript", "cross-session-memory")
    registry = content.SourceRegistry(P.scope())
    for name in C.CONTENT_SOURCES:
        assert registry.register(P.source(name)) == ""
    assert registry.registered() == C.CONTENT_SOURCES


@pytest.mark.parametrize("name", ["other-chat-raw", "web", "external-feed", "profile-other",
                                 "session-transcript-2", ""])
def test_a_non_whitelisted_source_cannot_be_registered(name):
    """A name outside the table is refused by the registry itself: there is nothing to call."""
    registry = content.SourceRegistry(P.scope())
    adapter = P.source(name, [P.rec(name, "x1", "别处的聊天原文")])
    assert registry.register(adapter) == C.DENIED_UNKNOWN_SOURCE
    assert registry.refused() == {name: C.DENIED_UNKNOWN_SOURCE}
    assert registry.registered() == ()
    assert adapter.calls == 0, "the refused adapter was never called"


def test_reading_a_non_whitelisted_source_is_denied_without_calling_anything():
    registry = content.SourceRegistry(P.scope())
    outcome = registry.read("other-chat-raw")
    assert (outcome.status, outcome.reason) == (C.SOURCE_STATUS_DENIED, C.DENIED_UNKNOWN_SOURCE)
    assert outcome.records == ()
    # ... and the whole assembly stays silent rather than looking for a substitute:
    assert P.run(()).silent is True


def test_a_network_source_is_refused_at_registration():
    """The assembly path offers no egress: an adapter that would fetch the web never registers."""
    registry = content.SourceRegistry(P.scope())
    adapter = P.source(C.SOURCE_MEMORY, [P.rec(C.SOURCE_MEMORY, "m1", "网上的说法")], network=True)
    assert registry.register(adapter) == C.DENIED_NETWORK_SOURCE
    assert registry.registered() == ()
    assert adapter.calls == 0


def test_a_write_capable_source_is_refused_at_registration():
    """A source that could write is not a source: it is refused, and never read."""
    class Writable:
        name = C.SOURCE_MEMORY
        read_only = False
        network = False

        def __init__(self) -> None:
            self.calls = 0

        def read(self, scope):  # pragma: no cover - must never be reached
            self.calls += 1
            return ()

    writable = Writable()
    registry = content.SourceRegistry(P.scope())
    assert registry.register(writable) == C.DENIED_WRITE_SOURCE
    assert registry.registered() == ()
    assert writable.calls == 0

    # A duck-typed adapter carrying a write method is refused for the same reason.
    class Writer:
        name = C.SOURCE_SESSION
        read_only = True
        network = False
        calls = 0

        def read(self, scope):  # pragma: no cover - must never be reached
            Writer.calls += 1
            return ()

        def write(self, *a, **kw):  # pragma: no cover - must never be reached
            raise AssertionError("a write path must not exist on the assembly surface")

    assert registry.register(Writer()) == C.DENIED_WRITE_SOURCE
    assert Writer.calls == 0


def test_another_session_or_profile_is_unreachable_from_this_scope():
    """The scope is bound at construction: a read aimed elsewhere is refused before the adapter."""
    mine = P.scope(session_key="telegram:dm:42", session_id="sess-1", profile="dev")
    registry = content.SourceRegistry(mine)
    adapter = P.source(C.SOURCE_SESSION, [P.rec(C.SOURCE_SESSION, "s1", "别的会话里说的话")])
    assert registry.register(adapter) == ""

    for other in (P.scope(session_id="sess-2"), P.scope(profile="product-manager"),
                  P.scope(session_key="telegram:dm:99"), P.scope(chat_id="-100")):
        outcome = registry.read(C.SOURCE_SESSION, scope=other)
        assert (outcome.status, outcome.reason) == (C.SOURCE_STATUS_DENIED, C.DENIED_SCOPE)
    assert adapter.calls == 0, "no cross-session read ever reached the adapter"

    # The registry's own scope still works, so the refusal is about the scope, not about the source.
    ok = registry.read(C.SOURCE_SESSION)
    assert ok.ok and adapter.calls == 1


def test_the_assembly_runs_with_a_scope_of_its_own_so_other_sessions_are_not_in_reach():
    """Two sessions assembled side by side stay separate: neither can produce the other's record."""
    left = P.source(C.SOURCE_SESSION, [P.rec(C.SOURCE_SESSION, "s-left", "左边会话的事")])
    right = P.source(C.SOURCE_SESSION, [P.rec(C.SOURCE_SESSION, "s-right", "右边会话的事")])
    a = P.run((left,), sc=P.scope(session_key="k-left", session_id="i-left"))
    b = P.run((right,), sc=P.scope(session_key="k-right", session_id="i-right"))
    assert a.ready and b.ready
    assert "左边会话的事" in a.candidate and "右边会话的事" not in a.candidate
    assert "右边会话的事" in b.candidate and "左边会话的事" not in b.candidate


def test_every_whitelisted_source_is_readable_and_can_supply_the_topic():
    """Positive half: each whitelisted source, on its own, is readable and citable."""
    for name in C.CONTENT_SOURCES:
        adapter = P.source(name, [P.rec(name, f"{name}-1", "一件真实的小事")])
        assembly = P.run((adapter,))
        assert assembly.ready, f"{name} should be a usable source: {assembly.reason}"
        assert assembly.audit["topic"]["source"] == name
        assert [o[0] for o in assembly.sources] == list(C.CONTENT_SOURCES)
        assert adapter.calls == 1


def test_sources_that_were_not_composed_are_reported_unavailable_not_invented():
    adapter = P.source(C.SOURCE_SESSION, [P.rec(C.SOURCE_SESSION, "s1", "交房租")])
    assembly = P.run((adapter,))
    by_name_ = {name: (status, reason, n) for name, status, reason, n in assembly.sources}
    assert by_name_[C.SOURCE_SESSION] == (C.SOURCE_STATUS_OK, "", 1)
    assert by_name_[C.SOURCE_MEMORY] == (C.SOURCE_STATUS_UNAVAILABLE,
                                         C.UNAVAILABLE_NOT_COMPOSED, 0)
    assert assembly.ready and assembly.audit["topic"]["source"] == C.SOURCE_SESSION


# ── a failing source does not become a weaker claim ─────────────────────────────────────────


def test_a_403_source_is_unavailable_and_its_records_do_not_exist():
    """The failing source contributes nothing; the assembly is silent, not approximate."""
    down = P.source(C.SOURCE_SESSION, [], failure="unavailable:http-403")
    assembly = P.run((down,))
    assert assembly.silent
    assert assembly.reason == C.SILENT_SOURCE_DOWN
    assert by_name(assembly)[C.SOURCE_SESSION] == (
        C.SOURCE_STATUS_UNAVAILABLE, "unavailable:http-403", 0)
    assert assembly.candidate == "" and assembly.material == "" and assembly.topic is None


def test_a_timeout_on_one_source_does_not_promote_another_source_into_a_claim():
    """A weaker source is not promoted to fill the gap: silence, with no invented detail."""
    timeout = P.source(C.SOURCE_MEMORY, [], failure="unavailable:timeout")
    thin = P.source(C.SOURCE_SESSION, [])  # readable, but has nothing to say
    assembly = P.run((timeout, thin))
    assert assembly.silent and assembly.reason == C.SILENT_NO_TOPIC
    assert assembly.audit["counts"]["records"] == 0
    assert "老王" not in assembly.material and assembly.material == ""


def test_a_failing_source_does_not_change_a_topic_that_another_source_can_support():
    timeout = P.source(C.SOURCE_SESSION, [], failure="unavailable:timeout")
    clean = P.source(C.SOURCE_MEMORY, [P.rec(C.SOURCE_MEMORY, "t1", "周五和老王吃饭")])
    assembly = P.run((timeout, clean))
    assert assembly.ready
    assert assembly.audit["topic"]["source"] == C.SOURCE_MEMORY
    assert "周五和老王吃饭" in assembly.candidate
    assert "训练" not in assembly.candidate


def test_an_adapter_that_raises_is_unavailable_rather_than_a_broken_run():
    class Exploding:
        name = C.SOURCE_SESSION
        read_only = True
        network = False

        def read(self, scope):
            raise RuntimeError("driver blew up")

    assembly = P.run((Exploding(),))
    assert assembly.silent and assembly.reason == C.SILENT_SOURCE_DOWN
    assert by_name(assembly)[C.SOURCE_SESSION] == (
        C.SOURCE_STATUS_UNAVAILABLE, C.UNAVAILABLE_READ_FAILED, 0)


def by_name(assembly) -> dict:
    return {name: (status, reason, n) for name, status, reason, n in assembly.sources}
