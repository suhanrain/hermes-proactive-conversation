"""The write tools are isolated in the *execution layer*, not by the model's self-restraint.

Two write tools are offered to the model in a proactive round: write a memory, send a message.
Each carries a **real handler with its own counter**.

What the tests establish is the difference between the two claims:

* "the model was told not to" — nothing observable; and
* "the call could not execute" — the attempt is visible (the executor counted it), the refusal has a
  reason code, and the handler's own counter is still **0**.

The last part is the proof: an identical handler registered as a **read** tool does execute (count
1), so what stopped the write was the registry's capability classification — code, not politeness.
A request that *claims* ``read`` for a write tool is refused just the same: the claim is ignored.
"""

from __future__ import annotations

import json

import pytest

import p4_support as P
import support  # noqa: F401
from proactive import content, permissions
from proactive import contract as C


class CountingHandler:
    """A real callable that counts how often it actually ran (and can be made to raise)."""

    def __init__(self, name: str, *, boom: bool = False) -> None:
        self.name = name
        self.boom = boom
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        if self.boom:
            raise RuntimeError("side effect attempted")
        return f"{self.name}-result"


def build_boundary():
    """The injected write tools plus one genuine read tool."""
    handlers = {
        C.TOOL_WRITE_MEMORY: CountingHandler(C.TOOL_WRITE_MEMORY),
        C.TOOL_SEND_MESSAGE: CountingHandler(C.TOOL_SEND_MESSAGE),
    }
    read = CountingHandler("memory.search")
    tools = [
        permissions.ToolSpec(C.TOOL_WRITE_MEMORY, permissions.CAP_WRITE, handlers[C.TOOL_WRITE_MEMORY]),
        permissions.ToolSpec(C.TOOL_SEND_MESSAGE, permissions.CAP_EGRESS, handlers[C.TOOL_SEND_MESSAGE]),
        permissions.ToolSpec("memory.search", permissions.CAP_READ, read),
    ]
    return permissions.make_read_only_boundary(tools), handlers, read


WRITE_TOOLS = [C.TOOL_WRITE_MEMORY, C.TOOL_SEND_MESSAGE]


# ── the gate ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,capability,reason", [
    (C.TOOL_WRITE_MEMORY, permissions.CAP_WRITE, C.DENIED_WRITE_TOOL),
    (C.TOOL_SEND_MESSAGE, permissions.CAP_EGRESS, C.DENIED_SEND_TOOL),
])
def test_a_write_tool_attempt_is_refused_and_its_handler_never_runs(name, capability, reason):
    boundary, handlers, _read = build_boundary()
    result = boundary.executor.execute(permissions.ToolCall(name=name, args={"x": 1}))

    assert result.status == "denied"
    assert result.reason == reason
    assert result.capability == capability
    assert handlers[name].calls == 0, "the handler must not have been reached"
    counters = boundary.executor.counters()
    assert counters["attempts"][name] == 1, "the attempt is visible — the call did execute and was refused"
    assert counters["denied"][name] == 1 and counters["denied_reasons"][reason] == 1
    assert counters["executed"] == {}


def test_the_refusal_is_code_not_politeness_the_same_handler_runs_when_it_is_a_read_tool():
    """Identical handler, one difference: the registry's capability. That difference is the gate."""
    handler = CountingHandler("thing")
    read_boundary = permissions.make_read_only_boundary(
        [permissions.ToolSpec("thing", permissions.CAP_READ, handler)])
    result = read_boundary.executor.execute(permissions.ToolCall(name="thing"))
    assert result.status == "ok" and handler.calls == 1

    handler2 = CountingHandler("thing")
    write_boundary = permissions.make_read_only_boundary(
        [permissions.ToolSpec("thing", permissions.CAP_WRITE, handler2)])
    denied = write_boundary.executor.execute(permissions.ToolCall(name="thing"))
    assert denied.status == "denied" and denied.reason == C.DENIED_WRITE_TOOL
    assert handler2.calls == 0


