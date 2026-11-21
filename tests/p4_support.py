"""Shared content-assembly helpers: fictional records, injected adapters, one assembly run.

Everything here is fake by construction — no real memory API is called, no
credential is read, no network is touched. The adapters are ordinary objects injected into the
assembly path, which is how the read-only sources are exercised here (real read APIs are replaced
by fixtures and injected adapters).
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

import support  # noqa: F401  (importing it pins HERMES_HOME before any hermes module loads)
from proactive import content, contract as C

#: A fixed "now" (2026-10-16 12:00 Asia/Shanghai) so freshness is readable in the fixtures.
NOW = support.epoch("Asia/Shanghai", 2026, 10, 16, 12, 0)

#: The freshness window every fixture supplies: the assembly has no default for it and never invents one.
WINDOW = 48 * 3600

HOUR = 3600.0


def scope(session_key: str = "telegram:dm:42", session_id: str = "sess-1",
          profile: str = "dev", chat_id: str = "42") -> content.Scope:
    """The scope an assembly run may read for."""
    return content.Scope(session_key=session_key, session_id=session_id, profile=profile,
                         platform="telegram", chat_id=chat_id, thread_id=None)


def rec(source: str, record_id: str, text: str, **kw: Any) -> content.Record:
    """One fictional record. Defaults: about the user, one hour old, not sensitive."""
    kw.setdefault("at", NOW - HOUR)
    kw.setdefault("subject", "user")
    return content.Record(source=source, record_id=record_id, text=text, **kw)


def source(name: str, records: Sequence[content.Record] = (), **kw: Any) -> content.StaticSource:
    """A whitelisted adapter over fictional records (``failure=`` makes it 403/timeout)."""
    return content.StaticSource(name=name, records=tuple(records), **kw)


def assembler(adapters: Iterable[Any] = (), *, window: Optional[float] = WINDOW,
              **kw: Any) -> content.Assembler:
    """An assembler with the fixture window (health records are always excluded)."""
    policy = content.ContentPolicy(recent_record_seconds=window)
    return content.Assembler(tuple(adapters), policy, **kw)


def run(adapters: Iterable[Any] = (), *, window: Optional[float] = WINDOW,
        sc: Optional[content.Scope] = None, now: float = NOW,
        **kw: Any) -> content.Assembly:
    """One assembly run with fixture defaults."""
    return assembler(adapters, window=window, **kw).assemble(
        sc if sc is not None else scope(), now)


def release_material(clock: Any) -> content.Assembler:
    """The assembly a *configured* profile has — for harnesses that measure a **released** round.

    An installed assembly is the precondition of a release: the released round's only allowed
    material is the assembled candidate, so a wake with no assembly is refused
    (``off:content-unconfigured``). Harnesses whose subject is the release/delivery layer therefore
    install this stand-in instead of running the real local sources — one ordinary, recent,
    user-authored memory record. ``clock`` is the harness clock callable, so the record is always "an
    hour before the round" and never becomes stale when a test moves the clock to make a window due.
    """
    class _ClockRelativeSource(content.StaticSource):
        """One ordinary memory record, placed an hour before the round being decided."""

        def read(self, scope):  # noqa: ANN001 - the adapter protocol
            self.calls += 1
            return (content.Record(source=C.SOURCE_MEMORY, record_id="t-fixture",
                                   text="周五和老王吃饭", at=float(clock()) - HOUR,
                                   subject="user"),)

    return content.Assembler((_ClockRelativeSource(name=C.SOURCE_MEMORY),),
                             content.ContentPolicy(recent_record_seconds=WINDOW))


def all_topic_pair() -> tuple:
    """Two ordinary memory records: the everyday 'there is a usable topic' case.

    The newer one (``t-2``) is the topic, the older one (``t-1``) is still inside the window.
    """
    return (source(C.SOURCE_MEMORY, [
        rec(C.SOURCE_MEMORY, "t-1", "周五和老王吃饭", at=NOW - 5 * HOUR),
        rec(C.SOURCE_MEMORY, "t-2", "18 号交房租", at=NOW - 1 * HOUR),
    ]),)
