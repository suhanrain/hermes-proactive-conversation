"""The composition root: what it wires, what it refuses, and what it never decides.

These tests drive :mod:`proactive.composition` over the isolated home: real adapters, a real
``state.db`` written by Hermes' own store, a real memory file written by the memory tool. The only
things injected are the two windows and the clock (the same seams the plugin itself exposes).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

import p6a_support as S
from proactive import composition, content, contract as C
from proactive.sources import CrossSessionMemorySource, SessionTranscriptSource

NOW = 1_800_000_000.0
DAY = 86400.0
WINDOWS = dict(recent_record_seconds=30 * DAY, transcript_max_messages=50,
               transcript_window_seconds=30 * DAY, memory_window_seconds=30 * DAY)
TOPIC = "周五和老王吃饭"
MEMO = "用户在做减脂"


@pytest.fixture(autouse=True)
def _clean_memory_files():
    for name in ("MEMORY.md", "USER.md"):
        (S.memory_dir() / name).unlink(missing_ok=True)
    yield
    for name in ("MEMORY.md", "USER.md"):
        (S.memory_dir() / name).unlink(missing_ok=True)


def compose(**over):
    config = composition.CompositionConfig(**dict(WINDOWS, **over))
    return composition.build(config, home=S.iso_home(), profile="dev", clock=lambda: NOW)


def seeded_session(tag, rows=((TOPIC, NOW - 60),)):
    sid, key = "p6a_c_" + tag, f"agent:dev:feishu:dm:{tag}"
    S.seed_transcript(S.session_db(), session_id=sid, session_key=key, profile="dev",
                      rows=[("user", text, {"timestamp": when}) for text, when in rows])
    return sid, key


# ── the setting value: parsed, never defaulted ─────────────────────────────────────────────
# ``runtime.refresh_content`` reads ``plugins.entries.proactive-conversation.settings.composition`` and hands
# it to these functions on every hook call. The rule they enforce is the user's: 时间次数不得写死 —
# nothing is filled in, and anything the operator got wrong is a *problem name*, so the caller can
# fail closed without guessing what was meant.


def test_a_complete_value_is_parsed_into_the_config():
    config = composition.from_settings(dict(WINDOWS))
    assert config.recent_record_seconds == 30 * DAY
    assert config.transcript_max_messages == 50
    assert config.problems() == ()


def test_the_rule_tables_default_without_being_written():
    """Only the four windows/counts are required; the shipped rules apply when no table is given."""
    config = composition.from_settings(dict(WINDOWS))
    assert config.transcript_topic_rules == composition.TRANSCRIPT_TOPIC_RULES
    assert config.memory_topic_rules == composition.MEMORY_TOPIC_RULES


@pytest.mark.parametrize("missing", sorted(composition.REQUIRED_SETTING_FIELDS))
def test_a_missing_window_is_named_instead_of_being_defaulted(missing):
    raw = dict(WINDOWS)
    raw.pop(missing)
    config, problems = composition.try_from_settings(raw)
    assert config is None
    assert problems == (missing,), problems
    with pytest.raises(composition.CompositionIncomplete):
        composition.from_settings(raw)


@pytest.mark.parametrize("raw", [None, {}, [], "30 days", 30, [dict(WINDOWS)]])
def test_a_value_that_is_not_a_mapping_is_refused(raw):
    assert composition.try_from_settings(raw)[0] is None
    with pytest.raises(composition.CompositionIncomplete):
        composition.from_settings(raw)


def test_a_null_required_value_is_missing_not_defaulted():
    """``JSON null`` is not a number: YAML ``null`` must not become "some sane default"."""
    raw = dict(WINDOWS, transcript_window_seconds=None)
    config, problems = composition.try_from_settings(raw)
    assert config is None and problems == ("transcript_window_seconds",)


def test_an_unknown_field_is_refused_rather_than_ignored():
    """A typo would otherwise be a silently different behaviour than the operator wrote."""
    config, problems = composition.try_from_settings(dict(WINDOWS, recent_record_second=60))
    assert config is None and problems == ("recent_record_second",), problems


@pytest.mark.parametrize("bad", [
    dict(recent_record_seconds=0), dict(recent_record_seconds=-1),
    dict(transcript_max_messages="50"), dict(transcript_max_messages=0),
    dict(memory_window_seconds=float("nan")),
])
def test_an_unusable_window_value_is_refused_by_the_config_itself(bad):
    config, problems = composition.try_from_settings(dict(WINDOWS, **bad))
    assert config is None and problems, problems


#: Values that are numbers but cannot be safely *computed with*: an infinite window makes every record
#: "recent" (fail open), and an integer too large for a float makes ``float()`` raise inside a pure
#: validation function. Both are refused by field name.
UNSAFE_NUMBERS = [float("inf"), float("-inf"), float("nan"), 10 ** 400, -(10 ** 400)]


@pytest.mark.parametrize("field", sorted(composition.REQUIRED_SETTING_FIELDS))
@pytest.mark.parametrize("bad", UNSAFE_NUMBERS)
def test_a_non_finite_or_overflowing_window_is_named_and_never_raises(field, bad):
    """Every required number refuses non-finite/overflowing values instead of using or raising on them."""
    raw = dict(WINDOWS, **{field: bad})
    config, problems = composition.try_from_settings(raw)          # must not raise
    assert config is None, (field, bad, config)
    assert problems == (field,), (field, bad, problems)
    with pytest.raises(composition.CompositionIncomplete) as refused:
        composition.from_settings(raw)
    assert refused.value.problems == (field,)

    direct = composition.CompositionConfig(**raw)
    assert direct.problems() == (field,), (field, bad, direct.problems())
    assert direct.usable is False
    with pytest.raises(composition.CompositionIncomplete):
        composition.build(direct, home=S.iso_home(), profile="dev")
    # Detection, not repair: the fingerprint of a hostile value is still a plain string (it is only
    # compared, never interpreted), so the hot-read path can decide on it without raising either.
    assert isinstance(composition.settings_fingerprint(raw), str)


@pytest.mark.parametrize("field", sorted(composition.REQUIRED_SETTING_FIELDS))
def test_the_largest_representable_value_still_works(field):
    """The screen refuses *unrepresentable* numbers, not large-but-computable ones (no invented cap)."""
    value = 2 ** 53 if field == "transcript_max_messages" else float(2 ** 53)
    config = composition.from_settings(dict(WINDOWS, **{field: value}))
    assert config.problems() == ()


def test_the_transcript_count_must_be_bindable_by_the_database_that_reads_it():
    """The count becomes a SQL parameter, so its domain is the database's, not float's.

    ``2**63`` passes every float-shaped screen (it is finite and converts cleanly) and yet cannot be
    bound: ``sqlite3`` raises ``OverflowError`` on it. Both bounds are pinned here against a real
    ``sqlite3`` bind so the constant cannot drift from the driver the adapter really uses.
    """
    import sqlite3

    probe = sqlite3.connect(":memory:")
    try:
        assert probe.execute("select 1 limit ?", (composition.SQLITE_INT_MAX,)).fetchall() == [(1,)]
        with pytest.raises(OverflowError):
            probe.execute("select 1 limit ?", (composition.SQLITE_INT_MAX + 1,)).fetchall()
    finally:
        probe.close()

    unbindable = composition.SQLITE_INT_MAX + 1
    raw = dict(WINDOWS, transcript_max_messages=unbindable)
    config, problems = composition.try_from_settings(raw)          # must not raise
    assert config is None and problems == ("transcript_max_messages",), problems
    assert composition.CompositionConfig(**raw).problems() == ("transcript_max_messages",)
    with pytest.raises(composition.CompositionIncomplete) as refused:
        composition.build(composition.CompositionConfig(**raw), home=S.iso_home(), profile="dev")
    assert refused.value.problems == ("transcript_max_messages",)

    # The boundary itself is usable, and it reaches the adapter unchanged (a real value, not a cap).
    largest = composition.from_settings(dict(WINDOWS, transcript_max_messages=composition.SQLITE_INT_MAX))
    assert largest.problems() == ()
    built = composition.build(largest, home=S.iso_home(), profile="dev", clock=lambda: NOW)
    assert built.transcript.windows()["max_messages"] == composition.SQLITE_INT_MAX


def test_a_rule_table_is_parsed_and_its_typos_are_refused():
    raw = dict(WINDOWS, transcript_topic_rules={"min_chars": 6, "acknowledgement_words": ["好的"]})
    config = composition.from_settings(raw)
    assert config.transcript_topic_rules.min_chars == 6
    assert config.transcript_topic_rules.acknowledgement_words == ("好的",)
    broken, problems = composition.try_from_settings(
        dict(WINDOWS, transcript_topic_rules={"min_char": 6}))
    assert broken is None and problems == ("transcript_topic_rules.min_char",), problems


def test_the_fingerprint_changes_exactly_when_the_value_does():
    """What makes a hot read cheap: an unchanged value must not rebuild the assembly."""
    base = composition.settings_fingerprint(dict(WINDOWS))
    assert base == composition.settings_fingerprint(dict(reversed(list(WINDOWS.items()))))
    assert base != composition.settings_fingerprint(dict(WINDOWS, transcript_max_messages=51))
    assert base != composition.settings_fingerprint("absent")


def test_the_pm_recommendation_is_a_value_set_not_a_default():
    """``PM_RECOMMENDED_SETTINGS`` exists to be written into a profile, never to be fallen back to."""
    config = composition.from_settings(composition.PM_RECOMMENDED_SETTINGS)
    assert config == composition.PM_RECOMMENDED
    assert composition.try_from_settings({}) == (None, tuple(composition.REQUIRED_SETTING_FIELDS))


# ── configuration: values, never constants ──────────────────────────────────────────────────


def test_every_window_is_required_so_no_default_can_be_inherited_by_accident():
    with pytest.raises(TypeError):
        composition.CompositionConfig()
    with pytest.raises(TypeError):
        composition.CompositionConfig(recent_record_seconds=DAY, transcript_max_messages=50,
                                      transcript_window_seconds=DAY)  # memory window missing
    assert composition.PM_RECOMMENDED.problems() == ()
    assert composition.PM_RECOMMENDED.transcript_max_messages == 50
    assert composition.PM_RECOMMENDED.transcript_window_seconds == 30 * DAY
    assert composition.PM_RECOMMENDED.memory_window_seconds == 30 * DAY


@pytest.mark.parametrize("field,bad", [
    ("recent_record_seconds", 0),
    ("recent_record_seconds", None),
    ("transcript_max_messages", 0),
    ("transcript_max_messages", 1.5),
    ("transcript_window_seconds", -1),
    ("memory_window_seconds", "30d"),
])
def test_an_unusable_value_is_named_and_refuses_to_build(field, bad):
    config = composition.CompositionConfig(**dict(WINDOWS, **{field: bad}))
    assert config.usable is False
    assert config.problems() == (field,)
    with pytest.raises(composition.CompositionIncomplete) as refused:
        composition.build(config, home=S.iso_home(), profile="dev")
    assert refused.value.problems == (field,)
    assert field in str(refused.value)


def test_the_config_value_is_what_the_adapters_get_not_the_pm_recommendation():
    fresh = S.write_memory([MEMO], when=NOW - 2 * DAY)
    short = compose(memory_window_seconds=DAY)          # one day: the 2-day-old file is not recent
    long = compose(memory_window_seconds=30 * DAY)
    assert short.memory.windows()["window_seconds"] == DAY
    assert long.memory.windows()["window_seconds"] == 30 * DAY
    assert short.memory.windows()["window_seconds"] != composition.PM_RECOMMENDED.memory_window_seconds
    assert fresh.exists()


def test_a_narrower_assembly_window_is_reported_as_such(caplog):
    with caplog.at_level(logging.INFO, logger="proactive.composition"):
        comp = compose(recent_record_seconds=60.0, transcript_window_seconds=30 * DAY)
    assert comp.policy.recent_record_seconds == 60.0
    assert "narrower than a source window" in caplog.text


# ── what is wired ───────────────────────────────────────────────────────────────────────────


def test_the_composition_registers_exactly_the_two_whitelisted_sources():
    sid, key = seeded_session(S.unique("w1"))
    comp = compose()
    scope = S.scope_for(session_key=key, session_id=sid, profile="dev")
    registry = content.SourceRegistry(scope)
    refusals = registry.register_all(comp.sources)
    assert refusals == {}, refusals
    assert registry.registered() == C.CONTENT_SOURCES
    names = [name for name, _ in [(s.name, s) for s in comp.sources]]
    assert sorted(names) == sorted(C.CONTENT_SOURCES)
    assert isinstance(comp.transcript, SessionTranscriptSource)
    assert isinstance(comp.memory, CrossSessionMemorySource)


def test_where_each_adapter_reads_from_and_which_profile_it_is_bound_to():
    comp = compose()
    assert comp.transcript.db_path == S.iso_home() / "state.db"
    assert comp.memory.memory_dir == S.iso_home() / "memories"
    assert comp.home == S.iso_home() and comp.profile == "dev"
    assert composition.profile_for_home(S.iso_home() / "profiles" / "dev") == "dev"
    assert composition.profile_for_home(S.iso_home()) == "default"
    wiring = comp.wiring()
    assert wiring["sources"] == ["session-transcript", "cross-session-memory"]
    assert wiring["windows"]["transcript"]["max_messages"] == 50
    print(f"[P6a wiring] {wiring}")


def test_default_home_follows_the_profiles_own_home_variable(monkeypatch, tmp_path):
    """``HERMES_HOME`` wins; unset, the fallback is hermes' own resolution — not a second opinion.

    A caller that already knows its home (the plugin does) should pass it explicitly. The fallback
    asks hermes, and in a test process hermes' answer is the *live* home.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "somewhere"))
    assert composition.default_home() == tmp_path / "somewhere"
    monkeypatch.delenv("HERMES_HOME")
    from hermes_constants import get_hermes_home

    assert composition.default_home() == Path(get_hermes_home())


