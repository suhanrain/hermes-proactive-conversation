"""The one way a source in this package opens a database: read-only, or not at all.

``mode=ro`` in the SQLite URI is what makes the read-only claim a property of the connection rather
than a promise in a docstring: the database file is opened read-only by SQLite itself, so no
statement can write, no ``-wal`` is checkpointed away, and the journal is never deleted. A
read-only connection is also *the* reason the plugin may point at a live ``state.db`` at all —
measured evidence and the read-only assertion live in the delivery report (``[P6A-EVIDENCE]``
lines) and in ``tests/test_sources_real.py``.

``PRAGMA query_only=1`` is set on top of it as a second, connection-local lock: even if a future
SQLite build ignored the URI flag (or a caller passed a hand-built connection), a write attempt
still fails with ``OperationalError`` instead of landing. Neither the URI nor the pragma is a
guard against a *caller* holding another writable handle — that is out of this module's scope, and
nothing in this package ever opens one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


def read_only_uri(path: PathLike) -> str:
    """The ``file:...?mode=ro`` URI SQLite needs to open *path* without write access.

    Absolute POSIX paths need no escaping for this form (SQLite's URI parser takes everything up to
    the first ``?`` as the path), and a relative path is refused upstream: every adapter resolves
    its database path from a home directory before it gets here.
    """
    return f"file:{path}?mode=ro"


def open_read_only(path: PathLike) -> sqlite3.Connection:
    """Open *path* read-only. Raises :class:`sqlite3.Error` when the file cannot be opened at all.

    The caller turns that into ``unavailable:read-failed`` (see
    :mod:`proactive.sources.session_transcript`): a database that cannot be opened is an
    *unavailable source*, never an empty one.
    """
    conn = sqlite3.connect(read_only_uri(path), uri=True)
    conn.execute("PRAGMA query_only=1")
    return conn


def is_read_only(conn: sqlite3.Connection) -> bool:
    """``True`` when *conn* refuses writes (the adaptive assertion used by the tests).

    ``PRAGMA query_only`` reports the connection-local lock. A connection opened through
    :func:`read_only_uri` also reports ``1`` here, so the two halves of the claim are checked by
    one call.
    """
    try:
        return int(conn.execute("PRAGMA query_only").fetchone()[0]) == 1
    except sqlite3.Error:  # pragma: no cover - a connection that cannot answer is not read-only
        return False
