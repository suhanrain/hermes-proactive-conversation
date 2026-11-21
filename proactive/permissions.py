"""The assembly/generation path's tool boundary — enforced when a tool call is *executed*.

Why this module exists
----------------------
The product rule is「不主动调用会写入 / 支付 / 外发的工具」. A rule that lives in a prompt is not a
boundary: it holds only as long as the model chooses to obey. So the same rule is implemented here
as an execution-layer gate:

* every tool is registered with a **capability** taken from the registry (code), never from the
  request. A call that *claims* ``read`` for a write tool is still refused — the claim is ignored.
* a tool whose capability is not ``read`` is refused **before its handler is looked at**. The
  handler is a real, callable object with a counter of its own; the tests assert that counter stays
  0 while the attempt counter moves, which is precisely the difference between "the model was
  polite" and "the call could not execute".
* a read tool is additionally refused when its arguments look like a mutation (``denied:write-args``),
  and :class:`DisabledTool` is the shape handed to the generation path for write tools: callable,
  returns a denial, can never reach the handler.
* the boundary is the only way the path can run *any* tool (``ReadOnlyToolbox``), so there is no
  second door to slip through.
* the same rule also runs on **Hermes' own dispatch path**: the plugin's ``pre_tool_call`` callback
  (``proactive.runtime.pre_tool_call``) consults :func:`released_round_denial` for every tool call
  made inside a round this plugin released itself, and blocks it with a reason code. That is the gate
  a released proactive turn actually runs under — the toolbox alone only covers this package's own
  callers, while this callback sees every tool Hermes dispatches inside the round.

Nothing here reads a clock, a file, the network, or the live profile.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import contract as C

logger = logging.getLogger(__name__)

#: Capabilities. ``read`` is the only one the proactive path may use.
CAP_READ = "read"
CAP_WRITE = "write"
CAP_EGRESS = "egress"
CAP_PAYMENT = "payment"
#: A tool name this plugin cannot vouch for. Not ``read``, therefore refused — but reported as its
#: own class so the audit says "unknown", never "we know it writes".
CAP_UNKNOWN = "unknown"

#: Denial reason per non-read capability.
_DENY_REASON: Dict[str, str] = {
    CAP_WRITE: C.DENIED_WRITE_TOOL,
    CAP_EGRESS: C.DENIED_SEND_TOOL,
    CAP_PAYMENT: C.DENIED_PAYMENT_TOOL,
}

#: Argument names that mean "this call would change something" even on a read-capable tool.
#: A read tool that is handed a payload is not a read; it is a write in disguise.
WRITE_SHAPED_ARGS = frozenset({
    "payload", "body", "content", "text", "value", "data", "patch", "update", "delete",
    "append", "write", "create", "send", "post", "put", "commit", "amount", "charge", "pay",
})


class WriteToolDenied(Exception):
    """Raised by the toolbox when a refused call is made through the read-only door."""

    def __init__(self, result: "ToolResult") -> None:
        super().__init__(f"{result.name}: {result.reason}")
        self.result = result


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one (attempted) tool call. ``status`` is ``"ok"`` or ``"denied"``."""

    name: str
    status: str
    reason: str = ""
    capability: str = ""
    value: Any = None

    @property
    def denied(self) -> bool:
        return self.status == "denied"


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the registry knows it: name + capability (code) + the real handler."""

    name: str
    capability: str
    handler: Callable[..., Any]

    @property
    def read_only(self) -> bool:
        return self.capability == CAP_READ


@dataclass(frozen=True)
class ToolCall:
    """One requested call.

    ``claimed_capability`` is what the request says about itself. It is recorded for the audit and
    otherwise **ignored**: the registry's own classification decides.
    """

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    claimed_capability: Optional[str] = None
    origin: str = "generation-path"


class ToolExecutor:
    """Runs tool calls behind the capability gate and keeps the counters the report quotes."""

    def __init__(self, tools: Iterable[Any] = ()) -> None:
        self._specs: Dict[str, ToolSpec] = {}
        self._attempts: Dict[str, int] = {}
        self._executed: Dict[str, int] = {}
        self._denied: Dict[str, int] = {}
        self._denied_reasons: Dict[str, int] = {}
        for tool in tools or ():
            self.register(tool)

    # ── registration ────────────────────────────────────────────────────────────────────────

    def register(self, tool: Any) -> ToolSpec:
        """Register a tool. Accepts a :class:`ToolSpec` or any object carrying the same attributes."""
        spec = tool if isinstance(tool, ToolSpec) else ToolSpec(
            name=str(getattr(tool, "name", "")),
            capability=str(getattr(tool, "capability", CAP_WRITE)),
            handler=getattr(tool, "handler", tool),
        )
        if not spec.name:
            raise ValueError("a tool without a name is not registrable")
        self._specs[spec.name] = spec
        return spec

    def specs(self) -> Dict[str, ToolSpec]:
        return dict(self._specs)

    def allowed_names(self) -> Tuple[str, ...]:
        """The names this boundary will actually run (read capability only)."""
        return tuple(sorted(name for name, spec in self._specs.items() if spec.read_only))

    def denied_names(self) -> Tuple[str, ...]:
        return tuple(sorted(name for name, spec in self._specs.items() if not spec.read_only))

    # ── the gate ────────────────────────────────────────────────────────────────────────────

    def execute(self, call: Any) -> ToolResult:
        """Attempt one call. Refusals happen here — before the handler, never by the caller."""
        name = str(getattr(call, "name", "") or "")
        args = getattr(call, "args", None) or {}
        self._attempts[name] = self._attempts.get(name, 0) + 1
        spec = self._specs.get(name)
        if spec is None:
            return self._deny(name, C.DENIED_UNKNOWN_TOOL, capability="")
        if not spec.read_only:
            # The registry says this tool writes / pays / sends. The request's own claim is ignored
            # and the handler is not touched: no side effect can happen from a refused call.
            reason = _DENY_REASON.get(spec.capability) or C.DENIED_WRITE_TOOL
            return self._deny(name, reason, capability=spec.capability)
        bad = _write_shaped_args(args)
        if bad:
            return self._deny(name, C.DENIED_WRITE_ARGS, capability=spec.capability, detail=bad)
        try:
            value = spec.handler(**dict(args)) if args else spec.handler()
        except Exception as exc:
            logger.warning("proactive-conversation: read tool %s failed (%s)", name, type(exc).__name__)
            return ToolResult(name=name, status="denied", reason=C.TOOL_READ_FAILED,
                              capability=spec.capability)
        self._executed[name] = self._executed.get(name, 0) + 1
        return ToolResult(name=name, status="ok", reason="", capability=spec.capability, value=value)

    def _deny(self, name: str, reason: str, *, capability: str, detail: str = "") -> ToolResult:
        self._denied[name] = self._denied.get(name, 0) + 1
        self._denied_reasons[reason] = self._denied_reasons.get(reason, 0) + 1
        logger.info("proactive-conversation: tool %s refused (%s)", name, reason)
        return ToolResult(name=name, status="denied", reason=reason, capability=capability,
                          value=detail or None)

    # ── counters (the raw numbers a report quotes) ──────────────────────────────────────────

    def counters(self) -> Dict[str, Any]:
        return {
            "attempts": dict(self._attempts),
            "executed": dict(self._executed),
            "denied": dict(self._denied),
            "denied_reasons": dict(self._denied_reasons),
            "allowed": list(self.allowed_names()),
            "refused": list(self.denied_names()),
        }

    def reset_counters(self) -> None:
        self._attempts.clear()
        self._executed.clear()
        self._denied.clear()
        self._denied_reasons.clear()


def _write_shaped_args(args: Any) -> str:
    if not isinstance(args, Mapping):
        return ""
    for key in args:
        if str(key).lower() in WRITE_SHAPED_ARGS:
            return str(key)
    return ""


class ReadOnlyToolbox:
    """The generation path's only tool door: read tools run, everything else is refused."""

    def __init__(self, executor: ToolExecutor) -> None:
        self._executor = executor

    @property
    def executor(self) -> ToolExecutor:
        return self._executor

    def allowed_names(self) -> Tuple[str, ...]:
        return self._executor.allowed_names()

    def call(self, name: str, **args: Any) -> ToolResult:
        """Run a read tool, or raise :class:`WriteToolDenied` — there is no silent failure."""
        result = self._executor.execute(ToolCall(name=name, args=args, origin="read-only-toolbox"))
        if result.denied:
            raise WriteToolDenied(result)
        return result