# ── installation (plugin-internal, nothing registered with Hermes) ───────────────────────────


class FakeRuntime:
    def __init__(self):
        self.installed = "unset"

    def set_content(self, assembler):
        self.installed = assembler


def test_install_and_uninstall_touch_only_the_runtime_seam():
    module = FakeRuntime()
    comp = composition.install(module, composition.CompositionConfig(**WINDOWS),
                               home=S.iso_home(), profile="dev", clock=lambda: NOW)
    assert isinstance(module.installed, content.Assembler)
    assert module.installed is comp.assembler
    assert module.installed.assemble  # a callable assembly path, not a placeholder
    composition.uninstall(module)
    assert module.installed is None


def test_install_refuses_anything_that_is_not_the_plugin_runtime():
    with pytest.raises(TypeError):
        composition.install(object(), composition.CompositionConfig(**WINDOWS), home=S.iso_home())


# ── the assembly the composition produces ───────────────────────────────────────────────────


def test_a_real_round_is_ready_and_cites_the_newest_real_record():
    tag = S.unique("r1")
    sid, key = seeded_session(tag, rows=((TOPIC, NOW - 60), ("腰还是有点酸", NOW - 600)))
    S.write_memory([MEMO], when=NOW - 120)
    comp = compose()
    assembly = comp.assembler.assemble(S.scope_for(session_key=key, session_id=sid, profile="dev"), NOW)
    assert assembly.ready, (assembly.status, assembly.reason, assembly.sources)
    assert TOPIC in assembly.candidate
    assert assembly.fingerprint.startswith(f"{C.SOURCE_SESSION}:messages#")
    assert comp.transcript.calls == 1 and comp.memory.calls == 1
    statuses = {name: (status, reason, count) for name, status, reason, count in assembly.sources}
    assert statuses[C.SOURCE_SESSION][0] == "ok"
    assert statuses[C.SOURCE_MEMORY][1] == ""
    print(f"[P6a round] reason={assembly.reason} fingerprint={assembly.fingerprint} "
          f"sources={assembly.sources} counts={assembly.audit['counts']}")
    # The fingerprint the material carries is resolvable against the records that were read.
    outcomes = tuple(content.SourceOutcome(name=source.name, status="ok", reason="",
                                           records=tuple(source.read(
                                               S.scope_for(session_key=key, session_id=sid,
                                                           profile="dev"))))
                     for source in (comp.transcript, comp.memory))
    resolved = content.resolve_fingerprint(outcomes, assembly.fingerprint)
    assert resolved is not None and resolved.text == TOPIC
    assert assembly.fingerprint in assembly.material
    assert assembly.candidate in assembly.material


