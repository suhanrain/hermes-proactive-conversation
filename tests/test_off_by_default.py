"""OFF judgement.

The rule under test: a session is OFF unless every required configuration key is present *and*
valid. There is no path in this package that substitutes an example default for a missing or
invalid value — and the test for that is not "the code looks right", it is that
``config.parse`` returns no config object at all for such input, so no later decision can consult
one.
"""

from __future__ import annotations

import pytest

import support
from proactive import contract as C
from proactive import config as cfg_mod

REQUIRED = list(C.CONFIG_KEYS)


def test_complete_valid_config_is_accepted():
    parsed = cfg_mod.parse(dict(support.CONFIG_A))
    assert parsed.ok is True
    assert parsed.reason == ""
    assert parsed.config.enabled is True
    assert parsed.config.min_idle_seconds == 6 * 3600
    assert parsed.config.window_max_attempts == 6
    assert parsed.config.window_min_visible == 1
    assert parsed.config.day_max_silent == 2
    assert parsed.config.day_max_attempts == 2


@pytest.mark.parametrize("missing", REQUIRED)
def test_every_required_key_missing_is_off(missing):
    """Each required key, on its own, makes the session OFF (never a default)."""
    raw = dict(support.CONFIG_A)
    raw.pop(missing)
    parsed = cfg_mod.parse(raw)
    assert parsed.ok is False, f"a config without {missing!r} was accepted"
    assert parsed.reason == C.REASON_INVALID
    assert parsed.field == missing
    assert parsed.config is None, "an unusable config must not exist for later decisions to use"


@pytest.mark.parametrize(
    "field, value",
    [
        ("enabled", "true"),                 # string, not a bool
        ("enabled", 1),                      # int, not a bool
        ("timezone", "Mars/Phobos"),         # not a real IANA zone
        ("timezone", ""),                    # empty
        ("timezone", 42),                    # not a string
        ("dnd_start", "25:00"),              # hour out of range
        ("dnd_start", "23:60"),              # minute out of range
        ("dnd_start", "2300"),               # wrong shape
        ("dnd_end", "09:0"),                 # wrong shape
        ("min_idle_seconds", 0),             # must be > 0
        ("min_idle_seconds", -60),
        ("min_idle_seconds", 6.0),           # float is not accepted silently
        ("min_idle_seconds", True),          # bool is not an int here
        ("max_idle_seconds", 60),            # max < min
        ("window_max_attempts", -1),
        ("window_max_attempts", "2"),
        ("window_min_visible", -1),
        ("day_max_silent", None),
    ],
)
def test_invalid_values_are_off(field, value):
    raw = dict(support.CONFIG_A)
    raw[field] = value
    parsed = cfg_mod.parse(raw)
    assert parsed.ok is False, f"{field}={value!r} was accepted"
    assert parsed.reason == C.REASON_INVALID
    assert parsed.field == field
    assert parsed.config is None


def test_equal_dnd_bounds_are_off():
    """A zero-length (or 24h) DND window is not interpretable: equal bounds are invalid."""
    raw = dict(support.CONFIG_A, dnd_start="09:00", dnd_end="09:00")
    parsed = cfg_mod.parse(raw)
    assert parsed.ok is False
    assert parsed.field == "dnd_end"
    assert parsed.config is None


def test_legal_but_disabled_config_is_off_and_distinguishable():
    """``enabled: false`` is complete and valid, yet OFF — and says so with its own reason."""
    raw = dict(support.CONFIG_A, enabled=False)
    parsed = cfg_mod.parse(raw)
    assert parsed.ok is False
    assert parsed.reason == C.REASON_DISABLED
    assert parsed.config is not None and parsed.config.enabled is False


def test_absent_or_malformed_config_is_off():
    assert cfg_mod.parse(None).reason == C.REASON_NO_BLOB
    assert cfg_mod.parse({}).ok is False
    assert cfg_mod.parse([]).ok is False
    assert cfg_mod.parse("enabled: true").ok is False


def test_no_code_path_reads_an_example_default():
    """A config that is complete except for one key yields *no* config object at all.

    This is the machine-checkable form of "no fallback to example defaults": the only way a later
    decision could use a default is if a config object existed, and here it does not.
    """
    for missing in REQUIRED:
        raw = dict(support.CONFIG_A)
        raw.pop(missing)
        parsed = cfg_mod.parse(raw)
        assert parsed.config is None, missing


def test_documented_units_are_seconds_and_counts():
    """The contract fixes the units: idle windows in seconds, caps as plain counts."""
    parsed = cfg_mod.parse(dict(support.CONFIG_B))
    assert parsed.ok
    assert parsed.config.min_idle_seconds == 3600
    assert parsed.config.max_idle_seconds == 3 * 3600
    assert parsed.config.day_max_attempts == 1
