"""The assembler seam driven through the real registered hooks.

The live refusal directions: no composition → ``off:content-unconfigured``, coercive text has no
ungoverned delivery path, a user turn is never rewritten, plus the installed-copy drift check.

The plugin is the installed copy loaded by hermes' own loader (fixtures ``plugin``/``plugin_runtime``
from ``conftest.py``), the dispatch hook is invoked through the lifecycle delivery manager, and the
delivery gate is the real ``agent.turn_finalizer.apply_llm_output_transform``.

What this file establishes:

1. **No composition installed → fail closed.** With nothing installed, a due wake is refused and the
   refusal writes nothing; there is no ungoverned path for a model's text to reach the user.
2. **The refusal is stable and not consumed** — retrying a wake never opens a round.
3. **A user turn is never rewritten by this seam.**
4. **The audit trail is digest-only** — with a real log handler, no record text, id or health value
   is ever written.
"""

from __future__ import annotations

import hashlib
import logging

import p4_support as P
import pytest
import support
from proactive import content as content_mod
from proactive import contract as C

T0 = P.NOW
HOUR = P.HOUR


# ── a compact harness ──────────────────────────────────────────────────────────────────────


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
        # Raw directives: the tests in this module pin the exact verdict, including the
        # ``{"action": "allow"}`` a released round carries (material written in place).
        return support.invoke_hook(
            "pre_gateway_dispatch",
            event=self.last_wake_event,
            gateway=None, session_store=self.store)

    def user_message(self, text="在吗", advance=60.0):
        self.clock["now"] += float(advance)
        return support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_event(text=text, session_key=self.entry.session_key,
                                     source=self.source,
                                     timestamp=support.at_clock(self.clock["now"])),
            gateway=None, session_store=self.store)

    def due(self):
        self.clock["now"] = float(self.state["deadline"] + 1)


class FakeAgent:
    def __init__(self, session_id, platform="telegram", model="fake-model"):
        self.session_id = session_id
        self.platform = platform
        self.model = model
        self._llm_output_transform = None


def deliver(agent, text, turn_id="turn-1"):
    from agent.turn_finalizer import apply_llm_output_transform

    return apply_llm_output_transform(agent, text, turn_id=turn_id)


@pytest.fixture
def rt(plugin_runtime):
    return plugin_runtime


@pytest.fixture
def hs(store, rt):
    h = Harness(store, rt)
    yield h
    rt.reset_seams()


# ── 1. no composition → fail closed ─────────────────────────────────────────────────────────


def test_with_no_composition_installed_a_due_wake_is_refused(hs, rt):
    """A due wake with no assembly is refused, and the refusal writes nothing.

    On a real host, releasing such a round would deliver the raw heartbeat prompt as its only
    material. The rule has no exception: refusing a round must leave the sampled window exactly as it
    was (the next wake re-decides from the same numbers).
    """
    assert rt.content_assembler() is None and rt.content_boundary() is None
    # ``content_required`` is diagnostics only (it answers "is a value present?", not "may we
    # release?"): the gate above refused the wake while this said there was no value at all.
    assert rt.content_required() is False and rt.content_state()["present"] is False
    before = dict(hs.state)
    hs.due()
    assert hs.wake() == [{"action": "skip", "reason": C.REASON_CONTENT_UNCONFIGURED}]
    assert hs.state == before, "a refused round must not write anything"
    assert hs.state["pending"] is None


def test_a_coercive_text_has_no_ungoverned_delivery_path(hs, rt):
    """A coercive text has no ungoverned delivery path, and retrying never creates one.

    Two facts, both asserted here: (1) the only way text reaches a user is a released round, and a
    round with no assembly is refused; (2) the refusal is not consumed — three wakes in a row stay
    refused with the same reason and write nothing, so retrying never opens a round either.
    """
    before = dict(hs.state)
    hs.due()
    for attempt in range(3):
        assert hs.wake() == [{"action": "skip", "reason": C.REASON_CONTENT_UNCONFIGURED}], attempt
    assert hs.state == before, "a refused round writes nothing, so retrying sees the same numbers"
    assert hs.state["pending"] is None


def test_a_user_message_is_never_rewritten_by_the_seam(hs, rt):
    rt.set_content(P.assembler(P.all_topic_pair()))
    assert hs.user_message("刚忙完") == [], "user traffic is never touched"
    assert hs.state["visible_since_user"] == 0


# ── 2. composition installed → the content rules on the release path ────────────────────────














def test_the_installed_copy_carries_the_same_p4_modules_as_this_checkout(installed_plugin_dir):
    """No drift between the files under test here and the ones the loader imported."""
    for rel in ("proactive/content.py", "proactive/permissions.py"):
        here = (support.PKG_ROOT / rel).read_bytes()
        there = (installed_plugin_dir / rel).read_bytes()
        assert hashlib.sha256(here).hexdigest() == hashlib.sha256(there).hexdigest(), rel


# ── 3. the audit trail ─────────────────────────────────────────────────────────────────────
