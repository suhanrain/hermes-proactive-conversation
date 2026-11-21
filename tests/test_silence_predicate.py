"""The definition of "the model kept this round silent", one case at a time.

The delivery seam exempts a round from the counters only when the **host's own** delivery decision
for that round is the same silence. So the interesting cases are not "is this text quiet" but "would
the gateway really send nothing here". This module pins that boundary with the host's own predicates,
because the plugin deliberately owns no copy of them:

* exact, host-recognised markers (any case, edge whitespace, edge punctuation) → **exempt**;
* prose that merely *mentions* a marker, a marker with trailing prose, the autonomous lane's loose
  form (``[SILENT] nothing new this tick``), a blank/``None`` response, and a failed turn's notice →
  **not exempt** (the gateway delivers those, so the plugin must count them);
* a round whose ownership the plugin cannot prove (taken over by a user message, or a release that is
  provably retired) → **never exempt**, whatever the text says. That is the one thing this seam must
  not swallow: a user's own answer;
* a round the host would answer with its own "silence marker rejected" text (the marker is not legal
  on that lane) → **not exempt**.

Two limits are stated here as host facts rather than measured plugin behaviour, and are marked as
such: an **interrupted** turn never reaches this seam at all (``apply_llm_output_transform`` is only
reached under ``if final_response and not interrupted``, in both call sites of
``agent/turn_finalizer.py``), and an **empty** response is not a silence decision (the same function
returns before firing any hook), so the plugin can neither exempt nor count it — it sees nothing.
The plugin's own answer for a missing text is asserted directly below: not silence.

The **failed** ordering is *not* left as a comparison of predicates: ``tests/host_e2e/
test_p11_failure_ordering.py`` drives the real host path for both shapes (a hard agent failure that
never reaches the seam, and a failure stamped after the model's marker) and pins what the platform
and the durable counters show. What this module still owns is the host rule those measurements rest
on (``is_intentional_silence_agent_result`` requires ``failed=False``).
"""

from __future__ import annotations

import support  # noqa: F401  (importing it pins a throwaway HERMES_HOME)

import pytest

from proactive import contract as C
from proactive import runtime as rt

#: The gateway's exact-response rule, asked directly: the plugin must agree with this, case by case.
from gateway.response_filters import (
    is_autonomous_silence_response,
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
)

#: Texts the host treats as an intentional silence **on the interactive (DM) lane**.
HOST_SILENCE_TEXTS = [
    "[SILENT]",
    "[SILENT]\n",
    "  [SILENT]  ",
    "silent",
    "Silent",
    "NO_REPLY",
    "no reply",
    "*NO_REPLY*",
    "[静默]",
    "静默",
    "[沉默]",
    "沉默。",
]

#: Texts the host does **not** treat as silence: they reach the user, so they must be counted.
HOST_DELIVERED_TEXTS = [
    "",
    "   ",
    "\n\n",
    None,
    123,
    "[SILENT] 今天没什么要紧事",
    "[SILENT]\n\n周五老王那顿饭，后来去了吗？",
    "我说过 [SILENT] 是给模型的暗号",
    "silence is golden",
    "[SILENTED]",
    "SILENTLY",
    "[SILENT] and [NO_REPLY]",
    "上次你提到「周五和老王吃饭」，后来怎么样了？",
]


# ── 1. agreement with the host's own rule, per case ─────────────────────────────────────────


@pytest.mark.parametrize("text", HOST_SILENCE_TEXTS + HOST_DELIVERED_TEXTS)
def test_the_plugin_verdict_is_the_hosts_verdict(text):
    """One predicate, one answer: the plugin never invents a silence the host would deliver."""
    assert rt._host_silence_verdict(text) is bool(is_intentional_silence_response(text))


def test_the_loose_autonomous_rule_is_deliberately_not_used():
    """``[SILENT] nothing new`` is silence for cron lanes and a *message* on a DM.

    The plugin follows the exact-response rule, which is the stricter of the two — a per-case proof
    rather than "we copied the list once".
    """
    loose = "[SILENT] nothing new this tick"
    assert is_autonomous_silence_response(loose) is True, "the loose autonomous rule does honour it"
    assert is_intentional_silence_response(loose) is False, "the DM rule does not"
    assert rt._host_silence_verdict(loose) is False, (
        "so the plugin must not exempt it: the gateway would deliver this text to the user")


