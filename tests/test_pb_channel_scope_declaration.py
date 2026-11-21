"""Channel-level declaration authorisation: ``session_id`` is optional (fast integration lane).

These are the executable-acceptance siblings of the host-level declaration cases
(the real-lane file). They drive the plugin's **registered** hooks through hermes' own delivery
manager against a **real** ``SessionStore`` — the same path the host uses — but do not start the
loopback model server, so they run without the heavy host harness.

What they pin is exactly the decision this card is about: a declaration that omits ``session_id``
authorises the whole channel and self-heals across a ``/reset`` (same ``session_key``, new
``session_id``, empty metadata), while an explicit ``session_id`` still pins one instance and an
explicit mismatch is still ``off:dims-mismatch``.

The mirror is asserted through a **user message** hook: unlike a heartbeat wake (which needs an
installed assembly before it may mirror), an ordinary inbound message authorises and mirrors the
declaration unconditionally, so the durable blob (``config`` + the *current* ``dims.session_id``)
is the honest evidence that the channel was authorised — no model call, no delivery needed.
"""

from __future__ import annotations

import logging

import pytest

import support
from proactive import contract as C

T0 = 1_800_000_000.0

GROUP_A = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "dnd_start": "23:00",
    "dnd_end": "09:00",
    "min_idle_seconds": 600,
    "max_idle_seconds": 1800,
    "window_max_attempts": 4,
    "window_min_visible": 1,
    "day_max_silent": 2,
    "day_max_attempts": 2,
}

_OMIT = object()


def _declaration(session_key, *, session_id=_OMIT):
    """One declaration; ``session_id`` omitted = channel-level, given = pinned instance."""
    entry = {"session_key": session_key, **GROUP_A}
    if session_id is not _OMIT:
        entry["session_id"] = session_id
    return entry


class _Session:
    """A real gateway session driven through the registered hook with a controllable clock."""

    def __init__(self, store, source, entry, clock):
        self.store = store
        self.source = source
        self.entry = entry
        self.clock = clock
        self._n = 0

    @property
    def key(self):
        return self.entry.session_key

    @property
    def sid(self):
        return self.entry.session_id

    @property
    def blob(self):
        return self.store.get_session_metadata(self.key, C.META_KEY, None)

    def blob_present(self) -> bool:
        return self.store.get_session_metadata(self.key, C.META_KEY, "MISSING") != "MISSING"

    def message(self, text: str = "在吗"):
        """One inbound user message through the real ``pre_gateway_dispatch`` hook.

        A user message is never blocked and, when a declaration authorises this channel, mirrors it
        into the metadata key regardless of whether any assembly is installed — which is precisely
        the authorisation decision under test. A *refusal* is swallowed for a user message (it still
        passes through), so a refusal's reason code is observed through :meth:`wake` instead.
        """
        self._n += 1
        self.clock["now"] += 1.0
        event = support.make_event(text=text, source=self.source, session_key=self.key,
                                   timestamp=support.at_clock(self.clock["now"]))
        return support.invoke_hook(
            "pre_gateway_dispatch", event=event, gateway=None, session_store=self.store)

    def wake(self, *, advance: float = 0.0):
        """One heartbeat wake: its verdict carries the refusal reason code when the session is OFF.

        A wake cannot *mirror* without an installed assembly, but every refusal (``perm:closed``,
        ``off:dims-mismatch``, …) is decided before the assembly gate, so this is the seam that
        surfaces the reason without running a model.
        """
        self.clock["now"] += float(advance)
        return support.invoke_hook(
            "pre_gateway_dispatch",
            event=support.make_wake(self.key, self.sid, source=self.source),
            gateway=None, session_store=self.store)

    def reset(self):
        """The gateway's own ``/reset``: a new entry (new id, empty metadata) for this key."""
        old = self.sid
        entry = self.store.get_or_create_session(self.source, force_new=True)
        assert entry.session_id != old and entry.session_key == self.key
        self.entry = entry
        return entry


def _reasons(verdict) -> set:
    return {d.get("reason") for d in (verdict or []) if isinstance(d, dict)}


@pytest.fixture
def make_session(tmp_path, plugin_runtime):
    rt = plugin_runtime
    clock = {"now": T0}
    rt.set_clock(lambda: clock["now"])
    rt.set_rng(support.FixedRng(0.0))
    store = support.new_store(tmp_path)

    def _make(chat_id: str = "42"):
        source = support.dm_source(chat_id=chat_id, profile="product-manager")
        entry = support.seed_session(store, source)
        return _Session(store, source, entry, clock)

    yield _make

    rt.reset_seams()


