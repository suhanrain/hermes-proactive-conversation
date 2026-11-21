"""Is this plugin running inside hermes' plugin host (``plugins.isolation: host``)?

Why this module exists. ``plugins.isolation: host`` keeps third-party
plugin code out of the hermes process: the plugin is loaded in a plugin-host **child process** and
every value that crosses the boundary is degraded (``hermes_cli/plugin_host_wire``). ``gateway/
run_goals.py`` stamps a heartbeat wake with the dynamic attribute ``event._heartbeat_session_id``,
but the wire codec carries a dataclass as its **declared fields only**, so the stamp is gone; the
live gateway runner and session store arrive as ``Opaque`` placeholders. A hook handed that shape
cannot tell a heartbeat wake from a user message and cannot read the session's policy, so it can
neither honour the "default off" guarantee nor refuse a wake safely — refusing everything would
block ordinary user traffic, which this plugin must never do.

So the degradation is handled where it *is* decidable:

* **load time** — :func:`refuse_in_plugin_host` raises, i.e. the plugin refuses to arm at all rather
  than pretending to police traffic it cannot see;
* **hook time** — :func:`degraded_shape` recognises a placeholder-shaped delivery and
  ``proactive.runtime`` logs it (``degraded:plugin-host``) instead of deciding anything. A wake that
  already reaches the lane under this degradation is **not released by this plugin** — nothing was
  released on a guess, because a guess is not available: the only payload difference between a
  heartbeat wake and a real user message in the host is ``message_id`` (``None`` vs the platform's
  id), and a goal continuation (``gateway/run_goals.py`` builds it with the same
  ``_synthetic_prompt_event``) carries ``message_id = None`` too. ``message_id`` therefore marks
  *synthetic prompt*, not *heartbeat*, and this plugin was never authorised to govern every
  synthetic prompt.

Measured surfaces:

* ``HERMES_PLUGIN_HOST_PROCESS=1`` in ``os.environ`` — set by ``hermes_cli/plugin_host`` for the
  child, and therefore visible to ``register()`` itself, because the load is an RPC served *inside*
  the child (``plugin_host_child.op_load``). This is the primary detector.
* the ``ctx`` handed to ``register()`` is ``RemotePluginContext`` there and ``PluginContext``
  in-process (its public surface collapses to ``manifest``/``on_unload``/``plugin_id``/
  ``profile_name``/``spawn_task``) — a corroborating detector that does not depend on the env var.
* at hook time: a dataclass argument arrives as ``Record`` (a ``dict``) and a live handle as
  ``Opaque``.

Measured **non**-detectors:

* ``hermes_cli.plugin_isolation.isolation_mode()`` answers ``"in_process"`` inside the host by
  design (it keys off the same env var, in the opposite direction);
* a missing ``_heartbeat_session_id`` on its own — a real user message does not carry it either;
* ``hermes plugins validate``'s "Isolation" line — a static source audit, byte-identical under both
  ``plugins.isolation`` values, and ``hermes plugins doctor`` always loads the plugin in-process
  (its temporary home has no ``isolation`` key), so neither command can report the live mode.

No hermes import lives here: the capability probe imports this package in a bare interpreter, so
detection is duck-typed on names and environment only. The names themselves, and the process
environment read, live in the host adapter; this module decides.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from . import contract as C
from . import host_adapter as _adapter

logger = logging.getLogger(__name__)

#: Environment variable ``hermes_cli.plugin_host.PluginHost._env`` sets for the plugin-host child.
HOST_PROCESS_ENV = _adapter.HOST_PROCESS_ENV

#: The concrete ``ctx`` class the host hands ``register()`` (``hermes_cli.plugin_host_child``).
HOST_CTX_TYPE = _adapter.HOST_CONTEXT_TYPE

#: Wire-codec class names (``hermes_cli/plugin_host_wire.py``): a dataclass becomes ``Record``, any
#: live handle becomes ``Opaque``.
WIRE_RECORD_TYPE = _adapter.WIRE_RECORD_TYPE
WIRE_PLACEHOLDER_TYPE = _adapter.WIRE_PLACEHOLDER_TYPE


def in_plugin_host(environ: Optional[Mapping[str, str]] = None) -> bool:
    """True when the env flag only hermes' plugin-host child carries is set (``=1``).

    The flag's name and the ``os.environ`` read live in the host adapter; this module keeps the
    decision.
    """
    return _adapter.host_process_flag(environ)


def is_host_context(ctx: Any) -> bool:
    """True when ``ctx`` is the plugin-host facade rather than hermes' in-process context."""
    return ctx is not None and type(ctx).__name__ == HOST_CTX_TYPE