def test_a_request_that_claims_to_be_a_read_is_still_refused():
    """The capability comes from the registry; the request's own claim is recorded and ignored."""
    boundary, handlers, _read = build_boundary()
    call = permissions.ToolCall(name=C.TOOL_SEND_MESSAGE, args={},
                               claimed_capability=permissions.CAP_READ)
    result = boundary.executor.execute(call)
    assert result.status == "denied" and result.reason == C.DENIED_SEND_TOOL
    assert handlers[C.TOOL_SEND_MESSAGE].calls == 0


def test_an_unknown_tool_cannot_run_either():
    boundary, _handlers, _read = build_boundary()
    result = boundary.executor.execute(permissions.ToolCall(name="shell.exec", args={"cmd": "rm -rf /"}))
    assert (result.status, result.reason) == ("denied", C.DENIED_UNKNOWN_TOOL)


def test_a_read_tool_handed_mutation_arguments_is_refused():
    """A read tool with a payload is a write in disguise: refused before the handler runs."""
    handler = CountingHandler("state.read")
    boundary = permissions.make_read_only_boundary(
        [permissions.ToolSpec("state.read", permissions.CAP_READ, handler)])
    for key in ("payload", "body", "content", "send", "amount"):
        result = boundary.executor.execute(permissions.ToolCall(name="state.read", args={key: "x"}))
        assert result.reason == C.DENIED_WRITE_ARGS
    assert handler.calls == 0
    assert boundary.executor.execute(permissions.ToolCall(name="state.read", args={"day": "2026-10-16"})).status == "ok"
    assert handler.calls == 1


def test_a_failing_read_tool_is_reported_not_silently_empty():
    boundary = permissions.make_read_only_boundary(
        [permissions.ToolSpec("memory.search", permissions.CAP_READ, CountingHandler("memory.search", boom=True))])
    result = boundary.executor.execute(permissions.ToolCall(name="memory.search"))
    assert result.status == "denied" and result.reason == C.TOOL_READ_FAILED
    assert result.value is None


# ── the disabled tools handed to the model ──────────────────────────────────────────────────


def test_the_disabled_write_tools_are_callable_and_harmless():
    """「可执行但被禁用」: a call returns a reason code; the wrapped handler is never invoked."""
    boundary, handlers, _read = build_boundary()
    disabled = boundary.disabled_by_name()
    assert set(disabled) == set(WRITE_TOOLS)
    for name, tool in disabled.items():
        result = tool(payload="写点东西")
        assert result.status == "denied"
        assert result.reason in (C.DENIED_WRITE_TOOL, C.DENIED_SEND_TOOL)
        assert tool.calls == 1, "the disabled tool itself was reached (it is executable)"
        assert handlers[name].calls == 0, "but the real handler was not"
    assert boundary.executor.counters()["attempts"] == {name: 1 for name in WRITE_TOOLS}


def test_the_read_only_toolbox_is_the_only_door_and_it_raises_on_a_write():
    boundary, handlers, read = build_boundary()
    assert boundary.toolbox.allowed_names() == ("memory.search",)
    assert boundary.toolbox.call("memory.search", day="2026-10-16").status == "ok"
    assert read.calls == 1
    with pytest.raises(permissions.WriteToolDenied) as exc:
        boundary.toolbox.call(C.TOOL_SEND_MESSAGE, payload="外发")
    assert exc.value.result.reason == C.DENIED_SEND_TOOL
    assert handlers[C.TOOL_SEND_MESSAGE].calls == 0


def test_the_assembly_path_gets_the_boundary_and_still_touches_no_write_tool():
    """One end-to-end path: sources are read, the injected write tools are disabled, nothing writes."""
    boundary, handlers, _read = build_boundary()
    assembly = P.run(P.all_topic_pair(), toolbox=boundary.toolbox,
                     injected_tools=tuple(boundary.disabled))
    assert assembly.ready
    assert [tool.name for tool in assembly.tools] == WRITE_TOOLS
    counters = boundary.executor.counters()
    assert counters.get("attempts", {}) == {}
    assert all(handlers[name].calls == 0 for name in WRITE_TOOLS)
    assert "只读" in assembly.material and "写与外部发送工具在本回合不可用" in assembly.material


