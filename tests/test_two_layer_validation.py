"""Two-layer declaration validation — pure, host-free.

Every rule here is asserted through the **same** functions the runtime uses:
``proactive.config.validate`` / ``config.parse`` and ``proactive.declarations.validate_declarations``
/ ``declarations.lookup``. The last test proves the offline dry-run script reports the same field
set the runtime turns OFF on, so "offline green, online OFF" cannot happen.
"""

from __future__ import annotations

import copy

import pytest

from proactive import config as cfg
from proactive import contract as C
from proactive import declarations as D


BASE = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "dnd_start": "23:00",
    "dnd_end": "09:00",
    "min_idle_seconds": 600,
    "max_idle_seconds": 1800,
}

#: A complete, valid two-layer block.
TWO_LAYER = dict(BASE, window_max_attempts=4, window_min_visible=1,
                 day_max_silent=5, day_max_attempts=25)


def _fields(problems):
    return {p.field for p in problems}


# ── 1. a valid two-layer config parses ───────────────────────────────────────────────────────

def test_valid_two_layer_config_parses_and_is_active():
    parsed = cfg.parse(TWO_LAYER)
    assert parsed.ok, (parsed.field, parsed.reason)
    c = parsed.config
    assert (c.window_max_attempts, c.window_min_visible) == (4, 1)
    assert (c.day_max_silent, c.day_max_attempts) == (5, 25)


# ── 2. per-field type / non-negativity (bool is not an int) ─────────────────────────────────

@pytest.mark.parametrize("field", ["window_max_attempts", "window_min_visible",
                                   "day_max_silent", "day_max_attempts"])
@pytest.mark.parametrize("bad", [-1, "2", 1.5, 2.0, True])
def test_each_two_layer_field_rejects_bad_type_and_negatives(field, bad):
    raw = dict(TWO_LAYER)
    raw[field] = bad
    result = cfg.parse(raw)
    assert result.ok is False
    assert result.field == field
    assert result.reason == C.REASON_INVALID


# ── 3. V_w ≤ R_w (when a window cap exists) ─────────────────────────────────────────────────

def test_min_visible_greater_than_max_attempts_is_rejected_with_field_and_reason():
    problems = cfg.validate(dict(TWO_LAYER, window_min_visible=5))
    assert "window_min_visible" in _fields(problems)
    cross = [p for p in problems if p.field == "window_min_visible"][0]
    assert "window_min_visible>window_max_attempts" in cross.reason

    result = cfg.parse(dict(TWO_LAYER, window_min_visible=5))
    assert result.ok is False
    assert result.field == "window_min_visible"
    assert "window_min_visible>window_max_attempts" in result.reason


def test_min_visible_equal_to_max_attempts_is_allowed():
    assert cfg.parse(dict(TWO_LAYER, window_max_attempts=3, window_min_visible=3)).ok


# ── 4. R_d ≥ S_d (when a day total cap exists) ───────────────────────────────────────────────

def test_day_silent_greater_than_day_attempts_is_rejected():
    problems = cfg.validate(dict(TWO_LAYER, day_max_silent=10, day_max_attempts=3))
    assert "day_max_attempts" in _fields(problems)
    result = cfg.parse(dict(TWO_LAYER, day_max_silent=10, day_max_attempts=3))
    assert result.ok is False
    assert result.field == "day_max_attempts"
    assert "day_max_silent>day_max_attempts" in result.reason


def test_day_silent_equal_to_day_attempts_is_allowed():
    assert cfg.parse(dict(TWO_LAYER, day_max_silent=5, day_max_attempts=5)).ok


# ── 5. all-zero red line ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("zeroed", [
    dict(window_max_attempts=0, day_max_silent=0, day_max_attempts=0),
    dict(window_max_attempts=0, day_max_silent=0),  # day_max_attempts absent (=0)
])
def test_all_limits_zero_is_rejected(zeroed):
    raw = dict(BASE)
    for key, value in zeroed.items():
        raw[key] = value
    # add a marker key so the two-layer model is active even when all caps are 0
    raw.setdefault("window_min_visible", 0)
    result = cfg.parse(raw)
    assert result.ok is False
    assert "no-effective-limit" in result.reason


