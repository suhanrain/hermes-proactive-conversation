"""Per-session isolation between dimensions; durability, migration and cleanup.

Real ``SessionStore`` objects over real journal files under ``tmp_path`` — only the hermes *home*
is throwaway (see ``support.py``). Nothing in this file touches a live profile or a live session.
"""

from __future__ import annotations

import json

import pytest

import p4_support as P
import support
from proactive import contract as C
from proactive import policy, runtime
from proactive import store as store_mod
from proactive.state import STATE_KEYS, SessionState

T0 = support.epoch("Asia/Shanghai", 2026, 10, 16, 12, 0)


def _enable(store, source, raw=None, session_id=None):
    """Seed a real session, enable proactive chat on it, return (entry, dims, config)."""
    entry = support.seed_session(store, source)
    dims = support.dims_for(store, entry, source)
    if session_id is not None:  # simulate a session reset: new id under the same key
        dims = dict(dims, session_id=session_id)
    raw = dict(raw if raw is not None else support.CONFIG_A)
    assert runtime.enable_session(store, entry.session_key, dims, raw) is True
    return entry, dims, raw


def _blob(store, session_key):
    return store.get_session_metadata(session_key, C.META_KEY, "MISSING")


def test_blob_lives_under_one_key_with_the_documented_shape(store):
    entry, dims, raw = _enable(store, support.dm_source())
    stored = _blob(store, entry.session_key)
    assert isinstance(stored, dict)
    assert stored["version"] == C.BLOB_VERSION
    assert stored["config"] == raw, "the user's configuration is stored verbatim"
    assert set(stored["state"]) == set(STATE_KEYS)
    assert stored["dims"] == dims
    assert stored["state"]["status"] == C.STATUS_ACTIVE
    assert stored["state"]["visible_since_user"] == 0 and stored["state"]["visible_count"] == 0


def test_no_blob_means_off_not_a_default(store):
    entry = support.seed_session(store, support.dm_source())
    dims = support.dims_for(store, entry, support.dm_source())
    read = store_mod.read(store, entry.session_key, dims)
    assert read.usable is False
    assert read.reason == C.REASON_NO_BLOB


def test_an_unconfigured_session_gets_no_state_written_by_the_gate(store):
    """A session without a blob stays untouched: no state is created for it, ever."""
    source = support.dm_source()
    entry = support.seed_session(store, source)
    runtime.set_clock(lambda: T0)
    try:
        decision = runtime.pre_gateway_dispatch(
            event=support.make_wake(entry.session_key, entry.session_id),
            gateway=None, session_store=store)
    finally:
        runtime.reset_seams()
    assert decision == {"action": "skip", "reason": C.REASON_NO_BLOB}
    assert _blob(store, entry.session_key) == "MISSING"


@pytest.mark.parametrize(
    "changed",
    [
        {"profile": "work"},
        {"platform": "discord"},
        {"chat_id": "999"},
        {"thread_id": "17"},
        {"session_id": "sess-reset-0001"},
        {"session_key": "telegram:dm:other"},
    ],
)
def test_any_dimension_change_makes_the_blob_invisible(store, changed):
    """A switch (profile / platform / chat / thread) or a reset must not inherit an enablement."""
    source = support.group_source(thread_id="7")
    entry, dims, _ = _enable(store, source)
    other = dict(dims)
    other.update(changed)

    read = store_mod.read(store, other["session_key"], other)
    if changed.get("session_key"):
        # A different session key is simply a different session: it has no configuration at all.
        assert read.reason == C.REASON_NO_BLOB
    else:
        assert read.reason == C.REASON_DIMS
    assert read.usable is False
    assert read.config is None and read.state is None

    # ... while the original session is untouched and still usable.
    original = store_mod.read(store, entry.session_key, dims)
    assert original.usable is True


def test_a_second_session_never_sees_the_first_sessions_blob(store):
    a_source = support.dm_source("42")
    b_source = support.dm_source("43")
    a_entry, _, _ = _enable(store, a_source, raw=support.CONFIG_A)
    b_entry = support.seed_session(store, b_source)
    assert a_entry.session_key != b_entry.session_key

    b_dims = support.dims_for(store, b_entry, b_source)
    read_b = store_mod.read(store, b_entry.session_key, b_dims)
    assert read_b.usable is False and read_b.reason == C.REASON_NO_BLOB
    read_a = store_mod.read(store, a_entry.session_key,
                            support.dims_for(store, a_entry, a_source))
    assert read_a.usable is True