def test_a_transcript_that_cannot_be_read_still_leaves_the_memory_source_usable():
    tag = S.unique("r2")
    sid, key = seeded_session(tag)
    S.write_memory([MEMO], when=NOW - 120)
    broken = SessionTranscriptSource(S.iso_home() / "missing.db",
                                     max_messages=50, window_seconds=30 * DAY, profile="dev",
                                     clock=lambda: NOW)
    config = composition.CompositionConfig(**WINDOWS)
    policy = content.ContentPolicy(recent_record_seconds=30 * DAY)
    assembler = content.Assembler((
        broken, CrossSessionMemorySource(S.memory_dir(), window_seconds=30 * DAY, profile="dev",
                                         clock=lambda: NOW)), policy)
    assembly = assembler.assemble(S.scope_for(session_key=key, session_id=sid, profile="dev"), NOW)
    statuses = {name: (status, reason) for name, status, reason, _ in assembly.sources}
    assert statuses[C.SOURCE_SESSION] == ("unavailable", C.UNAVAILABLE_READ_FAILED)
    assert assembly.ready, "the readable source still supplies the topic"
    assert assembly.fingerprint.startswith(f"{C.SOURCE_MEMORY}:")
    assert MEMO in assembly.candidate
    assert config.usable
    assert all(name in statuses for name in C.CONTENT_SOURCES)