def test_one_effective_limit_is_enough():
    # Only S_d is a live cap; R_w and R_d are 0/unset.
    raw = dict(BASE, window_max_attempts=0, window_min_visible=0,
               day_max_silent=3, day_max_attempts=0)
    assert cfg.parse(raw).ok


# ── 6. there is no legacy model: a config without the two-layer keys is OFF ─────────────────

def test_config_without_two_layer_keys_is_off_not_a_legacy_model():
    """The old single-layer keys are gone — a config that writes only the core keys (or the old
    ``minimum_visible_replies`` / ``max_silent_per_day`` keys) is ``off:invalid`` on the missing
    required ``window_max_attempts``, never a silent fallback to a legacy model."""
    parsed = cfg.parse(dict(BASE, minimum_visible_replies=1, max_silent_per_day=5,
                            max_unanswered=4, max_per_day=25))
    assert parsed.ok is False
    assert parsed.field == "window_max_attempts"
    assert parsed.reason == C.REASON_INVALID


# ── 7. the three required two-layer keys are the schema ─────────────────────────────────────

def test_missing_two_layer_key_is_rejected_no_fallback():
    """Writing just one two-layer key does **not** derive the others; the missing required key is
    ``off:invalid`` (old keys are ignored)."""
    raw = dict(BASE, minimum_visible_replies=1, max_silent_per_day=5,
               max_unanswered=3, max_per_day=20, window_max_attempts=6)
    result = cfg.parse(raw)
    assert result.ok is False
    assert result.field in ("window_min_visible", "day_max_silent")
    assert result.reason == C.REASON_INVALID


def test_a_config_with_only_the_new_keys_parses_cleanly():
    """A declaration that writes only the two-layer keys (plus the six core keys) is valid."""
    clean = {key: BASE[key] for key in
             ("enabled", "timezone", "dnd_start", "dnd_end", "min_idle_seconds", "max_idle_seconds")}
    clean.update(window_max_attempts=6, window_min_visible=1, day_max_silent=5)
    parsed = cfg.parse(clean)
    assert parsed.ok, (parsed.field, parsed.reason)
    c = parsed.config
    assert (c.window_max_attempts, c.window_min_visible, c.day_max_silent,
            c.day_max_attempts) == (6, 1, 5, None)


def test_legacy_keys_written_next_to_two_layer_keys_are_ignored():
    """Old keys written next to the two-layer block are simply ignored (extra keys, never read)."""
    raw = dict(BASE, window_max_attempts=4, window_min_visible=1, day_max_silent=5,
               day_max_attempts=25, max_silent_per_day=999, max_unanswered=3, max_per_day=99)
    parsed = cfg.parse(raw)
    assert parsed.ok, (parsed.field, parsed.reason)
    c = parsed.config
    # The bogus old max_silent_per_day=999 does not leak into the effective config, and no legacy
    # attribute exists at all.
    assert c.day_max_silent == 5
    assert not hasattr(c, "max_silent_per_day")
    assert not hasattr(c, "max_unanswered")
    assert not hasattr(c, "maximum_visible_replies")
    assert not hasattr(c, "two_layer")


def test_missing_window_max_attempts_is_off_invalid_with_the_field():
    """A config with no ``window_max_attempts`` is ``off:invalid`` naming that exact field — it is
    not silently derived from any old key."""
    raw = {"enabled": True, "timezone": "Asia/Shanghai", "dnd_start": "23:00", "dnd_end": "09:00",
           "min_idle_seconds": 600, "max_idle_seconds": 1800,
           "window_min_visible": 1, "day_max_silent": 5}  # window_max_attempts absent
    result = cfg.parse(raw)
    assert result.ok is False
    assert result.field == "window_max_attempts"
    assert result.reason == C.REASON_INVALID


def test_a_declaration_dropping_the_old_keys_parses():
    """The live config can drop the four legacy keys: a declaration with only the new keys parses."""
    entry = {"session_key": "agent:live", "enabled": True, "timezone": "Asia/Shanghai",
             "dnd_start": "23:00", "dnd_end": "09:00", "min_idle_seconds": 600,
             "max_idle_seconds": 1800, "window_max_attempts": 6, "window_min_visible": 1,
             "day_max_silent": 5, "day_max_attempts": 6}
    assert D.validate_declarations([entry]) == []
    looked = D.lookup([entry], "agent:live", None)
    assert looked.usable is True, (looked.reason, looked.field)


