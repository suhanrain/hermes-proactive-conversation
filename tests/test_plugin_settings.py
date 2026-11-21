"""Plugin-level settings: defaults, overrides, and the fail-closed fallback.

Two layers, both real:

* the **resolution rules** in ``proactive.settings`` — what a raw value means, which reason code
  comes out (``config:absent:<key>`` / ``config:invalid:<key>``), and what gets logged;
* the **read path of the loaded plugin** — ``plugin.yaml``'s ``config_schema`` as the manifest
  parser exposes it, and the values the *loaded* plugin resolves through the context it was handed
  (the isolated profile's real ``config.yaml``, read with hermes' own loader).

The behavioural half — a changed setting changing what the plugin actually does, end to end through
the real hooks and the real delivery gate — lives in ``tests/test_gate_integration.py``, next to the
session harness and the delivery gate it needs, instead of being duplicated here.
"""

from __future__ import annotations

import logging

import pytest

import support
from proactive import contract as C
from proactive import runtime as rt_proc
from proactive import settings as S


@pytest.fixture
def rt(plugin_runtime):
    """The plugin's *own* runtime module: seams, in-flight tickets and the bound settings context."""
    return plugin_runtime


# ── the resolution rules ─────────────────────────────────────────────────────────────────────


def test_an_unset_key_is_the_documented_default():
    assert S.resolve_number(S.PENDING_RELEASE_TTL, S.MISSING) == (
        C.PENDING_RELEASE_TTL_DEFAULT_SECONDS, "config:absent:pending_release_ttl_seconds")
    assert S.resolve_number(S.EVENT_TIMESTAMP_SANITY, None) == (
        C.EVENT_TIMESTAMP_SANITY_DEFAULT_SECONDS, "config:absent:event_timestamp_sanity_seconds")


def test_a_configured_value_is_used_and_the_boundaries_are_inclusive():
    assert S.resolve_number(S.PENDING_RELEASE_TTL, 30) == (30.0, "")
    assert S.resolve_number(S.PENDING_RELEASE_TTL, 0.5) == (0.5, "")
    assert S.resolve_number(S.EVENT_TIMESTAMP_SANITY, 3600) == (3600.0, "")
    for setting in S.SETTINGS:
        assert 0 < setting.default <= setting.maximum
        assert S.resolve_number(setting, setting.maximum) == (setting.maximum, "")
        assert S.resolve_number(setting, setting.default) == (setting.default, "")


@pytest.mark.parametrize("raw", [True, False, "600", "", [], {}, [600], {"seconds": 600}, object()])
def test_a_non_number_is_refused_and_collapses_to_the_default(raw):
    for setting in S.SETTINGS:
        assert S.resolve_number(setting, raw) == (
            setting.default, f"config:invalid:{setting.key}")


@pytest.mark.parametrize("raw", [0, 0.0, -1, -0.5, 600_000, C.PENDING_RELEASE_TTL_MAX_SECONDS + 1,
                                 1e18])
def test_zero_negative_and_over_the_cap_are_refused(raw):
    assert S.resolve_number(S.PENDING_RELEASE_TTL, raw) == (
        C.PENDING_RELEASE_TTL_DEFAULT_SECONDS,
        "config:invalid:pending_release_ttl_seconds")


def test_a_milliseconds_value_is_a_unit_slip_not_a_longer_bound():
    """600 000 s is 600 s written in ms: neither bound may silently become 7 days / 19 years."""
    assert S.resolve_number(S.PENDING_RELEASE_TTL, 600_000)[0] == C.PENDING_RELEASE_TTL_DEFAULT_SECONDS
    assert S.resolve_number(S.EVENT_TIMESTAMP_SANITY, 604_800_000)[0] == (
        C.EVENT_TIMESTAMP_SANITY_DEFAULT_SECONDS)


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_number_is_refused(raw):
    """``nan`` passes every ``<``/``>`` comparison, so it needs its own gate."""
    for setting in S.SETTINGS:
        assert S.resolve_number(setting, raw) == (
            setting.default, f"config:invalid:{setting.key}")