class DisabledTool:
    """A **callable but disabled** write tool, as handed to the assembly/generation path.

    Calling it is possible (so a model that tries gets a definite answer instead of a crash) and it
    always returns a denial with the capability's reason code. The wrapped handler is never called,
    which the tests assert by counting on both sides.
    """

    def __init__(self, spec: ToolSpec, *, executor: Optional[ToolExecutor] = None) -> None:
        self.spec = spec
        self._executor = executor
        self.calls = 0

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def capability(self) -> str:
        return self.spec.capability

    def __call__(self, *args: Any, **kwargs: Any) -> ToolResult:
        claimed = kwargs.pop("capability", None)
        self.calls += 1
        if self._executor is not None:
            return self._executor.execute(ToolCall(name=self.spec.name, args=kwargs,
                                                   claimed_capability=claimed,
                                                   origin="disabled-tool"))
        reason = _DENY_REASON.get(self.spec.capability) or C.DENIED_WRITE_TOOL
        return ToolResult(name=self.spec.name, status="denied", reason=reason,
                          capability=self.spec.capability)


@dataclass(frozen=True)
class Boundary:
    """One execution-layer boundary: the executor, its read-only door, and the disabled write tools."""

    executor: ToolExecutor
    toolbox: ReadOnlyToolbox
    disabled: Tuple[DisabledTool, ...] = ()

    def disabled_by_name(self) -> Dict[str, DisabledTool]:
        return {tool.name: tool for tool in self.disabled}


