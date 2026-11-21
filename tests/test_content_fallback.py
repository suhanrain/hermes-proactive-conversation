"""The opener and topic materials a proactive round may be given.

A legitimately due wake must end in **one natural chat**, not in silence. A round with no citable
topic still opens, with an *opener* material: no candidate, no fingerprint, no old fact, and one
instruction — say something natural, do not claim to remember anything, and never answer with a
marker. A round with a real record gets the topic material, which cites the record it came from and
never offers the silence marker as an escape hatch.

What is real here: the plugin is the installed copy loaded by hermes' own loader (fixtures ``plugin``
/ ``plugin_runtime``), and the store is a real ``SessionStore``. The assembly is a fixture, because a
unit test has no data root to read.
"""

from __future__ import annotations

import logging

import p4_support as P
import pytest
import support
from proactive import content as _content
from proactive import contract as C

T0 = P.NOW
HOUR = P.HOUR


# ── the compact harness ────────────────────────────────────────────────────────────────────


class Harness:
    def __init__(self, store, rt, source=None, raw=None, start=T0):
        self.rt = rt
        self.store = store
        self.source = source or support.dm_source()
        self.clock = {"now": float(start)}
        rt.set_clock(lambda: self.clock["now"])
        rt.set_rng(support.FixedRng(0.0))
        self.entry = support.seed_session(store, self.source)
        self.dims = support.dims_for(store, self.entry, self.source)
        assert rt.enable_session(store, self.entry.session_key, self.dims,
                                 dict(raw or support.CONFIG_A)) is True

    @property
    def blob(self):
        return self.store.get_session_metadata(self.entry.session_key, C.META_KEY, None)

    @property
    def state(self):
        return (self.blob or {}).get("state", {})

    def wake(self):
        self.last_wake_event = support.make_wake(self.entry.session_key, self.entry.session_id,
                                                source=self.source)
        return support.invoke_hook("pre_gateway_dispatch", event=self.last_wake_event,
                                   gateway=None, session_store=self.store)

    def due(self):
        self.clock["now"] = float(self.state["deadline"] + 1)


@pytest.fixture
def rt(plugin_runtime):
    return plugin_runtime


@pytest.fixture
def hs(store, rt):
    h = Harness(store, rt)
    yield h
    rt.reset_seams()


# ── 1. the two materials: what a round may say ──────────────────────────────────────────────


def test_the_opener_material_never_offers_a_citation():
    """No candidate, no fingerprint, no record — the whole point is a chat that claims nothing."""
    material = _content.render_opener_material(None)
    assert material.startswith(_content.MATERIAL_OPENER), material
    assert "候选消息" not in material, "an opener has nothing to cite"
    assert "线索指纹" not in material, "an opener has no fingerprint"
    assert material.strip() != "", "an opener material must be a real instruction"


def test_the_opener_material_demands_one_chat_and_never_offers_the_marker():
    """「不管如何，LLM 必须发送一条聊天」is the instruction the model gets now."""
    material = _content.render_opener_material(None)
    assert "自然" in material, material
    assert C.SILENCE_MARKER in material, (
        "the material must *name* the marker in order to forbid it; what it may never do is offer it "
        "as an allowed answer")
    assert "不要回复" in material or "不得回复" in material, material
    assert "不得声称" in material or "不要声称" in material, (
        "an opener must not let the model pretend to remember something: " + material)
    assert _content.expression_violation(material) is None


def test_the_topic_material_no_longer_offers_the_silence_escape_hatch():
    """The material must tell the round to send something, never to answer the marker when stuck."""
    record = P.rec(C.SOURCE_SESSION, "t-1", "18 号交房租", at=T0 - HOUR)
    material = _content.render_material(_content.render_candidate(record), record.fingerprint(), record)
    assert "若无法使用这些线索" not in material, material
    assert "必须" in material, "a released round must be told to send something: " + material
    assert "候选消息" in material and record.fingerprint() in material, "the citation is unchanged"


@pytest.mark.parametrize("reason", [C.SILENT_NO_TOPIC, C.SILENT_SOURCE_DOWN, C.SILENT_HEALTH,
                                    C.SILENT_EXPRESSION, C.SILENT_INTERNAL])
def test_no_material_leaks_an_internal_code(reason):
    """The model is never handed the plugin's internal vocabulary — and never the reason either."""
    material = _content.render_opener_material(reason)
    for code in ("silent:", "off:", "opener:", "unavailable:"):
        assert code not in material, (code, material)
    if reason.startswith("silent:"):
        assert reason.split(":", 1)[1] not in material, (reason, material)


# ── 2. an assembly fault is still a refusal ─────────────────────────────────────────────────
def test_an_assembly_fault_still_refuses_the_round(hs, rt, caplog):
    """A missing window / a broken assembly is a configuration fault, not 「no topic」: still no round."""
    caplog.set_level(logging.INFO)
    # No ContentPolicy window at all: the assembler refuses to decide what is recent.
    rt.set_content(_content.Assembler((P.source(C.SOURCE_SESSION, []),)))
    hs.due()

    assert hs.wake() == [{"action": "skip", "reason": C.SILENT_POLICY}]
    assert hs.state["pending"] is None
    assert hs.state["visible_count"] == 0




# ── 3. the opener never cites what it must not mention ──────────────────────────────────────


def test_the_opener_material_forbids_citing_the_sessions_old_facts():
    """Nothing to cite means a chat that claims nothing — including the health kind."""
    material = _content.render_opener_material(C.SILENT_HEALTH)
    assert "旧" in material, material
    assert "健康" in material or "私人" in material, (
        "the opener must name the private facts it may not bring up: " + material)
    assert "不要提及" in material or "不得提及" in material, material
    assert "silent:" not in material and "health" not in material, material
    assert _content.expression_violation(material) is None