def plugin_host_signature(ctx: Any = None,
                          environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Which detector fired, as a short greppable string; ``None`` when this is not the host.

    Both detectors are load-time facts and both are cheap: the env flag is what hermes actually sets
    (``hermes_cli/plugin_host.py``), the context class is what it actually passes, and neither can be
    true in the in-process loader.
    """
    if in_plugin_host(environ):
        return f"env:{HOST_PROCESS_ENV}=1"
    if is_host_context(ctx):
        return f"ctx:{HOST_CTX_TYPE}"
    return None


def is_placeholder(value: Any) -> bool:
    """True for the wire codec's stand-in for a live object the other process owns."""
    return value is not None and type(value).__name__ == WIRE_PLACEHOLDER_TYPE


def is_wire_record(value: Any) -> bool:
    """True for a dataclass that crossed the wire (``Record``: a ``dict`` with attribute access)."""
    return isinstance(value, dict) and type(value).__name__ == WIRE_RECORD_TYPE


def degraded_shape(event: Any = None, gateway: Any = None, session_store: Any = None) -> Optional[str]:
    """The observed degradation of one hook call, as ``"kind:Detail"`` parts; ``None`` when clean.

    Hook-time companion of :func:`plugin_host_signature`: it cannot prevent the degradation (that is
    load time's job), it only makes it visible at the moment it happens, so a hook that is somehow
    armed while its arguments are placeholders says so instead of silently deciding nothing.
    """
    parts = []
    if is_wire_record(event):
        parts.append(f"event:{WIRE_RECORD_TYPE}")
    elif isinstance(event, dict):
        parts.append("event:dict")
    if is_placeholder(gateway):
        parts.append(f"gateway:{WIRE_PLACEHOLDER_TYPE}")
    if is_placeholder(session_store):
        parts.append(f"session_store:{WIRE_PLACEHOLDER_TYPE}")
    return ",".join(parts) or None


def refusal_text(signature: str) -> str:
    """The one message an operator sees at load time (log ERROR and the loader's failure record)."""
    return (
        f"proactive-conversation cannot run under plugins.isolation: host ({signature}, "
        f"{C.REASON_DEGRADED_PLUGIN_HOST}): in the plugin host a heartbeat wake cannot be told apart "
        f"from a user message — the event crosses the wire as a Record, so its _heartbeat_session_id "
        f"stamp is lost and the gateway runner / session store arrive as Opaque placeholders — so the "
        f"per-session policy cannot be read and this plugin can hold no guarantee at all: refusing "
        f"every round would block ordinary user messages, and releasing every round would send "
        f"proactive wakes out of sessions the user never enabled. It therefore registers NO hook and "
        f"this load fails, instead of appearing to work. Fix: set plugins.isolation: in_process in "
        f"this profile's config.yaml and reload (hermes plugins show proactive-conversation prints the mode "
        f"it sees)."
    )


def refuse_in_plugin_host(ctx: Any = None, *,
                          environ: Optional[Mapping[str, str]] = None) -> None:
    """Fail closed at load time: never arm a gate this process boundary cannot honour.

    Raises :class:`PluginHostIsolationRefused` (after an ERROR log) when the plugin was loaded into
    the plugin host. Failing the load is deliberate: hermes' own loader then records the reason on
    the plugin (``loaded.error``, plus a ``Failed to load plugin ...`` warning in the log), which is
    this package's only user-visible surface under host isolation — ``hermes plugins doctor``
    deliberately loads the plugin in-process and can never see this mode.
    """
    signature = plugin_host_signature(ctx, environ)
    if signature is None:
        return
    message = refusal_text(signature)
    logger.error("proactive-conversation: %s", message)
    raise PluginHostIsolationRefused(message)


class PluginHostIsolationRefused(RuntimeError):
    """``register()`` refused to arm because hermes loaded the plugin into the plugin host."""


__all__ = [
    "HOST_CTX_TYPE", "HOST_PROCESS_ENV", "PluginHostIsolationRefused", "WIRE_PLACEHOLDER_TYPE",
    "WIRE_RECORD_TYPE", "degraded_shape", "in_plugin_host", "is_host_context", "is_placeholder",
    "is_wire_record", "plugin_host_signature", "refuse_in_plugin_host", "refusal_text",
]