def test_state_survives_a_restart_with_the_same_deadline(tmp_path):
    """The state is durable — a new store instance over the same directory reads it back."""
    s1 = support.new_store(tmp_path)
    entry, dims, _ = _enable(s1, support.dm_source())
    before = _blob(s1, entry.session_key)

    s2 = support.new_store(tmp_path)  # a "restart": fresh object, same journal directory
    after = _blob(s2, entry.session_key)
    assert after == before
    read = store_mod.read(s2, entry.session_key, dims)
    assert read.usable is True
    assert read.state.deadline == before["state"]["deadline"]
    assert read.state.anchor_at == before["state"]["anchor_at"]




def test_pause_and_resume_are_durable_and_visible_after_a_restart(tmp_path):
    s1 = support.new_store(tmp_path)
    source = support.dm_source()
    entry, dims, _ = _enable(s1, source)
    assert runtime.pause_session(s1, entry.session_key, dims) is True
    runtime.set_clock(lambda: T0)
    try:
        decision = runtime.pre_gateway_dispatch(
            event=support.make_wake(entry.session_key, entry.session_id),
            gateway=None, session_store=s1)
    finally:
        runtime.reset_seams()
    assert decision == {"action": "skip", "reason": C.REASON_PAUSED}

    s2 = support.new_store(tmp_path)
    assert _blob(s2, entry.session_key)["state"]["status"] == C.STATUS_PAUSED
    assert runtime.resume_session(s2, entry.session_key, dims) is True
    assert _blob(s2, entry.session_key)["state"]["status"] == C.STATUS_ACTIVE


def test_permanent_close_leaves_no_enabled_marker(store):
    """Closing writes an explicit null, and no readable enabled marker survives."""
    source = support.dm_source()
    entry, dims, _ = _enable(store, source)
    assert store_mod.read(store, entry.session_key, dims).usable is True

    assert runtime.revoke_session(store, entry.session_key) is True
    assert _blob(store, entry.session_key) is None, "the value is an explicit JSON null"

    read = store_mod.read(store, entry.session_key, dims)
    assert read.usable is False and read.reason == C.REASON_NO_BLOB

    raw_journal = json.dumps(store.get_session_metadata(entry.session_key, C.META_KEY, None))
    assert "enabled" not in raw_journal, "no enabled marker may survive a close"


def test_unknown_blob_version_is_off_not_guessed(store):
    entry = support.seed_session(store, support.dm_source())
    source = support.dm_source()
    dims = support.dims_for(store, entry, source)
    store.set_session_metadata(entry.session_key, C.META_KEY, {
        "version": C.BLOB_VERSION + 1,
        "dims": dims,
        "config": dict(support.CONFIG_A),
        "state": SessionState().to_dict(),
    })
    read = store_mod.read(store, entry.session_key, dims)
    assert read.usable is False
    assert read.reason == C.REASON_INVALID and read.field_name == "version"


def test_unreadable_state_is_off_not_repaired(store):
    entry = support.seed_session(store, support.dm_source())
    source = support.dm_source()
    dims = support.dims_for(store, entry, source)
    store.set_session_metadata(entry.session_key, C.META_KEY, {
        "version": C.BLOB_VERSION,
        "dims": dims,
        "config": dict(support.CONFIG_A),
        "state": {"anchor_at": T0},  # truncated / handed over wrong
    })
    read = store_mod.read(store, entry.session_key, dims)
    assert read.usable is False
    assert read.reason == C.REASON_INVALID and read.field_name == "state"


def test_a_partial_config_stored_by_something_else_is_off(store):
    entry = support.seed_session(store, support.dm_source())
    source = support.dm_source()
    dims = support.dims_for(store, entry, source)
    partial = dict(support.CONFIG_A)
    partial.pop("day_max_silent")
    store.set_session_metadata(entry.session_key, C.META_KEY, {
        "version": C.BLOB_VERSION,
        "dims": dims,
        "config": partial,
        "state": SessionState(anchor_at=T0, deadline=T0, day="2026-10-16",
                              last_user_at=T0).to_dict(),
    })
    read = store_mod.read(store, entry.session_key, dims)
    assert read.usable is False
    assert read.reason == C.REASON_INVALID and read.field_name == "day_max_silent"
    decision = runtime.pre_gateway_dispatch(
        event=support.make_wake(entry.session_key, entry.session_id),
        gateway=None, session_store=store)
    assert decision == {"action": "skip", "reason": C.REASON_INVALID}