def test_the_report_numbers_for_the_injected_write_tools():
    """The raw counters the acceptance report quotes (attempted → refused → handler calls)."""
    boundary, handlers, _read = build_boundary()
    for name in WRITE_TOOLS:
        boundary.executor.execute(permissions.ToolCall(name=name, args={"x": 1}))
    counters = boundary.executor.counters()
    assert counters["attempts"] == {C.TOOL_WRITE_MEMORY: 1, C.TOOL_SEND_MESSAGE: 1}
    assert counters["denied"] == counters["attempts"]
    assert counters["executed"] == {}
    assert counters["denied_reasons"] == {C.DENIED_WRITE_TOOL: 1, C.DENIED_SEND_TOOL: 1}
    assert {name: handlers[name].calls for name in WRITE_TOOLS} == {
        C.TOOL_WRITE_MEMORY: 0, C.TOOL_SEND_MESSAGE: 0}


def test_write_tool_boundary_builds_the_same_boundary_from_bare_handlers():
    handlers = {name: CountingHandler(name) for name in WRITE_TOOLS}
    boundary = permissions.write_tool_boundary(handlers)
    assert set(boundary.disabled_by_name()) == set(WRITE_TOOLS)
    for name in WRITE_TOOLS:
        boundary.disabled_by_name()[name](payload="x")
    assert all(handlers[name].calls == 0 for name in WRITE_TOOLS)
    assert set(boundary.executor.counters()["denied_reasons"]) <= {
        C.DENIED_WRITE_TOOL, C.DENIED_SEND_TOOL}


# ── privacy (the audit half; the log half is exercised in test_content_runtime) ─────────────


def test_the_audit_trail_carries_no_record_text_ids_health_values_or_credentials():
    adapters = (
        P.source(C.SOURCE_SESSION, [P.rec(C.SOURCE_SESSION, "task-8842", "18 号交房租",
                                          at=P.NOW - 30 * 60)]),
        P.source(C.SOURCE_MEMORY, [
            P.rec(C.SOURCE_MEMORY, "g-1", "HRV 28ms", sensitive=True),
            P.rec(C.SOURCE_MEMORY, "m-1", "这是一个秘密计划"),
        ]),
    )
    assembly = P.run(adapters)
    blob = json.dumps(assembly.audit, ensure_ascii=False, sort_keys=True)

    for forbidden in ("交房租", "task-8842", "HRV", "28ms", "秘密计划", "g-1", "m-1"):
        assert forbidden not in blob, f"{forbidden!r} leaked into the audit trail"
    assert assembly.audit["session"] not in P.scope().session_key
    assert len(assembly.audit["session"]) == 12
    assert assembly.audit["topic"]["source"] == C.SOURCE_SESSION
    assert assembly.audit["topic"]["record"] != "task-8842" and len(assembly.audit["topic"]["record"]) == 12


def test_the_session_digest_is_salted_and_not_a_plain_hash():
    import hashlib

    scope = P.scope(session_key="telegram:dm:42", session_id="sess-1")
    digest = scope.digest()
    assert len(digest) == 12 and digest.isalnum()
    assert "telegram" not in digest and "42" not in digest
    plain = hashlib.sha256(f"{scope.session_key}\x1f{scope.session_id}".encode()).hexdigest()[:12]
    assert digest != plain, "an unsalted digest would be joinable across processes"


def test_the_record_text_digest_is_recomputable_so_the_fingerprint_can_be_checked():
    record = P.rec(C.SOURCE_SESSION, "t1", "周五和老王吃饭")
    assert record.fingerprint() == f"session-transcript:t1:{content.text_digest('周五和老王吃饭')[:8]}"
    assert content.text_digest("周五和老王吃饭") == content.text_digest("周五和老王吃饭")


def test_an_assembly_error_is_silence_never_a_leak_or_a_crash():
    class Exploding(content.Assembler):
        def _assemble(self, scope, now):  # pragma: no cover - deliberate fault injection
            raise RuntimeError("assembler bug")

    assembly = Exploding((), content.ContentPolicy(recent_record_seconds=P.WINDOW)).assemble(
        P.scope(), P.NOW)
    assert assembly.silent and assembly.reason == C.SILENT_INTERNAL
    assert assembly.candidate == "" and assembly.material == ""