# ══ 1. a channel-level declaration authorises and mirrors the current id ═══════════════════════

def test_channel_level_declaration_authorises_and_mirrors_current_session_id(make_session, plugin_settings):
    s = make_session("oc_channel_scope")
    plugin_settings({"session_declarations": [_declaration(s.key)]})

    verdict = s.message()
    assert C.REASON_DIMS not in _reasons(verdict), verdict
    blob = s.blob
    assert isinstance(blob, dict), f"the channel declaration did not mirror: {blob!r}"
    assert blob["config"] == GROUP_A, blob["config"]
    assert blob["dims"]["session_id"] == s.sid, blob["dims"]
    assert blob["dims"]["session_key"] == s.key, blob["dims"]


# ══ 2. a reset (same key, new id, empty metadata) self-heals on the next hook ══════════════════

def test_a_reset_self_heals_with_no_dims_mismatch(make_session, plugin_settings):
    s = make_session("oc_channel_reset")
    plugin_settings({"session_declarations": [_declaration(s.key)]})
    s.message()
    old_id = s.sid
    assert s.blob["dims"]["session_id"] == old_id

    s.reset()  # the /reset: new session id, metadata empty
    assert s.sid != old_id
    assert s.blob_present() is False, "a reset must leave the new entry with no plugin metadata"

    verdict = s.message()  # next hook after the reset
    assert C.REASON_DIMS not in _reasons(verdict), (
        f"a channel declaration went off:dims-mismatch after a reset: {verdict!r}")
    blob = s.blob
    assert isinstance(blob, dict), f"the reset was not self-healed (no mirror): {blob!r}"
    assert blob["dims"]["session_id"] == s.sid, blob["dims"]
    assert blob["config"] == GROUP_A, blob["config"]


# ══ 3. a different session_key (another group / thread) never inherits ═════════════════════════

def test_a_different_session_key_does_not_inherit_the_authorisation(make_session, plugin_settings):
    declared = make_session("oc_declared_channel")
    other = make_session("oc_other_channel")
    assert declared.key != other.key
    plugin_settings({"session_declarations": [_declaration(declared.key)]})

    verdict = other.message()
    # No declaration names this key: the source is silent → the untouched per-session metadata
    # decides, i.e. there is no blob and the session stays OFF. Nothing may be mirrored.
    assert other.blob_present() is False, f"a different session_key inherited a mirror: {other.blob!r}"
    assert C.REASON_DIMS not in _reasons(verdict), verdict


# ══ 4. an explicit session_id still pins one instance (regression) ═════════════════════════════

def test_an_explicit_session_id_still_pins_the_instance_and_goes_dims_mismatch_after_reset(
        make_session, plugin_settings):
    s = make_session("oc_pinned")
    plugin_settings({"session_declarations": [_declaration(s.key, session_id=s.sid)]})
    s.message()
    assert s.blob["dims"]["session_id"] == s.sid

    old_id = s.sid
    s.reset()
    assert s.sid != old_id
    verdict = s.wake(advance=601)
    assert C.REASON_DIMS in _reasons(verdict), (
        f"a pinned declaration must go off:dims-mismatch after a reset: {verdict!r}")
    assert s.blob_present() is False, "a pinned mismatch must not write metadata for the new id"


# ══ 5. perm:closed after a reset follows the owner's semantics: a reset re-authorises ═══════════

def test_a_reset_re_authorises_a_previously_permanently_closed_channel(make_session, plugin_settings):
    s = make_session("oc_closed_reset")
    plugin_settings({"session_declarations": [_declaration(s.key)]})
    s.message()
    assert s.blob["config"] == GROUP_A

    # The operator permanently closes *this* entry: the key holds JSON null.
    assert s.store.set_session_metadata(s.key, C.META_KEY, None)
    verdict = s.wake()
    assert C.REASON_PERM_CLOSED in _reasons(verdict), (
        f"a declared close must stay perm:closed on the same entry: {verdict!r}")
    assert s.blob is None, "the closed key must not be re-created on the same entry"

    # The user's own /reset produces a NEW entry (empty metadata): the owner's decision is that this
    # re-authorises the channel — the declaration mirrors the new id again, no perm:closed.
    s.reset()
    assert s.blob_present() is False
    verdict = s.message()
    assert C.REASON_PERM_CLOSED not in _reasons(verdict), (
        f"a reset must re-authorise the channel (owner semantics): {verdict!r}")
    blob = s.blob
    assert isinstance(blob, dict), f"the reset did not re-authorise the channel: {blob!r}"
    assert blob["dims"]["session_id"] == s.sid, blob["dims"]


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 6. the TRIGGER CLOCK enumerates a channel-level declared session (the layer `t_d28b654a` missed)
# ════════════════════════════════════════════════════════════════════════════════════════════════
# The tests above drive the *inbound* hook. They cannot catch the gap this card is about: the clock
# scan `runtime._clock_sessions()` was still requiring a non-empty ``session_id``, so a channel-level
# declaration was skipped there and ``tick()`` saw zero sessions — the round never released even
# though the mirror above succeeded. These drive that exact layer: enumerate then let the real
# ``trigger.tick`` release a due round.