def test_a_failed_turn_never_has_its_marker_honoured_by_the_host():
    """Host fact for the failure口径: the marker only suppresses a *successful* turn.

    This is the host's rule, and it is measured end to end in
    ``tests/host_e2e/test_p11_failure_ordering.py``: a failure stamped after the text leaves this
    plugin's exemption standing (the transform seam is handed no ``failed`` bit), and the gateway
    then sends its own failed-turn notice — so the bare marker is never what the user receives, and
    the round stays uncounted (1 host disclosure, 0 counted rounds).
    """
    from agent.turn_failure_copy import FAILED_TURN_NOTICE

    assert is_intentional_silence_agent_result({"failed": True}, "[SILENT]") is False
    assert is_intentional_silence_agent_result({"failed": False}, "[SILENT]") is True
    # The gateway appends its failure notice to the turn's text, so what is delivered is never the
    # bare marker the seam saw (measured: marker + notice, exactly one send):
    failed_text = "[SILENT]\n\n" + FAILED_TURN_NOTICE
    assert rt._host_silence_verdict(failed_text) is False
    assert is_intentional_silence_response(failed_text) is False


def test_an_empty_response_is_not_a_silence_decision():
    """Host fact for the empty-output口径: the seam is not even reached for an empty final.

    ``apply_llm_output_transform`` returns before firing any hook when the turn produced no text, so
    the plugin writes nothing and counts nothing for it. If a
    host ever did call the seam with no text, the plugin answers "not a silence" — never a free round.
    """
    from agent.turn_finalizer import apply_llm_output_transform

    class _Agent:
        session_id = "s-empty"
        model = "fake-model"
        platform = "feishu"

    for empty in ("", None, 0):
        final, transformed, pre = apply_llm_output_transform(_Agent(), empty, turn_id="t-empty")
        assert (final, transformed, pre) == (empty, False, None)
        assert not hasattr(_Agent, "_llm_output_transform"), "no hook outcome was recorded"
    assert rt._host_silence_verdict("") is False
    assert rt._host_silence_verdict(None) is False


# ── 2. the round's ownership, not the text's shape, decides the exemption ────────────────────


@pytest.mark.parametrize("owner", ["round:not-ours", "round:taken-over", "", "anything-else"])
def test_only_this_plugins_own_round_may_be_exempted(owner):
    """An exact marker on a round we cannot prove ours is never exempted."""
    ticket = {"silence_legal": True}
    assert rt._model_kept_the_round_silent(ticket, owner, "[SILENT]") is False


def test_a_round_the_host_would_not_silence_is_never_exempted():
    """``silence_legal`` is the host's answer for *this* round's lane, captured at release."""
    assert rt._model_kept_the_round_silent({"silence_legal": False}, rt.ROUND_OURS, "[SILENT]") is False
    assert rt._model_kept_the_round_silent({}, rt.ROUND_OURS, "[SILENT]") is False
    assert rt._model_kept_the_round_silent({"silence_legal": True}, rt.ROUND_OURS,
                                           "[SILENT]") is True


@pytest.mark.parametrize("text", HOST_DELIVERED_TEXTS)
def test_a_delivered_text_is_never_exempted_even_on_our_own_round(text):
    """The text half of the rule is exactly the host's: everything else is delivered and counted."""
    assert rt._model_kept_the_round_silent({"silence_legal": True}, rt.ROUND_OURS, text) is False


# ── 3. the exemption is a named verdict, and the marker it returns is the host's own ─────────


def test_the_reason_code_is_a_silent_reason_and_the_marker_is_the_hosts():
    assert C.SILENT_MODEL_CHOICE == "silent:model-choice"
    assert C.SILENCE_MARKER == "[SILENT]"
    assert is_intentional_silence_response(C.SILENCE_MARKER) is True, (
        "the marker the seam returns must itself be one the host honours")
