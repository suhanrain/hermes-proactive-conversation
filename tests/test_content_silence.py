"""The six fake-data cases (a)–(f), and the 2A fingerprint's machine-checkability.

(a) a reliable topic, (b) no topic at all, (c) contradicting / expired records, (d) a read-only
source that times out, (e) sensitive health material, (f) a memory that is not about this user.

(b)–(f) must be **silent**: no candidate sentence, no material, nothing sendable — and no invented
topic. (a) must be ready, its candidate must quote the record it came from, and its fingerprint must
resolve back to that exact record in the sources it was read from.
"""

from __future__ import annotations

import p4_support as P
import support  # noqa: F401
from proactive import content
from proactive import contract as C


def outcomes(adapters, sc=None):
    """The source outcomes one registry read produces for the same scope (for fingerprint checks)."""
    registry = content.SourceRegistry(sc if sc is not None else P.scope())
    registry.register_all(adapters)
    return tuple(registry.read(name) for name in C.CONTENT_SOURCES)


# ── (a) a reliable topic ────────────────────────────────────────────────────────────────────


def test_case_a_a_recent_record_about_the_user_is_a_usable_topic():
    assembly = P.run(P.all_topic_pair())
    assert assembly.ready and assembly.status == C.ASSEMBLY_READY
    assert assembly.reason == C.ASSEMBLY_READY
    assert "18 号交房租" in assembly.candidate, "the newest record is the topic"
    assert assembly.topic is not None and assembly.topic.record_id == "t-2"
    assert assembly.silent is False


# ── (b) no topic ────────────────────────────────────────────────────────────────────────────


def test_case_b_an_empty_day_has_nothing_to_say():
    assembly = P.run((P.source(C.SOURCE_SESSION, []), P.source(C.SOURCE_MEMORY, [])))
    assert assembly.silent and assembly.reason == C.SILENT_NO_TOPIC
    assert _nothing_sendable(assembly)


def test_case_b_with_no_sources_composed_at_all_there_is_no_topic():
    assembly = P.run(())
    assert assembly.silent and assembly.reason == C.SILENT_SOURCE_DOWN
    assert _nothing_sendable(assembly)


# ── (c) contradicting / expired records ─────────────────────────────────────────────────────


def test_case_c_contradicting_records_are_dropped_rather_than_picked_from():
    """Two records about the same thing that disagree: neither side may be asserted."""
    conflict = P.source(C.SOURCE_MEMORY, [
        P.rec(C.SOURCE_MEMORY, "m1", "下周去北京出差", topic_key="trip", value="peking"),
        P.rec(C.SOURCE_MEMORY, "m2", "下周去上海出差", topic_key="trip", value="shanghai"),
    ])
    assembly = P.run((conflict,))
    assert assembly.silent and assembly.reason == C.SILENT_CONFLICT
    assert assembly.audit["counts"]["conflict"] == 2
    assert _nothing_sendable(assembly)
    assert "北京" not in assembly.material and "上海" not in assembly.material


def test_case_c_expired_records_are_not_topics():
    old = P.source(C.SOURCE_SESSION, [
        P.rec(C.SOURCE_SESSION, "t-old", "三个月前说过的事", at=P.NOW - 90 * 24 * P.HOUR),
    ])
    assembly = P.run((old,))
    assert assembly.silent and assembly.reason == C.SILENT_STALE
    assert assembly.audit["counts"]["stale"] == 1
    assert _nothing_sendable(assembly)
    assert "三个月前" not in assembly.material


def test_case_c_the_window_is_the_callers_not_a_hardcoded_one():
    """Same record, two windows: the boundary is supplied by the caller, never baked in."""
    record = P.source(C.SOURCE_SESSION, [
        P.rec(C.SOURCE_SESSION, "t1", "上周的事", at=P.NOW - 6 * 24 * P.HOUR)])
    assert P.run((record,), window=48 * P.HOUR).silent is True
    assert P.run((record,), window=14 * 24 * P.HOUR).ready is True

    # ... and with no window supplied the assembly refuses to decide what "recent" means.
    bare = P.run((record,), window=None)
    assert bare.silent and bare.reason == C.SILENT_POLICY
    assert bare.audit["counts"] == {}


def test_case_c_a_contradiction_does_not_poison_an_unrelated_clean_topic():
    mixed = (
        P.source(C.SOURCE_MEMORY, [
            P.rec(C.SOURCE_MEMORY, "m1", "下周去北京", topic_key="trip", value="a"),
            P.rec(C.SOURCE_MEMORY, "m2", "下周去上海", topic_key="trip", value="b"),
        ]),
        P.source(C.SOURCE_SESSION, [P.rec(C.SOURCE_SESSION, "t1", "周五和老王吃饭")]),
    )
    assembly = P.run(mixed)
    assert assembly.ready
    assert "周五和老王吃饭" in assembly.candidate
    assert "北京" not in assembly.candidate and "上海" not in assembly.candidate


# ── (d) a read-only source times out ────────────────────────────────────────────────────────


def test_case_d_a_timeout_leaves_nothing_to_say():
    assembly = P.run((P.source(C.SOURCE_SESSION, [], failure="unavailable:timeout"),
                      P.source(C.SOURCE_MEMORY, [], failure="unavailable:read-failed")))
    assert assembly.silent and assembly.reason == C.SILENT_SOURCE_DOWN
    assert _nothing_sendable(assembly)


def test_case_d_a_timeout_never_yields_a_message_about_the_downed_source():
    """No template exists for a source that has no record: nothing can be written about it."""
    assembly = P.run((P.source(C.SOURCE_MEMORY, [], failure="unavailable:timeout"),))
    assert assembly.silent
    assert assembly.material == "" and assembly.candidate == ""
    assert "记录里" not in assembly.material and "训练" not in assembly.material


