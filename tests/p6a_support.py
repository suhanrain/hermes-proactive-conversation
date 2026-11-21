"""Support for the P6a source tests: real stores, real memory files, throwaway homes.

Everything here seeds through Hermes' *own* writes — ``SessionDB.create_session`` /
``append_message`` for the transcript, ``MemoryStore.add`` for the memory file — so the adapters
are always reading a format Hermes produced, never a hand-rolled stand-in. Importing ``support``
first (which this module does) pins the throwaway ``HERMES_HOME``, so every path below resolves
inside the isolated home: no test in this suite reads or writes a live profile.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import support  # noqa: F401  (importing it pins HERMES_HOME before any hermes module)

PLUGIN_PACKAGE = "proactive-conversation"


def iso_home() -> Path:
    """The throwaway hermes home this test session resolves against."""
    return Path(os.environ["HERMES_HOME"])


def db_path() -> Path:
    """The session database of that home — the same file hermes' own store opens."""
    return iso_home() / "state.db"


def session_db():
    """Hermes' real ``SessionDB`` for the isolated home (the writer of ``state.db``)."""
    from hermes_cli.goals import _get_session_db

    return _get_session_db()


def memory_dir() -> Path:
    """The isolated home's memory directory (``<HERMES_HOME>/memories``)."""
    from tools.memory_tool import get_memory_dir

    return Path(get_memory_dir())


def seed_transcript(db, *, session_id: str, session_key: Optional[str] = None,
                    profile: Optional[str] = None, source: str = "feishu",
                    rows: Sequence[Tuple[str, str, Dict[str, Any]]] = ()) -> str:
    """Create one session and append its rows through hermes' own writer.

    ``rows`` are ``(role, content, options)``: ``options`` may carry ``timestamp`` (the stored
    message time) and ``display_kind`` (the gateway's own marker for non-user turns). Both are
    written by the real ``append_message``, so the fixture cannot produce a row hermes could not.
    """
    db.create_session(session_id, source=source, session_key=session_key, profile_name=profile)
    for role, content, options in rows:
        db.append_message(session_id=session_id, role=role, content=content,
                          timestamp=options.get("timestamp"), display_kind=options.get("display_kind"))
    return session_id


def row_count(db, session_id: str) -> int:
    """How many message rows the session really holds (fixture bookkeeping, read through hermes)."""
    return len(db.get_messages(session_id) or [])


def write_memory(entries: Iterable[str], *, target: str = "memory", when: Optional[float] = None,
                 filename: Optional[str] = None) -> Path:
    """Write a memory file the way the memory tool does; return its path.

    ``target`` picks the file (``memory`` → ``MEMORY.md``, ``user`` → ``USER.md``); ``filename``
    overrides it when a test needs the *other* of the two files' name in the memory directory.
    ``when`` sets the file's mtime — the only recency anchor the format offers, so a test that
    works with a synthetic clock must move it too.
    """
    from tools.memory_tool_store import MemoryStore

    store = MemoryStore()
    entries = list(entries)
    if entries:
        result = store.add(target, entries[0])
        assert result.get("success") is not False, result
        for extra in entries[1:]:
            store.add(target, extra)
    path = memory_dir() / (filename or ("USER.md" if target == "user" else "MEMORY.md"))
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8-sig")
    if when is not None:
        os.utime(path, (float(when), float(when)))
    return path


def touch_memory(path: Path, when: float) -> Path:
    os.utime(path, (float(when), float(when)))
    return path


def memory_entries(path: Path) -> List[str]:
    """The entries of a memory file as *hermes* parses them (the drift reference for the adapter)."""
    from tools.memory_tool_store import MemoryStore

    return MemoryStore._parse_entries(MemoryStore._read_raw_checked(path)[0])


def scope_for(*, session_key: str, session_id: str, profile: Optional[str] = None) -> Any:
    """A content scope for a seeded session (the same shape ``Scope.from_dims`` produces)."""
    from proactive import content

    return content.Scope(session_key=session_key, session_id=session_id, profile=profile,
                         platform="feishu", chat_id="oc_p6a")


def text_sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()


def unique(*parts: Any) -> str:
    """A short unique tag so rows from one test cannot be picked up by another."""
    import uuid

    stem = "".join(ch for ch in "".join(str(p) for p in parts) if ch.isalnum())[:18]
    return f"{stem}{uuid.uuid4().hex[:8]}"