def make_read_only_boundary(tools: Iterable[Any]) -> Boundary:
    """Build the boundary the assembly/generation path runs with.

    Read tools end up in the toolbox; every non-read tool ends up as a :class:`DisabledTool`. Both
    are views over the same :class:`ToolExecutor`, so there is exactly one gate and one set of
    counters.
    """
    specs: List[Any] = []
    for tool in tools or ():
        if isinstance(tool, ToolSpec):
            specs.append(tool)
        else:
            specs.append(ToolSpec(name=str(getattr(tool, "name", "")),
                                  capability=str(getattr(tool, "capability", CAP_WRITE)),
                                  handler=getattr(tool, "handler", tool)))
    executor = ToolExecutor(specs)
    disabled: List[DisabledTool] = []
    for spec in specs:
        if not spec.read_only:
            disabled.append(DisabledTool(spec, executor=executor))
    return Boundary(executor=executor, toolbox=ReadOnlyToolbox(executor), disabled=tuple(disabled))


def write_tool_boundary(handlers: Mapping[str, Callable[..., Any]],
                        capabilities: Optional[Mapping[str, str]] = None) -> Boundary:
    """The injected write tools (write memory / send a message).

    ``handlers`` maps a tool name to a real callable whose own counter proves it never ran; when
    ``capabilities`` is omitted the standard mapping is used (write / egress).
    """
    caps = dict(capabilities or {
        C.TOOL_WRITE_MEMORY: CAP_WRITE,
        C.TOOL_SEND_MESSAGE: CAP_EGRESS,
    })
    return make_read_only_boundary(
        ToolSpec(name=name, capability=caps.get(name, CAP_WRITE), handler=handler)
        for name, handler in (handlers or {}).items()
    )


# ── the gate on Hermes' own dispatch path (``pre_tool_call``) ───────────────────────────────
#
# The rule「不主动调用会写入 / 支付 / 外发的工具」has to hold on the path a *real* tool call takes, not
# only inside this package's toolbox. Hermes dispatches ``pre_tool_call`` immediately before every
# tool execution — built-ins and plugin tools alike — and turns a ``{"action": "block", ...}`` return
# into that call's result, so a released round actually runs under the decision below.
#
# Classification is code and the direction is deliberate: a name is ``read`` **only** when this table
# says so (or when this package's own registry classified the tool ``read``). Everything else,
# including a name nobody here has heard of, is not read and is therefore refused.
#
# The lists are read-only *observations*: no entry writes, sends, pays, spawns a child agent, drives
# a browser or terminal, or mutates the board. Adding a name here is the only way to widen the gate,
# so the list stays short on purpose.