def test_enable_refuses_an_incomplete_config_without_writing(store):
    entry = support.seed_session(store, support.dm_source())
    dims = support.dims_for(store, entry, support.dm_source())
    bad = dict(support.CONFIG_A)
    bad.pop("enabled")
    assert runtime.enable_session(store, entry.session_key, dims, bad) is False
    assert _blob(store, entry.session_key) == "MISSING", "a refused enable must write nothing"


def test_a_user_message_re_anchors_the_stored_window(store):
    source = support.dm_source()
    runtime.set_clock(lambda: T0)
    runtime.set_rng(support.FixedRng(0.5))
    try:
        entry, dims, _ = _enable(store, source)  # enabled at T0: deadline = T0 + 15h
        before = _blob(store, entry.session_key)["state"]
        assert before["anchor_at"] == T0 and before["deadline"] == T0 + 15 * 3600

        runtime.set_rng(support.FixedRng(0.0))  # the re-sample lands on the floor
        runtime.pre_gateway_dispatch(
            event=support.make_event(session_key=entry.session_key, source=source,
                                     timestamp=support.at_clock(T0)),
            gateway=None, session_store=store)
    finally:
        runtime.reset_seams()
    after = _blob(store, entry.session_key)["state"]
    assert after["last_user_at"] == T0
    assert after["anchor_at"] == T0
    assert after["deadline"] == T0 + 6 * 3600, "the window was re-sampled by the user message"
    assert after["deadline"] != before["deadline"]


def test_state_round_trip_is_lossless(store):
    entry, dims, _ = _enable(store, support.dm_source())
    stored = _blob(store, entry.session_key)["state"]
    original = SessionState.from_dict(stored)
    assert original is not None
    assert original.to_dict() == stored
    read = store_mod.read(store, entry.session_key, dims)
    assert policy.sample_deadline(read.state.anchor_at, read.config, support.FixedRng(0.5)) == (
        read.state.anchor_at + 15 * 3600)


def test_a_valid_blob_without_an_origin_stamp_is_off(store):
    """A well-formed blob with NO authorisation stamp is NOT authorised (the hidden metadata-only
    path is removed): only a declaration mirror or an explicit api enable can turn a session on."""
    entry = support.seed_session(store, support.dm_source())
    source = support.dm_source()
    dims = support.dims_for(store, entry, source)
    store.set_session_metadata(entry.session_key, C.META_KEY, {
        "version": C.BLOB_VERSION,
        "dims": dims,
        "config": dict(support.CONFIG_A),
        "state": SessionState(anchor_at=T0, deadline=T0, day="2026-10-16",
                              last_user_at=T0).to_dict(),
    })  # deliberately NO "origin"
    read = store_mod.read(store, entry.session_key, dims)
    assert read.usable is False
    assert read.reason == C.REASON_UNDECLARED


def test_enable_session_stamps_the_api_origin(store):
    """enable_session is an explicit programmatic authorisation, stored with origin="api"."""
    entry, dims, _raw = _enable(store, support.dm_source())
    stored = _blob(store, entry.session_key)
    assert stored.get("origin") == C.ORIGIN_API
    read = store_mod.read(store, entry.session_key, dims)
    assert read.usable is True and read.origin == C.ORIGIN_API


def test_a_declaration_stamped_blob_is_authorised(store):
    """A blob stamped origin="declaration" (as the declaration mirror writes) is usable."""
    entry = support.seed_session(store, support.dm_source())
    source = support.dm_source()
    dims = support.dims_for(store, entry, source)
    store.set_session_metadata(entry.session_key, C.META_KEY, {
        "version": C.BLOB_VERSION,
        "dims": dims,
        "config": dict(support.CONFIG_A),
        "state": SessionState(anchor_at=T0, deadline=T0, day="2026-10-16",
                              last_user_at=T0).to_dict(),
        "origin": C.ORIGIN_DECLARATION,
    })
    read = store_mod.read(store, entry.session_key, dims)
    assert read.usable is True and read.origin == C.ORIGIN_DECLARATION
