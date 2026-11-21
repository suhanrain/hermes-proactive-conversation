"""Config-file declarations of which sessions may be spoken to.

The declaration source is a **plugin-level setting**, read on every hook call from
``plugins.entries.proactive-conversation.settings.session_declarations`` (the key
``contract.SETTING_SESSION_DECLARATIONS``) through ``ctx.get_config``. It is a list::

    session_declarations:
      - session_key: "agent:product-manager:feishu:dm:oc_0000000000000000000000000000000"
        session_id: "20260101_000000_deadbeef"
        enabled: true
        timezone: "Asia/Shanghai"
        dnd_start: "23:00"
        dnd_end: "09:00"
        min_idle_seconds: 600
        max_idle_seconds: 1800
        window_max_attempts: 4
        window_min_visible: 2
        day_max_silent: 3

A declaration addresses a channel by its ``session_key``; the ``session_id`` is **optional**:

* **``session_id`` omitted = channel-level authorisation.** Any current session under that
  ``session_key`` is authorised, and the mirror writes the *current* ``session_id`` into the blob,
  so a ``/reset`` / ``/new`` (same key, new id, empty metadata) self-heals on the next hook
  instead of going ``off:dims-mismatch``. ``session_key`` already encodes profile + platform +
  chat + thread, so a different group / thread is a different key and never inherits — only a
  reset of the *same* channel is covered. This is what lets an operator declare a *channel* once
  instead of re-editing the id after every reset.
* **``session_id`` written = exact instance match** (exact-instance match): only that
  one session instance is authorised; a reset makes the declaration stop matching
  (``off:dims-mismatch``) until it is re-declared against the new id.

The ``session_id`` never selects the address on its own; when it is present it is an additional
exact-match requirement on top of ``session_key``.

The nine ``contract.CONFIG_KEYS`` are that session's per-session configuration — the *same* keys, the
*same* validation, the *same* OFF judgement as ``proactive.config.parse``. The **optional**
keys (``contract.OPTIONAL_CONFIG_KEYS``, i.e. ``day_max_attempts``) travel along when a declaration
writes them and are otherwise left to the parser's default; they are never injected, so the mirror
stays verbatim. :func:`validate_declarations` runs the **same** ``proactive.config.validate`` the
runtime uses, so the offline dry-run script and the live OFF verdict cannot disagree.

A declaration is an **additional source of authorisation and parameters**, never a replacement for
the per-session metadata blob: what is mirrored into the blob is exactly (a) the declaration's config
values, verbatim, and (b) the current dimensions. The blob's ``state`` (counters, window, pause,
pending ticket) is never invented here and is preserved when the session already has a usable one.

Two tiers of refusal, both fail closed:

* **the list itself is unreadable** — a value that is not a list, or an entry that is not a mapping
  naming a non-empty string ``session_key`` (``session_id`` is optional, but when present must be a
  non-empty string) — makes the whole declaration list
  unusable: every session is OFF (``off:invalid``), because a half-written list must not silently
  authorise the entries that happen to parse;
* **one declaration refuses one session** — two declarations for the same ``session_key``, an
  explicit ``session_id`` that is not this session's, or a per-session config that is incomplete/invalid
  (including ``enabled: false``) → that session is OFF with the reason code ``config.parse``
  produced, and nothing is written for it.

Everything else (empty list, key absent, no declaration for this ``session_key``) means "this
source says nothing about this session": ``found`` is ``False`` and the caller keeps its existing
per-session-metadata behaviour. That is what keeps this source additive: a profile that never writes
the key behaves exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import config as _config
from . import contract as C
from . import settings as _settings

#: The required address key every declaration must carry (besides ``contract.CONFIG_KEYS``). The
#: ``session_id`` is *not* here: it is an optional exact-match refinement (see the module docstring).
ADDRESS_KEYS = ("session_key",)
#: The optional exact-match refinement. Present → precise instance match; absent → channel-level.
OPTIONAL_ADDRESS_KEYS = ("session_id",)


@dataclass(frozen=True)
class Declaration:
    """One usable declaration: the session it addresses and its parsed per-session config.

    ``session_id`` is ``None`` for a channel-level (wildcard) declaration and the exact instance id
    for a pinned one. The mirror always writes the *current* dimensions regardless (runtime
    ``_mirror_declaration`` uses ``dims``), so a wildcard declaration self-heals across a reset.
    """

    session_key: str
    session_id: Optional[str]
    config: _config.SessionConfig
    config_raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Lookup:
    """The outcome of asking the declaration list about one session.

    ``found`` distinguishes "the declaration list has an opinion about this ``session_key``" from
    "it does not". ``reason`` is empty exactly when the declaration is usable; ``field`` names the
    declaration field that refused the session (diagnostics only — never a config value).
    ``unreadable`` means the *source itself* could not be read, which is not the same as "the
    operator declared nothing": no session may fall back to its stored authorisation in that case.
    """

    found: bool = False
    declaration: Optional[Declaration] = None
    reason: str = ""
    field: str = ""
    unreadable: bool = False

    @property
    def usable(self) -> bool:
        return self.found and self.declaration is not None and not self.reason

    @property
    def refused(self) -> bool:
        return self.found and not self.usable


def _refused(reason: str, field_name: str) -> Lookup:
    return Lookup(found=True, reason=reason, field=field_name)


def _address_of(item: Any) -> Optional[Dict[str, Optional[str]]]:
    """``{session_key, session_id}`` for one raw entry, or ``None`` when it is not well-formed.

    ``session_key`` is required and must be a non-empty string. ``session_id`` is optional: a
    missing / ``None`` value means a channel-level (wildcard) declaration and is returned as
    ``None``; when the key *is* present it must be a non-empty string or the entry is malformed
    (the same fail-closed rule the list has always applied to a half-written address).
    """
    if not isinstance(item, dict):
        return None
    address: Dict[str, Optional[str]] = {}
    for key in ADDRESS_KEYS:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        address[key] = value.strip()
    for key in OPTIONAL_ADDRESS_KEYS:
        value = item.get(key)
        if value is None:
            address[key] = None
        elif isinstance(value, str) and value.strip():
            address[key] = value.strip()
        else:
            return None  # present but unusable: fail closed for the whole list
    return address


def raw_value() -> Any:
    """The configured declaration value (``settings.MISSING`` when unset, unreadable or no reader)."""
    return _settings.read_raw(C.SETTING_SESSION_DECLARATIONS)


def lookup(raw: Any, session_key: Any, session_id: Any) -> Lookup:
    """Ask the declaration list about ``(session_key, session_id)``; never raises.

    See the module docstring for the two refusal tiers and for why "no declaration for this
    session_key" is *not* a refusal.
    """
    if raw is _settings.MISSING or raw is None:
        return Lookup()  # the key is not configured: this source has no opinion
    if not isinstance(raw, list):
        return _refused(C.REASON_INVALID, C.SETTING_SESSION_DECLARATIONS)

    entries: List[tuple] = []
    for index, item in enumerate(raw):
        address = _address_of(item)
        if address is None:
            return _refused(C.REASON_INVALID,
                            f"{C.SETTING_SESSION_DECLARATIONS}[{index}]")
        entries.append((address, item))

    wanted = str(session_key) if session_key else ""
    matching = [pair for pair in entries if pair[0]["session_key"] == wanted]
    if not matching:
        return Lookup()  # nothing declared for this session: keep the metadata behaviour
    if len(matching) > 1:
        # Two declarations for one session address could each claim the session; which one wins
        # would be an accident of list order, so neither does.
        return _refused(C.REASON_INVALID, "session_key")

    address, entry = matching[0]
    current_id = str(session_id) if session_id else ""
    declared_id = address["session_id"]
    if declared_id is not None and declared_id != current_id:
        # A *pinned* declaration naming a session that is not *this* one (a reset / a switched
        # session): OFF, and the existing blob is left untouched (no inheritance across dimensions).
        # A channel-level declaration (``declared_id is None``) matches the whole channel and
        # mirrors the current id below, so a ``/reset`` self-heals instead of going dims-mismatch.
        return _refused(C.REASON_DIMS, "session_id")

    # The eight required keys plus the optional ones the entry actually wrote. The optional key is
    # passed through only when present: injecting its default here would make the mirrored
    # ``config_raw`` claim a value the operator never declared, and the parser already supplies
    # that default (0) on its own.
    config_raw = {
        key: entry[key]
        for key in C.CONFIG_KEYS + C.OPTIONAL_CONFIG_KEYS
        if key in entry
    }
    parsed = _config.parse(config_raw)
    if parsed.reason == C.REASON_DISABLED:
        return _refused(C.REASON_DISABLED, "enabled")
    if not parsed.ok:
        return _refused(parsed.reason or C.REASON_INVALID,
                        parsed.field or C.SETTING_SESSION_DECLARATIONS)
    return Lookup(
        found=True,
        declaration=Declaration(
            session_key=address["session_key"],
            session_id=address["session_id"],
            config=parsed.config,
            config_raw=config_raw,
        ),
    )


@dataclass(frozen=True)
class DeclarationProblem:
    """One problem found in a raw ``session_declarations`` value (offline dry-run).

    ``index`` is the declaration's position in the list (-1 for a whole-list fault); ``field`` names
    the offending key (``session_declarations[i]`` for a malformed entry, the config key otherwise);
    ``reason``/``detail`` are the same codes/notes :func:`proactive.config.validate` produces, so the
    offline report and the runtime OFF verdict are the same set of facts, not two paraphrases.
    """

    index: int
    field: str
    reason: str = C.REASON_INVALID
    detail: str = ""

    def line(self) -> str:
        where = f"session_declarations[{self.index}]" if self.index >= 0 else "session_declarations"
        note = f" ({self.detail})" if self.detail else ""
        return f"{where} invalid: {self.field}{note} [{self.reason}]"


def validate_declarations(raw: Any) -> List[DeclarationProblem]:
    """Every problem in a raw ``session_declarations`` value; ``[]`` means fully green.

    Pure and host-free — it uses the **same** :func:`proactive.config.validate` the runtime's
    :func:`lookup` → ``config.parse`` path uses, so an offline "green" cannot hide a runtime OFF.
    A ``session_key`` declared twice is reported for each entry (the runtime refuses both, since
    list order must not decide which one wins). Reporting every problem (not just the first) is what
    makes the dry-run script useful; the runtime still judges OFF on the first problem it meets.
    """
    problems: List[DeclarationProblem] = []
    if raw is _settings.MISSING or raw is None:
        return problems  # the key is not configured: nothing to validate
    if not isinstance(raw, list):
        return [DeclarationProblem(-1, C.SETTING_SESSION_DECLARATIONS, C.REASON_INVALID,
                                   "not a list")]
    seen: Dict[str, List[int]] = {}
    for index, item in enumerate(raw):
        address = _address_of(item)
        if address is None:
            problems.append(DeclarationProblem(
                index, f"{C.SETTING_SESSION_DECLARATIONS}[{index}]", C.REASON_INVALID,
                "needs a non-empty session_key (session_id optional; when present must be a "
                "non-empty string)",
            ))
            continue
        seen.setdefault(address["session_key"], []).append(index)
        for problem in _config.validate(item):
            problems.append(DeclarationProblem(index, problem.field, problem.reason, problem.detail))
    for indices in seen.values():
        if len(indices) > 1:
            for index in indices:
                problems.append(DeclarationProblem(
                    index, "session_key", C.REASON_INVALID,
                    f"session_key is declared {len(indices)} times",
                ))
    return problems


def lookup_configured(session_key: Any, session_id: Any) -> Lookup:
    """Convenience: :func:`lookup` against the live plugin setting."""
    return lookup(raw_value(), session_key, session_id)


def lookup_setting(session_key: Any, session_id: Any) -> Lookup:
    """Like :func:`lookup_configured`, but reports an unreadable source as such (fail closed).

    ``settings.read_raw_checked`` tells "the key is not set" apart from "the reader raised". The
    second case is **not** "no declaration names this session": a session whose authorisation came
    from a declaration must go OFF rather than fall back to its stored mirror, so the caller gets
    ``unreadable=True`` and decides with a reason code of its own.
    """
    raw, read_ok = _settings.read_raw_checked(C.SETTING_SESSION_DECLARATIONS)
    if not read_ok:
        return Lookup(unreadable=True)
    return lookup(raw, session_key, session_id)