def test_without_a_bound_reader_every_setting_is_its_default():
    S.reset_reader()
    assert S.reader_bound() is False
    assert S.value(S.PENDING_RELEASE_TTL) == C.PENDING_RELEASE_TTL_DEFAULT_SECONDS
    assert S.read(S.EVENT_TIMESTAMP_SANITY) == (
        C.EVENT_TIMESTAMP_SANITY_DEFAULT_SECONDS,
        "config:absent:event_timestamp_sanity_seconds")


def test_the_reader_is_asked_for_exactly_the_declared_key_and_our_own_sentinel():
    """The sentinel is what tells "the key is not set" apart from "set to the default value"."""
    seen = []

    def reader(key, default):
        seen.append((key, default))
        return S.MISSING

    S.bind_reader(reader)
    try:
        assert S.value(S.PENDING_RELEASE_TTL) == C.PENDING_RELEASE_TTL_DEFAULT_SECONDS
    finally:
        S.reset_reader()
    assert seen == [(C.SETTING_PENDING_RELEASE_TTL_SECONDS, S.MISSING)]


def test_a_reader_that_raises_is_an_unset_key_not_a_crash(caplog):
    def boom(key, default):
        raise RuntimeError("config unreadable")

    S.bind_reader(boom)
    try:
        with caplog.at_level(logging.WARNING):
            value, reason = S.read(S.PENDING_RELEASE_TTL)
    finally:
        S.reset_reader()
    assert (value, reason) == (C.PENDING_RELEASE_TTL_DEFAULT_SECONDS,
                               "config:absent:pending_release_ttl_seconds")
    # The reader's *body* is untrusted text (it may quote the key it was given), so echoing it is
    # forbidden; the diagnostic that survives is the setting name and the exception type.
    assert S.PENDING_RELEASE_TTL.key in caplog.text
    assert "RuntimeError" in caplog.text
    assert "config unreadable" not in caplog.text, caplog.text


def test_an_invalid_value_is_logged_with_its_reason_code_and_the_default(caplog):
    S.bind_reader(lambda key, default: "soon")
    try:
        with caplog.at_level(logging.WARNING):
            value, reason = S.read(S.EVENT_TIMESTAMP_SANITY)
    finally:
        S.reset_reader()
    assert (value, reason) == (C.EVENT_TIMESTAMP_SANITY_DEFAULT_SECONDS,
                               "config:invalid:event_timestamp_sanity_seconds")
    assert "config:invalid:event_timestamp_sanity_seconds" in caplog.text
    assert "'soon'" in caplog.text and "604800" in caplog.text


def test_a_valid_value_and_an_absent_key_log_no_warning(caplog):
    with caplog.at_level(logging.WARNING):
        S.bind_reader(lambda key, default: 30)
        try:
            assert S.read(S.PENDING_RELEASE_TTL) == (30.0, "")
        finally:
            S.reset_reader()
        assert S.read(S.PENDING_RELEASE_TTL)[1] == "config:absent:pending_release_ttl_seconds"
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ── the loaded plugin's read path ───────────────────────────────────────────────────────────


def test_the_manifest_declares_both_settings_and_matches_the_contract(plugin):
    """plugin.yaml is the user-facing half: it must not drift from the code's defaults."""
    schema = plugin.manifest.config_schema
    assert set(schema) == set(C.PLUGIN_SETTING_KEYS)
    assert plugin.manifest.manifest_version == 2, "config_schema is a manifest v2 field"
    for key, default in ((C.SETTING_PENDING_RELEASE_TTL_SECONDS,
                          C.PENDING_RELEASE_TTL_DEFAULT_SECONDS),
                         (C.SETTING_EVENT_TIMESTAMP_SANITY_SECONDS,
                          C.EVENT_TIMESTAMP_SANITY_DEFAULT_SECONDS)):
        spec = schema[key]
        assert str(spec.get("type")).lower() in {"number", "float", "int"}
        assert float(spec["default"]) == float(default)
        assert spec.get("description"), f"{key} has no description for the settings form"