def test_no_source_can_supply_a_topic_means_silence_not_a_substitute():
    tag = S.unique("r3")
    sid, key = seeded_session(tag, rows=())
    comp = compose()  # no memory file either: both real sources are readable and empty
    assembly = comp.assembler.assemble(S.scope_for(session_key=key, session_id=sid, profile="dev"), NOW)
    assert assembly.silent
    assert assembly.reason == C.SILENT_NO_TOPIC
    assert assembly.candidate == "" and assembly.material == "" and assembly.fingerprint == ""
    counts = assembly.audit["counts"]
    assert counts["records"] == 0 and counts["accepted"] == 0
    print(f"[P6a silence] reason={assembly.reason} sources={assembly.sources}")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="creating a symlink needs SeCreateSymbolicLinkPrivilege on Windows; the escape case "
           "is verified on POSIX CI",
)
def test_every_source_unavailable_is_source_down_not_a_weaker_substitute(tmp_path):
    """A round whose sources are all unavailable is silent, and cites nothing."""
    tag = S.unique("r4")
    sid, key = seeded_session(tag)
    outside = tmp_path / "outside.md"
    outside.write_text(MEMO, encoding="utf-8")
    leaking = tmp_path / "memories"
    leaking.mkdir()
    (leaking / "MEMORY.md").symlink_to(outside)
    import os

    os.utime(outside, (NOW - 5, NOW - 5))
    sources = (
        SessionTranscriptSource(S.iso_home() / "missing.db", max_messages=50,
                                window_seconds=30 * DAY, profile="dev", clock=lambda: NOW),
        CrossSessionMemorySource(leaking, window_seconds=30 * DAY, profile="dev",
                                 clock=lambda: NOW))
    assembler = content.Assembler(sources, content.ContentPolicy(recent_record_seconds=30 * DAY))
    scope = S.scope_for(session_key=key, session_id=sid, profile="dev")
    assembly = assembler.assemble(scope, NOW)
    assert assembly.silent and assembly.reason == C.SILENT_SOURCE_DOWN
    assert assembly.candidate == "" and assembly.material == ""
    statuses = {name: status for name, status, _, _ in assembly.sources}
    assert statuses[C.SOURCE_SESSION] == "unavailable"
    assert statuses[C.SOURCE_MEMORY] == "unavailable"
    assert all(source.calls == 1 for source in sources), "every source was really asked once"
    reasons = {reason for _, _, reason, _ in assembly.sources}
    assert reasons == {C.UNAVAILABLE_READ_FAILED}
    print(f"[P6a source-down] reason={assembly.reason} sources={assembly.sources}")