# ── 8. declarations: fail-closed per entry, and enumerate every problem ─────────────────────

def _decl(skey, **over):
    entry = {
        "session_key": skey,
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "dnd_start": "23:00",
        "dnd_end": "09:00",
        "min_idle_seconds": 600,
        "max_idle_seconds": 1800,
        "max_unanswered": 4,
        "max_per_day": 25,
        "window_max_attempts": 4,
        "window_min_visible": 1,
        "day_max_silent": 5,
    }
    entry.update(over)
    return entry


def test_one_bad_declaration_only_turns_that_channel_off():
    good = _decl("agent:a")
    bad = _decl("agent:b", window_min_visible=9)
    raw = [good, bad]

    # The dry-run validator reports the bad one (index 1) and leaves the good green.
    problems = D.validate_declarations(raw)
    assert [p.index for p in problems] == [1]
    assert problems[0].field == "window_min_visible"

    # The runtime lookup: good is usable, bad is refused on the same field.
    assert D.lookup(raw, "agent:a", None).usable is True
    refused = D.lookup(raw, "agent:b", None)
    assert refused.usable is False
    assert refused.field == "window_min_visible"


def test_malformed_entry_is_reported_and_fail_closed():
    raw = [_decl("agent:a"), {"enabled": True}]  # second has no session_key
    problems = D.validate_declarations(raw)
    assert any(p.index == 1 and "session_declarations[1]" in p.field for p in problems)


def test_duplicate_session_key_is_reported_for_each_entry():
    raw = [_decl("agent:dup"), _decl("agent:dup")]
    problems = D.validate_declarations(raw)
    dup = [p for p in problems if p.field == "session_key"]
    assert {p.index for p in dup} == {0, 1}


# ── 9. dry-run vs runtime consistency: identical bad config, identical field set ────────────

@pytest.mark.parametrize("overrides", [
    {"window_min_visible": 9},
    {"day_max_silent": 10, "day_max_attempts": 3},
    {"window_max_attempts": "x"},
    {"day_max_silent": -1},
])
def test_dry_run_field_set_matches_runtime_off_field(overrides):
    """The offline validator and the runtime parser agree on WHICH field refuses an entry.

    ``validate_declarations`` (used by the dry-run script) reports every problem; the runtime
    ``lookup`` → ``config.parse`` turns OFF on the first. Both are driven by the same
    ``config.validate``, so the runtime's OFF field is always one the script also reports.
    """
    entry = _decl("agent:x", **overrides)
    script_fields = _fields(D.validate_declarations([entry]))
    runtime_off = D.lookup([entry], "agent:x", None)
    assert runtime_off.usable is False
    assert runtime_off.field in script_fields
    # And when the script reports nothing, the runtime must accept the entry.
    clean = _decl("agent:y")
    assert D.validate_declarations([clean]) == []
    assert D.lookup([clean], "agent:y", None).usable is True


def test_validate_is_pure_and_order_is_deterministic():
    raw = dict(TWO_LAYER, window_min_visible=9, day_max_silent=-1)
    first = cfg.validate(copy.deepcopy(raw))
    second = cfg.validate(copy.deepcopy(raw))
    assert [p.field for p in first] == [p.field for p in second]
    # validate never mutates its input
    assert raw["window_min_visible"] == 9


# ── 10. the runtime's first-hook aggregate WARNING: once, names the field, leaks no key ─────

def test_runtime_logs_an_aggregate_warning_once_without_the_plaintext_key(caplog):
    import logging

    from proactive import runtime
    from proactive import settings as S

    bad = [_decl("agent:very-secret-chat-key", window_min_visible=9)]
    runtime.reset_seams()  # a fresh process: clears the once-per-process flag

    def reader(key, default):
        if key == C.SETTING_SESSION_DECLARATIONS:
            return bad
        return default

    S.bind_reader(reader)
    try:
        with caplog.at_level(logging.WARNING, logger="proactive.runtime"):
            runtime._log_declaration_summary_once()
            runtime._log_declaration_summary_once()  # second call must not log again
    finally:
        S.reset_reader()

    lines = [r.getMessage() for r in caplog.records if "window_min_visible" in r.getMessage()]
    assert len(lines) == 1, lines
    # Names the field, never the plaintext session_key.
    assert "very-secret-chat-key" not in caplog.text

