"""i18n activation: the ``proactive_locale`` setting selects the language rules (card t_f9b01bed).

These tests pin the **mechanism** and the four activation modes the card specifies:

* **unset ⇒ neutral** — no expression regex, no acknowledgement table, no length floor; only the
  global machine markers + structural filter survive (a normal message is never mis-dropped);
* **``zh-Hans`` ⇒ the shipped behaviour** — the migrated Chinese regex / ack table / 6-char floor;
* **``en`` ⇒ the English skeleton** — English regex, English ack words, ``min_words`` floor;
* **``auto`` ⇒ host locale**, falling back to ``en`` with a WARNING when it cannot be read;
* **invalid ⇒ neutral + WARNING** (fail-safe);
* **machine markers always apply** (even neutral).
"""

from __future__ import annotations

import logging

import pytest

import p4_support as P
import support  # noqa: F401  (pins HERMES_HOME before any hermes import)
from proactive import content, locales
from proactive import contract as C
from proactive.sources import topic_rules_from_profile
from proactive.sources.usability import MEMORY_TOPIC_RULES, TRANSCRIPT_TOPIC_RULES

MISSING = locales._settings.MISSING


# ── the resolver ─────────────────────────────────────────────────────────────────────────────


def test_unset_is_neutral_and_inactive():
    act = locales.resolve_activation(MISSING)
    assert act.code == locales.NEUTRAL_CODE == "neutral"
    assert act.active is False and act.filtering is False
    assert act.source == locales.SOURCE_UNSET


def test_null_is_neutral():
    assert locales.resolve_activation(None).active is False


@pytest.mark.parametrize("raw", ["zh-Hans", "zh-hans", "ZH_HANS", "zh", "zh-CN"])
def test_chinese_codes_resolve_to_zh_hans(raw):
    act = locales.resolve_activation(raw)
    assert act.code == "zh-Hans" and act.active is True
    assert act.profile is locales.ZH_HANS


@pytest.mark.parametrize("raw", ["en", "EN", "en-US", "english"])
def test_english_codes_resolve_to_en(raw):
    act = locales.resolve_activation(raw)
    assert act.code == "en" and act.active is True


@pytest.mark.parametrize("bad", ["klingon", "fr", "123", "zh-Hant", "yes"])
def test_an_unknown_value_is_neutral_and_warns(caplog, bad):
    with caplog.at_level(logging.WARNING):
        act = locales.resolve_activation(bad)
    assert act.active is False and act.code == "neutral"
    assert act.source == locales.SOURCE_INVALID
    assert any(C.SETTING_LOCALE in rec.getMessage() for rec in caplog.records), bad


def test_a_non_string_value_is_neutral_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        act = locales.resolve_activation(123)
    assert act.active is False and act.source == locales.SOURCE_INVALID
    assert any(C.SETTING_LOCALE in rec.getMessage() for rec in caplog.records)


# ── auto ─────────────────────────────────────────────────────────────────────────────────────


def test_auto_reads_a_chinese_host_locale():
    act = locales.resolve_activation("auto", host_locale="zh_CN.UTF-8")
    assert act.code == "zh-Hans" and act.active is True and act.source == locales.SOURCE_AUTO


def test_auto_reads_an_english_host_locale():
    act = locales.resolve_activation("auto", host_locale="en_US.UTF-8")
    assert act.code == "en" and act.source == locales.SOURCE_AUTO


@pytest.mark.parametrize("host", [None, "", "fr_FR.UTF-8", "C"])
def test_auto_falls_back_to_en_with_a_warning(caplog, host):
    with caplog.at_level(logging.WARNING):
        act = locales.resolve_activation("auto", host_locale=host)
    assert act.code == "en" and act.active is True
    assert act.source == locales.SOURCE_AUTO_FALLBACK
    assert any(C.SETTING_LOCALE in rec.getMessage() for rec in caplog.records)


# ── neutral keeps only the global machine markers + structural filter ───────────────────────


def test_neutral_has_no_language_rules():
    assert locales.NEUTRAL.expression_patterns == ()
    assert locales.NEUTRAL.acknowledgement_words == ()
    assert topic_rules_from_profile(locales.NEUTRAL).min_chars == 0
    assert topic_rules_from_profile(locales.NEUTRAL).min_words == 0
    assert list(locales.expression_resources(locales.NEUTRAL)) == []


@pytest.mark.parametrize("text", ["[CONTEXT COMPACTION x", "Gateway message origin (json)",
                                  "[Cron delivery: job]", "[Replying to: \"hi\"]"])
def test_neutral_still_withholds_machine_text(text):
    rules = topic_rules_from_profile(locales.NEUTRAL)
    assert rules.why_unusable(text)  # not empty -> withheld as machine text


def test_neutral_does_not_drop_an_acknowledgement_or_short_line():
    rules = topic_rules_from_profile(locales.NEUTRAL)
    # The ack table and the floor are language rules: neutral has neither.
    assert rules.why_unusable("好的") == ""
    assert rules.why_unusable("嗯") == ""
    assert rules.why_unusable("ok") == ""