# ── (e) sensitive health material ───────────────────────────────────────────────────────────


def test_case_e_health_material_defaults_to_silence():
    """The single switch is off by default (PM ruling): health values are not citable."""
    health = P.source(C.SOURCE_MEMORY, [
        P.rec(C.SOURCE_MEMORY, "g1", "HRV 28ms，昨晚睡了 4 小时 50 分", sensitive=True),
    ])
    assembly = P.run((health,))
    assert assembly.silent and assembly.reason == C.SILENT_HEALTH
    assert assembly.audit["counts"]["health"] == 1
    assert _nothing_sendable(assembly)
    assert "28" not in assembly.material and "4 小时" not in assembly.material


def test_case_e_health_material_is_always_excluded_there_is_no_switch():
    """No switch exists: a sensitive record is dropped from the citable set unconditionally.

    The old build had a single health switch; removing it must not loosen privacy,
    so this pins the strongest direction: there is no configuration, no argument, and no source that
    makes a health value citable.
    """
    health = P.source(C.SOURCE_MEMORY, [
        P.rec(C.SOURCE_MEMORY, "g1", "昨晚睡了 4 小时 50 分", sensitive=True)])
    assembly = P.run((health,))
    assert assembly.silent and assembly.reason == C.SILENT_HEALTH
    assert assembly.audit["counts"]["health"] == 1
    assert _nothing_sendable(assembly)
    assert "4 小时" not in assembly.material


def test_case_e_health_material_does_not_hide_a_normal_topic():
    mixed = (
        P.source(C.SOURCE_MEMORY, [
            P.rec(C.SOURCE_MEMORY, "g1", "HRV 28ms", sensitive=True)]),
        P.source(C.SOURCE_SESSION, [P.rec(C.SOURCE_SESSION, "t1", "周五和老王吃饭")]),
    )
    assembly = P.run(mixed)
    assert assembly.ready and "周五和老王吃饭" in assembly.candidate
    assert "HRV" not in assembly.candidate and "28ms" not in assembly.candidate


# ── (f) a memory that is not about this user ────────────────────────────────────────────────


def test_case_f_an_unrelated_memory_is_not_a_topic():
    unrelated = P.source(C.SOURCE_MEMORY, [
        P.rec(C.SOURCE_MEMORY, "m1", "同事说他家猫生病了", subject="other"),
    ])
    assembly = P.run((unrelated,))
    assert assembly.silent and assembly.reason == C.SILENT_UNRELATED
    assert assembly.audit["counts"]["unrelated"] == 1
    assert _nothing_sendable(assembly)
    assert "猫" not in assembly.material


def test_case_f_an_undated_record_is_not_recent():
    undated = P.source(C.SOURCE_MEMORY, [P.rec(C.SOURCE_MEMORY, "m1", "不知道什么时候说的话", at=None)])
    assembly = P.run((undated,))
    assert assembly.silent and assembly.reason == C.SILENT_STALE


# ── the fingerprint is real and resolvable ──────────────────────────────────────────────────


def test_the_candidate_carries_a_fingerprint_that_hits_the_original_record():
    adapters = P.all_topic_pair()
    assembly = P.run(adapters)
    assert assembly.ready
    found = content.resolve_fingerprint(outcomes(adapters), assembly.fingerprint)
    assert found is not None, f"{assembly.fingerprint} does not resolve to any read record"
    assert found.record_id == assembly.topic.record_id
    assert found.text in assembly.candidate
    assert assembly.fingerprint in assembly.material, "the turn material must carry the provenance"


def test_the_fingerprint_is_the_record_not_a_similar_one():
    adapters = (P.source(C.SOURCE_SESSION, [
        P.rec(C.SOURCE_SESSION, "t1", "周五和老王吃饭", at=P.NOW - 3 * P.HOUR),
        P.rec(C.SOURCE_SESSION, "t2", "周日交房租", at=P.NOW - 1 * P.HOUR),
    ]),)
    assembly = P.run(adapters)
    found = content.resolve_fingerprint(outcomes(adapters), assembly.fingerprint)
    assert found is not None and found.record_id == "t2"
    assert assembly.fingerprint.startswith(f"{C.SOURCE_SESSION}:t2:")


def test_a_fingerprint_cannot_be_forged_or_resolved_against_an_unavailable_source():
    adapters = (P.source(C.SOURCE_SESSION, [P.rec(C.SOURCE_SESSION, "t1", "周五和老王吃饭")]),)
    assembly = P.run(adapters)
    assert content.resolve_fingerprint(outcomes(adapters), "session-transcript:t-ghost:deadbeef") is None
    assert content.resolve_fingerprint(outcomes(adapters), assembly.fingerprint[:-1] + "0") is None

    down = (P.source(C.SOURCE_SESSION, [], failure="unavailable:timeout"),)
    assert content.resolve_fingerprint(outcomes(down), assembly.fingerprint) is None
    assert content.resolve_fingerprint((), "session-transcript:t1:abc") is None


def test_no_candidate_means_no_fingerprint_at_all():
    """Silence has nothing to point at: no candidate, no fingerprint, no material."""
    assembly = P.run(())
    assert assembly.silent
    assert assembly.fingerprint == "" and assembly.candidate == "" and assembly.material == ""


def _nothing_sendable(assembly) -> bool:
    """The precise meaning of 静默: no sentence, no material, no topic, no fingerprint."""
    return (assembly.candidate == "" and assembly.material == ""
            and assembly.topic is None and assembly.fingerprint == "")