def test_an_untouched_profile_reads_the_documented_defaults(plugin, rt):
    """``config.yaml`` has only ``plugins.enabled``: both values are the documented defaults."""
    assert rt._settings.reader_bound() is True, "register(ctx) must bind the settings context"
    assert rt.pending_release_ttl() == C.PENDING_RELEASE_TTL_DEFAULT_SECONDS
    assert rt.event_timestamp_sanity() == C.EVENT_TIMESTAMP_SANITY_DEFAULT_SECONDS
    assert rt._settings.read(rt._settings.PENDING_RELEASE_TTL)[1] == (
        "config:absent:pending_release_ttl_seconds")


def test_the_loaded_plugin_reads_the_values_from_the_profile_config(plugin_settings, rt):
    plugin_settings({"pending_release_ttl_seconds": 42,
                     "event_timestamp_sanity_seconds": 90.5})
    assert rt.pending_release_ttl() == 42.0
    assert rt.event_timestamp_sanity() == 90.5
    assert rt._settings.read(rt._settings.PENDING_RELEASE_TTL)[1] == ""


def test_a_config_edit_applies_to_the_next_call_without_reloading_the_plugin(plugin_settings, rt):
    """No reload, no restart: only ``config.yaml`` changes between the two reads."""
    assert rt.pending_release_ttl() == C.PENDING_RELEASE_TTL_DEFAULT_SECONDS
    plugin_settings({"pending_release_ttl_seconds": 7})
    assert rt.pending_release_ttl() == 7.0
    plugin_settings(None)  # the key is gone again, exactly like a user removing it
    assert rt.pending_release_ttl() == C.PENDING_RELEASE_TTL_DEFAULT_SECONDS


@pytest.mark.parametrize("raw", [10 ** 9, -5, 0, "7 days"])
def test_an_unusable_value_in_the_profile_config_is_logged_and_uses_the_default(
        plugin_settings, rt, caplog, raw):
    plugin_settings({"pending_release_ttl_seconds": raw,
                     "event_timestamp_sanity_seconds": raw})
    with caplog.at_level(logging.WARNING):
        assert rt.pending_release_ttl() == C.PENDING_RELEASE_TTL_DEFAULT_SECONDS
        assert rt.event_timestamp_sanity() == C.EVENT_TIMESTAMP_SANITY_DEFAULT_SECONDS
    assert "config:invalid:pending_release_ttl_seconds" in caplog.text
    assert "config:invalid:event_timestamp_sanity_seconds" in caplog.text


def test_a_context_without_get_config_keeps_every_setting_at_its_default(plugin_settings):
    """The capability probe / ``plugins doctor`` call ``register`` with a recording context."""

    class RecordingContext:
        def __init__(self):
            self.hooks = []

        def register_hook(self, name, callback):
            self.hooks.append((name, callback))

    plugin_settings({"pending_release_ttl_seconds": 7})  # a set key that must be ignored now
    rt_proc.bind_plugin_context(RecordingContext())
    try:
        assert S.reader_bound() is False
        assert rt_proc.pending_release_ttl() == C.PENDING_RELEASE_TTL_DEFAULT_SECONDS
        assert rt_proc.event_timestamp_sanity() == C.EVENT_TIMESTAMP_SANITY_DEFAULT_SECONDS
    finally:
        S.reset_reader()
    # ... and a missing/garbage context is just as harmless.
    rt_proc.bind_plugin_context(None)
    assert S.reader_bound() is False
    rt_proc.bind_plugin_context(object())
    assert S.reader_bound() is False


def test_reset_seams_unbinds_the_settings_reader(plugin, plugin_settings):
    """The documented seam contract: after ``reset_seams`` nothing configured is still in effect."""
    plugin_settings({"pending_release_ttl_seconds": 7})
    loaded = support.loaded_runtime()
    assert loaded.pending_release_ttl() == 7.0
    loaded.reset_seams()
    assert loaded._settings.reader_bound() is False
    assert loaded.pending_release_ttl() == C.PENDING_RELEASE_TTL_DEFAULT_SECONDS