def _recorder(result=True):
    calls = []

    def inject(text, session_key=None):
        calls.append((text, session_key))
        return result

    return inject, calls


def test_clock_enumerates_a_mirrored_channel_level_session_and_releases(
        make_session, plugin_settings, plugin_runtime, caplog):
    """Channel-level declaration + a mirrored blob → enumerated → a due ``tick`` releases a round."""
    rt = plugin_runtime
    s = make_session("oc_clock_channel")
    plugin_settings({"session_declarations": [_declaration(s.key)]})  # no session_id ⇒ channel-level

    s.message()  # one inbound message mirrors the *current* id into the blob
    blob = s.blob
    assert isinstance(blob, dict), f"the channel declaration did not mirror: {blob!r}"
    assert blob["dims"]["session_id"] == s.sid, blob["dims"]

    # The message opened a window of U[600, 1800]s (FixedRng(0.0) ⇒ 600s). Advance past the minimum,
    # well inside the maximum: the round is now due.
    s.clock["now"] += 700.0

    sessions = rt._clock_sessions(s.store)
    assert len(sessions) == 1, f"the clock did not enumerate the channel-level session: {sessions!r}"
    assert sessions[0]["session_key"] == s.key
    # Channel-level follows the *current* id the blob was mirrored with (not a declared one).
    assert sessions[0]["session_id"] == s.sid
    assert sessions[0]["dims"] == blob["dims"], "the clock must reuse the blob's stored dims verbatim"

    inject, calls = _recorder(True)
    caplog.set_level(logging.INFO)
    outcomes = rt._trigger.tick(s.store, sessions, s.clock["now"], inject=inject,
                                rng=support.FixedRng(0.0))

    assert outcomes[0][1].accepted is True, f"the due round was not released: {outcomes!r}"
    assert len(calls) == 1 and calls[0][1] == s.key, calls
    assert "[proactive-conversation]" in calls[0][0], calls
    assert "proactive round released" in caplog.text, caplog.text


def test_clock_still_enumerates_a_pinned_declaration_only_for_its_instance(
        make_session, plugin_settings, plugin_runtime):
    """A written ``session_id`` stays an exact-instance match: the clock does not follow the blob."""
    rt = plugin_runtime
    s = make_session("oc_clock_pinned")
    plugin_settings({"session_declarations": [_declaration(s.key, session_id=s.sid)]})

    s.message()  # mirrors the pinned id
    sessions = rt._clock_sessions(s.store)
    assert [x["session_id"] for x in sessions] == [s.sid], sessions

    # As if the operator had pinned a *stale* instance: the declaration names another id, so the
    # clock must not enumerate this (now mismatching) session — the channel-level loosening must not
    # have made a pinned declaration follow whatever id the blob currently holds.
    plugin_settings({"session_declarations": [_declaration(s.key, session_id="stale-instance-id")]})
    assert rt._clock_sessions(s.store) == []


def test_clock_does_not_enumerate_a_different_session_key(make_session, plugin_settings, plugin_runtime):
    """A channel-level declaration is scoped to its ``session_key``; a sibling channel is not scanned."""
    rt = plugin_runtime
    declared = make_session("oc_clock_declared")
    other = make_session("oc_clock_other")
    assert declared.key != other.key
    plugin_settings({"session_declarations": [_declaration(declared.key)]})

    declared.message()  # only the declared channel mirrors
    keys = [x["session_key"] for x in rt._clock_sessions(declared.store)]
    assert keys == [declared.key], keys
    assert other.key not in keys


def test_clock_does_not_enumerate_a_declared_session_with_no_blob(make_session, plugin_settings, plugin_runtime):
    """A channel-level declaration with no mirrored blob yet has nothing to release → not enumerated."""
    rt = plugin_runtime
    s = make_session("oc_clock_noblob")
    plugin_settings({"session_declarations": [_declaration(s.key)]})
    # No inbound message: no blob has been mirrored for this channel yet.
    assert s.blob_present() is False
    assert rt._clock_sessions(s.store) == []