def test_neutral_assembler_does_not_apply_the_expression_boundary():
    # A coercive record is NOT dropped under neutral (no language regex).
    coercive = P.source(C.SOURCE_MEMORY, [P.rec(C.SOURCE_MEMORY, "m1", "你怎么都不理我")])
    assembled = content.Assembler((coercive,), content.ContentPolicy(recent_record_seconds=P.WINDOW),
                                  expression_patterns=locales.expression_resources(locales.NEUTRAL)
                                  ).assemble(P.scope(), P.NOW)
    assert assembled.ready  # neutral: not filtered


# ── zh-Hans reproduces the shipped behaviour ────────────────────────────────────────────────


def test_zh_hans_assembler_drops_the_coercive_record():
    coercive = P.source(C.SOURCE_MEMORY, [P.rec(C.SOURCE_MEMORY, "m1", "你怎么都不理我")])
    assembled = content.Assembler((coercive,), content.ContentPolicy(recent_record_seconds=P.WINDOW),
                                  expression_patterns=locales.expression_resources(locales.ZH_HANS)
                                  ).assemble(P.scope(), P.NOW)
    assert assembled.silent and assembled.reason == C.SILENT_EXPRESSION


def test_zh_hans_topic_rules_are_the_shipped_table():
    rules = topic_rules_from_profile(locales.ZH_HANS)
    assert rules == TRANSCRIPT_TOPIC_RULES
    assert rules.min_chars == 6 and rules.min_words == 0
    assert rules.why_unusable("好的，谢谢") == "unusable:acknowledgement"
    assert rules.why_unusable("嗯") == "unusable:acknowledgement"
    assert rules.why_unusable("周五和老王吃饭") == ""


def test_memory_rules_are_markers_only_in_every_locale():
    for profile in (locales.NEUTRAL, locales.ZH_HANS, locales.EN):
        rules = topic_rules_from_profile(profile, memory=True)
        assert rules.min_chars == 0 and rules.min_words == 0
        assert rules.acknowledgement_words == ()
        assert rules.exclude_compaction_summaries is False
    assert topic_rules_from_profile(locales.ZH_HANS, memory=True) == MEMORY_TOPIC_RULES


# ── en: the English skeleton ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "this is your last chance",
    "reply me right now",
    "you'll regret this",
    "you never listen to me",
    "you are ignoring me",
    "i'm the only one who cares about you",
    "i'll always be here for you",
])
def test_en_expression_patterns_trip_on_pressure_blame_dependency(text):
    assert content.expression_violation(text, locales.expression_resources(locales.EN)) is not None


@pytest.mark.parametrize("text", [
    "have a nice day",
    "your package arrives on friday",
    "the meeting moved to 3pm",
    "",
])
def test_en_expression_patterns_let_ordinary_text_through(text):
    assert content.expression_violation(text, locales.expression_resources(locales.EN)) is None


def test_en_expression_ids_are_unique_and_named():
    ids = [pid for pid, _ in locales.EN.expression_patterns]
    assert len(ids) == len(set(ids))
    assert {i.split(":")[0] for i in ids} == {"pressure", "blame", "dependency"}


def test_en_topic_rules_use_min_words_and_the_english_ack_table():
    rules = topic_rules_from_profile(locales.EN)
    assert rules.min_words == 2 and rules.min_chars == 0
    assert rules.why_unusable("got it") == "unusable:acknowledgement"
    assert rules.why_unusable("OK") == "unusable:acknowledgement"
    assert rules.why_unusable("thanks") == "unusable:acknowledgement"
    # A one-word fragment is below the word floor; a real phrase is a topic.
    assert rules.why_unusable("Friday") == "unusable:too-short"
    assert rules.why_unusable("visit my parents on friday") == ""


def test_en_assembler_drops_a_coercive_english_record():
    coercive = P.source(C.SOURCE_MEMORY, [P.rec(C.SOURCE_MEMORY, "m1", "reply me right now")])
    assembled = content.Assembler((coercive,), content.ContentPolicy(recent_record_seconds=P.WINDOW),
                                  expression_patterns=locales.expression_resources(locales.EN)
                                  ).assemble(P.scope(), P.NOW)
    assert assembled.silent and assembled.reason == C.SILENT_EXPRESSION


# ── prompt wording is per-language; neutral keeps the pre-i18n English default ───────────────


def test_neutral_and_en_share_the_english_wording():
    assert locales.NEUTRAL.wording == locales.EN.wording


def test_zh_hans_wording_differs_and_has_the_progress_placeholders():
    assert locales.ZH_HANS.wording != locales.EN.wording
    w = locales.ZH_HANS.wording.forced_progress_min
    assert "{visible_since_user}" in w and "{minimum_visible_replies}" in w


def test_neutral_wording_is_byte_identical_to_the_old_default():
    from proactive import trigger

    neutral = locales.NEUTRAL
    assert trigger.PROMPT_WITH_TOOLS == (neutral.wording.soft_body
                                         + trigger._TOOL_CLAUSE_SOFT[True]
                                         + neutral.wording.soft_tail)
