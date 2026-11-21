"""proactive-conversation policy package — pure modules, no hermes imports.

Layout:

``contract``  frozen names, units, per-session metadata key, reason codes, plugin-setting keys
``config``    parse + validate the per-session configuration (missing/invalid => OFF)
``state``     the durable per-session runtime state (serialization + migration gate)
``settings``  the two plugin-level settings (default + bounds + fail-closed resolution)
``policy``    deterministic transitions: sampling, caps, DND, pause, and the single
              counting anchor ``commit_release``
``store``     read/write/clear the plugin-owned per-session metadata blob
``runtime``   the two hook callbacks the plugin registers (thin adapters over the above)
``content``   P4: assemble one citable recent record from the read-only source whitelist, or
              stay silent; owns the expression boundary
``permissions`` P4: the execution-layer tool boundary (capability gate + disabled write tools)

Nothing in this package reads a clock or an RNG implicitly: ``now`` and ``rng`` are arguments, and
``runtime`` exposes explicit seams (``set_clock``/``set_rng``/``set_content``/``reset_seams``) so
behaviour is reproducible in tests and in an incident. Plugin-level settings reach the hooks through
the bound plugin context (``runtime.bind_plugin_context``), never through a module-level snapshot.
"""

from __future__ import annotations

__all__ = ["contract", "config", "state", "settings", "policy", "store", "runtime", "content", "permissions"]
