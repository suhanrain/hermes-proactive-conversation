"""The expression boundary: pressure, blame and dependency-inducing wording.

The boundary is a decidable pattern table (``content.EXPRESSION_PATTERNS``), not a taste call. The
tests below are the two halves of that claim: counterexamples that MUST trip it, and ordinary warm
messages that MUST NOT (so the boundary cannot be satisfied by refusing to speak).

It is applied where it matters: to the candidate the assembly would hand on, and — for a round this
plugin released — to the text that would actually reach the user, at the delivery gate.
"""

from __future__ import annotations

import pytest

import p4_support as P
import support  # noqa: F401
from proactive import content
from proactive import contract as C

VIOLATIONS = [
    ("pressure", "你怎么都不理我"),
    ("pressure", "这是最后一次机会，别怪我"),
    ("pressure", "必须马上回我"),
    ("blame", "是不是把我忘了"),
    ("blame", "又把我晾在一边"),
    ("dependency", "只有我记得你的事"),
    ("dependency", "我会一直等你的"),
]


@pytest.mark.parametrize("kind,text", VIOLATIONS)
def test_the_boundary_trips_on_pressure_blame_and_dependency(kind, text):
    assert content.expression_violation(text) is not None
    assert content.inside_expression_boundary(text) is False


def test_every_pattern_id_is_named_and_unique():
    ids = [pattern_id for pattern_id, _ in content.EXPRESSION_PATTERNS]
    assert len(ids) == len(set(ids))
    assert {i.split(":")[0] for i in ids} == {"pressure", "blame", "dependency"}


SAFE = [
    "早上好，今天杭州有雨，出门带伞。",
    "你滴答清单里的「18 号交房租」还没勾掉，要我提醒你吗？",
    "上次你提到周五和老王吃饭，后来怎么样了？",
    "这周的训练节奏不错，保持就好。",
    "记得喝水，早点休息。",
    "",
    None,
]


@pytest.mark.parametrize("text", SAFE)
def test_ordinary_messages_stay_inside_the_boundary(text):
    assert content.expression_violation(text) is None
    assert content.inside_expression_boundary(text) is True


def test_a_record_whose_own_wording_pressures_is_dropped_not_softened():
    """No candidate is produced — the plugin never rewrites the user's own record into a claim."""
    coercive = P.source(C.SOURCE_MEMORY, [
        P.rec(C.SOURCE_MEMORY, "m1", "你怎么都不理我"),
    ])
    assembly = P.run((coercive,))
    assert assembly.silent and assembly.reason == C.SILENT_EXPRESSION
    assert assembly.audit["counts"]["expression"] == 1
    assert assembly.candidate == "" and assembly.material == ""
    assert "不理我" not in assembly.material


def test_a_clean_record_behind_a_coercive_one_is_still_usable():
    adapters = (
        P.source(C.SOURCE_MEMORY, [P.rec(C.SOURCE_MEMORY, "m1", "你怎么都不理我",
                                         at=P.NOW - 2 * P.HOUR)]),
        P.source(C.SOURCE_SESSION, [P.rec(C.SOURCE_SESSION, "t1", "周五和老王吃饭",
                                       at=P.NOW - 4 * P.HOUR)]),
    )
    assembly = P.run(adapters)
    assert assembly.ready
    assert "周五和老王吃饭" in assembly.candidate
    assert content.inside_expression_boundary(assembly.candidate) is True
    assert content.inside_expression_boundary(assembly.material.splitlines()[1]) is True


def test_every_rendered_candidate_is_inside_the_boundary_for_all_five_sources():
    for name in C.CONTENT_SOURCES:
        assembly = P.run((P.source(name, [P.rec(name, "1", "一件普通的小事")]),))
        assert assembly.ready, name
        assert content.inside_expression_boundary(assembly.candidate), (name, assembly.candidate)


def test_the_material_carries_the_constraints_the_turn_must_obey():
    assembly = P.run(P.all_topic_pair())
    assert assembly.ready
    lines = assembly.material.splitlines()
    assert lines[0].startswith("[proactive-conversation:assembled]")
    assert "不得编造其它旧话题" in assembly.material
    assert "不得引入健康数值" in assembly.material
    assert "[SILENT]" in assembly.material
    assert "只读" in assembly.material
