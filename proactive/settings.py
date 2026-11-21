"""Plugin-level settings: ``plugins.entries.<plugin-id>.settings.*`` with documented defaults.

Everything that influences behaviour comes from configuration — the per-session values from the
session's own config, and the plugin-level settings from here, because they belong to the plugin
as a whole rather than to one session. There are **three** mechanical values —
``pending_release_ttl_seconds`` and ``event_timestamp_sanity_seconds`` —
plus the two structural settings that are not scalars (``session_declarations`` and
``composition``), which are parsed elsewhere.

How they are read: the plugin context handed to ``register(ctx)`` is the only config surface used
(``ctx.get_config(key, default)``, which the official plugin guide documents as resolving
``plugins.entries.<plugin-id>.settings.<key>``; the host's ``config`` subtree is also accepted as a
fallback). :func:`bind_context` captures it once at registration; the lookup itself happens on
**every** hook call, so editing ``config.yaml`` takes effect on the next call — no plugin reload,
no restart. A context without ``get_config`` (the capability probe, ``plugins doctor``) leaves every
setting at its documented default, and nothing here imports hermes, so this module stays usable in
a bare interpreter.

Decision order for one key:

1. **absent** — no reader bound, the key is not set anywhere, or the value is ``None`` → the
   documented default, reason ``config:absent:<key>`` (normal; logged at debug);
2. **invalid** — the key is set to something the plugin refuses (not a number; a ``bool``; not
   finite; ``<= 0``; above the key's maximum) → the documented default, reason
   ``config:invalid:<key>`` **and** a ``WARNING`` naming the key, the value and the reason;
3. otherwise the configured value itself, reason ``""``.

An invalid value collapses to the documented default and to nothing else: a typo can never widen a
safety valve, and it can never invent a third behaviour that neither the default nor a valid
setting would produce.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from . import contract as C

logger = logging.getLogger(__name__)


class _Missing:
    """Sentinel passed to ``ctx.get_config`` as its default: it is how "the key is not set" is told
    apart from "the key is set to exactly the default value"."""

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<unset>"


MISSING = _Missing()


@dataclass(frozen=True)
class NumericSetting:
    """One plugin-level knob: its settings key, its documented default and its upper bound."""

    key: str
    default: float
    maximum: float


PENDING_RELEASE_TTL = NumericSetting(
    key=C.SETTING_PENDING_RELEASE_TTL_SECONDS,
    default=C.PENDING_RELEASE_TTL_DEFAULT_SECONDS,
    maximum=C.PENDING_RELEASE_TTL_MAX_SECONDS,
)
EVENT_TIMESTAMP_SANITY = NumericSetting(
    key=C.SETTING_EVENT_TIMESTAMP_SANITY_SECONDS,
    default=C.EVENT_TIMESTAMP_SANITY_DEFAULT_SECONDS,
    maximum=C.EVENT_TIMESTAMP_SANITY_MAX_SECONDS,
)
#: Every plugin-level setting, in one place (plugin.yaml's ``config_schema`` mirrors this list).
SETTINGS: Tuple[NumericSetting, ...] = (PENDING_RELEASE_TTL, EVENT_TIMESTAMP_SANITY)

_reader: Optional[Callable[[str, Any], Any]] = None


def bind_reader(reader: Optional[Callable[[str, Any], Any]]) -> None:
    """Bind the ``get_config(key, default)``-shaped reader; ``None``/non-callable leaves it unbound."""
    global _reader
    _reader = reader if callable(reader) else None


def bind_context(ctx: Any) -> None:
    """Bind the host adapter's config reader; a context without one leaves settings unbound.

    The context itself is the host adapter's business; this module only ever holds
    the ``get_config(key, default)``-shaped reader it is handed.
    """
    from . import host_adapter as _adapter

    bind_reader(_adapter.config_reader())


def reset_reader() -> None:
    """Drop the bound reader: every setting falls back to its documented default."""
    bind_reader(None)


def reader_bound() -> bool:
    """Whether a reader is bound (diagnostics: distinguishes "unset" from "no config access")."""
    return _reader is not None


def resolve_number(setting: NumericSetting, raw: Any) -> Tuple[float, str]:
    """``(value, reason)`` for one raw setting value; never raises.

    ``reason`` is ``""`` exactly when a configured value was accepted; otherwise it is
    ``config:absent:<key>`` or ``config:invalid:<key>`` and ``value`` is the documented default.
    """
    absent = C.setting_reason(C.SETTING_ABSENT, setting.key)
    invalid = C.setting_reason(C.SETTING_INVALID, setting.key)
    if raw is MISSING or raw is None:
        return float(setting.default), absent
    # ``bool`` is an ``int`` subclass: ``true`` is a config typo here, not the number 1.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return float(setting.default), invalid
    value = float(raw)
    if not math.isfinite(value) or value <= 0.0 or value > float(setting.maximum):
        return float(setting.default), invalid
    return value, ""


def read(setting: NumericSetting) -> Tuple[float, str]:
    """Resolve one setting through the bound reader; never raises, never returns a refused value."""
    absent = C.setting_reason(C.SETTING_ABSENT, setting.key)
    invalid = C.setting_reason(C.SETTING_INVALID, setting.key)
    raw: Any = MISSING
    if _reader is not None:
        try:
            raw = _reader(setting.key, MISSING)
        except Exception as exc:  # an unreadable config is treated as "not set"
            # The TYPE only: an exception body may quote the key it was given — a session key, a
            # chat id — which must never appear in a log line.
            logger.warning("proactive-conversation: reading plugin setting %s failed (%s)",
                           setting.key, type(exc).__name__)
            raw = MISSING
    value, reason = resolve_number(setting, raw)
    if reason == invalid:
        logger.warning(
            "proactive-conversation: plugin setting %s=%r is unusable (%s); using the default %rs",
            setting.key, raw, reason, setting.default,
        )
    elif reason == absent:
        logger.debug("proactive-conversation: plugin setting %s is not set (%s); using the default %rs",
                     setting.key, reason, setting.default)
    return value, reason


def value(setting: NumericSetting) -> float:
    """The effective value of one setting (see :func:`read` for the reason and the log)."""
    return read(setting)[0]


# ── non-numeric settings (the session declarations) ──────────────────────────────────────────
# A declaration list is not a number and has no documented default: the *shape* of the value (a list
# of well-formed declarations) is what ``proactive.declarations`` validates. This function is only
# the read path, and it keeps the same two distinctions as :func:`read`: "the key is not set"
# (``MISSING``) is told apart from any configured value, and an unreadable reader is "not set".


def read_raw(key: str) -> Any:
    """The raw configured value for ``key``; ``MISSING`` when unset, unreadable or no reader bound."""
    return read_raw_checked(key)[0]


def read_raw_checked(key: str) -> Tuple[Any, bool]:
    """``(raw_value, read_ok)`` — distinguishes "not set" from "the reader itself failed".

    ``read_raw`` collapses both into ``MISSING``, which is right for a knob whose documented default
    applies either way. The declaration source must not make that collapse: "the operator
    declared nothing" and "I could not read the declarations" lead to opposite verdicts for a
    session whose authorisation came from a declaration, so the caller gets the fact and fails
    closed on it.
    """
    if _reader is None:
        return MISSING, True  # no config access at all: the documented default, not a fault
    try:
        return _reader(key, MISSING), True
    except Exception as exc:  # an unreadable config is never "no declaration"
        logger.warning("proactive-conversation: reading plugin setting %s failed: %s", key,
                       type(exc).__name__)
        return MISSING, False

