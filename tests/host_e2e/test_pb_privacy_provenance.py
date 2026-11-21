"""A proactive turn may cite only a value it read **this round**, with a named **source** and
**time** (verified-citation-only).

What the criterion claims
-------------------------
It is deliberately weaker than a compose-time gate: there is **no compose-time filter**.
Enforcement is the injected prompt's instruction plus the tool deny list, so the criterion is not
"the model obeys" — it is whether the harness makes a **this-round, source+timestamp
attributable** value *distinguishable* from an invented (recalled / computed / interpolated /
rounded) one. This file tests exactly that: **citation provenance availability**, not message
wording.

How the fixture is real
-----------------------
* the data-source tool is a **fake** tool (no network, no live profile) but it is executed through the
  **real** execution-layer door, :class:`proactive.permissions.ToolExecutor` /
  :class:`proactive.permissions.ReadOnlyToolbox`, so a value only becomes citable if the gate actually
  *ran* a read tool this round and the value came straight back (no coercion, no rounding);
* the **provenance ledger** (the harness) is populated *only* from real ``ToolResult`` objects whose
  ``status == "ok"`` and whose payload carries an identifiable ``source`` + ``timestamp`` — a denied
  tool, a failed tool, or a value with no source records **nothing**;
* an **exact** match is required for a citation: a rounded / interpolated / perturbed value, and a
  value returned in a *previous* round, all have no this-round provenance and are **not** citable.

Measured posture
----------------
The central assertion — "a rounded / previous-round value is not citable" — is exactly what a naive
ledger would fail: one that matches on tolerance or on a whole-session history. Enforcement is not a
gate; the accepted residual (a model that ignores the instruction can still echo a value) is not
claimed here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

_TESTS_DIR = Path(__file__).resolve().parent.parent
for _p in (str(_TESTS_DIR), str(_TESTS_DIR / "host_e2e")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

import support  # noqa: E402  (pins a throwaway HERMES_HOME; keeps permissions import hermetic)

from proactive.permissions import (CAP_EGRESS, CAP_READ, CAP_WRITE, ReadOnlyToolbox, ToolCall,  # noqa: E402
                                   ToolExecutor, ToolSpec)


# ── the fake data-source tool: returns {value, source, timestamp} ──────────────────────────────


class FakeDataSourceTool:
    """A read-capability tool that returns one reading tagged with an identifiable source + time.

    ``handler`` is the real callable the executor invokes; ``calls`` proves it ran (and that a denied
    tool's handler did *not*).
    """

    def __init__(self, payload: Mapping[str, Any], *, name: str = "weather.read_temperature",
                 capability: str = CAP_READ):
        self.name = name
        self.capability = capability
        self.payload = dict(payload)
        self.calls: List[Dict[str, Any]] = []

    def handler(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(dict(kwargs))
        return dict(self.payload)


# ── the provenance ledger: this-round citation availability (the AC-PRIV-002 harness) ──────────


def _same(cited: Any, returned: Any) -> bool:
    """Strict identity for a citation: same type *and* same value.

    No tolerance and no cross-type equality, so ``7`` (a rounded int), ``7.0`` or ``7.4000001`` do
    not match a returned ``7.4``; rounding/interpolation cannot slip through as a citation.
    """
    return type(cited) is type(returned) and cited == returned


class ProvenanceLedger:
    """Records this round's citable readings, strictly from real tool returns."""

    def __init__(self) -> None:
        self._round = 0
        self._records: List[Dict[str, Any]] = []

    def begin_round(self) -> None:
        """Start a fresh proactive round: previous rounds' provenance does not count as this round's."""
        self._round += 1
        self._records.clear()

    def record(self, result: Any) -> Optional[Dict[str, Any]]:
        """Record one tool return as provenance **iff** it is an ok read with a source + timestamp."""
        if getattr(result, "denied", True):          # denied / failed / no result → nothing citable
            return None
        payload = getattr(result, "value", None)
        if not isinstance(payload, Mapping):
            return None
        value = payload.get("value")
        source = payload.get("source")
        timestamp = payload.get("timestamp")
        if value is None or not source or timestamp is None:
            return None                                # no identifiable source/time → not provenance
        rec = {"value": value, "source": source, "timestamp": timestamp,
               "tool": result.name, "round": self._round}
        self._records.append(rec)
        return rec

    def records(self) -> List[Dict[str, Any]]:
        """Every this-round provenance record (value + source + timestamp), for inspection/asserts."""
        return [dict(r) for r in self._records]

    def provenance_for(self, cited: Any) -> Optional[Dict[str, Any]]:
        """The this-round provenance a ``cited`` value can point to, or ``None``."""
        for rec in self._records:
            if _same(cited, rec["value"]):
                return dict(rec)
        return None

    def is_citable(self, cited: Any) -> bool:
        return self.provenance_for(cited) is not None


def _executor(*tools: FakeDataSourceTool) -> ToolExecutor:
    return ToolExecutor([ToolSpec(name=t.name, capability=t.capability, handler=t.handler) for t in tools])


_READING = {"value": 7.4, "source": "weather:temp:2026-11-16", "timestamp": "2026-11-17T07:03:00+08:00"}


# ── AC-PRIV-002 Then (first half): a this-round return with source+time is citable ────────────


def test_a_this_round_tool_return_is_citable_with_source_and_time():
    tool = FakeDataSourceTool(_READING)
    ledger = ProvenanceLedger()
    ledger.begin_round()
    result = _executor(tool).execute(ToolCall(name=tool.name, args={"date": "2026-11-16"}))
    assert result.status == "ok" and tool.calls == [{"date": "2026-11-16"}], "the read tool must have run"
    rec = ledger.record(result)
    assert rec is not None
    prov = ledger.provenance_for(7.4)
    assert prov is not None, "the returned value must have this-round provenance"
    assert prov["source"] == "weather:temp:2026-11-16"
    assert prov["timestamp"] == "2026-11-17T07:03:00+08:00"
    assert ledger.is_citable(7.4)


# ── AC-PRIV-002 Then (second half): an invented value has no source → not citable ─────────────


def test_a_rounded_value_has_no_provenance():
    tool = FakeDataSourceTool(_READING)
    ledger = ProvenanceLedger()
    ledger.begin_round()
    ledger.record(_executor(tool).execute(ToolCall(name=tool.name)))
    # 7.4 cited as 7 (rounded int) / 7.0 (rounded float) / 7.40 string — none is the exact return
    for invented in (7, 7.0, "7.4"):
        assert ledger.provenance_for(invented) is None, f"{invented!r} is a rounded/reformatted value"
        assert not ledger.is_citable(invented)


def test_an_interpolated_or_perturbed_value_has_no_provenance():
    tool = FakeDataSourceTool(_READING)
    ledger = ProvenanceLedger()
    ledger.begin_round()
    ledger.record(_executor(tool).execute(ToolCall(name=tool.name)))
    for invented in (7.4000001, 7.4 + 1e-9, 7.35):   # perturbation / interpolation
        assert not ledger.is_citable(invented)


def test_a_recalled_previous_round_value_is_not_citable_this_round():
    """The core "本轮返回可引、无本轮来源不可引": a value read in round N is not citable in round N+1."""
    tool = FakeDataSourceTool(_READING)
    ledger = ProvenanceLedger()

    ledger.begin_round()
    ledger.record(_executor(tool).execute(ToolCall(name=tool.name)))
    assert ledger.is_citable(7.4), "citable in the round that read it"

    # A new proactive round where the model recalls the same value without a tool call this round.
    ledger.begin_round()
    assert ledger.records() == [], "a fresh round starts with no provenance"
    assert ledger.provenance_for(7.4) is None, "a recalled value has no this-round source"
    assert not ledger.is_citable(7.4)


def test_a_value_from_a_denied_tool_is_never_citable_and_the_handler_never_runs():
    """A write/egress tool cannot manufacture provenance: the real gate denies it, handler not called."""
    payload = {"value": 9.9, "source": "forged", "timestamp": "2026-11-17T00:00:00+08:00"}
    write_tool = FakeDataSourceTool(payload, name="health.publish", capability=CAP_WRITE)
    egress_tool = FakeDataSourceTool(payload, name="health.share", capability=CAP_EGRESS)
    ex = _executor(write_tool, egress_tool)
    ledger = ProvenanceLedger()
    ledger.begin_round()

    for tool in (write_tool, egress_tool):
        result = ex.execute(ToolCall(name=tool.name))
        assert result.denied, "a non-read tool must be refused before its handler"
        assert ledger.record(result) is None
        assert tool.calls == [], "the denied tool's handler must never run (no side effect, no value)"
    assert not ledger.is_citable(9.9), "a value from a denied tool has no this-round provenance"


def test_compose_cites_only_the_measured_value_and_flags_the_fabricated_one():
    """End-to-end shape of AC-PRIV-002: of two values a message might carry, only the measured one has
    provenance; the fabricated one is detectable as unverified."""
    tool = FakeDataSourceTool(_READING)
    ledger = ProvenanceLedger()
    ledger.begin_round()
    ledger.record(_executor(tool).execute(ToolCall(name=tool.name)))

    measured = 7.4        # matches the tool return exactly
    fabricated = 8.0      # "computed"/rounded by the model, no this-round return
    citable = [v for v in (measured, fabricated) if ledger.is_citable(v)]
    assert citable == [measured], citable
    assert ledger.provenance_for(fabricated) is None
