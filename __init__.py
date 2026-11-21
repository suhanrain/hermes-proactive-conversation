"""proactive-conversation — Hermes plugin entry point.

Registered hooks (all four declared in ``plugin.yaml``):

* ``pre_gateway_dispatch`` — the eligibility gate. Runs before auth and before any LLM work. For a
  heartbeat wake it evaluates the session's declaration and content rules; a permitted wake is
  dropped with ``silent:consume-lane-retired`` (this plugin does not self-generate a message). For a
  real user message it advances the session's silence-window state and always lets the round through.
* ``pre_llm_call`` — recognises the proactive turn the trigger opened and binds its ``turn_id``, so
  the text and tool brakes apply to that turn and no other. Returns ``None``: it injects no context.
* ``transform_llm_output`` — the text brake. For a bound or released round it returns ``[SILENT]``
  when the policy no longer allows the round; otherwise the host's text is delivered and the round
  is settled once. An ordinary turn is never touched.
* ``pre_tool_call`` — the execution-layer tool gate. Only for a round this plugin released: every
  non-read tool call is blocked before it executes, with a reason code. A round the plugin did not
  release has no ticket, so an ordinary conversation is never affected.
"""

from __future__ import annotations

import logging

__all__ = ["register"]

logger = logging.getLogger(__name__)



def register(ctx) -> None:
    """Register the four hooks declared in ``plugin.yaml``, and bind ``ctx`` for the plugin-level
    settings.

    Nothing else is registered except the read-only ``/proactive-check`` slash command (and its
    optional ``hermes proactive-check`` CLI twin), both of which are pure self-checks. ``ctx.get_config``
    is only *captured* here (never called at import/registration time), so the plugin still works
    against a recording context that does not expose it.

    Raises :class:`proactive.isolation.PluginHostIsolationRefused` — i.e. registers nothing at all —
    when this load happened inside hermes' plugin host. hermes' loader turns that into
    ``loaded.error`` and a ``Failed to load plugin`` warning, which is the only surface on which the
    user can see why the plugin is inert under that isolation mode.
    """
    from .proactive import host_adapter, isolation, runtime

    isolation.refuse_in_plugin_host(ctx)

    runtime.bind_plugin_context(ctx)
    # P9: install the configured read-only composition into this plugin's own runtime module. It
    # happens here so the assembly is in place before the first round, and is re-read on every hook
    # call afterwards (a config edit applies to the next round). An unusable/absent value installs
    # nothing and makes every wake fail closed — it never raises, so registration still succeeds.
    runtime.refresh_content()
    # The hook names come from the host adapter's single table (seam: hook surface) and the
    # registration call itself is the adapter's too (seam 13), so a host-side change is a one-file
    # edit there and not a hunt through this package. The table is a generator on purpose: the
    # callback is resolved hook by hook, exactly as the old loop did, so a missing runtime attribute
    # still fails after registering the hooks before it rather than before registering any.
    host_adapter.register_hooks(
        ctx, ((hook_name, getattr(runtime, hook_name)) for hook_name in host_adapter.hook_names()))

    # Read-only in-session self-check (card t_ee7ccaa7): ``/proactive-check [--json]`` validates the
    # currently-effective ``session_declarations`` with the same validator the runtime uses, without
    # writing or delivering anything. It reuses the host adapter's ``ctx.get_config`` read path. Both
    # registration surfaces are optional: a recording context (the capability probe) or an older host
    # without ``register_command`` simply keeps the hooks and skips the command.
    from .proactive import check as _check

    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        try:
            register_command(
                "proactive-check",
                _check.handle_check,
                description="Validate proactive-conversation session_declarations and show effective caps/tools.",
                args_hint="[--json]",
            )
        except Exception as exc:  # noqa: BLE001 - a command must not abort hook registration
            logger.warning("proactive-conversation: could not register /proactive-check (%s)",
                           type(exc).__name__)

    register_cli_command = getattr(ctx, "register_cli_command", None)
    if callable(register_cli_command):
        try:
            register_cli_command(
                "proactive-check",
                help="Validate proactive-conversation session_declarations and show effective caps/tools.",
                setup_fn=_check.cli_setup,
                handler_fn=_check.cli_run,
                description="Read-only dry-run of the plugin's session_declarations.",
            )
        except Exception as exc:  # noqa: BLE001 - cosmetic CLI surface; never fatal
            logger.warning("proactive-conversation: could not register 'hermes proactive-check' (%s)",
                           type(exc).__name__)