#: Tool names a released round may still use.
READ_TOOL_NAMES = frozenset({
    "read_file",          # observe one file
    "read_terminal",      # observe a terminal's buffer
    "read_window_below",  # observe the desktop
    "search_files",       # find files / search their contents
    "web_search",         # look something up
    "web_extract",        # read a page
    "x_search",
    "session_search",     # look up own past conversations
    "skill_view",
    "skills_list",
    "vision_analyze",     # read an existing image
    "video_analyze",
    "todo_list",
    "kanban_show",
    "kanban_list",
    "kanban_attachments",
})

#: Names whose capability is known to be ``write``: they are refused either way (they are not in the
#: read list), and naming them keeps the reason code honest (``denied:write-tool``, not
#: ``denied:unknown-tool``).
WRITE_TOOL_NAMES = frozenset({
    "write_file", "patch", "terminal", "execute_code", "process_manage", "computer_use",
    "cronjob_manage", "skill_manage", "memory", "delegate_task", "manage_connections",
    "manage_catalog", "start_chat", "focus_pane", "close_terminal", "gui_tour", "setup_choose",
    "annotate_preview", "apply_layout", "image_generate", "video_generate", "browser_exec",
    "browser_cdp", "browser_console", "browser_type", "browser_click", "browser_press",
    "browser_navigate", "browser_dialog", "browser_scroll", "browser_back",
    "browser_vault_fill", "browser_vault_save_login",
    "browser_vault_unlock", "browser_vault_enter_code",
    "kanban_create", "kanban_comment", "kanban_complete", "kanban_block", "kanban_unblock",
    "kanban_link", "kanban_schedule", "kanban_attach", "kanban_attach_url",
    "kanban_request_changes", "kanban_request_review", "kanban_heartbeat",
})

#: Names whose capability is known to be ``egress``: they put something in front of another human
#: (``denied:send-tool``).
EGRESS_TOOL_NAMES = frozenset({
    C.TOOL_SEND_MESSAGE, "text_to_speech", "react_to_message", "discord", "discord_admin",
    "yb_send_dm", "yb_send_sticker", "feishu_drive_add_comment", "feishu_drive_reply_comment",
})

#: Capability of the tools this package itself injects (the write / send pair), so the
#: toolbox gate and the dispatch gate read one classification instead of two that can drift.
KNOWN_TOOL_CAPABILITIES: Dict[str, str] = {
    C.TOOL_WRITE_MEMORY: CAP_WRITE,
    C.TOOL_SEND_MESSAGE: CAP_EGRESS,
}


def classify_tool(name: str, *, specs: Optional[Mapping[str, "ToolSpec"]] = None) -> str:
    """The capability of *name*: ``read`` only when code says so, else a non-read class.

    ``specs`` is this package's own registry (``ToolExecutor.specs()``) when a boundary is installed;
    a spec wins over the name tables because it is the more specific statement.
    """
    key = str(name or "")
    spec = (specs or {}).get(key)
    if spec is not None:
        return spec.capability
    known = KNOWN_TOOL_CAPABILITIES.get(key)
    if known:
        return known
    if key in READ_TOOL_NAMES:
        return CAP_READ
    if key in WRITE_TOOL_NAMES:
        return CAP_WRITE
    if key in EGRESS_TOOL_NAMES:
        return CAP_EGRESS
    return CAP_UNKNOWN


def released_round_denial(tool_name: str, *,
                          specs: Optional[Mapping[str, "ToolSpec"]] = None) -> Optional[ToolResult]:
    """The ONE decision for a tool call inside a released round: ``None`` to proceed, else refusal.

    The caller decides *whether* the call belongs to such a round; this function only decides what
    capability the tool has. Nothing is read from the request — a call cannot argue its way in.
    """
    name = str(tool_name or "")
    capability = classify_tool(name, specs=specs)
    if capability == CAP_READ:
        return None
    reason = _DENY_REASON.get(capability) or C.DENIED_UNKNOWN_TOOL
    return ToolResult(name=name or "?", status="denied", reason=reason, capability=capability)


def block_directive(result: ToolResult) -> Dict[str, str]:
    """The ``pre_tool_call`` block directive for a refusal (non-empty ``message`` is required)."""
    return {
        "action": "block",
        "message": f"{C.ROUND_GATE_MESSAGE_PREFIX} {result.reason} "
                   f"{C.ROUND_GATE_MESSAGE_SUFFIX}",
    }
